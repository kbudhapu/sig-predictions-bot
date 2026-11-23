"""The MM's price pump must NOT be blocked by the slow order-book depth poll.

Observed live (2026-10-02): order-flow depth was polled INSIDE the price-pump loop — ~20 serial
venue reads at ~4-8s each. That blew the loop's period out from 5s to minutes, with two visible
consequences:

    mm_market_selection  configured=474 quotable=0 stale_skipped=237   (snapshot frozen >10min)
    dead_man_heartbeat_stale  age_seconds=49.9  threshold=10.0         (watchdog pet starved)

The MM quotes only rows fresher than market_making.max_quote_age_s, so every second the pump is
blocked is added to the apparent age of EVERY price it sees — a slow depth poll silently switches
market-making off. Depth now has its own task.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
import yaml

import main_sig


class _Store:
    def __init__(self, rows): self._rows = rows; self.reads = 0
    async def read_prices(self):
        self.reads += 1
        return dict(self._rows)


class _MM:
    def __init__(self): self.snapshots = 0
    def set_prices(self, prices): self.snapshots += 1


class _OF:
    def __init__(self): self.prices = 0; self.books = 0
    def set_prices(self, prices): self.prices += 1
    def set_books(self, books): self.books += 1


# --------------------------------------------------------------------------- #
# The regression itself
# --------------------------------------------------------------------------- #
def test_price_pump_does_not_poll_order_books():
    """Structural: the depth poll must not be reachable from the price pump. This is the actual bug
    — a behavioural test can only catch it by waiting out a real slow poll."""
    src = inspect.getsource(main_sig._price_and_watchdog_pump)
    assert "_poll_orderbooks" not in src, (
        "the depth poll is back inside the price pump: a slow venue read will freeze the MM's "
        "price snapshot and starve the systemd watchdog")
    assert "set_books" not in src


def test_order_book_pump_is_its_own_task_that_does_poll_depth():
    src = inspect.getsource(main_sig._orderbook_pump)
    assert "_poll_orderbooks" in src and "set_books" in src


@pytest.mark.asyncio
async def test_price_pump_keeps_pumping_while_depth_is_slow():
    """A depth poll that takes far longer than the price interval must not reduce the MM's refresh
    count — the two loops are independent."""
    store, mm, of = _Store({"1": {"best_bid": 0.4, "best_ask": 0.6}}), _MM(), _OF()

    async def _slow_books(client, ids):
        await asyncio.sleep(10.0)          # a depth poll that outlives the whole test
        return {"1": {"bids": [], "asks": []}}

    orig = main_sig._poll_orderbooks
    main_sig._poll_orderbooks = _slow_books
    try:
        price = asyncio.create_task(main_sig._price_and_watchdog_pump(
            store=store, mm=mm, client=None, exchange_ids=["1"],
            interval_s=1.0, max_ids_per_call=50, order_flow=of))
        books = asyncio.create_task(main_sig._orderbook_pump(
            order_flow=of, of_client=object(), exchange_ids=["1"],
            interval_s=1.0, of_max_markets=1))
        await asyncio.sleep(0.25)
        for t in (price, books):
            t.cancel()
        for t in (price, books):
            try:
                await t
            except asyncio.CancelledError:
                pass
    finally:
        main_sig._poll_orderbooks = orig

    assert mm.snapshots >= 1, "the MM never got a price snapshot while depth was polling"
    assert of.books == 0, "the slow depth poll should still be in flight, not applied"


@pytest.mark.asyncio
async def test_depth_pump_is_a_noop_when_order_flow_is_off():
    """No order-flow / no client / no ids ⇒ return immediately rather than spin."""
    await asyncio.wait_for(
        main_sig._orderbook_pump(order_flow=None, of_client=None, exchange_ids=["1"],
                                 interval_s=1.0, of_max_markets=1), timeout=1.0)
    await asyncio.wait_for(
        main_sig._orderbook_pump(order_flow=_OF(), of_client=object(), exchange_ids=[],
                                 interval_s=1.0, of_max_markets=0), timeout=1.0)


def test_live_config_sets_the_depth_cadence():
    of = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["order_flow"]
    assert float(of["book_poll_interval_s"]) >= 15.0, (
        "depth is ~max_markets serial venue reads; polling it faster than this just burns the "
        "100-reads/min budget")
