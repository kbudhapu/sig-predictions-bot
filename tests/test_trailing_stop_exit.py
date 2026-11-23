"""Directional TRAILING-STOP / stop-loss exit — the fix that replaced the fixed +100% take-profit.

Why this exists: a fixed +100% take-profit CAPPED winners (a +453% runner sold at +100% leaves +353%
on the table), while removing the TP let a winner bleed all the way back on reversal. The trailing
stop is the middle path — let a winner RUN as its PEAK climbs, and sell only when it pulls back by
`trail_pct` OF THE PEAK; cut an outright loser at `-stop_loss_pct`; never touch an arb basket leg
(those hold to settlement).

Two layers are pinned:
  * `_directional_exit_decision` — the PURE verdict (arming off the peak, give-back-of-peak trigger,
    stop-loss). It is the ONE rule both exit paths (venue-valued and the state/local fallback) call,
    so they cannot drift — the drift that had left the fallback on the old fixed +100% TP.
  * `_run_takeprofit_exits` fallback path — end to end against a REAL StateStore: the peak PERSISTS
    across cycles, a winner rides past +100% and is sold NEAR its peak on a pullback (not capped, not
    bled to zero), a monotonic climber is held, a loser is stopped out, and an arb leg is left alone.
"""
from __future__ import annotations

import asyncio

import pytest  # noqa: F401  (imported for parity with the suite; asyncio.run drives the coroutines)

import yaml

from infra.state.store import StateStore
from sig.engine.composition import (
    AllocatorAdapter,
    RiskGateAdapter,
    SimulatedBroker,
    SizingAdapter,
    StateAdapter,
)
from sig.engine.orchestrator import Orchestrator, _directional_exit_decision
from sig.oms.manager import OrderManagementSystem
from strategies.allocator.allocator import Allocator
from strategies.risk_gate.gate import RiskGate
from strategies.sizing.sizing import Sizer

CONFIG_PATH = "config/sig.yaml"
EXCH = "948"
ARM, TRAIL, STOP = 0.5, 0.2, 0.5     # arm at +50% peak; sell on a 20%-of-peak give-back; cut at -50%


def _cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ───────────────────────────── the pure verdict ───────────────────────────── #
def _d(pnl: float, peak: float):
    return _directional_exit_decision(pnl, peak, trail_arm_pct=ARM, trail_pct=TRAIL, stop_loss_pct=STOP)


def test_helper_below_arm_is_held():
    # a peak that never reached +50% is not trailed at all (only the stop-loss protects it)
    assert _d(0.40, 0.40) is None
    assert _d(-0.30, 0.40) is None          # a small loss, not past the stop → hold


def test_helper_climbing_winner_is_held():
    # at its peak (still climbing) the give-back trigger is strictly below current → HOLD
    assert _d(4.53, 4.53) is None           # +453% at the high
    assert _d(1.00, 1.00) is None           # +100% — NOT capped; the old fixed TP would have sold here


def test_helper_sells_on_giveback_of_peak():
    # peak +453%, trail 20% of the PEAK → trigger +362.4%
    assert _d(3.62, 4.53) == "trailing_take_profit"   # gave back just over 20% of the peak → SELL
    assert _d(3.63, 4.53) is None                     # still within 20% of the peak → HOLD


def test_helper_stop_loss_cuts_a_loser():
    assert _d(-0.50, 0.0) == "stop_loss"
    assert _d(-0.51, 0.10) == "stop_loss"             # a loser past -50% is cut whether or not it armed
    assert _d(-0.49, 0.0) is None                     # not yet at the stop


def test_helper_arms_off_the_peak_not_the_current_gain():
    # reached a +60% peak (armed), then pulled back to +45% (now below the arm): STILL armed, and
    # +45% <= 60%*(1-0.2)=+48% → SELL. Arming keys off the peak, so a pullback cannot disarm it.
    assert _d(0.45, 0.60) == "trailing_take_profit"


# ───────────────────────── the fallback path, end to end ───────────────────────── #
def _stack(store: StateStore):
    """SIM stack over a REAL StateStore. SimulatedBroker has no get_positions_valued, so
    `_run_takeprofit_exits` takes the state/local FALLBACK path — the one being pinned here."""
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


async def _mark(store: StateStore, bid: float, *, seq: int) -> None:
    await store.upsert_price(EXCH, best_bid=bid, best_ask=bid + 0.005, sequence=seq)


def _closes(broker) -> list[dict]:
    # BUY-TO-CANCEL: the directional exit closes by BUYING the OPPOSITE outcome (side="buy",
    # action="close"), which SIG nets against the held position — never a same-side "sell".
    return [p for p in broker.placed if str(p.get("side", "")).lower() == "buy"]


def _sells(broker) -> list[dict]:
    return [p for p in broker.placed if str(p.get("side", "")).lower() == "sell"]


def test_winner_rides_past_100pct_and_closes_near_peak_via_buy_to_cancel(tmp_path):
    """+50% → +100% → +453% (held throughout) → pullback to +350% → CLOSED by buy-to-cancel.

    Proves the whole point: the winner is NOT capped at +100%, it rides to its peak, and it is closed
    NEAR that peak on a pullback (not at +100%, not bled to zero). The close at +350% also proves the
    PEAK PERSISTED across cycles — it fires against the remembered +453%, not the current +350%. The
    close is a BUY of the OPPOSITE outcome (held YES ⇒ buy NO), never a same-side sell.
    """
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)   # +100 ⇒ held YES
        orch, broker = _stack(store)

        await _mark(store, 0.15, seq=1)
        assert await orch._run_takeprofit_exits() == 0        # +50% → armed, held
        await _mark(store, 0.20, seq=2)
        assert await orch._run_takeprofit_exits() == 0        # +100% → HELD (the fixed TP is gone)
        await _mark(store, 0.553, seq=3)
        assert await orch._run_takeprofit_exits() == 0        # +453% → held (still climbing)
        assert _closes(broker) == [], "a climbing winner is never closed"

        await _mark(store, 0.45, seq=4)                       # +350% < peak*(1-0.2)=+362.4% → CLOSE
        assert await orch._run_takeprofit_exits() == 1
        closes = _closes(broker)
        assert len(closes) == 1
        c = closes[0]
        assert c["exchange_id"] == EXCH
        assert c["side"] == "buy" and c["outcome"] == "no"    # held YES → BUY NO to cancel (not sell YES)
        assert c["quantity"] == 100                           # EXACTLY the held qty → nets to ~0, no naked NO
        assert c["price"] == 0.55                             # complement of the 0.45 bid (1 − 0.45)
        await store.close()

    asyncio.run(main())


def test_close_is_buy_exactly_held_opposite_no_naked_short(tmp_path):
    """Headline mechanic in isolation: held 100 YES → the close is BUY 100 NO at the complement.

    Buying EXACTLY the held quantity of the OPPOSITE outcome nets the position to ~0 at FIFO lot cost
    (per the SIG spec, an opposite-signed buy reduces the position); it never opens a naked NO, and it
    is a BUY — not a naked same-side sell that would need a YES buyer and trip the gate.
    """
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)
        orch, broker = _stack(store)
        await _mark(store, 0.80, seq=1)                       # +700% → peak set, held (no pullback yet)
        assert await orch._run_takeprofit_exits() == 0
        await _mark(store, 0.50, seq=2)                       # +400% < peak*(1-0.2)=+560% → CLOSE
        assert await orch._run_takeprofit_exits() == 1
        closes = _closes(broker)
        assert len(closes) == 1
        c = closes[0]
        assert c["side"] == "buy", "closes by BUYING the opposite, not selling the held side"
        assert c["outcome"] == "no", "held YES ⇒ buy NO to cancel"
        assert c["quantity"] == 100, "exactly the held qty ⇒ nets to ~0, never past flat into a naked NO"
        assert c["price"] == round(1.0 - 0.50, 3)             # complement of the 0.50 bid (0.50)
        assert _sells(broker) == [], "nothing is sold — the close is a buy-to-cancel"
        await store.close()

    asyncio.run(main())


def test_the_persisted_peak_is_what_fires_the_close(tmp_path):
    """Isolates peak PERSISTENCE: same position at +350%, opposite outcome depending on the peak.

    (a) An orch that watched it peak at +453% closes at +350% (gave back >20% of the remembered peak).
    (b) A FRESH orch meeting the position at +350% takes +350% as its own peak (trigger +280%) and
        HOLDS. Same inputs, opposite verdict ⇒ the cross-cycle peak is doing the work.
    """
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)

        orch, _ = _stack(store)
        await _mark(store, 0.553, seq=1)
        assert await orch._run_takeprofit_exits() == 0        # peak recorded at +453% (held)
        await _mark(store, 0.45, seq=2)
        assert await orch._run_takeprofit_exits() == 1        # closes against the remembered +453%

        orch2, broker2 = _stack(store)                        # fresh peak memory
        assert await orch2._run_takeprofit_exits() == 0       # sees only +350% → its own peak → holds
        assert _closes(broker2) == []
        await store.close()

    asyncio.run(main())


def test_monotonic_climber_is_always_held(tmp_path):
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)
        orch, broker = _stack(store)
        for i, bid in enumerate([0.15, 0.25, 0.40, 0.70, 1.00], start=1):
            await _mark(store, bid, seq=i)
            assert await orch._run_takeprofit_exits() == 0
        assert _closes(broker) == [], "a position that only climbs is never closed"
        await store.close()

    asyncio.run(main())


def test_loser_is_cut_at_the_stop_loss(tmp_path):
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)
        orch, broker = _stack(store)
        await _mark(store, 0.08, seq=1)
        assert await orch._run_takeprofit_exits() == 0        # -20% → above the stop → hold
        await _mark(store, 0.04, seq=2)
        assert await orch._run_takeprofit_exits() == 1        # -60% → stop-loss → cut
        closes = _closes(broker)
        assert len(closes) == 1 and closes[0]["exchange_id"] == EXCH
        assert closes[0]["side"] == "buy" and closes[0]["outcome"] == "no"   # cut a YES loser by buying NO
        await store.close()

    asyncio.run(main())


def test_arb_basket_leg_is_never_directional_exited(tmp_path):
    """A contract that is a leg of an OPEN arb basket holds to settlement — the trailing stop skips it
    even at an enormous gain (its profit is contracted, not at risk)."""
    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        await store.upsert_position(EXCH, quantity=100.0, avg_price=0.10)
        await store.open_basket(
            "b1", strategy_id="arb", kind="hard_arb",
            legs=[{"exchange_id": EXCH, "side": "yes", "quantity": 100, "entry_price": 0.10}],
            entry_cost=10.0, settlement_value=100.0,
        )
        orch, broker = _stack(store)
        await _mark(store, 0.90, seq=1)                       # +800% — would trip any directional exit
        assert await orch._run_takeprofit_exits() == 0
        assert broker.placed == [], "an arb basket leg is held to settlement, never closed here"
        await store.close()

    asyncio.run(main())
