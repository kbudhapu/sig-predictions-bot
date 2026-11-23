"""Cross-market divergence strategy — the discipline + safety tests.

Proves: toward-external direction; inverted-polarity flips correctly; the skepticism curve
shrinks/refuses an implausible gap (+ logs a mapping-audit); stale external → no trade; venue
disagreement → shrunk size; election-night called race → max conviction; low-confidence/unverified
mappings ignored (dormant-safe); and the whole thing routes through the real sizer/gate/OMS.
"""
import os

import pytest
import yaml

from signals.mapping import RaceMapping, ExternalRef
from strategies.cross_market import CrossMarketDivergenceStrategy, DivergenceConfig, ExternalQuote

SIG_EX = "948"


def _mapping(*, confidence="high", resolution_match=True, polarity="direct", liquidity_ok=True,
             with_kalshi=False, kalshi_polarity="direct"):
    externals = {"polymarket": ExternalRef(venue="polymarket", slug="s", yes_outcome="R",
                                           polarity=polarity, liquidity_ok=liquidity_ok)}
    if with_kalshi:
        externals["kalshi"] = ExternalRef(venue="kalshi", ticker="T", yes_outcome="R",
                                          polarity=kalshi_polarity, liquidity_ok=True)
    return {"race_x": RaceMapping(key="race_x", description="", resolution_match=resolution_match,
                                  confidence=confidence, externals=externals,
                                  sig_market_id="1", sig_exchange_id=SIG_EX)}


def _sig(price, age=1.0):
    return lambda ex: (price, age) if ex == SIG_EX else None


def _ext(quotes):
    return lambda ex: quotes if ex == SIG_EX else None


def _only(intents):
    assert len(intents) == 1
    return intents[0]


# 1 — normal divergence → toward-external (SIG below sharp → BUY)
def test_normal_divergence_moves_toward_external():
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 1.0)}))
    it = _only(strat.evaluate(None))
    assert it.side == "buy" and it.strategy_type == "directional"
    assert it.metadata["external_fair"] == 0.70 and it.metadata["divergence"] == 0.20
    assert it.metadata["sigma_p"] < DivergenceConfig().max_sigma     # a real (sized) bet
    assert "forecast" not in it.metadata and "expected_move" not in it.metadata  # sized by GAP, not forecast


# 2 — INVERTED polarity → correctly flipped (not backwards)
def test_inverted_polarity_flips_signal():
    # venue raw YES = 0.30; inverted ⇒ SIG YES = 0.70. SIG at 0.50 ⇒ +0.20 ⇒ BUY (NOT sell).
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(polarity="inverted"), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.30, 1.0)}))
    it = _only(strat.evaluate(None))
    assert it.side == "buy"                              # flipped: 1−0.30=0.70 > 0.50
    assert it.metadata["external_fair"] == 0.70          # aligned to SIG polarity


# 3 — implausible gap → skepticism REFUSES + logs a mapping-audit (NOT a huge bet)
def test_implausible_gap_refused_and_audited():
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.95, 1.0)}))  # gap 0.45 > 0.40
    intents = strat.evaluate(None)
    assert intents == []                                 # refused — no bet
    assert len(strat.audit_flags) == 1                   # flagged for mapping audit
    assert strat.audit_flags[0]["race"] == "race_x"


# 4 — stale external price → no trade (illusory divergence)
def test_stale_external_no_trade():
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 999.0)}),  # age > 30s
        config=DivergenceConfig(external_stale_s=30.0))
    assert strat.evaluate(None) == []


# 5 — Polymarket/Kalshi disagreement → shrunk size (bigger sigma_p)
def test_venue_disagreement_shrinks_size():
    agree = CrossMarketDivergenceStrategy(
        mapping=_mapping(with_kalshi=True), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 1.0),
                                "kalshi": ExternalQuote("kalshi", 0.70, 1.0)}))
    disagree = CrossMarketDivergenceStrategy(
        mapping=_mapping(with_kalshi=True), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 1.0),
                                "kalshi": ExternalQuote("kalshi", 0.50, 1.0)}))   # spread 0.20 ≫ tol
    s_agree = _only(agree.evaluate(None)).metadata["sigma_p"]
    s_dis = _only(disagree.evaluate(None)).metadata["sigma_p"]
    assert s_dis > s_agree                               # disagreement → less confident → smaller bet


# 6 — election-night: a CALLED race (external ≥ 0.97) → max conviction toward ~1.0
def test_election_night_called_race_max_conviction():
    cfg = DivergenceConfig(election_night_mode=True)
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.52),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.98, 1.0)}), config=cfg)
    it = _only(strat.evaluate(None))
    assert it.metadata["called"] is True
    assert it.metadata["external_fair"] == 0.99          # snapped to ~decided
    assert it.side == "buy" and it.metadata["sigma_p"] == cfg.called_sigma   # max conviction
    # the called race bypasses the skepticism curve (its 0.47 gap would otherwise refuse)
    assert it.metadata["skepticism"] == 1.0


def test_election_night_mode_off_refuses_the_same_huge_gap():
    # Same inputs, flag OFF → the 0.46 gap is treated as implausible → refused (proves the overlay is the only path).
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.52),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.98, 1.0)}))
    assert strat.evaluate(None) == [] and len(strat.audit_flags) == 1


# 7 — dormant-safe: low-confidence / unverified / illiquid mappings are ignored
@pytest.mark.parametrize("kw", [
    {"confidence": "low"}, {"confidence": "skip"}, {"resolution_match": False}, {"liquidity_ok": False},
])
def test_ignores_unverified_or_illiquid_mappings(kw):
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(**kw), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 1.0)}))
    assert strat.evaluate(None) == []


def _real_mapping():
    from signals.mapping import load_race_mapping
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return load_race_mapping(os.path.join(root, "signals", "race_mapping.yaml"))


def test_real_race_mapping_activates_and_routes_a_direct_live_race():
    # LIVE Polymarket mapping: the control races + the 24 governor races are active (count grows as more are
    # added, so assert the core control races are active rather than an exact count). A DIRECT active race
    # routes toward the sharp price. us_senate_control_rep (SIG ex 843): Poly 'Republican controls' ≈0.365.
    mapping = _real_mapping()
    active_keys = {k for k, r in mapping.items() if r.is_active()}
    assert {"us_senate_control_rep", "us_senate_control_dem",
            "us_house_control_rep", "us_house_control_dem"} <= active_keys and len(active_keys) >= 5
    EX = "843"
    strat = CrossMarketDivergenceStrategy(
        mapping=mapping,
        sig_price_provider=lambda ex: (0.25, 1.0) if ex == EX else None,   # SIG below the sharp 0.365
        external_provider=lambda ex: ({"polymarket": ExternalQuote("polymarket", 0.365, 1.0)}
                                       if ex == EX else None))
    it = _only(strat.evaluate(None))
    assert it.exchange_id == EX and it.side == "buy"       # SIG 0.25 < sharp 0.365 → BUY toward sharp
    assert it.metadata["external_fair"] == 0.365           # DIRECT: Polymarket Yes used as-is (no flip)


def test_seam_live_inverted_race_routes_in_correct_direction():
    """THE POLARITY SEAM on a REAL inverted race from the new mapping: us_house_control_dem (SIG ex 840)
    ↔ the Republican-House Polymarket market, read INVERTED. The resolver flips ONCE: raw Poly Yes
    (R controls) 0.075 → aligned SIG-Democrat-controls 0.925. SIG at 0.80 is BELOW the aligned sharp
    0.925 → BUY (toward sharp). A missing/double flip would feed 0.075 → divergence −0.725 → SELL
    (backwards). This is the #1-trap guard, on real mapping data."""
    mapping = _real_mapping()
    assert mapping["us_house_control_dem"].polymarket.polarity == "inverted"
    EX = "840"
    strat = CrossMarketDivergenceStrategy(
        mapping=mapping,
        sig_price_provider=lambda ex: (0.80, 1.0) if ex == EX else None,
        external_provider=lambda ex: ({"polymarket": ExternalQuote("polymarket", 0.075, 1.0)}
                                       if ex == EX else None))
    it = _only(strat.evaluate(None))
    assert it.exchange_id == EX
    assert it.metadata["external_fair"] == 0.925           # ONE flip: 1 − 0.075 (not 0.075, not double-flipped)
    assert it.metadata["divergence"] == round(0.925 - 0.80, 4)
    assert it.side == "buy"                                # SIG 0.80 < aligned sharp 0.925 → BUY, not backwards


# end-to-end: routes detection → real sizer → real gate → OMS → broker
@pytest.mark.asyncio
async def test_routes_through_real_stack_and_places_order():
    from sig.engine.composition import build_sim_stack

    def _cfg():
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"cross_market_divergence": SIG_EX})
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 1.0)}))
    strat.cadence_s = 0.0
    orch.register_strategy(strat)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    await orch._run_cycle()
    assert len(broker.placed) == 1                       # the divergence intent → real sizer/gate/OMS → broker
    assert broker.placed[0]["exchange_id"] == SIG_EX and broker.placed[0]["side"] == "buy"
    assert broker.placed[0]["quantity"] > 0              # the REAL Browne sizer produced a size from sigma_p


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# SEAM: an INVERTED-polarity race through the FULL path (resolver → strategy → intent), flip EXACTLY ONCE.
#
# The double-flip bug this guards against lived ONLY at the seam: signals/cross_market.resolve_external_prices
# flips inverted legs, and the strategy USED to flip them again — each layer's own tests passed, but together
# they flipped twice (0.30 → 0.70 → 0.30) and inverted the signal. The contract now: the resolver is the ONE
# flip owner; the strategy consumes already-aligned prices and never flips. These tests pin that at the seam.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_seam_resolver_is_the_single_flip_owner():
    # The flip lives in exactly one place — the resolver: inverted flips once (0.30→0.70), direct is untouched.
    from signals.cross_market import resolve_external_prices
    rm_inv = _mapping(polarity="inverted")["race_x"]
    rm_dir = _mapping(polarity="direct")["race_x"]
    assert resolve_external_prices(rm_inv, {"polymarket": 0.30}) == {"polymarket": 0.70}   # ONE flip
    assert resolve_external_prices(rm_dir, {"polymarket": 0.30}) == {"polymarket": 0.30}   # ZERO flips


def test_seam_inverted_race_full_path_is_buy_not_double_flipped():
    # SIG "Republican wins X" ↔ external Democrat-leg (inverted), raw Dem price 0.30; SIG R-price 0.50.
    #   correct (ONE flip, resolver only): R-aligned 0.70 vs 0.50 ⇒ +0.20 ⇒ BUY (toward the sharp price)
    #   the bug (TWO flips):               0.70→0.30      vs 0.50 ⇒ −0.20 ⇒ SELL  (backwards)
    #   no flip (ZERO flips):              0.30           vs 0.50 ⇒ −0.20 ⇒ SELL  (backwards)
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(polarity="inverted"), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.30, 1.0)}))
    it = _only(strat.evaluate(None))
    assert it.metadata["external_fair"] == 0.70          # the value fed to the divergence math — ONE flip …
    assert it.metadata["external_fair"] != 0.30          # … NOT 0.30, which zero OR two flips would produce
    assert it.metadata["divergence"] == 0.20             # +0.20 toward the sharp price
    assert it.side == "buy"                              # BUY — not the backwards SELL a double-flip yields


def test_seam_direct_race_is_not_flipped():
    # "Exactly one layer flips" means ZERO flips on a DIRECT leg — raw 0.70 reaches divergence as 0.70.
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(polarity="direct"), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.70, 1.0)}))
    it = _only(strat.evaluate(None))
    assert it.metadata["external_fair"] == 0.70 and it.side == "buy"


@pytest.mark.asyncio
async def test_seam_inverted_race_routes_through_real_stack_as_buy():
    # The whole seam, end to end: resolver flip → strategy → REAL sizer/gate/OMS → broker, on an inverted race.
    # A double-flip would place a SELL here; the single flip places the correct BUY.
    from sig.engine.composition import build_sim_stack

    def _cfg():
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"cross_market_divergence": SIG_EX})
    strat = CrossMarketDivergenceStrategy(
        mapping=_mapping(polarity="inverted"), sig_price_provider=_sig(0.50),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.30, 1.0)}))  # raw Dem 0.30 ⇒ R 0.70
    strat.cadence_s = 0.0
    orch.register_strategy(strat)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    await orch._run_cycle()
    assert len(broker.placed) == 1
    assert broker.placed[0]["side"] == "buy"             # correct direction through the entire stack
    assert broker.placed[0]["quantity"] > 0


def test_conviction_weighting_gates_by_benchmark_confidence():
    # flag ON: the divergence threshold + size become benchmark-conviction-dependent.
    cfg = DivergenceConfig(conviction_weighting=True)
    # CONFIDENT benchmark 0.90, a small 4pt gap (SIG 0.86) → conviction high → threshold ~2.5pt → TRADES
    conf_trades = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.86),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.90, 1.0)}), config=cfg)
    it = _only(conf_trades.evaluate(None))
    assert it.side == "buy" and it.metadata["conviction"] > 0.5
    # MUSHY benchmark 0.55, the SAME 4pt gap (SIG 0.51) → conviction low → threshold ~20pt → NO trade
    mushy = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.51),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.55, 1.0)}), config=cfg)
    assert mushy.evaluate(None) == []
    # COST FLOOR: confident 0.95 but a sub-cost 1.5pt gap (SIG 0.935) → still refused (conviction can't override)
    subcost = CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(0.935),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", 0.95, 1.0)}), config=cfg)
    assert subcost.evaluate(None) == []
