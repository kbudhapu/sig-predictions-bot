"""A venue 500 on the connect snapshot must NOT discard the session.

THE TEARDOWN BUG. _apply_snapshot runs between joining the market channels and entering the update
loop. SIG intermittently 500s on /exchanges/prices; the exception propagated out of _run_session and
run() treated it as a disconnect — throwing away a session that had just spent ~67s joining all 237
market channels, then doing it again. Live trace (2026-10-02):

    07:37:41 ws_sharded_session_open      <- died before the update loop
    07:38:25 ws_sharded_session_open      <- died again
    07:39:23 ws_sharded_session_open      <- this one survived
    07:40:30 ws_shard_first_broadcast     joins_ok=78/81/80 on all three shards

which looks exactly like a ~45s teardown timer but is really "the snapshot 500'd twice".

The subscribe is far more expensive than the snapshot, so the snapshot now retries and then fails
SOFT: the session continues, and the feed is marked STALE + reconcile_needed so nothing downstream
mistakes a snapshot-less session for fresh truth.
"""
from __future__ import annotations

import asyncio

import pytest

from sig.feed.feed import RealtimeFeed, FeedConfig


class _Store:
    def __init__(self):
        self.writes = 0

    async def upsert_prices(self, rows):
        self.writes += 1
        return len(rows or [])


def _feed(snapshot_fetcher, *, ws_factory=None):
    return RealtimeFeed(
        _Store(),
        config=FeedConfig(),
        ws_factory=ws_factory or (lambda token: _NullSession()),
        token_provider=_token_provider,
        snapshot_fetcher=snapshot_fetcher,
    )


async def _token_provider():
    return "jwt", 10_000.0


class _NullSession:
    """A session that yields nothing and ends — enough to exercise _run_session's ordering."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


# --------------------------------------------------------------------------- #
# Retry, then continue
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_snapshot_retries_before_giving_up():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("SIG API 500 on /exchanges/prices")
        return [{"exchangeId": "1", "bestBid": 0.4, "bestAsk": 0.6}], 7

    feed = _feed(flaky)
    rev = await feed._apply_snapshot_soft(context="connect")
    assert calls["n"] == 3, "the snapshot must be retried, not abandoned on the first 500"
    assert rev == 7
    assert feed.health.stale is False, "a successful snapshot is fresh authoritative truth"


@pytest.mark.asyncio
async def test_a_persistently_failing_snapshot_does_NOT_raise():
    """The whole fix: no exception escapes, so run() never sees a 'disconnect'."""
    async def always_500():
        raise RuntimeError("SIG API 500 on /exchanges/prices")

    feed = _feed(always_500)
    rev = await feed._apply_snapshot_soft(context="connect", retries=2)
    assert rev is None


@pytest.mark.asyncio
async def test_a_failed_snapshot_marks_the_feed_stale_and_needing_reconcile():
    """Failing soft must not mean pretending to be fresh — the risk gates stay conservative."""
    async def always_500():
        raise RuntimeError("SIG API 500 on /exchanges/prices")

    feed = _feed(always_500)
    feed.health.stale = False
    feed.health.reconcile_needed = False
    await feed._apply_snapshot_soft(context="connect", retries=1)
    assert feed.health.stale is True
    assert feed.health.reconcile_needed is True


@pytest.mark.asyncio
async def test_the_session_survives_a_failing_snapshot_and_reaches_the_update_loop():
    """End-to-end ordering: the session must get past the snapshot to where updates are consumed.
    Previously the 500 escaped _run_session and the whole session (and its subscribe) was binned."""
    entered = {"ws": False}

    class _Session(_NullSession):
        async def __aenter__(self):
            entered["ws"] = True
            return self

    async def always_500():
        raise RuntimeError("SIG API 500 on /exchanges/prices")

    feed = _feed(always_500, ws_factory=lambda token: _Session())
    reason = await feed._run_session()
    assert entered["ws"] is True
    assert reason == "disconnect", "ended by the stream ending, NOT by the snapshot raising"


@pytest.mark.asyncio
async def test_cancellation_still_propagates_through_the_soft_snapshot():
    """Failing soft must not swallow shutdown."""
    async def cancelled():
        raise asyncio.CancelledError()

    feed = _feed(cancelled)
    with pytest.raises(asyncio.CancelledError):
        await feed._apply_snapshot_soft(context="connect")
