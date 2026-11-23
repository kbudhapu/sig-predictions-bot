"""The edge base: f*, the per-position uncertainty shrinkage k(σ_p), and n_eff.

f* — Kelly for a binary prediction-market contract (§2.1)
    buy YES at p_m with fair value p:   f* = (p − p_m) / (1 − p_m)
    buy NO  at (1−p_m), fair value 1−p: f* = ((1−p) − (1−p_m)) / (1 − (1−p_m)) = (p_m − p) / p_m
The two are the same expression with the roles of the outcome swapped, which is what "symmetric form
for NO" means. In both cases the denominator is (1 − entry_price): the amount you WIN per contract if
right, so f* is edge-per-unit-won, and shares = f* · W / entry_price.

k(σ_p) — §2.1a, Baker–McHale
k IS the per-position Kelly fraction, not a multiplier applied on top of a flat quarter. The spec is
explicit: "Quarter-Kelly is the AVERAGE this produces across a mixed-confidence book — not a floor
applied uniformly", with k → ~0.5 for confident and → ~0.1 or less for shaky estimates. So a very
confident edge gets half-Kelly, a mid-confidence edge gets exactly the quarter-Kelly base, and a shaky
one gets a tenth or less. (Multiplying 0.25 × k instead would put the book average near 1/16-Kelly,
which contradicts the spec's own statement about what the average should be. The grid in config is
calibrated so σ_p == k_reference_sigma returns kelly_base_fraction exactly.)

This is the module's main defence against the §1b failure mode: estimation error is a SYSTEMATIC bias,
because the positions whose edge was overestimated are disproportionately the uncertain ones, and
Kelly-on-estimates bets biggest on exactly those. Shrinking hardest where σ_p is largest pushes back
on that tilt directly, rather than hoping it averages out — it does not average out.
"""

from __future__ import annotations

import math

YES = "yes"
NO = "no"


def entry_price(p_m: float, side: str) -> float:
    """What one contract costs on the given side. YES costs p_m; NO costs 1 − p_m."""
    return float(p_m) if side == YES else 1.0 - float(p_m)


def kelly_fraction_star(p: float, p_m: float, side: str) -> float:
    """f* for the chosen side. Returns <= 0 when there is no edge on that side.

    Guards the degenerate book: an entry price of 0 or 1 means the contract is already settled in
    price terms, and dividing by (1 − entry) would explode. No edge is claimable there.
    """
    p, p_m = float(p), float(p_m)
    e = entry_price(p_m, side)
    if not (0.0 < e < 1.0):
        return 0.0
    fair = p if side == YES else 1.0 - p
    return (fair - e) / (1.0 - e)


def k_of_sigma(
    sigma_p: float | None,
    *,
    sigma_grid: tuple[float, ...],
    k_grid: tuple[float, ...],
    k_missing_sigma: float,
) -> float:
    """Piecewise-linear k(σ_p), decreasing. A MISSING σ_p returns k_missing_sigma (0.0 by default).

    'No reported uncertainty' is treated as maximally uncertain, not as confident — §6 states it
    outright, and it is the only safe reading: an estimate that declines to say how sure it is cannot
    be staked on. A NaN is treated the same way.
    """
    if sigma_p is None:
        return float(k_missing_sigma)
    s = float(sigma_p)
    if math.isnan(s) or s < 0.0:
        return float(k_missing_sigma)
    if s <= sigma_grid[0]:
        return float(k_grid[0])
    if s >= sigma_grid[-1]:
        return float(k_grid[-1])
    for i in range(1, len(sigma_grid)):
        if s <= sigma_grid[i]:
            lo_s, hi_s = sigma_grid[i - 1], sigma_grid[i]
            lo_k, hi_k = k_grid[i - 1], k_grid[i]
            w = (s - lo_s) / (hi_s - lo_s) if hi_s > lo_s else 0.0
            return float(lo_k + w * (hi_k - lo_k))
    return float(k_grid[-1])


def correlation_from_loadings(loadings: list[float]) -> list[list[float]]:
    """Single-factor correlation matrix: C_ij = β_i·β_j off-diagonal, 1 on the diagonal.

    The shared national-swing factor is the dominant correlation in a midterm book, so one loading per
    race captures most of it without needing a full estimated matrix.
    """
    n = len(loadings)
    return [
        [1.0 if i == j else float(loadings[i]) * float(loadings[j]) for j in range(n)]
        for i in range(n)
    ]


def effective_n(weights: list[float], correlation: list[list[float]]) -> float:
    """n_eff = (Σ wᵢ)² / (wᵀ C w) — the participation ratio from §6.

    1.0 when every position loads fully on one factor (a 10-position book sharing one swing factor is
    ONE bet, and must be scaled as one), N when they are independent. Clamped to [1, N] so numerical
    noise in C cannot produce n_eff > N and thereby *inflate* size — the clamp direction matters, since
    an over-large n_eff would under-scale the divisor and over-bet.
    """
    w = [abs(float(x)) for x in weights]
    n = len(w)
    if n == 0:
        return 1.0
    total = sum(w)
    if total <= 0.0:
        return 1.0
    quad = 0.0
    for i in range(n):
        for j in range(n):
            quad += w[i] * w[j] * float(correlation[i][j])
    if quad <= 0.0:
        return 1.0
    return min(max((total * total) / quad, 1.0), float(n))


def diversification_divisor(n_eff: float) -> float:
    """√n_eff — the §2.1 portfolio-variance scaling, so book variance stays at single-bet level."""
    return math.sqrt(max(float(n_eff), 1.0))
