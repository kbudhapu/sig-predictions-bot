"""Arb sizing — many smaller concurrent arbs instead of ~3 maximal ones.

The headline claim being pinned: with the DIRECTIONAL 15% per-market cap, a 2-leg basket is ~30% of
the bankroll and the deploy cap admits about THREE concurrent arbs. With the arb cap it admits about
TEN, each still cost-efficient and exitable. Both halves are asserted against the real RiskGate and
the real config, not against restated numbers.

Also pinned, because it is the thing most likely to be broken by accident: directional and
market-making orders are completely unaffected.
"""
from __future__ import annotations

import pytest
import yaml

from strategies.arb_sizing import (
    B_BUDGET,
    B_ENTRY,
    B_EXIT,
    B_PER_ARB,
    R_BELOW_MIN_NOTIONAL,
    R_BUDGET_EXHAUSTED,
    R_EDGE_TO_COST_TOO_LOW,
    R_NO_EXIT_DEPTH,
    R_SIZED,
    ArbSizingConfig,
    basket_unit_cost,
    load_arb_sizing_config,
    size_arb_basket,
)
from strategies.risk_gate.gate import RiskGate

BANKROLL = 100_000.0
LEG_A, LEG_B = "1077", "1076"
PX = 0.46                      # 2 legs at 0.46 -> 0.92 per basket unit, 0.08 edge
EDGE = 0.08


def _raw_cfg() -> dict:
    with open("config/sig.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


CFG = load_arb_sizing_config()


def book(bid: float = 0.49, qty: int = 1_000_000, ask: float = PX, ask_qty: int = 1_000_000) -> dict:
    return {"bids": [{"price": bid, "quantity": qty}],
            "asks": [{"price": ask, "quantity": ask_qty}]}


def legs(qty: int = 10**6, price: float = PX) -> list[dict]:
    return [{"exchange_id": LEG_A, "side": "yes", "action": "buy", "price": price, "quantity": qty},
            {"exchange_id": LEG_B, "side": "yes", "action": "buy", "price": price, "quantity": qty}]


def deep_books() -> dict:
    return {LEG_A: book(), LEG_B: book()}


def _size(**kw):
    params = dict(edge_per_contract=EDGE, bankroll=BANKROLL, arb_deployed=0.0,
                  exit_books=deep_books(), cfg=CFG)
    params.update(kw)
    return size_arb_basket(params.pop("legs", legs()), **params)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_config_loads_every_param_from_yaml():
    raw = _raw_cfg()["arb_sizing"]
    assert CFG.arb_max_per_arb_frac == raw["arb_max_per_arb_frac"]
    assert CFG.arb_total_max_frac == raw["arb_total_max_frac"]
    assert CFG.arb_min_notional == raw["arb_min_notional"]
    assert CFG.arb_min_edge_to_cost_ratio == raw["arb_min_edge_to_cost_ratio"]
    assert CFG.arb_max_frac_of_exit_depth == raw["arb_max_frac_of_exit_depth"]
    assert CFG.arb_fees == raw["arb_fees"]


def test_arb_cap_is_smaller_than_the_directional_cap():
    """The entire premise: arb's cap must be TIGHTER per position so more of them fit."""
    directional = float(_raw_cfg()["capital"]["max_per_market_frac"])
    assert CFG.arb_max_per_arb_frac < directional, \
        "an arb cap at or above the directional cap cannot increase concurrency"


def test_config_rejects_a_per_arb_cap_above_the_total_budget():
    """Otherwise one basket may exceed the budget for all baskets, and 'many smaller' silently
    becomes one big one again."""
    with pytest.raises(ValueError, match="arb_max_per_arb_frac"):
        ArbSizingConfig(arb_max_per_arb_frac=0.9, arb_total_max_frac=0.5, arb_min_notional=0.0,
                        arb_min_edge_to_cost_ratio=1.0, arb_fees=0.0,
                        arb_max_frac_of_exit_depth=0.5).validate()


def test_config_rejects_an_edge_to_cost_ratio_below_one():
    """A ratio under 1 authorises trades whose costs exceed their edge."""
    with pytest.raises(ValueError, match="arb_min_edge_to_cost_ratio"):
        ArbSizingConfig(arb_max_per_arb_frac=0.08, arb_total_max_frac=0.85, arb_min_notional=0.0,
                        arb_min_edge_to_cost_ratio=0.5, arb_fees=0.0,
                        arb_max_frac_of_exit_depth=0.5).validate()


# --------------------------------------------------------------------------- #
# THE HEADLINE: 10 at ~$8k, not 3 at $30k
# --------------------------------------------------------------------------- #
def test_ten_smaller_arbs_fit_where_three_maximal_ones_did():
    """Fill the budget one basket at a time and count how many fit.

    Each basket is sized by the module, its notional is added to `arb_deployed`, and the next is
    sized against the reduced budget — exactly what the strategy does within a scan. The old
    directional regime is computed alongside from the real config so the comparison is not a
    restated number.
    """
    deployed = 0.0
    sizes: list[float] = []
    for _ in range(40):                       # generous bound; the budget stops us
        s = _size(arb_deployed=deployed)
        if not s.sized:
            assert s.reason == R_BUDGET_EXHAUSTED, f"stopped for the wrong reason: {s.reason}"
            break
        sizes.append(s.basket_notional)
        deployed += s.basket_notional

    assert len(sizes) >= 10, f"expected >=10 concurrent arbs, got {len(sizes)}"
    # each one is ~8% of bankroll, i.e. ~$8k — not ~$30k
    assert sizes[0] == pytest.approx(CFG.arb_max_per_arb_frac * BANKROLL, rel=0.01)
    assert 7_000 <= sizes[0] <= 9_000, f"per-arb notional {sizes[0]} is not the ~$8k target"
    # ...and the total never breaches the arb budget
    assert deployed <= CFG.arb_total_max_frac * BANKROLL + 1e-6

    # The regime it replaces: 2 legs x 15% of bankroll per basket, inside max_deploy_frac.
    # 0.85 / 0.30 = 2.83 -> TWO full-size baskets and a partial third. "~3" is the capacity, not a
    # whole count, and the honest comparison is capacity-to-capacity.
    raw = _raw_cfg()
    directional_notional = 2 * float(raw["capital"]["max_per_market_frac"]) * BANKROLL
    old_capacity = float(raw["capital"]["max_deploy_frac"]) * BANKROLL / directional_notional
    assert 2.5 <= old_capacity <= 3.0, f"premise check: expected ~3 capacity, got {old_capacity:.2f}"
    assert len(sizes) >= 3 * old_capacity, "the point is a multiple, not a marginal improvement"


def test_total_budget_is_a_real_constraint_not_a_per_order_one():
    """Already-deployed arb must reduce the next basket, and exhaust to a refusal."""
    nearly_full = CFG.arb_total_max_frac * BANKROLL - 1_000.0
    s = _size(arb_deployed=nearly_full)
    assert s.sized and s.binding == B_BUDGET
    assert s.basket_notional <= 1_000.0 + 1e-6

    full = _size(arb_deployed=CFG.arb_total_max_frac * BANKROLL)
    assert not full.sized and full.reason == R_BUDGET_EXHAUSTED


def test_per_arb_cap_binds_when_everything_else_is_abundant():
    s = _size()
    assert s.sized and s.binding == B_PER_ARB
    expected_q = int(CFG.arb_max_per_arb_frac * BANKROLL / basket_unit_cost(legs()))
    assert s.quantity == expected_q
    assert s.per_unit_cost == pytest.approx(0.92)


# --------------------------------------------------------------------------- #
# Ceiling: exit liquidity
# --------------------------------------------------------------------------- #
def test_exit_depth_ceiling_binds_on_a_thin_book():
    """Measured SIG bid depth ranges ~134 to ~64k contracts. A thin leg must shrink the basket."""
    thin = {LEG_A: book(), LEG_B: book(qty=4_000)}
    s = _size(exit_books=thin)
    assert s.sized and s.binding == B_EXIT
    assert s.quantity == int(4_000 * CFG.arb_max_frac_of_exit_depth) == 1_000


def test_a_leg_is_never_more_than_the_configured_fraction_of_its_exit_depth():
    depth = 20_000
    s = _size(exit_books={LEG_A: book(), LEG_B: book(qty=depth)})
    assert s.quantity <= depth * CFG.arb_max_frac_of_exit_depth


def test_unknown_exit_depth_fails_closed():
    """An un-exitable arb is a hold-to-settlement position wearing a velocity trade's clothes."""
    assert _size(exit_books=None).reason == R_NO_EXIT_DEPTH
    assert _size(exit_books={LEG_A: book()}).reason == R_NO_EXIT_DEPTH   # leg B missing


def test_entry_depth_still_binds():
    s = _size(legs=legs(qty=500))
    assert s.sized and s.binding == B_ENTRY and s.quantity == 500


# --------------------------------------------------------------------------- #
# Floor: cost efficiency
# --------------------------------------------------------------------------- #
def test_min_notional_floor_rejects_a_pointlessly_small_arb():
    tiny = {LEG_A: book(qty=40), LEG_B: book(qty=40)}       # 25% of 40 = 10 contracts ~= $9.20
    s = _size(exit_books=tiny)
    assert not s.sized and s.reason == R_BELOW_MIN_NOTIONAL
    assert s.basket_notional < CFG.arb_min_notional


def test_edge_to_cost_floor_rejects_when_costs_eat_the_edge():
    """A high fee makes a thin edge not worth capturing, at any size."""
    fee_cfg = ArbSizingConfig(
        arb_max_per_arb_frac=CFG.arb_max_per_arb_frac, arb_total_max_frac=CFG.arb_total_max_frac,
        arb_min_notional=0.0, arb_min_edge_to_cost_ratio=3.0,
        arb_fees=0.02, arb_max_frac_of_exit_depth=CFG.arb_max_frac_of_exit_depth)
    s = _size(cfg=fee_cfg, edge_per_contract=0.005)
    assert not s.sized and s.reason == R_EDGE_TO_COST_TOO_LOW
    assert s.captured_edge < 3.0 * s.est_round_trip_cost


def test_the_floor_is_checked_at_the_size_actually_traded():
    """Checking it against the REQUESTED size would approve a basket on the economics of a size the
    book cannot support. Entry asks for a huge basket; exit depth allows 10 contracts; the floor
    must then judge those 10."""
    s = _size(legs=legs(qty=10**6), exit_books={LEG_A: book(qty=40), LEG_B: book(qty=40)})
    assert not s.sized and s.reason == R_BELOW_MIN_NOTIONAL
    assert s.meta["candidates"][B_EXIT] == 10


def test_fees_are_counted_on_both_sides():
    fee_cfg = ArbSizingConfig(
        arb_max_per_arb_frac=CFG.arb_max_per_arb_frac, arb_total_max_frac=CFG.arb_total_max_frac,
        arb_min_notional=0.0, arb_min_edge_to_cost_ratio=1.0,
        arb_fees=0.01, arb_max_frac_of_exit_depth=CFG.arb_max_frac_of_exit_depth)
    s = _size(cfg=fee_cfg)
    assert s.meta["fees"] == pytest.approx(0.01 * s.basket_notional * 2.0, rel=1e-9)


def test_no_edge_is_never_sized():
    assert not _size(edge_per_contract=0.0).sized
    assert not _size(edge_per_contract=-0.01).sized


# --------------------------------------------------------------------------- #
# The gate applies the ARB cap to arb and the DIRECTIONAL cap to everything else
# --------------------------------------------------------------------------- #
def _gate() -> RiskGate:
    return RiskGate(config=_raw_cfg())


def _ps() -> dict:
    return {"balance": BANKROLL, "positions": {}, "deployed": 0.0}


def _order(**kw) -> dict:
    o = {"exchange_id": LEG_A, "action": "buy", "side": "yes", "quantity": 100_000, "price": PX}
    o.update(kw)
    return o


def test_gate_reads_the_arb_cap_from_config():
    g = _gate()
    assert g.arb_max_per_arb_frac == CFG.arb_max_per_arb_frac
    assert g.arb_max_per_arb_frac < g.max_per_market_frac


@pytest.mark.parametrize("marker", [{"strategy_type": "arb"}, {"is_arb": True}])
def test_gate_clamps_an_arb_order_to_the_arb_cap(marker):
    """Both spellings work: the single-order path carries strategy_type, the multi-leg path stamps
    is_arb onto each gate leg."""
    res = _gate().check_order(_order(**marker), _ps(), None)
    assert res.approved
    notional = res.order["quantity"] * PX
    assert notional == pytest.approx(CFG.arb_max_per_arb_frac * BANKROLL, rel=0.01)
    assert any("arb_cap" in c for c in res.checks)


@pytest.mark.parametrize("marker", [{}, {"strategy_type": "directional"},
                                    {"strategy_type": "market_making"}])
def test_gate_leaves_directional_and_mm_on_the_directional_cap(marker):
    """REGRESSION GUARD: only arb gets the smaller cap. Everything else keeps 15%."""
    g = _gate()
    res = g.check_order(_order(**marker), _ps(), None)
    assert res.approved
    notional = res.order["quantity"] * PX
    assert notional == pytest.approx(g.max_per_market_frac * BANKROLL, rel=0.01)
    assert not any("arb_cap" in c for c in res.checks)


def test_an_arb_order_is_clamped_tighter_than_the_same_directional_order():
    g = _gate()
    arb = g.check_order(_order(strategy_type="arb"), _ps(), None)
    direc = g.check_order(_order(strategy_type="directional"), _ps(), None)
    assert arb.order["quantity"] < direc.order["quantity"]
    ratio = direc.order["quantity"] / arb.order["quantity"]
    assert ratio == pytest.approx(g.max_per_market_frac / g.arb_max_per_arb_frac, rel=0.02)


def test_arb_cap_is_not_inflated_by_the_endgame_override():
    """The override lets a LOSING bot concentrate a directional swing. Concentrating arb would mean
    FEWER locked spreads — the opposite of what the endgame needs."""
    g = _gate()
    behind = {"rho": 0.2, "tau": 0.05}            # far behind, nearly out of time
    assert g._is_override(behind["rho"], behind["tau"]), "fixture must actually trigger the override"
    arb = g.check_order(_order(strategy_type="arb"), _ps(), behind)
    direc = g.check_order(_order(strategy_type="directional"), _ps(), behind)
    assert arb.order["quantity"] * PX == pytest.approx(CFG.arb_max_per_arb_frac * BANKROLL, rel=0.01)
    assert direc.order["quantity"] > arb.order["quantity"], \
        "the directional book should be the one allowed to concentrate"


def test_multi_leg_arb_legs_are_gated_on_the_arb_cap():
    g = _gate()
    gate_legs = [{"exchange_id": LEG_A, "action": "buy", "side": "yes", "quantity": 100_000,
                  "price": PX, "is_arb": True},
                 {"exchange_id": LEG_B, "action": "buy", "side": "yes", "quantity": 100_000,
                  "price": PX, "is_arb": True}]
    res = g.check_multi_leg(gate_legs, _ps(), None)
    assert res.approved
    for leg in res.order["legs"]:
        assert leg["quantity"] * PX <= CFG.arb_max_per_arb_frac * BANKROLL * 1.01


def test_adapter_stamps_the_arb_marker_onto_multi_leg_gate_legs():
    """The gate decides from the ORDER, so the adapter must stamp it — otherwise arb baskets are
    silently gated on the directional cap and nothing changes."""
    from sig.engine.composition import RiskGateAdapter

    adapter = RiskGateAdapter(_gate(), balance=BANKROLL)
    verdict = adapter.validate_multi_leg(
        [{"exchange_id": LEG_A, "side": "buy", "action": "buy", "quantity": 100_000, "price": PX},
         {"exchange_id": LEG_B, "side": "buy", "action": "buy", "quantity": 100_000, "price": PX}],
        strategy_type="arb",
    )
    assert verdict.approved
    for leg in verdict.legs:
        assert leg["quantity"] * PX <= CFG.arb_max_per_arb_frac * BANKROLL * 1.01


def test_gate_deployed_and_notional_are_derived_so_the_total_cap_binds():
    """The adapter used to pass notional 0 / deployed 0, which made every cap compute FULL room no
    matter how much was already open."""
    from sig.engine.composition import RiskGateAdapter

    adapter = RiskGateAdapter(_gate(), balance=BANKROLL)
    adapter.set_positions({LEG_B: {"quantity": 50_000, "avg_price": 0.50}})   # $25k open
    ps = adapter._portfolio_state()
    assert ps["deployed"] == pytest.approx(25_000.0)
    assert ps["positions"][LEG_B]["notional"] == pytest.approx(25_000.0)
