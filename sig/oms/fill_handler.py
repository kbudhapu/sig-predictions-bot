"""
Autonomous partial-fill handler — replaces mbappe's "abandon and alert operator".

mbappe mechanics REUSED:
  - advance_pending_cycles pattern: each cycle, refresh every tracked bundle.
  - cycle_cancel_threshold (INCOMPLETE_CYCLE_CANCEL_THRESHOLD = 3 in mbappe).
  - cancel-then-resubmit for replace (no native amend).
  - broker-authoritative position update after every action.

mbappe pattern NOT COPIED:
  - "partial_fill_residual_abandoned" + "operator must act manually".
    Replaced with three autonomous policies (see PartialFillPolicy):
    1. UNWIND_ALL_LEGS  — arb: cancel everything, net-flat, log critical.
    2. LEAVE_RESTING    — market-making: do nothing, bundle stays tracked.
    3. CHASE            — directional: cancel residual, re-quote with idempotency.
    4. ACCEPT_PARTIAL   — directional: record actual fill, update position, done.
"""

from __future__ import annotations

import structlog
import uuid
from datetime import datetime, timezone
from typing import Any

from sig.oms.models import (
    FINISHED_ORDER_STATUSES,
    cancel_confirmed,
    FillPolicy,
    OrderBundle,
    OrderLifecycle,
    OrderSlice,
    PartialFillPolicy,
    StrategyClass,
    TERMINAL_ORDER_STATUSES,
    OPEN_ORDER_STATUSES,
)
from sig.oms.protocols import BrokerOrderSnapshot, SigBroker, SigStateStore

log = structlog.get_logger()

# Chase price-laddering (exit/entry fill-through on the thin peer-to-peer book). Re-quoting at the same
# price never fills; each chase attempt crosses one more tick toward liquidity, bounded by the max walk.
_CHASE_TICK = 0.005
_CHASE_MAX_WALK = 0.03        # never give away more than ~6 ticks chasing a fill
_PRICE_LO, _PRICE_HI = 0.005, 0.995


def _chase_price(base: float | None, side: str | None, attempt: int) -> float | None:
    """Price for a chase re-quote: walk `base` toward liquidity by one tick per attempt (capped).
    SELL → undercut (lower, hit a resting buyer); BUY → lift (higher, take a resting seller)."""
    if base is None:
        return None
    try:
        base = float(base)
    except (TypeError, ValueError):
        return base
    walk = min(max(int(attempt), 1) * _CHASE_TICK, _CHASE_MAX_WALK)
    d = str(side or "").lower()
    if d == "sell":
        p = base - walk
    elif d == "buy":
        p = base + walk
    else:
        p = base
    p = round(round(p / _CHASE_TICK) * _CHASE_TICK, 3)
    return min(max(p, _PRICE_LO), _PRICE_HI)


async def _refresh_slice(
    slice_: OrderSlice,
    broker: SigBroker,
) -> OrderSlice:
    """Fetch latest order state from broker and update the slice in place."""
    if slice_.order_id is None:
        return slice_
    snapshot = await broker.get_order(slice_.order_id)
    if snapshot is None:
        return slice_
    if getattr(slice_, "cancel_confirmed", False):
        # our cancel was CONFIRMED: final. A later read may still say partially_filled (closed with a partial
        # fill and no terminalReasonCode) — that must not make the slice look resting again (re-cancel loop).
        try:
            slice_.filled_qty = max(float(slice_.filled_qty or 0.0), float(snapshot.filled_qty or 0.0))
        except (TypeError, ValueError):
            pass
        slice_.status = "cancelled"
        return slice_
    if str(getattr(snapshot, "status", "")).lower() == "unknown":
        # failed read (429/5xx/transport): keep the last known status/fill — overwriting with the read's
        # filled_qty=0 + 'unknown' made a resting order look finished and orphaned it.
        return slice_
    slice_.filled_qty = float(snapshot.filled_qty)
    # The canonical broker's OrderResult exposes `avg_price`; some snapshots use `avg_fill_price`.
    _avg = getattr(snapshot, "avg_price", None)
    try:
        _avg = float(_avg)
    except (TypeError, ValueError):
        try:
            _avg = float(getattr(snapshot, "avg_fill_price", 0.0))
        except (TypeError, ValueError):
            _avg = 0.0
    slice_.avg_fill_price = _avg
    slice_.status = str(snapshot.status).lower()
    slice_.last_updated = datetime.now(timezone.utc)
    return slice_


async def _apply_broker_authority_to_state(
    bundle: OrderBundle,
    broker: SigBroker,
    state: SigStateStore,
) -> None:
    """
    After any fill action, re-fetch broker positions and write them as truth.

    Ported from mbappe _apply_broker_authority + detect_leg_broker_memory_drift.
    Broker wins: never let believed state diverge from broker reality.
    """
    try:
        broker_positions = await broker.get_positions()
    except Exception as exc:  # noqa: BLE001 — a read 429 must not fail the trading cycle; the 60s
        # position-resync loop pins state to venue truth anyway.
        log.warning("broker_authority_refresh_skipped", bundle_id=bundle.bundle_id, error=type(exc).__name__)
        return
    broker_map = {p.contract_id.upper(): p for p in broker_positions}

    for slice_ in bundle.slices:
        cid = slice_.contract_id.upper()
        broker_pos = broker_map.get(cid)
        await state.upsert_position(
            {
                "strategy_id": bundle.strategy_id,
                "contract_id": cid,
                "qty": float(broker_pos.qty) if broker_pos else 0.0,
                "side": broker_pos.side if broker_pos else "flat",
                "avg_entry_price": float(broker_pos.avg_entry_price) if broker_pos else 0.0,
                "source": "fill_handler:broker_authority",
                "bundle_id": bundle.bundle_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        # Detect drift — if broker says flat but bundle says filled, log warning
        if broker_pos is None or float(broker_pos.qty) < 1e-9:
            if slice_.filled_qty > 1e-9:
                log.warning(
                    "broker_memory_drift_detected",
                    strategy_id=bundle.strategy_id,
                    contract_id=cid,
                    believed_filled_qty=slice_.filled_qty,
                    broker_qty=0.0,
                )


# ---------------------------------------------------------------------------
# Terminal autonomous actions (replaces mbappe's abandon-to-human)
# ---------------------------------------------------------------------------

async def _handle_arb_unwind(
    bundle: OrderBundle,
    broker: SigBroker,
    state: SigStateStore,
) -> None:
    """
    ARB: atomic-or-cancel-ALL-legs.  Any partial → unwind everything.

    mbappe would abandon and alert a human.  We cancel every open leg and
    bring the bot back to flat — no naked directional positions.
    """
    cancelled = 0
    for slice_ in bundle.slices:
        if slice_.order_id and (slice_.is_open or slice_.status == "partially_filled"):
            ok = cancel_confirmed(await broker.cancel_order(slice_.order_id))
            if ok:
                slice_.status = "cancelled"
                slice_.last_updated = datetime.now(timezone.utc)
                cancelled += 1
                await state.update_order_status(
                    slice_.order_id, "cancelled",
                    metadata={"reason": "arb_partial_unwind"}
                )

    # Broker authority: re-fetch and write truth
    await _apply_broker_authority_to_state(bundle, broker, state)

    bundle.terminal_action_taken = True
    log.error(
        "arb_partial_fill_full_unwind",
        bundle_id=bundle.bundle_id,
        strategy_id=bundle.strategy_id,
        cancelled_legs=cancelled,
        total_legs=len(bundle.slices),
        filled_fraction=bundle.filled_fraction(),
        action="all_legs_cancelled_bot_is_flat",
    )
    await state.append_audit(
        {
            "event": "arb_partial_unwind",
            "bundle_id": bundle.bundle_id,
            "strategy_id": bundle.strategy_id,
            "cancelled_legs": cancelled,
            "filled_fraction": bundle.filled_fraction(),
        }
    )


async def _handle_directional_chase(
    bundle: OrderBundle,
    broker: SigBroker,
    state: SigStateStore,
    policy: FillPolicy,
) -> None:
    """
    DIRECTIONAL/CHASE: cancel residual open orders, re-quote unfilled qty.

    Replace = cancel + re-submit with a new idempotency key.
    The idempotency key prevents double-placement if the first cancel is slow.
    """
    for slice_ in bundle.slices:
        if not (slice_.is_open or slice_.status == "partially_filled"):
            continue
        residual = slice_.residual_qty
        if residual < 1e-9:
            continue
        if slice_.chase_attempts >= policy.chase_max_attempts:
            # Exhausted — fall back to ACCEPT_PARTIAL for this slice
            log.warning(
                "directional_chase_exhausted_accepting_partial",
                bundle_id=bundle.bundle_id,
                strategy_id=bundle.strategy_id,
                contract_id=slice_.contract_id,
                chase_attempts=slice_.chase_attempts,
                filled_qty=slice_.filled_qty,
                residual_qty=residual,
            )
            if slice_.order_id:
                if not cancel_confirmed(await broker.cancel_order(slice_.order_id)):
                    log.warning("chase_cancel_unconfirmed_keep_tracking", order_id=slice_.order_id)
                    continue                            # still resting: retry the cancel next cycle
                slice_.status = "cancelled"
                await state.update_order_status(
                    slice_.order_id, "cancelled",
                    metadata={"reason": "chase_exhausted_accept_partial"}
                )
            continue

        # Cancel open order — and NEVER re-place unless the cancel is confirmed (else two live orders)
        if slice_.order_id:
            if not cancel_confirmed(await broker.cancel_order(slice_.order_id)):
                log.warning("chase_cancel_unconfirmed_no_requote", order_id=slice_.order_id)
                continue
            slice_.cancel_confirmed = True
            await state.update_order_status(
                slice_.order_id, "cancelled",
                metadata={"reason": "chase_cancel_for_requote"}
            )

        # Re-quote the residual. IDEMPOTENCY IS OWNED BY THE BROKER — it derives a deterministic key
        # from (strategy_id, market_id, outcome, intent_hash); the OMS must NOT mint or pass one. Rebuild
        # the FULL order from the slice's captured placement inputs via the broker's REAL signature
        # (strategy_id, market_id, exchange_id, side, action, quantity, price, outcome). The previous
        # call used place_order(contract_id=…, qty=…, order_type=…, idempotency_key=…) — kwargs that do
        # not exist — so it raised TypeError, failed the cycle, tripped soft-degrade, and left the
        # residual CANCELLED-then-DROPPED (this path also runs take-profit EXIT re-quotes).
        residual_int = int(round(residual))
        if residual_int < 1:
            # Nothing meaningful left (sub-contract residual); the cancel above stands — resolve slice.
            slice_.status = "cancelled"
            slice_.last_updated = datetime.now(timezone.utc)
            continue
        slice_.chase_attempts += 1
        # LADDER THE PRICE toward liquidity on each chase attempt. Re-quoting at the SAME price never fills
        # on a thin peer-to-peer book (the reason exits sat at quantityTraded:0 and we couldn't get out) —
        # each attempt crosses one more tick: a SELL undercuts further (hit a resting buyer), a BUY lifts
        # further (take a resting seller). Bounded by _CHASE_MAX_WALK so we never give away more than that;
        # when the book is genuinely empty it hits the cap and the accept-partial path resolves it.
        quote_price = _chase_price(slice_.price, slice_.side, slice_.chase_attempts)
        new_result = await broker.place_order(
            strategy_id=bundle.strategy_id,
            # a fresh idempotency scope per chase attempt: a re-quote whose price/size repeats an earlier one
            # (price clamped at the band, same residual) must not replay that CANCELLED order's stored response
            market_id=f"{(slice_.market_id or slice_.contract_id)}#chase{slice_.chase_attempts}-{uuid.uuid4().hex[:8]}",
            exchange_id=(slice_.exchange_id or slice_.contract_id),
            side=slice_.side,                 # DIRECTION (buy/sell) → SIG action
            action=slice_.action,             # open/close intent
            quantity=residual_int,
            price=quote_price,                # laddered toward liquidity (broker validates tick)
            outcome=slice_.outcome,           # OUTCOME (yes/no) → SIG side; must match the original
        )
        new_order_id = getattr(new_result, "order_id", None)
        new_status = str(getattr(new_result, "status", "submitted") or "submitted").lower()
        broker_key = getattr(new_result, "idempotency_key", None)
        slice_.order_id = new_order_id
        slice_.requested_qty = residual
        slice_.filled_qty = float(getattr(new_result, "filled_qty", 0.0) or 0.0)
        slice_.status = new_status
        slice_.last_updated = datetime.now(timezone.utc)

        await state.write_order(
            {
                "order_id": new_order_id,
                "bundle_id": bundle.bundle_id,
                "strategy_id": bundle.strategy_id,
                "contract_id": slice_.contract_id,
                "side": slice_.side,
                "qty": residual,
                "status": new_status,
                "idempotency_key": broker_key if isinstance(broker_key, str) else None,  # broker-owned
                "chase_attempt": slice_.chase_attempts,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.info(
            "directional_chase_requote",
            bundle_id=bundle.bundle_id,
            contract_id=slice_.contract_id,
            residual_qty=residual,
            attempt=slice_.chase_attempts,
            new_order_id=new_order_id,
            status=new_status,
        )

    await _apply_broker_authority_to_state(bundle, broker, state)


async def _handle_directional_accept_partial(
    bundle: OrderBundle,
    broker: SigBroker,
    state: SigStateStore,
) -> None:
    """
    DIRECTIONAL/ACCEPT_PARTIAL: take what filled, cancel the rest.

    CRITICAL: actual filled qty flows back to state/positions/allocator so the
    bot's view matches reality — the position is intentionally smaller than
    intended, and the sizing/allocation layer sees that.
    """
    unconfirmed = 0
    for slice_ in bundle.slices:
        if slice_.is_open and slice_.order_id:
            if not cancel_confirmed(await broker.cancel_order(slice_.order_id)):
                unconfirmed += 1
                continue
            slice_.status = "cancelled"
            await state.update_order_status(
                slice_.order_id, "cancelled",
                metadata={"reason": "accept_partial_cancel_residual"}
            )
    if unconfirmed:
        log.warning("accept_partial_cancel_unconfirmed_keep_tracking", bundle_id=bundle.bundle_id, n=unconfirmed)
        return False

    # Broker authority — actual fill written to positions
    await _apply_broker_authority_to_state(bundle, broker, state)

    actual_fills = bundle.per_leg_actual_fill()
    bundle.terminal_action_taken = True
    log.info(
        "directional_accept_partial_fill",
        bundle_id=bundle.bundle_id,
        strategy_id=bundle.strategy_id,
        actual_fills=actual_fills,
        filled_fraction=bundle.filled_fraction(),
    )
    await state.append_audit(
        {
            "event": "directional_accept_partial",
            "bundle_id": bundle.bundle_id,
            "strategy_id": bundle.strategy_id,
            "actual_fills": actual_fills,
            "filled_fraction": bundle.filled_fraction(),
        }
    )


# ---------------------------------------------------------------------------
# Main advance function — called each cycle for all tracked bundles
# ---------------------------------------------------------------------------

async def advance_bundle(
    bundle: OrderBundle,
    broker: SigBroker,
    state: SigStateStore,
) -> bool:
    """
    Advance one tracked bundle by one cycle.

    Ported from mbappe advance_pending_cycles / _advance_bundle.
    Returns True when the bundle is done (should be removed from tracking).
    """
    if bundle.terminal_action_taken:
        return True

    # Refresh all slices from broker
    for slice_ in bundle.slices:
        await _refresh_slice(slice_, broker)
        if slice_.filled_qty > 1e-9 or slice_.status in TERMINAL_ORDER_STATUSES:
            await state.update_order_status(
                slice_.order_id or "",
                slice_.status,
                filled_qty=slice_.filled_qty,
                avg_fill_price=slice_.avg_fill_price,
            )

    # Fully filled — broker authority + done
    if bundle.is_fully_filled():
        await _apply_broker_authority_to_state(bundle, broker, state)
        log.info(
            "bundle_fully_filled",
            bundle_id=bundle.bundle_id,
            strategy_id=bundle.strategy_id,
        )
        return True

    # MAKER REST: resolve as soon as every slice is DEFINITIVELY finished (filled/cancelled/rejected/expired) —
    # e.g. after the timeout sweep's confirmed cancel. partially_filled (may still rest) and unknown (failed
    # read) keep the bundle tracked so the sweep can cancel the residual. Scoped to maker_rest only; every other
    # policy is unchanged. (Without this, maker bundles — which never reach the cycle threshold — would stay
    # pending forever and block the strategy's next entry.)
    if (getattr(bundle.fill_policy, "maker_rest", False) and bundle.slices
            and all(getattr(s, "cancel_confirmed", False) or str(s.status).lower() in FINISHED_ORDER_STATUSES
                    for s in bundle.slices)):
        await _apply_broker_authority_to_state(bundle, broker, state)
        bundle.terminal_action_taken = True
        log.info("maker_rest_bundle_resolved", bundle_id=bundle.bundle_id, strategy_id=bundle.strategy_id,
                 filled_fraction=bundle.filled_fraction())
        return True

    # Not fully filled — still needs tracking?
    if not bundle.needs_tracking():
        await _apply_broker_authority_to_state(bundle, broker, state)
        return True

    # Increment unresolved cycle count (ported from mbappe)
    bundle.unresolved_cycles += 1
    policy = bundle.fill_policy

    if bundle.unresolved_cycles < policy.cycle_cancel_threshold:
        # Still within patience window — leave open
        log.debug(
            "bundle_awaiting_fill",
            bundle_id=bundle.bundle_id,
            strategy_id=bundle.strategy_id,
            unresolved_cycles=bundle.unresolved_cycles,
            threshold=policy.cycle_cancel_threshold,
        )
        return False

    # --- Terminal resolution (autonomous — NOT abandon-to-human) ---
    log.info(
        "bundle_terminal_resolution",
        bundle_id=bundle.bundle_id,
        strategy_id=bundle.strategy_id,
        policy=policy.partial_fill_policy.value,
        unresolved_cycles=bundle.unresolved_cycles,
        filled_fraction=bundle.filled_fraction(),
    )

    if policy.partial_fill_policy == PartialFillPolicy.UNWIND_ALL_LEGS:
        await _handle_arb_unwind(bundle, broker, state)
        return True

    if policy.partial_fill_policy == PartialFillPolicy.LEAVE_RESTING:
        # Market-making: resting quote is fine, keep tracking indefinitely
        # Reset cycle counter so we don't spam on every cycle
        bundle.unresolved_cycles = 0
        log.debug(
            "market_making_leave_resting",
            bundle_id=bundle.bundle_id,
        )
        return False

    if policy.partial_fill_policy == PartialFillPolicy.CHASE:
        await _handle_directional_chase(bundle, broker, state, policy)
        # If all slices exhausted chase and switched to accept, mark done
        all_done = all(
            s.is_terminal and not s.is_open for s in bundle.slices
        )
        if all_done:
            bundle.terminal_action_taken = True
            return True
        bundle.unresolved_cycles = 0
        return False

    if policy.partial_fill_policy == PartialFillPolicy.ACCEPT_PARTIAL:
        done = await _handle_directional_accept_partial(bundle, broker, state)
        return done is not False                    # unconfirmed cancel -> keep tracking, retry next cycle

    # Unreachable — failsafe: cancel everything
    log.error(
        "unknown_fill_policy_failsafe_cancel",
        policy=policy.partial_fill_policy,
        bundle_id=bundle.bundle_id,
    )
    unconfirmed = 0
    for slice_ in bundle.slices:
        if slice_.order_id and slice_.is_open:
            if not cancel_confirmed(await broker.cancel_order(slice_.order_id)):
                unconfirmed += 1
    if unconfirmed:
        return False
    bundle.terminal_action_taken = True
    return True
