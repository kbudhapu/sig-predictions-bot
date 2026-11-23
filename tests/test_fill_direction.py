"""2026-10-11: SIG fills carry no action; their quantity sign follows the OUTCOME for buys and sells alike. A NO sale
was stored as a NO buy -> the fresh-gap pilot's loss measure read -$4,015 for a real +$16 and falsely halted it.
The direction is now captured at placement (order_attribution.action) and fill ingest derives the true YES-delta."""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main_sig  # noqa: E402
from sig.engine.fresh_gap_live import fills_pnl  # noqa: E402


def test_yes_delta_from_action():
    f_no = {"side": "no", "quantity": -100}           # venue sign: NO -> negative, for buys AND sells
    f_yes = {"side": "yes", "quantity": 100}
    assert main_sig._fill_yes_delta(f_no, "buy") == -100
    assert main_sig._fill_yes_delta(f_no, "sell") == 100
    assert main_sig._fill_yes_delta(f_yes, "buy") == 100
    assert main_sig._fill_yes_delta(f_yes, "sell") == -100
    assert main_sig._fill_yes_delta(f_no, None) == -100      # unknown: raw venue sign (right for buys)


def test_pilot_round_trip_scores_its_real_result():
    """The live case: 1045 bought 1739 NO @0.575, sold 1505 @0.585 + 234 @0.58 -> about +$16, not -$4k."""
    buys = [{"exchange_id": "1045", "outcome": "no", "price": 0.575, "quantity": 1739,
             "signed_qty": main_sig._fill_yes_delta({"side": "no", "quantity": -1739}, "buy")}]
    sells = [{"exchange_id": "1045", "outcome": "no", "price": 0.585, "quantity": 1505,
              "signed_qty": main_sig._fill_yes_delta({"side": "no", "quantity": -1505}, "sell")},
             {"exchange_id": "1045", "outcome": "no", "price": 0.58, "quantity": 234,
              "signed_qty": main_sig._fill_yes_delta({"side": "no", "quantity": -234}, "sell")}]
    pnl = fills_pnl(buys + sells, {})
    assert abs(pnl - (1505 * 0.01 + 234 * 0.005)) < 0.01
    wrong = [dict(x, signed_qty=-x["quantity"]) for x in sells]           # the old (wrong) sign
    assert fills_pnl(buys + wrong, {}) < -1000                             # what tripped the false halt


def test_store_records_and_reads_actions_with_migration(tmp_path):
    from infra.state.schema import init_state_db
    from infra.state.store import StateStore
    db = str(tmp_path / "s.db")
    con = sqlite3.connect(db)                                              # an OLD database without the column
    con.execute("CREATE TABLE order_attribution (broker_order_id TEXT PRIMARY KEY, strategy TEXT NOT NULL, "
                "exchange_id TEXT, created_at TEXT NOT NULL)")
    con.commit()
    con.close()
    init_state_db(db)

    async def go():
        st = await StateStore(db).open()
        try:
            n = await st.record_order_attribution([("o1", "fresh_gap", "1045", "sell"), ("o2", "cross", "9")])
            return n, await st.read_attribution_actions(), await st.read_attribution_map()
        finally:
            await st.close()
    n, actions, smap = asyncio.run(go())
    assert n == 2 and actions == {"o1": "sell"} and smap["o2"] == "cross"


def test_broker_records_the_direction_of_a_placed_sell():
    from test_broker import FakeTransport, mk
    from sig.write_gate import HttpResult
    t = FakeTransport()
    t.script("POST", "/orders", [HttpResult(200, json.dumps({"id": "srv9", "status": "open"}))])
    b = mk(t, waits=[])
    asyncio.run(b.place_order(strategy_id="trim", market_id="m", exchange_id="1045", side="sell",
                              action="sell", quantity=10, price=0.58, outcome="no"))
    rows = b.drain_attribution()
    assert rows and rows[0][0] == "srv9" and rows[0][3] == "sell"


def _fill(fid, oid, outcome, q, signed):
    return {"fill_id": fid, "order_id": oid, "strategy": "fresh_gap", "exchange_id": "842", "outcome": outcome,
            "price": 0.57, "quantity": q, "signed_qty": signed, "filled_at": "2026-10-13T10:27:03Z", "mid_at_fill": None}


def test_resign_repairs_fills_signed_before_their_action_was_flushed(tmp_path):
    """2026-10-16 live: three pilot SELLS ingested 12s before their attribution (action) was flushed -> stored
    with the venue's buy-direction sign -> loss measure -$9,781 for a real ~+$100 -> false halt."""
    from infra.state.schema import init_state_db
    from infra.state.store import StateStore
    db = str(tmp_path / "s.db")
    init_state_db(db)

    async def go():
        st = await StateStore(db).open()
        try:
            await st.append_strategy_fills([
                _fill("f1", "sellY", "yes", 3603.0, 3603.0),      # sell YES stored as +  (wrong)
                _fill("f2", "sellN", "no", 3540.0, -3540.0),      # sell NO stored as -   (wrong)
                _fill("f3", "buyY", "yes", 100.0, 100.0),         # buy YES +             (right)
                _fill("f4", "unknown", "yes", 50.0, 50.0)])       # no action recorded: untouched
            await st.record_order_attribution([("sellY", "fresh_gap", "842", "sell"), ("sellN", "fresh_gap", "843", "sell"),
                                               ("buyY", "fresh_gap", "842", "buy")])
            await st.resign_fills_from_actions()
            return {r["fill_id"]: r["signed_qty"] for r in await st._read("SELECT fill_id, signed_qty FROM strategy_fills", ())}
        finally:
            await st.close()
    got = asyncio.run(go())
    assert got == {"f1": -3603.0, "f2": 3540.0, "f3": 100.0, "f4": 50.0}


def test_ingest_flushes_pending_directions_before_signing(tmp_path, monkeypatch):
    """The race itself: the fill is ingested while its order's direction is still only in the broker's buffer."""
    from infra.state.schema import init_state_db
    from infra.state.store import StateStore
    db = str(tmp_path / "s.db")
    init_state_db(db)

    class Client:
        def portfolio_fills(self, slug=None, limit=200, cursor=None):
            return {"fills": [{"id": "x1", "orderId": "o9", "exchangeId": "842", "side": "yes", "price": 0.57,
                               "quantity": 3603, "filledAt": "2026-10-13T10:27:03Z"}]}

    class Broker:
        def __init__(self):
            self.buf = [("o9", "fresh_gap", "842", "sell")]

        def drain_attribution(self):
            b, self.buf = self.buf, []
            return b
    stop = asyncio.Event()
    real_sleep = asyncio.sleep
    n = {"k": 0}

    async def fast_sleep(s):
        n["k"] += 1
        if n["k"] > 1:
            stop.set()
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)

    async def go():
        st = await StateStore(db).open()
        try:
            await main_sig._fill_ingest_loop(store=st, client=Client(), stop_event=stop, interval_s=30, broker=Broker())
            return [(r["signed_qty"], r["strategy"]) for r in await st._read("SELECT signed_qty, strategy FROM strategy_fills", ())]
        finally:
            await st.close()
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(go()) == [(-3603.0, "fresh_gap")]
    finally:
        loop.close()
