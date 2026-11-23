"""Order-flow fade signal + the market-making informed-flow hook — pure logic, no I/O.

The edge (spec §2.3): fade one-sided crowd extremes — BUT only when the flow DISAGREES
with the informed view (fair-value / real-money cross-market). Flow that AGREES with the
real-money markets is INFORMED (right before a real move) — do NOT fade it; follow it or
stand aside. This "don't fade informed flow" rule is the safety core of the module.

Classification of a current extreme, given the informed reference:
  QUIET        — not a one-sided extreme → no signal.
  DUMB         — extreme AND opposes the informed direction → FADE (trade against the flow).
  INFORMED     — extreme AND agrees with the informed direction → do NOT fade (follow/stand aside).
  UNCONFIRMED  — extreme but no informed reference → no fade (needs confirmation — §2.3 honest limit).

Output conforms to the edge-signal handoff ({market_id, exchange_id, …, updated_at} + flags,
research-signal spec §5): a SIGNAL-tier directional TILT (not a fair-value p). The sizing
module sizes it; this module only detects/classifies.

Thresholds/windows live in flow.py and are PLACEHOLDERS to tune at open.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .flow import FlowBook, FlowConfig, FlowState, Trade

QUIET = "QUIET"
DUMB = "DUMB"
INFORMED = "INFORMED"
UNCONFIRMED = "UNCONFIRMED"

EDGE_TAG = "order_flow"


@dataclass(frozen=True)
class ReferenceView:
    """The informed view this module checks the flow against. Supply whichever is known;
    `informed_direction` resolves the net informed price direction (+1 up / −1 down / 0 none).

    * fair_divergence  = p − p_m (fair value − SIG price): >0 → price should rise (+1).
    * realmoney_direction = direction the real-money venues (Polymarket/Kalshi) imply (+1/−1/0).
    * realmoney_agree  = do the real-money venues agree with each other (→ higher confidence it's informed).
    """
    fair_divergence: float | None = None
    realmoney_direction: int | None = None
    realmoney_agree: bool = False

    def informed_direction(self) -> int:
        # Real-money direction leads (sharper, spec §4); fall back to fair-value divergence.
        if self.realmoney_direction:
            return 1 if self.realmoney_direction > 0 else -1
        if self.fair_divergence is not None and self.fair_divergence != 0:
            return 1 if self.fair_divergence > 0 else -1
        return 0

    @property
    def has_view(self) -> bool:
        return self.informed_direction() != 0


@dataclass(frozen=True)
class SignalConfig:
    # If True, an extreme with no informed reference counts as (possibly) informed for the
    # MM hook — MM pulls to be safe. It never produces a FADE (fades need confirmation).
    pull_on_unconfirmed: bool = True
    emit_follow: bool = False          # also emit a (non-fade) follow tilt on INFORMED flow. Off by default.


@dataclass
class OrderFlowSignal:
    exchange_id: str
    classification: str                # QUIET | DUMB | INFORMED | UNCONFIRMED
    fade: bool                         # True = trade AGAINST the flow (the edge)
    action: str | None                 # "buy" | "sell" — the fade (or follow) order side; None if no trade
    direction: int                     # price tilt this signal implies: +1 up / −1 down / 0 none
    strength: float                    # 0..1 — how one-sided the flow is (|imbalance| scaled)
    confidence: float                  # 0..1 — how sure it is dumb-not-informed (0 if no view)
    flow_imbalance: float
    informed: bool                     # is this (looks-)informed flow? (drives the MM hook)
    market_id: str | None = None
    flags: list[str] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        """Edge-signal handoff shape (sizing module / shared state, §5)."""
        return {"edge": EDGE_TAG, "market_id": self.market_id, "exchange_id": self.exchange_id,
                "classification": self.classification, "fade": self.fade, "action": self.action,
                "direction": self.direction, "strength": self.strength, "confidence": self.confidence,
                "flow_imbalance": self.flow_imbalance, "informed": self.informed,
                "flags": list(self.flags), "updated_at": self.updated_at}


def _fade_action(flow_direction: int) -> str:
    # Crowd pushing price up (+1) → fade = SELL YES; pushing down (−1) → fade = BUY YES.
    return "sell" if flow_direction > 0 else "buy"


class OrderFlowEngine:
    """Composes the tape (FlowBook) + the informed reference into a fade signal, and exposes
    the market-making informed-flow hook. Stateful only in tape + latest reference per market."""

    def __init__(self, flow_cfg: FlowConfig | None = None, cfg: SignalConfig | None = None) -> None:
        self.flow = FlowBook(flow_cfg)
        self._cfg = cfg or SignalConfig()
        self._ref: dict[str, ReferenceView] = {}

    # ── tape ingestion ──
    def ingest(self, trade: Trade, now: float | None = None) -> None:
        self.flow.ingest(trade, now)

    def ingest_many(self, trades, now: float | None = None) -> None:
        self.flow.ingest_many(trades, now)

    def set_reference(self, exchange_id: str, view: ReferenceView) -> None:
        """Store the latest informed view for a market (so the MM hook can be called without
        passing it each time)."""
        self._ref[exchange_id] = view

    # ── classification ──
    def classify(self, exchange_id: str, view: ReferenceView | None = None,
                 now: float | None = None) -> tuple[str, FlowState, ReferenceView]:
        st = self.flow.state(exchange_id, now)
        ref = view if view is not None else self._ref.get(exchange_id, ReferenceView())
        if not st.extreme:
            return QUIET, st, ref
        if not ref.has_view:
            return UNCONFIRMED, st, ref
        # extreme with a view: dumb if it OPPOSES the informed direction, informed if it AGREES.
        if st.direction == ref.informed_direction():
            return INFORMED, st, ref
        return DUMB, st, ref

    # ── the fade signal ──
    def signal(self, exchange_id: str, view: ReferenceView | None = None,
               market_id: str | None = None, now: float | None = None) -> OrderFlowSignal | None:
        now = time.monotonic() if now is None else now
        cls, st, ref = self.classify(exchange_id, view, now)
        if cls == QUIET:
            return None

        strength = min(1.0, abs(st.imbalance))
        flags: list[str] = []
        informed = cls == INFORMED or (cls == UNCONFIRMED and self._cfg.pull_on_unconfirmed)

        if cls == DUMB:
            # FADE: trade against the crowd. Confidence up when the real-money venues agree.
            conf = strength * (1.0 if ref.realmoney_agree else 0.6)
            if ref.realmoney_agree:
                flags.append("realmoney_agree")
            flags.append("fade_crowd")
            return OrderFlowSignal(
                exchange_id=exchange_id, classification=cls, fade=True,
                action=_fade_action(st.direction), direction=-st.direction,
                strength=strength, confidence=round(conf, 4), flow_imbalance=st.imbalance,
                informed=False, market_id=market_id, flags=flags, updated_at=now)

        if cls == INFORMED:
            # DO NOT FADE informed flow (the safety rule). Optionally emit a follow tilt.
            flags.append("informed_flow_no_fade")
            action = (_fade_action(-st.direction) if self._cfg.emit_follow else None)  # follow = WITH the flow
            return OrderFlowSignal(
                exchange_id=exchange_id, classification=cls, fade=False,
                action=action, direction=(st.direction if self._cfg.emit_follow else 0),
                strength=strength, confidence=0.0, flow_imbalance=st.imbalance,
                informed=True, market_id=market_id, flags=flags, updated_at=now)

        # UNCONFIRMED: extreme but no informed reference → no fade (needs confirmation, §2.3).
        flags.append("unconfirmed_no_reference")
        return OrderFlowSignal(
            exchange_id=exchange_id, classification=cls, fade=False, action=None, direction=0,
            strength=strength, confidence=0.0, flow_imbalance=st.imbalance,
            informed=informed, market_id=market_id, flags=flags, updated_at=now)

    # ── market-making hook (MM calls this; MM is NOT imported) ──
    def informed_flow_detected(self, exchange_id: str, now: float | None = None) -> bool:
        """True when the tape currently shows (looks-)informed one-sided flow — the cue for
        market-making to pull its quotes (don't provide liquidity into an informed move).
        Uses the stored reference (set_reference). An extreme that is confirmed DUMB is
        fadeable, not informed → False; an unconfirmed extreme is treated as informed when
        pull_on_unconfirmed (the safe default for MM)."""
        cls, _st, _ref = self.classify(exchange_id, None, now)
        if cls == INFORMED:
            return True
        if cls == UNCONFIRMED:
            return self._cfg.pull_on_unconfirmed
        return False

    # ── async entrypoint for the executor / signal loop (non-blocking) ──
    async def on_tape(self, trades, *, exchange_id: str, view: ReferenceView | None = None,
                      market_id: str | None = None, now: float | None = None) -> OrderFlowSignal | None:
        self.ingest_many(trades, now)
        return self.signal(exchange_id, view, market_id, now)
