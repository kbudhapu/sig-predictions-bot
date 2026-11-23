"""PORTED from mbappe src/execution/idempotent_execution.py — the idempotency keystone.

Broker-agnostic by construction: everything operates against the small ``ExecutionBroker`` protocol,
so it is exercised entirely with a mock in tests and NEVER sends a live order itself. A SIG broker
becomes an ``ExecutionBroker`` by exposing submit / get_by_client_order_id / open_orders / positions.

SIG ADAPTATION (the only change vs mbappe): the deterministic client-order-id FORMAT. mbappe hardcoded
``mbappe-`` + 38 hex (Alpaca's 48-char ceiling). SIG sends an ``idempotencyKey``; the prefix and hash
length are now CONFIG-DRIVEN module constants so matching SIG's key rules is a one-line change, not a
rewrite. (Exact SIG length/charset is confirmed against the live /orders schema in Phase 2/3; the
default below is conservative and URL/JSON-safe.)

Design invariants (unchanged from mbappe):
- Idempotency by construction: deterministic id = hash(leg_id, signal_ts, side, epoch); query-before-
  submit NO-OPs on a match, so a duplicate-submit storm yields exactly one broker order.
- No silent adoption: boot reconciliation DIFFS internal vs broker and trips ENTRY_GATE_HALT on ANY
  mismatch; it never overwrites internal state from the broker.
- Preemption leaves no orphan: the id is reserved (deterministic) before the network send.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from infra.escalation import (
    EnginePreemptedException,
    PreemptionCheckpoint,
    RiskEscalationEngine,
    RiskEscalationLevel,
)
from infra.entry_authority import EntryDecision

# ── SIG idempotency-key format (replaces Alpaca's hardcoded "mbappe-" + 38 hex / 48-char ceiling) ──
# Confirm SIG's idempotencyKey max length + charset against the live schema in Phase 2/3; these
# defaults (prefix + 48 hex = 52 chars, lowercase hex + one dash) are safe for any sane limit.
SIG_KEY_PREFIX = "sig"
SIG_KEY_HASH_LEN = 48


class EntryBlockedException(Exception):
    """Raised at the single submission gate (E1) when the composed entry authority blocks the order.
    Because the client_order_id is reserved deterministically BEFORE this check, a blocked order leaves
    no orphan and no broker order is ever sent."""

    def __init__(self, decision: EntryDecision, *, leg_id: str, symbol: str) -> None:
        self.decision = decision
        self.leg_id = leg_id
        self.symbol = symbol
        super().__init__(f"entry blocked at submission: {decision.block_reason} (leg={leg_id} sym={symbol})")


# Terminal broker statuses (no further fills expected). SIG's status enum is REMAPPED onto this set by
# the SIG broker adapter (sig/sig_broker.py) — this layer stays vocabulary-agnostic.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "cancelled", "expired", "rejected"})
FILLABLE_STATUSES = frozenset({"filled", "partially_filled"})


def deterministic_client_order_id(
    leg_id: str, signal_ts: datetime | str, side: str, epoch: int | str,
    *, prefix: str = SIG_KEY_PREFIX, hash_len: int = SIG_KEY_HASH_LEN,
) -> str:
    """Deterministic, collision-resistant idempotency key. Same (leg_id, signal_ts, side, epoch) ->
    same key, so retries and post-restart re-submits dedupe. Prefix + hash length are config-driven to
    fit SIG's idempotencyKey rules."""
    ts = signal_ts.isoformat() if isinstance(signal_ts, datetime) else str(signal_ts)
    raw = f"{leg_id}|{ts}|{str(side).lower()}|{epoch}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:hash_len]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class OrderIntent:
    leg_id: str
    symbol: str
    side: str                     # generic "buy" | "sell"; SIG adapter maps to exchangeId/side/action
    qty: float
    signal_ts: datetime | str
    epoch: int

    @property
    def client_order_id(self) -> str:
        return deterministic_client_order_id(self.leg_id, self.signal_ts, self.side, self.epoch)


@dataclass
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str
    qty: float
    status: str = "new"
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    order_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in TERMINAL_STATUSES

    @property
    def remainder(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)


class ExecutionBroker(Protocol):
    """Minimal contract the robustness layer needs (mock in tests, SigBroker in prod)."""

    def submit(self, intent: OrderIntent) -> BrokerOrder: ...
    def get_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def positions(self) -> dict[str, float]: ...


@dataclass
class SubmitResult:
    order: BrokerOrder
    was_noop: bool                # True == an existing order matched the id (dedup)


class IdempotentSubmitter:
    """Query-before-submit idempotent order placement. Thread-safe: a duplicate-submit storm on one
    client_order_id yields exactly one broker order (first submit wins; others observe it and NO-OP)."""

    def __init__(self, broker: ExecutionBroker, *, escalation: RiskEscalationEngine | None = None,
                 report_sink: Callable[[dict], None] | None = None) -> None:
        self._broker = broker
        self._escalation = escalation or RiskEscalationEngine()
        self._report_sink = report_sink or (lambda _r: None)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _coid_lock(self, coid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(coid, threading.Lock())

    def submit(self, intent: OrderIntent, *, preempt_check: Callable[[], bool] | None = None,
               entry_guard: Callable[[], EntryDecision] | None = None) -> SubmitResult:
        """Submit ``intent`` idempotently. ``entry_guard`` (optional) is the ONE composed kill-state gate,
        evaluated AFTER the id is reserved but BEFORE the network send (kill outranks all). ``preempt_check``
        is polled at the same checkpoint; either leaves no orphan because the id is deterministic."""
        coid = intent.client_order_id
        with self._coid_lock(coid):
            existing = self._broker.get_by_client_order_id(coid)
            if existing is not None:
                return SubmitResult(order=existing, was_noop=True)

            if entry_guard is not None:
                decision = entry_guard()
                if not decision.allowed:
                    raise EntryBlockedException(decision, leg_id=intent.leg_id, symbol=intent.symbol)

            if preempt_check is not None and preempt_check():
                raise EnginePreemptedException(
                    PreemptionCheckpoint(phase="submit", step="pre_send",
                                         strategy_id=intent.leg_id, symbol=intent.symbol),
                    escalation_level=self._escalation.snapshot().level)

            order = self._broker.submit(intent)
            if order.status.lower() == "rejected":
                self._report_sink({"kind": "order_rejected", "leg_id": intent.leg_id,
                                   "symbol": intent.symbol, "side": intent.side,
                                   "client_order_id": coid, "qty": intent.qty})
            return SubmitResult(order=order, was_noop=False)


@dataclass
class TrackedPosition:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0


class PositionTracker:
    """Applies broker fills to internal positions with explicit state transitions. Idempotent per
    client_order_id: applying the same order twice never double-counts."""

    def __init__(self) -> None:
        self._positions: dict[str, TrackedPosition] = {}
        self._applied: set[str] = set()

    def position(self, symbol: str) -> float:
        p = self._positions.get(symbol.upper())
        return p.qty if p else 0.0

    def snapshot(self) -> dict[str, float]:
        return {s: p.qty for s, p in self._positions.items() if abs(p.qty) > 1e-12}

    def apply(self, order: BrokerOrder, *, fill_price: float | None = None) -> float:
        status = order.status.lower()
        if status in {"rejected", "submission_failed", "canceled", "cancelled", "new"} and order.filled_qty <= 0.0:
            return order.remainder if status == "new" else 0.0
        if order.client_order_id in self._applied:
            return order.remainder
        if order.filled_qty <= 0.0:
            return order.remainder

        sym = order.symbol.upper()
        pos = self._positions.setdefault(sym, TrackedPosition(symbol=sym))
        signed = order.filled_qty if order.side.lower() == "buy" else -order.filled_qty
        price = fill_price if fill_price is not None else order.filled_avg_price
        new_qty = pos.qty + signed
        if pos.qty == 0.0 or (pos.qty > 0) == (signed > 0):
            total = abs(pos.qty) + abs(signed)
            if total > 0:
                pos.avg_price = (abs(pos.qty) * pos.avg_price + abs(signed) * price) / total
        pos.qty = new_qty
        self._applied.add(order.client_order_id)
        return order.remainder


@dataclass(frozen=True)
class ReconcileMismatch:
    kind: str
    symbol: str
    internal: float | None
    broker: float | None


@dataclass
class ReconcileReport:
    mismatches: list[ReconcileMismatch] = field(default_factory=list)
    tripped: bool = False

    @property
    def clean(self) -> bool:
        return not self.mismatches


def reconcile_boot(internal_positions: dict[str, float], broker_positions: dict[str, float], *,
                   internal_open_coids: set[str] | None = None, broker_open_coids: set[str] | None = None,
                   qty_tol: float = 1e-6, escalation: RiskEscalationEngine | None = None,
                   report_sink: Callable[[dict], None] | None = None) -> ReconcileReport:
    """Diff internal vs broker state at startup. ANY mismatch trips ENTRY_GATE_HALT and emits a
    structured report; internal state is NEVER overwritten from the broker (silent adoption prohibited)."""
    report = ReconcileReport()
    syms = {s.upper() for s in internal_positions} | {s.upper() for s in broker_positions}
    internal = {s.upper(): q for s, q in internal_positions.items()}
    broker = {s.upper(): q for s, q in broker_positions.items()}
    for sym in sorted(syms):
        iq = internal.get(sym, 0.0)
        bq = broker.get(sym, 0.0)
        if abs(iq) < qty_tol and abs(bq) >= qty_tol:
            report.mismatches.append(ReconcileMismatch("unknown_broker_position", sym, iq, bq))
        elif abs(iq) >= qty_tol and abs(bq) < qty_tol:
            report.mismatches.append(ReconcileMismatch("missing_broker_position", sym, iq, bq))
        elif abs(iq - bq) > qty_tol:
            report.mismatches.append(ReconcileMismatch("position_qty", sym, iq, bq))

    if internal_open_coids is not None or broker_open_coids is not None:
        ioc = internal_open_coids or set()
        boc = broker_open_coids or set()
        for coid in sorted(boc - ioc):
            report.mismatches.append(ReconcileMismatch("orphan_open_order", coid, None, None))

    if report.mismatches:
        report.tripped = True
        if escalation is not None:
            escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT, commanded_by="boot_reconciliation")
        if report_sink is not None:
            report_sink({"kind": "boot_reconciliation_mismatch", "mismatch_count": len(report.mismatches),
                         "mismatches": [{"kind": m.kind, "symbol": m.symbol, "internal": m.internal, "broker": m.broker}
                                        for m in report.mismatches]})
    return report
