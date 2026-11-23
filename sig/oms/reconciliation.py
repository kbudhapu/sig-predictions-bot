"""
Startup and periodic reconciliation — compare believed state vs broker truth.

Mechanics ported from mbappe:
  - pre_flight_reconciliation.py: hard-lock on unreconcilable state,
    unmapped-symbol → hard lock, broker-wins principle.
  - broker_fill_sieve.py: broker-authoritative position update after every fill,
    detect_leg_broker_memory_drift (mismatch → second position fetch + forced update).

Key difference from mbappe:
  - mbappe latches a SQLite lock; we latch via state.set_halt() so the
    rest of the bot (running as a coroutine) sees the halt immediately without
    re-reading a file.
  - "Significant mismatch → halt-hold" (risk spec) replaces mbappe's
    pre_flight_recon_lock; the semantics are the same.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sig.oms.models import PreFlightLock
from sig.oms.protocols import BrokerPosition, SigBroker, SigStateStore

log = structlog.get_logger()

QTY_TOLERANCE = 1e-4
PRICE_TOLERANCE = 0.02


@dataclass(frozen=True)
class PositionDelta:
    """One field mismatch between believed state and broker truth."""
    strategy_id: str
    contract_id: str
    field_name: str
    believed_value: float | str
    broker_value: float | str


@dataclass
class ReconciliationResult:
    success: bool
    hard_locked: bool
    lock_reason: str
    deltas: list[PositionDelta] = field(default_factory=list)
    broker_positions_applied: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def _broker_by_contract(positions: list[BrokerPosition]) -> dict[str, BrokerPosition]:
    return {p.contract_id.upper(): p for p in positions}


def _detect_unmapped_symbol(
    broker_by_contract: dict[str, BrokerPosition],
    known_contracts: set[str],
    broker_open_orders: list[Any],
    known_contracts_upper: set[str],
) -> str:
    """Return a hard-lock reason if broker holds anything we don't know about.

    Ported from mbappe _detect_irrecoverable_mismatch.
    """
    for cid in broker_by_contract:
        if cid not in known_contracts_upper:
            return f"unmapped_broker_contract:{cid}"
    for order in broker_open_orders:
        cid = str(order.contract_id).upper()
        if cid not in known_contracts_upper:
            return f"unmapped_open_order_contract:{cid}"
    return ""


async def run_pre_flight_reconciliation(
    *,
    broker: SigBroker,
    state: SigStateStore,
    strategy_contracts: dict[str, str],  # strategy_id → contract_id
    adopt_unmapped: bool = False,
) -> ReconciliationResult:
    """
    Compare persisted positions against live broker state on startup.

    Ported from mbappe PreFlightReconciliationEngine.execute():
    - Unmapped symbol → hard lock (halt-hold, block all trading).
    - Delta found → apply broker truth to state.
    - Unreconcilable (broker has position, no matching strategy) → hard lock.

    `adopt_unmapped=True` (the mechanical floor): a venue contract not in strategy_contracts is a
    LEGITIMATE multi-contract fill (market-making/arb trade 90+ markets; strategy_contracts only maps
    one anchor per strategy), NOT a foreign position. Instead of hard-locking, ADOPT every venue
    position (upsert by exchange_id) so the gate / MM ask-side / take-profit exit can see it. Without
    this, the first MM fill bricks startup forever (unmapped_broker_contract → hard lock → restart loop).

    Returns ReconciliationResult; caller checks .hard_locked before allowing trades.
    """
    broker_positions = await broker.get_positions()
    broker_orders = await broker.get_open_orders()
    open_orders_local = await state.read_open_orders()

    known_upper = {cid.upper() for cid in strategy_contracts.values()}
    broker_map = _broker_by_contract(broker_positions)

    irrecoverable = "" if adopt_unmapped else _detect_unmapped_symbol(
        broker_map, known_upper, broker_orders, known_upper
    )
    if irrecoverable:
        lock = PreFlightLock(
            reason=irrecoverable,
            metadata={
                "broker_contracts": list(broker_map.keys()),
                "known_contracts": list(known_upper),
            },
        )
        await state.set_halt(
            irrecoverable,
            {
                "lock_type": "pre_flight",
                "broker_contracts": list(broker_map.keys()),
            },
        )
        await state.append_audit(
            {
                "event": "pre_flight_hard_lock",
                "reason": irrecoverable,
                "locked_at": lock.locked_at.isoformat(),
            }
        )
        log.error(
            "pre_flight_hard_lock",
            reason=irrecoverable,
            broker_contracts=list(broker_map.keys()),
        )
        return ReconciliationResult(
            success=False,
            hard_locked=True,
            lock_reason=irrecoverable,
            metadata={"lock": irrecoverable},
        )

    deltas: list[PositionDelta] = []
    # Build local believed-state from open_orders + exposure
    exposure = await state.read_exposure()
    local_positions: dict[str, dict[str, Any]] = {}
    for sid, cid in strategy_contracts.items():
        cid_upper = cid.upper()
        exp = exposure.get(sid) or exposure.get(cid_upper) or {}
        local_positions[sid] = {
            "contract_id": cid_upper,
            "qty": float(exp.get("qty", 0.0)),
            "side": str(exp.get("side", "flat")),
            "avg_entry_price": float(exp.get("avg_entry_price", 0.0)),
        }

    for sid, local in local_positions.items():
        cid_upper = local["contract_id"]
        broker_pos = broker_map.get(cid_upper)
        broker_qty = float(broker_pos.qty) if broker_pos else 0.0
        broker_side = broker_pos.side if broker_pos else "flat"
        broker_price = float(broker_pos.avg_entry_price) if broker_pos else 0.0

        if abs(local["qty"] - broker_qty) > QTY_TOLERANCE:
            deltas.append(
                PositionDelta(sid, cid_upper, "qty", local["qty"], broker_qty)
            )
        if local["side"] != broker_side and not (
            local["side"] == "flat" and broker_qty <= QTY_TOLERANCE
        ):
            deltas.append(
                PositionDelta(sid, cid_upper, "side", local["side"], broker_side)
            )
        if broker_qty > QTY_TOLERANCE and abs(
            local["avg_entry_price"] - broker_price
        ) > PRICE_TOLERANCE:
            deltas.append(
                PositionDelta(
                    sid, cid_upper, "avg_entry_price",
                    local["avg_entry_price"], broker_price
                )
            )

    applied = 0
    for sid, cid in strategy_contracts.items():
        cid_upper = cid.upper()
        broker_pos = broker_map.get(cid_upper)
        position_record: dict[str, Any] = {
            "strategy_id": sid,
            "contract_id": cid_upper,
            "qty": float(broker_pos.qty) if broker_pos else 0.0,
            "side": broker_pos.side if broker_pos else "flat",
            "avg_entry_price": float(broker_pos.avg_entry_price) if broker_pos else 0.0,
            "source": "pre_flight_reconciliation",
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        await state.upsert_position(position_record)
        applied += 1

    # Mechanical floor: adopt EVERY venue position (by exchange_id), not just the strategy_contracts
    # anchors — so the gate / MM ask-side / take-profit exit see the multi-contract MM/arb holdings.
    if adopt_unmapped:
        for cid_upper, broker_pos in broker_map.items():
            if cid_upper in known_upper:
                continue  # already upserted above
            await state.upsert_position({
                "strategy_id": f"venue:{cid_upper}",
                "contract_id": cid_upper,
                "qty": float(broker_pos.qty),
                "side": broker_pos.side,
                "avg_entry_price": float(broker_pos.avg_entry_price),
                "source": "pre_flight_adopt_unmapped",
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
            })
            applied += 1

    if deltas:
        await state.append_audit(
            {
                "event": "pre_flight_reconciliation_delta",
                "delta_count": len(deltas),
                "deltas": [d.__dict__ for d in deltas],
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.warning(
            "pre_flight_reconciliation_delta_applied",
            delta_count=len(deltas),
        )

    return ReconciliationResult(
        success=True,
        hard_locked=False,
        lock_reason="",
        deltas=deltas,
        broker_positions_applied=applied,
    )


async def reconcile_against_broker(
    *,
    broker: SigBroker,
    state: SigStateStore,
    strategy_contracts: dict[str, str],
    trigger: str = "periodic",
    adopt_unmapped: bool = False,
) -> ReconciliationResult:
    """
    Periodic / feed-reconnect reconciliation.  Broker wins on any mismatch.

    Ported from mbappe _apply_broker_authority + detect_leg_broker_memory_drift:
    - First position fetch → compare to believed state.
    - Any mismatch → apply broker truth; if mismatch is significant, set halt-hold.
    - Never silently diverge.

    `adopt_unmapped=True` (mechanical floor): venue contracts outside strategy_contracts are legitimate
    multi-contract MM/arb fills, not foreign positions — don't hard-lock on them (see pre-flight).
    """
    broker_positions = await broker.get_positions()
    broker_orders = await broker.get_open_orders()
    known_upper = {cid.upper() for cid in strategy_contracts.values()}
    broker_map = _broker_by_contract(broker_positions)

    # Unmapped symbol check (same as pre-flight) — skipped when adopting multi-contract fills.
    irrecoverable = "" if adopt_unmapped else _detect_unmapped_symbol(
        broker_map, known_upper, broker_orders, known_upper
    )
    if irrecoverable:
        await state.set_halt(irrecoverable, {"trigger": trigger})
        await state.append_audit(
            {"event": "reconciliation_hard_lock", "reason": irrecoverable, "trigger": trigger}
        )
        log.error("reconciliation_hard_lock", reason=irrecoverable, trigger=trigger)
        return ReconciliationResult(
            success=False, hard_locked=True, lock_reason=irrecoverable
        )

    exposure = await state.read_exposure()
    deltas: list[PositionDelta] = []
    significant_mismatch = False

    for sid, cid in strategy_contracts.items():
        cid_upper = cid.upper()
        exp = exposure.get(sid) or exposure.get(cid_upper) or {}
        local_qty = float(exp.get("qty", 0.0))
        broker_pos = broker_map.get(cid_upper)
        broker_qty = float(broker_pos.qty) if broker_pos else 0.0

        if abs(local_qty - broker_qty) > QTY_TOLERANCE:
            deltas.append(PositionDelta(sid, cid_upper, "qty", local_qty, broker_qty))
            # significant if believed position is nonzero and broker disagrees
            if local_qty > QTY_TOLERANCE or broker_qty > QTY_TOLERANCE:
                significant_mismatch = True

        # Apply broker truth regardless
        await state.upsert_position(
            {
                "strategy_id": sid,
                "contract_id": cid_upper,
                "qty": float(broker_pos.qty) if broker_pos else 0.0,
                "side": broker_pos.side if broker_pos else "flat",
                "avg_entry_price": float(broker_pos.avg_entry_price) if broker_pos else 0.0,
                "source": f"reconciliation:{trigger}",
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    if deltas:
        await state.append_audit(
            {
                "event": "reconciliation_delta",
                "trigger": trigger,
                "delta_count": len(deltas),
                "deltas": [d.__dict__ for d in deltas],
            }
        )
        log.warning(
            "reconciliation_broker_truth_applied",
            trigger=trigger,
            delta_count=len(deltas),
            significant=significant_mismatch,
        )

    if significant_mismatch:
        reason = f"significant_position_mismatch:{trigger}"
        await state.set_halt(reason, {"delta_count": len(deltas)})
        log.error("reconciliation_halt_hold_latched", reason=reason)
        return ReconciliationResult(
            success=False,
            hard_locked=True,
            lock_reason=reason,
            deltas=deltas,
        )

    return ReconciliationResult(
        success=True,
        hard_locked=False,
        lock_reason="",
        deltas=deltas,
        broker_positions_applied=len(strategy_contracts),
    )
