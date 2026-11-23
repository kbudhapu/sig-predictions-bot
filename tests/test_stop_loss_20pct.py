"""Directional stop-loss tightened to -20% (live losers bled to ~-24% without ever reaching -50%, so
the stop never fired). Pins: the config value, a directional loser cut at -20% of REAL entry, that the
stop measures REAL P&L (an avg_price=0 position is NOT cut — it is invisible, the avgCost-bug class),
and that an arb basket leg is EXEMPT even well past -20% (it holds to settlement)."""
from __future__ import annotations

import asyncio

import yaml

from infra.state.store import StateStore
from sig.engine.composition import (
    AllocatorAdapter, RiskGateAdapter, SimulatedBroker, SizingAdapter, StateAdapter,
)
from sig.engine.orchestrator import Orchestrator, _directional_exit_decision, _snap_tick
from sig.oms.manager import OrderManagementSystem
from strategies.allocator.allocator import Allocator
from strategies.risk_gate.gate import RiskGate
from strategies.sizing.sizing import Sizer

CONFIG_PATH = "config/sig.yaml"
EXCH = "948"
ARM, TRAIL, STOP = 0.5, 0.2, 0.20     # the LIVE stop is now -20%


def _cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_config_directional_stop_is_20pct():
    """The deployed config cuts directional losers at -20% (not the old -50%)."""
    assert float(_cfg()["directional_exit"]["stop_loss_pct"]) == 0.20


# ── the pure verdict at the new threshold ──
def test_helper_cuts_at_20pct_not_before():
    d = lambda pnl, peak: _directional_exit_decision(pnl, peak, trail_arm_pct=ARM,
                                                     trail_pct=TRAIL, stop_loss_pct=STOP)
    assert d(-0.20, 0.0) == "stop_loss"       # exactly -20% → cut
    assert d(-0.25, 0.0) == "stop_loss"       # worse → cut
    assert d(-0.19, 0.0) is None              # -19% → still held (above the stop)


def _stack(store: StateStore):
    """SIM stack over a REAL StateStore with the -20% stop. SimulatedBroker has no
    get_positions_valued, so _run_takeprofit_exits takes the state/local FALLBACK path."""
    cfg = _cfg()
    equity = 100_000.0
    broker = SimulatedBroker(buying_power=equity, equity=equity)
    state = StateAdapter(store=store)
    oms = OrderManagementSystem(broker=broker, state=state, strategy_contracts={})
    orch = Orchestrator(
        broker=broker, state=state, oms=oms,
        risk_gate=RiskGateAdapter(RiskGate(config=cfg), balance=equity),
        sizing=SizingAdapter(Sizer(cfg), balance=equity),
        allocator=AllocatorAdapter(Allocator(cfg), capital=equity),
        tick_seconds=5.0, environment="sim",
        exit_trail_arm_pct=ARM, exit_trail_pct=TRAIL, exit_stop_loss_pct=STOP,
    )
    return orch, broker


async def _mark(store, bid, *, seq):
    await store.upsert_price(EXCH, best_bid=bid, best_ask=bid + 0.005, sequence=seq)


def _closes(broker):
    return [p for p in broker.placed if str(p.get("side", "")).lower() == "buy"]


def test_directional_loser_cut_at_20pct_of_real_entry(tmp_path):
    """avg 0.10 (REAL) → bid 0.081 (-19%) HELD, then bid 0.08 (-20%) CUT via buy-to-cancel."""
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)   # +100 ⇒ held YES, REAL avg
        orch, broker = _stack(store)

        await _mark(store, 0.081, seq=1)
        assert await orch._run_takeprofit_exits() == 0        # -19% → above the stop → HELD
        assert _closes(broker) == []

        await _mark(store, 0.08, seq=2)
        assert await orch._run_takeprofit_exits() == 1        # -20% → CUT
        closes = _closes(broker)
        assert len(closes) == 1 and closes[0]["exchange_id"] == EXCH
        assert closes[0]["side"] == "buy" and closes[0]["outcome"] == "no"   # YES loser cut by buying NO
        assert closes[0]["quantity"] == 100                                   # exactly held ⇒ nets to ~0
        await store.close()

    asyncio.run(main())


def test_zero_avg_position_is_NOT_cut_stop_needs_real_pnl(tmp_path):
    """The avgCost-bug class: a position with avg_price=0 is invisible to the stop (pnl is uncomputable),
    so it is skipped — proving the -20% stop only acts on REAL entry prices, never on avg=0 garbage."""
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.0)      # broken avg
        orch, broker = _stack(store)
        await _mark(store, 0.01, seq=1)                       # an apparent collapse, but avg=0 ⇒ skipped
        assert await orch._run_takeprofit_exits() == 0
        assert _closes(broker) == []
        await store.close()

    asyncio.run(main())


def test_snap_tick_puts_close_price_on_the_0005_grid():
    """The buy-to-cancel complement must land on the 0.005 tick or the venue rejects it (400) and the
    cut never fills — the live failure seen on exch 1049 (price 0.329)."""
    assert _snap_tick(1.0 - 0.671244) == 0.330     # 0.328756 → 0.330 (was 0.329, off-tick → 400)
    assert _snap_tick(0.917) == 0.915
    assert _snap_tick(1.2) == 0.995 and _snap_tick(-0.1) == 0.005
    for x in (0.329, 0.0011, 0.9999, 0.5023, 0.328756):
        p = _snap_tick(x)
        assert abs(round(p / 0.005) - p / 0.005) < 1e-9, f"{p} is off the 0.005 tick"


def test_stop_loss_close_price_is_tick_valid(tmp_path):
    """End to end: a -21% loser is cut AND the buy-to-cancel limit is on the 0.005 grid (so the venue
    accepts it). avg 0.10, bid 0.079 ⇒ complement 0.921 → snapped 0.920, not the off-tick 0.921."""
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)
        orch, broker = _stack(store)
        await _mark(store, 0.079, seq=1)                      # -21% → stop
        assert await orch._run_takeprofit_exits() == 1
        c = _closes(broker)[0]
        p = float(c["price"])
        assert abs(round(p / 0.005) - p / 0.005) < 1e-9, f"close price {p} is off the 0.005 tick"
        await store.close()

    asyncio.run(main())


def test_arb_basket_leg_exempt_from_20pct_stop(tmp_path):
    """An open arb basket leg is EXEMPT: even down -30% it is NOT cut — the pair settles to 1.00, so a
    mid-flight mark loss on a hedged leg is meaningless."""
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)
        await store.open_basket(
            "b1", strategy_id="arb", kind="hard_arb",
            legs=[{"exchange_id": EXCH, "side": "yes", "quantity": 100, "entry_price": 0.10}],
            entry_cost=10.0, settlement_value=100.0,
        )
        orch, broker = _stack(store)
        await _mark(store, 0.07, seq=1)                       # -30% (well past the -20% stop)
        assert await orch._run_takeprofit_exits() == 0
        assert broker.placed == [], "an arb basket leg is held to settlement, never stopped out"
        await store.close()

    asyncio.run(main())
