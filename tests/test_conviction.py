"""signals/conviction.py — the benchmark-conviction primitive.

Pins: 0 at the toss-up, high at the extremes, monotone in distance-from-0.5, the quality multiplier
(coarse < sharp at the same price), THE ASYMMETRY (conviction read at the benchmark: SIG-below-a-
confident-0.72 beats SIG-above-a-mushier-0.67), and that the cost floor can NEVER be overridden.
"""
import math

import pytest

from signals.conviction import (
    DEFAULT_COST_FLOOR,
    conviction,
    conviction_size_multiplier,
    effective_threshold,
    is_tradeable,
    quality_factor,
)


def test_zero_at_tossup_high_at_extremes():
    assert conviction(0.50, "sharp") == 0.0
    assert conviction(0.50, "rating_derived") == 0.0
    assert conviction(0.95, "sharp") > 0.8
    assert conviction(0.05, "sharp") > 0.8                 # symmetric below 0.5
    assert conviction(0.30, "sharp") == pytest.approx(conviction(0.70, "sharp"))  # symmetry about 0.5


def test_monotonic_in_distance_from_half():
    xs = [0.50, 0.60, 0.70, 0.80, 0.90, 0.99]
    cs = [conviction(p, "sharp") for p in xs]
    assert all(b >= a for a, b in zip(cs, cs[1:]))         # non-decreasing toward the extreme


def test_quality_multiplies_in_coarse_below_sharp():
    assert conviction(0.72, "rating_derived") < conviction(0.72, "sharp")
    assert conviction(0.72, "rating_derived") == pytest.approx(conviction(0.72, "sharp") * 0.5)
    # a coarse benchmark must be MORE extreme to match a sharp one's conviction
    assert conviction(0.95, "rating_derived") < conviction(0.80, "sharp") * 1.5  # sanity: coarse discounted
    assert quality_factor("sharp") == 1.0 and quality_factor("wiki_ratings") == 0.5
    assert quality_factor(0.7) == 0.7 and quality_factor("unknown_src") == 0.5   # explicit num + default


def test_the_asymmetry_benchmark_level_gates_the_same_gap():
    # SIG 0.67 vs benchmark 0.72 (SIG below a confident benchmark): read conviction AT 0.72
    c_below = conviction(0.72, "sharp")
    # SIG 0.72 vs benchmark 0.67 (SIG above a mushier benchmark): read conviction AT 0.67
    c_above = conviction(0.67, "sharp")
    assert c_below > c_above                               # the same 5pt gap is weighted differently
    # and it flips the 5pt gap's tradeability: below-confident trades, above-mushy does not
    assert is_tradeable(0.05, c_below) is True
    assert is_tradeable(0.05, c_above) is False


def test_effective_threshold_confident_small_mushy_big_never_below_cost():
    hi = conviction(0.92, "sharp")                         # very confident
    lo = conviction(0.58, "sharp")                         # mushy
    assert effective_threshold(hi) < effective_threshold(lo)
    assert effective_threshold(hi) >= DEFAULT_COST_FLOOR   # floored at cost even when confident
    assert effective_threshold(0.0) == math.inf            # toss-up benchmark → never trade


def test_cost_floor_cannot_be_overridden_by_conviction():
    # full conviction, but a sub-cost (1.5pt) gap is STILL refused — cost floor is the hard guardrail
    assert is_tradeable(0.015, conviction(0.99, "sharp"), cost_floor=0.02) is False
    assert is_tradeable(0.05, conviction(0.99, "sharp"), cost_floor=0.02) is True
    # conviction can only RAISE the threshold above cost, never lower it below
    assert effective_threshold(1.0, cost_floor=0.02) == pytest.approx(0.02)


def test_conviction_gated_stop_holds_backed_cuts_evaporated_backstops_hard():
    from signals.conviction import conviction_gated_stop
    # long, -10%, benchmark STILL 0.85 (confident, our side) → HOLD (a discount, not a mistake)
    assert conviction_gated_stop(-0.10, 0.85, is_long=True) == (False, "benchmark_still_backs")
    # long, -10%, benchmark drifted to 0.52 (conviction gone) → STOP
    assert conviction_gated_stop(-0.10, 0.52, is_long=True) == (True, "conviction_evaporated")
    # long, -25% → the -20% hard backstop ALWAYS cuts, even with a confident benchmark
    assert conviction_gated_stop(-0.25, 0.95, is_long=True) == (True, "hard_stop_backstop")
    # short mirror: benchmark 0.15 (low = our side) still backs a short
    assert conviction_gated_stop(-0.10, 0.15, is_long=False)[0] is False
    # no benchmark → fall back to stopping
    assert conviction_gated_stop(-0.05, None, is_long=True)[0] is True


def test_size_multiplier_scales_with_gap_and_conviction():
    assert conviction_size_multiplier(0.10, 0.8) > conviction_size_multiplier(0.10, 0.3)   # conviction
    assert conviction_size_multiplier(0.20, 0.5) > conviction_size_multiplier(0.05, 0.5)   # gap
    assert conviction_size_multiplier(0.10, 0.0) == 0.0
    assert conviction_size_multiplier(-0.10, 0.5) == pytest.approx(0.05)                    # uses |gap|
