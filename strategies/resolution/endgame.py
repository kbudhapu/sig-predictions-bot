"""Endgame / single-wall positioning (CORE tier, spec §1.3; recon §7.5).

Recon's decisive finding: all 237 markets settle at ONE wall (2026-11-04T17:00Z), none earlier —
there are NO staggered pre-cutoff resolutions. So the endgame is not "capture early settlements";
it is a pure HOLD-vs-EXIT decision taken into that single wall:

  • HOLD into the freeze — a position whose outcome is HIGH-CONFIDENCE decided and that is ALIGNED
    with the called outcome. It settles to $1 in our favour; holding captures the full remaining
    gap to settlement that decided_gap flagged.
  • EXIT before the cutoff — everything else:
      - UNCERTAIN (no call, or confidence below the hold bar): lock the current mark-to-market
        rather than gamble the position through a coin-flip settlement.
      - DECIDED AGAINST us (high-confidence call for the OTHER side): it settles to $0 — cut it and
        salvage whatever MTM remains instead of holding a known loser to zero.

Pure decision logic on position + resolution-state dicts and a clock. No network, non-blocking; the
executor acts on the decisions. Confidence/outcome come from decided_gap.EffectiveResolution.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .decided_gap import (
    DEFAULT_MIN_CONFIDENCE,
    EffectiveResolution,
    ResolutionOutcome,
)
from .rules_table import STRATEGY_TAG, TOURNAMENT_CUTOFF


class EndgameAction(str, Enum):
    HOLD = "hold"
    EXIT = "exit"


def _held_outcome(side: str) -> ResolutionOutcome:
    """Map a held contract side ('yes'/'no') to the outcome it pays on."""
    return ResolutionOutcome.YES if side.strip().lower() == "yes" else ResolutionOutcome.NO


@dataclass(frozen=True)
class HeldPosition:
    """A position carried toward the wall. ``side`` is the contract held ('yes' | 'no');
    ``sig_price`` is the current YES mark (for MTM context / logging)."""

    market_id: str
    side: str            # "yes" | "no"
    qty: float
    sig_price: float = 0.0


@dataclass(frozen=True)
class EndgameDecision:
    market_id: str
    action: EndgameAction
    reason: str
    settles_in_favor: bool | None   # True hold-to-$1, False cut-the-loser, None unknown/uncertain
    confidence: float
    actionable: bool                # False once the cutoff has passed (exit window closed)
    strategy: str = STRATEGY_TAG
    notes: list[str] = field(default_factory=list)


def decide_endgame(
    position: HeldPosition,
    resolution: EffectiveResolution | None,
    *,
    now: datetime,
    cutoff: datetime = TOURNAMENT_CUTOFF,
    min_hold_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> EndgameDecision:
    """HOLD-into-freeze vs EXIT-before-cutoff for one position.

    HOLD iff the position's outcome is decided at >= ``min_hold_confidence`` AND the held side is
    aligned with the called outcome. Everything else EXITs (uncertain → lock MTM; decided-against →
    cut). If ``now >= cutoff`` the exit window is closed and the decision is marked not actionable.
    """
    before_cutoff = now < cutoff
    held = _held_outcome(position.side)
    notes: list[str] = []
    if not before_cutoff:
        notes.append(
            f"cutoff {cutoff.isoformat()} passed — exit window closed; position settles as-is."
        )

    confidence = resolution.resolved_confidence() if resolution is not None else 0.0

    # Uncertain: no call, or the call is too soft to bet the settlement on → lock MTM.
    if resolution is None or confidence < min_hold_confidence:
        reason = (
            "no effective resolution — exit to lock mark-to-market before the wall"
            if resolution is None
            else f"confidence {confidence:.3f} < hold bar {min_hold_confidence:.3f} "
            f"— uncertain, exit to lock MTM"
        )
        return EndgameDecision(
            market_id=position.market_id,
            action=EndgameAction.EXIT,
            reason=reason,
            settles_in_favor=None,
            confidence=confidence,
            actionable=before_cutoff,
            notes=notes,
        )

    # High-confidence decided.
    aligned = held is resolution.outcome
    if aligned:
        return EndgameDecision(
            market_id=position.market_id,
            action=EndgameAction.HOLD,
            reason=(
                f"decided {resolution.outcome.value.upper()} at conf {confidence:.3f}; held side "
                f"'{position.side}' is aligned — hold into the freeze, settles to $1."
            ),
            settles_in_favor=True,
            confidence=confidence,
            actionable=before_cutoff,
            notes=notes,
        )
    return EndgameDecision(
        market_id=position.market_id,
        action=EndgameAction.EXIT,
        reason=(
            f"decided {resolution.outcome.value.upper()} at conf {confidence:.3f}; held side "
            f"'{position.side}' is AGAINST the call — settles to $0, exit to salvage MTM."
        ),
        settles_in_favor=False,
        confidence=confidence,
        actionable=before_cutoff,
        notes=notes,
    )


def plan_endgame(
    positions: list[HeldPosition],
    resolutions: Mapping[str, EffectiveResolution],
    *,
    now: datetime,
    cutoff: datetime = TOURNAMENT_CUTOFF,
    min_hold_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, EndgameDecision]:
    """Batch the per-position decision across the book. ``resolutions`` maps market_id → the known
    effective resolution (absent key ⇒ uncertain ⇒ exit)."""
    return {
        pos.market_id: decide_endgame(
            pos,
            resolutions.get(pos.market_id),
            now=now,
            cutoff=cutoff,
            min_hold_confidence=min_hold_confidence,
        )
        for pos in positions
    }


async def on_endgame_tick(
    positions: list[HeldPosition],
    resolutions: Mapping[str, EffectiveResolution],
    *,
    now: datetime,
    cutoff: datetime = TOURNAMENT_CUTOFF,
    min_hold_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, EndgameDecision]:
    """Async entrypoint for the executor's event loop (non-blocking — pure CPU, no I/O)."""
    return plan_endgame(
        positions, resolutions, now=now, cutoff=cutoff, min_hold_confidence=min_hold_confidence
    )
