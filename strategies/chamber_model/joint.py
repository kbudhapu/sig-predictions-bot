"""The JOINT-DISTRIBUTION layer — a HYPOTHESIS, not a result. Read this before trusting a number.

WHAT THIS DOES
    Individual-race prices are marginals. Chamber control is a function of the JOINT distribution,
    and the two are only reconcilable if you assume something about how races move together. This
    module assumes the simplest defensible thing -- ONE national swing factor -- converts the
    market's marginals into a distribution over seat counts, and reports where the implied control
    probability sits versus the control market's own price.

WHY IT IS NOT TRUSTWORTHY YET
    The output is dominated by `rho`, the common-factor correlation, which is NOT measured. It is a
    guess in a config file. Concretely:
      * rho -> 0  : races independent. Seat totals concentrate tightly around the sum of marginals,
                    so extreme control outcomes look nearly impossible and the model will scream
                    that any control price far from 0/1 is mispriced.
      * rho -> 1  : races move as one. Seat totals become nearly bimodal (one side sweeps) and the
                    model will instead insist control should be priced near the average marginal.
    These give OPPOSITE trading signals from the same prices. Nothing in the data pins rho down,
    and with ~35 correlated binaries resolving on ONE day there is exactly one observation of the
    national swing to learn from -- so rho is not estimable from this competition's own outcome.
    That is why this edge is TAIL-tier and logged-before-sized. The structural layer (baselines.py)
    is provable; this is not, and the code does not pretend otherwise.

WHAT WOULD MAKE IT TRUSTWORTHY
    Out-of-sample agreement between `implied_control_probability` and the control market across
    many independent snapshots, logged by `signal.py` BEFORE any meaningful capital is attached.
    Prove, then size.

THE MATH (standard one-factor Gaussian copula — chosen because it is the simplest thing that is
not obviously wrong, and because its single parameter makes the assumption auditable)
    latent_i = sqrt(rho)*S + sqrt(1-rho)*e_i ,   S, e_i ~ iid N(0,1)
    seat i is won iff latent_i > -Phi^-1(p_i)
    => conditional on S = s, P(win_i | s) = Phi( (Phi^-1(p_i) + sqrt(rho)*s) / sqrt(1-rho) )
    Conditional on s the races are INDEPENDENT, so the conditional seat-count law is a
    Poisson-binomial (exact convolution), and the unconditional law integrates that over s on a
    Gauss-Legendre grid. No simulation: the result is deterministic and reproducible, which matters
    because a signal that differs run-to-run cannot be evaluated after the fact.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

_N = NormalDist()

#: Probabilities are clamped off the boundary: Phi^-1(0) is -inf and would poison the convolution.
_P_EPS = 1e-6


def _clamp_p(p: float) -> float:
    return min(max(float(p), _P_EPS), 1.0 - _P_EPS)


def _gauss_legendre(n: int) -> tuple[list[float], list[float]]:
    """Nodes/weights for integrating over the standard normal, via a truncated grid on [-6, 6].

    A plain trapezoid on a wide grid is used rather than true Gauss-Hermite because it is
    transparent, has no special-function dependency, and the integrand (a smooth CDF composition)
    is well behaved. +-6 sigma captures the factor to ~2e-9, far below the precision this model
    deserves given rho is a guess.
    """
    n = max(int(n), 9)
    lo, hi = -6.0, 6.0
    step = (hi - lo) / (n - 1)
    nodes = [lo + i * step for i in range(n)]
    dens = [_N.pdf(x) for x in nodes]
    # trapezoid weights, renormalised so they sum to exactly 1 (the truncation loses ~1e-9)
    w = []
    for i, d in enumerate(dens):
        edge = 0.5 if i in (0, n - 1) else 1.0
        w.append(d * step * edge)
    total = sum(w) or 1.0
    return nodes, [x / total for x in w]


def conditional_win_probs(marginals: list[float], *, rho: float, s: float) -> list[float]:
    """P(win_i | national swing = s) for every race, under the one-factor model."""
    r = min(max(float(rho), 0.0), 0.999999)
    a = math.sqrt(r)
    b = math.sqrt(1.0 - r)
    out = []
    for p in marginals:
        z = _N.inv_cdf(_clamp_p(p))
        out.append(_N.cdf((z + a * s) / b) if b > 0 else (1.0 if z + a * s > 0 else 0.0))
    return out


def poisson_binomial(probs: list[float]) -> list[float]:
    """Exact distribution of the number of successes among independent Bernoullis.

    O(n^2) convolution. n is ~35 here, so exactness is free and Monte Carlo would only add noise
    to a model whose real error is the rho assumption.
    """
    dist = [1.0]
    for p in probs:
        p = min(max(float(p), 0.0), 1.0)
        nxt = [0.0] * (len(dist) + 1)
        for k, acc in enumerate(dist):
            if acc == 0.0:
                continue
            nxt[k] += acc * (1.0 - p)
            nxt[k + 1] += acc * p
        dist = nxt
    return dist


@dataclass(frozen=True)
class SeatDistribution:
    """The joint law of seats-won among the priced races. `hypothesis` is not decoration."""

    seats_won_pmf: tuple[float, ...]        # index = seats won among the priced races
    rho: float
    n_races: int
    expected_seats: float
    hypothesis: bool = True

    def p_at_least(self, k: int) -> float:
        if k <= 0:
            return 1.0
        if k > len(self.seats_won_pmf) - 1:
            return 0.0
        return sum(self.seats_won_pmf[k:])


def seat_distribution(marginals: list[float], *, rho: float, quadrature_nodes: int = 65
                      ) -> SeatDistribution:
    """Integrate the conditional Poisson-binomial over the national swing factor."""
    ms = [_clamp_p(p) for p in (marginals or [])]
    n = len(ms)
    if n == 0:
        return SeatDistribution((1.0,), float(rho), 0, 0.0)
    nodes, weights = _gauss_legendre(quadrature_nodes)
    acc = [0.0] * (n + 1)
    for s, w in zip(nodes, weights):
        cond = conditional_win_probs(ms, rho=rho, s=s)
        pmf = poisson_binomial(cond)
        for k, v in enumerate(pmf):
            acc[k] += w * v
    total = sum(acc) or 1.0
    acc = [v / total for v in acc]
    expected = sum(k * v for k, v in enumerate(acc))
    return SeatDistribution(tuple(acc), float(rho), n, expected)


@dataclass(frozen=True)
class ControlEstimate:
    """What the model thinks, and -- equally important -- why you may not be able to use it."""

    chamber: str
    party: str
    implied_control_probability: float
    market_control_probability: float | None
    divergence: float | None                 # model - market; None when the market is unpriced
    seats_needed_from_priced: int
    expected_seats: float
    n_priced_races: int
    n_expected_races: int
    rho: float
    degraded: bool                           # True => NOT tradeable, log only
    degraded_reasons: tuple[str, ...] = ()
    hypothesis: bool = True
    meta: dict = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return not self.degraded and self.market_control_probability is not None


def estimate_control(
    chamber,
    party: str,
    *,
    race_prices: dict[str, float],
    market_control_price: float | None = None,
    rho: float,
    assume_unpriced_held_by_incumbent_party: bool = False,
    quadrature_nodes: int = 65,
) -> ControlEstimate:
    """Model P(`party` controls `chamber`) from the individual-race prices.

    `race_prices` maps state -> P(`party` wins that seat). Coverage is checked against the baseline
    rather than assumed, because the failure is silent and severe: SIG does not list the Ohio Senate
    special, so a naive run would compute control over 34 of 35 up-seats and report it as if it
    covered the chamber. A chamber whose seat set is incomplete cannot have its control priced --
    the missing seat is exactly as decisive as any other.

    `assume_unpriced_held_by_incumbent_party` lets the caller fill gaps with the structural prior
    "the party holding it keeps it". That is an ASSUMPTION, so using it still marks the estimate
    DEGRADED: it is recorded for evaluation, never sized into.
    """
    party = str(party).strip().casefold()
    reasons: list[str] = []

    priced: list[float] = []
    assumed_won = 0
    missing: list[str] = []
    for seat in chamber.seats_up:
        if seat.state in chamber.indeterminate_states:
            # A listed Independent means the outcome space is not two-way, and a winning
            # independent's effect on control depends on who they caucus with -- unknowable now.
            reasons.append(f"indeterminate_three_way:{seat.state}")
            continue
        p = race_prices.get(seat.state)
        if p is None:
            missing.append(seat.state)
            if assume_unpriced_held_by_incumbent_party:
                assumed_won += 1 if seat.party == party else 0
            continue
        priced.append(_clamp_p(p))

    if missing:
        reasons.append("unpriced_seats:" + ",".join(sorted(missing)))
    if chamber.indeterminate_states:
        # Seats removed from the convolution still count toward control if won, so the threshold
        # below is computed over the PRICED set only and the gap is reported, not hidden.
        pass

    dist = seat_distribution(priced, rho=rho, quadrature_nodes=quadrature_nodes)

    # Threshold over the priced subset: total needed, minus seats assumed won outside it.
    needed_total = chamber.seats_needed_from_up(party)
    needed_from_priced = max(0, needed_total - assumed_won)
    implied = dist.p_at_least(needed_from_priced)

    n_expected = len(chamber.seats_up)
    if len(priced) + assumed_won + len(chamber.indeterminate_states) < n_expected:
        reasons.append("incomplete_seat_coverage")
    if not chamber.has_control_market:
        reasons.append("no_control_market_for_this_chamber")
    if market_control_price is None:
        reasons.append("control_market_unpriced")

    divergence = None
    if market_control_price is not None:
        divergence = implied - _clamp_p(market_control_price)

    return ControlEstimate(
        chamber=chamber.name,
        party=party,
        implied_control_probability=implied,
        market_control_probability=market_control_price,
        divergence=divergence,
        seats_needed_from_priced=needed_from_priced,
        expected_seats=dist.expected_seats,
        n_priced_races=len(priced),
        n_expected_races=n_expected,
        rho=float(rho),
        degraded=bool(reasons),
        degraded_reasons=tuple(reasons),
        meta={
            "needed_total_from_up": needed_total,
            "assumed_won_unpriced": assumed_won,
            "missing_states": sorted(missing),
            "indeterminate_states": sorted(chamber.indeterminate_states),
            "seats_not_up": dict(chamber.seats_not_up),
            "control_threshold": dict(chamber.control_threshold),
            # So a reviewer can see how load-bearing the guess is without re-running anything.
            "rho_is_a_hypothesis": True,
        },
    )


def rho_sensitivity(chamber, party: str, *, race_prices: dict[str, float],
                    rhos: tuple[float, ...] = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9),
                    **kw) -> dict[float, float]:
    """Implied control probability across a sweep of rho.

    This exists to be LOOKED AT before anyone trusts the signal. If the spread across this sweep is
    wide, the "mispricing" the model reports is a statement about rho, not about the market.
    """
    out: dict[float, float] = {}
    for r in rhos:
        out[r] = estimate_control(chamber, party, race_prices=race_prices, rho=r,
                                  **kw).implied_control_probability
    return out
