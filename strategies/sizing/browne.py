"""M(ρ, τ) — the Browne tournament multiplier, implemented as a BEHAVIORAL SHAPE.

READ THIS BEFORE CHANGING ANYTHING HERE.

Browne's closed form is
    α(π, t) = 1/(σ√(T−t)) · φ(ν)/Φ(ν),   ν = Φ⁻¹(π/b)
and the spec (§1, "Honest scope of the theory") is explicit that it does **NOT** apply to our
contracts: it assumes ONE continuously-rebalanced risky asset with constant volatility σ in a
Black–Scholes world and a fixed target. Our bets are DISCRETE, BINARY and SETTLE-AT-THE-END. Applying
that equation literally to a prediction-market contract would be wrong — it would be claiming a
continuous-time hedging control over an instrument that cannot be continuously rebalanced and whose
payoff is a step function.

So what is implemented here is the SHAPE, not the control. Three concrete consequences of that
distinction, visible in the code:

  1. There is NO σ (asset volatility) term. The literal formula divides by σ√(T−t); a volatility for a
     settle-at-end binary is not a meaningful input to a rebalancing rate, so it is absent. What
     remains is the τ-dependence (1/√τ) and the standing-dependence (φ(ν)/Φ(ν)).
  2. The result is NORMALISED by M_0 and returned as a DIMENSIONLESS MULTIPLIER on an
     independently-computed Kelly base — never as "the fraction of wealth to put at risk". Browne's α
     IS a fraction-at-risk; our M is a tilt. That is the whole architectural difference.
  3. It is CAPPED (M_cap) and its τ is floored (tau_floor). The continuous control genuinely diverges
     as t → T; a divergent number is meaningless for a discrete order size, so the divergence is
     truncated and the all-in cap takes over (§5).

What DOES transfer, and is reproduced faithfully, is the behaviour the spec enumerates:
  * aggressiveness increases as the deadline nears        (1/√τ)
  * aggressiveness increases the further BEHIND (ρ < 1)   (φ(ν)/Φ(ν) rises as ν → −∞)
  * aggressiveness decreases the further AHEAD (ρ > 1)    (φ(ν)/Φ(ν) → 0 as ν → +∞)
  * depends ONLY on ρ and τ — memoryless, no path state
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist

#: Standard normal. stdlib (no scipy dependency) and carries pdf/cdf/inv_cdf.
_N = NormalDist()

#: Below this the normal ratio is evaluated by its asymptotic form instead of 0/0 in float.
_NU_ASYMPTOTIC = -8.0


def _phi_over_Phi(nu: float) -> float:
    """φ(ν)/Φ(ν), the inverse Mills ratio — the distance-to-target term.

    For very negative ν both φ and Φ underflow and the naive quotient becomes 0/0 -> nan or inf.
    The asymptotic expansion φ(ν)/Φ(ν) ≈ −ν + 1/(−ν) keeps it finite and MONOTONE deep in the
    far-behind tail, which matters because that is exactly the region the endgame override reads: a
    nan there would propagate into an order size.
    """
    if nu <= _NU_ASYMPTOTIC:
        a = -nu
        return a + 1.0 / a
    cdf = _N.cdf(nu)
    if cdf <= 0.0:
        a = max(-nu, 1e-12)
        return a + 1.0 / a
    return _N.pdf(nu) / cdf


@dataclass(frozen=True, slots=True)
class BrowneState:
    """The complete (memoryless) input: standing and clock, nothing else."""

    rho: float      # balance / third-place threshold
    tau: float      # fraction of competition time remaining, 1 -> 0

    @staticmethod
    def from_standing(
        balance: float, third_place_threshold: float | None, time_fraction_remaining: float
    ) -> "BrowneState | None":
        """None when ρ is not computable — a missing or zero threshold must NOT silently become
        ρ=1 ('mid-field'), because that would quietly size as if the bot were on the bubble when its
        standing is actually unknown. The caller treats None as 'no directional sizing'."""
        if third_place_threshold is None or float(third_place_threshold) <= 0.0:
            return None
        return BrowneState(
            rho=float(balance) / float(third_place_threshold),
            tau=float(time_fraction_remaining),
        )


def standing_to_cdf_argument(rho: float, *, rho_min: float, rho_max: float) -> float:
    """Map ρ = B/T₃ into the (0,1) argument Φ⁻¹ needs, via the odds transform u = ρ/(1+ρ).

    WHY NOT THE SPEC'S LITERAL `ν = Φ⁻¹(ρ)` — this is a deliberate, load-bearing deviation.

    The spec writes `ν = Φ⁻¹(clamp(ρ, ρ_min, ρ_max))` and then states "ρ = 1 (at threshold): ν = 0".
    Those two are inconsistent: Φ⁻¹(1) = +∞, not 0. Taken literally the formula breaks twice:

      1. ρ = 1 clamps to ρ_max = 0.99, giving M ≈ 0.034 at the very point the spec and the config's
         `M_0 = 1.1284` both define as the M ≈ 1 reference. (M_0 is exactly (1/√0.5)·φ(0)/Φ(0), i.e.
         it pins ν = 0 — which under the literal map is ρ = 0.5, not ρ = 1.)
      2. Worse, EVERY ρ > 1 clamps to the same ρ_max, so ρ = 1.01 and ρ = 10 produce an identical
         multiplier. The spec's own required behaviour "aggressiveness DECREASES the further AHEAD"
         becomes unrepresentable — the formula cannot see how far ahead the bot is.

    The odds transform is monotone in ρ, defined on all of ρ > 0, and places ν = 0 exactly at ρ = 1,
    so the stated reference point, the config's M_0, and all four behavioural requirements hold
    simultaneously:

        ρ → 0   ⇒ u → 0   ⇒ ν → −∞ ⇒ φ/Φ large  ⇒ over-bet (far behind)
        ρ = 1   ⇒ u = 0.5 ⇒ ν = 0   ⇒ φ/Φ ≈ 0.80 ⇒ M ≈ 1 (at threshold, "moderate")
        ρ → ∞   ⇒ u → 1   ⇒ ν → +∞ ⇒ φ/Φ → 0    ⇒ de-risk toward cash (far ahead)

    This is the licence §1 grants explicitly: reproduce the SHAPE and the direction, never pretend the
    closed form is a literal solution. The ρ clamp is retained as a numerical backstop on u.
    """
    r = max(float(rho), 0.0)
    u = r / (1.0 + r)
    return min(max(u, rho_min), rho_max)


def multiplier(
    state: BrowneState,
    *,
    rho_min: float,
    rho_max: float,
    tau_floor: float,
    M_0: float,
    M_cap: float,
) -> float:
    """The normalised, clamped, capped tilt. M ≈ 1 at the (ρ=1, τ=0.5) reference point.

    ρ is mapped into the Φ⁻¹-safe open interval (see `standing_to_cdf_argument`) and τ is floored,
    both per §2.2/§5.
    """
    tau = max(float(state.tau), tau_floor)
    nu = _N.inv_cdf(standing_to_cdf_argument(state.rho, rho_min=rho_min, rho_max=rho_max))
    m_raw = (1.0 / (tau ** 0.5)) * _phi_over_Phi(nu)
    m = m_raw / M_0 if M_0 else m_raw
    return min(max(m, 0.0), M_cap)


def is_endgame_behind(state: BrowneState, m: float, *, endgame_tau: float) -> bool:
    """§2.3's precondition, all three parts: M > 1 AND behind AND late.

    All three are required. M > 1 alone can happen early while merely slightly behind, and
    concentrating the book then would throw away the grind's compounding for no reason.
    """
    return m > 1.0 and state.rho < 1.0 and state.tau <= endgame_tau


def concentration_fraction(
    state: BrowneState, m: float, *, M_full: float, all_in_cap_frac: float
) -> float:
    """How much of the bankroll the endgame override may put into the single best cluster.

    Ramps linearly from 0 at M = 1 up to `all_in_cap_frac` at M >= M_full. NEVER reaches 1.0 — §5:
    "never literally 100% on a single position, even at the all-in limit", so one estimation error
    cannot be fatal before the freeze.
    """
    if m <= 1.0:
        return 0.0
    span = max(M_full - 1.0, 1e-9)
    ramp = min((m - 1.0) / span, 1.0)
    return ramp * all_in_cap_frac
