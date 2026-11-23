"""MM fill-efficiency per market — defines the 'region' MM should own: where it actually FILLS and EARNS.

MM's ceiling is fills (venue liquidity), not capital. This ranks the markets MM has filled by both volume
(does it fill there) and exec_edge (does it earn the spread there), so we concentrate MM on the markets
that pay and set per-market caps from evidence — rather than spraying 237 books or reserving capital blind.

Reads the strategy_fills ledger (strategy='market_making') + mid_history for exec_edge. Run after ~a day of
post-join-touch fills:  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/mm_fill_efficiency.py
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

DB = "data/state.db"


def main() -> int:
    c = sqlite3.connect(DB)
    # order_id -> action (buy/sell) for the YES-direction of each fill
    action_of = {}
    for bid, action in c.execute(
        "SELECT broker_order_id, action FROM orders WHERE broker_order_id IS NOT NULL AND broker_order_id != ''"
    ):
        action_of[str(bid)] = str(action).lower()

    per = defaultdict(lambda: {"fills": 0, "notional": 0.0, "edge": 0.0, "edge_n": 0})
    tot = {"fills": 0, "notional": 0.0, "edge": 0.0, "edge_n": 0}
    for oid, strat, ex, outcome, price, qty, mid in c.execute(
        "SELECT order_id, strategy, exchange_id, outcome, price, quantity, mid_at_fill FROM strategy_fills "
        "WHERE strategy='market_making'"
    ):
        try:
            price = float(price or 0)
            qty = abs(float(qty or 0))
        except (TypeError, ValueError):
            continue
        p = per[str(ex)]
        p["fills"] += 1
        p["notional"] += price * qty
        tot["fills"] += 1
        tot["notional"] += price * qty
        if mid is not None:
            try:
                m = float(mid)
            except (TypeError, ValueError):
                continue
            vside = str(outcome or "").lower()
            yes_price = price if vside == "yes" else (1.0 - price)
            act = action_of.get(str(oid), "buy")
            yes_buy = (act == "buy" and vside == "yes") or (act == "sell" and vside == "no")
            e = ((m - yes_price) if yes_buy else (yes_price - m)) * qty
            p["edge"] += e
            p["edge_n"] += 1
            tot["edge"] += e
            tot["edge_n"] += 1

    print(f"# MM fill-efficiency by market — {tot['fills']} fills, ${tot['notional']:.0f} notional, "
          f"exec_edge ${tot['edge']:+.1f} ({100*tot['edge']/tot['notional']:+.2f}% of notional)"
          if tot['notional'] else "# MM fill-efficiency — no MM fills recorded yet")
    print("# the REGION = markets with both real fill volume AND positive exec_edge (concentrate MM here)\n")
    rows = [(ex, v) for ex, v in per.items()]
    rows.sort(key=lambda kv: kv[1]["notional"], reverse=True)
    print("%-7s %6s %10s %10s %9s" % ("exch", "fills", "notional$", "exec_edge$", "edge%"))
    print("-" * 48)
    for ex, v in rows[:25]:
        epct = (100 * v["edge"] / v["notional"]) if (v["notional"] and v["edge_n"]) else 0.0
        print("%-7s %6d %10.0f %+10.1f %+8.2f%%" % (ex, v["fills"], v["notional"], v["edge"], epct))
    print("\nown the markets with high fills + positive edge%; drop the ones with fills but negative edge "
          "(adverse selection) and the ones that never fill. re-run as volume grows — the region shifts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
