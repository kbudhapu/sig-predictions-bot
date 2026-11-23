"""Multi-source SHARP-benchmark agreement gate.

The lesson from the New Hampshire Senate loss: the bot sold a *correctly-priced* SIG market (D ~0.84,
matching the forecaster/market consensus ~0.78-0.84) down toward a lone **Wikipedia rating (0.72)** that
was simply wrong. A single coarse source manufactured a fake ~11% "edge" that cleared every threshold.

The fix encoded here: a SIG-vs-benchmark divergence is tradeable ONLY when **at least two independent
REAL-MONEY sources agree** on the benchmark. Two independent sharp markets (Polymarket, Kalshi) agreeing
is the confidence bar; a lone rating is not. If the sharp sources disagree with each other, the "truth"
is unknown and we stand down — exactly what would have blocked NH.

Pure + source-agnostic: callers pass SIG-aligned prices (post the single polarity flip they already own)
per source; this module decides. No network, no polarity logic here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean


# Real-money sources only. Ratings (wikipedia/cook/sabato) are deliberately NOT here — they are coarse
# and must never, alone, trigger a trade. Keep this the allow-list of what counts toward agreement.
SHARP_SOURCES = frozenset({"polymarket", "kalshi"})


@dataclass(frozen=True)
class SourceQuote:
    """One source's SIG-ALIGNED benchmark probability for a race (already polarity-flipped by the caller)."""
    source: str
    aligned: float | None
    age_s: float = 0.0


@dataclass
class SharpVerdict:
    tradeable: bool
    reason: str                       # tradeable | insufficient_sources | sources_disagree | below_min_divergence
    agreed_sharp: float | None = None  # consensus benchmark (mean of the agreeing sharp sources)
    divergence: float | None = None    # agreed_sharp - sig_mid  (+ = SIG too LOW -> buy; - = too HIGH -> sell)
    n_sources: int = 0
    spread: float | None = None        # max-min across the fresh sharp sources (the disagreement measure)
    sources: dict = field(default_factory=dict)


def evaluate(
    sig_mid: float | None,
    quotes: list[SourceQuote],
    *,
    min_sources: int = 2,
    agree_tol: float = 0.03,
    min_divergence: float = 0.05,
    max_age_s: float = 120.0,
) -> SharpVerdict:
    """Decide whether a SIG-vs-sharp divergence is tradeable.

    Gates, in order (fail-closed):
      1. keep only SHARP sources with a fresh, present aligned price (age <= max_age_s);
      2. require >= min_sources of them (default 2 — the corroboration rule);
      3. require them to AGREE: spread (max-min) <= agree_tol, else the truth is unknown -> stand down;
      4. consensus = mean of the agreeing sources; divergence = consensus - sig_mid;
      5. require |divergence| >= min_divergence (the 5-10% "confident bet" bar).
    """
    fresh = [q for q in quotes
             if q.source in SHARP_SOURCES and q.aligned is not None and q.age_s <= max_age_s]
    srcmap = {q.source: round(float(q.aligned), 4) for q in fresh}
    if len(fresh) < min_sources:
        return SharpVerdict(False, "insufficient_sources", n_sources=len(fresh), sources=srcmap)
    prices = [float(q.aligned) for q in fresh]
    spread = max(prices) - min(prices)
    if spread > agree_tol:
        return SharpVerdict(False, "sources_disagree", n_sources=len(fresh), spread=round(spread, 4), sources=srcmap)
    agreed = mean(prices)
    if sig_mid is None:
        return SharpVerdict(False, "no_sig_price", agreed_sharp=round(agreed, 4),
                            n_sources=len(fresh), spread=round(spread, 4), sources=srcmap)
    divergence = agreed - float(sig_mid)
    if abs(divergence) < min_divergence:
        return SharpVerdict(False, "below_min_divergence", agreed_sharp=round(agreed, 4),
                            divergence=round(divergence, 4), n_sources=len(fresh),
                            spread=round(spread, 4), sources=srcmap)
    return SharpVerdict(True, "tradeable", agreed_sharp=round(agreed, 4), divergence=round(divergence, 4),
                        n_sources=len(fresh), spread=round(spread, 4), sources=srcmap)
