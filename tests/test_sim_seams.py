"""SIM tests for the two mechanical-floor seams closed in the orchestrator:
  SEAM 1 — atomic MULTI-LEG arb intent routed through the orchestrator (all-fill-or-unwind, no naked
           leg; total reserved once against the ledger; capture-fillable sizing, not Browne).
  SEAM 2 — feed-stale halt wired into the cycle (global halt-but-hold; per-market skip; auto-recover).
"""
import os

import pytest
import yaml

from sig.engine.composition import build_sim_stack, SimulatedBroker
from sig.engine.protocols import Intent, EdgePerf
from sig.oms.models import FillPolicy


def _cfg():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class _Arb:
    """Emits one ATOMIC 2-leg arb intent (capture-fillable quantities set by the strategy)."""
    tier = "CORE"
    cadence_s = 0.0

    def __init__(self, *, name="arb", sid="arb", qty=10, price=0.5, pair="AZ"):
        self.name = name
        self._sid, self._qty, self._price, self._pair = sid, qty, price, pair
        self.evaluate_calls = 0
        self.fills = []

    def evaluate(self, state):
        self.evaluate_calls += 1
        legs = [{"exchange_id": f"YES_{self._pair}_D", "side": "buy", "action": "open",
                 "price": self._price, "quantity": self._qty},
                {"exchange_id": f"YES_{self._pair}_R", "side": "buy", "action": "open",
                 "price": self._price, "quantity": self._qty}]
        return [Intent(strategy_id=self._sid, exchange_id=f"ARB_{self._pair}", side="buy",
                       action="open", strategy_type="arb", price=self._price, metadata={}, legs=legs)]

    def on_fill(self, f):
        self.fills.append(f)

    def realized_performance(self):
        return EdgePerf()


class _Dir:
    tier = "CORE"
    cadence_s = 0.0

    def __init__(self, name, *, sid):
        self.name = name
        self._sid = sid
        self.evaluate_calls = 0
        self.fills = []

    def evaluate(self, state):
        self.evaluate_calls += 1
        return [Intent(strategy_id=self._sid, exchange_id=f"YES_{self.name.upper()}", side="buy",
                       action="open", strategy_type="directional", price=0.5,
                       metadata={"sigma_p": 0.06})]

    def on_fill(self, f):
        self.fills.append(f)

    def realized_performance(self):
        return EdgePerf()


class _PartialMultiBroker(SimulatedBroker):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancelled = []

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return await super().cancel_order(order_id)

    async def place_multi_leg(self, *, strategy_id, market_id, legs, priority=True):
        res = await super().place_multi_leg(strategy_id=strategy_id, market_id=market_id,
                                            legs=legs, priority=priority)
        if len(res.legs) >= 2:                       # leg 1 does NOT fill → partial arb
            res.legs[1]["status"] = "rejected"
            res.legs[1]["filled_qty"] = 0.0
            self._orders[res.legs[1]["order_id"]] = {"status": "rejected", "filled_qty": 0.0,
                                                     "avg_price": 0.0}
        return res


# ═══ SEAM 1 (a) — multi-leg arb fills ATOMICALLY through the orchestrator ═══════
@pytest.mark.asyncio
async def test_a_multileg_arb_atomic_fill_through_orchestrator():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb": "YES_AZ_D"})
    orch.register_strategy(_Arb(qty=10, price=0.5))
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await orch._run_cycle()
    assert len(broker.placed) == 2                   # BOTH legs placed in one atomic submit
    # capture-fillable: quantities are the arb strategy's, NOT the Browne sizer's
    assert all(p["quantity"] == 10 for p in broker.placed)
    assert {p["exchange_id"] for p in broker.placed} == {"YES_AZ_D", "YES_AZ_R"}
    assert await oms.pending_bundle_count() == 0     # both filled → resolved


# ═══ SEAM 1 (b) — PARTIAL multi-leg arb fully UNWINDS (no naked leg) ════════════
@pytest.mark.asyncio
async def test_b_multileg_arb_partial_unwinds_no_naked_leg():
    broker = _PartialMultiBroker()
    orch, _b, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb": "YES_AZ_D"}, broker=broker)
    orch.register_strategy(_Arb(qty=10, price=0.5))
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await orch._run_cycle()                          # submits the atomic bundle (one leg won't fill)
    assert await oms.pending_bundle_count() == 1     # tracked (partial)
    for _ in range(5):                               # past cycle_cancel_threshold → arb UNWIND_ALL_LEGS
        await oms.advance_pending_bundles()
    assert await oms.pending_bundle_count() == 0     # resolved — NO naked arb leg left pending


# ═══ SEAM 1 (c) — ledger reserves the multi-leg TOTAL atomically (no double-spend) ═
@pytest.mark.asyncio
async def test_c_multileg_total_reserved_atomically_no_double_spend():
    # Each arb bundle costs 2 × 10 × 0.5 = 10. buying_power = 12 → the FIRST bundle fits (reserves 10),
    # the SECOND cannot (only 2 left) and must place NOTHING (atomic — never a partial/naked arb).
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb": "YES_AZ_D", "arb2": "YES_TX_D"},
        buying_power=12.0, equity=100_000.0)
    orch.register_strategy(_Arb(name="arb_az", sid="arb", pair="AZ", qty=10, price=0.5))
    orch.register_strategy(_Arb(name="arb_tx", sid="arb2", pair="TX", qty=10, price=0.5))
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await orch._run_cycle()
    committed = sum(p["quantity"] * (p["price"] or 0.5) for p in broker.placed)
    assert committed <= 12.0 + 1e-6                  # total never exceeds the cycle snapshot
    assert orch.ledger.reserved_total <= 12.0 + 1e-6
    assert len(broker.placed) == 2                   # only the FIRST bundle's two legs — second was shut out


# ═══ SEAM 2 (d) — dead feed halts NEW orders (halt-but-hold) + auto-recovers ════
@pytest.mark.asyncio
async def test_d_dead_feed_halts_new_orders_then_recovers():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"market_making": "YES_MM"})
    orch.register_strategy(_Dir("mm", sid="market_making"))
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()

    await orch._run_cycle()
    assert len(broker.placed) == 1                   # trades on fresh feed

    state.set_feed_dead(True)                         # feed dies
    await orch._run_cycle()
    assert len(broker.placed) == 1                   # HALT-BUT-HOLD: no new order; positions untouched
    assert orch.is_halted is False                    # not a hard halt — a soft per-cycle skip

    state.set_feed_dead(False)                        # feed recovers
    await orch._run_cycle()
    assert len(broker.placed) == 2                   # auto-recovers → trades again


# ═══ SEAM 2 (e) — partial staleness skips ONLY the stale market ════════════════
@pytest.mark.asyncio
async def test_e_partial_staleness_skips_only_stale_market():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"market_making": "YES_MKTA", "arb": "YES_MKTB"})
    orch.register_strategy(_Dir("mkta", sid="market_making"))   # exchange_id YES_MKTA (stale)
    orch.register_strategy(_Dir("mktb", sid="arb"))             # exchange_id YES_MKTB (fresh)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    state.set_stale_markets({"YES_MKTA"})
    await orch._run_cycle()
    placed = {p["exchange_id"] for p in broker.placed}
    assert "YES_MKTB" in placed and "YES_MKTA" not in placed   # fresh placed, stale skipped
