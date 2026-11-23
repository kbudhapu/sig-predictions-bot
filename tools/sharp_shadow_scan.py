"""SHADOW sharp-benchmark scan (LOG-ONLY — places no orders).

For each mapped race it prints SIG mid vs the SIG-aligned sharp benchmarks (Polymarket now; Kalshi when a
ticker is mapped) and the agreement-gate verdict. In shadow it reports the Polymarket-vs-SIG imbalance
with min_sources=1 so you can SEE the opportunities; the LIVE gate uses min_sources=2 (Polymarket AND
Kalshi must agree) so a lone source can never trigger a trade. Run on the droplet:
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/sharp_shadow_scan.py
"""
from __future__ import annotations

import asyncio
import time

from signals.mapping import load_race_mapping
from signals.poly_client import PolymarketClient
from signals.cross_market import resolve_external_prices
from signals.sharp_benchmark import evaluate, SourceQuote
from sig.sig_client import SigClient


def _sig_mids(client: SigClient, exchange_ids: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(0, len(exchange_ids), 90):
        resp = client.exchange_prices(exchange_ids[i:i + 90])
        for r in resp.get("data", []):
            b, a = r.get("bestBid"), r.get("bestAsk")
            if b is not None and a is not None:
                out[str(r.get("exchangeId"))] = (float(b) + float(a)) / 2.0
    return out


async def _poly_aligned(rm, pc) -> float | None:
    """SIG-aligned Polymarket price for a race (RAW midpoint -> resolver's single flip)."""
    ref = rm.usable_externals().get("polymarket")
    if ref is None or not getattr(ref, "yes_token_id", None):
        return None
    raw = await pc.get_midpoint(ref.yes_token_id)
    if raw is None:
        return None
    aligned = resolve_external_prices(rm, {"polymarket": float(raw)})
    return aligned.get("polymarket")


async def main():
    mapping = load_race_mapping()
    active = {str(rm.sig_exchange_id): rm for rm in mapping.values() if rm.sig_exchange_id and rm.is_active()}
    sig = SigClient(key_env="SIG_API_KEY_READ", timeout=20.0)
    sig_mids = _sig_mids(sig, list(active.keys()))
    print("=== SHADOW sharp-benchmark scan (log-only) ===")
    print(f"{'race':34s} {'SIG':>7s} {'POLY':>7s} {'KALSHI':>7s} {'div':>7s}  verdict")
    async with PolymarketClient(timeout=10.0) as pc:
        for ex, rm in active.items():
            sig_mid = sig_mids.get(ex)
            poly = await _poly_aligned(rm, pc)
            kalshi = None   # TODO: wire KalshiClient once the 2026 race tickers are mapped
            quotes = []
            if poly is not None:
                quotes.append(SourceQuote("polymarket", poly, age_s=1.0))
            if kalshi is not None:
                quotes.append(SourceQuote("kalshi", kalshi, age_s=1.0))
            # shadow: min_sources=1 to SURFACE Polymarket imbalances; live would use min_sources=2
            v = evaluate(sig_mid, quotes, min_sources=1, min_divergence=0.05)
            print("%-34s %7s %7s %7s %7s  %s%s" % (
                rm.key[:34],
                f"{sig_mid:.3f}" if sig_mid is not None else "  -",
                f"{poly:.3f}" if poly is not None else "  -",
                f"{kalshi:.3f}" if kalshi is not None else "  -",
                f"{v.divergence:+.3f}" if v.divergence is not None else "  -",
                v.reason,
                "  <<< WOULD-TRADE (shadow; live needs Kalshi agree)" if v.tradeable else ""))

asyncio.run(main())
