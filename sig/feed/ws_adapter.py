"""Supabase Realtime WebSocket wire-adapter — the `ws_factory`/`token_provider` seam for feed.py.

feed.py owns ALL the safety logic (revision continuity, gap→refetch→resync, staleness, reconnect
backoff, token refresh). This module is ONLY the wire: fetch the per-session Supabase credentials
(POST /realtime/token), connect to SIG's Supabase Realtime (Phoenix channels over a raw WebSocket),
join the private channels (the user channel + one per market), and yield each broadcast parsed into
the shape feed.py's run() consumes — `{revision, previousRevision, prices:[rows], fills:[...]}` that
`FeedUpdate.parse` understands.

CREDENTIALS COME FROM THE TOKEN RESPONSE, not static env vars. POST /realtime/token returns:
    {token, expiresAt, supabaseUrl, anonKey, channels:{user:"user:<uuid>"}}
  • supabaseUrl → the Supabase project URL (the WS host).
  • anonKey     → the realtime connection apikey (query param).
  • token       → the 3h access JWT (carried as access_token on every phx_join).
  • channels.user → the user channel name, already "user:<uuid>".
`supabase_token_provider` writes all four into a SHARED mutable dict; `make_ws_factory` reads the
host/apikey/channel out of that same dict when it builds the connection. feed.py calls the token
provider FIRST, then ws_factory(token), so the shared dict is always fresh when the socket is built.

PHOENIX PROTOCOL (v1 object serializer; v2 list serializer tolerated as a fallback):
  • URI:  wss://{host}/realtime/v1/websocket?apikey={anonKey}&vsn=1.0.0   (host = supabaseUrl w/o scheme)
  • phx_join (PRIVATE channel): {"topic":"realtime:{channel}","event":"phx_join",
        "payload":{"config":{"private":true,"broadcast":{"self":false,"ack":false},
                             "presence":{"key":""},"postgres_changes":[]},"access_token":"{jwt}"},
        "ref":"{n}","join_ref":"{n}"}.  The "realtime:" prefix is already on the topics we join.
  • heartbeat: {"topic":"phoenix","event":"heartbeat","payload":{},"ref":"{n}"} every ~25s.
  • a BROADCAST frame nests the real event + data:
        {"topic":"realtime:...","event":"broadcast",
         "payload":{"type":"broadcast","event":"market_batch"|"account_batch","payload":{...DATA...}}}
    → the real event is at frame.payload.event and the real data at frame.payload.payload.
  • control frames (phx_reply/phx_close/phx_error/phx_leave/presence_*/system, or topic "phoenix")
    are skipped.

GO-LIVE UNKNOWN (SIM cannot verify): the exact per-exchange order-book field names inside a
`market_batch` books[] entry are being captured in parallel against a live frame. `_books_to_price_rows`
is deliberately TOLERANT (bestBid/bestAsk, else top-of-book from bids[0]/asks[0], else skip) and logs
at debug what keys a book actually carries on the first few — verify against the capture before LIVE.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import structlog

log = structlog.get_logger()

# Phoenix control events that are NOT broadcast data — skipped by the parser.
_PHX_CONTROL = frozenset({
    "phx_reply", "phx_join", "phx_close", "phx_error", "phx_leave",
    "presence_state", "presence_diff", "heartbeat", "system",
})

# How many book shapes to dump at debug before we trust the key extraction (go-live unknown).
_BOOK_SHAPE_LOG_BUDGET = 5
_book_shape_logged = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── TTL from the token response's ABSOLUTE expiresAt (there is no expiresIn) ────────────────────
def _ttl_from_expires(expires_at: Any, expires_in: Any = None, *, ttl_default: float = 10800.0,
                      lead_s: float = 300.0, now: datetime | None = None) -> float:
    """ttl = max(60, (parse(expiresAt) - now) - 300s refresh lead). On a parse failure → ttl_default
    (10800s = 3h). `expires_in` is a defensive fallback only (the live API does not send it)."""
    if expires_at:
        try:
            s = str(expires_at).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            exp = datetime.fromisoformat(s)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            delta = (exp - (now or _utcnow())).total_seconds() - lead_s
            return max(60.0, delta)
        except (ValueError, TypeError):
            return ttl_default
    if expires_in is not None:
        try:
            return float(expires_in)
        except (ValueError, TypeError):
            return ttl_default
    return ttl_default


def _result_to_dict(result: Any) -> dict:
    """POST /realtime/token via SigClient.raw returns an HttpResult (NOT parsed JSON) — we must
    json.loads(result.body). Tolerant: accepts an already-parsed dict, an HttpResult (.body), or a
    raw str/bytes body."""
    if isinstance(result, dict):
        return result
    body = getattr(result, "body", None)
    if body is None and isinstance(result, (str, bytes, bytearray)):
        body = result
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("could not parse /realtime/token response body as JSON") from exc
        return parsed if isinstance(parsed, dict) else {}
    return {}


def supabase_token_provider(client: Any, shared: dict | None = None, *,
                            ttl_default: float = 10800.0) -> Callable[[], Awaitable[tuple[str, float]]]:
    """Return feed.py's token_provider: POST /realtime/token (NOT GET — GET 405s), json.loads the body,
    store supabaseUrl/anonKey/token/channels/expiresAt into the SHARED mutable dict the ws_factory reads,
    and return (jwt, ttl_seconds). The SigClient is sync, so the call runs on a worker thread."""
    bag = shared if shared is not None else {}

    async def _provider() -> tuple[str, float]:
        if hasattr(client, "raw"):
            result = await asyncio.to_thread(client.raw, "POST", "/realtime/token")
        else:  # pragma: no cover - defensive; the real SigClient always has .raw
            result = await asyncio.to_thread(client.get, "/realtime/token")
        data = _result_to_dict(result)
        token = data.get("token") or data.get("accessToken") or data.get("jwt")
        if not token:
            raise RuntimeError("no token in POST /realtime/token response")
        ttl = _ttl_from_expires(data.get("expiresAt"), data.get("expiresIn"), ttl_default=ttl_default)
        # publish the connection params for the ws_factory (read at __aenter__, after this returns)
        bag["token"] = str(token)
        bag["supabaseUrl"] = data.get("supabaseUrl") or data.get("supabase_url")
        bag["anonKey"] = data.get("anonKey") or data.get("anon_key") or data.get("apikey")
        bag["channels"] = data.get("channels") or {}
        bag["expiresAt"] = data.get("expiresAt")
        return str(token), ttl

    return _provider


# ── book → price-row extraction (TOLERANT; exact field names are a go-live unknown) ──────────────
def _to_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _level_price(level: Any) -> float | None:
    """Top-of-book price from one book level: a dict ({price|px|p}), a [price, size] pair, or a bare
    number."""
    if isinstance(level, dict):
        for k in ("price", "px", "p"):
            if level.get(k) is not None:
                return _to_float(level[k])
        return None
    if isinstance(level, (list, tuple)) and level:
        return _to_float(level[0])
    return _to_float(level)


def _best_price(book: dict, side: str) -> float | None:
    """TOLERANT best-bid/best-ask: explicit book.bestBid/bestAsk if present, else top-of-book from
    book.bids[0]/book.asks[0], else None."""
    if side == "bid":
        v = book.get("bestBid", book.get("best_bid"))
        levels = book.get("bids")
    else:
        v = book.get("bestAsk", book.get("best_ask"))
        levels = book.get("asks")
    if v is not None:
        return _to_float(v)
    if isinstance(levels, (list, tuple)) and levels:
        return _level_price(levels[0])
    return None


def _ver_newer(a: tuple, b: tuple) -> bool:
    """Is version a (sequence, at) newer than b? Numeric sequence first, string `at` as tiebreak.
    A known sequence beats an unknown one; otherwise fall back to the `at` stamp."""
    a_seq, a_at = _to_float(a[0]), a[1]
    b_seq, b_at = _to_float(b[0]), b[1]
    if a_seq is not None and b_seq is not None:
        if a_seq != b_seq:
            return a_seq > b_seq
    elif a_seq is not None or b_seq is not None:
        return a_seq is not None            # a known sequence wins over an unknown one
    return str(a_at or "") > str(b_at or "")


def _books_to_price_rows(books: Any, book_versions: dict | None) -> list[dict]:
    """Convert a market_batch books[] list → price rows for store.upsert_prices.

    Apply BY VERSION (asOf.sequence, tiebreak asOf.at): within one batch the newest book per exchange
    wins; across batches a book whose sequence is <= the last applied for that exchange is dropped as
    stale (so a duplicate delivery.revision carrying a newer book still updates, and a re-sent older
    book never regresses). `book_versions` (per ws_factory, persists across reconnects) holds the
    last-applied sequence per exchange; pass None to disable the cross-batch guard (unit tests)."""
    global _book_shape_logged
    now = _iso(_utcnow())
    chosen: dict[str, tuple[Any, Any, dict]] = {}   # exchangeId -> (sequence, at, row)
    for book in books or []:
        if not isinstance(book, dict):
            continue
        ex = book.get("exchangeId", book.get("exchange_id", book.get("id")))
        if ex is None:
            continue
        ex = str(ex)
        if _book_shape_logged < _BOOK_SHAPE_LOG_BUDGET:
            log.debug("ws_book_shape", exchange_id=ex, keys=sorted(book.keys()))
            _book_shape_logged += 1
        asof = book.get("asOf") if isinstance(book.get("asOf"), dict) else {}
        seq = asof.get("sequence")
        if seq is None:
            seq = book.get("sequence")
        at = asof.get("at")
        bid = _best_price(book, "bid")
        ask = _best_price(book, "ask")
        if bid is None and ask is None:
            continue                                  # neither side derivable → skip this book
        spread = book.get("spread")
        if spread is None and bid is not None and ask is not None:
            spread = round(ask - bid, 6)
        row = {"exchangeId": ex, "bestBid": bid, "bestAsk": ask, "spread": spread,
               "sequence": seq, "updated_at": now}
        prev = chosen.get(ex)
        if prev is None or _ver_newer((seq, at), (prev[0], prev[1])):
            chosen[ex] = (seq, at, row)

    rows: list[dict] = []
    for ex, (seq, _at, row) in chosen.items():
        if book_versions is not None and seq is not None:
            last = book_versions.get(ex)
            seq_f, last_f = _to_float(seq), _to_float(last)
            if last_f is not None and seq_f is not None and seq_f <= last_f:
                continue                              # stale vs last applied — drop (no regression)
            book_versions[ex] = seq
        rows.append(row)
    return rows


# ── broadcast → FeedUpdate-shaped dict ──────────────────────────────────────────────────────────
def _normalise_frame(raw: Any):
    """Decode one WS frame into (topic, event, payload) for either Phoenix serializer, or None."""
    msg = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if isinstance(msg, list) and len(msg) >= 5:          # v2: [join_ref, ref, topic, event, payload]
        return msg[2], msg[3], msg[4]
    if isinstance(msg, dict):                             # v1: {topic, event, payload, ref}
        return msg.get("topic"), msg.get("event"), msg.get("payload")
    return None


def _parse_market_batch(data: dict, book_versions: dict | None) -> dict | None:
    """market_batch data → an intermediate dict {_market, _resync, prices, fills}.

    IMPORTANT — revision handling. SIG runs one INDEPENDENT delivery.revision stream PER MARKET channel
    (market 388 goes 1179→1180 while market 512 goes 40→41). feed.py tracks ONE global
    last_processed_revision, so if we fed the per-market delivery.revision straight in, every other
    market's frame would look like a gap → a REST resync on essentially every frame → the WS would be
    no better than polling. We DON'T do that. Each market_batch carries the FULL current books (not
    deltas — confirmed against a live frame), and `_books_to_price_rows` dedups per exchange by
    asOf.sequence, so a frame is self-contained and safe to apply on its own. The SESSION therefore
    rewrites these into a single CONTINUOUS synthetic stream (see _RealtimeSession.__anext__), and the
    per-market delivery.revision is used only to detect an explicit resync need.

    resyncRequired is honoured: a truthy compact batch (server says "you missed too much, re-fetch")
    maps to `_resync=True`, which the session turns into feed.py's gap→REST-resync path."""
    rows = _books_to_price_rows(data.get("books"), book_versions)
    return {"_market": True, "_resync": bool(data.get("resyncRequired")), "prices": rows, "fills": []}


def _parse_account_batch(data: dict) -> dict | None:
    """account_batch data → a marker dict carrying only the fills. feed.py AUDITS fills on a
    continuous update (it never writes authoritative order state — the OMS is the single writer), so
    the session anchors these fills onto the last market revision (see _RealtimeSession.__anext__) to
    ride the price stream as a no-op continuous update.

    TODO(oms-reconcile): real-time position updates from account_batch (fills + orderUpdates) should
    route through the OMS reconciliation hook, NOT a direct state write. We deliberately only
    observe + audit here (safe), and never upsert positions, so we do not fight the authoritative OMS."""
    fills = data.get("fills")
    return {"_account": True, "fills": list(fills) if isinstance(fills, list) else []}


def _parse_broadcast(frame: Any, book_versions: dict | None = None) -> dict | None:
    """Map one Supabase/Phoenix frame → a FeedUpdate-shaped dict (market_batch), an account marker
    ({_account:True, fills}), or None for a control frame / unknown broadcast. The real event is
    NESTED at frame.payload.event and the real data at frame.payload.payload."""
    parts = _normalise_frame(frame)
    if parts is None:
        return None
    topic, event, payload = parts
    if str(topic or "") == "phoenix":
        return None
    if str(event or "").lower() != "broadcast":          # control frame (phx_reply, presence, …)
        return None
    inner = payload if isinstance(payload, dict) else {}
    inner_event = inner.get("event")
    inner_data = inner.get("payload")
    inner_data = inner_data if isinstance(inner_data, dict) else {}
    if inner_event == "market_batch":
        return _parse_market_batch(inner_data, book_versions)
    if inner_event == "account_batch":
        return _parse_account_batch(inner_data)
    return None                                           # unknown broadcast event — skip


async def _default_connect(uri: str, headers: dict | None = None):  # pragma: no cover - live socket
    """Default WS connect via the `websockets` library (Phoenix speaks raw WS). ping_interval is None
    — Phoenix has its own heartbeat (see _RealtimeSession._heartbeat)."""
    import websockets  # lazy: tests inject `connect=`, so websockets isn't needed to import this module
    return await websockets.connect(uri, open_timeout=15, ping_interval=None,
                                    additional_headers=headers or {})


# Queue sentinel: a shard's socket ended. A distinct object (not None or "") so it can never
# collide with a real frame.
_SHARD_DOWN = object()


class _RealtimeSession:
    """One Supabase Realtime SESSION — possibly SHARDED over several sockets — as an async context
    manager + async-iterator of parsed updates.
    feed.py uses it as:  `async with ws_factory(token) as ws:  async for raw in ws: ...`.

    WHY SHARDING. Supabase Realtime caps channels-per-connection (~100) and joining more SILENTLY
    stops streaming (measured: 237 channels -> 0 updates applied; 40 -> 186). One market = one
    channel, so a single socket cannot cover a 237-market board: the first ~90 streamed and the
    remaining 147 were never subscribed at all, froze at their connect-snapshot price, and were
    (correctly) skipped by the trader as stale. We therefore open ceil(N/max_channels_per_connection)
    sockets and spread the market channels across them.

    WHY THIS IS SAFE w.r.t. the continuity invariant. The revision feed.py checks is SYNTHESISED HERE
    (`_emit_rev` + 1 per emitted update — see __anext__), not taken from the server. Frames from every
    socket are merged into ONE queue and numbered by ONE counter, so feed.py still sees a single
    strictly-continuous stream and is wholly unaware of the sharding.

    TWO INVARIANTS the sharding must not break:
      • The USER (account) channel is joined on exactly ONE socket. Joining it per-shard would
        deliver every fill N times, and account_batch frames carry fills — i.e. duplicate fills.
      • If ANY socket drops, the WHOLE session ends. A session still streaming 2 of 3 shards is a
        feed that lies by omission: the dropped shard's markets would silently freeze while the feed
        still reported healthy. Ending the session routes into feed.py's existing reconnect + REST
        authoritative-resync path, which is exactly the right recovery.

    Reads the connection params (supabaseUrl host, anonKey, user channel) out of the SHARED dict that
    the token provider populated for this session."""

    def __init__(self, token: str, *, shared: dict, topics_provider: Callable[[], list[str]],
                 connect: Callable[..., Awaitable[Any]], heartbeat_s: float = 25.0,
                 book_versions: dict | None = None,
                 max_channels_per_connection: int = 80,
                 token_factory: Callable[[], Awaitable[str]] | None = None) -> None:
        self._token = token
        self._shared = shared
        self._topics_provider = topics_provider
        self._connect = connect
        self._heartbeat_s = heartbeat_s
        self._book_versions = book_versions if book_versions is not None else {}
        self._max_channels = max(1, int(max_channels_per_connection))
        # One token serves EVERY shard socket. Verified against the live venue 2026-10-02: two
        # sockets sharing one token both joined fine (ok=5 refused=0 each), and POST /realtime/token
        # returns the SAME token on repeat calls — so a per-shard token would be identical anyway.
        self._token_factory = token_factory   # accepted for compatibility; not needed
        self._sockets: list[Any] = []
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closing = False
        self._ref = 0
        # Synthetic CONTINUOUS revision the adapter presents to feed.py (NOT the per-market
        # delivery.revision — see _parse_market_batch). Primed lazily from feed.py's post-snapshot
        # last_processed_revision (read via shared["feed_health"]) so the first emitted update is
        # continuous; then advanced by +1 per emitted market update.
        self._emit_rev: int | None = None

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    @staticmethod
    def _build_uri(supabase_url: str | None, anon: str | None) -> str:
        host = supabase_url or ""
        for scheme in ("https://", "http://", "wss://", "ws://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
                break
        host = host.rstrip("/")
        uri = f"wss://{host}/realtime/v1/websocket?"
        if anon:
            uri += f"apikey={anon}&"
        return uri + "vsn=1.0.0"

    def _shard_topics(self) -> list[list[str]]:
        """Split the topics into per-socket shards of at most `max_channels_per_connection`.

        The USER channel (there is at most one) is pinned to shard 0 and counts against its budget;
        it must never be duplicated across shards or every fill arrives N times."""
        topics = list(self._topics_provider() or [])
        user = [t for t in topics if ":market:" not in t]
        markets = [t for t in topics if ":market:" in t]
        shards: list[list[str]] = []
        head = user[:]                                    # shard 0 carries the account channel
        room = max(1, self._max_channels - len(head))
        shards.append(head + markets[:room])
        rest = markets[room:]
        for i in range(0, len(rest), self._max_channels):
            shards.append(rest[i:i + self._max_channels])
        return [sh for sh in shards if sh]

    def _join_frame(self, topic: str, token: str) -> str:
        ref = self._next_ref()
        return json.dumps({
            "topic": topic,
            "event": "phx_join",
            "payload": {
                "config": {
                    "private": True,
                    "broadcast": {"self": False, "ack": False},
                    "presence": {"key": ""},
                    "postgres_changes": [],
                },
                "access_token": token,
            },
            "ref": ref,
            "join_ref": ref,
        })

    async def _token_for_shard(self, idx: int) -> str:
        """Every shard uses the session token — see __init__. Kept as a seam in case SIG later ties
        a token to one connection, which it does NOT today."""
        return self._token

    async def __aenter__(self) -> "_RealtimeSession":
        supabase_url = self._shared.get("supabaseUrl")
        anon = self._shared.get("anonKey")
        if not supabase_url:
            raise RuntimeError("no supabaseUrl in the shared token response — cannot build WS URI")
        uri = self._build_uri(supabase_url, anon)
        shards = self._shard_topics()
        # Open one socket per shard and join that shard's channels. The "realtime:" prefix is already
        # on each topic (topics_provider builds the full topic strings).
        for idx, topics in enumerate(shards):
            token = await self._token_for_shard(idx)
            ws = await self._connect(uri)
            self._sockets.append(ws)
            for topic in topics:
                await ws.send(self._join_frame(topic, token))
            self._tasks.append(asyncio.ensure_future(self._heartbeat(ws)))
            self._tasks.append(asyncio.ensure_future(self._reader(ws, idx)))
        log.info("ws_sharded_session_open", sockets=len(shards),
                 channels=sum(len(t) for t in shards),
                 max_channels_per_connection=self._max_channels,
                 per_socket=[len(t) for t in shards])
        return self

    async def __aexit__(self, *_exc) -> None:
        self._closing = True
        for t in self._tasks:
            t.cancel()
        for ws in self._sockets:
            try:
                await ws.close()
            except Exception:
                pass
        self._tasks.clear()
        self._sockets.clear()

    async def _reader(self, ws: Any, idx: int) -> None:
        """Pump one socket's frames into the shared queue. On end-of-socket push the STOP sentinel:
        a dead shard ends the whole session (see the class docstring) rather than silently dropping
        that shard's markets.

        Also reports each shard's JOIN verdict and its first broadcast. Without this a shard whose
        phx_join was refused looks identical to a quiet market: the socket stays open, nothing
        arrives, and the board silently goes dark."""
        joins_ok = joins_err = 0
        first_broadcast = False
        try:
            while True:
                msg = await ws.recv()
                # observe-only bookkeeping; never alters what is forwarded
                try:
                    d = json.loads(msg)
                    ev = d.get("event")
                    if ev == "phx_reply":
                        status = ((d.get("payload") or {}).get("status"))
                        if status == "ok":
                            joins_ok += 1
                        else:
                            joins_err += 1
                            if joins_err <= 2:
                                log.warning("ws_join_refused", shard=idx, status=status,
                                            topic=d.get("topic"),
                                            response=str((d.get("payload") or {}).get("response"))[:200])
                        if (joins_ok + joins_err) % 40 == 0:
                            log.info("ws_join_progress", shard=idx, ok=joins_ok, refused=joins_err)
                    elif ev in ("broadcast", "phx_error", "phx_close"):
                        if ev != "broadcast":
                            log.warning("ws_channel_event", shard=idx, event=ev,
                                        topic=d.get("topic"))
                        elif not first_broadcast:
                            first_broadcast = True
                            log.info("ws_shard_first_broadcast", shard=idx,
                                     joins_ok=joins_ok, joins_refused=joins_err)
                except Exception:
                    pass
                await self._queue.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                log.warning("ws_shard_closed", shard=idx, error=type(exc).__name__,
                            note="ending the whole session → feed.py reconnect + REST resync")
            await self._queue.put(_SHARD_DOWN)

    async def _heartbeat(self, ws: Any) -> None:
        """Phoenix heartbeat for ONE socket. Every shard needs its own or the server reaps it."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_s)
                hb = {"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": self._next_ref()}
                try:
                    await ws.send(json.dumps(hb))
                except Exception:
                    return  # connection gone; the reader side ends the session
        except asyncio.CancelledError:
            return

    def _primed_rev(self) -> int:
        """feed.py's current last_processed_revision (set by the connect REST snapshot), so our first
        emitted update is continuous. Falls back to 0 if feed health isn't wired."""
        health = self._shared.get("feed_health")
        lp = getattr(health, "last_processed_revision", None) if health is not None else None
        try:
            return int(lp)
        except (TypeError, ValueError):
            return 0

    def __aiter__(self) -> "_RealtimeSession":
        return self

    async def __anext__(self) -> dict:
        while True:
            msg = await self._queue.get()
            if msg is _SHARD_DOWN:
                # Any shard dropping ends the session → feed.py reconnects + REST-refetches. Never
                # keep streaming a partial board.
                raise StopAsyncIteration
            parsed = _parse_broadcast(msg, book_versions=self._book_versions)
            if parsed is None:
                continue                                  # control frame / unknown broadcast

            # ── account_batch: ride the current synthetic revision as a continuous no-op carrying fills
            if parsed.get("_account"):
                fills = parsed.get("fills") or []
                if not fills:
                    continue
                if self._emit_rev is None:
                    # No market frame yet this session → no continuous anchor; OBSERVE + log only.
                    log.info("ws_account_fills_preanchor", n=len(fills))
                    continue
                log.info("ws_account_fills", n=len(fills), anchor_rev=self._emit_rev)
                return {"revision": self._emit_rev, "previousRevision": self._emit_rev,
                        "prices": [], "fills": fills}

            # ── market_batch: present feed.py a single CONTINUOUS synthetic stream
            if parsed.get("_resync"):
                # Explicit server resync request → force feed.py's gap→REST path, then re-prime.
                prev = self._emit_rev
                self._emit_rev = None                     # next market frame re-primes from feed health
                log.info("ws_resync_required", prev_rev=prev)
                return {"revision": (prev or 0), "previousRevision": None, "prices": [], "fills": []}

            if self._emit_rev is None:
                self._emit_rev = self._primed_rev()       # == feed.py last_processed → first is continuous
            prev_rev = self._emit_rev
            self._emit_rev = prev_rev + 1
            return {"revision": self._emit_rev, "previousRevision": prev_rev,
                    "prices": parsed.get("prices") or [], "fills": parsed.get("fills") or []}


def make_ws_factory(*, shared: dict, topics_provider: Callable[[], list[str]],
                    connect: Callable[..., Awaitable[Any]] | None = None,
                    heartbeat_s: float = 25.0,
                    max_channels_per_connection: int = 80,
                    token_factory: Callable[[], Awaitable[str]] | None = None
                    ) -> Callable[[str], _RealtimeSession]:
    """Build the `ws_factory(token)` feed.py injects. Reads supabaseUrl/anonKey/channels out of
    `shared` (populated by supabase_token_provider), joins the topics from `topics_provider()`, and
    yields parsed updates. `connect` is injectable (default: the websockets library) so tests drive
    the adapter with a fake socket. `book_versions` is per-factory so per-exchange book ordering
    survives reconnects within a process."""
    conn = connect or _default_connect
    book_versions: dict = {}

    def _factory(token: str) -> _RealtimeSession:
        return _RealtimeSession(token, shared=shared, topics_provider=topics_provider,
                                connect=conn, heartbeat_s=heartbeat_s, book_versions=book_versions,
                                max_channels_per_connection=max_channels_per_connection,
                                token_factory=token_factory)

    return _factory


def build_realtime_feed(*, store: Any, client: Any, config: dict | None, exchange_ids: list[str],
                        tournament_id: str, market_ids: list[str], connect=None,
                        on_resync: Callable[[str], Any] | None = None, heartbeat_s: float = 25.0,
                        max_channels_per_connection: int = 80):
    """Assemble a fully-wired RealtimeFeed from the POST /realtime/token response (NOT static env):

      • token_provider → supabase_token_provider(client, shared): POSTs the token, populates `shared`
        with supabaseUrl/anonKey/token/channels, returns (jwt, ttl-from-expiresAt).
      • ws_factory     → make_ws_factory(shared, topics_provider): builds the wss URI from `shared`,
        joins the user channel (from shared.channels.user) + one topic per market
        ("realtime:tournament:{tid}:market:{mid}"), and yields parsed market_batch/account_batch updates.
      • snapshot_fetcher → LEFT as feed.py's REST default (GET /exchanges/prices via client+exchange_ids):
        the resync/fallback feed.py applies on every (re)connect and gap. The REST safety net is KEPT.

    feed.py calls token_provider() THEN ws_factory(token) per session, so `shared` is fresh (host,
    apikey, user channel) by the time the socket is built."""
    from sig.feed.feed import RealtimeFeed, FeedConfig

    shared: dict = {}

    def _topics_provider() -> list[str]:
        topics: list[str] = []
        channels = shared.get("channels") or {}
        user = channels.get("user")
        if user:
            topics.append("realtime:" + str(user))      # channels.user is already "user:<uuid>"
        for mid in market_ids or []:
            topics.append(f"realtime:tournament:{tournament_id}:market:{mid}")
        return topics

    token_provider = supabase_token_provider(client, shared)

    async def _shard_token() -> str:
        """A FRESH token for each additional shard socket. SIG binds a token to one connection, so
        every socket needs its own; the provider is re-callable and each call POSTs /realtime/token."""
        tok, _ttl = await token_provider()
        return tok

    feed = RealtimeFeed(
        store,
        config=FeedConfig.from_config(config),
        ws_factory=make_ws_factory(shared=shared, topics_provider=_topics_provider,
                                   connect=connect, heartbeat_s=heartbeat_s,
                                   max_channels_per_connection=max_channels_per_connection,
                                   token_factory=_shard_token),
        token_provider=token_provider,
        # snapshot_fetcher left as the default → REST GET /exchanges/prices (resync/fallback KEPT)
        client=client,
        exchange_ids=exchange_ids,
        on_resync=on_resync,
    )
    # Let the WS session read feed.py's last_processed_revision (set by the connect REST snapshot) so it
    # can prime its synthetic continuous stream and the first emitted update is continuous, not a gap.
    shared["feed_health"] = feed.health
    return feed
