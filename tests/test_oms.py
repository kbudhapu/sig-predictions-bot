"""
OMS test suite.

Covers:
  1. Lifecycle transitions (PENDING → SUBMITTED → WORKING → FILLED/CANCELLED/…)
  2. Partial-fill policy — arb: unwind all legs
  3. Partial-fill policy — market-making: leave resting
  4. Partial-fill policy — directional: chase (re-quote residual)
  5. Partial-fill policy — directional: accept-partial with actual-size-to-state
  6. Unfilled timeout: cancel + NOT abandon-to-human
  7. Cancel/replace does NOT double-place (idempotency)
  8. Reconciliation detects + corrects mismatch, broker truth wins
  9. Pre-flight hard-lock on unreconcilable state (unmapped symbol)
 10. Entry-guard blocks re-entry while bundle unresolved
 11. Arb partial → full unwind (never a naked leg)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sig.oms.fill_handler import advance_bundle
from sig.oms.manager import OrderManagementSystem
from sig.oms.models import (
    FillPolicy,
    OrderBundle,
    OrderLifecycle,
    OrderSlice,
    PartialFillPolicy,
    StrategyClass,
)
from sig.oms.reconciliation import run_pre_flight_reconciliation


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _make_order_snapshot(
    order_id: str = "ord-1",
    contract_id: str = "YES_AZ",
    side: str = "buy",
    qty: float = 10.0,
    filled_qty: float = 0.0,
    avg_fill_price: float = 0.0,
    status: str = "open",
) -> MagicMock:
    snap = MagicMock()
    snap.order_id = order_id
    snap.contract_id = contract_id
    snap.side = side
    snap.qty = qty
    snap.filled_qty = filled_qty
    snap.avg_fill_price = avg_fill_price
    snap.status = status
    return snap


def _make_position(
    contract_id: str = "YES_AZ",
    qty: float = 10.0,
    side: str = "long",
    avg_entry_price: float = 0.55,
) -> MagicMock:
    pos = MagicMock()
    pos.contract_id = contract_id
    pos.qty = qty
    pos.side = side
    pos.avg_entry_price = avg_entry_price
    return pos


def _make_broker(
    place_order_result: MagicMock | None = None,
    get_order_result: MagicMock | None = None,
    positions: list | None = None,
    open_orders: list | None = None,
    cancel_result: bool = True,
    place_multi_result: list | None = None,
) -> MagicMock:
    broker = MagicMock()
    broker.place_order = AsyncMock(
        return_value=place_order_result or _make_order_snapshot()
    )
    broker.place_multi_leg = AsyncMock(
        return_value=place_multi_result or [_make_order_snapshot()]
    )
    broker.cancel_order = AsyncMock(return_value=cancel_result)
    broker.cancel_all = AsyncMock(return_value=0)
    broker.get_order = AsyncMock(return_value=get_order_result or _make_order_snapshot())
    broker.get_positions = AsyncMock(return_value=positions or [])
    broker.get_open_orders = AsyncMock(return_value=open_orders or [])
    return broker


def _make_state() -> MagicMock:
    state = MagicMock()
    state.write_order = AsyncMock()
    state.update_order_status = AsyncMock()
    state.upsert_position = AsyncMock()
    state.read_open_orders = AsyncMock(return_value=[])
    state.read_exposure = AsyncMock(return_value={})
    state.append_audit = AsyncMock()
    state.set_halt = AsyncMock()
    state.clear_halt = AsyncMock()
    state.get_halt = AsyncMock(return_value=None)
    return state


def _make_oms(
    broker: MagicMock | None = None,
    state: MagicMock | None = None,
    strategy_contracts: dict | None = None,
    policies: dict | None = None,
) -> OrderManagementSystem:
    return OrderManagementSystem(
        broker=broker or _make_broker(),
        state=state or _make_state(),
        strategy_contracts=strategy_contracts or {"strat_1": "YES_AZ"},
        default_fill_policies=policies or {
            "strat_1": FillPolicy.for_directional_accept(),
        },
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle transitions
# ---------------------------------------------------------------------------

class TestLifecycleTransitions:
    def test_order_lifecycle_from_broker_status(self):
        assert OrderLifecycle.from_broker_status("new") == OrderLifecycle.WORKING
        assert OrderLifecycle.from_broker_status("filled") == OrderLifecycle.FILLED
        assert OrderLifecycle.from_broker_status("partially_filled") == OrderLifecycle.PARTIALLY_FILLED
        assert OrderLifecycle.from_broker_status("canceled") == OrderLifecycle.CANCELLED
        assert OrderLifecycle.from_broker_status("rejected") == OrderLifecycle.REJECTED
        assert OrderLifecycle.from_broker_status("expired") == OrderLifecycle.EXPIRED

    def test_terminal_states(self):
        assert OrderLifecycle.FILLED.is_terminal()
        assert OrderLifecycle.CANCELLED.is_terminal()
        assert OrderLifecycle.REJECTED.is_terminal()
        assert not OrderLifecycle.WORKING.is_terminal()
        assert not OrderLifecycle.SUBMITTED.is_terminal()

    @pytest.mark.asyncio
    async def test_submit_creates_bundle_and_writes_order(self):
        broker = _make_broker(
            place_order_result=_make_order_snapshot(
                order_id="ord-abc", status="open", filled_qty=0.0
            )
        )
        state = _make_state()
        oms = _make_oms(broker=broker, state=state)
        bundle = await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        assert bundle.strategy_id == "strat_1"
        assert len(bundle.slices) == 1
        assert bundle.slices[0].order_id == "ord-abc"
        state.write_order.assert_awaited_once()
        call_kwargs = state.write_order.call_args[0][0]
        assert call_kwargs["order_id"] == "ord-abc"
        assert call_kwargs["strategy_id"] == "strat_1"

    @pytest.mark.asyncio
    async def test_fully_filled_bundle_not_tracked(self):
        broker = _make_broker(
            place_order_result=_make_order_snapshot(
                status="filled", filled_qty=5.0, qty=5.0
            )
        )
        oms = _make_oms(broker=broker)
        bundle = await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        assert await oms.pending_bundle_count() == 0

    @pytest.mark.asyncio
    async def test_open_bundle_is_tracked(self):
        broker = _make_broker(
            place_order_result=_make_order_snapshot(status="open", filled_qty=0.0)
        )
        oms = _make_oms(broker=broker)
        await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        assert await oms.pending_bundle_count() == 1


# ---------------------------------------------------------------------------
# 2. Partial-fill policy — arb: unwind all legs
# ---------------------------------------------------------------------------

class TestArbUnwindAllLegs:
    def _arb_bundle(
        self,
        leg1_status: str = "partially_filled",
        leg1_filled: float = 3.0,
        leg2_status: str = "open",
        leg2_filled: float = 0.0,
    ) -> OrderBundle:
        policy = FillPolicy.for_arb(cycle_cancel_threshold=3)
        bundle = OrderBundle(
            bundle_id="b-arb-1",
            strategy_id="arb_az",
            strategy_class=StrategyClass.ARB,
            fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="leg-1", leg_id="leg_0",
                contract_id="YES_AZ", side="buy",
                requested_qty=10.0, filled_qty=leg1_filled, status=leg1_status,
            ),
            OrderSlice(
                order_id="leg-2", leg_id="leg_1",
                contract_id="NO_AZ", side="sell",
                requested_qty=10.0, filled_qty=leg2_filled, status=leg2_status,
            ),
        ]
        return bundle

    @pytest.mark.asyncio
    async def test_arb_partial_cancels_all_legs(self):
        bundle = self._arb_bundle()
        bundle.unresolved_cycles = 3  # at threshold

        broker = _make_broker(
            get_order_result=_make_order_snapshot(
                status="partially_filled", filled_qty=3.0
            )
        )
        state = _make_state()

        done = await advance_bundle(bundle, broker, state)

        assert done is True
        assert bundle.terminal_action_taken
        # Both legs should be cancelled
        assert broker.cancel_order.await_count == 2
        cancel_ids = {c.args[0] for c in broker.cancel_order.call_args_list}
        assert "leg-1" in cancel_ids
        assert "leg-2" in cancel_ids

    @pytest.mark.asyncio
    async def test_arb_partial_broker_authority_applied(self):
        """After arb unwind, broker positions are fetched and written as truth."""
        bundle = self._arb_bundle()
        bundle.unresolved_cycles = 3
        flat_pos = _make_position("YES_AZ", qty=0.0, side="flat")
        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="partially_filled", filled_qty=3.0),
            positions=[flat_pos],
        )
        state = _make_state()

        await advance_bundle(bundle, broker, state)

        broker.get_positions.assert_awaited()
        state.upsert_position.assert_awaited()

    @pytest.mark.asyncio
    async def test_arb_partial_audit_appended(self):
        bundle = self._arb_bundle()
        bundle.unresolved_cycles = 3
        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="partially_filled", filled_qty=3.0)
        )
        state = _make_state()
        await advance_bundle(bundle, broker, state)
        state.append_audit.assert_awaited_once()
        event = state.append_audit.call_args[0][0]["event"]
        assert event == "arb_partial_unwind"

    @pytest.mark.asyncio
    async def test_arb_fully_filled_does_not_cancel(self):
        """If all legs are fully filled, no cancellation."""
        policy = FillPolicy.for_arb()
        bundle = OrderBundle(
            bundle_id="b-arb-2", strategy_id="arb_az",
            strategy_class=StrategyClass.ARB, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="leg-1", leg_id="leg_0",
                contract_id="YES_AZ", side="buy",
                requested_qty=10.0, filled_qty=10.0, status="filled",
            ),
        ]
        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="filled", filled_qty=10.0)
        )
        state = _make_state()
        done = await advance_bundle(bundle, broker, state)
        assert done is True
        broker.cancel_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Partial-fill policy — market-making: leave resting
# ---------------------------------------------------------------------------

class TestMarketMakingLeaveResting:
    @pytest.mark.asyncio
    async def test_mm_partial_leaves_bundle_tracked(self):
        policy = FillPolicy.for_market_making(cycle_cancel_threshold=1)
        bundle = OrderBundle(
            bundle_id="b-mm-1", strategy_id="mm_az",
            strategy_class=StrategyClass.MARKET_MAKING, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="mm-1", leg_id="leg_0",
                contract_id="YES_AZ", side="buy",
                requested_qty=20.0, filled_qty=5.0, status="partially_filled",
            )
        ]
        bundle.unresolved_cycles = 5  # way past threshold

        broker = _make_broker(
            get_order_result=_make_order_snapshot(
                status="partially_filled", filled_qty=5.0
            )
        )
        state = _make_state()

        # Should return False (keep tracking)
        done = await advance_bundle(bundle, broker, state)
        assert done is False
        broker.cancel_order.assert_not_awaited()
        assert bundle.terminal_action_taken is False

    @pytest.mark.asyncio
    async def test_mm_fully_filled_resolves(self):
        policy = FillPolicy.for_market_making()
        bundle = OrderBundle(
            bundle_id="b-mm-2", strategy_id="mm_az",
            strategy_class=StrategyClass.MARKET_MAKING, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="mm-2", leg_id="leg_0",
                contract_id="YES_AZ", side="buy",
                requested_qty=20.0, filled_qty=20.0, status="filled",
            )
        ]
        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="filled", filled_qty=20.0)
        )
        state = _make_state()
        done = await advance_bundle(bundle, broker, state)
        assert done is True

    @pytest.mark.asyncio
    async def test_mm_timeout_not_cancelled(self):
        """Market-making bundles are exempt from unfilled-timeout sweeps."""
        import datetime as dt

        broker = _make_broker(
            place_order_result=_make_order_snapshot(status="open", filled_qty=0.0)
        )
        state = _make_state()
        policy = FillPolicy.for_market_making(unfilled_timeout_seconds=0.001)
        oms = _make_oms(
            broker=broker, state=state,
            policies={"strat_1": policy},
        )
        await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        await asyncio.sleep(0.01)
        cancelled = await oms.sweep_unfilled_timeouts()
        # MM is exempt — 0 cancelled
        assert cancelled == 0


# ---------------------------------------------------------------------------
# 4. Partial-fill policy — directional: chase
# ---------------------------------------------------------------------------

class TestDirectionalChase:
    @pytest.mark.asyncio
    async def test_directional_chase_requotes_residual(self):
        policy = FillPolicy.for_directional_chase(
            cycle_cancel_threshold=1,
            chase_max_attempts=3,
        )
        bundle = OrderBundle(
            bundle_id="b-chase-1", strategy_id="dir_az",
            strategy_class=StrategyClass.DIRECTIONAL, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="ord-chase-1", leg_id="leg_0",
                contract_id="YES_AZ", side="buy",
                requested_qty=10.0, filled_qty=4.0, status="partially_filled",
                # placement inputs the chase must re-quote with (captured at submit)
                market_id="AZ_GOV", exchange_id="1077", price=0.42, outcome="yes",
            )
        ]
        bundle.unresolved_cycles = 1  # at threshold

        new_order = _make_order_snapshot(order_id="ord-chase-2", status="open")
        broker = _make_broker(
            get_order_result=_make_order_snapshot(
                status="partially_filled", filled_qty=4.0
            ),
        )
        # the chase only re-quotes after the venue CONFIRMS the cancel (else two live orders)
        from sig.broker import OrderResult as _OR
        broker.cancel_order = AsyncMock(return_value=_OR(idempotency_key="", status="canceled",
                                                         order_id="ord-chase-1"))
        # STRICT re-quote: enforce the broker's REAL place_order signature so the regression that
        # crashed the live cycle — place_order(contract_id=…, qty=…, order_type=…, idempotency_key=…) —
        # raises TypeError HERE instead of being silently swallowed by a permissive AsyncMock.
        def _strict_place_order(*, strategy_id, market_id, exchange_id, side, action,
                                quantity, price=None, priority=False, outcome=None):
            return new_order
        broker.place_order = AsyncMock(side_effect=_strict_place_order)
        state = _make_state()

        done = await advance_bundle(bundle, broker, state)

        assert done is False  # still needs tracking (new order open)
        # Old order cancelled
        broker.cancel_order.assert_awaited_with("ord-chase-1")
        # New order placed via the REAL signature for residual qty = 6 (integer contracts)
        kw = broker.place_order.call_args.kwargs
        assert kw["quantity"] == 6
        assert kw["exchange_id"] == "1077"
        assert kw["market_id"].startswith("AZ_GOV#chase1-")    # fresh idempotency scope per chase attempt
        assert kw["side"] == "buy"
        assert kw["outcome"] == "yes"
        assert kw["price"] == pytest.approx(0.425)        # chase ladder: one tick toward liquidity per attempt
        assert kw["strategy_id"] == "dir_az"
        # the buggy kwargs must never reappear (broker owns idempotency; no contract_id/qty/order_type)
        for forbidden in ("contract_id", "qty", "order_type", "idempotency_key"):
            assert forbidden not in kw
        # Slice updated
        assert bundle.slices[0].order_id == "ord-chase-2"
        assert bundle.slices[0].chase_attempts == 1

    @pytest.mark.asyncio
    async def test_directional_chase_exhausted_accepts_partial(self):
        policy = FillPolicy.for_directional_chase(
            cycle_cancel_threshold=1,
            chase_max_attempts=1,
        )
        bundle = OrderBundle(
            bundle_id="b-chase-2", strategy_id="dir_az",
            strategy_class=StrategyClass.DIRECTIONAL, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="ord-chase-x", leg_id="leg_0",
                contract_id="YES_AZ", side="buy",
                requested_qty=10.0, filled_qty=4.0, status="partially_filled",
                chase_attempts=1,  # already at max
            )
        ]
        bundle.unresolved_cycles = 1

        broker = _make_broker(
            get_order_result=_make_order_snapshot(
                status="partially_filled", filled_qty=4.0
            )
        )
        state = _make_state()

        await advance_bundle(bundle, broker, state)

        # Slice should be cancelled (exhausted, fall back to accept-partial)
        broker.cancel_order.assert_awaited()
        # No new order placed
        broker.place_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. Directional accept-partial: actual filled size flows back to state
# ---------------------------------------------------------------------------

class TestDirectionalAcceptPartial:
    @pytest.mark.asyncio
    async def test_accept_partial_fills_written_to_state(self):
        policy = FillPolicy.for_directional_accept(cycle_cancel_threshold=1)
        bundle = OrderBundle(
            bundle_id="b-accept-1", strategy_id="dir_bz",
            strategy_class=StrategyClass.DIRECTIONAL, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="ord-accept-1", leg_id="leg_0",
                contract_id="YES_BZ", side="buy",
                requested_qty=10.0, filled_qty=3.0, status="partially_filled",
            )
        ]
        bundle.unresolved_cycles = 1

        partial_pos = _make_position("YES_BZ", qty=3.0, side="long", avg_entry_price=0.6)
        broker = _make_broker(
            get_order_result=_make_order_snapshot(
                status="partially_filled", filled_qty=3.0
            ),
            positions=[partial_pos],
        )
        state = _make_state()

        done = await advance_bundle(bundle, broker, state)

        assert done is True
        # Broker authority applied — actual qty=3.0 written to state, NOT 10.0
        upsert_calls = state.upsert_position.call_args_list
        assert len(upsert_calls) >= 1
        written = upsert_calls[-1][0][0]
        assert written["qty"] == pytest.approx(3.0)
        assert written["contract_id"] == "YES_BZ"
        # Audit entry confirms partial acceptance
        audit_event = state.append_audit.call_args[0][0]["event"]
        assert audit_event == "directional_accept_partial"

    @pytest.mark.asyncio
    async def test_accept_partial_actual_fills_in_bundle(self):
        policy = FillPolicy.for_directional_accept(cycle_cancel_threshold=1)
        bundle = OrderBundle(
            bundle_id="b-accept-2", strategy_id="dir_bz",
            strategy_class=StrategyClass.DIRECTIONAL, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="ord-a1", leg_id="leg_0",
                contract_id="YES_BZ", side="buy",
                requested_qty=10.0, filled_qty=7.0, status="partially_filled",
            )
        ]
        bundle.unresolved_cycles = 1
        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="partially_filled", filled_qty=7.0)
        )
        state = _make_state()

        await advance_bundle(bundle, broker, state)

        # per_leg_actual_fill reflects reality
        actual = bundle.per_leg_actual_fill()
        assert actual["leg_0"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# 6. Unfilled-order timeout: cancel — NOT abandon-to-human
# ---------------------------------------------------------------------------

class TestUnfilledTimeout:
    @pytest.mark.asyncio
    async def test_unfilled_timeout_cancels_order(self):
        broker = _make_broker(
            place_order_result=_make_order_snapshot(status="open", filled_qty=0.0)
        )
        state = _make_state()
        policy = FillPolicy.for_directional_accept(unfilled_timeout_seconds=0.001)
        oms = _make_oms(
            broker=broker, state=state,
            policies={"strat_1": policy},
        )
        await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        await asyncio.sleep(0.01)
        cancelled = await oms.sweep_unfilled_timeouts()
        assert cancelled >= 1
        broker.cancel_order.assert_awaited()
        cancel_reason = state.update_order_status.call_args[1]["metadata"]["reason"]
        assert cancel_reason == "unfilled_timeout"

    @pytest.mark.asyncio
    async def test_unfilled_timeout_no_human_abandon(self):
        """Verify no 'alert_operator' or 'abandon' path is exercised on timeout."""
        broker = _make_broker(
            place_order_result=_make_order_snapshot(status="open", filled_qty=0.0)
        )
        state = _make_state()
        policy = FillPolicy.for_directional_accept(unfilled_timeout_seconds=0.001)
        oms = _make_oms(broker=broker, state=state, policies={"strat_1": policy})
        await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        await asyncio.sleep(0.01)
        await oms.sweep_unfilled_timeouts()
        # set_halt must NOT be called (no halt on timeout — just cancel)
        state.set_halt.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Cancel/replace doesn't double-place (idempotency)
# ---------------------------------------------------------------------------

class TestCancelReplace:
    @pytest.mark.asyncio
    async def test_replace_cancels_old_and_submits_new(self):
        broker = _make_broker(
            place_order_result=_make_order_snapshot(order_id="ord-orig", status="open"),
        )
        state = _make_state()
        oms = _make_oms(broker=broker, state=state)
        bundle = await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 10.0, "leg_id": "leg_0"}],
        )

        # Now replace with new qty
        new_snap = _make_order_snapshot(order_id="ord-new", status="open", qty=6.0)
        broker.place_order = AsyncMock(return_value=new_snap)

        result = await oms.replace_order(
            bundle_id=bundle.bundle_id,
            leg_id="leg_0",
            new_qty=6.0,
        )

        assert result is not None
        broker.cancel_order.assert_awaited_with("ord-orig")
        place_call = broker.place_order.call_args
        # COLLAPSE: the OMS delegates idempotency to the broker — it no longer mints or passes a key.
        assert "idempotency_key" not in place_call.kwargs
        assert place_call.kwargs["exchange_id"] == "YES_AZ"
        assert place_call.kwargs["quantity"] == 6

    @pytest.mark.asyncio
    async def test_replace_delegates_idempotency_to_broker(self):
        """COLLAPSE: replace no longer mints idempotency keys — the broker owns them. The OMS
        just calls place_order with the real interface (strategy_id/exchange_id, no idempotency_key)."""
        broker = _make_broker(
            place_order_result=_make_order_snapshot(order_id="ord-orig", status="open"),
        )
        state = _make_state()
        oms = _make_oms(broker=broker, state=state)
        bundle = await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 10.0, "leg_id": "leg_0"}],
        )
        snap2 = _make_order_snapshot(order_id="ord-r1", status="open")
        snap3 = _make_order_snapshot(order_id="ord-r2", status="open")
        broker.place_order = AsyncMock(side_effect=[snap2, snap3])

        await oms.replace_order(bundle_id=bundle.bundle_id, leg_id="leg_0", new_qty=6.0)
        await oms.replace_order(bundle_id=bundle.bundle_id, leg_id="leg_0", new_qty=4.0)
        assert len(broker.place_order.call_args_list) == 2
        for call in broker.place_order.call_args_list:
            assert "idempotency_key" not in call.kwargs   # broker owns the key now
            assert "strategy_id" in call.kwargs and "exchange_id" in call.kwargs


# ---------------------------------------------------------------------------
# 8. Reconciliation: detects + corrects mismatch, broker truth wins
# ---------------------------------------------------------------------------

class TestReconciliation:
    @pytest.mark.asyncio
    async def test_reconcile_applies_broker_truth(self):
        # Local believed: qty=0 (flat)
        # Broker says: qty=5, long
        state = _make_state()
        state.read_exposure = AsyncMock(return_value={
            "strat_1": {"qty": 0.0, "side": "flat", "avg_entry_price": 0.0}
        })
        broker_pos = _make_position("YES_AZ", qty=5.0, side="long", avg_entry_price=0.55)
        broker = _make_broker(positions=[broker_pos])

        oms = _make_oms(broker=broker, state=state)
        result = await oms.reconcile(trigger="test")

        assert result.success is False  # significant mismatch → halt
        assert result.hard_locked is True
        # Broker truth still applied to state
        state.upsert_position.assert_awaited()
        upsert_call = state.upsert_position.call_args_list[-1][0][0]
        assert upsert_call["qty"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_reconcile_no_delta_succeeds(self):
        state = _make_state()
        state.read_exposure = AsyncMock(return_value={
            "strat_1": {"qty": 5.0, "side": "long", "avg_entry_price": 0.55}
        })
        broker_pos = _make_position("YES_AZ", qty=5.0, side="long", avg_entry_price=0.55)
        broker = _make_broker(positions=[broker_pos])

        oms = _make_oms(broker=broker, state=state)
        result = await oms.reconcile(trigger="test")

        assert result.success is True
        assert result.hard_locked is False
        assert result.deltas == []

    @pytest.mark.asyncio
    async def test_reconcile_unmapped_symbol_hard_locks(self):
        state = _make_state()
        state.read_exposure = AsyncMock(return_value={})
        # Broker holds a position in a contract we don't know about
        unknown_pos = _make_position("UNKNOWN_CONTRACT", qty=3.0, side="long")
        broker = _make_broker(positions=[unknown_pos])

        oms = _make_oms(broker=broker, state=state)
        result = await oms.reconcile(trigger="test")

        assert result.hard_locked is True
        assert "unmapped_broker_contract" in result.lock_reason
        state.set_halt.assert_awaited()


# ---------------------------------------------------------------------------
# 9. Pre-flight hard-lock on unreconcilable state
# ---------------------------------------------------------------------------

class TestPreFlightReconciliation:
    @pytest.mark.asyncio
    async def test_pre_flight_hard_locks_on_unmapped_symbol(self):
        state = _make_state()
        state.read_exposure = AsyncMock(return_value={})
        broker = _make_broker(
            positions=[_make_position("GHOST_CONTRACT", qty=2.0)]
        )
        result = await run_pre_flight_reconciliation(
            broker=broker,
            state=state,
            strategy_contracts={"strat_1": "YES_AZ"},
        )
        assert result.hard_locked is True
        assert "unmapped_broker_contract" in result.lock_reason
        state.set_halt.assert_awaited_once()
        state.append_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pre_flight_clean_state_succeeds(self):
        state = _make_state()
        state.read_exposure = AsyncMock(return_value={
            "strat_1": {"qty": 0.0, "side": "flat", "avg_entry_price": 0.0}
        })
        broker = _make_broker(positions=[])
        result = await run_pre_flight_reconciliation(
            broker=broker,
            state=state,
            strategy_contracts={"strat_1": "YES_AZ"},
        )
        assert result.success is True
        assert result.hard_locked is False

    @pytest.mark.asyncio
    async def test_oms_startup_blocks_submit_on_hard_lock(self):
        state = _make_state()
        state.read_exposure = AsyncMock(return_value={})
        broker = _make_broker(
            positions=[_make_position("GHOST_CONTRACT", qty=2.0)]
        )
        oms = OrderManagementSystem(
            broker=broker,
            state=state,
            strategy_contracts={"strat_1": "YES_AZ"},
            default_fill_policies={"strat_1": FillPolicy.for_directional_accept()},
        )
        await oms.startup()
        assert oms.is_hard_locked

        with pytest.raises(RuntimeError, match="hard-locked"):
            await oms.submit(
                strategy_id="strat_1",
                legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
            )


# ---------------------------------------------------------------------------
# 10. Entry-guard blocks re-entry on unresolved bundle
# ---------------------------------------------------------------------------

class TestEntryGuard:
    @pytest.mark.asyncio
    async def test_entry_guard_blocks_second_submit(self):
        broker = _make_broker(
            place_order_result=_make_order_snapshot(status="open", filled_qty=0.0)
        )
        state = _make_state()
        oms = _make_oms(broker=broker, state=state)

        # First submit — creates a tracked bundle
        await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        assert await oms.has_pending_bundle("strat_1")

        # Second submit for same strategy should be blocked
        with pytest.raises(RuntimeError, match="pending_bundle_exists"):
            await oms.submit(
                strategy_id="strat_1",
                legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 3.0}],
            )

    @pytest.mark.asyncio
    async def test_entry_guard_allows_after_bundle_resolved(self):
        open_snap = _make_order_snapshot(status="open", filled_qty=0.0)
        filled_snap = _make_order_snapshot(status="filled", filled_qty=5.0, qty=5.0)

        broker = _make_broker(place_order_result=open_snap)
        state = _make_state()
        oms = _make_oms(broker=broker, state=state)

        await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 5.0}],
        )
        assert await oms.has_pending_bundle("strat_1")

        # Simulate fill: get_order now returns filled
        broker.get_order = AsyncMock(return_value=filled_snap)
        await oms.advance_pending_bundles()
        assert not await oms.has_pending_bundle("strat_1")

        # Now submit should succeed
        broker.place_order = AsyncMock(return_value=filled_snap)
        bundle2 = await oms.submit(
            strategy_id="strat_1",
            legs=[{"contract_id": "YES_AZ", "side": "buy", "qty": 3.0}],
        )
        assert bundle2 is not None


# ---------------------------------------------------------------------------
# 11. Arb partial → full unwind (never a naked leg)
# ---------------------------------------------------------------------------

class TestArbAtomicUnwind:
    @pytest.mark.asyncio
    async def test_arb_partial_no_naked_leg_remains(self):
        """
        If leg_0 partially filled and leg_1 open, both must be cancelled.
        After unwind, broker positions must be queried and state updated.
        The bot must never be left with a naked directional position.
        """
        policy = FillPolicy.for_arb(cycle_cancel_threshold=2)
        bundle = OrderBundle(
            bundle_id="b-atomic-1", strategy_id="arb_test",
            strategy_class=StrategyClass.ARB, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="arb-leg-1", leg_id="yes_leg",
                contract_id="YES_AZ", side="buy",
                requested_qty=10.0, filled_qty=7.0, status="partially_filled",
            ),
            OrderSlice(
                order_id="arb-leg-2", leg_id="no_leg",
                contract_id="NO_AZ", side="sell",
                requested_qty=10.0, filled_qty=0.0, status="open",
            ),
        ]
        bundle.unresolved_cycles = 2  # at threshold

        flat_yes = _make_position("YES_AZ", qty=0.0, side="flat")
        flat_no = _make_position("NO_AZ", qty=0.0, side="flat")
        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="partially_filled", filled_qty=7.0),
            positions=[flat_yes, flat_no],
        )
        state = _make_state()

        done = await advance_bundle(bundle, broker, state)

        assert done is True
        assert bundle.terminal_action_taken

        cancelled_ids = {c.args[0] for c in broker.cancel_order.call_args_list}
        assert "arb-leg-1" in cancelled_ids
        assert "arb-leg-2" in cancelled_ids

        # Broker was consulted — positions written as truth (flat)
        broker.get_positions.assert_awaited()
        upsert_calls = [c[0][0] for c in state.upsert_position.call_args_list]
        contracts_written = {c["contract_id"] for c in upsert_calls}
        assert "YES_AZ" in contracts_written
        assert "NO_AZ" in contracts_written

    @pytest.mark.asyncio
    async def test_arb_does_not_accept_partial(self):
        """Arb must NEVER accept partial — it must always unwind."""
        policy = FillPolicy.for_arb(cycle_cancel_threshold=1)
        bundle = OrderBundle(
            bundle_id="b-arb-noaccept", strategy_id="arb_test",
            strategy_class=StrategyClass.ARB, fill_policy=policy,
        )
        bundle.slices = [
            OrderSlice(
                order_id="arb-x", leg_id="l0",
                contract_id="YES_AZ", side="buy",
                requested_qty=5.0, filled_qty=2.0, status="partially_filled",
            )
        ]
        bundle.unresolved_cycles = 1

        broker = _make_broker(
            get_order_result=_make_order_snapshot(status="partially_filled", filled_qty=2.0)
        )
        state = _make_state()

        await advance_bundle(bundle, broker, state)

        # Must have cancelled, not accepted
        broker.cancel_order.assert_awaited()
        # Audit must show arb_partial_unwind, not directional_accept_partial
        audit_event = state.append_audit.call_args[0][0]["event"]
        assert audit_event == "arb_partial_unwind"
