"""Tests for the resolution-mechanics strategy (strategies/resolution/, spec §1.3).

Covers the four required behaviours:
  1. the rules table stores/retrieves correctly (and flags source/criterion as needing population);
  2. decided-gap computes the right edge AND direction
       (decided-YES + underpriced → buy; decided-NO + overpriced → sell);
  3. confidence flagging (AP call → high / actionable; a "leaning" → low / not actionable);
  4. endgame hold-vs-exit (high-confidence-decided-aligned → hold into freeze;
       uncertain → exit before cutoff; decided-against → exit).
Plus the single-wall seeding, the Kelly/σ_p sizing-shape fields, and the async entrypoints.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from strategies.resolution import (
    SETTLEMENT_WALL,
    TOURNAMENT_CUTOFF,
    ConfidenceTier,
    DecidedGapSignal,
    EffectiveResolution,
    EndgameAction,
    HeldPosition,
    ResolutionOutcome,
    ResolutionRule,
    ResolutionRulesTable,
    classify_confidence,
    compute_decided_gap,
    confidence_from_source,
    decide_endgame,
    on_endgame_tick,
    on_resolution_update,
    plan_endgame,
    seed_from_markets,
    seed_from_recon,
    settlement_time_from_iso,
)

WALL = SETTLEMENT_WALL
BEFORE = WALL - timedelta(hours=2)   # election night, exit window open
AFTER = WALL + timedelta(minutes=1)  # cutoff passed


# ── 1. rules table: store / retrieve + population flagging ────────────────────
def test_rules_table_stores_and_retrieves():
    table = seed_from_recon(["388", "387", "386"])
    assert len(table) == 3
    assert "388" in table
    rule = table.get("388")
    assert rule is not None
    assert rule.market_id == "388"
    # recon: all settle at the one wall, known from the API
    assert rule.settlement_time == WALL
    # source/criterion are NOT API-exposed → unpopulated, flagged for manual entry
    assert rule.resolution_source is None and rule.resolution_criterion is None
    assert not rule.fully_populated
    assert rule.missing_fields() == ("resolution_source", "resolution_criterion")


def test_rules_table_needs_population_worklist_and_fill():
    table = seed_from_recon(["388", "387"])
    assert {r.market_id for r in table.needs_population()} == {"388", "387"}
    assert not table.is_fully_populated()

    table.populate(
        "388",
        resolution_source="AP race call",
        resolution_criterion="party with the AP-called plurality in the state race",
    )
    r = table.get("388")
    assert r.resolution_source == "AP race call"
    assert r.criterion_populated and r.fully_populated
    # retrieval is stable and the other row still needs population
    assert {x.market_id for x in table.needs_population()} == {"387"}


def test_rules_table_upsert_overwrites_and_missing_returns_none():
    table = ResolutionRulesTable()
    assert table.get("zzz") is None
    table.upsert(ResolutionRule(market_id="1", settlement_time=WALL, resolution_source="AP race call"))
    table.upsert(ResolutionRule(market_id="1", settlement_time=WALL, resolution_source="State SoS"))
    assert len(table) == 1 and table.get("1").resolution_source == "State SoS"


def test_seed_from_markets_reads_settlement_date():
    markets = [
        {"id": "388", "settlementDate": "2026-11-04"},
        {"id": "387", "settlementDate": "2026-11-04T17:00:00Z"},
        {"id": "386"},  # no date → falls back to the recon wall
    ]
    table = seed_from_markets(markets)
    assert table.get("388").settlement_time == WALL
    assert table.get("387").settlement_time == WALL
    assert table.get("386").settlement_time == WALL


def test_settlement_time_parsing():
    assert settlement_time_from_iso("2026-11-04") == WALL
    assert settlement_time_from_iso("2026-11-04T17:00:00Z") == WALL
    assert settlement_time_from_iso("2026-11-04T17:00:00+00:00").tzinfo is not None


# ── 2. decided-gap: edge + DIRECTION ─────────────────────────────────────────
def test_decided_yes_underpriced_buys_with_right_edge():
    # "effectively called for YES at 95%" and SIG at 0.80 → 0.20 edge toward YES, BUY.
    res = EffectiveResolution("m1", ResolutionOutcome.YES, confidence=0.95, source="AP race call")
    sig = compute_decided_gap(res, 0.80)
    assert sig.side == "buy"
    assert sig.settlement_target == 1.0
    assert sig.edge == pytest.approx(0.20)
    assert sig.f_star > 0.0
    assert sig.actionable is True


def test_decided_no_overpriced_sells_with_right_edge():
    # decided NO at 95% but SIG still prices YES at 0.80 → overpriced → SELL, edge 0.80 toward $0.
    res = EffectiveResolution("m2", ResolutionOutcome.NO, confidence=0.95, source="AP race call")
    sig = compute_decided_gap(res, 0.80)
    assert sig.side == "sell"
    assert sig.settlement_target == 0.0
    assert sig.edge == pytest.approx(0.80)
    assert sig.f_star > 0.0
    assert sig.actionable is True


def test_decided_yes_already_repriced_has_no_actionable_edge():
    res = EffectiveResolution("m3", ResolutionOutcome.YES, confidence=0.99, source="AP race call")
    sig = compute_decided_gap(res, 0.99)          # SIG already at ~settlement
    assert sig.edge == pytest.approx(0.01)
    assert sig.actionable is False                # below DEFAULT_MIN_EDGE (0.02)


def test_sizing_input_shape_has_kelly_and_sigma():
    res = EffectiveResolution("m1", ResolutionOutcome.YES, confidence=0.95, source="AP race call")
    payload = compute_decided_gap(res, 0.80).to_sizing_input()
    # the sizing layer consumes p, p_m, f_star, edge, confidence, sigma_p, side, actionable
    for key in ("market_id", "side", "p", "p_m", "edge", "f_star", "confidence", "sigma_p",
                "actionable", "strategy"):
        assert key in payload
    assert payload["p_m"] == pytest.approx(0.80)
    assert payload["sigma_p"] == pytest.approx(0.05)    # 1 − confidence
    assert payload["strategy"] == "resolution"


# ── 3. confidence flagging (AP call high, leaning low) ───────────────────────
def test_confidence_tier_classification():
    assert classify_confidence(0.95) is ConfidenceTier.HIGH
    assert classify_confidence(0.75) is ConfidenceTier.MEDIUM
    assert classify_confidence(0.55) is ConfidenceTier.LOW
    assert confidence_from_source("ap_call") >= 0.90
    assert confidence_from_source("leaning") < 0.70


def test_ap_call_high_leaning_low_gating():
    # AP call → HIGH tier, clears the gate.
    ap = EffectiveResolution("m1", ResolutionOutcome.YES, source_kind="ap_call")
    ap_sig = compute_decided_gap(ap, 0.80)
    assert ap_sig.confidence_tier is ConfidenceTier.HIGH
    assert ap_sig.actionable is True

    # "leaning" → LOW tier, SAME price/edge but NOT actionable (confidence gate fails).
    lean = EffectiveResolution("m1", ResolutionOutcome.YES, source_kind="leaning")
    lean_sig = compute_decided_gap(lean, 0.80)
    assert lean_sig.confidence_tier is ConfidenceTier.LOW
    assert lean_sig.edge == pytest.approx(0.20)        # the gap is identical
    assert lean_sig.actionable is False                # but a lean is too soft to size


# ── 4. endgame hold-vs-exit ──────────────────────────────────────────────────
def test_high_confidence_decided_aligned_holds_into_freeze():
    pos = HeldPosition("m1", side="yes", qty=1000, sig_price=0.82)
    res = EffectiveResolution("m1", ResolutionOutcome.YES, confidence=0.97, source="AP race call")
    d = decide_endgame(pos, res, now=BEFORE)
    assert d.action is EndgameAction.HOLD
    assert d.settles_in_favor is True
    assert d.actionable is True


def test_uncertain_exits_before_cutoff_to_lock_mtm():
    pos = HeldPosition("m2", side="yes", qty=1000, sig_price=0.55)
    d = decide_endgame(pos, None, now=BEFORE)          # no effective resolution
    assert d.action is EndgameAction.EXIT
    assert d.settles_in_favor is None


def test_low_confidence_decided_exits_to_lock_mtm():
    pos = HeldPosition("m3", side="yes", qty=1000, sig_price=0.60)
    res = EffectiveResolution("m3", ResolutionOutcome.YES, confidence=0.60, source="leaning")
    d = decide_endgame(pos, res, now=BEFORE)
    assert d.action is EndgameAction.EXIT              # below the hold bar → lock MTM, don't gamble


def test_decided_against_held_side_exits_to_salvage():
    # we hold YES but the race is decided NO → settles to $0 → cut it.
    pos = HeldPosition("m4", side="yes", qty=1000, sig_price=0.40)
    res = EffectiveResolution("m4", ResolutionOutcome.NO, confidence=0.97, source="AP race call")
    d = decide_endgame(pos, res, now=BEFORE)
    assert d.action is EndgameAction.EXIT
    assert d.settles_in_favor is False


def test_endgame_decision_not_actionable_after_cutoff():
    pos = HeldPosition("m2", side="yes", qty=1000, sig_price=0.55)
    d = decide_endgame(pos, None, now=AFTER)           # exit window closed
    assert d.action is EndgameAction.EXIT
    assert d.actionable is False
    assert TOURNAMENT_CUTOFF == WALL


def test_plan_endgame_batches_the_book():
    positions = [
        HeldPosition("hold_me", side="yes", qty=1000),
        HeldPosition("exit_uncertain", side="no", qty=1000),
        HeldPosition("exit_wrong", side="yes", qty=1000),
    ]
    resolutions = {
        "hold_me": EffectiveResolution("hold_me", ResolutionOutcome.YES, confidence=0.98),
        "exit_wrong": EffectiveResolution("exit_wrong", ResolutionOutcome.NO, confidence=0.98),
        # "exit_uncertain" absent → uncertain
    }
    plan = plan_endgame(positions, resolutions, now=BEFORE)
    assert plan["hold_me"].action is EndgameAction.HOLD
    assert plan["exit_uncertain"].action is EndgameAction.EXIT
    assert plan["exit_wrong"].action is EndgameAction.EXIT


# ── async entrypoints (non-blocking, drop into the executor loop) ─────────────
def test_async_entrypoints_match_sync():
    res = EffectiveResolution("m1", ResolutionOutcome.YES, confidence=0.95, source="AP race call")
    sig = asyncio.run(on_resolution_update(res, 0.80))
    assert isinstance(sig, DecidedGapSignal) and sig.side == "buy"

    positions = [HeldPosition("m1", side="yes", qty=1000)]
    plan = asyncio.run(on_endgame_tick(positions, {"m1": res}, now=BEFORE))
    assert plan["m1"].action is EndgameAction.HOLD
