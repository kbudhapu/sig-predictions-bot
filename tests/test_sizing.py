"""Sizing tests (SIG_TOURNAMENT_SIZING_SPEC).

Params come from config/sig.yaml via load_sizing_config() — nothing is hardcoded here either, except
the hand-computed expectations in the worked example, which exist precisely to pin the arithmetic.
"""

from __future__ import annotations

import asyncio
import math
from statistics import NormalDist

import pytest

from strategies.sizing import (
    BrowneState,
    Edge,
    Sizer,
    Standing,
    concentration_fraction,
    correlation_from_loadings,
    effective_n,
    kelly_fraction_star,
    load_sizing_config,
    max_fillable_with_edge,
    multiplier,
    standing_to_cdf_argument,
)

CFG = load_sizing_config()
N = NormalDist()


def book(levels, side="asks"):
    """Orderbook in the live shape: {asks:[{price,quantity}], bids:[...]}."""
    other = "bids" if side == "asks" else "asks"
    return {side: [{"price": p, "quantity": q} for p, q in levels], other: []}


def thin_book(price=0.50, qty=1000):
    """Recon §4: a single seeded level of ~1000 contracts is the entire book."""
    return book([(price, qty)])


def mid_standing(balance=100_000.0):
    """ρ = 1 (on the bubble), τ = 0.5 (mid-competition) — the M ≈ 1 reference point."""
    return Standing(balance=balance, third_place_threshold=balance, time_fraction_remaining=0.5)


# --------------------------------------------------------------------------- #
# f* — the edge formula
# --------------------------------------------------------------------------- #
def test_f_star_formula_yes_and_no():
    """REQUIRED: f* edge formula correct. f* = (p − p_m)/(1 − p_m) for YES; symmetric for NO."""
    assert kelly_fraction_star(0.60, 0.50, "yes") == pytest.approx((0.60 - 0.50) / (1 - 0.50))
    assert kelly_fraction_star(0.60, 0.50, "yes") == pytest.approx(0.20)
    # p=0.70, p_m=0.40 -> 0.30/0.60 = 0.50
    assert kelly_fraction_star(0.70, 0.40, "yes") == pytest.approx(0.50)

    # NO side: fair value of NO is 1−p, entry cost is 1−p_m  ->  f* = (p_m − p)/p_m
    assert kelly_fraction_star(0.40, 0.50, "no") == pytest.approx((0.50 - 0.40) / 0.50)
    assert kelly_fraction_star(0.40, 0.50, "no") == pytest.approx(0.20)

    # perfectly-priced market has exactly zero edge on both sides
    assert kelly_fraction_star(0.50, 0.50, "yes") == pytest.approx(0.0)
    assert kelly_fraction_star(0.50, 0.50, "no") == pytest.approx(0.0)

    # degenerate prices claim no edge rather than dividing by zero
    assert kelly_fraction_star(0.9, 1.0, "yes") == 0.0
    assert kelly_fraction_star(0.9, 0.0, "yes") == 0.0


def test_no_bet_when_f_star_non_positive(tmp_path):
    """REQUIRED: the edge gate. f* <= 0 -> NO order, hard."""

    async def main():
        sizer = Sizer(CFG)
        # market priced ABOVE fair value -> buying YES is negative edge
        d = await sizer.size_edge(
            Edge("m", "1001", p=0.40, p_m=0.60, sigma_p=0.02), mid_standing(),
            budget=50_000.0, book=thin_book(0.60),
        )
        assert d.sized is False and d.order is None
        assert d.f_star < 0 and "no_edge" in d.reason

        # exactly zero edge is also no bet
        d0 = await sizer.size_edge(
            Edge("m", "1001", p=0.50, p_m=0.50, sigma_p=0.02), mid_standing(),
            budget=50_000.0, book=thin_book(0.50),
        )
        assert d0.sized is False

        # positive but inside the noise floor -> still no bet
        tiny = Edge("m", "1001", p=0.5004, p_m=0.50, sigma_p=0.02)
        dt = await sizer.size_edge(tiny, mid_standing(), budget=50_000.0, book=thin_book(0.50))
        assert dt.sized is False and "noise_floor" in dt.reason

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# quarter-Kelly base, not half
# --------------------------------------------------------------------------- #
def test_quarter_kelly_is_the_base_not_half():
    """REQUIRED: quarter-Kelly base (not half).

    §2.1a makes k(σ_p) the per-position fraction, with quarter-Kelly the AVERAGE a mixed-confidence
    book produces. The grid is calibrated so a reference-confidence estimate gets exactly 0.25 — and
    the test pins that it is 0.25, NOT 0.50.
    """
    sizer = Sizer(CFG)
    k_ref = sizer.k_for(CFG.k_reference_sigma)
    assert k_ref == pytest.approx(CFG.kelly_base_fraction)
    assert k_ref == pytest.approx(0.25)
    assert k_ref != pytest.approx(0.50), "the base must be QUARTER-Kelly, not half"

    # only a very confident estimate earns half-Kelly, and nothing earns more
    assert sizer.k_for(CFG.sigma_grid[0]) == pytest.approx(0.50)
    assert max(CFG.k_grid) <= 0.5, "no position may ever exceed half-Kelly"


# --------------------------------------------------------------------------- #
# uncertainty shrinkage
# --------------------------------------------------------------------------- #
def test_uncertainty_shrinkage_is_monotone_and_zero_without_sigma():
    """REQUIRED: higher σ_p -> smaller size; no σ_p -> ~zero."""
    sizer = Sizer(CFG)
    ks = [sizer.k_for(s) for s in (0.01, 0.02, 0.04, 0.06, 0.09, 0.12, 0.20, 0.40)]
    assert all(a >= b for a, b in zip(ks, ks[1:])), f"k must be non-increasing in sigma: {ks}"
    assert ks[0] > ks[-1]
    assert sizer.k_for(None) == pytest.approx(CFG.k_missing_sigma) == 0.0
    assert sizer.k_for(float("nan")) == 0.0, "an unusable sigma is maximally uncertain, not confident"


def test_higher_sigma_gives_strictly_smaller_order(tmp_path):
    async def main():
        sizer = Sizer(CFG)
        st = mid_standing()
        sizes = []
        for sigma in (0.02, 0.06, 0.12):
            d = await sizer.size_edge(
                Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=sigma), st,
                budget=50_000.0, book=book([(0.50, 100_000)]),
            )
            assert d.sized, d.reason
            sizes.append(d.order.quantity)
        assert sizes[0] > sizes[1] > sizes[2], f"size must shrink as sigma rises: {sizes}"

        # no sigma at all -> no order
        d_none = await sizer.size_edge(
            Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=None), st,
            budget=50_000.0, book=book([(0.50, 100_000)]),
        )
        assert d_none.sized is False and "no_sigma_p" in d_none.reason

    asyncio.run(main())


def test_low_confidence_positive_edge_is_treated_as_noise(tmp_path):
    """§5: the reality gate is absolute — positive is not enough, it must be TRUSTWORTHY."""

    async def main():
        sizer = Sizer(CFG)
        d = await sizer.size_edge(
            Edge("m", "1001", p=0.95, p_m=0.50, sigma_p=CFG.max_sigma_to_act + 0.01), mid_standing(),
            budget=50_000.0, book=book([(0.50, 100_000)]),
        )
        assert d.sized is False
        assert "low_confidence" in d.reason
        assert d.f_star > 0.5, "the edge was large — it is the CONFIDENCE that rejected it"

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# the Browne SHAPE
# --------------------------------------------------------------------------- #
def _M(rho, tau):
    return multiplier(
        BrowneState(rho, tau), rho_min=CFG.rho_min, rho_max=CFG.rho_max,
        tau_floor=CFG.tau_floor, M_0=CFG.M_0, M_cap=CFG.M_cap,
    )


def test_browne_normalised_to_one_at_the_reference_point():
    """M ≈ 1 at (ρ=1, τ=0.5) — the M_0 calibration in config."""
    assert _M(1.0, 0.5) == pytest.approx(1.0, abs=1e-3)


def test_browne_shape_behind_and_late_is_bigger():
    """REQUIRED: behind+late -> bigger; ahead -> smaller."""
    # later is more aggressive, at fixed standing
    assert _M(1.0, 0.5) < _M(1.0, 0.25) < _M(1.0, 0.05)
    # further behind is more aggressive, at fixed time
    assert _M(1.0, 0.5) < _M(0.8, 0.5) < _M(0.5, 0.5) < _M(0.2, 0.5)
    # ahead de-risks toward zero
    assert _M(1.2, 0.5) < _M(1.0, 0.5)
    assert _M(2.0, 0.5) < _M(1.2, 0.5)
    assert _M(5.0, 0.5) < 0.45
    assert _M(10.0, 0.5) < 0.30, "far ahead must approach cash"
    # and -- the literal formula cannot do this -- further ahead keeps de-risking
    assert _M(10.0, 0.5) < _M(5.0, 0.5) < _M(2.0, 0.5)
    # behind AND late compounds both effects
    assert _M(0.5, 0.05) > _M(0.5, 0.5)
    assert _M(0.5, 0.05) > _M(1.0, 0.05)


def test_browne_is_memoryless_and_depends_only_on_rho_and_tau():
    """Same (ρ, τ) from different balances/thresholds must give the same M — no path state."""
    a = BrowneState.from_standing(50_000.0, 100_000.0, 0.3)
    b = BrowneState.from_standing(5_000.0, 10_000.0, 0.3)
    assert a.rho == pytest.approx(b.rho) == pytest.approx(0.5)
    assert _M(a.rho, a.tau) == pytest.approx(_M(b.rho, b.tau))


def test_browne_is_a_shape_not_the_literal_continuous_formula():
    """The distinction the spec insists on (§1, 'Honest scope of the theory').

    Browne's control is α = 1/(σ√(T−t)) · φ(ν)/Φ(ν) — a FRACTION-AT-RISK for a continuously
    rebalanced Black–Scholes asset. What is implemented is the dimensionless SHAPE:
      * no σ (asset-volatility) term exists in the signature at all;
      * the output is normalised by M_0 and used as a MULTIPLIER on an independently-computed Kelly
        base, never as a fraction of wealth;
      * the τ -> 0 divergence is truncated (tau_floor, M_cap) rather than allowed to blow up.
    """
    import inspect

    sig = inspect.signature(multiplier)
    assert "sigma" not in sig.parameters, "a continuous-asset volatility must not appear"
    assert {"rho_min", "rho_max", "tau_floor", "M_0", "M_cap"} <= set(sig.parameters)

    # the divergence is capped: the literal control -> infinity as tau -> 0; this does not.
    assert _M(0.01, 1e-12) <= CFG.M_cap
    assert math.isfinite(_M(CFG.rho_min / 10, 0.0)), "deep tail must stay finite, never nan/inf"

    # but the SHAPE below the cap still tracks the reference form it reproduces
    for rho, tau in ((0.9, 0.4), (1.0, 0.5), (1.1, 0.6)):
        nu = N.inv_cdf(standing_to_cdf_argument(rho, rho_min=CFG.rho_min, rho_max=CFG.rho_max))
        expected = (1.0 / math.sqrt(max(tau, CFG.tau_floor))) * (N.pdf(nu) / N.cdf(nu)) / CFG.M_0
        assert _M(rho, tau) == pytest.approx(expected, rel=1e-9)


def test_rho_is_clamped_and_unknown_standing_blocks_sizing():
    # the clamp applies to u = rho/(1+rho); rho=0 and an absurd rho both pin at the bounds
    assert standing_to_cdf_argument(0.0, rho_min=CFG.rho_min, rho_max=CFG.rho_max) == CFG.rho_min
    assert standing_to_cdf_argument(1e9, rho_min=CFG.rho_min, rho_max=CFG.rho_max) == CFG.rho_max
    assert _M(0.0, 0.5) > _M(1.0, 0.5), 'broke -> maximally aggressive'
    assert _M(1e9, 0.5) < _M(1.0, 0.5), 'untouchable lead -> de-risk'
    # an unknown threshold must NOT silently become rho=1 ("mid-field")
    assert BrowneState.from_standing(100_000.0, None, 0.5) is None
    assert BrowneState.from_standing(100_000.0, 0.0, 0.5) is None


def test_browne_multiplier_moves_the_order_size(tmp_path):
    async def main():
        sizer = Sizer(CFG)
        e = Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=0.02)
        deep = book([(0.50, 1_000_000)])
        ahead = await sizer.size_edge(
            e, Standing(100_000.0, 50_000.0, 0.5), budget=1e9, book=deep)      # rho = 2.0
        mid = await sizer.size_edge(
            e, Standing(100_000.0, 100_000.0, 0.5), budget=1e9, book=deep)     # rho = 1.0
        behind_late = await sizer.size_edge(
            e, Standing(50_000.0, 100_000.0, 0.05), budget=1e9, book=deep)     # rho = 0.5, late
        assert ahead.M < mid.M < behind_late.M
        # size follows M (balances differ, so compare the fraction-of-bankroll)
        f_ahead = ahead.final_notional / 100_000.0
        f_mid = mid.final_notional / 100_000.0
        f_behind = behind_late.final_notional / 50_000.0
        assert f_ahead < f_mid < f_behind

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# market impact
# --------------------------------------------------------------------------- #
def test_market_impact_reduces_size_on_a_thin_book(tmp_path):
    """REQUIRED: market-impact reduces size on a thin book.

    Same edge, same standing — only the book differs. The thin book (one 1000-lot level, the recon
    shape) must produce a strictly smaller order than a deep one.
    """

    async def main():
        sizer = Sizer(CFG)
        e = Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=0.02)
        st = mid_standing()
        deep = await sizer.size_edge(e, st, budget=1e9, book=book([(0.50, 1_000_000)]))
        thin = await sizer.size_edge(e, st, budget=1e9, book=thin_book(0.50, 1000))
        assert deep.sized and thin.sized
        assert thin.order.quantity < deep.order.quantity
        assert thin.order.quantity <= 1000, "cannot fill beyond the book"

    asyncio.run(main())


def test_impact_adjusted_edge_bounds_the_quantity():
    """§2.1b: never size beyond the depth that keeps the impact-adjusted edge positive.

    A laddered book whose deeper levels price past fair value: the quantity must stop before those
    levels, even though more contracts are physically available.
    """
    p = 0.60
    laddered = book([(0.50, 100), (0.55, 100), (0.59, 100), (0.65, 100), (0.80, 500)])
    q = max_fillable_with_edge(laddered, p, "yes", min_f=CFG.min_impact_adjusted_f)
    assert 0 < q < 900, f"must stop before the edge-destroying levels, got {q}"

    from strategies.sizing import impact_adjusted_f

    f_at_q, fill = impact_adjusted_f(laddered, q, p, "yes")
    assert f_at_q > CFG.min_impact_adjusted_f
    assert fill.avg_price > 0.50, "average fill is worse than top-of-book — that is the impact"
    f_beyond, _ = impact_adjusted_f(laddered, q + 60, p, "yes")
    assert f_beyond < f_at_q, "deeper fills must only reduce the edge"


def test_no_order_when_top_of_book_has_no_edge():
    """If even one contract fails the impact test, there is nothing to size."""
    assert max_fillable_with_edge(book([(0.70, 1000)]), 0.60, "yes",
                                  min_f=CFG.min_impact_adjusted_f) == 0

    async def main():
        sizer = Sizer(CFG)
        # quote 0.50 gives edge, but the only liquidity sits at 0.69 (above fair 0.70 after min_f)
        d = await sizer.size_edge(
            Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=0.02), mid_standing(),
            budget=1e9, book=book([(0.699, 1000)]),
        )
        assert d.sized is False and "impact_kills_edge" in d.reason

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 1/sqrt(n_eff) correlated scaling
# --------------------------------------------------------------------------- #
def test_n_eff_collapses_under_correlation_and_equals_N_when_independent():
    """REQUIRED (part 1): n_eff from the participation ratio."""
    w = [1.0] * 10
    independent = [[1.0 if i == j else 0.0 for j in range(10)] for i in range(10)]
    assert effective_n(w, independent) == pytest.approx(10.0)

    # a book all loading fully on one swing factor is ONE bet
    assert effective_n(w, correlation_from_loadings([1.0] * 10)) == pytest.approx(1.0, abs=1e-9)

    # partial loading sits strictly between
    mid = effective_n(w, correlation_from_loadings([0.7] * 10))
    assert 1.0 < mid < 10.0


def test_one_over_sqrt_n_eff_scaling_shrinks_a_correlated_book(tmp_path):
    """REQUIRED (part 2): 1/√n_eff scaling for correlated positions.

    The same edge sized alone vs inside a 9-position correlated book: each position in the book must
    be smaller by ≈ 1/√n_eff, so total book variance stays near the single-bet level.
    """

    async def main():
        sizer = Sizer(CFG)
        st = mid_standing()
        deep = {f"10{i:02d}": book([(0.50, 1_000_000)]) for i in range(9)}
        # p=0.60 (not 0.70) deliberately: f*=0.20, k(0.02)=0.5 -> target 10,000 on a 100,000
        # bankroll, comfortably under the 15,000 per-market cap. With a bigger edge the SOLO leg
        # saturates that cap and the ratio below would measure the cap instead of the divisor.
        edges = [
            Edge("m", f"10{i:02d}", p=0.60, p_m=0.50, sigma_p=0.02,
                 cluster="national_swing", factor_loading=0.7)
            for i in range(9)
        ]
        solo = await sizer.size_edge(edges[0], st, budget=1e9, book=deep["1000"], n_eff=1.0)
        booked = await sizer.size_book(edges, st, budgets=1e9, books=deep)
        assert all(d.sized for d in booked)

        # the divisor is the ONLY thing allowed to differ between the two
        assert solo.meta["binding_cap"] is None, "solo leg must be cap-free for this comparison"
        assert booked[0].meta["binding_cap"] is None

        n_eff = booked[0].n_eff
        assert 1.0 < n_eff < 9.0, f"correlated book must have 1 < n_eff < N, got {n_eff}"
        assert booked[0].divisor == pytest.approx(math.sqrt(n_eff))
        assert booked[0].order.quantity < solo.order.quantity
        assert booked[0].order.quantity == pytest.approx(
            solo.order.quantity / math.sqrt(n_eff), rel=0.02
        )

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# endgame override
# --------------------------------------------------------------------------- #
def test_endgame_override_inverts_diversification_and_concentrates(tmp_path):
    """§2.3: behind + late + M>1 -> treat the best correlated cluster as ONE bet; do not size the rest."""

    async def main():
        sizer = Sizer(CFG)
        late_behind = Standing(40_000.0, 100_000.0, 0.03)       # rho=0.4, tau=0.03
        books = {f"20{i:02d}": book([(0.50, 1_000_000)]) for i in range(6)}
        edges = [
            # the strong, CONFIDENT cluster
            Edge("a", "2000", p=0.80, p_m=0.50, sigma_p=0.02, cluster="swing_A"),
            Edge("b", "2001", p=0.78, p_m=0.50, sigma_p=0.02, cluster="swing_A"),
            # a weaker cluster
            Edge("c", "2002", p=0.56, p_m=0.50, sigma_p=0.02, cluster="swing_B"),
            Edge("d", "2003", p=0.55, p_m=0.50, sigma_p=0.02, cluster="swing_B"),
        ]
        out = await sizer.size_book(edges, late_behind, budgets=1e9, books=books)
        sized = [d for d in out if d.sized]
        assert sized, "the override must still place the concentrated bet"
        assert all(d.endgame_override for d in sized)
        # only the strongest cluster is funded
        assert {d.order.exchange_id for d in sized} == {"2000", "2001"}
        # and diversification is INVERTED — divisor 1.0, not sqrt(n_eff)
        assert all(d.divisor == pytest.approx(1.0) for d in sized)
        skipped = [d for d in out if not d.sized]
        assert all("endgame_concentration" in d.reason for d in skipped)

    asyncio.run(main())


def test_endgame_never_concentrates_onto_a_shaky_estimate(tmp_path):
    """§2.3's sharpened condition — the lethal combination is size × bad estimate × no time."""

    async def main():
        sizer = Sizer(CFG)
        late_behind = Standing(40_000.0, 100_000.0, 0.03)
        shaky = Edge("a", "2000", p=0.90, p_m=0.50,
                     sigma_p=CFG.endgame_min_confidence_sigma + 0.02, cluster="swing_A")
        d = await sizer.size_edge(shaky, late_behind, budget=1e9,
                                  book=book([(0.50, 1_000_000)]))
        assert d.endgame_override is False, "a huge but shaky edge must not be concentrated"
        # it may still be sized normally (it passes the reality gate) but WITHOUT the override
        if d.sized:
            assert d.final_notional <= CFG.max_per_market_frac * 40_000.0 + 1e-6

    asyncio.run(main())


def test_concentration_never_reaches_all_in():
    """§5: never literally 100% into one scenario."""
    st = BrowneState(0.1, 0.01)
    big_m = 50.0
    frac = concentration_fraction(st, big_m, M_full=CFG.M_full, all_in_cap_frac=CFG.all_in_cap_frac)
    assert frac == pytest.approx(CFG.all_in_cap_frac)
    assert frac < 1.0
    assert concentration_fraction(st, 1.0, M_full=CFG.M_full,
                                  all_in_cap_frac=CFG.all_in_cap_frac) == 0.0


def test_ahead_never_invokes_the_override():
    """§2.4: ahead, variance is the enemy — the override is unreachable however late it gets."""
    from strategies.sizing import is_endgame_behind

    ahead_late = BrowneState(rho=1.5, tau=0.01)
    m = _M(1.5, 0.01)
    assert is_endgame_behind(ahead_late, m, endgame_tau=CFG.endgame_tau) is False


# --------------------------------------------------------------------------- #
# the "when uncertain, smaller" posture
# --------------------------------------------------------------------------- #
def test_when_uncertain_resolve_smaller(tmp_path):
    """REQUIRED: the §1b/Thorp posture — every ambiguous input resolves toward SMALLER or to no trade."""

    async def main():
        sizer = Sizer(CFG)
        st = mid_standing()
        deep = book([(0.50, 1_000_000)])
        base = Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=0.02)
        full = await sizer.size_edge(base, st, budget=1e9, book=deep)
        assert full.sized

        # 1. missing sigma -> no trade (not a default-confident trade)
        assert (await sizer.size_edge(
            Edge("m", "1001", p=0.70, p_m=0.50, sigma_p=None), st, budget=1e9, book=deep
        )).sized is False
        # 2. unknown standing -> no trade (not an assumed mid-field)
        assert (await sizer.size_edge(
            base, Standing(100_000.0, None, 0.5), budget=1e9, book=deep
        )).sized is False
        # 3. no book supplied -> sized, but impact is NOT claimed as checked
        nb = await sizer.size_edge(base, st, budget=1e9, book=None)
        assert nb.sized and nb.meta["impact_checked"] is False and nb.avg_fill_price is None
        # 4. a smaller budget binds downward, never upward
        tight = await sizer.size_edge(base, st, budget=100.0, book=deep)
        assert tight.sized and tight.order.quantity < full.order.quantity
        assert tight.meta["binding_cap"] == "edge_budget"
        # 5. the per-market cap binds when the budget is huge
        assert full.meta["binding_cap"] in ("per_market", "edge_budget")
        assert full.final_notional <= CFG.max_per_market_frac * 100_000.0 + 1.0

    asyncio.run(main())


def test_limit_price_rounds_against_us():
    from strategies.sizing import round_limit_price

    assert round_limit_price(0.5012, "buy") == pytest.approx(0.505)   # buy rounds UP
    assert round_limit_price(0.5012, "sell") == pytest.approx(0.50)   # sell rounds DOWN


def test_config_rejects_a_non_monotone_k_grid():
    """A grid where k rises with sigma would shrink CONFIDENT estimates hardest — the inverse of
    §2.1a — so it is rejected at load rather than silently sizing backwards."""
    from strategies.sizing import SizingConfig

    bad = {**CFG.raw}
    bad["sizing"] = {**CFG.raw["sizing"], "k_grid": [0.1, 0.2, 0.3, 0.4]}
    with pytest.raises(ValueError, match="non-increasing"):
        SizingConfig.from_cfg(bad)


# --------------------------------------------------------------------------- #
# WORKED NUMERICAL EXAMPLE
# --------------------------------------------------------------------------- #
def test_worked_example_end_to_end(tmp_path):
    """REQUIRED: a worked numerical example, every step pinned by hand.

    SETUP
        fair value      p      = 0.70
        market          p_m    = 0.50   (buy YES)
        uncertainty     sigma_p= 0.06   (the reference-confidence point)
        balance         B      = 100,000      third-place T3 = 125,000  -> rho = 0.8 (behind)
        time remaining  tau    = 0.25         (three-quarters gone)
        allocator edge budget  = 40,000
        book: 5,000 @ 0.50 then 5,000 @ 0.52   (enough depth to show impact without killing the edge)

    HAND ARITHMETIC
        f*      = (0.70 − 0.50)/(1 − 0.50)                     = 0.40
        k(0.06) = 0.25                                          (quarter-Kelly, the reference point)
        f_shrunk= 0.25 × 0.40                                   = 0.10
        nu      = Phi^-1(0.8)                                   = 0.841621
        M       = (1/sqrt(0.25)) × phi(nu)/Phi(nu) / 1.1284
                = 2 × (0.279962/0.788145) / 1.1284              = 0.629601
        fraction= 0.10 × 0.629601 / sqrt(1)                     = 0.0629601
        target  = 0.0629601 × 100,000                           = 6,296.01
        caps    : edge_budget 40,000 ; per_market 0.15×100,000 = 15,000  -> target is not capped
        quantity= floor(6,296.01 / 0.50)                        = 12,592  contracts
                  ...but the book holds only 10,000, and impact must keep the edge alive, so the
                  order is reduced to the fillable, edge-positive quantity (<= 10,000).
    """

    async def main():
        sizer = Sizer(CFG)
        edge = Edge("GA-SEN", "1001", p=0.70, p_m=0.50, sigma_p=0.06)
        standing = Standing(
            balance=100_000.0, third_place_threshold=125_000.0, time_fraction_remaining=0.25
        )
        depth = book([(0.50, 5_000), (0.52, 5_000)])

        d = await sizer.size_edge(edge, standing, budget=40_000.0, book=depth)

        # --- every intermediate, against the hand arithmetic ---
        assert d.f_star == pytest.approx(0.40)
        assert d.k == pytest.approx(0.25)
        assert d.f_shrunk == pytest.approx(0.10)

        u = standing_to_cdf_argument(0.8, rho_min=CFG.rho_min, rho_max=CFG.rho_max)
        assert u == pytest.approx(0.444444, abs=1e-6)
        nu = N.inv_cdf(u)
        assert nu == pytest.approx(-0.139710, abs=1e-5)
        expected_M = (1.0 / math.sqrt(0.25)) * (N.pdf(nu) / N.cdf(nu)) / CFG.M_0
        assert expected_M == pytest.approx(1.5755094, abs=1e-6)
        assert d.M == pytest.approx(expected_M, rel=1e-9)
        assert d.M > 1.0, "behind (rho=0.8) and three-quarters through -> over-bet tilt"

        assert d.n_eff == pytest.approx(1.0) and d.divisor == pytest.approx(1.0)
        assert d.target_notional == pytest.approx(0.10 * expected_M * 100_000.0)
        assert d.target_notional == pytest.approx(15_755.1, abs=1.0)
        # the per-market cap binds BEFORE the book does
        assert d.meta["binding_cap"] == "per_market"
        assert d.meta["rho"] == pytest.approx(0.8) and d.meta["tau"] == pytest.approx(0.25)

        # --- the order ---
        assert d.sized, d.reason
        o = d.order
        assert o.exchange_id == "1001" and o.side == "yes" and o.action == "buy"
        # capped by the book (10,000) rather than by the 12,592 the fraction wanted
        assert o.quantity <= 10_000
        assert o.quantity == pytest.approx(10_000, rel=0.01)
        assert d.avg_fill_price is not None and d.avg_fill_price > 0.50, "impact raised the fill"
        assert d.impact_f > CFG.min_impact_adjusted_f
        assert o.price >= 0.52, "limit must reach the deepest level consumed"
        assert round(o.price / 0.005) == pytest.approx(o.price / 0.005, abs=1e-6)

        # the executor contract
        assert set(o.to_dict()) == {"exchange_id", "side", "action", "quantity", "price"}

        print(
            f"\nWORKED EXAMPLE: f*={d.f_star:.3f} k={d.k:.2f} f_shrunk={d.f_shrunk:.3f} "
            f"M={d.M:.4f} target=${d.target_notional:,.0f} -> {o.quantity} contracts @ {o.price}"
        )

    asyncio.run(main())
