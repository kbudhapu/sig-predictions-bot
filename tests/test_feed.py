"""Realtime-feed tests — mock the WebSocket + REST (no live connection). Real StateStore on a temp db.

Covers (per the task): a continuous revision sequence applies in order; a GAP (previousRevision mismatch)
→ mark-stale + refetch + resync (the core safety invariant); a disconnect → reconnect-with-refetch (never
resume mid-stream); staleness markers set on writes AND a dead feed leaves state STALE (degrade safe, not
silent); token-refresh-before-expiry; reconnect backoff doesn't storm.
"""
import contextlib
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.state.store import StateStore
from sig.feed.feed import RealtimeFeed, FeedConfig, FeedUpdate


@contextlib.contextmanager
def tmp_db():
    """A temp state.db path. Cleanup ignores errors — on Windows the StateStore's thread-local WAL
    connections in the to_thread pool keep the file locked until the process exits; the OS frees it."""
    d = tempfile.mkdtemp()
    try:
        yield os.path.join(d, "state.db")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── mocks ──
class MockWS:
    """Async-context WebSocket yielding a scripted list of raw update dicts; optionally raises to
    simulate a mid-stream disconnect. Empty iteration = a clean disconnect."""

    def __init__(self, updates, *, raise_exc=None):
        self._updates = list(updates)
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._updates:
            return self._updates.pop(0)
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        raise StopAsyncIteration


def ws_factory_from(scripts):
    """Return a ws_factory that serves one MockWS per session (each a list of updates)."""
    seq = list(scripts)

    def factory(token):
        updates = seq.pop(0) if seq else []
        return MockWS(updates)

    return factory


async def make_feed(tmpdb, **kw):
    store = StateStore(db_path=tmpdb)
    await store.open()
    kw.setdefault("config", FeedConfig(reconnect_reset_after_s=120.0))
    feed = RealtimeFeed(store, **kw)
    return store, feed


def price(ex, rev, bid=0.40, ask=0.42):
    return {"exchangeId": ex, "bestBid": bid, "bestAsk": ask, "sequence": rev}


def update(prev, rev, rows):
    return {"previousRevision": prev, "revision": rev, "prices": rows}


@pytest.mark.asyncio
async def test_continuous_sequence_applies_in_order():
    with tmp_db() as db:
        snaps = {"n": 0}

        async def snapshot():
            snaps["n"] += 1
            return [price("MKT_A", 10, 0.50, 0.52)], 10

        store, feed = await make_feed(db, ws_factory=lambda t: MockWS([]), snapshot_fetcher=snapshot)
        await feed._apply_snapshot()  # rev 10
        assert feed.health.last_processed_revision == 10 and not feed.health.stale

        assert await feed.handle_update(update(10, 11, [price("MKT_A", 11, 0.55, 0.57)])) == "applied"
        assert await feed.handle_update(update(11, 12, [price("MKT_A", 12, 0.60, 0.62)])) == "applied"
        assert feed.health.last_processed_revision == 12
        assert feed.health.gaps == 0 and snaps["n"] == 1  # no refetch on a continuous stream
        row = await store.read_price("MKT_A")
        assert row["best_bid"] == 0.60 and row["sequence"] == 12  # latest applied in order
        await store.close()


@pytest.mark.asyncio
async def test_gap_triggers_mark_stale_refetch_resync():
    # THE CORE SAFETY TEST
    with tmp_db() as db:
        calls = {"n": 0}
        resyncs = []

        async def snapshot():
            calls["n"] += 1
            # first snapshot rev 10; the post-gap refetch returns the authoritative rev 50
            rev = 10 if calls["n"] == 1 else 50
            return [price("MKT_A", rev, 0.70, 0.72)], rev

        async def on_resync(reason):
            resyncs.append(reason)

        store, feed = await make_feed(db, ws_factory=lambda t: MockWS([]), snapshot_fetcher=snapshot, on_resync=on_resync)
        await feed._apply_snapshot()  # rev 10
        assert await feed.handle_update(update(10, 11, [price("MKT_A", 11)])) == "applied"

        # GAP: previousRevision 40 != last_processed 11 → missed data
        result = await feed.handle_update(update(40, 41, [price("MKT_A", 41)]))
        assert result == "gap_resynced"
        assert feed.health.gaps == 1
        assert calls["n"] == 2                       # it REFETCHED the authoritative snapshot
        assert feed.health.last_processed_revision == 50  # resynced to snapshot truth, NOT the gapped 41
        assert feed.health.reconcile_needed is True  # signalled OMS reconcile-after-gap
        assert "revision_gap" in resyncs
        # the gapped update's price (rev 41) was NOT applied as continuous; snapshot (rev 50) won
        row = await store.read_price("MKT_A")
        assert row["sequence"] == 50
        # the gap was surfaced, not hidden
        assert any(a["event_type"] == "feed_gap" for a in await store.read_audit(limit=20))
        await store.close()


@pytest.mark.asyncio
async def test_disconnect_reconnects_and_always_refetches():
    with tmp_db() as db:
        snaps = {"n": 0}
        reconciles = []

        async def snapshot():
            snaps["n"] += 1
            return [price("MKT_A", 100 + snaps["n"])], 100 + snaps["n"]

        async def on_resync(reason):
            reconciles.append(reason)

        # two sessions, each a short stream then a clean disconnect (empty iterator)
        scripts = [[update(101, 102, [price("MKT_A", 102)])], [update(102, 103, [price("MKT_A", 103)])]]
        slept = []

        async def fake_sleep(s):
            slept.append(s)

        store, feed = await make_feed(
            db, ws_factory=ws_factory_from(scripts), snapshot_fetcher=snapshot,
            token_provider=_token_provider(), on_resync=on_resync,
            sleep=fake_sleep, monotonic=lambda: 0.0,
        )
        await feed.run(max_sessions=2)
        # each (re)connect refetched the authoritative snapshot — never resumed mid-stream
        assert snaps["n"] == 2
        assert feed.health.reconnects >= 1
        # reconcile signalled on every connect (reconcile-after-reconnect discipline)
        assert reconciles.count("connect") == 2
        await store.close()


@pytest.mark.asyncio
async def test_staleness_markers_set_and_dead_feed_leaves_state_stale():
    with tmp_db() as db:

        async def snapshot():
            return [price("MKT_A", 5, 0.33, 0.35)], 5

        store, feed = await make_feed(db, ws_factory=lambda t: MockWS([]), snapshot_fetcher=snapshot)
        # before the first snapshot the feed is STALE (nothing trusted yet)
        assert feed.health.stale is True

        await feed._apply_snapshot()
        row = await store.read_price("MKT_A")
        assert row["updated_at"] is not None          # staleness marker SET on write
        assert feed.health.stale is False

        # feed "dies": a session error ends the only session → state must go STALE (degrade safe, not silent)
        def boom_factory(token):
            raise ConnectionError("ws down")

        feed._ws_factory = boom_factory
        feed._sleep = _noop_sleep
        await feed.run(max_sessions=1)
        assert feed.health.stale is True              # feed self-reports stale
        assert feed.health.connected is False
        # and the cross-process marker: the store treats the now-unfreshened price as stale (age > 0)
        assert await store.is_price_stale("MKT_A", max_age_seconds=0.0) is True
        # a price that was never written is stale too (unknown age == stale, never silently fresh)
        assert await store.is_price_stale("NEVER_SEEN", max_age_seconds=9999) is True
        await store.close()


@pytest.mark.asyncio
async def test_token_refresh_before_expiry():
    with tmp_db() as db:
        tokens = {"n": 0}

        async def token_provider():
            tokens["n"] += 1
            return f"jwt-{tokens['n']}", 100.0   # ttl 100s, but refresh lead is 300s → expiring immediately

        async def snapshot():
            return [price("MKT_A", 1)], 1

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        # a session whose first update trips the "token expiring" check → ends as token_refresh (planned)
        store, feed = await make_feed(
            db, config=FeedConfig(token_refresh_lead_s=300.0, token_ttl_s=100.0),
            ws_factory=ws_factory_from([[update(1, 2, [price("MKT_A", 2)])], [update(1, 2, [price("MKT_A", 2)])]]),
            snapshot_fetcher=snapshot, token_provider=token_provider, sleep=fake_sleep, monotonic=lambda: 0.0,
        )
        await feed.run(max_sessions=2)
        assert tokens["n"] == 2          # refreshed the JWT for the second session
        assert slept == []               # a planned token refresh does NOT incur reconnect backoff
        await store.close()


@pytest.mark.asyncio
async def test_reconnect_backoff_is_exponential_and_does_not_storm():
    with tmp_db() as db:

        async def snapshot():
            return [price("MKT_A", 1)], 1

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        # every session fails immediately (ws raises) and lasts ~0s (< reset_after) → backoff escalates
        def boom(token):
            raise ConnectionError("down")

        cfg = FeedConfig(reconnect_backoff_base_s=1.0, reconnect_backoff_factor=2.0,
                         reconnect_backoff_cap_s=10.0, reconnect_reset_after_s=120.0)
        store, feed = await make_feed(
            db, config=cfg, ws_factory=boom, snapshot_fetcher=snapshot,
            token_provider=_token_provider(), sleep=fake_sleep, monotonic=lambda: 0.0,
        )
        await feed.run(max_sessions=5)
        # 1, 2, 4, 8, then capped at 10 — strictly bounded, never a tight-loop storm
        assert slept == [1.0, 2.0, 4.0, 8.0, 10.0]
        await store.close()


# ── small helpers ──
def _token_provider():
    async def tp():
        return "jwt", 10800.0
    return tp


async def _noop_sleep(s):
    return None
