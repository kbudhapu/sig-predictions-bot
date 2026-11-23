"""Deterministic divergence / edge detection for the SIG research signal — NO LLM, pure math.

Two distinct, separately-reported edges (SIG_RESEARCH_SIGNAL_SPEC §4):
  • AGGREGATE divergence:    p − p_m^SIG        (our calibrated fair value vs SIG's price)
  • CROSS-MARKET divergence: p_m^SIG − p_m^venue (SIG's price vs a real-money venue's price)

Favorite–longshot flagging (§4, HYPOTHESIS 2 — unproven for this market, so flags are advisory):
  • p_m^SIG a FAVORITE (high) and p > p_m  → SIG under-pricing the favorite  → bias-consistent, trust more.
  • p_m^SIG a LONGSHOT (low)  and p < p_m  → SIG over-pricing the longshot   → bias-consistent, fade.
  • divergence AGAINST the known bias → more likely a broken estimate → SUSPECT.
  • mid-range divergence → noise-prone → LOW.

A SIG price that diverges from BOTH real-money venues while those venues AGREE with each other is the
highest-confidence case (SIG, the student market, is the likely-wrong outlier). This module only
DETECTS/labels; the risk layer's concave divergence-skepticism curve does the shrinking of suspicious
edges — so nothing here is clamped for plausibility.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum


class FavoriteLongshotFlag(str, Enum):
    UNDERPRICED_FAVORITE = "underpriced_favorite"  # p_m high, p>p_m — bias-consistent (lean YES / buy)
    OVERPRICED_LONGSHOT = "overpriced_longshot"    # p_m low,  p<p_m — bias-consistent (fade / sell)
    AGAINST_BIAS = "against_bias"                  # divergence opposes the known bias — suspect
    MIDRANGE = "midrange"                          # p_m mid — divergence is noise-prone
    NONE = "none"                                  # no material divergence


class ConfidenceTier(str, Enum):
    HIGH = "high"        # real-money consensus outlier, or bias-consistent favorite/longshot edge
    MEDIUM = "medium"    # material divergence, no structural support
    LOW = "low"          # small or mid-range divergence
    SUSPECT = "suspect"  # divergence against the documented bias


@dataclass(frozen=True)
class DivergenceResult:
    aggregate_divergence: float              # p − p_m^SIG  (signed: + ⇒ SIG too low)
    cross_market: dict[str, float]           # venue → (p_m^SIG − p_m^venue); + ⇒ SIG above that venue
    fl_flag: FavoriteLongshotFlag
    confidence_tier: ConfidenceTier
    real_money_consensus_outlier: bool       # ≥2 venues agree AND SIG is far from their mean
    notes: list[str] = field(default_factory=list)


def aggregate_divergence(p: float, p_m: float) -> float:
    """p − p_m^SIG. Positive ⇒ our fair value is above SIG's price (SIG under-pricing YES)."""
    return float(p) - float(p_m)


def cross_market_divergence(p_m_sig: float, p_m_venue: float) -> float:
    """p_m^SIG − p_m^venue. Positive ⇒ SIG prices YES higher than the real-money venue does."""
    return float(p_m_sig) - float(p_m_venue)


def assess_divergence(
    p: float,
    p_m_sig: float,
    *,
    real_money_prices: dict[str, float] | None = None,
    fav_threshold: float = 0.65,
    long_threshold: float = 0.35,
    material_edge: float = 0.02,
    agree_tol: float = 0.03,
    outlier_tol: float = 0.05,
) -> DivergenceResult:
    """Compute both divergences, the favorite–longshot flag, and a confidence tier. Deterministic.

    `real_money_prices` maps venue → that venue's YES price (e.g. {"polymarket": .71, "kalshi": .69}).
    `material_edge` is the |divergence| below which the edge is treated as negligible (LOW/NONE).
    """
    agg = aggregate_divergence(p, p_m_sig)
    notes: list[str] = []

    cross: dict[str, float] = {}
    if real_money_prices:
        for venue, price in real_money_prices.items():
            cross[venue] = cross_market_divergence(p_m_sig, price)

    # ── favorite–longshot classification (direction relative to the documented bias) ──
    if p_m_sig >= fav_threshold:
        if agg > material_edge:
            fl = FavoriteLongshotFlag.UNDERPRICED_FAVORITE
            notes.append("SIG under-pricing a favorite (p>p_m, p_m high) — favorite-longshot-consistent.")
        elif agg < -material_edge:
            fl = FavoriteLongshotFlag.AGAINST_BIAS
            notes.append("SIG over-pricing a favorite (p<p_m) — AGAINST the documented bias → suspect.")
        else:
            fl = FavoriteLongshotFlag.NONE
    elif p_m_sig <= long_threshold:
        if agg < -material_edge:
            fl = FavoriteLongshotFlag.OVERPRICED_LONGSHOT
            notes.append("SIG over-pricing a longshot (p<p_m, p_m low) — favorite-longshot-consistent (fade).")
        elif agg > material_edge:
            fl = FavoriteLongshotFlag.AGAINST_BIAS
            notes.append("SIG under-pricing a longshot (p>p_m) — AGAINST the documented bias → suspect.")
        else:
            fl = FavoriteLongshotFlag.NONE
    else:
        fl = FavoriteLongshotFlag.MIDRANGE if abs(agg) > material_edge else FavoriteLongshotFlag.NONE
        if fl is FavoriteLongshotFlag.MIDRANGE:
            notes.append("Mid-range SIG price — divergence is more likely noise.")

    # ── real-money consensus outlier: ≥2 venues agree with each other, SIG far from their mean ──
    outlier = False
    if len(cross) >= 2:
        venue_prices = list(real_money_prices.values())  # type: ignore[union-attr]
        spread = max(venue_prices) - min(venue_prices)
        venue_mean = statistics.fmean(venue_prices)
        if spread <= agree_tol and abs(p_m_sig - venue_mean) >= outlier_tol:
            outlier = True
            notes.append(
                f"Real-money venues agree (spread {spread:.3f} ≤ {agree_tol}); SIG is the outlier "
                f"(|p_m − venue_mean| {abs(p_m_sig - venue_mean):.3f} ≥ {outlier_tol}) → high-confidence edge."
            )

    # ── confidence tier ──
    if abs(agg) < material_edge and not outlier:
        tier = ConfidenceTier.LOW
    elif outlier:
        tier = ConfidenceTier.HIGH
    elif fl in (FavoriteLongshotFlag.UNDERPRICED_FAVORITE, FavoriteLongshotFlag.OVERPRICED_LONGSHOT):
        tier = ConfidenceTier.HIGH
    elif fl is FavoriteLongshotFlag.AGAINST_BIAS:
        tier = ConfidenceTier.SUSPECT
    elif fl is FavoriteLongshotFlag.MIDRANGE:
        tier = ConfidenceTier.LOW
    else:
        tier = ConfidenceTier.MEDIUM

    return DivergenceResult(
        aggregate_divergence=agg,
        cross_market=cross,
        fl_flag=fl,
        confidence_tier=tier,
        real_money_consensus_outlier=outlier,
        notes=notes,
    )
