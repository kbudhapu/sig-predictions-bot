"""ELECTION-NIGHT CASH RESERVE sell-down (operator-approved 2026-10-29: raise ~$40k by Nov 3).

Every --every-min minutes until cash >= --target or --deadline: rank our holdings by REMAINING edge vs Polymarket
(held-side fair - held-side bid; lowest first), pick just enough of them (cash + their value at the bid >= target),
and run ONE reduce-only trim pass on them (tools/trim_positions.py: sells within --leash of the touch from the live
order book, 120s venue expiry, 3s pacing). Markets owned by an isolated strategy are skipped (the broker refuses them
anyway). Re-ranked every pass, because rotation keeps reshuffling the book until Nov 2.

DRY RUN by default. Run (droplet):
  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/sell_down.py --target 40000 --deadline 2026-11-03T20:00:00+00:00 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from datetime import datetime, timezone


def select_markets(rows: list[dict], cash: float, target: float, min_edge_skip: float | None = None) -> list[str]:
    """rows: {ex, edge, usd, owner}. Lowest remaining edge first, skipping owned markets and unknown edges, until
    cash + selected value at the bid >= target. Pure (tested)."""
    need = target - cash
    if need <= 0:
        return []
    out, got = [], 0.0
    for r in sorted((r for r in rows if r.get("owner") is None and r.get("edge") is not None and r["usd"] > 0),
                    key=lambda r: r["edge"]):
        if min_edge_skip is not None and r["edge"] >= min_edge_skip:
            break
        out.append(r["ex"])
        got += r["usd"]
        if got >= need:
            break
    return out


def _rank(db_path: str) -> tuple[float, list[dict]]:
    from sig.engine.ownership import registry
    from sig.sig_client import SigClient
    c = SigClient(timeout=30.0)
    acct, pos = c.account(), c.portfolio_positions()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        px = {str(e): (b, a) for e, b, a in con.execute("SELECT exchange_id, best_bid, best_ask FROM prices") if b and a}
        sharp = {str(e): float(s) for e, s in con.execute(
            "SELECT exchange_id, sharp FROM sharp_history WHERE ts >= strftime('%Y-%m-%dT%H:%M','now','-30 minutes') "
            "ORDER BY ts")}
    finally:
        con.close()
    reg = registry()
    rows = []
    for p in pos["positions"]:
        if p.get("settled"):
            continue
        ex, q = str(p["exchangeId"]), float(p["quantity"])
        if abs(q) < 1 or ex not in px:
            continue
        b, a = px[ex]
        bid_held = b if q > 0 else 1 - a
        f = sharp.get(ex)
        edge = None if f is None else ((f if q > 0 else 1 - f) - bid_held)
        rows.append({"ex": ex, "edge": edge, "usd": abs(q) * bid_held, "owner": reg.owner_of(ex)})
    return float(acct["buyingPower"]), rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=40000.0)
    ap.add_argument("--deadline", type=str, default="2026-11-03T20:00:00+00:00")
    ap.add_argument("--every-min", type=float, default=20.0)
    ap.add_argument("--leash", type=float, default=0.005, help="sell at most this far below the touch")
    ap.add_argument("--max-edge", type=float, default=0.10, help="never sell a holding with >= this edge left")
    ap.add_argument("--db", type=str, default="data/state.db")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    from tools.trim_positions import execute, plan
    deadline = datetime.fromisoformat(a.deadline).timestamp()
    while True:
        now = datetime.now(timezone.utc)
        try:
            cash, rows = _rank(a.db)
        except Exception as exc:  # noqa: BLE001 — transient venue/db error: retry next pass
            print(f"{now:%m-%d %H:%M} rank failed: {type(exc).__name__} {str(exc)[:120]}", flush=True)
            cash, rows = None, []
        if cash is not None:
            pick = select_markets(rows, cash, a.target, a.max_edge)
            val = sum(r["usd"] for r in rows if r["ex"] in pick)
            print(f"{now:%m-%d %H:%M} cash ${cash:,.0f} target ${a.target:,.0f} -> selling {len(pick)} markets "
                  f"(${val:,.0f} at bid): {','.join(pick)}", flush=True)
            if not pick:
                print("target reached (or nothing eligible) - done", flush=True)
                return
            try:
                nav, _, plan_rows = plan(1.0, a.leash, 10, False, None, set(pick))
                for ex, p, held, excess, q, price, why in plan_rows:
                    print(f"   ex={ex:>5} {held:3} hold={abs(float(p['quantity'])):>7.0f} sell_now={q:>6} @ {price} [{why}]",
                          flush=True)
                if a.execute:
                    asyncio.run(execute(plan_rows, 10))
            except Exception as exc:  # noqa: BLE001
                print(f"   pass failed: {type(exc).__name__} {str(exc)[:160]}", flush=True)
        if a.once or time.time() + a.every_min * 60 > deadline:
            print("deadline / once - stopping", flush=True)
            return
        time.sleep(a.every_min * 60)


if __name__ == "__main__":
    main()
