"""Shared-state tests.

Every test redirects the data root through SIG_DATA_DIR (the seam infra/config.py documents), so a
run never touches the real data/ dir.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
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


# --------------------------------------------------------------------------- #
# WAL + concurrency
# --------------------------------------------------------------------------- #
def test_wal_mode_is_actually_on(state_mod):
    async def main():
        store = await _open(state_mod)
        try:
            conn = state_mod.connect(state_mod.STATE_DB_PATH)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            finally:
                conn.close()
            assert str(mode).lower() == "wal"
            assert int(busy) >= 30000
        finally:
            await store.close()

    asyncio.run(main())


def test_concurrent_read_write_under_wal_does_not_corrupt(state_mod):
    """REQUIRED: concurrent read/write under WAL doesn't corrupt.

    Models the real shape of §5: the feed hammers prices while both loops read continuously and the
    order path writes. Asserts afterwards that (a) nothing raised, (b) every write landed exactly
    once with no lost updates, (c) readers never saw a torn row (a price row with a bid but no
    matching ask/spread), and (d) SQLite's own integrity check passes.
    """

    async def main():
        store = await _open(state_mod)
        n_ticks = 150
        n_exchanges = 12
        errors: list[BaseException] = []
        torn: list[dict] = []
        reads_done = 0

        async def feed():
            # the realtime feed: batched NBBO sweeps, continuously
            for i in range(n_ticks):
                rows = [
                    {
                        "exchangeId": 1000 + (i % n_exchanges),
                        "bestBid": 0.40 + (i % 10) / 1000,
                        "bestAsk": 0.48 + (i % 10) / 1000,
                        "sequence": i,
                    }
                ]
                await store.upsert_prices(rows)
                await asyncio.sleep(0)

        async def order_writer(tag: str, count: int):
            for i in range(count):
                await store.write_order(
                    idempotency_key=f"{tag}-{i}",
                    exchange_id=1000 + (i % n_exchanges),
                    side="yes",
                    action="buy",
                    quantity=10,
                    price=0.45,
                    strategy=tag,
                )
                await store.append_audit("order_submitted", component=tag, payload={"i": i})
                await asyncio.sleep(0)

        async def reader(tag: str):
            nonlocal reads_done
            while True:
                prices = await store.read_prices()
                for ex, row in prices.items():
                    # a torn read would show one side populated and the other missing
                    if (row["best_bid"] is None) != (row["best_ask"] is None):
                        torn.append({"exchange": ex, **row})
                await store.read_exposure()
                await store.read_open_orders()
                reads_done += 1
                await asyncio.sleep(0)

        readers = [asyncio.create_task(reader(f"r{i}")) for i in range(4)]
        try:
            results = await asyncio.gather(
                feed(),
                order_writer("fast_arb", 60),
                order_writer("slow_strategy", 60),
                return_exceptions=True,
            )
            errors.extend([r for r in results if isinstance(r, BaseException)])
        finally:
            for t in readers:
                t.cancel()
            for t in readers:
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        assert errors == [], f"concurrent access raised: {errors}"
        assert torn == [], f"readers observed torn rows: {torn[:3]}"
        assert reads_done > 0, "readers never ran — the test would prove nothing"

        # no lost updates: both writers' 60 orders present, and the audit rows all landed
        assert len(await store.read_orders(strategy="fast_arb")) == 60
        assert len(await store.read_orders(strategy="slow_strategy")) == 60
        assert await store.audit_count() == 120

        conn = state_mod.connect(state_mod.STATE_DB_PATH)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # last write wins per exchange; nothing duplicated the primary key
            n_rows = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            assert n_rows == n_exchanges
        finally:
            conn.close()
        await store.close()

    asyncio.run(main())


def test_writes_do_not_block_the_event_loop(state_mod):
    """Disk I/O runs off-loop, so a heartbeat coroutine keeps ticking during a write burst. If
    sqlite3 were called directly from a coroutine, the loop would stall and the tick count collapse."""

    async def main():
        store = await _open(state_mod)
        ticks = 0
        stop = False

        async def heartbeat():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.001)

        hb = asyncio.create_task(heartbeat())
        for i in range(60):
            await store.append_audit("burst", payload={"i": i})
        stop = True
        await hb
        assert ticks > 5, f"event loop appears to have stalled during writes (ticks={ticks})"
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# order dedup
# --------------------------------------------------------------------------- #
def test_order_dedup_on_idempotency_key(state_mod):
    """REQUIRED: dedup on idempotency_key — the single-writer order path."""

    async def main():
        store = await _open(state_mod)
        first = await store.write_order(
            idempotency_key="k-1", exchange_id=1001, side="yes", action="buy",
            quantity=10, price=0.45, strategy="fast_arb",
        )
        assert first.inserted is True

        # the SAME key from the other loop, with different numbers: must not create a second order
        second = await store.write_order(
            idempotency_key="k-1", exchange_id=1001, side="yes", action="buy",
            quantity=999, price=0.99, strategy="slow_strategy",
        )
        assert second.inserted is False, "a repeated idempotency key must be a no-op"

        assert len(await store.read_orders()) == 1
        row = await store.read_order("k-1")
        assert row["quantity"] == 10 and row["strategy"] == "fast_arb", "the original must stand"
        await store.close()

    asyncio.run(main())


def test_concurrent_same_key_submits_create_exactly_one_order(state_mod):
    """Both loops racing the same deterministic key. A read-then-write check would let both see
    'absent' and insert; the atomic ON CONFLICT means exactly one wins."""

    async def main():
        store = await _open(state_mod)
        results = await asyncio.gather(
            *[
                store.write_order(
                    idempotency_key="race-key", exchange_id=1001, side="yes",
                    action="buy", quantity=5, price=0.5, strategy=f"loop{i}",
                )
                for i in range(12)
            ]
        )
        assert sum(1 for r in results if r.inserted) == 1
        assert len(await store.read_orders()) == 1
        await store.close()

    asyncio.run(main())


def test_order_requires_an_idempotency_key(state_mod):
    async def main():
        store = await _open(state_mod)
        with pytest.raises(ValueError, match="idempotency_key"):
            await store.write_order(
                idempotency_key="", exchange_id=1, side="yes", action="buy", quantity=1,
            )
        await store.close()

    asyncio.run(main())


def test_order_status_update_round_trip(state_mod):
    async def main():
        store = await _open(state_mod)
        await store.write_order(
            idempotency_key="k-2", exchange_id=1002, side="yes", action="buy",
            quantity=10, price=0.45, strategy="fast_arb",
        )
        assert await store.update_order_status(
            "k-2", status="filled", broker_order_id="b-9", filled_quantity=10, fill_price=0.44
        )
        row = await store.read_order("k-2")
        assert row["status"] == "filled" and row["filled_quantity"] == 10
        assert row["broker_order_id"] == "b-9"
        assert await store.update_order_status("nope", status="filled") is False
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# staleness
# --------------------------------------------------------------------------- #
def test_staleness_helpers(state_mod):
    """REQUIRED: staleness helper works."""

    async def main():
        store = await _open(state_mod)
        now = datetime.now(timezone.utc)
        await store.upsert_price(1001, best_bid=0.4, best_ask=0.48, updated_at=_iso(now))
        await store.upsert_price(
            1002, best_bid=0.4, best_ask=0.48, updated_at=_iso(now - timedelta(seconds=300))
        )
        await store.upsert_signal(1001, p=0.6, sigma_p=0.05, updated_at=_iso(now))
        await store.upsert_signal(
            1002, p=0.6, sigma_p=0.05, updated_at=_iso(now - timedelta(seconds=7200))
        )

        assert await store.is_price_stale(1001, 60) is False
        assert await store.is_price_stale(1002, 60) is True
        assert await store.is_signal_stale(1001, 3600) is False
        assert await store.is_signal_stale(1002, 3600) is True

        assert (await store.price_age(1001)) < 10
        assert (await store.price_age(1002)) > 200

        # a market we have never seen is STALE, not fresh
        assert await store.is_price_stale(999999, 60) is True
        assert await store.price_age(999999) is None

        assert await store.stale_exchanges(60) == ["1002"]
        assert await store.stale_exchanges(3600, table="signals") == ["1002"]
        with pytest.raises(ValueError):
            await store.stale_exchanges(60, table="orders")
        await store.close()

    asyncio.run(main())


def test_unparseable_timestamp_counts_as_stale(state_mod):
    """'Unknown age' must never read as fresh — a corrupt timestamp from a dead feed would otherwise
    keep a loop trading on old prices."""

    async def main():
        store = await _open(state_mod)
        await store.upsert_price(1003, best_bid=0.4, best_ask=0.48, updated_at="not-a-timestamp")
        assert state_mod.age_seconds("not-a-timestamp") is None
        assert await store.is_price_stale(1003, 60) is True
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# verify-by-reread
# --------------------------------------------------------------------------- #
def test_reread_returns_disk_truth_not_stale_in_memory(state_mod):
    """REQUIRED: reread returns disk truth, not stale in-memory.

    Models the drift the discipline exists to catch: a loop takes its cycle-top snapshot, the feed
    (a SEPARATE connection, as in the real process) then moves the price, and the loop's cached
    snapshot still shows the old value. Only reread() picks up the change.
    """

    async def main():
        store = await _open(state_mod)
        await store.upsert_price(1001, best_bid=0.40, best_ask=0.48)

        snap1 = await store.reread()
        assert snap1.prices["1001"]["best_bid"] == pytest.approx(0.40)
        assert store.snapshot is snap1 and store.reread_seq == 1

        # the feed writes through its own connection, exactly as the real feed does
        feed_conn = state_mod.connect(state_mod.STATE_DB_PATH)
        try:
            with feed_conn:
                feed_conn.execute(
                    "UPDATE prices SET best_bid=?, best_ask=?, updated_at=? WHERE exchange_id=?",
                    (0.70, 0.78, datetime.now(timezone.utc).isoformat(), "1001"),
                )
        finally:
            feed_conn.close()

        # the in-memory snapshot is now STALE — this is the drift, demonstrated
        assert store.snapshot.prices["1001"]["best_bid"] == pytest.approx(0.40)

        # a direct read already hits disk (no hidden cache)...
        assert (await store.read_price(1001))["best_bid"] == pytest.approx(0.70)

        # ...and reread() refreshes the snapshot to disk truth
        snap2 = await store.reread()
        assert snap2.prices["1001"]["best_bid"] == pytest.approx(0.70)
        assert store.reread_seq == 2
        await store.close()

    asyncio.run(main())


def test_snapshot_is_none_until_first_reread(state_mod):
    async def main():
        store = await _open(state_mod)
        assert store.snapshot is None, "a loop must reread before trusting a snapshot"
        await store.reread()
        assert store.snapshot is not None
        await store.close()

    asyncio.run(main())


def test_reread_clears_a_held_read_snapshot(state_mod):
    """In WAL a connection inside a transaction keeps the snapshot it opened with. reread() rolls
    that back, so the next statement sees committed data rather than the loop's own history."""

    async def main():
        store = await _open(state_mod)
        await store.upsert_price(1001, best_bid=0.40, best_ask=0.48)

        # force this thread's connection into an open read transaction
        def _begin_read():
            conn = store._conn()
            conn.execute("BEGIN")
            conn.execute("SELECT * FROM prices").fetchall()
            return conn.in_transaction

        assert await asyncio.to_thread(_begin_read) is True

        other = state_mod.connect(state_mod.STATE_DB_PATH)
        try:
            with other:
                other.execute("UPDATE prices SET best_bid=0.9 WHERE exchange_id='1001'")
        finally:
            other.close()

        snap = await store.reread()
        assert snap.prices["1001"]["best_bid"] == pytest.approx(0.9)
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# audit log append-only
# --------------------------------------------------------------------------- #
def test_audit_log_is_append_only(state_mod):
    """REQUIRED: audit log is append-only — enforced by DB triggers, not convention."""

    async def main():
        store = await _open(state_mod)
        rid = await store.append_audit(
            "halt", severity="critical", component="supervisor", payload={"reason": "mismatch"}
        )
        assert rid > 0
        await store.append_audit("order_submitted", component="fast_arb")

        rows = await store.read_audit()
        assert len(rows) == 2
        assert rows[0]["event_type"] == "order_submitted"       # newest first

        conn = state_mod.connect(state_mod.STATE_DB_PATH)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with conn:
                    conn.execute("UPDATE audit_log SET severity='info' WHERE id=?", (rid,))
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with conn:
                    conn.execute("DELETE FROM audit_log WHERE id=?", (rid,))
            assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2
            # the original row is untouched
            sev = conn.execute("SELECT severity FROM audit_log WHERE id=?", (rid,)).fetchone()[0]
            assert sev == "critical"
        finally:
            conn.close()
        await store.close()

    asyncio.run(main())


def test_audit_filter_and_payload_round_trip(state_mod):
    async def main():
        store = await _open(state_mod)
        await store.append_audit("decision", payload={"size": 10, "edge": "arb"})
        await store.append_audit("halt")
        halts = await store.read_audit(event_type="halt")
        assert len(halts) == 1 and halts[0]["event_type"] == "halt"
        dec = await store.read_audit(event_type="decision")
        assert '"edge": "arb"' in dec[0]["payload"]
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# exposure against the combined book
# --------------------------------------------------------------------------- #
def test_exposure_is_computed_against_the_combined_book(state_mod):
    """REQUIRED: exposure computed against the combined book.

    §5: the caps are GLOBAL. An arb fill consumes exposure the directional book then cannot deploy.
    If exposure were computed per strategy, both loops would each see the full cap available and
    jointly breach it — so the assertion is that one number covers positions from both paths.
    """

    async def main():
        store = await _open(state_mod)
        # a position from the fast arb loop and one from the slow directional loop
        await store.upsert_position(1001, quantity=100, avg_price=0.40)    # 40 long  (arb)
        await store.upsert_position(1002, quantity=200, avg_price=0.25)    # 50 long  (directional)
        await store.upsert_position(1003, quantity=-80, avg_price=0.50)    # 40 short (directional)
        await store.upsert_position(1004, quantity=0, avg_price=0.90)      # flat -> excluded

        exp = await store.read_exposure()
        assert exp.n_positions == 3, "a flat position is not exposure"
        assert exp.gross_notional == pytest.approx(130.0)   # 40 + 50 + 40, BOTH loops together
        assert exp.net_notional == pytest.approx(50.0)      # 40 + 50 - 40
        assert exp.long_notional == pytest.approx(90.0)
        assert exp.short_notional == pytest.approx(40.0)

        # open orders are attributed per strategy for reporting, but exposure stays global
        await store.write_order(
            idempotency_key="o-arb", exchange_id=1005, side="yes", action="buy",
            quantity=10, price=0.5, strategy="fast_arb", status="open",
        )
        await store.write_order(
            idempotency_key="o-dir", exchange_id=1006, side="yes", action="buy",
            quantity=20, price=0.5, strategy="slow_strategy", status="open",
        )
        exp2 = await store.read_exposure()
        assert exp2.gross_notional == pytest.approx(130.0), "orders are not positions"
        assert exp2.by_strategy_open_orders == {
            "fast_arb": pytest.approx(5.0),
            "slow_strategy": pytest.approx(10.0),
        }

        # and the snapshot carries the same combined figure the gate would read
        snap = await store.reread()
        assert snap.exposure.gross_notional == pytest.approx(130.0)
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# leaderboard / edge performance
# --------------------------------------------------------------------------- #
def test_leaderboard_and_rho(state_mod):
    async def main():
        store = await _open(state_mod)
        assert await store.read_leaderboard() is None
        assert await store.read_rho() is None

        await store.write_leaderboard(rank=5, balance=120_000.0, third_place_threshold=100_000.0)
        lb = await store.read_leaderboard()
        assert lb["rank"] == 5
        assert await store.read_rho() == pytest.approx(1.2)

        # a zero/absent threshold must yield None, never a divide-by-zero or an invented rho
        await store.write_leaderboard(rank=5, balance=120_000.0, third_place_threshold=0.0)
        assert await store.read_rho() is None

        # single-row table: the upsert replaces, never appends
        conn = state_mod.connect(state_mod.STATE_DB_PATH)
        try:
            assert conn.execute("SELECT COUNT(*) FROM leaderboard").fetchone()[0] == 1
        finally:
            conn.close()
        await store.close()

    asyncio.run(main())


def test_edge_performance_round_trip(state_mod):
    async def main():
        store = await _open(state_mod)
        await store.upsert_edge_performance(
            "hard_arb", realized_pnl=250.5, n_trades=12, window=[1.0, -0.5, 3.25]
        )
        await store.upsert_edge_performance("market_making", realized_pnl=-10.0, n_trades=3)
        rows = await store.read_edge_performance()
        assert [r["edge"] for r in rows] == ["hard_arb", "market_making"]
        arb = next(r for r in rows if r["edge"] == "hard_arb")
        assert arb["realized_pnl"] == pytest.approx(250.5)
        assert arb["window"] == [1.0, -0.5, 3.25]

        await store.upsert_edge_performance("hard_arb", realized_pnl=300.0, n_trades=14)
        again = await store.read_edge_performance("hard_arb")
        assert len(again) == 1 and again[0]["n_trades"] == 14
        await store.close()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def test_init_is_idempotent_and_separate_from_the_inherited_db(state_mod):
    """state.db is its own file: infra/persistence/db.py already defines an incompatible `orders`
    table, so sharing one file would mean losing the UNIQUE idempotency_key dedup."""

    async def main():
        store = await _open(state_mod)
        await store.close()
        await (await _open(state_mod)).close()        # second init must not raise
        import infra.config as cfg

        assert state_mod.STATE_DB_PATH != cfg.DB_PATH
        assert state_mod.STATE_DB_PATH.name == "state.db"
        conn = state_mod.connect(state_mod.STATE_DB_PATH)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
            assert "idempotency_key" in cols and "exchange_id" in cols
        finally:
            conn.close()

    asyncio.run(main())


def test_async_context_manager(state_mod):
    async def main():
        async with state_mod.StateStore() as store:
            await store.upsert_price(1001, best_bid=0.4, best_ask=0.48)
            assert (await store.read_price(1001))["spread"] == pytest.approx(0.08)

    asyncio.run(main())
