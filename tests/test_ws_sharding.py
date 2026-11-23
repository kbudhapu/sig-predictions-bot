"""Full-board WS coverage: the market channels are SHARDED across sockets.

Supabase Realtime caps channels-per-CONNECTION (~100) and joining more silently stops streaming
(measured: 237 channels -> 0 updates applied; 40 -> 186). The entrypoint used to cope by truncating:

    market_ids = all_market_ids[:90]            # <- 147 of 237 markets subscribed to NOTHING

Those 147 markets froze at their connect-snapshot price forever. Live evidence (2026-10-02):

    feed_transport_ws    markets=90  markets_total=237
    age histogram of the 237 price rows:  >600s: 147   <- one frozen timestamp, never updated
    mm_market_selection  quotable=27        <- the MM could only ever quote the live ~90

The cap is per-connection, so coverage is a sharding problem, not a ceiling. These tests pin the
shard arithmetic and the two invariants that sharding must not break.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from sig.feed.ws_adapter import make_ws_factory


class _FakeWS:
    """One socket. `frames` are delivered then the socket ends (as the real one does on close)."""

    def __init__(self, frames, hold: bool = False):
        self._frames = [json.dumps(f) if isinstance(f, (dict, list)) else f for f in frames]
        self._hold = hold
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        if self._hold:
            await asyncio.sleep(3600)          # a healthy but quiet socket
        raise ConnectionError("closed")

    async def close(self):
        self.closed = True

    def joined(self) -> list[str]:
        out = []
        for s in self.sent:
            d = json.loads(s)
            if d.get("event") == "phx_join":
                out.append(d["topic"])
        return out


def _connect_recorder(frames_per_socket=None, hold=False):
    made: list[_FakeWS] = []

    async def _connect(uri, headers=None):
        frames = []
        if frames_per_socket is not None and len(made) < len(frames_per_socket):
            frames = frames_per_socket[len(made)]
        # In frames mode the socket must stay OPEN once drained: a shard that ends would (correctly)
        # end the whole session, which is a different test.
        ws = _FakeWS(frames, hold=hold or frames_per_socket is not None)
        made.append(ws)
        return ws

    _connect.made = made
    return _connect


def _broadcast(topic, event, payload):
    return {"topic": topic, "event": "broadcast",
            "payload": {"type": "broadcast", "event": event, "payload": payload}}


def _topics(n_markets: int, user: bool = True) -> list[str]:
    t = ["realtime:user:u1"] if user else []
    return t + [f"realtime:tournament:T:market:M{i}" for i in range(n_markets)]


def _session(topics, connect, cap=80):
    """make_ws_factory returns a factory; calling it with a token builds the session."""
    return make_ws_factory(shared={"supabaseUrl": "https://p.supabase.co", "anonKey": "a"},
                           topics_provider=lambda: topics, connect=connect,
                           heartbeat_s=999, max_channels_per_connection=cap)("jwt")


# --------------------------------------------------------------------------- #
# Shard arithmetic — the actual coverage fix
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_all_237_markets_are_subscribed_across_shards():
    """THE regression: every market must be joined somewhere. 237 markets + 1 user channel = 238
    topics at 80 per socket => 3 sockets (80 + 80 + 78), and the union is the whole board."""
    connect = _connect_recorder(hold=True)
    async with _session(_topics(237), connect) as ws:
        await asyncio.sleep(0)
        sockets = connect.made
        joined = [t for s in sockets for t in s.joined()]
        assert len(sockets) == 3, f"238 topics at 80/socket needs 3 sockets, opened {len(sockets)}"
        assert [len(s.joined()) for s in sockets] == [80, 80, 78]
        assert len(joined) == 238, f"joined {len(joined)} topics, expected 238"
        assert len(set(joined)) == 238, "a topic was joined twice"
        for i in range(237):
            assert f"realtime:tournament:T:market:M{i}" in joined, f"market M{i} not subscribed"


@pytest.mark.asyncio
async def test_no_socket_exceeds_the_per_connection_channel_budget():
    """Exceeding ~100 channels is the failure that silently kills streaming — never do it."""
    connect = _connect_recorder(hold=True)
    async with _session(_topics(237), connect) as ws:
        await asyncio.sleep(0)
        for s in connect.made:
            assert len(s.joined()) <= 80


@pytest.mark.asyncio
async def test_a_small_board_still_uses_one_socket():
    """No needless connections — the single-socket behaviour is preserved below the cap."""
    connect = _connect_recorder(hold=True)
    async with _session(_topics(10), connect) as ws:
        await asyncio.sleep(0)
        assert len(connect.made) == 1


# --------------------------------------------------------------------------- #
# Invariant 1 — the account channel exactly once (else every fill arrives N times)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_user_channel_is_joined_exactly_once_across_all_shards():
    connect = _connect_recorder(hold=True)
    async with _session(_topics(237), connect) as ws:
        await asyncio.sleep(0)
        joined = [t for s in connect.made for t in s.joined()]
        assert joined.count("realtime:user:u1") == 1, (
            "the account channel is joined per-shard: every fill would be delivered once per socket")


# --------------------------------------------------------------------------- #
# Invariant 2 — a dead shard ends the session (never stream a partial board)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_one_dead_shard_ends_the_whole_session():
    """A session streaming 2 of 3 shards is a feed that lies by omission: the dropped shard's
    markets would freeze while the feed still looked healthy. Ending the session routes into
    feed.py's reconnect + REST authoritative-resync."""
    made: list[_FakeWS] = []

    async def _connect(uri, headers=None):
        ws = _FakeWS([], hold=(len(made) == 0))   # socket 0 stays open, socket 1 dies at once
        made.append(ws)
        return ws

    async with _session(_topics(100), _connect) as ws:
        with pytest.raises(StopAsyncIteration):
            await ws.__anext__()
    assert len(made) == 2, "100 markets + user at 80/socket should be 2 sockets"


@pytest.mark.asyncio
async def test_exiting_the_session_closes_every_socket():
    connect = _connect_recorder(hold=True)
    async with _session(_topics(237), connect) as ws:
        await asyncio.sleep(0)
    assert connect.made and all(s.closed for s in connect.made), "a socket was leaked"


# --------------------------------------------------------------------------- #
# The continuity invariant feed.py depends on, across MERGED shards
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_merged_shard_stream_is_strictly_continuous():
    """feed.py accepts an update only when previousRevision == its last_processed_revision. The
    revision is synthesised HERE from a single counter, so frames merged from several sockets must
    still come out as one unbroken chain — otherwise every other frame is a 'gap' and the feed
    thrashes REST resyncs."""
    def book(ex, seq):
        return _broadcast(f"realtime:tournament:T:market:{ex}", "market_batch",
                          {"books": [{"exchangeId": ex, "asOf": {"sequence": seq},
                                      "bestBid": 0.5, "bestAsk": 0.52}],
                           "delivery": {"revision": seq, "previousRevision": seq - 1}})

    frames = [[book("A", 1), book("A", 2)], [book("B", 1), book("B", 2)]]
    connect = _connect_recorder(frames_per_socket=frames)
    got = []
    async with _session(_topics(100), connect) as ws:
        async for upd in ws:
            got.append(upd)
            if len(got) >= 4:
                break

    assert len(got) == 4, f"expected 4 merged updates, got {len(got)}"
    for prev, cur in zip(got, got[1:]):
        assert cur["previousRevision"] == prev["revision"], (
            f"discontinuity across shards: {prev['revision']} -> {cur['previousRevision']}")
    exchanges = {p["exchangeId"] for u in got for p in u["prices"]}
    assert exchanges == {"A", "B"}, f"both shards must contribute prices, got {exchanges}"


# --------------------------------------------------------------------------- #
# One token serves every socket (verified against the live venue)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_shard_socket_joins_with_the_session_token():
    """Checked against the live venue 2026-10-02: two sockets SHARING one token both joined fine
    (ok=5 refused=0 each), and POST /realtime/token returns the SAME token on repeat calls. So SIG
    does NOT bind a token to a connection and no per-shard token is needed."""
    connect = _connect_recorder(hold=True)
    async with _session(_topics(237), connect) as ws:
        await asyncio.sleep(0)
        assert len(connect.made) == 3
        for sock in connect.made:
            toks = {json.loads(m)["payload"]["access_token"]
                    for m in sock.sent if json.loads(m).get("event") == "phx_join"}
            assert toks == {"jwt"}, f"socket used an unexpected token set: {toks}"
