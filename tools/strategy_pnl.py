"""Per-strategy attribution — WHO makes/loses/churns, from venue fill truth + the local strategy tag.

The local `orders` table's filled_quantity is unreliable (never updated — a separate sync defect), so we
trust the VENUE for fills (portfolio_fills: orderId, exchangeId, price, quantity, side, filledAt) and use
the local orders table ONLY for the order_id -> (strategy, action) tag (broker_order_id is populated on
~88% of orders). Execution edge per fill = how much better than the mid we transacted, in YES-space:
positive = we captured spread (maker-ish), negative = we paid spread (taker/churn). Mid-at-fill comes from
the self-collected `mid_history` time-series. Net spread per strategy is the churn-cost / maker-earn signal
you asked for ("is the sniper's churn costing us, and how much").

Run on the droplet:  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/strategy_pnl.py
NOTE: the venue returns only the most recent ~200 fills, so this is a RECENT-WINDOW view, not all-time.
"""
from __future__ import annotations

import bisect
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from sig.sig_client import SigClient, SigAPIError

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
MID_TOLERANCE_S = 600.0   # accept a mid sample within this of the fill time


def _epoch(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _venue_fills() -> list[dict]:
    c = SigClient(timeout=30.0)
    for _ in range(8):
        try:
            f = c.portfolio_fills()
            break
        except SigAPIError as e:
            if e.status == 429:
                time.sleep(3)
                continue
            raise
    else:
        return []
    rows = f.get("fills", f.get("data", f)) if isinstance(f, dict) else f
    return rows if isinstance(rows, list) else []


def _current_mids(conn) -> dict:
    out = {}
    for ex, b, a in conn.execute("SELECT exchange_id, best_bid, best_ask FROM prices"):
        try:
            if b is not None and a is not None:
                out[str(ex)] = (float(b) + float(a)) / 2.0
        except (TypeError, ValueError):
            pass
    return out


def main() -> int:
    conn = sqlite3.connect(DB)

    # order_id -> action (buy/sell), for the YES-direction of each fill (strategy comes from the ledger)
    action_of: dict[str, str] = {}
    for bid, action in conn.execute(
        "SELECT broker_order_id, action FROM orders "
        "WHERE broker_order_id IS NOT NULL AND broker_order_id != ''"
    ):
        action_of[str(bid)] = str(action).lower()

    agg = defaultdict(lambda: {"fills": 0, "qty": 0.0, "notional": 0.0,
                               "edge": 0.0, "edge_n": 0, "netyes": defaultdict(float)})
    unattributed = 0
    total = 0
    # cumulative ledger (ingested over time) — strategy + mid_at_fill already stored
    for fid, oid, strat, ex, outcome, price, qty, mid_at in conn.execute(
        "SELECT fill_id, order_id, strategy, exchange_id, outcome, price, quantity, mid_at_fill "
        "FROM strategy_fills"
    ):
        total += 1
        try:
            price = float(price or 0)
            qty = abs(float(qty or 0))
        except (TypeError, ValueError):
            continue
        strat = strat or "UNATTRIBUTED"
        if strat == "UNATTRIBUTED":
            unattributed += 1
        action = action_of.get(str(oid), "buy")
        vside = str(outcome or "").lower()
        a = agg[strat]
        a["fills"] += 1
        a["qty"] += qty
        a["notional"] += abs(price * qty)
        yes_price = price if vside == "yes" else (1.0 - price)
        yes_buy = (action == "buy" and vside == "yes") or (action == "sell" and vside == "no")
        if mid_at is not None:
            try:
                m = float(mid_at)
                edge = (m - yes_price) if yes_buy else (yes_price - m)
                a["edge"] += edge * qty
                a["edge_n"] += 1
            except (TypeError, ValueError):
                pass
        a["netyes"][ex] += qty if yes_buy else -qty

    placed = dict(conn.execute("SELECT strategy, COUNT(*) FROM orders GROUP BY strategy"))
    cur_mid = _current_mids(conn)

    print(f"# per-strategy attribution (CUMULATIVE strategy_fills ledger: {total} fills)")
    print("%-26s %6s %10s %12s %10s %7s" %
          ("strategy", "fills", "notional$", "exec_edge$", "net_mv$", "fill%"))
    print("-" * 78)
    for strat, a in sorted(agg.items(), key=lambda kv: kv[1]["notional"], reverse=True):
        p = placed.get(strat)
        fill_pct = (100.0 * a["fills"] / p) if p else 0.0
        edge = f"{a['edge']:.1f}" if a["edge_n"] else "n/a"
        # rough net mark value of this strategy's attributed net-YES position (approx: ignores x-strategy netting)
        net_mv = 0.0
        for ex, nq in a["netyes"].items():
            m = cur_mid.get(ex)
            if m is not None:
                net_mv += (nq * m) if nq > 0 else (abs(nq) * (1.0 - m))
        print("%-26s %6d %10.0f %12s %10.0f %6.0f%%" %
              (strat, a["fills"], a["notional"], edge, net_mv, fill_pct))
    print("-" * 78)
    print(f"unattributed fills: {unattributed} "
          f"(no matching broker_order_id — older orders or non-strategy writes)")
    print("exec_edge$ > 0 = captured spread (maker-ish); < 0 = paid spread (taker/churn). "
          "net_mv$ is an approx per-strategy mark (ignores cross-strategy venue netting).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
