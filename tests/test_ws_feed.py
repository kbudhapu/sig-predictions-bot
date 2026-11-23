"""Tests for the Supabase Realtime WebSocket wire-adapter (sig/feed/ws_adapter.py), driven by a
MOCKED socket — no live connection. Proves: Supabase/Phoenix broadcast parsing (market_batch with a
NESTED delivery.revision + books[], account_batch fills, resyncRequired, control frames), the POST
/realtime/token provider that parses HttpResult.body and populates the shared connection dict, the
ws_factory async-CM/iterator contract feed.py expects, and feed.py processing versioned updates into
state with a gap → REST refetch."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from sig.feed.ws_adapter import (
    _parse_broadcast, supabase_token_provider, make_ws_factory, build_realtime_feed,
)
from sig.feed.feed import RealtimeFeed, FeedConfig
from sig.write_gate import HttpResult


def _broadcast(topic, inner_event, inner_payload):
    """A Supabase broadcast frame: the real event/data are NESTED under payload."""
    return {"topic": topic, "event": "broadcast", "ref": None,
            "payload": {"type": "broadcast", "event": inner_event, "payload": inner_payload}}


# ── broadcast parsing (nested event + nested delivery.revision + tolerant books) ───────────────
def test_parse_market_batch_nested_delivery_and_books():
    # _parse_broadcast returns an INTERMEDIATE market dict; the SESSION assigns the synthetic
    # continuous revision (per-market delivery.revision is NOT fed to feed.py's single-stream tracker).
    frame = _broadcast(
        "realtime:tournament:T1:market:M1", "market_batch",
        {"trades": [], "bookDirty": [], "marketSettled": [],
         "books": [{"exchangeId": "YES_X", "asOf": {"sequence": 10, "at": "2026-10-01T00:00:00Z"},
                    "bestBid": 0.5, "bestAsk": 0.52}],
         "delivery": {"revision": 7, "previousRevision": 6}})
    upd = _parse_broadcast(frame)
    assert upd["_market"] is True and upd["_resync"] is False
    assert upd["fills"] == []
    assert len(upd["prices"]) == 1
    row = upd["prices"][0]
    assert row["exchangeId"] == "YES_X" and row["bestBid"] == 0.5 and row["bestAsk"] == 0.52
    assert row["sequence"] == 10 and row["updated_at"]          # version stamp + freshness stamp


def test_parse_market_batch_top_of_book_fallback():
    # No bestBid/bestAsk fields — derive from bids[0]/asks[0] (price field OR [price,size] pair).
    frame = _broadcast(
        "realtime:tournament:T1:market:M1", "market_batch",
        {"books": [{"id": "YES_Y", "asOf": {"sequence": 3},
                    "bids": [{"price": 0.4, "size": 100}], "asks": [[0.45, 50]]}],
         "delivery": {"revision": 2, "previousRevision": 1}})
    row = _parse_broadcast(frame)["prices"][0]
    assert row["exchangeId"] == "YES_Y" and row["bestBid"] == 0.4 and row["bestAsk"] == 0.45


def test_parse_resync_required_flags_resync():
    # resyncRequired compact batch → _resync True; the SESSION turns that into previousRevision=None
    # (feed.py's gap→REST-resync path). See test_feed_processes_market_batches_and_resync_refetches.
    frame = _broadcast("realtime:tournament:T1:market:M1", "market_batch",
                       {"resyncRequired": True, "delivery": {"revision": 99}})
    upd = _parse_broadcast(frame)
    assert upd["_market"] is True and upd["_resync"] is True and upd["prices"] == []


def test_parse_account_batch_marks_fills():
    frame = _broadcast("realtime:user:abc", "account_batch",
                       {"fills": [{"orderId": "o1", "qty": 5}], "orderUpdates": []})
    upd = _parse_broadcast(frame)
    assert upd == {"_account": True, "fills": [{"orderId": "o1", "qty": 5}]}


def test_parse_skips_control_and_unknown_frames():
    assert _parse_broadcast({"event": "phx_reply", "payload": {"status": "ok"}}) is None
    assert _parse_broadcast({"event": "heartbeat", "payload": {}}) is None
    assert _parse_broadcast({"topic": "phoenix", "event": "broadcast", "payload": {}}) is None
    assert _parse_broadcast("not json") is None
    # a broadcast whose inner event is not one we handle → skipped
    assert _parse_broadcast(_broadcast("realtime:x", "presence_state", {})) is None


def test_parse_v2_list_serializer_fallback():
    v2 = [None, "1", "realtime:tournament:T:market:M", "broadcast",
          {"type": "broadcast", "event": "market_batch",
           "payload": {"books": [{"exchangeId": "YES_Z", "asOf": {"sequence": 4},
                                  "bestBid": 0.6, "bestAsk": 0.61}],
                       "delivery": {"revision": 2, "previousRevision": 1}}}]
    upd = _parse_broadcast(v2)
    assert upd["_market"] is True and upd["prices"][0]["exchangeId"] == "YES_Z"


# ── POST /realtime/token: parses HttpResult.body + populates the shared dict + ttl from expiresAt ─
@pytest.mark.asyncio
async def test_token_provider_posts_parses_body_and_fills_shared():
    calls = []
    expires = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")

    class _Client:
        def raw(self, method, path, **kw):
            calls.append((method, path))
            # SigClient.raw returns an HttpResult (NOT parsed JSON) — body is a JSON string.
            return HttpResult(status=200, body=json.dumps({
                "token": "jwt-abc", "expiresAt": expires,
                "supabaseUrl": "https://proj.supabase.co", "anonKey": "anon-xyz",
                "channels": {"user": "user:9c443bff"}}))

    shared = {}
    provider = supabase_token_provider(_Client(), shared)
    token, ttl = await provider()
    assert token == "jwt-abc"
    assert calls == [("POST", "/realtime/token")]          # POST, never GET
    # ttl = (expiresAt - now) - 300s lead ≈ 3h - 5min, and always >= 60
    assert 10000.0 < ttl < 10800.0
    assert shared["supabaseUrl"] == "https://proj.supabase.co"
    assert shared["anonKey"] == "anon-xyz"
    assert shared["channels"]["user"] == "user:9c443bff"


@pytest.mark.asyncio
async def test_token_provider_bad_expires_defaults_ttl():
    class _Client:
        def raw(self, method, path, **kw):
            return HttpResult(status=200, body=json.dumps({"token": "t", "expiresAt": "not-a-date"}))
    token, ttl = await supabase_token_provider(_Client(), {})()
    assert token == "t" and ttl == 10800.0                 # parse failure → 3h default


# ── a fake socket to drive the adapter without a network ───────────────────────
class _FakeWS:
    def __init__(self, frames):
        self._frames = [json.dumps(f) if isinstance(f, (dict, list)) else f for f in frames]
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._frames:
            raise ConnectionError("closed")                # end of stream → session ends
        return self._frames.pop(0)

    async def close(self):
        pass


def _fake_connect_factory(frames):
    async def _connect(uri, headers=None):
        _connect.last_uri = uri
        _connect.ws = _FakeWS(frames)
        return _connect.ws
    return _connect


# ── ws_factory session: builds URI from shared, joins private channels, yields parsed updates ──
@pytest.mark.asyncio
async def test_ws_factory_builds_uri_joins_private_and_yields():
    frames = [
        {"event": "phx_reply", "payload": {"status": "ok"}},                        # control → skipped
        _broadcast("realtime:tournament:T:market:M", "market_batch",
                   {"books": [{"exchangeId": "YES_X", "asOf": {"sequence": 1},
                               "bestBid": 0.5, "bestAsk": 0.52}],
                    "delivery": {"revision": 1, "previousRevision": 0}}),
    ]
    shared = {"supabaseUrl": "https://proj.supabase.co", "anonKey": "anon",
              "channels": {"user": "user:u1"}}
    connect = _fake_connect_factory(frames)
    ws_factory = make_ws_factory(
        shared=shared,
        topics_provider=lambda: ["realtime:user:u1", "realtime:tournament:T:market:M"],
        connect=connect, heartbeat_s=999)
    got = []
    async with ws_factory("jwt-xyz") as ws:
        async for upd in ws:
            got.append(upd)
    assert len(got) == 1 and got[0]["revision"] == 1
    assert got[0]["prices"][0]["exchangeId"] == "YES_X"
    # URI built from shared: Supabase host, realtime path, anon apikey, vsn.
    assert connect.last_uri == "wss://proj.supabase.co/realtime/v1/websocket?apikey=anon&vsn=1.0.0"
    # joined BOTH private channels (user + market), each a phx_join with private:true + the JWT.
    assert len(connect.ws.sent) == 2
    join0 = json.loads(connect.ws.sent[0])
    assert join0["event"] == "phx_join" and join0["topic"] == "realtime:user:u1"
    assert join0["payload"]["config"]["private"] is True
    assert join0["payload"]["access_token"] == "jwt-xyz"


# ── feed.py end-to-end over the mocked WS ──────────────────────────────────────
class _FakeStore:
    def __init__(self):
        self.upserts = []
        self.audits = []

    async def upsert_prices(self, rows):
        self.upserts.append(list(rows))
        return len(rows)

    async def append_audit(self, **kw):
        self.audits.append(kw)


def _token_response_client(frames, snap_calls):
    class _Client:
        def raw(self, method, path, **kw):
            return HttpResult(status=200, body=json.dumps({
                "token": "jwt", "expiresAt": None, "supabaseUrl": "https://proj.supabase.co",
                "anonKey": "anon", "channels": {"user": "user:u1"}}))

        def exchange_prices(self, ids):
            snap_calls["n"] += 1
            return {"data": [{"exchangeId": "SNAP", "bestBid": 0.49, "bestAsk": 0.51, "sequence": 0}]}

        # unused by this path but present on the real client
        def markets_all(self, *a, **k):
            return []
    return _Client(), _fake_connect_factory(frames)


@pytest.mark.asyncio
async def test_feed_processes_market_batches_and_resync_refetches():
    # connect snapshot sets last_processed=0; rev1/prev0 + rev2/prev1 apply; resyncRequired → gap → REST.
    frames = [
        _broadcast("realtime:tournament:T:market:M", "market_batch",
                   {"books": [{"exchangeId": "YES_X", "asOf": {"sequence": 1},
                               "bestBid": 0.5, "bestAsk": 0.52}],
                    "delivery": {"revision": 1, "previousRevision": 0}}),
        _broadcast("realtime:tournament:T:market:M", "market_batch",
                   {"books": [], "delivery": {"revision": 2, "previousRevision": 1}}),
        _broadcast("realtime:tournament:T:market:M", "market_batch",
                   {"resyncRequired": True, "delivery": {"revision": 9}}),          # GAP → resync
    ]
    snap_calls = {"n": 0}
    client, connect = _token_response_client(frames, snap_calls)
    store = _FakeStore()
    feed = build_realtime_feed(store=store, client=client, config=None, exchange_ids=["SNAP"],
                               tournament_id="T", market_ids=["M"], connect=connect)
    feed._sleep = lambda _d: asyncio.sleep(0)             # no real backoff delay
    await feed.run(max_sessions=1)

    assert feed.health.updates_applied == 2               # rev1 + rev2 applied from the WS
    assert feed.health.gaps == 1                          # resyncRequired forced a gap
    assert snap_calls["n"] >= 2                            # connect snapshot + the gap REST refetch
    # the book price row reached store.upsert_prices
    assert any(any(r.get("exchangeId") == "YES_X" for r in rows) for rows in store.upserts)


@pytest.mark.asyncio
async def test_multimarket_independent_revisions_apply_without_spurious_resync():
    # THE core fix: SIG runs an INDEPENDENT delivery.revision per market channel. Here two markets
    # arrive with totally unrelated revisions (M1: 1180/1179, M2: 41/40, M1: 1181/1180). The adapter
    # must present feed.py ONE continuous synthetic stream so ALL THREE apply and NOTHING false-gaps
    # (the connect REST snapshot is the only snapshot call; gaps stay 0).
    frames = [
        _broadcast("realtime:tournament:T:market:M1", "market_batch",
                   {"books": [{"exchangeId": "A", "asOf": {"sequence": 100}, "bestBid": 0.5, "bestAsk": 0.52}],
                    "delivery": {"revision": 1180, "previousRevision": 1179}}),
        _broadcast("realtime:tournament:T:market:M2", "market_batch",
                   {"books": [{"exchangeId": "B", "asOf": {"sequence": 7}, "bestBid": 0.3, "bestAsk": 0.31}],
                    "delivery": {"revision": 41, "previousRevision": 40}}),
        _broadcast("realtime:tournament:T:market:M1", "market_batch",
                   {"books": [{"exchangeId": "A", "asOf": {"sequence": 101}, "bestBid": 0.51, "bestAsk": 0.53}],
                    "delivery": {"revision": 1181, "previousRevision": 1180}}),
    ]
    snap_calls = {"n": 0}
    client, connect = _token_response_client(frames, snap_calls)
    store = _FakeStore()
    feed = build_realtime_feed(store=store, client=client, config=None, exchange_ids=["SNAP"],
                               tournament_id="T", market_ids=["M1", "M2"], connect=connect)
    feed._sleep = lambda _d: asyncio.sleep(0)
    await feed.run(max_sessions=1)

    assert feed.health.updates_applied == 3               # all three applied as one continuous stream
    assert feed.health.gaps == 0                           # NO false gap despite unrelated per-market revs
    assert snap_calls["n"] == 1                            # ONLY the connect snapshot — no per-frame resync
    seen = {r.get("exchangeId") for rows in store.upserts for r in rows}
    assert "A" in seen and "B" in seen


@pytest.mark.asyncio
async def test_feed_observes_account_batch_fills():
    # a market_batch anchors the revision, then an account_batch rides it as a continuous no-op whose
    # fills feed.py audits (feed.py never writes authoritative order state).
    frames = [
        _broadcast("realtime:tournament:T:market:M", "market_batch",
                   {"books": [], "delivery": {"revision": 1, "previousRevision": 0}}),
        _broadcast("realtime:user:u1", "account_batch", {"fills": [{"orderId": "o1", "qty": 3}]}),
    ]
    snap_calls = {"n": 0}
    client, connect = _token_response_client(frames, snap_calls)
    store = _FakeStore()
    feed = build_realtime_feed(store=store, client=client, config=None, exchange_ids=["SNAP"],
                               tournament_id="T", market_ids=["M"], connect=connect)
    feed._sleep = lambda _d: asyncio.sleep(0)
    await feed.run(max_sessions=1)

    assert any(a.get("event_type") == "feed_fill_observed" for a in store.audits)


@pytest.mark.asyncio
async def test_build_realtime_feed_keeps_rest_snapshot_default():
    # build_realtime_feed wires the WS primary but leaves the REST snapshot_fetcher as feed.py's
    # default (the kept resync/fallback).
    client, connect = _token_response_client([], {"n": 0})
    feed = build_realtime_feed(store=_FakeStore(), client=client, config=None, exchange_ids=["1"],
                               tournament_id="T", market_ids=["M"], connect=connect)
    assert feed._ws_factory is not None
    assert feed._snapshot_fetcher == feed._default_snapshot_fetcher
