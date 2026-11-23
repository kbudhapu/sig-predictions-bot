"""Verify the deterministic fair-value math (log-odds aggregation, extremizing, σ_p). No network, no LLM."""
import os
import sys
import math
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.fair_value import (
    aggregate_fair_value,
    SourceEstimate,
    to_logodds,
    from_logodds,
)


def test_logodds_roundtrip():
    for p in (0.01, 0.2, 0.5, 0.65, 0.99):
        assert math.isclose(from_logodds(to_logodds(p)), p, abs_tol=1e-9)
    # 0.5 ↔ 0 exactly
    assert math.isclose(to_logodds(0.5), 0.0, abs_tol=1e-12)


def test_logodds_clamps_extremes():
    # p=0 / p=1 would be ±inf; clamp keeps it finite.
    assert math.isfinite(to_logodds(0.0))
    assert math.isfinite(to_logodds(1.0))


def test_three_sources_aggregate_in_logodds_not_probability():
    est = [
        SourceEstimate("polymarket", 0.60),
        SourceEstimate("kalshi", 0.65),
        SourceEstimate("models_polls", 0.70),
    ]
    fv = aggregate_fair_value(est, extremize_factor=1.2)
    lbar = (to_logodds(0.60) + to_logodds(0.65) + to_logodds(0.70)) / 3
    # the un-extremized log-odds mean back-transforms to ~0.6511 (NOT the prob-mean 0.65)
    assert math.isclose(from_logodds(lbar), 0.6511, abs_tol=1e-3)
    # extremized p = sigmoid(1.2 * lbar)
    assert math.isclose(fv.p, from_logodds(1.2 * lbar), abs_tol=1e-9)
    assert math.isclose(fv.p, 0.6789, abs_tol=1e-3)
    assert fv.sources_used == ["kalshi", "models_polls", "polymarket"]
    assert fv.n_source_classes == 3


def test_extremize_pushes_away_from_half_both_sides():
    # favorite (>0.5): extremized > un-extremized
    up = aggregate_fair_value([SourceEstimate("a", 0.6), SourceEstimate("b", 0.7), SourceEstimate("c", 0.65)],
                              extremize_factor=1.3)
    lbar_up = (to_logodds(0.6) + to_logodds(0.7) + to_logodds(0.65)) / 3
    assert up.p > from_logodds(lbar_up) > 0.5
    # longshot (<0.5): extremized < un-extremized (pushed toward 0)
    dn = aggregate_fair_value([SourceEstimate("a", 0.4), SourceEstimate("b", 0.3), SourceEstimate("c", 0.35)],
                              extremize_factor=1.3)
    lbar_dn = (to_logodds(0.4) + to_logodds(0.3) + to_logodds(0.35)) / 3
    assert dn.p < from_logodds(lbar_dn) < 0.5
    # a=1.0 is a no-op (no extremizing)
    flat = aggregate_fair_value([SourceEstimate("a", 0.6), SourceEstimate("b", 0.7)], extremize_factor=1.0)
    assert math.isclose(flat.p, from_logodds((to_logodds(0.6) + to_logodds(0.7)) / 2), abs_tol=1e-9)


def test_sigma_reflects_dispersion_tight_vs_spread():
    tight = aggregate_fair_value([SourceEstimate("a", 0.6), SourceEstimate("b", 0.6), SourceEstimate("c", 0.6)])
    spread = aggregate_fair_value([SourceEstimate("a", 0.55), SourceEstimate("b", 0.65), SourceEstimate("c", 0.90)])
    assert tight.sigma_p < spread.sigma_p
    assert tight.sigma_logodds < spread.sigma_logodds


def test_single_source_is_maximally_uncertain():
    one = aggregate_fair_value([SourceEstimate("solo", 0.6)])
    many = aggregate_fair_value([SourceEstimate("a", 0.58), SourceEstimate("b", 0.60), SourceEstimate("c", 0.62)])
    assert one.sigma_p > many.sigma_p
    assert one.n_source_classes == 1


def test_correlated_sources_collapse_to_one_class():
    # three model/poll sources sharing ONE class count once (effective-N, §3) → NOT three independent draws
    collapsed = aggregate_fair_value([
        SourceEstimate("models_polls", 0.60),
        SourceEstimate("models_polls", 0.64),
        SourceEstimate("models_polls", 0.68),
    ])
    assert collapsed.n_source_classes == 1   # one effective class
    assert collapsed.sources_used == ["models_polls"]


def test_staleness_inflates_sigma():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    fresh = aggregate_fair_value(
        [SourceEstimate("a", 0.6, now), SourceEstimate("b", 0.7, now)], now=now)
    stale = aggregate_fair_value(
        [SourceEstimate("a", 0.6, now - timedelta(hours=72)), SourceEstimate("b", 0.7, now)], now=now)
    assert stale.sigma_p > fresh.sigma_p
    assert stale.stale is True and fresh.stale is False


def test_probability_bounds_and_sigma_bounds():
    fv = aggregate_fair_value([SourceEstimate("a", 0.999), SourceEstimate("b", 0.001)], extremize_factor=1.3)
    assert 0.0 < fv.p < 1.0
    assert 0.0 <= fv.sigma_p <= 0.5


def test_empty_raises():
    try:
        aggregate_fair_value([])
    except ValueError:
        return
    assert False, "expected ValueError on empty estimates"
