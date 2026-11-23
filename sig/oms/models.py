"""
OMS data model — order lifecycle states, bundle tracking, and per-strategy policies.

Mechanics ported from mbappe:
  - ChildFillSlice / PendingFillBundle  (broker_fill_sieve.py)
  - TERMINAL_ORDER_STATUSES / OPEN_ORDER_STATUSES  (broker_fill_sieve.py)
  - needs_tracking logic  (PendingFillBundle.needs_tracking, broker_fill_sieve.py)

What is NOT copied from mbappe:
  - The "abandon and alert operator" terminal policy.  Replaced with per-strategy
    autonomous resolution (see PartialFillPolicy).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Status sets (ported from mbappe broker_fill_sieve.py)
# ---------------------------------------------------------------------------

TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    {"filled", "partially_filled", "canceled", "cancelled", "expired", "rejected"}
)
OPEN_ORDER_STATUSES: frozenset[str] = frozenset(
    {"new", "accepted", "pending_new", "submitted", "open"}
)
def cancel_confirmed(res) -> bool:
    """True only when a cancel_order result CONFIRMS the cancel. broker.cancel_order returns an OrderResult, which
    is always truthy — `if ok:` treated refused/failed cancels (429, breaker, ownership refusal) as success, so a
    still-resting order was marked cancelled (arb unwind: naked leg; chase: re-placed on top = two live orders).
    Test doubles that return a bare bool keep working."""
    if isinstance(res, bool):
        return res
    return str(getattr(res, "status", "")).lower() in ("canceled", "cancelled")


# Statuses after which an order can never trade again (partially_filled is NOT here: it may still be resting).
FINISHED_ORDER_STATUSES: frozenset[str] = frozenset({"filled", "canceled", "cancelled", "expired", "rejected"})


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderLifecycle(str, enum.Enum):
    PENDING = "pending"           # created locally, not yet sent to broker
    SUBMITTED = "submitted"       # sent to broker, awaiting ack
    WORKING = "working"           # broker confirmed open / resting
    FILLED = "filled"             # fully filled
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @classmethod
    def from_broker_status(cls, status: str) -> "OrderLifecycle":
        mapping = {
            "new": cls.WORKING,
            "accepted": cls.WORKING,
            "pending_new": cls.SUBMITTED,
            "submitted": cls.SUBMITTED,
            "open": cls.WORKING,
            "filled": cls.FILLED,
            "partially_filled": cls.PARTIALLY_FILLED,
            "canceled": cls.CANCELLED,
            "cancelled": cls.CANCELLED,
            "expired": cls.EXPIRED,
            "rejected": cls.REJECTED,
        }
        return mapping.get(status.lower(), cls.SUBMITTED)

    def is_terminal(self) -> bool:
        return self in (
            self.FILLED,
            self.PARTIALLY_FILLED,
            self.CANCELLED,
            self.REJECTED,
            self.EXPIRED,
        )


class StrategyClass(str, enum.Enum):
    ARB = "arb"                   # relationship_engine — atomic-or-cancel-all-legs
    MARKET_MAKING = "market_making"  # leave partial resting
    DIRECTIONAL = "directional"   # chase or accept per config


class PartialFillPolicy(str, enum.Enum):
    """Terminal policy when a fill does not complete within the cycle budget.

    IMPORTANT: "ABANDON_TO_HUMAN" is intentionally absent — this bot runs unattended.
    """
    UNWIND_ALL_LEGS = "unwind_all_legs"     # arb: cancel everything, net-flat
    LEAVE_RESTING = "leave_resting"          # market-making: resting quote is fine
    CHASE = "chase"                          # directional: re-quote residual qty
    ACCEPT_PARTIAL = "accept_partial"        # directional: take filled size, update state


class FillPolicy:
    """Per-strategy fill configuration loaded from config/sig.yaml."""

    def __init__(
        self,
        strategy_class: StrategyClass,
        partial_fill_policy: PartialFillPolicy,
        *,
        unfilled_timeout_seconds: float = 30.0,
        chase_max_attempts: int = 3,
        cycle_cancel_threshold: int = 3,
        maker_rest: bool = False,
    ) -> None:
        self.strategy_class = strategy_class
        self.partial_fill_policy = partial_fill_policy
        self.unfilled_timeout_seconds = unfilled_timeout_seconds
        self.chase_max_attempts = chase_max_attempts
        # Ported from mbappe INCOMPLETE_CYCLE_CANCEL_THRESHOLD = 3
        self.cycle_cancel_threshold = cycle_cancel_threshold
        # MAKER REST (cross maker entries, 2026-10-08): rest at the limit, never chase; the timeout sweep cancels
        # the order (open, partially filled or status-unknown) and the bundle resolves only once every slice is
        # DEFINITIVELY finished (filled / cancelled / rejected / expired). Each bundle gets its own idempotency
        # key (an identical re-entry must not replay a previous, cancelled order's stored response).
        self.maker_rest = bool(maker_rest)

    @classmethod
    def for_arb(cls, **kwargs: Any) -> "FillPolicy":
        return cls(
            StrategyClass.ARB,
            PartialFillPolicy.UNWIND_ALL_LEGS,
            **kwargs,
        )

    @classmethod
    def for_market_making(cls, **kwargs: Any) -> "FillPolicy":
        return cls(
            StrategyClass.MARKET_MAKING,
            PartialFillPolicy.LEAVE_RESTING,
            **kwargs,
        )

    @classmethod
    def for_directional_chase(cls, **kwargs: Any) -> "FillPolicy":
        return cls(
            StrategyClass.DIRECTIONAL,
            PartialFillPolicy.CHASE,
            **kwargs,
        )

    @classmethod
    def for_directional_accept(cls, **kwargs: Any) -> "FillPolicy":
        return cls(
            StrategyClass.DIRECTIONAL,
            PartialFillPolicy.ACCEPT_PARTIAL,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Slice and bundle (ported from mbappe broker_fill_sieve.py)
# ---------------------------------------------------------------------------

@dataclass
class OrderSlice:
    """Tracks one child order within a bundle.

    Ported from mbappe ChildFillSlice with side-agnostic contract/outcome fields.
    """
    order_id: str | None
    leg_id: str                    # e.g. "YES_ARIZONA_GOV" or "leg_0"
    contract_id: str               # the prediction-market contract
    side: str                      # "buy" | "sell"
    requested_qty: float
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    status: str = "submitted"
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    chase_attempts: int = 0        # directional only — how many re-quotes attempted
    cancel_confirmed: bool = False # the venue CONFIRMED our cancel: final, whatever a later read reports
    cancel_attempt_at: float = 0.0 # monotonic time of the last cancel attempt (backoff)
    # Placement inputs captured at submit so a CHASE/replace can re-quote faithfully via the broker's
    # real place_order(strategy_id, market_id, exchange_id, side, action, quantity, price, outcome).
    # Without these the chase cannot reconstruct the order (it has only contract_id/side/qty) — the gap
    # that made the old place_order(contract_id=…, qty=…) call crash the cycle. Default-empty so a
    # legacy slice falls back to contract_id for market_id/exchange_id.
    market_id: str = ""            # SIG market id (falls back to contract_id)
    exchange_id: str = ""          # SIG exchange/contract id to re-place on (falls back to contract_id)
    price: float | None = None     # limit price to re-quote at (broker validates the 0.005 tick band)
    outcome: str | None = None     # OUTCOME (yes/no) → SIG side; MUST match the original or the re-quote flips sides
    action: str = "open"           # DIRECTION intent (open/close); broker derives buy/sell from `side`

    @property
    def residual_qty(self) -> float:
        return max(self.requested_qty - self.filled_qty, 0.0)

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in TERMINAL_ORDER_STATUSES

    @property
    def is_open(self) -> bool:
        return self.status.lower() in OPEN_ORDER_STATUSES

    def fill_fraction(self) -> float:
        if self.requested_qty <= 0.0:
            return 1.0
        return min(self.filled_qty / self.requested_qty, 1.0)


@dataclass
class OrderBundle:
    """Groups all legs/slices of one strategy decision.

    Ported from mbappe PendingFillBundle with autonomous terminal policies.
    """
    bundle_id: str
    strategy_id: str
    strategy_class: StrategyClass
    fill_policy: FillPolicy
    slices: list[OrderSlice] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unresolved_cycles: int = 0     # ported from mbappe PendingFillBundle
    terminal_action_taken: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- mbappe needs_tracking logic (ported verbatim in spirit) ---

    def needs_tracking(self) -> bool:
        """True while any slice is open or the bundle is not fully filled.

        Ported from mbappe PendingFillBundle.needs_tracking().
        """
        if not self.slices:
            return False
        if any(s.is_open for s in self.slices):
            return True
        if self._total_filled() + 1e-9 < self._total_requested():
            return True
        return any(
            s.status.lower() == "partially_filled"
            or (s.requested_qty > 0.0 and s.filled_qty + 1e-9 < s.requested_qty)
            for s in self.slices
        )

    def is_fully_filled(self) -> bool:
        if not self.slices:
            return False
        return all(s.status.lower() == "filled" for s in self.slices)

    def is_terminal(self) -> bool:
        if not self.slices:
            return True
        return all(s.is_terminal for s in self.slices)

    def _total_requested(self) -> float:
        return sum(max(s.requested_qty, 0.0) for s in self.slices)

    def _total_filled(self) -> float:
        return sum(max(s.filled_qty, 0.0) for s in self.slices)

    def filled_fraction(self) -> float:
        req = self._total_requested()
        if req <= 0.0:
            return 1.0
        return min(self._total_filled() / req, 1.0)

    def has_any_fill(self) -> bool:
        return self._total_filled() > 1e-9

    def per_leg_actual_fill(self) -> dict[str, float]:
        """Actual filled qty per leg — flows back to allocator/state on ACCEPT_PARTIAL."""
        return {s.leg_id: s.filled_qty for s in self.slices}

    def open_order_ids(self) -> list[str]:
        return [s.order_id for s in self.slices if s.order_id and s.is_open]


# ---------------------------------------------------------------------------
# Pre-flight lock
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreFlightLock:
    """Hard lock latched when startup reconciliation finds unreconcilable state.

    Ported from mbappe pre_flight_reconciliation.py engage_pre_flight_recon_lock.
    """
    reason: str
    metadata: dict[str, Any]
    locked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
