"""Position visibility, mark-to-market and open-basket persistence.

WHAT THIS SUITE IS DEFENDING
The bot enters multi-leg baskets atomically and then forgets them: `positions` is the NETTED book
keyed by exchange_id (so it cannot express "these four legs are one trade"), `positions.unrealized_pnl`
was hardcoded 0.0 by every writer, and the only record of basket identity lived in the OMS's
in-memory `_pending` dict — which pops a bundle the instant it becomes a fully-filled position worth
exiting. Nothing could see what was open, what it was worth now, or which legs belonged together.

These tests pin the data layer that fixes that, and in particular pin the two ways it is allowed to
answer "I don't know": a missing or unusable price must leave the previous mark ALONE, because a
0.0 written into unrealized_pnl is indistinguishable from "this basket lost everything" and is the
number most likely to trip a spurious exit.

Like tests/test_state_store.py, every test redirects the data root through SIG_DATA_DIR (the seam
infra/config.py documents) so a run never touches the real data/ dir.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def state_mod(tmp_path, monkeypatch):
    """Reload the state package against a tmp SIG_DATA_DIR, since DATA_DIR/STATE_DB_PATH are
    module-level constants resolved at import time."""
    monkeypatch.setenv("SIG_DATA_DIR", str(tmp_path))
    import infra.config as cfg

    importlib.reload(cfg)
    import infra.state.schema as schema

    importlib.reload(schema)
    import infra.state.store as store_mod

    importlib.reload(store_mod)
    import infra.state as state_pkg

    importlib.reload(state_pkg)
    assert str(tmp_path) in str(state_pkg.STATE_DB_PATH)
    return state_pkg


async def _open(state_mod):
    return await state_mod.StateStore().open()


def _iso(dt):
    return dt.isoformat()


def _ago(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


# --------------------------------------------------------------------------- #
# Orchestrator snapshot
# --------------------------------------------------------------------------- #
def _make_orch(state):
    """Orchestrator with only its state object wired.

    `__init__` merely assigns its collaborators (and builds a watchdog we never start), so the
    other seams can be None: this suite is about `_read_state_snapshot` and nothing else.
    """
    from sig.engine.orchestrator import Orchestrator

    return Orchestrator(
        broker=None,
        state=state,
        oms=None,
        risk_gate=None,
        sizing=None,
        allocator=None,
        tick_seconds=0.01,
        environment="sim",
        lock_path="/tmp/test_sig_position_visibility.lock",
    )


def test_snapshot_carries_per_position_data_and_open_baskets(state_mod):
    """The snapshot a strategy's evaluate() receives must name positions individually.

    Aggregate exposure is enough to decide how much MORE to deploy; it is not enough to decide to
    close anything, because it cannot say which exchange holds what at what cost.
    """

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_YES", quantity=40.0, avg_price=0.42)
            await store.upsert_position("EX_NO", quantity=40.0, avg_price=0.51)
            await store.upsert_price("EX_YES", best_bid=0.45, best_ask=0.47)
            await store.upsert_price("EX_NO", best_bid=0.53, best_ask=0.55)
            await store.open_basket(
                "bundle_1",
                strategy_id="hard_arb_v1",
                kind="hard_arb",
                legs=[
                    {"exchange_id": "EX_YES", "side": "buy", "quantity": 40.0, "entry_price": 0.42},
                    {"exchange_id": "EX_NO", "side": "buy", "quantity": 40.0, "entry_price": 0.51},
                ],
                entry_cost=37.2,
                settlement_value=40.0,
            )
            await store.mark_to_market()

            orch = _make_orch(store)
            snap = await orch._read_state_snapshot()

            # exposure keeps its existing meaning and shape (the real store's Exposure object)
            assert snap["exposure"].n_positions == 2

            positions = snap["positions"]
            assert set(positions) == {"EX_YES", "EX_NO"}
            assert positions["EX_YES"]["exchange_id"] == "EX_YES"
            assert positions["EX_YES"]["quantity"] == pytest.approx(40.0)
            assert positions["EX_YES"]["avg_price"] == pytest.approx(0.42)
            # marked, not the hardcoded 0.0 the column used to hold forever
            assert positions["EX_YES"]["unrealized_pnl"] == pytest.approx(40.0 * (0.45 - 0.42))
            assert positions["EX_YES"]["updated_at"]

            assert snap["prices"]["EX_NO"]["best_bid"] == pytest.approx(0.53)

            assert len(snap["baskets"]) == 1
            basket = snap["baskets"][0]
            assert basket["basket_id"] == "bundle_1"
            assert basket["status"] == "open"
            assert basket["entry_cost"] == pytest.approx(37.2)
            assert basket["settlement_value"] == pytest.approx(40.0)
            assert [leg["exchange_id"] for leg in basket["legs"]] == ["EX_YES", "EX_NO"]
        finally:
            await store.close()

    asyncio.run(main())


def test_snapshot_still_works_on_minimal_state_double(state_mod):
    """REGRESSION GUARD: `self._state` is sometimes the lightweight StateAdapter or a test double
    that implements only `read_exposure`. The added reads are best-effort (getattr + swallowed
    exception, exactly like `_read_stale_markets`), so a minimal double must still get the old
    single-key snapshot rather than an AttributeError mid-cycle."""

    class MinimalState:
        async def read_exposure(self):
            return {"gross": 123.0}

    class ExplodingState(MinimalState):
        """Implements the new methods but raises — a half-wired store must not kill the cycle."""

        async def read_positions(self):
            raise RuntimeError("no positions table here")

        async def read_open_baskets(self):
            raise RuntimeError("no baskets table here")

    async def main():
        for state in (MinimalState(), ExplodingState()):
            snap = await _make_orch(state)._read_state_snapshot()
            assert snap["exposure"] == {"gross": 123.0}
            assert "positions" not in snap
            assert "baskets" not in snap

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# Mark-to-market
# --------------------------------------------------------------------------- #
def test_mark_to_market_values_a_long_at_the_bid(state_mod):
    """A long is exited by SELLING, and a seller is filled at the BID. Marking at the ask or the
    mid would book spread the book cannot realise."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_A", quantity=100.0, avg_price=0.40)
            await store.upsert_price("EX_A", best_bid=0.44, best_ask=0.49)

            report = await store.mark_to_market()

            expected = 100.0 * (0.44 - 0.40)          # bid, NOT the 0.465 mid
            assert report.marked["EX_A"] == pytest.approx(expected)
            assert report.skipped == {}
            row = await store.read_position("EX_A")
            assert row["unrealized_pnl"] == pytest.approx(expected)
            # the mid would have been materially more generous — that gap is the whole point
            assert expected < 100.0 * (0.465 - 0.40)
        finally:
            await store.close()

    asyncio.run(main())


def test_mark_to_market_values_a_short_at_the_ask(state_mod):
    """Mirror case: a short is closed by BUYING BACK, and a buyer pays the ASK."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_S", quantity=-50.0, avg_price=0.60)
            await store.upsert_price("EX_S", best_bid=0.52, best_ask=0.57)

            report = await store.mark_to_market()

            expected = -50.0 * (0.57 - 0.60)          # = 50 * (0.60 - 0.57), a gain
            assert report.marked["EX_S"] == pytest.approx(expected)
            assert expected > 0
            row = await store.read_position("EX_S")
            assert row["unrealized_pnl"] == pytest.approx(expected)
        finally:
            await store.close()

    asyncio.run(main())


def test_mark_to_market_does_not_zero_a_position_with_no_price_row(state_mod):
    """THE IMPORTANT ONE. An exchange the feed has never reported must keep its previous mark.

    Writing 0.0 would read as "this position lost its entire value" and could trigger an exit on a
    gap in the price feed rather than on a move in the market.
    """

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_DARK", quantity=25.0, avg_price=0.30, unrealized_pnl=-2.5)
            # deliberately no price row for EX_DARK
            await store.upsert_position("EX_LIT", quantity=10.0, avg_price=0.20)
            await store.upsert_price("EX_LIT", best_bid=0.25, best_ask=0.26)

            report = await store.mark_to_market()

            assert report.skipped["EX_DARK"] == "no_price_row"
            assert "EX_DARK" not in report.marked
            row = await store.read_position("EX_DARK")
            assert row["unrealized_pnl"] == pytest.approx(-2.5)   # untouched, NOT 0.0
            # and the refusal is local: the priced position is still marked in the same pass
            assert report.marked["EX_LIT"] == pytest.approx(10.0 * (0.25 - 0.20))
        finally:
            await store.close()

    asyncio.run(main())


def test_mark_to_market_does_not_zero_a_position_with_a_null_bid(state_mod):
    """A price row can exist with the side we need missing (one-sided book, partial quote). That is
    'unknown', which is not zero — same rule as a missing row."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_ONE_SIDED", quantity=30.0, avg_price=0.35,
                                        unrealized_pnl=1.75)
            await store.upsert_price("EX_ONE_SIDED", best_bid=None, best_ask=0.40)

            report = await store.mark_to_market()

            assert report.skipped["EX_ONE_SIDED"] == "null_bid"
            assert "EX_ONE_SIDED" not in report.marked
            row = await store.read_position("EX_ONE_SIDED")
            assert row["unrealized_pnl"] == pytest.approx(1.75)   # untouched, NOT 0.0
        finally:
            await store.close()

    asyncio.run(main())


def test_mark_to_market_skips_a_stale_price_and_keeps_the_previous_mark(state_mod):
    """A quote too old to trade on is the same error class as no quote: refuse, don't revalue.
    Matches `is_price_stale` — unknown/old age is never treated as fresh."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_OLD", quantity=20.0, avg_price=0.50, unrealized_pnl=-4.0)
            await store.upsert_price("EX_OLD", best_bid=0.10, best_ask=0.12,
                                     updated_at=_ago(3600))

            report = await store.mark_to_market(max_price_age_seconds=15.0)
            assert report.skipped["EX_OLD"] == "stale_price"
            row = await store.read_position("EX_OLD")
            assert row["unrealized_pnl"] == pytest.approx(-4.0)   # the stale 0.10 bid was ignored

            # without a staleness budget the same pass DOES mark it — the gate is the caller's call
            report2 = await store.mark_to_market()
            assert report2.marked["EX_OLD"] == pytest.approx(20.0 * (0.10 - 0.50))
        finally:
            await store.close()

    asyncio.run(main())


def test_mark_to_market_never_touches_realized_pnl(state_mod):
    """realized_pnl is a cash fact, not a valuation. A flat position is also genuinely worth 0.0
    unrealized — that zero is computed, not a fallback."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_R", quantity=10.0, avg_price=0.50, realized_pnl=12.5)
            await store.upsert_price("EX_R", best_bid=0.55, best_ask=0.56)
            await store.upsert_position("EX_FLAT", quantity=0.0, avg_price=0.0, realized_pnl=-3.0)

            report = await store.mark_to_market()

            assert (await store.read_position("EX_R"))["realized_pnl"] == pytest.approx(12.5)
            assert (await store.read_position("EX_FLAT"))["realized_pnl"] == pytest.approx(-3.0)
            assert report.marked["EX_FLAT"] == pytest.approx(0.0)
            assert "EX_FLAT" not in report.skipped
        finally:
            await store.close()

    asyncio.run(main())


def test_a_later_position_update_does_not_wipe_the_mark(state_mod):
    """A mark is worthless if the next fill erases it.

    The OMS writes a position record on every fill and every reconciliation. With `unrealized_pnl`
    defaulting to 0.0 that write reset the mark (and realised cash) each time, so the column would
    have gone back to the all-zeros state this whole change exists to end. None now means
    'leave as is'; only an explicit number changes an accumulator.
    """

    async def main():
        store = await _open(state_mod)
        try:
            await store.upsert_position("EX_A", quantity=100.0, avg_price=0.40, realized_pnl=9.0)
            await store.upsert_price("EX_A", best_bid=0.44, best_ask=0.49)
            await store.mark_to_market()
            marked = (await store.read_position("EX_A"))["unrealized_pnl"]
            assert marked == pytest.approx(100.0 * (0.44 - 0.40))

            # a subsequent broker-truth position write (new fill) must not clobber either number
            await store.upsert_position("EX_A", quantity=120.0, avg_price=0.41)
            row = await store.read_position("EX_A")
            assert row["quantity"] == pytest.approx(120.0)
            assert row["avg_price"] == pytest.approx(0.41)
            assert row["unrealized_pnl"] == pytest.approx(marked)
            assert row["realized_pnl"] == pytest.approx(9.0)

            # a brand-new row still starts at 0.0 for both
            await store.upsert_position("EX_NEW", quantity=1.0, avg_price=0.5)
            fresh = await store.read_position("EX_NEW")
            assert fresh["realized_pnl"] == 0.0
            assert fresh["unrealized_pnl"] == 0.0

            # and an explicit value still overwrites
            await store.upsert_position("EX_A", quantity=120.0, avg_price=0.41, realized_pnl=11.0)
            assert (await store.read_position("EX_A"))["realized_pnl"] == pytest.approx(11.0)
        finally:
            await store.close()

    asyncio.run(main())


def test_unrealized_pnl_for_returns_none_not_zero_when_the_book_side_is_missing(state_mod):
    """The pure valuation function's contract, stated directly: None means UNKNOWN."""
    f = state_mod.unrealized_pnl_for
    assert f(quantity=10.0, avg_price=0.5, best_bid=None, best_ask=0.6) is None
    assert f(quantity=-10.0, avg_price=0.5, best_bid=0.4, best_ask=None) is None
    assert f(quantity=0.0, avg_price=0.5, best_bid=None, best_ask=None) == 0.0
    # a long ignores the ask entirely, and vice versa
    assert f(quantity=10.0, avg_price=0.5, best_bid=0.6, best_ask=99.0) == pytest.approx(1.0)
    assert f(quantity=-10.0, avg_price=0.5, best_bid=-99.0, best_ask=0.4) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Baskets
# --------------------------------------------------------------------------- #
def test_basket_round_trip_open_read_close(state_mod):
    """open_basket -> read_open_baskets (legs parsed) -> close_basket drops it from the open list."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.open_basket(
                "b1",
                strategy_id="hard_arb_v1",
                kind="hard_arb",
                legs=[{"exchange_id": "EX_1", "side": "buy", "quantity": 10.0, "entry_price": 0.30}],
                entry_cost=3.0,
                settlement_value=10.0,
            )

            open_now = await store.read_open_baskets()
            assert [b["basket_id"] for b in open_now] == ["b1"]
            assert open_now[0]["legs"] == [
                {"exchange_id": "EX_1", "side": "buy", "quantity": 10.0, "entry_price": 0.30}
            ]
            assert open_now[0]["status"] == "open"
            assert open_now[0]["closed_at"] is None
            assert open_now[0]["realized_pnl"] is None

            assert await store.close_basket("b1", realized_pnl=7.0) is True
            assert await store.read_open_baskets() == []

            # the row survives as realised history; it is not deleted
            closed = await store.read_basket("b1")
            assert closed["status"] == "closed"
            assert closed["realized_pnl"] == pytest.approx(7.0)
            assert closed["closed_at"]
        finally:
            await store.close()

    asyncio.run(main())


def test_basket_legs_are_recoverable_as_one_group(state_mod):
    """THE POINT OF THE TABLE. The entry was atomic, so the exit is per-basket.

    `positions` is keyed by exchange_id and nets everything together, so it can say "we hold 40 of
    EX_YES" but never "those 40 are one leg of this four-leg arb that cost 37.2 and settles at 40".
    Two concurrent baskets must stay separable, and each must come back whole.
    """

    async def main():
        store = await _open(state_mod)
        try:
            legs_a = [
                {"exchange_id": f"EX_A{i}", "side": "buy", "quantity": 40.0, "entry_price": 0.24}
                for i in range(4)
            ]
            legs_b = [
                {"exchange_id": "EX_B0", "side": "buy", "quantity": 15.0, "entry_price": 0.61},
                {"exchange_id": "EX_B1", "side": "buy", "quantity": 15.0, "entry_price": 0.33},
            ]
            await store.open_basket("basket_a", strategy_id="s1", kind="hard_arb", legs=legs_a,
                                    entry_cost=38.4, settlement_value=40.0,
                                    opened_at=_ago(600))
            await store.open_basket("basket_b", strategy_id="s2", kind="soft_mispricing",
                                    legs=legs_b, entry_cost=14.1, settlement_value=15.0,
                                    opened_at=_ago(60))
            # netted positions cannot express the grouping — some legs even share an exchange
            for leg in legs_a + legs_b:
                await store.upsert_position(leg["exchange_id"], quantity=leg["quantity"],
                                            avg_price=leg["entry_price"])

            baskets = await store.read_open_baskets()
            # oldest first: capital locked longest is what an exit rule looks at first
            assert [b["basket_id"] for b in baskets] == ["basket_a", "basket_b"]

            by_id = {b["basket_id"]: b for b in baskets}
            assert len(by_id["basket_a"]["legs"]) == 4
            assert len(by_id["basket_b"]["legs"]) == 2
            assert {leg["exchange_id"] for leg in by_id["basket_a"]["legs"]} == {
                "EX_A0", "EX_A1", "EX_A2", "EX_A3"
            }
            # the guaranteed settlement value minus what was paid IS the locked-in arb profit
            assert by_id["basket_a"]["settlement_value"] - by_id["basket_a"]["entry_cost"] == (
                pytest.approx(1.6)
            )
            assert by_id["basket_b"]["kind"] == "soft_mispricing"

            # closing one leaves the other entirely alone
            await store.close_basket("basket_a", realized_pnl=1.0)
            still_open = await store.read_open_baskets()
            assert [b["basket_id"] for b in still_open] == ["basket_b"]
            assert len(still_open[0]["legs"]) == 2
        finally:
            await store.close()

    asyncio.run(main())


def test_close_basket_is_a_one_way_transition(state_mod):
    """Two exit decisions on the same basket must not both believe they closed it — otherwise the
    second one's realized_pnl silently overwrites the first's."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.open_basket("b1", strategy_id="s", kind="hard_arb",
                                    legs=[{"exchange_id": "EX_1", "side": "buy",
                                           "quantity": 1.0, "entry_price": 0.5}],
                                    entry_cost=0.5, settlement_value=1.0)
            assert await store.close_basket("b1", realized_pnl=0.5) is True
            assert await store.close_basket("b1", realized_pnl=999.0) is False
            assert (await store.read_basket("b1"))["realized_pnl"] == pytest.approx(0.5)
            assert await store.close_basket("never_existed", realized_pnl=1.0) is False
        finally:
            await store.close()

    asyncio.run(main())


def test_open_basket_normalises_oms_contract_id_legs(state_mod):
    """The OMS's OrderSlice names the identifier `contract_id`; the state layer keys everything by
    `exchange_id`. Normalising at the boundary means no reader has to guess which key a basket used.
    Extra per-leg fields the caller attached survive."""

    async def main():
        store = await _open(state_mod)
        try:
            await store.open_basket(
                "b1",
                strategy_id="s",
                kind="hard_arb",
                legs=[{"contract_id": "YES_AZ", "side": "buy", "quantity": 5,
                       "entry_price": 0.44, "leg_id": "leg_0"}],
                entry_cost=2.2,
                settlement_value=5.0,
            )
            leg = (await store.read_open_baskets())[0]["legs"][0]
            assert leg["exchange_id"] == "YES_AZ"
            assert leg["quantity"] == pytest.approx(5.0)
            assert leg["leg_id"] == "leg_0"

            with pytest.raises(ValueError):
                await store.open_basket("b2", strategy_id="s", kind="hard_arb",
                                        legs=[{"side": "buy", "quantity": 1}],
                                        entry_cost=0.5, settlement_value=1.0)
        finally:
            await store.close()

    asyncio.run(main())


def test_open_basket_is_idempotent_on_basket_id(state_mod):
    """A retried entry-completion path must refresh the basket, never create a second claim on the
    same legs (which would double-count locked capital)."""

    async def main():
        store = await _open(state_mod)
        try:
            legs = [{"exchange_id": "EX_1", "side": "buy", "quantity": 10.0, "entry_price": 0.3}]
            await store.open_basket("b1", strategy_id="s", kind="hard_arb", legs=legs,
                                    entry_cost=3.0, settlement_value=10.0)
            await store.open_basket("b1", strategy_id="s", kind="hard_arb", legs=legs,
                                    entry_cost=3.1, settlement_value=10.0)
            open_now = await store.read_open_baskets()
            assert len(open_now) == 1
            assert open_now[0]["entry_cost"] == pytest.approx(3.1)
        finally:
            await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# SIM stack pass-through
# --------------------------------------------------------------------------- #
def test_state_adapter_carries_baskets_and_positions_for_the_sim_stack():
    """StateAdapter is what SIM and most engine tests run on. It must answer the same three
    questions, in the same shapes, so a consumer does not branch on which state object it holds."""
    from sig.engine.composition import StateAdapter

    async def main():
        adapter = StateAdapter()                      # no real store wired: pure in-memory SIM
        await adapter.upsert_position({"strategy_id": "s1", "contract_id": "YES_AZ",
                                       "qty": 12.0, "side": "long", "avg_entry_price": 0.40})

        positions = await adapter.read_positions()
        assert positions["YES_AZ"]["quantity"] == pytest.approx(12.0)
        assert positions["YES_AZ"]["avg_price"] == pytest.approx(0.40)
        assert positions["YES_AZ"]["unrealized_pnl"] == 0.0     # no price table in SIM: honest 0
        assert await adapter.read_prices() == {}

        await adapter.open_basket("b1", strategy_id="s1", kind="hard_arb",
                                  legs=[{"exchange_id": "YES_AZ", "side": "buy",
                                         "quantity": 12.0, "entry_price": 0.40}],
                                  entry_cost=4.8, settlement_value=12.0)
        open_now = await adapter.read_open_baskets()
        assert [b["basket_id"] for b in open_now] == ["b1"]
        assert open_now[0]["legs"][0]["exchange_id"] == "YES_AZ"
        assert open_now[0]["settlement_value"] == pytest.approx(12.0)

        assert await adapter.close_basket("b1", realized_pnl=0.5) is True
        assert await adapter.read_open_baskets() == []
        assert await adapter.close_basket("b1", realized_pnl=0.5) is False

        # exposure keeps its own (per-strategy) meaning — unchanged by any of the above
        assert await adapter.read_exposure() == {
            "s1": {"qty": 12.0, "side": "long", "avg_entry_price": 0.40}
        }

    asyncio.run(main())


def test_state_adapter_actually_reaches_the_real_positions_table(state_mod):
    """The OMS speaks position DICTS; StateStore.upsert_position is (exchange_id, *, quantity=...).

    Forwarding the dict positionally raised TypeError on every call and `_try` swallowed it, so with
    a real store wired the `positions` table stayed empty forever — and an empty table is exactly
    what an exit rule would read as "nothing is open". This pins the translation.
    """
    from sig.engine.composition import StateAdapter

    async def main():
        store = await _open(state_mod)
        try:
            adapter = StateAdapter(store=store)
            await adapter.upsert_position({"strategy_id": "s1", "contract_id": "yes_az",
                                           "qty": 12.0, "side": "long", "avg_entry_price": 0.40})

            row = await store.read_position("YES_AZ")
            assert row is not None, "the OMS position never reached the real positions table"
            assert row["quantity"] == pytest.approx(12.0)
            assert row["avg_price"] == pytest.approx(0.40)

            # ...and it is therefore markable, which is the whole reason this matters
            await store.upsert_price("YES_AZ", best_bid=0.46, best_ask=0.48)
            report = await store.mark_to_market()
            assert report.marked["YES_AZ"] == pytest.approx(12.0 * (0.46 - 0.40))

            # read_positions now prefers the real store's rows (truth) over the in-memory view
            positions = await adapter.read_positions()
            assert positions["YES_AZ"]["unrealized_pnl"] == pytest.approx(12.0 * (0.46 - 0.40))
        finally:
            await store.close()

    asyncio.run(main())


def test_orchestrator_snapshot_through_the_state_adapter():
    """End of the wire: the SIM stack's snapshot carries the new keys too."""
    from sig.engine.composition import StateAdapter

    async def main():
        adapter = StateAdapter()
        await adapter.upsert_position({"strategy_id": "s1", "contract_id": "YES_AZ",
                                       "qty": 7.0, "side": "long", "avg_entry_price": 0.25})
        await adapter.open_basket("b1", strategy_id="s1", kind="directional",
                                  legs=[{"exchange_id": "YES_AZ", "side": "buy",
                                         "quantity": 7.0, "entry_price": 0.25}],
                                  entry_cost=1.75, settlement_value=7.0)

        snap = await _make_orch(adapter)._read_state_snapshot()
        assert snap["exposure"]["s1"]["qty"] == pytest.approx(7.0)   # shape preserved
        assert snap["positions"]["YES_AZ"]["quantity"] == pytest.approx(7.0)
        assert [b["basket_id"] for b in snap["baskets"]] == ["b1"]

    asyncio.run(main())
