# sig-bot deployment runbook

Deploy scaffolding for running **sig-bot** 24/7 unattended on a 1-core / 1 GB Linux droplet
(Oct–Nov 2026). Adapts mbappe's proven systemd patterns, **strips** the equities/market-hours parts
(SIG is continuous), and **adds** the two safety nets mbappe's scan flagged as missing: a **flock
single-instance guard** and a **systemd `WatchdogSec` out-of-process watchdog**.

Build-only artifact: everything here lives under `deploy/`. The process entry points it targets
(`main_sig.py run|feed|signal|supervisor|reconcile|pre-event-check|post-settlement`, the `/health`
sidecar `api/main.py`, the flock in `infra/single_instance.py`, `scripts/external_watchdog.py`) are
**module-owned** — these units are written to that invocation contract.

---

## Process model (settled)

ONE capital-spending process + separate non-capital helpers, coordinating through shared local
SQLite (`data/trading.db`). One trader = one capital arbiter (no cross-process capital locking
needed — see the mbappe recon). Units:

| unit | role | capital? | restart | key hardening |
|---|---|---|---|---|
| `sig-bot.service` | **TRADER** (single) | **yes** | `always`, 10s | **flock in ExecStart + `WatchdogSec=90`** |
| `sig-bot-feed.service` | market-data feed | no | `always`, 5s | `After=` trader (soft) |
| `sig-bot-signal.service` | research/signal engine | no | `always`, 10s | `After=` trader (soft) |
| `sig-bot-supervisor.service` | continuous liveness monitor | no | `always`, 15s | independent of trader |
| `sig-bot-health.service` | FastAPI `/health` sidecar (localhost:8787) | no | `always`, 10s | isolated |
| `sig-bot-watchdog.{service,timer}` | external dead-man (periodic) | no | timer | catches startup crash |
| `sig-bot-reconcile.{service,timer}` | UTC state reconcile (15 min) | no | timer | — |
| `sig-bot-pre-event.{service,timer}` | election-night readiness (hourly Nov 3–4) | no | timer | — |
| `sig-bot-post-settlement.{service,timer}` | post-wall reconciliation | no | timer | — |

**Three liveness layers for the trader:** (1) in-process heartbeat thread (module) → writes the
SQLite heartbeat; (2) **`WatchdogSec`** → systemd restarts a *hang*; (3) the supervisor process
(continuous) **+** the watchdog timer (periodic, catches a crash before any monitor starts).

---

## 1. Provision (fresh droplet)

```bash
sudo timedatectl set-timezone UTC          # REQUIRED: the pre-event / post-settlement OnCalendar
                                           # timers are UTC; SIG is a UTC 24/7 market.
sudo adduser --system --group --home /opt/sig-bot sig
sudo apt-get update && sudo apt-get install -y python3-venv git util-linux curl
#                                                           ^util-linux = flock
```

## 2. Clone + venv

```bash
sudo -u sig git clone <REPO_URL> /opt/sig-bot
cd /opt/sig-bot
sudo -u sig python3 -m venv .venv
sudo -u sig .venv/bin/pip install -r requirements.txt   # includes fastapi+uvicorn for the sidecar
```

## 3. Secrets (`.env`, created manually, gitignored)

```bash
sudo -u sig cp .env.example .env
sudo -u sig nano .env         # fill SIG_API_KEY_READ/WRITE/FAST, SIG_BASE_URL, alert webhooks, CONTROL_API_TOKEN
sudo chmod 600 /opt/sig-bot/.env
```
`.env` is loaded by every unit via `EnvironmentFile=-/opt/sig-bot/.env` (the leading `-` means a
missing file does not block unit load — the trader still refuses to trade without keys, with a clear
error). `.env.example` is committed; real `.env` is never committed.

## 4. Install units + log retention

```bash
sudo /opt/sig-bot/deploy/scripts/install.sh
```
Copies `sig-bot*.service`/`*.timer` → `/etc/systemd/system`, installs the **journald retention cap**
`deploy/journald/99-sig-bot.conf` → `/etc/systemd/journald.conf.d/` (200 MB ceiling, 150 MB kept free
— the disk-exhaustion guard mbappe lacked), restarts journald, `daemon-reload`. Starts nothing.

## 5. `WatchdogSec` — the one required module hook ⚠️

`sig-bot.service` ships `Type=notify` + `WatchdogSec=90`. For this the trader **must** notify systemd
(a module-owned hook, not in this deploy dir). The orchestrator loop needs, once:

```python
# at startup, after init:
import systemd.daemon            # pip: systemd-python   (or send to $NOTIFY_SOCKET directly)
systemd.daemon.notify("READY=1")
# inside the main cycle loop (runs well under WatchdogSec/2 = 45s):
systemd.daemon.notify("WATCHDOG=1")
```

- **If the hook IS wired:** leave the unit as shipped. A hang in *any* path (not just the cycle)
  elapses the 90 s timer → systemd SIGABRT + restart. This is the true out-of-process dead-man.
- **If the hook is NOT wired yet (Phase 1):** in `sig-bot.service` comment the `Type=notify`,
  `NotifyAccess=main`, `WatchdogSec=90` lines and set `Type=simple`, then `daemon-reload`. The unit
  boots and `Restart=always` still covers crashes; you lose only hang-detection until the hook lands.
  The **SIM-test (step 7)** surfaces a missing hook as a start timeout — do not go live on `notify`
  until SIM shows the trader reaching `active (running)`.

## 6. Start + verify (order matters: trader first)

```bash
sudo /opt/sig-bot/deploy/scripts/start.sh        # enable --now, trader → helpers → timers
/opt/sig-bot/deploy/scripts/status.sh            # units, next timer fires, /health, trader log tail
curl -s http://127.0.0.1:8787/health             # expect HTTP 200 + healthy payload
```
Verify the **flock**: a second hand-run must be refused —
```bash
sudo -u sig flock -n /var/lock/sig-bot-trader.lock echo got-lock   # prints nothing: lock held ✔
```

## 7. SIM-test BEFORE live

Run against the SIM/paper environment first (set the env flag in `.env`, e.g.
`SIG_ENVIRONMENT=sim`). Confirm, over at least one full cycle interval:
- `systemctl is-active sig-bot.service` → `active` (on `Type=notify`, this proves the sd_notify hook);
- `/health` healthy; `last-cycle-age` advancing; feed fresh;
- kill the trader (`sudo systemctl kill -s SIGKILL sig-bot.service`) → it restarts within ~10 s and
  **pre-flight reconciliation** runs on the cold start (crash-mid-trade is safe);
- `journalctl -u sig-bot-watchdog.service` shows the external dead-man firing and NOT paging a
  healthy bot;
- `systemctl list-timers 'sig-bot*'` shows reconcile ~15 min out and the Nov 3–4 / post-wall timers
  scheduled in UTC.

## 8. Go-live

Flip `.env` to the live environment, `sudo systemctl restart sig-bot.service`, re-run
`status.sh`, confirm `/health` + first live cycle. Keep `journalctl -fu sig-bot.service` open for the
first few cycles.

**Stop / teardown:** `sudo deploy/scripts/stop.sh` (graceful SIGINT, positions **held** — flatten is
a separate governance action, never automatic on stop); add `--disable` for full teardown.

---

## What was stripped vs mbappe (equities → SIG)

Removed: ET-timezone pre-open/post-close/weekend cron, `resolve_trading_session()`, BOD/EOD email,
weekday/market-hours gates. Replaced with **UTC, election-calendar** timers: `reconcile` (every
15 min, continuous), `pre-event-check` (hourly across Nov 3–4, the only "event"), `post-settlement`
(after the single 2026-11-04T17:00Z wall). SIG trades 24/7, so there is no session to open/close.
