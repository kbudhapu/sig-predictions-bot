"""Benchmark CONVICTION weighting — the shared primitive behind every divergence/size/stop/fade call.

The edges all bet that an external BENCHMARK (Polymarket real-money, or Wikipedia ratings) is closer to
truth than SIG's student price. So every signal should be weighted by how CONVINCED that benchmark is —
and a benchmark is most convinced (and empirically most accurate) at the EXTREMES, least in the mushy
middle near 0.50. Two inputs:

  • DISTANCE FROM 0.50 — the base. A benchmark at 0.95 "knows"; at 0.50 is a shrug. The default is the
    linear `2·|p − 0.5|` (0 at 0.50 → 1 at the extremes). `gamma > 1` bends it CONVEX — low through the
    middle, climbing to the extremes — which the Polymarket-overlap calibration motivates (ratings match
    at the extremes, undershoot in the middle); it is left tunable (default 1.0 = linear) because too
    much convexity would stop a confident mid-range benchmark (~0.72) from ever clearing a small gap.
  • QUALITY — multiplies in. A SHARP benchmark (Polymarket, ~real-money) at 0.72 is more convinced than a
    COARSE one (Wikipedia ratings, ~7pt error) at the same 0.72. So a coarse benchmark must be MORE
    extreme to earn the same conviction.

conviction = clamp( (2·|p − 0.5|)^gamma · quality , 0, 1 ).  Pure, monotonic in |p−0.5|, 0 at 0.50.

THE ASYMMETRY this produces (the key insight): conviction is read at the BENCHMARK's level, so the SAME
gap is weighted differently by WHERE the benchmark sits — SIG 0.67 vs benchmark 0.72 (read conviction at
0.72, decent → tradeable) is stronger than SIG 0.72 vs benchmark 0.67 (read at 0.67, mushier → "eh").

THE COST FLOOR sits ON TOP and conviction can NEVER override it: `effective_threshold` is floored at the
round-trip cost, so a high-conviction gap thinner than cost is still refused.
"""
from __future__ import annotations

#: Benchmark quality → multiplier. Sharp real-money ≈ 1.0; coarse rating-derived ≈ 0.5 (the ~7pt error
#: the Polymarket-overlap calibration measured). Tunable; a coarse benchmark must be more extreme to earn
#: the same conviction, which correctly encodes "trust it less, especially in the middle".
DEFAULT_QUALITY: dict[str, float] = {
    "sharp": 1.0, "polymarket": 1.0, "real_money": 1.0,
    "rating_derived": 0.5, "wiki_ratings": 0.5, "coarse": 0.5,
}

DEFAULT_GAMMA = 1.0          # 1.0 = linear (2·|p−0.5|); raise >1 to bend convex (suppress the middle)
DEFAULT_COST_FLOOR = 0.02    # round-trip cost + margin (~2pt); NEVER trade a gap thinner than this


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def quality_factor(quality: str | float, *, table: dict[str, float] | None = None) -> float:
    """Resolve a quality name ('sharp'|'rating_derived'|…) or an explicit number in [0,1] to a factor."""
    if isinstance(quality, (int, float)):
        return _clamp(float(quality))
    return _clamp((table or DEFAULT_QUALITY).get(str(quality).strip().lower(), 0.5))


def conviction(
    benchmark_price: float,
    quality: str | float = "sharp",
    *,
    gamma: float = DEFAULT_GAMMA,
    table: dict[str, float] | None = None,
) -> float:
    """Benchmark conviction in [0, 1]: how strongly the benchmark's own level argues it is right.

    0 at a 0.50 toss-up (benchmark doesn't know), rising convexly toward 1 at the 0/1 extremes, scaled
    by the benchmark's quality (sharp ≈ 1.0, coarse ≈ 0.5). Read this AT THE BENCHMARK PRICE — that is
    what makes the gap weighting asymmetric (see the module docstring)."""
    p = _clamp(float(benchmark_price))
    dist = _clamp(abs(p - 0.5) * 2.0)                    # 0 at 0.5 → 1 at 0/1
    q = quality_factor(quality, table=table)
    return _clamp((dist ** max(gamma, 1e-6)) * q)


def effective_threshold(
    conv: float, *, cost_floor: float = DEFAULT_COST_FLOOR
) -> float:
    """The minimum gap worth trading, given conviction. A confident benchmark lets a SMALL gap through;
    a mushy one needs a BIG gap. ALWAYS floored at `cost_floor` (conviction can never let a sub-cost gap
    trade), and ∞ when conviction is ~0 (never trade a toss-up benchmark)."""
    cost_floor = max(0.0, cost_floor)
    if conv <= 1e-9:
        return float("inf")
    return max(cost_floor, cost_floor / conv)


def is_tradeable(gap: float, conv: float, *, cost_floor: float = DEFAULT_COST_FLOOR) -> bool:
    """A gap is tradeable iff |gap| clears the conviction-dependent threshold (which is itself floored at
    cost). Encodes 'high-conviction small gap OK; mushy-benchmark small gap not; nothing below cost'."""
    return abs(gap) >= effective_threshold(conv, cost_floor=cost_floor)


def conviction_size_multiplier(gap: float, conv: float) -> float:
    """The factor applied ON TOP of the existing Browne/Kelly size: |gap| × conviction. Bigger when the
    benchmark is confident AND the gap is large; small when either is weak. Non-negative."""
    return max(0.0, abs(gap)) * _clamp(conv)


def conviction_gated_stop(
    pnl_pct: float,
    benchmark_price: float | None,
    is_long: bool,
    *,
    quality: str | float = "sharp",
    hard_stop: float = 0.20,
    support_conviction: float = 0.30,
    gamma: float = DEFAULT_GAMMA,
    table: dict[str, float] | None = None,
) -> tuple[bool, str]:
    """Should a losing directional position be STOPPED? Returns (stop, reason).

    The powerful idea: a price drop is not itself a reason to cut if the sharp BENCHMARK still backs us
    with conviction — that is a discount, not a mistake. So above the hard backstop we stop only when the
    benchmark's conviction for our side has EVAPORATED (moved toward 0.50 / to the wrong side).

      • `pnl_pct <= -hard_stop`                → STOP ("hard_stop_backstop"), ALWAYS (the -20% floor; a
        collapsing position is cut regardless of what the benchmark says — the backstop conviction
        cannot override).
      • else, benchmark still on our side (price > 0.5 for a long / < 0.5 for a short) AND its conviction
        ≥ `support_conviction`  → DON'T stop ("benchmark_still_backs"): hold (or treat as add-more).
      • else (conviction gone / benchmark flipped, or no benchmark) → STOP ("conviction_evaporated").

    Caller gates this behind a toggle (default off); when off it uses the plain price stop-loss.
    """
    if pnl_pct <= -abs(hard_stop):
        return (True, "hard_stop_backstop")              # hard floor — conviction can NEVER veto this
    if benchmark_price is None:
        return (True, "no_benchmark")                    # nothing backs us → fall back to stopping
    conv = conviction(benchmark_price, quality, gamma=gamma, table=table)
    on_our_side = (benchmark_price > 0.5) if is_long else (benchmark_price < 0.5)
    if on_our_side and conv >= support_conviction:
        return (False, "benchmark_still_backs")          # the confident sharp money still agrees → hold
    return (True, "conviction_evaporated")
