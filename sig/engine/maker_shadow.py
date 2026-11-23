"""MAKER-ENTRY shadow — pure logic. Would resting at the touch on the Polymarket-favoured side enter cross-market
positions at better prices than crossing the spread? (research: tools/research/tape/mm2_sim.py, 2026-10-07)

Each minute, for every cross-market race whose gap passes the live entry rule (entry_threshold <= |gap| < 0.25),
record the resting quote we WOULD post:
  SIG below Poly (sharp > mid) -> a YES BID at best_bid   (buys YES when someone sells YES into it)
  SIG above Poly (sharp < mid) -> a YES ASK at best_ask   (sells YES to a YES buyer == buys NO at 1 - ask)
together with the displayed depth in front of it. tools/maker_shadow_validate.py later matches these against the
recorded trade tape (pro-rata and back-of-queue fill models) and compares with taking the ask on the same signal.
Record-only: no orders.
"""
from __future__ import annotations

from sig.engine.rotation import entry_threshold


def maker_quotes(rows: list[dict], *, cost_floor: float = 0.05, max_gap: float = 0.25) -> list[dict]:
    """rows: {exchange_id, sharp, best_bid, best_ask, bid_size, ask_size}. Returns the quotes we'd rest now."""
    out = []
    for r in rows:
        try:
            s, b, a = float(r["sharp"]), float(r["best_bid"]), float(r["best_ask"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 < b <= a < 1.0):
            continue
        mid = (b + a) / 2.0
        gap = s - mid
        if not (entry_threshold(s, cost_floor) <= abs(gap) < max_gap):
            continue
        side = "bid" if gap > 0 else "ask"
        out.append({"exchange_id": str(r["exchange_id"]), "side": side, "px": b if side == "bid" else a,
                    "depth": float(r.get("bid_size" if side == "bid" else "ask_size") or 0.0),
                    "sharp": round(s, 4), "mid": round(mid, 4), "bid": b, "ask": a, "gap": round(gap, 4)})
    return out
