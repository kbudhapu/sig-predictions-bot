"""Verify deterministic divergence detection + favorite–longshot flagging + confidence tiers. No network."""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.divergence import (
    aggregate_divergence,
    cross_market_divergence,
    assess_divergence,
    FavoriteLongshotFlag,
    ConfidenceTier,
)


def test_raw_divergences():
    assert math.isclose(aggregate_divergence(0.72, 0.60), 0.12)
    assert math.isclose(cross_market_divergence(0.60, 0.71), -0.11)  # SIG below the venue


def test_underpriced_favorite_is_high_confidence():
    # SIG prices a favorite at 0.70 but fair value says 0.80 → SIG under-pricing the favorite (bias-consistent)
    d = assess_divergence(p=0.80, p_m_sig=0.70)
    assert d.fl_flag is FavoriteLongshotFlag.UNDERPRICED_FAVORITE
    assert d.confidence_tier is ConfidenceTier.HIGH
    assert d.aggregate_divergence > 0


def test_overpriced_longshot_is_high_confidence_fade():
    # SIG prices a longshot at 0.30 but fair value says 0.18 → SIG over-pricing the longshot → fade
    d = assess_divergence(p=0.18, p_m_sig=0.30)
    assert d.fl_flag is FavoriteLongshotFlag.OVERPRICED_LONGSHOT
    assert d.confidence_tier is ConfidenceTier.HIGH
    assert d.aggregate_divergence < 0


def test_against_bias_is_suspect():
    # SIG over-pricing a FAVORITE (p<p_m while p_m high) runs against the documented bias → suspect
    d = assess_divergence(p=0.62, p_m_sig=0.78)
    assert d.fl_flag is FavoriteLongshotFlag.AGAINST_BIAS
    assert d.confidence_tier is ConfidenceTier.SUSPECT


def test_midrange_divergence_is_low_confidence():
    d = assess_divergence(p=0.58, p_m_sig=0.50)
    assert d.fl_flag is FavoriteLongshotFlag.MIDRANGE
    assert d.confidence_tier is ConfidenceTier.LOW


def test_negligible_divergence_is_low():
    d = assess_divergence(p=0.701, p_m_sig=0.70)
    assert abs(d.aggregate_divergence) < 0.02
    assert d.confidence_tier is ConfidenceTier.LOW


def test_real_money_consensus_outlier_high_confidence():
    # both venues agree (~0.70) and SIG is the outlier (0.60) → high-confidence standalone edge
    d = assess_divergence(p=0.70, p_m_sig=0.60, real_money_prices={"polymarket": 0.71, "kalshi": 0.70})
    assert d.real_money_consensus_outlier is True
    assert d.confidence_tier is ConfidenceTier.HIGH
    # cross-market divergences are reported per venue
    assert math.isclose(d.cross_market["polymarket"], 0.60 - 0.71, abs_tol=1e-9)
    assert math.isclose(d.cross_market["kalshi"], 0.60 - 0.70, abs_tol=1e-9)


def test_venues_disagree_not_outlier():
    # venues far apart → NOT a consensus outlier even if SIG differs
    d = assess_divergence(p=0.65, p_m_sig=0.60, real_money_prices={"polymarket": 0.52, "kalshi": 0.74})
    assert d.real_money_consensus_outlier is False
