"""Free cash for the fair-band pilot at the least expected-value cost (sig/engine/funding.py). DRY RUN by default.

Reads: venue positions + account (SIG read client), live order books for the cheapest candidate markets, fresh
Polymarket fair from sharp_history (<= 3 min old). Prints the plan: every order, its expected value given up
(vs fair) and its immediate leaderboard hit (vs the SIG mid).

--execute requires ALL of:
  --accept-ev-cost USD   >= the plan's EV cost (you confirm the price you are paying, after seeing it)
  the fair-band pilot already LIVE (data/market_ownership.json owns markets) so the freed cash lands in its
     carve-out instead of being re-spent by cross_market (which would buy the same positions back = churn)
Skips markets with ANY open order (no self-trades) and markets owned by an isolated strategy. Each order is
re-checked against the live book and fair right before placing (sig.engine.funding.execute_plan).
Run (droplet):  set -a; . .env; set +a; PYTHONPATH=. .venv/bin/python tools/fund_pilot.py --need 10000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from sig.engine.funding import FundingLimits, execute_plan, plan_funding
from sig.sig_client import SigClient

DB = os.environ.get("SIG_STATE_DB", "data/state.db")


def fresh_fairs() -> dict:
    now = datetime.now(timezone.utc)
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        out = {}
        for ex, s in c.execute("SELECT exchange_id, sharp FROM sharp_history WHERE ts >= ? ORDER BY ts",
                               ((now - timedelta(minutes=3)).isoformat(),)):
            out[str(ex)] = float(s)
        return out
    finally:
        c.close()


def _last_carve_out(path: str = "data/fair_band_live/ticks.jsonl") -> float | None:
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            return float(json.loads(fh.readlines()[-1]).get("carve_out") or 0.0)
    except Exception:  # noqa: BLE001
        return None


def pilot_carve_out_ok(proceeds: float, path: str = "data/fair_band_live/ticks.jsonl", max_age_s: float = 180.0,
                       now=None) -> tuple[bool, str]:
    """The live pilot must be RUNNING (a tick < max_age_s old), not winding down, and holding a ledger carve-out of
    at least `proceeds` — so every dollar this sale frees is invisible to cross_market's sizing."""
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            last = json.loads(fh.readlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return False, f"no pilot tick ({type(exc).__name__})"
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(str(last.get("ts")))).total_seconds()
    if age > max_age_s:
        return False, f"pilot tick is {age:.0f}s old (loop not running)"
    if last.get("winding_down") or last.get("halt"):
        return False, f"pilot winding down ({last.get('halt')})"
    if float(last.get("carve_out") or 0.0) + 1.0 < proceeds:
        return False, f"pilot carve-out ${float(last.get('carve_out') or 0):,.0f} < proceeds ${proceeds:,.0f}"
    return True, "ok"


def book_of(client, ex, tries: int = 3) -> dict | None:
    """Live order book, retried on venue 429/503 (SIG returns transient SERVICE_UNAVAILABLE). None if it cannot be
    read: the market is then skipped (plan) or the order skipped (execute) — never a crash mid-run."""
    import time as _t
    from sig.sig_client import SigAPIError
    for i in range(tries):
        try:
            b = client.orderbook(ex) or {}
            lv = lambda k: [(float(x["price"]), float(x["quantity"])) for x in (b.get(k) or []) if "price" in x]
            return {"bids": lv("bids"), "asks": lv("asks")}
        except SigAPIError as exc:
            if exc.status in (0, 429, 502, 503) and i < tries - 1:
                _t.sleep(2.0 * (i + 1))
                continue
            return None
        except Exception:  # noqa: BLE001
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", type=float, default=10000.0, help="cash target (free cash already held counts)")
    ap.add_argument("--max-ev-cost", type=float, default=0.05)
    ap.add_argument("--max-mark-cost", type=float, default=0.005)
    ap.add_argument("--books", type=int, default=25, help="live order books to read (cheapest candidates)")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--accept-ev-cost", type=float, default=None)
    a = ap.parse_args()
    from sig.engine.ownership import registry
    c = SigClient(timeout=30.0)
    acct = c.account()
    acct = acct.get("data", acct) if isinstance(acct, dict) else {}
    free = float(acct.get("balance", 0.0))
    carve = _last_carve_out()
    # never raise more than the live pilot's carve-out can absorb: free + proceeds <= carve-out, so cross_market
    # sizes against zero (a top-up past the carve-out would hand cash straight back to cross -> re-buy churn)
    cap_total = a.need if carve is None else min(a.need, carve)
    need = max(cap_total - free, 0.0)
    pos_rows = c.portfolio_positions("midterm-elections").get("positions", [])
    positions = {str(p["exchangeId"]): float(p["quantity"]) for p in pos_rows if not p.get("settled")}
    fairs = fresh_fairs()
    reg = registry()
    open_ex = set()
    try:
        r = c.get("/orders", params={"status": "open", "limit": 200})
        rows = r.get("data", r) if isinstance(r, dict) else r
        if isinstance(r, dict) and (r.get("pagination") or {}).get("hasMore"):
            print("more than 200 open orders: cannot prove which markets are clear -> refusing")
            return 2
        open_ex = {str(o.get("exchangeId")) for o in (rows or []) if o.get("open", True)}
    except Exception as exc:  # noqa: BLE001 — cannot prove there are no open orders -> refuse
        print("cannot read open orders:", exc)
        return 2
    exclude = open_ex | {ex for ex in positions if reg.owner_of(ex) is not None}
    # preselect by the cached touch (prices table) then read live books for the cheapest few
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    touch = {str(r[0]): r[1:] for r in con.execute("SELECT exchange_id, best_bid, best_ask FROM prices")}
    con.close()
    ranked = []
    for ex, q in positions.items():
        t, f = touch.get(ex), fairs.get(ex)
        if abs(q) < 1 or ex in exclude or not t or f is None or t[0] is None or t[1] is None:
            continue
        px, lf = (t[0], f) if q > 0 else (1 - t[1], 1 - f)
        if px > 0:
            ranked.append(((lf - px) / px, ex))
    ranked.sort()
    books = {}
    for _, ex in ranked[: a.books]:
        bk = book_of(c, ex)
        if bk is not None:
            books[ex] = bk
        else:
            print(f"  (skipped {ex}: order book unreadable)")
    lim = FundingLimits(max_ev_cost=a.max_ev_cost, max_mark_cost=a.max_mark_cost)
    plan = plan_funding(positions, books, fairs, need, lim, exclude)
    print(f"free cash ${free:,.0f}  target ${a.need:,.0f}  -> need ${need:,.0f}  | excluded {len(exclude)} markets "
          f"(open orders / owned)")
    for o in plan["orders"]:
        print(f"  sell {o['outcome']:3s} ex={o['ex']:>5s} qty={o['qty']:>6d} limit>={o['limit']:.3f}  "
              f"proceeds ${o['proceeds']:>8,.0f}  ev-cost ${o['ev_cost']:>6,.0f}  mark-hit ${o['mark_cost']:>5,.0f}")
    print(f"PLAN: proceeds ${plan['proceeds']:,.0f}  EV given up ${plan['ev_cost']:,.0f}  "
          f"leaderboard hit ${plan['mark_cost']:,.0f}  short ${plan['short_usd']:,.0f}")
    if not a.execute:
        print("DRY RUN — nothing placed")
        return 0
    if a.accept_ev_cost is None or a.accept_ev_cost + 1e-6 < plan["ev_cost"]:
        print(f"REFUSED: pass --accept-ev-cost >= {plan['ev_cost']:.0f} to confirm the cost")
        return 3
    ok, why = pilot_carve_out_ok(plan["proceeds"] + free)          # free cash + proceeds must fit inside it
    if not ok:
        print(f"REFUSED: {why} — freed cash would be re-spent by cross_market (it re-buys trimmed exposure: "
              f"995 on 2026-10-06 was re-bought within 30s of a trim)")
        return 4
    return asyncio.run(_execute(plan, need, lim, c, fairs))


async def _execute(plan, need, lim, c, fairs0) -> int:
    from main_sig import _make_read_client, _resolve_tournament_id
    from sig.broker import SigBroker
    b = SigBroker(tournament_id=_resolve_tournament_id(_make_read_client(), "midterm-elections"),
                  tournament_slug="midterm-elections")

    from sig.engine import ownership as own_mod

    async def read_position(ex):
        own_mod.set_registry(None)                        # re-read ownership before every order (bot may claim)
        for p in await b.get_positions(fresh=True):
            if str(p.contract_id) == str(ex):
                return -p.qty if str(p.side).lower() == "short" else p.qty
        return 0.0

    async def read_book(ex):
        return (await asyncio.to_thread(book_of, c, ex)) or {}        # unreadable -> recheck skips the order

    async def read_fair(ex):
        return fresh_fairs().get(str(ex))

    rep = await execute_plan(plan, broker=b, read_book=read_book, read_position=read_position, read_fair=read_fair,
                             need_usd=need, limits=lim)
    print("EXECUTED:", rep)
    try:                                                   # attribute the sells (else they land UNATTRIBUTED)
        from infra.state.store import StateStore
        st = await StateStore().open()
        try:
            print("attribution rows recorded:", await st.record_order_attribution(b.drain_attribution()))
        finally:
            await st.close()
    except Exception as exc:  # noqa: BLE001
        print("attribution flush failed:", exc)
    return 0 if not rep["stopped"] else 5


if __name__ == "__main__":
    sys.exit(main())
