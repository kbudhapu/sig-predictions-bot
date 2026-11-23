"""DRY-RUN REHEARSAL of the fair-band live trader against the REAL venue + REAL local DB — places NOTHING.

Real reads (positions, ALL open orders, account) through a real SigBroker; real books from the `prices` table;
fair = latest Polymarket benchmark from sharp_history (<= 3 min old). Writes are intercepted by DryBroker: each
place/cancel is recorded and shown back as an open order on later ticks (never filled), so the trader's
reconcile/cancel logic runs as it would live. Ownership uses a TEMP registry (never data/market_ownership.json).

Checks every tick: quotes on the 0.005 grid, inside [px_min, px_max], never crossing the live book (YES buy <=
best_bid, NO buy <= 1 - best_ask), only in owned markets, budget/market caps, write cap, and that real API rows
have the fields/values the trader relies on. Exit code 1 on any violation.
Run (droplet, beside the live bot; ~4 venue reads/tick):
  SIG_OWNERSHIP_PATH=/tmp/fbdry/own.json PYTHONPATH=. .venv/bin/python tools/fair_band_dryrun.py [ticks] [interval_s]
"""
import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SIG_OWNERSHIP_PATH", "/tmp/fbdry/own.json")
os.makedirs(os.path.dirname(os.environ["SIG_OWNERSHIP_PATH"]), exist_ok=True)

from sig.broker import OrderResult, SigBroker  # noqa: E402
from sig.engine.fair_band import classify  # noqa: E402
from sig.engine.fair_band_live import SID, FairBandLive, FairBandLiveConfig, venue_pnl  # noqa: E402
from sig.engine.orchestrator import _signed_qty  # noqa: E402
from sig.engine.ownership import registry  # noqa: E402

DB = os.environ.get("SIG_STATE_DB", "data/state.db")


class DryBroker:
    def __init__(self, real):
        self.real, self.fake, self.log, self.n = real, {}, [], 0

    async def get_positions(self, fresh=False):
        return await self.real.get_positions(fresh=fresh)

    async def get_open_orders_all(self):
        rows = await self.real.get_open_orders_all()
        return None if rows is None else rows + [dict(o) for o in self.fake.values()]

    async def get_account(self):
        return await self.real.get_account()

    async def place_order(self, *, strategy_id, market_id, exchange_id, side, action, quantity, price, outcome=None,
                          priority=False, **_kw):
        if not registry().allows(strategy_id, exchange_id):                 # same guard as the real chokepoint
            self.log.append(("REFUSED", strategy_id, exchange_id))
            return OrderResult(idempotency_key="", status="rejected", raw={"reason": "owned"})
        self.n += 1
        oid = f"dry{self.n}"
        self.fake[oid] = {"order_id": oid, "exchange_id": str(exchange_id), "side": outcome, "action": "buy",
                          "price": float(price), "quantity": float(quantity)}
        registry().note_order(strategy_id, oid, exchange_id)
        self.log.append(("place", exchange_id, outcome, price, quantity))
        return OrderResult(idempotency_key="", status="open", order_id=oid)

    async def cancel_order(self, order_id, *, strategy_id=None):
        if not registry().allows_cancel(strategy_id, order_id):
            self.log.append(("CANCEL_REFUSED", order_id))
            return OrderResult(idempotency_key="", status="rejected")
        self.fake.pop(order_id, None)
        self.log.append(("cancel", order_id))
        return OrderResult(idempotency_key="", status="canceled", order_id=order_id)


def db_rows():
    """{ex: row} from the live DB: book from `prices`, fair = latest sharp_history (<= 3 min)."""
    now = datetime.now(timezone.utc)
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        fair = {}
        for ex, ts, s in c.execute("SELECT exchange_id, ts, sharp FROM sharp_history WHERE ts >= ? ORDER BY ts",
                                   ((now - timedelta(minutes=3)).isoformat(),)):
            fair[str(ex)] = float(s)
        rows = {}
        for ex, b, a, bs, as_, upd in c.execute("SELECT exchange_id, best_bid, best_ask, bid_size, ask_size, "
                                                "updated_at FROM prices"):
            try:
                age = (now - datetime.fromisoformat(str(upd).replace("Z", "+00:00"))).total_seconds()
            except Exception:  # noqa: BLE001
                age = 1e9
            rows[str(ex)] = {"fair": fair.get(str(ex)), "best_bid": b, "best_ask": a, "bid_size": bs,
                             "ask_size": as_, "age_s": age}
        return rows
    finally:
        c.close()


def check_api_shapes(opens, snaps, viol):
    for o in (opens or [])[:200]:
        if o.get("side") not in ("yes", "no") or o.get("action") not in ("buy", "sell"):
            viol.append(("api_open_order_shape", {k: o.get(k) for k in ("side", "action", "price")}))
            break
    for p in snaps[:200]:
        if str(getattr(p, "side", "")).lower() not in ("long", "short", "flat"):
            viol.append(("api_position_side", getattr(p, "side", None)))
            break


async def main(ticks: int = 8, interval: float = 60.0) -> int:
    from main_sig import _fair_band_inputs, _fair_band_live_pnl, _make_read_client, _resolve_tournament_id
    slug = "midterm-elections"
    tid = _resolve_tournament_id(_make_read_client(), slug)
    real = SigBroker(tournament_id=tid, tournament_slug=slug)
    br = DryBroker(real)
    cfg = FairBandLiveConfig(stop_file="/tmp/fbdry/STOP")
    tr = FairBandLive(broker=br, ownership=registry(), cfg=cfg)
    viol, start = [], datetime.now(timezone.utc).isoformat()
    for t in range(ticks):
        t0 = time.time()
        opens = await br.get_open_orders_all()                 # same read order as the live loop
        snaps = await br.get_positions(fresh=True) or []
        pos = {str(getattr(p, "contract_id", "")): _signed_qty(p) for p in snaps}
        acct = await br.get_account()
        check_api_shapes([o for o in (opens or []) if not str(o["order_id"]).startswith("dry")], snaps, viol)
        rows_all = db_rows()
        if t == 0:
            med, vol, _ = _fair_band_inputs(DB)
            tags = classify(med, vol)
            cands = sorted((ex for ex, tg in tags.items() if "liquid" in tg and (rows_all.get(ex) or {}).get("fair")),
                           key=lambda ex: -(vol.get(ex) or 0.0))
            owned = tr.select_markets(cands, pos, opens or [])
            print(f"candidates {len(cands)} | claimed {owned} | account free cash {acct.buying_power:,.0f} | "
                  f"real open orders {len([o for o in (opens or []) if not str(o['order_id']).startswith('dry')])}")
            bad = [ex for ex in owned if abs(pos.get(ex, 0.0)) >= 1]
            if bad:
                viol.append(("claimed_market_with_position", bad))
        owned = registry().owned_by(SID)
        rows = {ex: rows_all.get(ex, {}) for ex in owned}
        mids = {ex: (float(r["best_bid"]) + float(r["best_ask"])) / 2 for ex, r in rows.items()
                if r.get("best_bid") is not None and r.get("best_ask") is not None}
        pnl = _fair_band_live_pnl(DB, owned, start, rows, registry().claimed_at)
        vp = venue_pnl(snaps, owned, rows)
        tr.check_kill(pnl, vp)
        n_log = len(br.log)
        cash = float(os.environ.get("FREE_CASH_OVERRIDE", acct.buying_power))   # dry run only: size as if funded
        out = await tr.tick(rows, pos, opens, cash)
        new = br.log[n_log:]
        if out["placed"] + out["cancelled"] > cfg.max_writes_per_min:
            viol.append(("write_cap", out))
        for e in new:
            if e[0] in ("REFUSED", "CANCEL_REFUSED"):
                viol.append(("refused_write", e))
            if e[0] != "place":
                continue
            _, ex, outc, px, q = e
            r = rows[ex]
            if ex not in owned:
                viol.append(("outside_owned", e))
            if abs(round(px / 0.005) * 0.005 - px) > 1e-9 or not (cfg.px_min <= px <= cfg.px_max):
                viol.append(("bad_price", e))
            if outc == "yes" and px > float(r["best_bid"]) + 1e-9:
                viol.append(("crosses_or_improves_yes", e, r["best_bid"], r["best_ask"]))
            if outc == "no" and px > 1 - float(r["best_ask"]) + 1e-9:
                viol.append(("crosses_or_improves_no", e, r["best_bid"], r["best_ask"]))
            if r.get("fair") is not None:
                fair_leg = r["fair"] if outc == "yes" else 1 - r["fair"]
                if px > fair_leg - cfg.w + 0.005 + 1e-9:
                    viol.append(("not_below_fair_minus_w", e, r["fair"]))
        committed = sum(FairBandLive._exposure(pos.get(ex, 0.0), mids.get(ex, 0.5)) for ex in owned) + sum(
            max(o["price"], (mids.get(o["exchange_id"], 0.5) if o["side"] == "yes" else 1 - mids.get(o["exchange_id"], 0.5)))
            * o["quantity"] for o in br.fake.values())
        if committed > cfg.budget_usd + 1:
            viol.append(("budget", round(committed, 2)))
        print(f"tick {t}: {out} | resting dry orders {len(br.fake)} committed ${committed:,.0f} pnl {pnl} venue_pnl {vp} "
              f"| {time.time() - t0:.1f}s")
        for e in new[:6]:
            ex = e[1] if e[0] == "place" else None
            print("    ", e, "| book", (rows.get(ex) or {}).get("best_bid"), (rows.get(ex) or {}).get("best_ask"),
                  "fair", round((rows.get(ex) or {}).get("fair") or 0, 4) if ex else "")
        if t < ticks - 1:
            await asyncio.sleep(max(0.0, interval - (time.time() - t0)))
    print("VIOLATIONS:", viol if viol else "none")
    return 1 if viol else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*(float(a) if i else int(a) for i, a in enumerate(sys.argv[1:3])))))
