"""Resolution-mechanics strategy (CORE tier, spec §1.3) — the factual repricing edge.

Knowing each market's resolution source/timing/criterion better than a careless student field, and
exploiting the gap between "effectively decided" and "SIG repriced." Recon §7.5: all 237 markets
settle at ONE wall (2026-11-04T17:00Z), so the value is concentrated in election-night repositioning
held into that single settlement.

Modules:
  rules_table  — per-market resolution table (settlement_time known from the API; source/criterion
                 need manual population at open — not API-exposed)
  decided_gap  — decided-vs-priced edge: known outcome + lagging SIG price → buy toward settlement,
                 in the sizing-consumed shape, confidence-gated
  endgame      — single-wall HOLD-vs-EXIT: hold high-confidence decided positions into the freeze,
                 exit uncertain ones before the cutoff to lock mark-to-market

Pure logic on state/rules dicts — no network, non-blocking. The executor places orders, not this.
"""
from .rules_table import (
    NEEDS_POPULATION,
    SETTLEMENT_WALL,
    STRATEGY_TAG,
    TOURNAMENT_CUTOFF,
    ResolutionRule,
    ResolutionRulesTable,
    seed_from_markets,
    seed_from_recon,
    settlement_time_from_iso,
)
from .decided_gap import (
    CONFIDENCE_BY_SOURCE_KIND,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_EDGE,
    SIDE_BUY,
    SIDE_SELL,
    ConfidenceTier,
    DecidedGapSignal,
    EffectiveResolution,
    ResolutionOutcome,
    classify_confidence,
    compute_decided_gap,
    confidence_from_source,
    on_resolution_update,
)
from .endgame import (
    EndgameAction,
    EndgameDecision,
    HeldPosition,
    decide_endgame,
    on_endgame_tick,
    plan_endgame,
)

__all__ = [
    # rules_table
    "NEEDS_POPULATION", "SETTLEMENT_WALL", "TOURNAMENT_CUTOFF", "STRATEGY_TAG",
    "ResolutionRule", "ResolutionRulesTable",
    "seed_from_recon", "seed_from_markets", "settlement_time_from_iso",
    # decided_gap
    "ResolutionOutcome", "ConfidenceTier", "EffectiveResolution", "DecidedGapSignal",
    "compute_decided_gap", "on_resolution_update", "classify_confidence", "confidence_from_source",
    "CONFIDENCE_BY_SOURCE_KIND", "DEFAULT_MIN_EDGE", "DEFAULT_MIN_CONFIDENCE", "SIDE_BUY", "SIDE_SELL",
    # endgame
    "EndgameAction", "EndgameDecision", "HeldPosition",
    "decide_endgame", "plan_endgame", "on_endgame_tick",
]
