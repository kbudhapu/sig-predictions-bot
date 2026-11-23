"""Classify every held position vs its CURRENT sharp (Polymarket, polarity-aligned) to drive capital
rotation: free capital from WRONG-SIDE (heading to 0) and CONVERGED (edge captured) positions, and
concentrate into the biggest live RIGHT-SIDE divergences (high-conviction bets).

For each position: our side = sign(quantity) (+ long YES, − short YES / long NO). gap = sharp − SIG.
  • |gap| <= CONV_TOL                         → CONVERGED   (edge captured → recycle / bank)
  • gap points OUR way, |gap| large           → RIGHT-SIDE  (conviction=|gap| → HOLD/ADD)
  • gap points AGAINST us                      → WRONG-SIDE  (sharp says we lose → CLEAR to free capital)
  • no mapping / no live sharp                 → NO-SHARP    (MM inventory etc. — not a convergence bet)

Read-only. Run:  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/position_classify.py
"""
from __future__ import annotations

import asyncio
import time

from sig.sig_client import SigClient, SigAPIError
from signals.mapping import load_race_mapping
from signals.cross_market import resolve_external_prices
from signals.poly_client import PolymarketClient

CONV_TOL = 0.02


def _positions():
    c = SigClient(timeout=30.0)
    for _ in range(6):
        try:
            p = c.portfolio_positions()
            break
        except SigAPIError as e:
            if e.status == 429:
                time.sleep(3)
                continue
            raise
    else:
        return []
    return p.get("positions") or []


async def _aligned_sharps(by_ex: dict) -> dict:
    out: dict[str, float | None] = {}
    sem = asyncio.Semaphore(20)

    async def one(pc, ex, rm):
        ref = rm.usable_externals().get("polymarket")
        raw = None
        if ref:
            async with sem:
                try:
                    if getattr(ref, "yes_token_id", None):
                        raw = await pc.get_midpoint(ref.yes_token_id)
                except Exception:  # noqa: BLE001
                    raw = None
                if (raw is None or not (0.0 < raw < 1.0)) and getattr(ref, "market_id", None):
                    try:
                        m = await pc.get_market(ref.market_id)
                        if m:
                            raw = await pc.get_market_probability(m, ref.yes_outcome or "Yes", prefer_clob=False)
                    except Exception:  # noqa: BLE001
                        raw = None
        if raw is None or not (0.0 < float(raw) < 1.0):
            out[ex] = None
            return
        aligned = resolve_external_prices(rm, {"polymarket": float(raw)})
        out[ex] = (aligned.get("polymarket") if aligned else None)

    async with PolymarketClient(timeout=10.0, max_retries=1) as pc:
        await asyncio.gather(*[one(pc, ex, rm) for ex, rm in by_ex.items()])
    return out


def main() -> int:
    mapping = load_race_mapping()
    by_ex = {str(rm.sig_exchange_id): rm for rm in mapping.values()
             if rm.sig_exchange_id and rm.is_active()}
    positions = _positions()
    # only fetch sharps for exchanges we actually hold AND have a mapping for
    held_mapped = {str(p.get("exchangeId")): by_ex[str(p.get("exchangeId"))]
                   for p in positions if str(p.get("exchangeId")) in by_ex}
    sharps = asyncio.run(_aligned_sharps(held_mapped)) if held_mapped else {}

    buckets = {"WRONG-SIDE (clear)": [], "CONVERGED (recycle)": [],
               "RIGHT-SIDE (hold/add)": [], "NO-SHARP (MM/unmapped)": []}
    for p in positions:
        ex = str(p.get("exchangeId"))
        q = float(p.get("quantity", 0) or 0)
        if abs(q) < 1:
            continue
        sig = float(p.get("currentPrice", 0) or 0)
        up = float(p.get("unrealizedPnl", 0) or 0)
        notional = abs(q) * float(p.get("avgCost", 0) or 0)
        title = str(p.get("marketTitle", ""))[:30]
        sharp = sharps.get(ex)
        if ex not in by_ex or sharp is None or not (0.0 < sig < 1.0):
            buckets["NO-SHARP (MM/unmapped)"].append((notional, ex, q, sig, None, up, title))
            continue
        gap = sharp - sig
        long_yes = q > 0
        aligned_us = (gap > 0) if long_yes else (gap < 0)
        if abs(gap) <= CONV_TOL:
            cat = "CONVERGED (recycle)"
        elif aligned_us:
            cat = "RIGHT-SIDE (hold/add)"
        else:
            cat = "WRONG-SIDE (clear)"
        buckets[cat].append((abs(gap), ex, q, sig, sharp, up, title))

    def _show(cat, rows, sort_key):
        rows.sort(key=sort_key, reverse=True)
        cap = sum(abs(r[2]) * r[3] for r in rows)
        upnl = sum(r[5] for r in rows)
        print(f"\n=== {cat} — {len(rows)} positions, ~${cap:.0f} capital, uPnL {upnl:+.0f} ===")
        for r in rows[:12]:
            key, ex, q, sig, sharp, up, title = r
            sh = f"{sharp:.3f}" if sharp is not None else "  -  "
            print("  ex=%-5s q=%+7.0f sig=%.3f sharp=%s gap=%+.3f uPnl=%+6.0f  %s"
                  % (ex, q, sig, sh, (sharp - sig) if sharp is not None else 0.0, up, title))

    print("# position classification vs current sharp (capital rotation)")
    _show("WRONG-SIDE (clear → free capital; sharp says this side loses)",
          buckets["WRONG-SIDE (clear)"], lambda r: abs(r[2]) * r[3])
    _show("CONVERGED (recycle → edge captured, bank & redeploy)",
          buckets["CONVERGED (recycle)"], lambda r: r[5])
    _show("RIGHT-SIDE (hold/add → biggest live divergences = the conviction bets)",
          buckets["RIGHT-SIDE (hold/add)"], lambda r: r[0] * abs(r[2]) * r[3])
    _show("NO-SHARP (MM/unmapped — not a convergence bet)",
          buckets["NO-SHARP (MM/unmapped)"], lambda r: r[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
