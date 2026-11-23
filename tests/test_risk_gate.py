"""Deterministic pre-trade risk-gate tests. No network, no LLM — pure validation on dicts.

Covers (per the task): each cap CLAMPS (not rejects) when breachable; erroneous orders are REJECTED
(off-tick, out-of-bounds, non-integer qty, qty≤0, absurd notional); authorized endgame concentration
PASSES while accidental over-concentration CLAMPS to the ceiling; a sizing bug over its own authorization
is still clamped; the gate NEVER increases a size; "fail small, not freeze"; sells are exempt; multi-leg
clamps proportionally (hedge ratio preserved).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.risk_gate.gate import RiskGate, GateResult

# Self-contained config mirroring config/sig.yaml (so tests don't depend on disk). Balance uses 1000 for
# round numbers: deploy≤850, per-market≤150, cluster≤250.
CFG = {
    "capital": {"max_deploy_frac": 0.85, "max_per_market_frac": 0.15, "max_cluster_frac": 0.25},
    "risk": {
        "tick": 0.005, "price_min": 0.005, "price_max": 0.995, "tick_tol": 1e-9,
        "hard_notional_frac": 1.0,
        "all_in_cap_frac": 0.95, "rho_min": 0.01, "rho_max": 0.99, "tau_floor": 0.01,
        "M_0": 1.1284, "M_full": 3.0,
    },
}
GRIND = {"rho": 1.0, "tau": 1.0}       # mid/early → M≤1 → no override
AHEAD = {"rho": 1.5, "tau": 0.1}       # ahead → no override
ENDGAME = {"rho": 0.3, "tau": 0.05}    # behind + late → M≫1 → override authorized


def gate():
    return RiskGate(config=CFG)


def order(price=0.50, qty=100, action="buy", exch="MKT_A", **kw):
    o = {"exchange_id": exch, "side": "yes", "action": action, "price": price, "quantity": qty}
    o.update(kw)
    return o


def empty_state(balance=1000.0):
    return {"balance": balance, "positions": {}}


# ─────────────────────────── erroneous orders → REJECT ───────────────────────────
def test_reject_off_tick_price():
    r = gate().check_order(order(price=0.513), empty_state(), GRIND)  # 0.513 not a 0.005 multiple
    assert not r.approved and r.reason == "off_tick_price"


def test_accept_on_tick_price():
    r = gate().check_order(order(price=0.515), empty_state(), GRIND)  # 0.515 = 103×0.005
    assert r.approved


def test_reject_price_out_of_bounds():
    assert gate().check_order(order(price=0.0), empty_state(), GRIND).reason == "price_out_of_bounds"
    assert gate().check_order(order(price=1.0), empty_state(), GRIND).reason == "price_out_of_bounds"


def test_reject_non_integer_quantity():
    r = gate().check_order(order(qty=10.5), empty_state(), GRIND)
    assert not r.approved and r.reason == "non_integer_quantity"


def test_integer_valued_float_quantity_ok():
    r = gate().check_order(order(qty=10.0), empty_state(), GRIND)
    assert r.approved and r.order["quantity"] == 10


def test_min_order_qty_skips_tiny_add():
    cfg = {**CFG, "capital": {**CFG["capital"], "min_order_qty": 10}}
    g = RiskGate(config=cfg)
    # a 1-share BUY (exposure-adding) is below the min → skipped, not placed
    r = g.check_order(order(qty=1, action="buy"), empty_state(), GRIND)
    assert not r.approved and r.reason == "below_min_order_qty"
    # a healthy buy still passes
    assert g.check_order(order(qty=100, action="buy"), empty_state(), GRIND).approved


def test_min_order_qty_exempts_reduce_only_sell():
    cfg = {**CFG, "capital": {**CFG["capital"], "min_order_qty": 10}}
    g = RiskGate(config=cfg)
    # a 1-share reduce-only SELL (dust flattening) is EXEMPT — must still be allowed to close
    st = {"balance": 1000.0, "positions": {"MKT_A": {"quantity": 5, "notional": 2.5}}}
    r = g.check_order(order(qty=1, action="sell"), st, GRIND)
    assert r.approved and r.order["quantity"] == 1


def test_min_order_qty_default_is_noop():
    # default (no min_order_qty in config) → a 1-share buy is NOT skipped on this count
    r = gate().check_order(order(qty=1, action="buy"), empty_state(), GRIND)
    assert r.approved and r.order["quantity"] == 1


def test_reject_non_positive_quantity():
    assert gate().check_order(order(qty=0), empty_state(), GRIND).reason == "non_positive_quantity"
    assert gate().check_order(order(qty=-5), empty_state(), GRIND).reason == "non_positive_quantity"


def test_erroneous_notional_is_a_fraction_of_balance_not_absolute():
    """The ONLY erroneous-order ceiling is notional > hard_notional_frac × balance. An order whose
    notional is large in ABSOLUTE dollars but small vs balance is NOT a bug — it is a big bankroll."""
    # $250k notional on a $10M book is 2.5% of balance → fine (the old absolute $100k cap would've rejected).
    ok = gate().check_order(order(price=0.50, qty=500_000), empty_state(balance=10_000_000), GRIND)
    assert ok.approved, "an absolute-$ ceiling must not reject an order that is small vs a large balance"
    # notional > 1× balance IS absurd for one order → rejected (the fraction cap, which scales with capital).
    bad = gate().check_order(order(price=0.50, qty=5_000_000), empty_state(balance=1_000_000), GRIND)
    assert not bad.approved and bad.reason == "notional_exceeds_balance_fraction_ceiling"


def test_no_share_count_ceiling_cheap_price_millions_of_shares_ok():
    """Size off CAPITAL only: a capital-sized order at a cheap price is millions of shares and must NOT
    be rejected on quantity — its notional is what matters. 2M @ 0.005 = $10k on a $10M book → fine."""
    r = gate().check_order(order(price=0.005, qty=2_000_000), empty_state(balance=10_000_000), GRIND)
    assert r.approved, "no raw share-count ceiling — $10k notional is not a fat-finger"


# ─────────────────────────── exposure caps → CLAMP (not reject) ───────────────────────────
def test_aggregate_cap_clamps_not_rejects():
    st = {"balance": 1000.0, "deployed": 800.0, "positions": {}}  # agg room = 850−800 = 50 → 100 shares @0.5
    r = gate().check_order(order(price=0.50, qty=1000), st, GRIND)
    assert r.approved and r.clamped and r.order["quantity"] == 100
    assert any("aggregate" in c for c in r.checks)


def test_per_market_cap_clamps():
    st = {"balance": 1000.0, "positions": {"MKT_A": {"notional": 100.0, "quantity": 200}}}  # per-mkt room 150−100=50
    r = gate().check_order(order(price=0.50, qty=1000, exch="MKT_A"), st, GRIND)
    assert r.approved and r.clamped and r.order["quantity"] == 100  # 50/0.5
    assert any("per_market" in c for c in r.checks)


def test_cluster_cap_is_binding_for_correlated_book():
    # two markets on one cluster "natl"; cluster already 200 → cluster room 250−200=50 → 100 shares
    st = {
        "balance": 1000.0,
        "positions": {"MKT_X": {"notional": 200.0, "quantity": 400, "cluster": "natl"}},
        "market_cluster": {"MKT_A": "natl"},
    }
    r = gate().check_order(order(price=0.50, qty=1000, exch="MKT_A", cluster="natl"), st, GRIND)
    assert r.approved and r.clamped and r.order["quantity"] == 100
    assert any("cluster" in c for c in r.checks)


def test_reasonable_order_passes_unclamped_fail_small_not_freeze():
    # a small, within-every-cap order must NOT be rejected or clamped (the 13-layer zero-trade trap)
    r = gate().check_order(order(price=0.50, qty=100), empty_state(), GRIND)  # notional 50 ≪ all caps
    assert r.approved and not r.clamped and r.order["quantity"] == 100


def test_no_room_rejects_this_order_only():
    st = {"balance": 1000.0, "deployed": 850.0, "positions": {}}  # already at aggregate cap
    r = gate().check_order(order(price=0.50, qty=100), st, GRIND)
    assert not r.approved and r.reason == "no_room_to_add_exposure"


# ─────────────────────────── authorized vs accidental concentration (the hard part) ───────────────────────────
def test_endgame_override_is_active_only_when_behind_and_late():
    g = gate()
    assert g.tournament_multiplier(**GRIND) <= 1.0      # grind → no override
    assert not g._is_override(**GRIND)
    assert not g._is_override(**AHEAD)                  # ahead → no override even if late
    assert g.tournament_multiplier(**ENDGAME) > 1.0     # behind+late → override
    assert g._is_override(**ENDGAME)
    # the authorized ceiling rises above the normal cluster cap only under override
    assert g.authorized_concentration_ceiling(**GRIND) == CFG["capital"]["max_cluster_frac"]
    assert g.authorized_concentration_ceiling(**ENDGAME) > CFG["capital"]["max_cluster_frac"]


def test_grind_clamps_concentration_to_per_market_cap():
    # an order concentrating 50% of the book in ONE market, during the grind → clamped to 15%
    r = gate().check_order(order(price=0.50, qty=1000), empty_state(), GRIND)  # 1000·0.5 = 500 = 50%
    assert r.approved and r.clamped
    assert r.order["quantity"] == 300  # 0.15·1000 / 0.50 = 300 shares (the per-market cap)


def test_endgame_permits_authorized_concentration():
    g = gate()
    st = empty_state()
    ceiling = g.authorized_concentration_ceiling(**ENDGAME)
    cap_frac = min(ceiling, CFG["capital"]["max_deploy_frac"])  # aggregate reserve still applies
    r = g.check_order(order(price=0.50, qty=1000), st, ENDGAME)  # wants 50% in one market
    assert r.approved
    final_notional = r.order["quantity"] * 0.50
    # permitted well beyond the 15% grind cap (the authorized skyrocket)
    assert final_notional > 0.15 * 1000 + 1e-9
    # but never above the authorized ceiling (nor the aggregate reserve)
    assert final_notional <= cap_frac * 1000 + 0.5  # +0.5 = one half-dollar share rounding


def test_accidental_over_concentration_clamped_to_ceiling():
    g = gate()
    st = empty_state()
    ceiling = g.authorized_concentration_ceiling(**ENDGAME)
    cap_frac = min(ceiling, CFG["capital"]["max_deploy_frac"])
    # a RUNAWAY order trying to put 99% into one market, even in the endgame → clamped to the ceiling
    r = g.check_order(order(price=0.50, qty=1980), st, ENDGAME)  # 1980·0.5 = 990 = 99%
    assert r.approved and r.clamped
    final_notional = r.order["quantity"] * 0.50
    assert final_notional <= cap_frac * 1000 + 0.5
    # and it is clamped DOWN, not through
    assert r.order["quantity"] < 1980


def test_sizing_module_bug_over_its_own_authorization_is_clamped():
    # Simulate a BUG in the sizing module: it computed a size above the authorization it itself should honor.
    # The gate re-derives the authorization and clamps it anyway (catches a bug in the sizing code).
    g = gate()
    st = empty_state()
    ceiling = g.authorized_concentration_ceiling(**ENDGAME)
    buggy_qty = 1900  # 1900·0.5 = 950 notional: within per-order sanity (≤ balance) but 95% concentration
    r = g.check_order(order(price=0.50, qty=buggy_qty), st, ENDGAME)
    assert r.approved and r.clamped
    final_notional = r.order["quantity"] * 0.50
    assert final_notional <= min(ceiling, 0.85) * 1000 + 0.5


# ─────────────────────────── invariants ───────────────────────────
def test_gate_never_increases_size():
    g = gate()
    # tiny order, huge caps, endgame override — must still never return more than proposed
    for st, ts in [(empty_state(), GRIND), (empty_state(), ENDGAME), (empty_state(), AHEAD)]:
        r = g.check_order(order(price=0.50, qty=10), st, ts)
        assert r.approved and r.order["quantity"] <= 10


# ─────────────────────────── sells exempt from exposure caps ───────────────────────────
def test_sell_is_exempt_from_exposure_caps():
    st = {"balance": 1000.0, "deployed": 850.0,  # book full — a BUY would be rejected
          "positions": {"MKT_A": {"notional": 400.0, "quantity": 800}}}
    r = gate().check_order(order(action="sell", qty=500, exch="MKT_A"), st, GRIND)
    assert r.approved and r.order["quantity"] == 500  # sell passes despite the full book


def test_sell_clamped_to_held_quantity():
    st = {"balance": 1000.0, "positions": {"MKT_A": {"notional": 100.0, "quantity": 150}}}
    r = gate().check_order(order(action="sell", qty=1000, exch="MKT_A"), st, GRIND)
    assert r.approved and r.order["quantity"] == 150 and r.clamped


def test_sell_with_no_holding_rejected():
    r = gate().check_order(order(action="sell", qty=100, exch="MKT_NONE"), empty_state(), GRIND)
    assert not r.approved


# ─────────────────────────── multi-leg: proportional clamp preserves the hedge ratio ───────────────────────────
def test_multi_leg_proportional_clamp_preserves_ratio():
    # cluster forces a clamp; both legs (2:1 ratio) must scale by the SAME factor
    st = {"balance": 1000.0,
          "positions": {"MKT_P": {"notional": 200.0, "quantity": 400, "cluster": "natl"}},
          "market_cluster": {"L1": "natl", "L2": "natl"}}
    legs = [
        {"exchange_id": "L1", "side": "yes", "action": "buy", "price": 0.50, "quantity": 200, "cluster": "natl"},
        {"exchange_id": "L2", "side": "no", "action": "buy", "price": 0.50, "quantity": 100, "cluster": "natl"},
    ]
    r = gate().check_multi_leg(legs, st, GRIND)
    assert r.approved and r.clamped
    q1, q2 = r.order["legs"][0]["quantity"], r.order["legs"][1]["quantity"]
    assert q1 == 2 * q2                       # the 2:1 hedge ratio is preserved
    assert q1 <= 200 and q2 <= 100            # never increased


def test_multi_leg_rejects_whole_batch_on_erroneous_leg():
    legs = [
        {"exchange_id": "L1", "side": "yes", "action": "buy", "price": 0.50, "quantity": 100},
        {"exchange_id": "L2", "side": "no", "action": "buy", "price": 0.513, "quantity": 100},  # off-tick bug
    ]
    r = gate().check_multi_leg(legs, empty_state(), GRIND)
    assert not r.approved and "off_tick" in r.reason


# ─────────────────────────── config loads from disk ───────────────────────────
def test_loads_real_config_from_disk():
    g = RiskGate()  # default path: repo config/sig.yaml
    assert 0.0 < g.max_per_market_frac <= g.max_cluster_frac <= g.max_deploy_frac <= 1.0
    assert g.tick == 0.005 and g.price_min == 0.005 and g.price_max == 0.995
