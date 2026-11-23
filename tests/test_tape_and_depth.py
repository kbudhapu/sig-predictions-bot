"""Microstructure recorders (2026-10-06): top-of-book depth in mid_history + the slow SIG trade-tape poller."""
from __future__ import annotations

import asyncio
import sqlite3

from infra.state.store import StateStore


def test_mid_history_accepts_depth_and_legacy_rows(tmp_path):
    async def _run():
        st = await StateStore(tmp_path / "d.db").open()
        try:
            await st.append_mid_history([("1", 0.5025, "2026-10-06T00:00:00+00:00", 0.50, 0.505, 3000, 4100),
                                         ("2", 0.25, "2026-10-06T00:00:00+00:00")])
        finally:
            await st.close()
    asyncio.run(_run())
    rows = sqlite3.connect(tmp_path / "d.db").execute(
        "select exchange_id, mid, bid, ask, bid_size, ask_size from mid_history order by exchange_id").fetchall()
    assert rows == [("1", 0.5025, 0.5, 0.505, 3000.0, 4100.0), ("2", 0.25, None, None, None, None)]


def test_mid_history_depth_columns_migrate_onto_old_table(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE mid_history (exchange_id TEXT NOT NULL, ts TEXT NOT NULL, mid REAL NOT NULL, "
              "PRIMARY KEY (exchange_id, ts))")
    c.commit(); c.close()

    async def _run():
        st = await StateStore(db).open()
        try:
            await st.append_mid_history([("1", 0.5, "t", 0.495, 0.505, 10, 20)])
        finally:
            await st.close()
    asyncio.run(_run())
    assert sqlite3.connect(db).execute("select bid_size from mid_history").fetchone() == (10.0,)


T1 = {"id": "66102791", "createdAt": "2026-10-06T09:56:21.600Z", "price": 0.865, "size": 300, "side": "YES"}
T2 = {"id": "66102792", "createdAt": "2026-10-06T09:57:00.000Z", "price": 0.86, "size": 50, "side": "NO"}


def test_append_trades_idempotent_and_latest(tmp_path):
    async def _run():
        st = await StateStore(tmp_path / "t.db").open()
        try:
            await st.append_trades("1076", [T1, T2], "now")
            await st.append_trades("1076", [T2], "later")             # overlap on resume -> ignored
            await st.append_trades("1076", [{"price": 0.5}], "x")    # no createdAt -> skipped
            return await st.latest_trade_ts()
        finally:
            await st.close()
    latest = asyncio.run(_run())
    assert latest == {"1076": "2026-10-06T09:57:00.000Z"}
    assert sqlite3.connect(tmp_path / "t.db").execute("select count(*), sum(size) from trade_tape").fetchone() == (2, 350.0)


class _Client:
    def __init__(self, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def exchange_trades(self, ex, *, limit, since_iso=None):
        from sig.sig_client import SigAPIError
        self.calls.append((ex, since_iso))
        if self.fail_first and len(self.calls) == 1:
            raise SigAPIError(500, "/x")
        return {"data": [T1] if ex == "1076" else []}


def test_tape_loop_cycles_markets_resumes_and_survives_errors(tmp_path, monkeypatch):
    import main_sig

    async def _run():
        st = await StateStore(tmp_path / "l.db").open()
        stop = asyncio.Event()
        cl = _Client(fail_first=True)
        task = asyncio.create_task(main_sig._trade_tape_loop(store=st, client=cl, exchange_ids=["1076", "999"],
                                                             stop_event=stop, reads_per_min=600, retention_days=0))
        await asyncio.sleep(0.75)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        n = (await st._read("select count(*) as n from trade_tape"))[0]["n"]
        await st.close()
        return cl.calls, n
    calls, n = asyncio.run(_run())
    assert n == 1                                                          # the failed first read didn't kill it
    assert {c[0] for c in calls} == {"1076", "999"}                        # cycles through every market
    later = [s for ex, s in calls if ex == "1076"][1:]
    assert later and later[-1] == T1["createdAt"]                          # resumes from the newest seen trade


class _Client404(_Client):
    def exchange_trades(self, ex, *, limit, since_iso=None):
        from sig.sig_client import SigAPIError
        self.calls.append((ex, since_iso))
        if ex == "387":
            raise SigAPIError(404, "/exchanges/387/trades")
        return {"data": []}


def test_tape_loop_drops_ids_that_404(tmp_path):
    import main_sig

    async def _run():
        st = await StateStore(tmp_path / "m.db").open()
        stop = asyncio.Event()
        cl = _Client404()
        task = asyncio.create_task(main_sig._trade_tape_loop(store=st, client=cl, exchange_ids=["387", "1076"],
                                                             stop_event=stop, reads_per_min=600, retention_days=0))
        await asyncio.sleep(0.75)
        stop.set(); task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await st.close()
        return cl.calls
    calls = asyncio.run(_run())
    assert [c[0] for c in calls].count("387") == 1 and [c[0] for c in calls].count("1076") >= 3
