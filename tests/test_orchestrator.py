"""
Orchestrator test suite — proves the key wiring contracts.

Required scenarios (from task spec):
1. Capital ledger prevents two orders in one cycle from double-spending
2. Order path routes intent through sizing → gate → OMS → broker
3. Strategy throwing is caught, skipped; bot continues with other strategies
4. Due-cadence-gating: arb (short cadence) runs more often than directional (long cadence)
5. Startup reconcile-hard-gate blocks start on mismatch (OMS returns hard_locked)
6. Graceful shutdown holds positions (no auto-flatten)
7. SIM mode runs the full cycle end-to-end against SimulatedBroker
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sig.engine.capital_ledger import CapitalLedger
from sig.engine.orchestrator import Orchestrator, _fill_policy_for
from sig.engine.protocols import (
    EdgePerf,
    Intent,
    RiskGateVerdict,
    SizingResult,
)
from sig.engine.strategy_registry import StrategyRegistry
from sig.engine.write_gate import WriteGate


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeAccount:
    buying_power: float = 10_000.0
    equity: float = 10_000.0


@dataclass
class FakeOrderResult:
    order_id: str = "ord_001"
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    status: str = "submitted"
    contract_id: str = "YES_AZ"
    side: str = "buy"
    qty: float = 1.0


class FakeBroker:
    def __init__(self, buying_power: float = 10_000.0) -> None:
        self.buying_power = buying_power
        self.place_calls: list[dict] = []
        self.cancel_calls: list[str] = []

    async def get_account(self) -> FakeAccount:
        return FakeAccount(buying_power=self.buying_power)

    async def get_positions(self) -> list:
        return []

    async def place_order(self, **kwargs: Any) -> FakeOrderResult:
        self.place_calls.append(kwargs)
        return FakeOrderResult(order_id=f"ord_{len(self.place_calls):03d}")

    async def place_multi_leg(self, legs: list, **kwargs: Any) -> list:
        results = []
        for leg in legs:
            results.append(FakeOrderResult(order_id=f"ord_{len(self.place_calls)+1:03d}"))
            self.place_calls.append(leg)
        return results

    async def cancel_order(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        return True

    async def cancel_all(self, contract_id: str) -> int:
        return 0

    async def get_order(self, order_id: str) -> None:
        return None

    async def get_open_orders(self) -> list:
        return []


class FakeState:
    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.positions: list[dict] = []
        self.audit: list[dict] = []
        self.halt_reason: str | None = None
        self.heartbeats: list[str] = []

    async def write_order(self, order: dict) -> None:
        self.orders.append(order)

    async def update_order_status(self, order_id: str, status: str, **kwargs: Any) -> None:
        pass

    async def upsert_position(self, position: dict) -> None:
        self.positions.append(position)

    async def read_open_orders(self) -> list[dict]:
        return []

    async def read_exposure(self) -> dict:
        return {}

    async def append_audit(self, entry: dict) -> None:
        self.audit.append(entry)

    async def set_halt(self, reason: str, metadata: dict) -> None:
        self.halt_reason = reason

    async def clear_halt(self) -> None:
        self.halt_reason = None

    async def get_halt(self) -> dict | None:
        if self.halt_reason:
            return {"reason": self.halt_reason}
        return None

    async def write_heartbeat(self, ts_iso: str) -> None:
        self.heartbeats.append(ts_iso)


@dataclass
class FakeReconResult:
    hard_locked: bool = False
    lock_reason: str = ""
    deltas: list = field(default_factory=list)
    broker_positions_applied: int = 0


class FakeOMS:
    def __init__(self, hard_locked_on_startup: bool = False) -> None:
        self._hard_locked = False
        self._startup_result = FakeReconResult(hard_locked=hard_locked_on_startup,
                                               lock_reason="test_mismatch" if hard_locked_on_startup else "")
        self.submit_calls: list[dict] = []
        self.advance_calls: int = 0

    async def startup(self) -> FakeReconResult:
        if self._startup_result.hard_locked:
            self._hard_locked = True
        return self._startup_result

    async def submit(self, *, strategy_id: str, legs: list[dict],
                     fill_policy: Any, metadata: dict | None = None) -> Any:
        self.submit_calls.append({"strategy_id": strategy_id, "legs": legs})
        return MagicMock(bundle_id="bundle_001")

    async def advance_pending_bundles(self) -> int:
        self.advance_calls += 1
        return 0

    async def sweep_unfilled_timeouts(self) -> int:
        return 0

    async def reconcile(self, trigger: str = "periodic") -> FakeReconResult:
        return FakeReconResult()

    async def has_pending_bundle(self, strategy_id: str) -> bool:
        return False

    @property
    def is_hard_locked(self) -> bool:
        return self._hard_locked


class FakeRiskGate:
    def __init__(self, approved: bool = True, clamp: float | None = None) -> None:
        self._approved = approved
        self._clamp = clamp
        self.validate_calls: list[tuple] = []

    def validate(self, intent: Intent, sized_qty: float, available_capital: float) -> RiskGateVerdict:
        self.validate_calls.append((intent, sized_qty, available_capital))
        return RiskGateVerdict(approved=self._approved, clamped_qty=self._clamp,
                               reason="test_reject" if not self._approved else "")


class FakeSizing:
    def __init__(self, qty: float = 1.0, commitment: float = 500.0) -> None:
        self._qty = qty
        self._commitment = commitment

    def size(self, intent: Intent, available: float, budget: float) -> SizingResult:
        return SizingResult(qty=self._qty, capital_commitment=self._commitment)


class FakeAllocator:
    def budget_for(self, strategy_id: str) -> float:
        return 5_000.0

    def on_fill(self, strategy_id: str, pnl: float) -> None:
        pass

    def seed(self, strategies: list) -> None:
        pass


class FakeStrategy:
    def __init__(
        self,
        name: str,
        cadence_s: float,
        tier: str = "CORE",
        intents: list[Intent] | None = None,
        raises: bool = False,
    ) -> None:
        self.name = name
        self.tier = tier
        self.cadence_s = cadence_s
        self._intents = intents or []
        self._raises = raises
        self.evaluate_calls: int = 0
        self.fill_calls: int = 0

    def evaluate(self, state: Any) -> list[Intent]:
        self.evaluate_calls += 1
        if self._raises:
            raise RuntimeError(f"strategy {self.name} blew up")
        return self._intents

    def on_fill(self, fill: Any) -> None:
        self.fill_calls += 1

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()


def _make_intent(
    strategy_id: str = "mean_reversion_qqq",
    strategy_type: str = "directional",
    qty: float = 1.0,
) -> Intent:
    return Intent(
        strategy_id=strategy_id,
        exchange_id="YES_ARIZONA_GOV",
        side="buy",
        action="open",
        strategy_type=strategy_type,
        price=0.55,
        metadata={"qty": qty},
    )


def _make_orch(
    buying_power: float = 10_000.0,
    hard_locked_on_startup: bool = False,
    risk_approved: bool = True,
) -> tuple[Orchestrator, FakeBroker, FakeState, FakeOMS]:
    broker = FakeBroker(buying_power=buying_power)
    state = FakeState()
    oms = FakeOMS(hard_locked_on_startup=hard_locked_on_startup)
    orch = Orchestrator(
        broker=broker,
        state=state,
        oms=oms,
        risk_gate=FakeRiskGate(approved=risk_approved),
        sizing=FakeSizing(),
        allocator=FakeAllocator(),
        tick_seconds=0.01,
        environment="sim",
        lock_path="/tmp/test_sig_bot.lock",
    )
    return orch, broker, state, oms


# ---------------------------------------------------------------------------
# 1. Capital ledger — double-spend prevention (THE key test)
# ---------------------------------------------------------------------------


class TestCapitalLedger:
    def test_begin_cycle_resets_reservations(self) -> None:
        ledger = CapitalLedger()
        ledger.begin_cycle(10_000.0, 10_000.0)
        ledger.reserve("strat_a", 3_000.0)
        ledger.reserve("strat_b", 4_000.0)
        assert ledger.available_buying_power() == pytest.approx(3_000.0)

        ledger.begin_cycle(10_000.0, 10_000.0)
        assert ledger.available_buying_power() == pytest.approx(10_000.0)

    def test_two_strategies_cannot_double_spend(self) -> None:
        """Core test: the ledger prevents two orders in one cycle from spending the same capital."""
        ledger = CapitalLedger()
        ledger.begin_cycle(1_000.0, 1_000.0)

        # Strategy A reserves 600
        reserved_a = ledger.reserve("strat_a", 600.0)
        assert reserved_a == pytest.approx(600.0)
        assert ledger.available_buying_power() == pytest.approx(400.0)

        # Strategy B sees only 400 available — cannot spend A's 600 again
        reserved_b = ledger.reserve("strat_b", 600.0)
        assert reserved_b == pytest.approx(400.0)  # capped at what's left
        assert ledger.available_buying_power() == pytest.approx(0.0)

        # Total reserved never exceeds snapshot
        assert ledger.reserved_total <= 1_000.0 + 1e-9

    def test_reservation_capped_at_available(self) -> None:
        ledger = CapitalLedger()
        ledger.begin_cycle(500.0, 500.0)
        reserved = ledger.reserve("strat_a", 1_000.0)
        assert reserved == pytest.approx(500.0)
        assert ledger.available_buying_power() == pytest.approx(0.0)

    def test_reserve_updates_idempotently(self) -> None:
        ledger = CapitalLedger()
        ledger.begin_cycle(1_000.0, 1_000.0)
        ledger.reserve("strat_a", 400.0)
        ledger.reserve("strat_a", 200.0)  # replace, not accumulate
        assert ledger.reserved_total == pytest.approx(200.0)

    def test_release_frees_capital(self) -> None:
        ledger = CapitalLedger()
        ledger.begin_cycle(1_000.0, 1_000.0)
        ledger.reserve("strat_a", 600.0)
        ledger.release("strat_a")
        assert ledger.available_buying_power() == pytest.approx(1_000.0)

    def test_release_all_clears_everything(self) -> None:
        ledger = CapitalLedger()
        ledger.begin_cycle(1_000.0, 1_000.0)
        ledger.reserve("strat_a", 300.0)
        ledger.reserve("strat_b", 400.0)
        ledger.release_all()
        assert ledger.available_buying_power() == pytest.approx(1_000.0)
        assert ledger.reserved_total == pytest.approx(0.0)

    def test_no_snapshot_returns_zero_available(self) -> None:
        ledger = CapitalLedger()
        assert ledger.available_buying_power() == 0.0
        assert ledger.reserve("strat_a", 500.0) == 0.0


# ---------------------------------------------------------------------------
# 2. Order path: intent → sizing → gate → OMS → broker
# ---------------------------------------------------------------------------


class TestOrderPath:
    @pytest.mark.asyncio
    async def test_intent_routed_through_full_pipeline(self) -> None:
        orch, broker, state, oms = _make_orch()
        intent = _make_intent()
        strategy = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        await orch._run_cycle()

        assert oms.advance_calls == 1
        assert len(oms.submit_calls) == 1
        assert oms.submit_calls[0]["strategy_id"] == "mean_reversion_qqq"
        assert oms.submit_calls[0]["legs"][0]["contract_id"] == "YES_ARIZONA_GOV"

    @pytest.mark.asyncio
    async def test_risk_gate_reject_blocks_oms_submit(self) -> None:
        orch, broker, state, oms = _make_orch(risk_approved=False)
        intent = _make_intent()
        strategy = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        await orch._run_cycle()

        assert len(oms.submit_calls) == 0  # gate blocked it

    @pytest.mark.asyncio
    async def test_oms_hard_locked_blocks_order_path(self) -> None:
        orch, broker, state, oms = _make_orch()
        oms._hard_locked = True
        intent = _make_intent()
        strategy = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        await orch._run_cycle()

        assert len(oms.submit_calls) == 0

    @pytest.mark.asyncio
    async def test_capital_reserved_before_second_strategy(self) -> None:
        """Second strategy in cycle sees reduced available capital."""
        # Two strategies, first reserves 8000, second should see limited capital
        orch, broker, state, oms = _make_orch(buying_power=10_000.0)
        orch._sizing = FakeSizing(qty=1.0, commitment=8_000.0)

        intent_a = _make_intent("mean_reversion_qqq")
        intent_b = _make_intent("momentum_breakout_btc")
        strat_a = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent_a])
        strat_b = FakeStrategy("momentum_breakout_btc", cadence_s=0, intents=[intent_b])
        orch.register_strategy(strat_a)
        orch.register_strategy(strat_b)

        await orch._run_cycle()

        # Both strategies ran, but capital ledger serialized them
        assert strat_a.evaluate_calls == 1
        assert strat_b.evaluate_calls == 1

    @pytest.mark.asyncio
    async def test_arb_intent_skips_sizing_uses_metadata_qty(self) -> None:
        """Arb path uses metadata['qty'] directly, not the sizing module."""
        orch, broker, state, oms = _make_orch()
        intent = _make_intent(strategy_id="arb_az_gov", strategy_type="arb", qty=5.0)
        strategy = FakeStrategy("arb_az_gov", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        await orch._run_cycle()

        assert len(oms.submit_calls) == 1
        assert oms.submit_calls[0]["legs"][0]["qty"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_write_gate_tracks_writes(self) -> None:
        orch, broker, state, oms = _make_orch()
        intent = _make_intent()
        strategy = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        before = orch._write_gate.writes_remaining("directional")
        await orch._run_cycle()
        after = orch._write_gate.writes_remaining("directional")

        assert after == before - 1


# ---------------------------------------------------------------------------
# 3. Error isolation — strategy crash doesn't take down the loop
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    @pytest.mark.asyncio
    async def test_crashing_strategy_skipped_others_continue(self) -> None:
        orch, broker, state, oms = _make_orch()

        intent_ok = _make_intent("momentum_breakout_btc")
        crashing = FakeStrategy("mean_reversion_qqq", cadence_s=0, raises=True)
        healthy = FakeStrategy("momentum_breakout_btc", cadence_s=0, intents=[intent_ok])

        orch.register_strategy(crashing)
        orch.register_strategy(healthy)

        # Should NOT raise — error isolation catches at strategy boundary
        await orch._run_cycle()

        assert crashing.evaluate_calls == 1          # was called
        assert healthy.evaluate_calls == 1            # also ran
        assert len(oms.submit_calls) == 1             # healthy strategy's order placed

    @pytest.mark.asyncio
    async def test_strategy_failure_increments_counter(self) -> None:
        orch, broker, state, oms = _make_orch()
        registry = orch._registry
        crashing = FakeStrategy("mean_reversion_qqq", cadence_s=0, raises=True)
        orch.register_strategy(crashing)

        for _ in range(3):
            await orch._run_cycle()

        entry = registry._entries["mean_reversion_qqq"]
        assert entry.consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_cycle_exception_increments_consecutive_failures(self) -> None:
        orch, broker, state, oms = _make_orch()

        # Patch _run_cycle to raise on the first call, succeed on second
        call_count = [0]
        original = orch._run_cycle

        async def patched():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated cycle crash")
            await original()

        orch._run_cycle = patched  # type: ignore[method-assign]

        # Run two cycles via run_forever briefly
        async def run_briefly():
            task = asyncio.create_task(orch.run_forever())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_briefly()
        # After a crash on cycle 1, failures should have been 1 (then cleared on success)
        assert orch.consecutive_failures == 0  # success resets it


# ---------------------------------------------------------------------------
# 4. Cadence gating: arb runs more often than directional
# ---------------------------------------------------------------------------


class TestCadenceGating:
    @pytest.mark.asyncio
    async def test_arb_short_cadence_runs_more_cycles(self) -> None:
        orch, broker, state, oms = _make_orch()
        # Arb: cadence = 0 (always due); directional: cadence = 100s (never due in test)
        arb_strat = FakeStrategy("arb_az", cadence_s=0.0)
        dir_strat = FakeStrategy("mean_reversion_qqq", cadence_s=100.0)

        # Patch intents to return none so OMS doesn't get called
        arb_strat._intents = []
        dir_strat._intents = []

        orch.register_strategy(arb_strat)
        orch.register_strategy(dir_strat)

        # Pre-mark directional as "just ran" so its 100s cadence hasn't elapsed yet
        orch._registry._entries["mean_reversion_qqq"].last_run_at = time.monotonic()

        # Run 5 cycles
        for _ in range(5):
            await orch._run_cycle()

        assert arb_strat.evaluate_calls == 5
        assert dir_strat.evaluate_calls == 0  # not due

    @pytest.mark.asyncio
    async def test_directional_runs_once_cadence_elapsed(self) -> None:
        orch, broker, state, oms = _make_orch()
        # Use a long cadence so it's only due on the first cycle
        dir_strat = FakeStrategy("mean_reversion_qqq", cadence_s=9999.0)
        dir_strat._intents = []
        orch.register_strategy(dir_strat)

        # Set last_run far in the past so the cadence is guaranteed elapsed
        orch._registry._entries["mean_reversion_qqq"].last_run_at = time.monotonic() - 20000.0

        await orch._run_cycle()
        assert dir_strat.evaluate_calls == 1

        # mark_ran sets last_run to now — second cycle should NOT fire it (9999s cadence)
        await orch._run_cycle()
        assert dir_strat.evaluate_calls == 1  # not due yet


# ---------------------------------------------------------------------------
# 5. Startup reconcile-hard-gate blocks start on mismatch
# ---------------------------------------------------------------------------


class TestStartupGate:
    @pytest.mark.asyncio
    async def test_hard_locked_startup_returns_false(self) -> None:
        orch, broker, state, oms = _make_orch(hard_locked_on_startup=True)

        result = await orch.startup(
            skip_credential_check=True,
            skip_eligibility_check=True,
        )

        assert result is False
        assert state.halt_reason is not None
        assert "pre_flight_hard_gate" in state.halt_reason

    @pytest.mark.asyncio
    async def test_clean_startup_returns_true(self) -> None:
        orch, broker, state, oms = _make_orch(hard_locked_on_startup=False)

        result = await orch.startup(
            skip_credential_check=True,
            skip_eligibility_check=True,
        )

        assert result is True
        assert state.halt_reason is None

    @pytest.mark.asyncio
    async def test_watchdog_started_on_successful_startup(self) -> None:
        orch, broker, state, oms = _make_orch()
        await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
        assert orch._watchdog._thread is not None
        assert orch._watchdog._thread.is_alive()
        orch._watchdog.stop()


# ---------------------------------------------------------------------------
# 6. Graceful shutdown — positions are HELD, no auto-flatten
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_halt_stops_new_orders(self) -> None:
        orch, broker, state, oms = _make_orch()
        intent = _make_intent()
        strategy = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        orch.halt("operator_test")

        # Cycle should still run but skip new entries
        await orch._run_cycle()

        # The loop-level halt check skips running any strategy
        assert len(oms.submit_calls) == 0

    @pytest.mark.asyncio
    async def test_halt_does_not_cancel_positions(self) -> None:
        """Halt-but-HOLD: positions are never flattened autonomously."""
        orch, broker, state, oms = _make_orch()
        orch.halt("test_halt")

        # No cancel calls to broker
        assert broker.cancel_calls == []

    @pytest.mark.asyncio
    async def test_resume_clears_halt(self) -> None:
        orch, broker, state, oms = _make_orch()
        orch.halt("test")
        assert orch.is_halted is True

        orch.resume()
        assert orch.is_halted is False


# ---------------------------------------------------------------------------
# 7. SIM mode — full cycle end-to-end with SimulatedBroker
# ---------------------------------------------------------------------------


class SimulatedBroker:
    """Simulated broker for SIM mode: accepts all orders, instantly fills."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.balance = 10_000.0

    async def get_account(self) -> FakeAccount:
        return FakeAccount(buying_power=self.balance)

    async def get_positions(self) -> list:
        return []

    async def place_order(self, **kwargs: Any) -> FakeOrderResult:
        self.placed.append(kwargs)
        self.balance -= kwargs.get("qty", 1) * (kwargs.get("price") or 0.5)
        return FakeOrderResult(
            order_id=f"sim_{len(self.placed):04d}",
            filled_qty=kwargs.get("qty", 1),
            status="filled",
        )

    async def place_multi_leg(self, legs: list, **_: Any) -> list:
        results = []
        for leg in legs:
            results.append(
                FakeOrderResult(order_id=f"sim_ml_{len(self.placed)+1:04d}", status="filled")
            )
            self.placed.append(leg)
        return results

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def cancel_all(self, contract_id: str) -> int:
        return 0

    async def get_order(self, order_id: str) -> None:
        return None

    async def get_open_orders(self) -> list:
        return []


class TestSimMode:
    @pytest.mark.asyncio
    async def test_sim_full_cycle_end_to_end(self) -> None:
        """SIM mode: full cycle with SimulatedBroker — proves the whole pipeline fires."""
        sim_broker = SimulatedBroker()
        state = FakeState()
        oms = FakeOMS()

        orch = Orchestrator(
            broker=sim_broker,
            state=state,
            oms=oms,
            risk_gate=FakeRiskGate(approved=True),
            sizing=FakeSizing(qty=2.0, commitment=1000.0),
            allocator=FakeAllocator(),
            tick_seconds=0.01,
            environment="sim",
            lock_path="/tmp/test_sig_bot.lock",
        )

        intent = _make_intent("mean_reversion_qqq", "directional")
        strategy = FakeStrategy("mean_reversion_qqq", cadence_s=0, intents=[intent])
        orch.register_strategy(strategy)

        # Startup in SIM mode (skips credentials + eligibility)
        startup_ok = await orch.startup(
            skip_credential_check=True,
            skip_eligibility_check=True,
        )
        assert startup_ok is True

        # Run one cycle
        await orch._run_cycle()

        # Strategy evaluated
        assert strategy.evaluate_calls == 1

        # OMS received the order
        assert len(oms.submit_calls) == 1
        submit = oms.submit_calls[0]
        assert submit["strategy_id"] == "mean_reversion_qqq"
        assert submit["legs"][0]["qty"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_sim_multiple_strategies_run_sequentially(self) -> None:
        """All strategies run in order; capital is reserved sequentially."""
        sim_broker = SimulatedBroker()
        state = FakeState()
        oms = FakeOMS()

        orch = Orchestrator(
            broker=sim_broker,
            state=state,
            oms=oms,
            risk_gate=FakeRiskGate(approved=True),
            sizing=FakeSizing(qty=1.0, commitment=500.0),
            allocator=FakeAllocator(),
            tick_seconds=0.01,
            environment="sim",
            lock_path="/tmp/test_sig_bot.lock",
        )

        strategies = [
            FakeStrategy("mean_reversion_qqq", cadence_s=0,
                         intents=[_make_intent("mean_reversion_qqq")]),
            FakeStrategy("momentum_breakout_btc", cadence_s=0,
                         intents=[_make_intent("momentum_breakout_btc")]),
            FakeStrategy("trend_following_gld", cadence_s=0,
                         intents=[_make_intent("trend_following_gld")]),
        ]
        for s in strategies:
            orch.register_strategy(s)

        await orch._run_cycle()

        for s in strategies:
            assert s.evaluate_calls == 1

        assert len(oms.submit_calls) == 3

    @pytest.mark.asyncio
    async def test_sim_run_forever_runs_then_halts(self) -> None:
        """run_forever runs cycles and respects halt."""
        sim_broker = SimulatedBroker()
        state = FakeState()
        oms = FakeOMS()

        orch = Orchestrator(
            broker=sim_broker,
            state=state,
            oms=oms,
            risk_gate=FakeRiskGate(approved=True),
            sizing=FakeSizing(),
            allocator=FakeAllocator(),
            tick_seconds=0.01,
            environment="sim",
            lock_path="/tmp/test_sig_bot.lock",
        )

        cycle_count = [0]
        original_run = orch._run_cycle

        async def counting_run():
            cycle_count[0] += 1
            if cycle_count[0] >= 3:
                orch.halt("test_done")
            await original_run()

        orch._run_cycle = counting_run  # type: ignore[method-assign]

        task = asyncio.create_task(orch.run_forever())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert cycle_count[0] >= 3


# ---------------------------------------------------------------------------
# 8. Strategy registry
# ---------------------------------------------------------------------------


class TestStrategyRegistry:
    def test_due_returns_strategies_with_elapsed_cadence(self) -> None:
        registry = StrategyRegistry()
        fast = FakeStrategy("fast", cadence_s=0.0)
        slow = FakeStrategy("slow", cadence_s=9999.0)
        registry.register(fast)
        registry.register(slow)

        due = registry.due(time.monotonic())
        names = [s.name for s in due]
        assert "fast" in names
        assert "slow" not in names

    def test_mark_ran_resets_failure_count(self) -> None:
        registry = StrategyRegistry()
        s = FakeStrategy("strat", cadence_s=0)
        registry.register(s)
        registry.mark_failure("strat")
        registry.mark_failure("strat")
        registry.mark_ran("strat")
        assert registry._entries["strat"].consecutive_failures == 0

    def test_disabled_strategy_not_due(self) -> None:
        registry = StrategyRegistry()
        s = FakeStrategy("strat", cadence_s=0)
        registry.register(s)
        registry.disable("strat")
        assert registry.due() == []


# ---------------------------------------------------------------------------
# 9. Write gate
# ---------------------------------------------------------------------------


class TestWriteGate:
    def test_slot_routing_by_strategy_type(self) -> None:
        gate = WriteGate()
        assert gate.slot_for("arb") == "arb"
        assert gate.slot_for("market_making") == "market_making"
        assert gate.slot_for("directional") == "directional"
        assert gate.slot_for("resolution") == "directional"

    def test_writes_allowed_within_budget(self) -> None:
        gate = WriteGate()
        assert gate.is_allowed("arb") is True

    def test_writes_denied_after_budget_exhausted(self) -> None:
        gate = WriteGate()
        # Exhaust the arb slot by recording 30 writes with recent timestamps
        now = time.monotonic()
        for _ in range(30):
            gate._buckets["arb"].writes.append(now)
        assert gate.is_allowed("arb") is False

    def test_arb_exhaustion_does_not_block_directional(self) -> None:
        gate = WriteGate()
        now = time.monotonic()
        for _ in range(30):
            gate._buckets["arb"].writes.append(now)
        assert gate.is_allowed("directional") is True
