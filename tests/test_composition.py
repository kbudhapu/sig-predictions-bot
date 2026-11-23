"""End-to-end proof: the Orchestrator runs a real cycle wired to the REAL risk-gate, sizer,
allocator, and OMS (SimulatedBroker for the broker) via the composition root.

This exercises the adapter layer: a strategy intent flows SIZING (real Sizer.k_for × Browne) →
RISK GATE (real check_order) → OMS (real manager, broker-owned idempotency) → SimulatedBroker.
"""
import os

import pytest
import yaml

from sig.engine.composition import RiskGateAdapter, build_sim_stack
from sig.engine.protocols import EdgePerf, Intent
from strategies.risk_gate.gate import RiskGate


def _load_config() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class _DirectionalStrategy:
    """A minimal real Strategy (satisfies the orchestrator's Strategy Protocol) that emits one
    directional buy intent carrying a confidence (sigma_p) the real sizer consumes."""
    name = "market_making"
    tier = "CORE"
    cadence_s = 0.0

    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.fills: list = []

    def evaluate(self, state):
        self.evaluate_calls += 1
        return [Intent(
            strategy_id="market_making", exchange_id="YES_TEST", side="buy", action="open",
            strategy_type="directional", signal_strength=1.0, price=0.5,
            metadata={"sigma_p": 0.06},
        )]

    def on_fill(self, fill):
        self.fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf(realized_pnl=0.0, n_trades=0)


@pytest.mark.asyncio
async def test_real_module_cycle_end_to_end():
    cfg = _load_config()
    orch, broker, allocator, state, oms = build_sim_stack(
        config=cfg,
        strategy_contracts={"market_making": "YES_TEST"},
        buying_power=100_000.0, equity=100_000.0,
    )
    strat = _DirectionalStrategy()
    orch.register_strategy(strat)

    # startup gates (SIM): credential/eligibility skipped; OMS pre-flight reconciliation runs against
    # the SimulatedBroker (flat venue) → not hard-locked.
    ok = await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    assert ok is True
    assert oms.is_hard_locked is False

    # per-cycle allocator refresh (real propose() → per-edge budgets)
    await allocator.refresh()
    assert allocator.budget_for("market_making") > 0.0        # real allocator gave a real budget

    # ONE real cycle: intent → real sizing → real gate → OMS → SimulatedBroker
    await orch._run_cycle()

    assert strat.evaluate_calls == 1                           # strategy ran
    assert len(broker.placed) == 1                             # order reached the broker
    placed = broker.placed[0]
    assert placed["strategy_id"] == "market_making"
    assert placed["exchange_id"] == "YES_TEST"
    assert placed["quantity"] > 0                              # real sizer produced a positive size

    # idempotency is BROKER-OWNED end-to-end: the OMS recorded the key the broker minted (prefix "sig-").
    recorded = list(state._orders.values())
    assert recorded and str(recorded[0]["idempotency_key"]).startswith("sig-")


def test_risk_gate_adapter_balance_tracks_live_nav():
    """The gate's fraction caps size off live NAV (equity = free cash + marked positions) when a
    nav_source is wired, not the frozen cash-only build seed — this is what lifts the phantom
    half-sized deploy cap. Absent/non-positive NAV safely falls back to the seed."""
    cfg = _load_config()
    nav = {"v": None}
    adapter = RiskGateAdapter(RiskGate(config=cfg), balance=54_000.0, nav_source=lambda: nav["v"])
    assert adapter._portfolio_state()["balance"] == pytest.approx(54_000.0)   # None → seed
    nav["v"] = 105_000.0
    assert adapter._portfolio_state()["balance"] == pytest.approx(105_000.0)  # live NAV wins
    nav["v"] = 0.0
    assert adapter._portfolio_state()["balance"] == pytest.approx(54_000.0)   # non-positive → seed


def test_risk_gate_adapter_nav_source_error_falls_back_to_seed():
    cfg = _load_config()

    def boom():
        raise RuntimeError("nav read failed")

    adapter = RiskGateAdapter(RiskGate(config=cfg), balance=54_000.0, nav_source=boom)
    assert adapter._portfolio_state()["balance"] == pytest.approx(54_000.0)


def test_risk_gate_adapter_defaults_to_static_balance_without_nav_source():
    cfg = _load_config()
    adapter = RiskGateAdapter(RiskGate(config=cfg), balance=77_000.0)   # SIM / tests: no nav_source
    assert adapter._portfolio_state()["balance"] == pytest.approx(77_000.0)


@pytest.mark.asyncio
async def test_real_gate_rejects_sell_without_holding():
    """The REAL risk gate is actually in the path: a sell with no holding is rejected (not placed)."""
    cfg = _load_config()
    orch, broker, allocator, state, oms = build_sim_stack(
        config=cfg, strategy_contracts={"market_making": "YES_TEST"},
    )

    class _SellStrategy(_DirectionalStrategy):
        def evaluate(self, state):
            self.evaluate_calls += 1
            return [Intent(strategy_id="market_making", exchange_id="YES_TEST", side="sell",
                           action="close", strategy_type="directional", signal_strength=1.0,
                           price=0.5, metadata={"sigma_p": 0.06})]

    orch.register_strategy(_SellStrategy())
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await allocator.refresh()
    await orch._run_cycle()
    assert len(broker.placed) == 0                             # real gate rejected the naked sell
