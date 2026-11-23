"""Decided-vs-priced gap (CORE tier, spec §1.3) — the factual repricing edge.

When a race is *effectively decided* (e.g. AP calls it for YES) but SIG has not yet repriced to the
settlement value (~1.0 for YES / ~0.0 for NO), buy toward the known outcome. This is not a forecast
— the outcome is treated as known input; the edge is purely the lag between "decided" and "SIG
repriced." (Recon §7.5: all markets settle at one wall, so this is realised as election-night
repositioning held into settlement — see endgame.py.)

Direction (what the tests pin):
  • decided YES + SIG under 1.0  → BUY YES  (edge = 1.0 − p_m, toward settlement $1).
  • decided NO  + SIG above 0.0  → SELL YES (edge = p_m − 0.0, toward settlement $0).
A decided market can only have edge TOWARD its known settlement value — you never trade away from a
called outcome, so the decided outcome alone fixes the side.

Confidence (spec §1.3 "AP call high-confidence; a 'leaning' lower"):
  an AP/network call → HIGH; a projection/lean → LOW/MEDIUM. Confidence gates and shrinks the bet,
  it does NOT change the direction. It feeds the sizing layer as σ_p (Baker–McHale shrinkage,
  SIG_TOURNAMENT_SIZING_SPEC §2.1a): low confidence ⇒ high σ_p ⇒ shrink hard.

Output conforms to the sizing-consumed shape (SIG_TOURNAMENT_SIZING_SPEC §2.1): a per-position edge
with fair value ``p``, market price ``p_m``, Kelly ``f* = (p − p_m)/(1 − p_m)`` (buy-YES form;
symmetric for NO), a confidence/σ_p, and an absolute REALITY edge-gate (``f* > 0`` AND confidence
over threshold). Pure math; the executor/sizer places orders, not this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

STRATEGY_TAG = "resolution"

# Buy toward YES / sell toward NO — matches the "action" vocabulary used by arb_detector and the
# market-maker contract ("buy" | "sell").
SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Default gates (SIG_TOURNAMENT_SIZING_SPEC §2.1 edge gate). Tune at open.
DEFAULT_MIN_EDGE = 0.02        # below this, the gap is noise / not worth the fee+tick
DEFAULT_MIN_CONFIDENCE = 0.90  # an AP-call-grade bar; a "leaning" (lower) does not clear it


class ResolutionOutcome(str, Enum):
    YES = "yes"
    NO = "no"


class ConfidenceTier(str, Enum):
    HIGH = "high"      # AP / multiple-network call, or certified result
    MEDIUM = "medium"  # single projection / strong lean
    LOW = "low"        # "leaning" / uncalled — advisory only


# Source-kind → representative confidence. A caller may pass an explicit numeric confidence instead;
# this is the fallback when only a label is known. "AP call high; leaning low" (spec §1.3).
CONFIDENCE_BY_SOURCE_KIND: dict[str, float] = {
    "certified": 1.0,
    "multiple_network_calls": 0.98,
    "ap_call": 0.95,
    "network_call": 0.95,
    "projection": 0.80,
    "likely": 0.75,
    "lean": 0.60,
    "leaning": 0.60,
    "uncalled": 0.50,
}

_TIER_HIGH = 0.90
_TIER_MEDIUM = 0.70


def classify_confidence(confidence: float) -> ConfidenceTier:
    if confidence >= _TIER_HIGH:
        return ConfidenceTier.HIGH
    if confidence >= _TIER_MEDIUM:
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


def confidence_from_source(source_kind: str, default: float = 0.50) -> float:
    return CONFIDENCE_BY_SOURCE_KIND.get(source_kind.strip().lower(), default)


@dataclass(frozen=True)
class EffectiveResolution:
    """An externally-known effective resolution state fed in as INPUT (not forecast here).

    ``confidence`` is the probability the call is right; if omitted (``None``) it is derived from
    ``source_kind`` via CONFIDENCE_BY_SOURCE_KIND. ``source`` is free text for the audit trail
    (e.g. "AP race call")."""

    market_id: str
    outcome: ResolutionOutcome
    confidence: float | None = None
    source: str = ""
    source_kind: str | None = None

    def resolved_confidence(self) -> float:
        if self.confidence is not None:
            return float(self.confidence)
        if self.source_kind is not None:
            return confidence_from_source(self.source_kind)
        return 0.50  # unknown provenance → coin-flip floor (will fail the gate)


@dataclass(frozen=True)
class DecidedGapSignal:
    """The decided-gap edge, in the sizing-consumed shape (SIG_TOURNAMENT_SIZING_SPEC §2.1)."""

    market_id: str
    outcome: ResolutionOutcome
    side: str                      # "buy" (long YES) | "sell" (short YES / long NO)
    sig_price: float               # p_m — SIG's current YES price
    settlement_target: float       # the KNOWN settlement value of YES: 1.0 (YES) | 0.0 (NO)
    fair_value: float              # p — confidence-weighted YES fair value (feeds Kelly)
    edge: float                    # gap toward the known settlement value (the "0.20" in the spec)
    f_star: float                  # Kelly fraction on the traded side, in [0, 1]
    confidence: float              # P(call correct)
    confidence_tier: ConfidenceTier
    sigma_p: float                 # 1 − confidence — uncertainty for Baker–McHale shrinkage
    actionable: bool               # passed the edge gate (edge ≥ min AND confidence ≥ min AND f*>0)
    source: str = ""
    strategy: str = STRATEGY_TAG
    notes: list[str] = field(default_factory=list)

    def to_sizing_input(self) -> dict:
        """Canonical dict the sizing layer consumes (mirrors market_maker.to_contract's role)."""
        return {
            "market_id": self.market_id,
            "strategy": self.strategy,
            "side": self.side,
            "p": self.fair_value,           # sizing's fair-value estimate
            "p_m": self.sig_price,          # sizing's market price
            "edge": self.edge,
            "f_star": self.f_star,
            "confidence": self.confidence,
            "sigma_p": self.sigma_p,
            "actionable": self.actionable,
            "source": self.source,
        }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def compute_decided_gap(
    resolution: EffectiveResolution,
    sig_price: float,
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> DecidedGapSignal:
    """Compute the decided-gap edge signal for one market.

    ``sig_price`` is SIG's current YES price p_m in [0, 1]. The edge is the gap from p_m to the
    known settlement value; the side is fixed by the decided outcome; confidence gates/shrinks.
    """
    p_m = _clamp01(sig_price)
    confidence = _clamp01(resolution.resolved_confidence())
    tier = classify_confidence(confidence)
    notes: list[str] = []

    if resolution.outcome is ResolutionOutcome.YES:
        settlement_target = 1.0
        side = SIDE_BUY
        edge = settlement_target - p_m           # 1.0 − p_m (buy YES toward $1)
        fair_value = confidence                  # confidence-weighted YES fair value
        # Kelly buy-YES: f* = (p − p_m)/(1 − p_m)
        f_star = (fair_value - p_m) / (1.0 - p_m) if p_m < 1.0 else 0.0
    else:  # NO
        settlement_target = 0.0
        side = SIDE_SELL
        edge = p_m - settlement_target           # p_m − 0.0 (sell YES toward $0)
        fair_value = 1.0 - confidence            # YES fair value when NO is called with conf c
        # Kelly buy-NO (= sell YES): f* = (p_m − p)/p_m  (symmetric form)
        f_star = (p_m - fair_value) / p_m if p_m > 0.0 else 0.0

    f_star = _clamp01(f_star)
    sigma_p = 1.0 - confidence

    gate_edge = edge >= min_edge
    gate_conf = confidence >= min_confidence
    gate_f = f_star > 0.0
    actionable = gate_edge and gate_conf and gate_f

    if not gate_edge:
        notes.append(f"edge {edge:.3f} < min_edge {min_edge:.3f} — already repriced / negligible.")
    if not gate_conf:
        notes.append(
            f"confidence {confidence:.3f} < min_confidence {min_confidence:.3f} "
            f"({tier.value}) — not actionable (spec §1.3: a 'leaning' is too soft to size)."
        )
    if actionable:
        notes.append(
            f"decided {resolution.outcome.value.upper()} at conf {confidence:.3f} ({tier.value}); "
            f"SIG {p_m:.3f} vs settlement {settlement_target:.1f} → {side} edge {edge:.3f}."
        )

    return DecidedGapSignal(
        market_id=resolution.market_id,
        outcome=resolution.outcome,
        side=side,
        sig_price=p_m,
        settlement_target=settlement_target,
        fair_value=fair_value,
        edge=edge,
        f_star=f_star,
        confidence=confidence,
        confidence_tier=tier,
        sigma_p=sigma_p,
        actionable=actionable,
        source=resolution.source,
        notes=notes,
    )


async def on_resolution_update(
    resolution: EffectiveResolution,
    sig_price: float,
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> DecidedGapSignal:
    """Async entrypoint for the executor's event loop (non-blocking — pure CPU, no I/O)."""
    return compute_decided_gap(
        resolution, sig_price, min_edge=min_edge, min_confidence=min_confidence
    )
