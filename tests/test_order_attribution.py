"""Fill-attribution capture: broker buffer + durable order_attribution table + authoritative-first map."""
import asyncio
import tempfile
import os

from infra.state.store import StateStore
from infra.state.schema import init_state_db


def _fresh_store():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "state.db")
    init_state_db(db)
    return StateStore(db)


def test_record_and_read_attribution_map():
    async def go():
        s = _fresh_store()
        n = await s.record_order_attribution([
            ("7878274", "market_making", "948"),
            ("7878275", "cross_market_divergence", "885"),
        ])
        assert n == 2
        m = await s.read_attribution_map()
        assert m["7878274"] == "market_making"
        assert m["7878275"] == "cross_market_divergence"
        await s.close()
    asyncio.run(go())


def test_attribution_is_idempotent_first_writer_wins():
    async def go():
        s = _fresh_store()
        await s.record_order_attribution([("7878274", "market_making", "948")])
        # a replay with a different strategy must NOT overwrite (venue id's strategy is fixed)
        n = await s.record_order_attribution([("7878274", "sniper", "948")])
        assert n == 0
        m = await s.read_attribution_map()
        assert m["7878274"] == "market_making"
        await s.close()
    asyncio.run(go())


def test_record_skips_empty_order_id():
    async def go():
        s = _fresh_store()
        n = await s.record_order_attribution([("", "market_making", "948"), (None, "x", "1")])
        assert n == 0
        assert await s.read_attribution_map() == {}
        await s.close()
    asyncio.run(go())


def test_broker_buffer_note_and_drain():
    # exercise the buffer logic in isolation (no network, no SigBroker init)
    class _Buf:
        def __init__(self):
            self._attr_buf = []
        _note_attr = __import__("sig.broker", fromlist=["SigBroker"]).SigBroker._note_attr
        drain_attribution = __import__("sig.broker", fromlist=["SigBroker"]).SigBroker.drain_attribution

    b = _Buf()
    b._note_attr("7878274", "market_making", "948")
    b._note_attr(None, "x", "1")          # null order id -> skipped
    b._note_attr("7878275", None, None)   # null strategy -> 'unknown'
    out = b.drain_attribution()
    assert ("7878274", "market_making", "948", None) in out
    assert ("7878275", "unknown", "", None) in out
    assert all(t[0] for t in out)         # no null ids
    assert b.drain_attribution() == []    # drained clears
