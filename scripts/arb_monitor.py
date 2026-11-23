"""READ-ONLY arb monitor — continuously scans SIG for fillable mispricings and surfaces the fat ones
(durable operator_alerts row + structured log + ntfy phone push if NTFY_TOPIC is set) so they can be
grabbed BY HAND. Places NO orders (read scope only). Arms immediately; idles until the tournament opens
(session calendar), then scans every --interval seconds. Writes a heartbeat each cycle so the ported
dead-man/external-watchdog can see it's alive. Robust: a transient API error logs + backs off, never exits.

Run:  python scripts/arb_monitor.py                 # continuous (default 30s)
      python scripts/arb_monitor.py --once           # single scan (smoke)
      python scripts/arb_monitor.py --interval 20 --min-edge 0.02
"""
from __future__ import annotations

import argparse, sys, os, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from sig.env import load_env
from sig.sig_client import SigClient, SigAPIError
from sig.session_calendar import SigSessionCalendar, COMPETITION_START
from strategies.arb_detector import ArbDetector
from infra.persistence import db
from infra.persistence.heartbeat_store import write_heartbeat_status
from infra.alerts.notifiers import build_log_and_push

log = structlog.get_logger()
COMPONENT = "arb_monitor"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--min-edge", type=float, default=0.01)
    ap.add_argument("--min-qty", type=float, default=1.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    load_env()
    db.init_db()
    client = SigClient(key_env="SIG_API_KEY_READ")      # READ scope — cannot place an order
    notifier = build_log_and_push(db.DB_PATH)            # durable LogNotifier (+ ntfy if NTFY_TOPIC set)
    detector = ArbDetector(client, notifier=notifier, min_edge=args.min_edge, min_qty=args.min_qty)
    calendar = SigSessionCalendar()
    log.info("arb_monitor_start", interval_s=args.interval, min_edge=args.min_edge, min_qty=args.min_qty,
             competition_opens=COMPETITION_START.isoformat())

    IDLE_SLEEP = 120.0           # poll slowly while the tournament is closed (reduce idle noise)
    backoff = args.interval
    while True:
        now = datetime.now(timezone.utc)
        wait = args.interval
        try:
            if not calendar.is_within_rth(now):
                # armed but idle until the tournament opens (or after it ends)
                write_heartbeat_status(COMPONENT, db.DB_PATH, status={"gate": "BLOCKED",
                    "gate_reason": "tournament_not_open", "last_cycle_ts": now.isoformat()})
                mins = max(0.0, (COMPETITION_START - now).total_seconds() / 60.0)
                log.info("arb_monitor_idle", reason="tournament_not_open", opens_in_min=round(mins, 1))
                wait = IDLE_SLEEP
            else:
                arbs = detector.scan()                  # read-only: surfaces capturable arbs via notifier
                write_heartbeat_status(COMPONENT, db.DB_PATH, status={"gate": "OPEN",
                    "gate_reason": "", "last_cycle_ts": now.isoformat(), "capturable": len(arbs)})
            backoff = args.interval                      # healthy cycle resets backoff
        except SigAPIError as e:
            log.warning("arb_monitor_api_error", status=getattr(e, "status", "?"), path=getattr(e, "path", "?"))
            backoff = min(backoff * 2, 300.0); wait = backoff   # bounded exponential backoff on API trouble
        except Exception as e:
            log.warning("arb_monitor_cycle_error", error=str(e)[:160])
            backoff = min(backoff * 2, 300.0); wait = backoff
        if args.once:
            break
        time.sleep(wait)
    client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("arb_monitor stopped")
