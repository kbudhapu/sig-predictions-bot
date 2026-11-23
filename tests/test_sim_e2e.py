"""Full end-to-end SIM integration tests (SIG_SIM_TEST_PLAN.md) — the whole bot wired through the
composition root with REAL modules + SimulatedBroker. Proves the modules work TOGETHER, not just in
isolation. No live orders. See the report for per-area findings + the seams.
"""
import os
import types

import pytest
import yaml

from sig.engine.composition import build_sim_stack, SimulatedBroker
from sig.engine.protocols import Intent, EdgePerf
from sig.oms.models import FillPolicy


def _cfg() -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# Strategy doubles that conform to the orchestrator's Strategy Protocol
# --------------------------------------------------------------------------- #
class _Strat:
    tier = "CORE"

    def __init__(self, name, *, cadence_s=0.0, sid=None, stype="directional", sigma_p=0.06,
                 side="buy", action="open", price=0.5, qty=None):
        self.name = name
        self.cadence_s = cadence_s
        self._sid = sid or name
        self._stype = stype
        self._sigma = sigma_p
        self._side, self._action, self._price, self._qty = side, action, price, qty
        self.evaluate_calls = 0
        self.fills = []

    def evaluate(self, state):
        self.evaluate_calls += 1
        meta = {"sigma_p": self._sigma}
        if self._qty is not None:
            meta["qty"] = self._qty
        return [Intent(strategy_id=self._sid, exchange_id=f"YES_{self.name.upper()}", side=self._side,
                       action=self._action, strategy_type=self._stype, signal_strength=1.0,
                       price=self._price, metadata=meta)]

    def on_fill(self, fill):
        self.fills.append(fill)

    def realized_performance(self):
        return EdgePerf()


class _Crash(_Strat):
    def evaluate(self, state):
        self.evaluate_calls += 1
        raise RuntimeError("simulated strategy crash in evaluate()")


class _TrackingSimBroker(SimulatedBroker):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancelled = []

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return await super().cancel_order(order_id)


# ═══ AREA 1 — the order path, repeatedly ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_area1_order_path_repeats_and_reconciles():
    # This exercises the order PATH repeating each cycle, not the caps: a directional buyer re-buying the
    # same market every cycle is (correctly) stopped by the per-market cap once just-submitted buys count as
    # pending exposure (2026-10-06 fix), so lift the per-market cap for this path test.
    cfg = _cfg()
    cfg["capital"]["max_per_market_frac"] = 1.0
    cfg["capital"]["max_cluster_frac"] = 1.0
    orch, broker, alloc, state, oms = build_sim_stack(
        config=cfg, strategy_contracts={"market_making": "YES_MM"})
    orch.register_strategy(_Strat("mm", sid="market_making"))
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    for _ in range(5):
        await orch._run_cycle()
    assert len(broker.placed) == 5                      # one order each cycle, all the way to the broker
    assert len(state._orders) >= 1                      # fills written to (SIM) state
    assert all(o["strategy_id"] == "market_making" for o in broker.placed)


# ═══ AREA 2 — the capital arbiter, REAL sizer, no double-spend ═════════════════
@pytest.mark.asyncio
async def test_area2_capital_arbiter_no_double_spend():
    # Small buying_power snapshot, large per-edge budgets → the two strategies TOGETHER want more
    # than the balance. The ledger must cap the second. name == strategy_id (the case that exposed
    # the release bug). REAL sizer produces the sizes.
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb": "YES_ARB", "market_making": "YES_MM"},
        buying_power=2_000.0, equity=100_000.0)
    orch.register_strategy(_Strat("arb", sid="arb", sigma_p=0.02))            # k=0.50
    orch.register_strategy(_Strat("mm", sid="market_making", sigma_p=0.02))
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    await orch._run_cycle()
    # The lock-free guarantee: total committed capital never exceeds the cycle's snapshot.
    assert orch.ledger.reserved_total <= 2_000.0 + 1e-6
    committed = sum(p["quantity"] * (p["price"] or 0.5) for p in broker.placed)
    assert committed <= 2_000.0 + 1e-6                  # second strategy could NOT re-spend the first's capital


# ═══ AREA 3 — two cadences + MM idempotency ════════════════════════════════════
@pytest.mark.asyncio
async def test_area3_cadence_gating_frequencies():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb": "YES_ARB", "market_making": "YES_MM"})
    fast = _Strat("arb", sid="arb", cadence_s=0.0)         # every cycle
    slow = _Strat("mm", sid="market_making", cadence_s=9999.0)  # not again within the test
    orch.register_strategy(fast)
    orch.register_strategy(slow)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    for _ in range(4):
        await orch._run_cycle()
    assert fast.evaluate_calls == 4 and slow.evaluate_calls == 0   # different frequencies, cadence-gated


def test_area3_market_making_is_idempotent():
    from strategies.market_making import MarketMaker
    mm = MarketMaker(equity=100_000.0, active_markets=1)
    a = mm.build("YES_X", 0.46, 0.54, now=0.0)
    b = mm.build("YES_X", 0.46, 0.54, now=0.1)          # same touch → identical desired orders
    assert a["orders"] == b["orders"]                    # no re-write → write budget preserved


# ═══ AREA 4 — feed → state → loop (seam: orchestrator does not consume the feed) ═
def test_area4_feed_staleness_invariant():
    from sig.feed.feed import RealtimeFeed, FeedConfig
    feed = RealtimeFeed(object(), ws_factory=lambda *a, **k: None, config=FeedConfig.from_config(_cfg()))
    # The real feed exposes the staleness invariant the loop/gate are supposed to key off.
    assert isinstance(feed.is_stale(), bool)
    assert hasattr(feed.health, "consume_reconcile_needed")   # gap→resync invariant exists
    # FINDING (reported): the Orchestrator cycle reads state.read_exposure() only — it does NOT
    # consume the feed/prices, so the stale-data→gate-halt path is NOT wired into the orchestrator
    # loop yet. The feed module is real and wireable; the loop→feed seam is unbuilt.


# ═══ AREA 5 — error isolation ══════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_area5_crashing_strategy_isolated():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb": "YES_ARB", "market_making": "YES_MM"})
    crash = _Crash("arb", sid="arb")
    healthy = _Strat("mm", sid="market_making")
    orch.register_strategy(crash)
    orch.register_strategy(healthy)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    for _ in range(3):
        await orch._run_cycle()                          # must NOT raise despite the crashing strategy
    assert healthy.evaluate_calls == 3                   # the healthy strategy kept running
    assert len(broker.placed) == 3                       # and its orders kept flowing
    assert orch._registry._entries["arb"].consecutive_failures >= 3   # crash strategy degraded/flagged


# ═══ AREA 6 — startup & shutdown gates ═════════════════════════════════════════
@pytest.mark.asyncio
async def test_area6_startup_hard_gate_blocks_on_mismatch():
    class _UnmappedBroker(SimulatedBroker):
        async def get_positions(self):
            return [types.SimpleNamespace(contract_id="YES_GHOST", side="long", qty=5.0,
                                          avg_entry_price=0.5)]     # a venue position we don't map
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"market_making": "YES_MM"},
        broker=_UnmappedBroker())
    ok = await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    assert ok is False and oms.is_hard_locked is True    # unmapped venue position → pre-flight hard-lock


@pytest.mark.asyncio
async def test_area6_halt_holds_positions():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"market_making": "YES_MM"})
    orch.register_strategy(_Strat("mm", sid="market_making"))
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()
    await orch._run_cycle()
    n_before = len(broker.placed)
    orch.halt("operator")
    assert orch.is_halted is True
    assert len(broker.placed) == n_before                # halt-but-HOLD: no flatten, positions untouched


# ═══ AREA 7 — the mechanical floor ═════════════════════════════════════════════
@pytest.mark.asyncio
async def test_area7_oms_multileg_atomic_fill():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb_pair": "YES_AZ_D"})
    bundle = await oms.submit(
        strategy_id="arb_pair",
        legs=[{"contract_id": "YES_AZ_D", "market_id": "YES_AZ_D", "side": "buy", "qty": 10, "leg_id": "d"},
              {"contract_id": "YES_AZ_R", "market_id": "YES_AZ_R", "side": "buy", "qty": 10, "leg_id": "r"}],
        fill_policy=FillPolicy.for_arb())
    assert len(bundle.slices) == 2
    assert all(s.status == "filled" for s in bundle.slices)   # all legs filled atomically


@pytest.mark.asyncio
async def test_area7_oms_partial_multileg_unwinds_no_naked_leg():
    class _PartialMultiBroker(_TrackingSimBroker):
        async def place_multi_leg(self, *, strategy_id, market_id, legs, priority=True):
            res = await super().place_multi_leg(strategy_id=strategy_id, market_id=market_id,
                                                legs=legs, priority=priority)
            if len(res.legs) >= 2:                        # leg 1 does NOT fill → partial arb
                res.legs[1]["status"] = "rejected"
                res.legs[1]["filled_qty"] = 0.0
                self._orders[res.legs[1]["order_id"]] = {"status": "rejected", "filled_qty": 0.0,
                                                         "avg_price": 0.0}   # truthful get_order
            return res
    broker = _PartialMultiBroker()
    orch, _b, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"arb_pair": "YES_AZ_D"}, broker=broker)
    bundle = await oms.submit(
        strategy_id="arb_pair",
        legs=[{"contract_id": "YES_AZ_D", "market_id": "YES_AZ_D", "side": "buy", "qty": 10, "leg_id": "d"},
              {"contract_id": "YES_AZ_R", "market_id": "YES_AZ_R", "side": "buy", "qty": 10, "leg_id": "r"}],
        fill_policy=FillPolicy.for_arb())
    assert bundle.strategy_class.value == "arb"
    # The OMS waits cycle_cancel_threshold (3) cycles for the resting leg, THEN runs the arb
    # UNWIND_ALL_LEGS terminal resolution (reconciled against venue truth). Advance past the
    # threshold: the bundle must RESOLVE (no naked arb leg left pending/tracked).
    for _ in range(5):
        await oms.advance_pending_bundles()
    assert await oms.pending_bundle_count() == 0, "ARB PARTIAL FILL LEFT A BUNDLE PENDING (no terminal unwind)"


@pytest.mark.asyncio
async def test_area7_allocator_funds_mechanical_core():
    from strategies.allocator.allocator import Allocator
    alloc = Allocator(_cfg())
    weights = (await alloc.propose())["weights"]          # dataless → priors × score; CORE should dominate
    assert weights["arb"] > weights["order_flow"]
    assert weights["market_making"] > weights["favorite_longshot"]


def test_area7_market_making_quote_skew_and_adverse_pull():
    from strategies.market_making import MarketMaker, MarketMakerConfig, AdverseConfig
    mm = MarketMaker(MarketMakerConfig(adverse=AdverseConfig(move_threshold=0.03, window_seconds=30)),
                     equity=100_000.0, active_markets=1)
    # FLAT: we hold no YES, so we may only BID. Posting an ask here would be a naked short the risk
    # gate rejects — so the MM quotes the bid only until inventory exists (no one-sided short leaks).
    base = mm.build("YES_X", 0.46, 0.54, now=0.0)
    assert any(o["action"] == "buy" for o in base["orders"])
    assert not any(o["action"] == "sell" for o in base["orders"])
    for _ in range(6):                                    # bids keep filling → long YES inventory
        mm.record_fill("YES_X", "buy", 100)
    skewed = mm.build("YES_X", 0.46, 0.54, now=0.2)
    assert skewed["position"] > 0                         # inventory tracked
    # Now LONG: the MM posts an ASK to sell that inventory back at a spread (the round-trip), sized
    # to the held inventory and never exceeding it (the gate would clamp any excess anyway).
    sells = [o for o in skewed["orders"] if o["action"] == "sell"]
    assert sells and all(0 < o["quantity"] <= skewed["position"] for o in sells)
    out = mm.build("YES_X", 0.60, 0.68, now=10.0)         # fast up-move → adverse selection
    assert out["adverse"].pull_ask is True                # pulled the picked-off side


# ═══ AREA 8 — multi-cycle soak with injected adverse events ════════════════════
@pytest.mark.asyncio
async def test_area8_soak_with_injected_adverse_events():
    orch, broker, alloc, state, oms = build_sim_stack(
        config=_cfg(), strategy_contracts={"market_making": "YES_MM"})
    healthy = _Strat("mm", sid="market_making")
    orch.register_strategy(healthy)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()

    # a crashing strategy injected mid-soak — must not take the loop down
    crash = _Crash("arb", sid="arb")
    orch.register_strategy(crash)

    reject_cycle = 20
    for i in range(50):
        if i == reject_cycle:
            orig = broker.place_order
            calls = {"n": 0}
            async def _reject_once(*a, **k):
                if calls["n"] == 0:
                    calls["n"] += 1
                    raise RuntimeError("simulated order reject")
                return await orig(*a, **k)
            broker.place_order = _reject_once
        await orch._run_cycle()                            # must never raise
        if i == reject_cycle:
            broker.place_order = orig
    assert healthy.evaluate_calls == 50                    # ran every cycle through all adverse events
    assert orch.consecutive_failures == 0                  # recovered (no lingering degrade)
    assert orch._heartbeat_age() < 5.0                     # dead-man would NOT false-fire (fresh heartbeat)


# ═══ AREA 8 (combined) — soak with ALL seams active + ALL adverse events together ═══
# The plan's §8 "run it with the seams in": one long soak that simultaneously exercises the
# multi-leg atomic arb (SEAM-1, incl. a partial→unwind window), the feed-staleness halt-but-hold
# (SEAM-2, a mid-soak dead-feed window), a persistently-crashing strategy (error isolation), AND an
# order reject — and proves nothing crashes/deadlocks, writes stay bounded (no runaway), pending
# bundles always resolve (no naked leg accumulates), the dead-man stays fresh (no false-fire), and
# the bot recovers from each event.
class _SoakArb:
    tier = "CORE"; cadence_s = 0.0

    def __init__(self, *, sid="arb", pair="AZ", qty=4, price=0.5):
        self.name = "arb"; self._sid = sid; self._pair = pair; self._qty = qty; self._price = price
        self.evaluate_calls = 0; self.fills = []

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


class _WindowedPartialBroker(_TrackingSimBroker):
    """Fills both arb legs normally, EXCEPT while `fail_leg2` is set → leg 2 is rejected (partial arb)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail_leg2 = False

    async def place_multi_leg(self, *, strategy_id, market_id, legs, priority=True):
        res = await super().place_multi_leg(strategy_id=strategy_id, market_id=market_id,
                                            legs=legs, priority=priority)
        if self.fail_leg2 and len(res.legs) >= 2:
            res.legs[1]["status"] = "rejected"; res.legs[1]["filled_qty"] = 0.0
            self._orders[res.legs[1]["order_id"]] = {"status": "rejected", "filled_qty": 0.0,
                                                     "avg_price": 0.0}
        return res


@pytest.mark.asyncio
async def test_area8_combined_soak_all_seams_and_events():
    broker = _WindowedPartialBroker()
    orch, _b, alloc, state, oms = build_sim_stack(
        config=_cfg(),
        strategy_contracts={"market_making": "YES_MM", "arb": "YES_AZ_D", "order_flow": "YES_OF"},
        broker=broker, buying_power=100_000.0, equity=100_000.0)
    healthy = _Strat("mm", sid="market_making")
    arb = _SoakArb(sid="arb", pair="AZ", qty=4, price=0.5)
    crash = _Crash("order_flow", sid="order_flow")           # persistent crash — must stay isolated
    orch.register_strategy(healthy)
    orch.register_strategy(arb)
    orch.register_strategy(crash)
    await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await alloc.refresh()

    N = 60
    DEAD = set(range(20, 26))        # feed-dead window (6 cycles): halt-but-hold
    PARTIAL = set(range(10, 14))     # partial-arb window: leg 2 rejected → must unwind
    REJECT = 35                      # single order reject (caught, loop continues)
    max_age_seen = 0.0
    fresh_cycles = 0

    for i in range(N):
        broker.fail_leg2 = i in PARTIAL
        state.set_feed_dead(i in DEAD)
        if i == REJECT:
            orig = broker.place_order
            calls = {"n": 0}
            async def _reject_once(*a, **k):
                if calls["n"] == 0:
                    calls["n"] += 1
                    raise RuntimeError("simulated order reject")
                return await orig(*a, **k)
            broker.place_order = _reject_once

        await orch._run_cycle()      # MUST never raise, through every injected event
        orch._write_heartbeat()      # mirror run_forever's per-tick beat (written even on feed-dead early-return)

        if i == REJECT:
            broker.place_order = orig
        if i not in DEAD:
            fresh_cycles += 1
        max_age_seen = max(max_age_seen, orch._heartbeat_age())

    # drain any still-pending arb bundle to its terminal unwind (no naked leg may survive the soak)
    for _ in range(10):
        await oms.advance_pending_bundles()

    # recovery + isolation
    assert healthy.evaluate_calls == fresh_cycles          # MM evaluated every FRESH cycle (dead window skipped)
    assert arb.evaluate_calls <= fresh_cycles               # arb gated by pending-bundle + staleness, never over-runs
    assert crash.evaluate_calls > 0                          # the crashing strategy WAS exercised
    assert orch._registry._entries["order_flow"].consecutive_failures >= 3   # and was degraded, not fatal
    assert orch.is_halted is False                          # feed recovered → soft gate cleared, no hard halt
    # no naked leg / no deadlock: every multi-leg bundle resolved (atomic fill OR full unwind)
    assert await oms.pending_bundle_count() == 0
    # no runaway writes: bounded by (fresh cycles × strategies), not unbounded re-submission
    assert len(broker.placed) <= fresh_cycles * 4
    assert len(broker.placed) > 0                            # the floor actually traded through the soak
    # dead-man never false-fired: the per-tick heartbeat stayed fresh across the dead-feed window too
    assert max_age_seen < 5.0 and orch._heartbeat_age() < 5.0
