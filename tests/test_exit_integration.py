"""Exit INTEGRATION — the exit step wired into the trading cycle, end to end.

The pure decision logic is covered by tests/test_exit_decision.py and the data layer by
tests/test_position_visibility.py. What is pinned HERE is the wiring, and specifically the four
places where a plausible-looking integration would silently not work:

  1. capital must actually come BACK (enter -> correct -> exit -> re-enter on freed capital)
  2. the exit must NOT reserve capital, or it refuses itself exactly when the bot is fully
     deployed — which is the only state in which anyone wants to exit
  3. the risk gate must be handed real positions, or its sell branch rejects every exit as a
     naked short
  4. with nothing better to do with the money, the basket must be LEFT ALONE

These run against a REAL StateStore on a temp file (so the real `baskets` table, real price rows
and real mark-to-market are exercised) plus a cash-tracking SimulatedBroker, because a static
broker cannot demonstrate recycling: with a frozen buying power, "capital was freed" is unfalsifiable.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from infra.state.store import StateStore
from sig.engine.composition import (
    AllocatorAdapter,
    RiskGateAdapter,
    SimulatedBroker,
    SizingAdapter,
    StateAdapter,
)
from sig.engine.orchestrator import Orchestrator
from sig.oms.manager import OrderManagementSystem
from strategies.allocator.allocator import Allocator
from strategies.exit import ExitConfig, load_exit_config
from strategies.risk_gate.gate import RiskGate
from strategies.sizing.sizing import Sizer

CONFIG_PATH = "config/sig.yaml"

# A corrected sibling pair: entered at 0.46 + 0.46 = 0.92 for a guaranteed 1.00, now bid 0.49/0.49.
LEG_A, LEG_B = "1077", "1076"
ENTRY_PX = 0.46
QTY = 1_000


def _cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def deep_book(bid: float, qty: int = 200_000) -> dict:
    """A book that can absorb the whole basket without slippage."""
    return {"bids": [{"price": bid, "quantity": qty}], "asks": []}


def thin_book(bid: float, qty: int = 134) -> dict:
    """Measured reality: some SIG books hold ~134 contracts of bid. Cannot absorb a 1000-lot."""
    return {"bids": [{"price": bid, "quantity": qty}], "asks": []}


async def _store(tmp_path, *, bid_a=0.49, bid_b=0.49) -> StateStore:
    store = await StateStore(tmp_path / "state.db").open()
    for eid, bid in ((LEG_A, bid_a), (LEG_B, bid_b)):
        await store.upsert_price(eid, best_bid=bid, best_ask=bid + 0.005, sequence=1)
        await store.upsert_position(eid, quantity=float(QTY), avg_price=ENTRY_PX)
    return store


async def _enter(broker, *, qty=QTY, price=ENTRY_PX):
    """Actually BUY the basket through the broker, so it debits cash and holds real inventory.

    Seeding the store alone is not enough: the broker refuses to sell what it does not hold (as a
    venue would), so an exit test that skipped the entry would place sells that fill for nothing and
    "prove" a recycle that could not happen live.
    """
    await broker.place_multi_leg(
        strategy_id="arb_ri_senate", market_id=LEG_A,
        legs=[{"exchange_id": LEG_A, "side": "yes", "action": "buy", "quantity": qty, "price": price},
              {"exchange_id": LEG_B, "side": "yes", "action": "buy", "quantity": qty, "price": price}],
    )
    broker.placed.clear()      # drop the entry from the audit so _sells() reads only the exit


async def _open_basket(state, *, basket_id="b1", entry_cost=None, settlement_value=None):
    await state.open_basket(
        basket_id,
        strategy_id="arb_ri_senate",
        kind="hard_arb",
        legs=[
            {"exchange_id": LEG_A, "side": "yes", "quantity": QTY, "entry_price": ENTRY_PX},
            {"exchange_id": LEG_B, "side": "yes", "quantity": QTY, "entry_price": ENTRY_PX},
        ],
        entry_cost=entry_cost if entry_cost is not None else 2 * QTY * ENTRY_PX,   # 920.0
        settlement_value=settlement_value if settlement_value is not None else float(QTY),  # 1000.0
    )


def _stack(store, *, exit_cfg, books, alt_edge, buying_power=100_000.0, max_exits=1):
    """The SIM stack, hand-built so StateAdapter wraps the REAL store (build_sim_stack's adapter is
    storeless, and a storeless adapter has no price table — which correctly makes exits decline)."""
    cfg = _cfg()
    equity = 100_000.0
    broker = SimulatedBroker(buying_power=buying_power, equity=equity, track_cash=True)
    state = StateAdapter(store=store)
    risk = RiskGateAdapter(RiskGate(config=cfg), balance=equity)
    oms = OrderManagementSystem(
        broker=broker, state=state,
        strategy_contracts={"arb_ri_senate": LEG_A},
    )
    orch = Orchestrator(
        broker=broker, state=state, oms=oms, risk_gate=risk,
        sizing=SizingAdapter(Sizer(cfg), balance=equity),
        allocator=AllocatorAdapter(Allocator(cfg), capital=equity),
        tick_seconds=5.0, environment="sim",
        exit_config=exit_cfg,
        exit_books_provider=(lambda ids: {i: books[i] for i in ids if i in books}) if books else None,
        best_alternative_edge_provider=lambda: alt_edge,
        max_exits_per_cycle=max_exits,
    )
    return orch, broker, state, risk


def _sells(broker) -> list[dict]:
    return [p for p in broker.placed if str(p.get("action", "")).lower() == "sell"]


# --------------------------------------------------------------------------- #
# 1. THE HEADLINE: capital actually recycles
# --------------------------------------------------------------------------- #
def test_enter_exit_recycle_reenter_frees_capital_for_a_new_arb(tmp_path):
    """enter -> price corrects -> exit -> capital freed -> immediately redeployable.

    This is the whole point of the feature, so it asserts the MONEY, not just that an order was
    sent: buying power must rise by the sale proceeds, and the ledger's next cycle must see it.
    """
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        # 3% available elsewhere, 34 days to the wall -> recycling beats the small remainder
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.03, buying_power=1_000.0)
        await _enter(broker)                     # 2 x 1000 @ 0.46 = 920 spent
        await _open_basket(state)

        bp_before = (await broker.get_account()).buying_power
        assert bp_before == pytest.approx(80.0), "the entry consumed the capital (1000 - 920)"
        exited = await orch._run_exits()

        assert exited == 1, "a corrected basket with a better home for the cash must exit"
        sells = _sells(broker)
        assert len(sells) == 2, "both legs sold atomically, never one"
        assert {s["exchange_id"] for s in sells} == {LEG_A, LEG_B}
        assert all(s["quantity"] == QTY for s in sells)

        # THE MONEY: 2 legs x 1000 @ 0.49 = 980 back in the account.
        bp_after = (await broker.get_account()).buying_power
        assert bp_after == pytest.approx(bp_before + 980.0), "proceeds must return to buying power"

        # ...and the NEXT cycle's snapshot is what makes it redeployable.
        account = await broker.get_account()
        orch._ledger.begin_cycle(buying_power=account.buying_power, equity=account.equity)
        assert orch._ledger.available_buying_power() == pytest.approx(bp_before + 980.0)
        assert orch._ledger.reserve("next_arb", 900.0) == pytest.approx(900.0), \
            "freed capital must be reservable by a NEW arb"

        # the basket is closed, so it cannot be exited twice
        assert await state.read_open_baskets() == []
        assert await orch._run_exits() == 0
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 2. The deadlock guard — an exit must not need capital it does not have
# --------------------------------------------------------------------------- #
def test_exit_succeeds_while_fully_deployed(tmp_path):
    """Zero available buying power must NOT block an exit.

    The entry path reserves Sum(qty x price) and aborts when the reservation cannot be met. Routing
    exits through that logic would refuse them exactly when the bot is fully invested — the state
    this feature exists to escape. So: buying power 0, exit must still go.
    """
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        # exactly enough to enter, leaving ZERO dry powder — the state this feature exists to escape
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.03, buying_power=920.0)
        await _enter(broker)
        await _open_basket(state)
        account = await broker.get_account()
        assert account.buying_power == pytest.approx(0.0), "fully deployed"
        orch._ledger.begin_cycle(buying_power=account.buying_power, equity=account.equity)
        assert orch._ledger.available_buying_power() == 0.0

        assert await orch._run_exits() == 1, "a fully-deployed bot must still be able to exit"
        assert len(_sells(broker)) == 2
        assert (await broker.get_account()).buying_power == pytest.approx(980.0)
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 3. The gate must see real holdings, or it rejects every sell
# --------------------------------------------------------------------------- #
def test_risk_gate_is_handed_real_positions(tmp_path):
    """gate.check_order's sell branch rejects `sell_without_holding` on an empty positions map.

    The adapter used to pass {} unconditionally, which would have made every exit fail. Pinned from
    both directions: populated -> approved; empty -> rejected and nothing placed.
    """
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        orch, broker, state, risk = _stack(store, exit_cfg=load_exit_config(), books=books,
                                           alt_edge=0.03)
        await _open_basket(state)

        await orch._run_exits()
        assert risk.positions, "the cycle must refresh the gate's positions from state"
        assert risk._held(LEG_A) == QTY
        assert len(_sells(broker)) == 2

        # Now prove the negative: an empty positions map rejects the sell.
        risk.set_positions({})
        verdict = risk.validate_multi_leg(
            [{"exchange_id": LEG_A, "side": "yes", "action": "sell", "quantity": QTY, "price": 0.49}],
            is_exit=True,
        )
        assert not verdict.approved
        assert "sell_without_holding" in str(verdict.reason)
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 4. HOLD when there is nothing better — the rule is not "always exit"
# --------------------------------------------------------------------------- #
def test_holds_to_settlement_when_no_better_opportunity(tmp_path):
    """Same corrected basket, same deep book, but no alternative: the position must STAY.

    Exiting here would surrender a contracted remainder for cash with nowhere to go.
    """
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.0)
        await _open_basket(state)

        assert await orch._run_exits() == 0, "no alternative -> hold the guaranteed remainder"
        assert _sells(broker) == []
        still_open = await state.read_open_baskets()
        assert [b["basket_id"] for b in still_open] == ["b1"], "the basket must remain open"
        await store.close()

    asyncio.run(main())


def test_holds_when_the_book_is_too_thin(tmp_path):
    """A rich alternative does not help if the bids cannot absorb the basket."""
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: thin_book(0.49, 134)}
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.25)
        await _open_basket(state)

        assert await orch._run_exits() == 0
        assert _sells(broker) == [], "one unsellable leg holds the WHOLE basket (no naked leg)"
        await store.close()

    asyncio.run(main())


def test_holds_when_depth_is_unknown(tmp_path):
    """No book provider wired -> depth unknown -> hold. Unknown must never read as 'absorbable'."""
    async def main():
        store = await _store(tmp_path)
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=None,
                                        alt_edge=0.25)
        await _open_basket(state)
        assert await orch._run_exits() == 0
        assert _sells(broker) == []
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 5. Capacity: one alternative cannot absorb every basket at once
# --------------------------------------------------------------------------- #
def test_capacity_cap_limits_exits_per_cycle(tmp_path):
    """Three qualifying baskets, one alternative -> exit ONE.

    decide_exits judges each basket against the same alternative edge, i.e. it assumes unbounded
    capacity. Acting on all three would free 3x the capital into an opportunity that can take 1x,
    and the surplus would have surrendered a guaranteed remainder for idle cash.
    """
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.03, max_exits=1)
        for bid in ("b1", "b2", "b3"):
            await _open_basket(state, basket_id=bid)

        assert await orch._run_exits() == 1
        assert len(_sells(broker)) == 2, "one basket's two legs, not three baskets' six"
        assert len(await state.read_open_baskets()) == 2, "the other two stay open"
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 6. Off by default — an orchestrator without exit config behaves exactly as before
# --------------------------------------------------------------------------- #
def test_exits_are_disabled_without_exit_config(tmp_path):
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        orch, broker, state, _ = _stack(store, exit_cfg=None, books=books, alt_edge=0.99)
        await _open_basket(state)
        assert await orch._run_exits() == 0
        assert _sells(broker) == []
        await store.close()

    asyncio.run(main())


def test_exit_step_runs_inside_the_cycle_before_entries(tmp_path):
    """_run_cycle must actually CALL the exit pass — a decision module nothing invokes is inert."""
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.03)
        await _open_basket(state)

        order: list[str] = []
        real_exits = orch._run_exits

        async def spy_exits():
            order.append("exit")
            return await real_exits()

        orch._run_exits = spy_exits
        orig_due = orch._registry.due
        orch._registry.due = lambda now: (order.append("entries"), orig_due(now))[1]

        await orch._run_cycle()
        assert order == ["exit", "entries"], "exits run BEFORE entries so freed capital is visible"
        assert len(_sells(broker)) == 2
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 7. A dead feed must not sell into prices we just declared untrustworthy
# --------------------------------------------------------------------------- #
def test_dead_feed_holds_instead_of_exiting(tmp_path):
    async def main():
        store = await _store(tmp_path)
        books = {LEG_A: deep_book(0.49), LEG_B: deep_book(0.49)}
        orch, broker, state, _ = _stack(store, exit_cfg=load_exit_config(), books=books,
                                        alt_edge=0.25)
        await _open_basket(state)
        state.set_feed_dead(True)

        await orch._run_cycle()
        assert _sells(broker) == [], "dead feed -> hold; an exit is priced off the same dead book"
        await store.close()

    asyncio.run(main())
