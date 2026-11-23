"""SIG order-execution WRITE PATH (async) — sig/broker.py.

The SIG-specific order methods on top of the existing, proven pieces (do NOT rewrite those):
  - auth / keys / Cloudflare User-Agent  ← sig.env + sig.sig_client (USER_AGENT, DEFAULT_BASE_URL)
  - HTTP-status → retry classification    ← sig.write_gate (classify / RetryAction / HttpResult)
  - sustained-429 circuit-breaker         ← infra.safeguards.RateLimitBreaker

Net-new here (and required to be async / non-blocking, Python 3.13 asyncio):
  1. SigBroker implementing the BrokerAPI Protocol (swappable with a future SimulatedBroker).
  2. Idempotency core: a DETERMINISTIC key from (strategy_id, market_id, side, intent_hash) — never
     random — with query-before-submit, submit-only-if-new, and reconcile-on-ambiguous (never a blind
     retry or double-submit).
  3. 429 / Retry-After handler: on 429 read Retry-After and back off EXACTLY that, retry with the SAME
     idempotency key (so a retry can never double-place); exponential backoff on repeat 429s; max-retry cap.
  4. Outbound write-gate: async token bucket, 30 writes/min per key, QUEUES when full, with a priority flag
     so arb orders jump ahead of routine quoting.

Keys come only from gitignored config/env (sig.env); a key is never hardcoded, printed, or logged.
HTTP is fully injectable (`transport=`) so tests mock it and NO live order is ever placed.
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import structlog

from sig.env import load_env, require
from sig.sig_client import DEFAULT_BASE_URL, USER_AGENT
from sig.write_gate import HttpResult, RetryAction, classify
from infra.safeguards import RateLimitBreaker

log = structlog.get_logger()

# SIG order status → normalized vocabulary (terminal flag used by callers / reconcile).
_STATUS_MAP = {
    "open": "open", "resting": "open", "pending": "open", "accepted": "open", "working": "open",
    "partially_filled": "partially_filled", "partial": "partially_filled",
    "filled": "filled", "matched": "filled", "executed": "filled",
    "canceled": "canceled", "cancelled": "canceled", "expired": "expired",
    "rejected": "rejected", "failed": "rejected",
}
_TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})


def norm_status(s: object) -> str:
    return _STATUS_MAP.get(str(s or "").lower(), "open")


#: Terminal reason codes SIG reports on a closed order, mapped to our vocabulary.
_TERMINAL_REASON_MAP = {
    "expired": "expired",
    "cancelled": "canceled", "canceled": "canceled",
    "selftradeprevented": "canceled",
    "rejected": "rejected",
}


def status_from_order(d: dict) -> str:
    """Normalised status for a SIG order payload.

    *** SIG ORDERS HAVE NO `status` FIELD. *** The documented object is
    {id, exchangeId, side, action, quantity, priceLimit, open, createdAt, expirationDate}, and the
    placement response adds {orderId, remainingQuantity, quantityTraded, fillPrice,
    terminalReasonCode, totalCost}. Reading `status` therefore always missed, and
    `norm_status(None)` defaulted to "open" — so EVERY order looked resting forever and no fill was
    ever recognised. State is derived from `open` instead:

        open=true  + nothing traded  -> open
        open=true  + partly traded   -> partially_filled   ("open" means resting, not unfilled)
        open=false + fully traded    -> filled
        open=false + otherwise       -> terminalReasonCode, else canceled

    An explicit `status` is still honoured first, so a reporting-backed payload that does carry one
    (GET /orders accepts status=open|closed|expired|all as a FILTER) is not second-guessed.
    """
    if not isinstance(d, dict):
        return "open"
    if d.get("status") is not None:
        return norm_status(d.get("status"))
    if d.get("open") is None:
        return "open"                       # nothing to derive from; caller reconciles

    traded = _as_float(d.get("quantityTraded"))
    qty = _as_float(d.get("quantity"))
    if bool(d.get("open")):
        return "partially_filled" if traded > 0.0 else "open"
    # closed
    if traded > 0.0 and (qty <= 0.0 or traded + 1e-9 >= qty):
        return "filled"
    reason = str(d.get("terminalReasonCode") or "").strip().lower()
    mapped = _TERMINAL_REASON_MAP.get(reason)
    if mapped:
        return mapped
    if traded > 0.0:
        return "partially_filled"           # closed with a partial fill and no reason code
    return "canceled"


def _as_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def filled_qty_from_order(d: dict) -> float:
    """Filled quantity from a SIG payload.

    `filledQuantity` (what the old code read) does not exist. The placement response reports
    `quantityTraded`; a plain Order object reports none at all, and its `quantity` field CANNOT be
    used as a proxy — the spec says that while `open` is true it is the executable REMAINDER, and a
    closed order reports "its unfilled remainder plus everything it filled". So an absent traded
    figure is reported as 0.0 and the fill is learned from GET /orders/{id}/fills instead.
    """
    if not isinstance(d, dict):
        return 0.0
    for k in ("quantityTraded", "filledQuantity", "filled"):
        if d.get(k) is not None:
            return _as_float(d.get(k))
    return 0.0


def avg_price_from_order(d: dict) -> float:
    if not isinstance(d, dict):
        return 0.0
    for k in ("fillPrice", "averagePrice", "avgPrice"):
        if d.get(k) is not None:
            return _as_float(d.get(k))
    return 0.0


# --------------------------------------------------------------------------- #
# Idempotency key — DETERMINISTIC (never random)
# --------------------------------------------------------------------------- #
def intent_hash(*, exchange_id: str, action: str, quantity: int, price: float | None) -> str:
    """Stable hash of the concrete order intent (what, how much, at what price). Part of the
    idempotency key so two DIFFERENT intents never collide, but a re-attempt of the SAME intent reuses
    the key (and therefore dedupes)."""
    payload = json.dumps({"exchange_id": str(exchange_id), "action": str(action).lower(),
                          "quantity": int(quantity), "price": (None if price is None else round(float(price), 3))},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _ownership_denies(strategy_id, exchange_ids) -> str | None:
    """Reason string if any exchange is OWNED by another strategy (sig/engine/ownership.py), else None.
    Fails CLOSED for owned markets: if the registry cannot be consulted, orders are refused."""
    try:
        from sig.engine.ownership import registry as _own
        reg = _own()
        for ex in exchange_ids:
            if not reg.allows(strategy_id, ex):
                log.warning("order_refused_market_owned", exchange=str(ex), caller=str(strategy_id),
                            owner=reg.owner_of(ex))
                return f"market_owned_by:{reg.owner_of(ex)}"
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("ownership_check_failed_refusing", error=type(exc).__name__)
        return "ownership_check_failed"


class _OwnedAtSend(Exception):
    """Raised inside a write's request callable when the market became owned by another strategy while the
    write waited in the gate queue (the pre-queue check passed), or the write went STALE in the queue (its
    deadline passed: the price it was computed from is too old). Nothing is sent."""


def _guarded(make_request, strategy_id, exchange_ids, deadline: float | None = None):
    """Wrap a write's request callable so ownership is RE-CHECKED at send time (after the gate wait, and on
    every retry) — closes the race where an order queued before a claim lands in the newly owned market.
    `deadline` (time.monotonic()): refuse to send after it (a quote priced off a book that is now too old)."""
    async def _req():
        if deadline is not None and time.monotonic() > deadline:
            raise _OwnedAtSend("stale_at_send")
        denied = _ownership_denies(strategy_id, exchange_ids)
        if denied:
            raise _OwnedAtSend(denied)
        return await make_request()
    return _req


def _ownership_note(strategy_id, order_id, exchange_id) -> None:
    try:
        from sig.engine.ownership import registry as _own
        _own().note_order(str(strategy_id), order_id, exchange_id)
    except Exception:  # noqa: BLE001 — bookkeeping never fails a placement
        pass


def idempotency_key(strategy_id: str, market_id: str, side: str, intent_h: str, *, prefix: str = "sig") -> str:
    """Deterministic idempotency key from (strategy_id, market_id, side, intent_hash). Same inputs →
    same key → a retry/reconnect/restart of the same logical order dedupes at the server."""
    raw = f"{strategy_id}|{market_id}|{str(side).lower()}|{intent_h}"
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:40]}"


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class OrderResult:
    idempotency_key: str
    status: str                       # normalized (open|partially_filled|filled|canceled|rejected|unknown)
    order_id: str | None = None
    filled_qty: float = 0.0
    avg_price: float = 0.0
    was_noop: bool = False            # True = an existing order matched the key (dedup; no new submit)
    reconciled: bool = False          # True = resolved an ambiguous response by re-query
    ambiguous: bool = False           # True = outcome could not be confirmed (surfaced; NOT blind-retried)
    legs: list[dict] = field(default_factory=list)   # multi-leg per-leg detail
    raw: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


# --------------------------------------------------------------------------- #
# Read snapshots for LIVE reconciliation (venue-truth). These STRUCTURALLY satisfy the OMS
# protocols (sig/oms/protocols.py BrokerPosition / BrokerOrderSnapshot) and the orchestrator's
# AccountSnapshot (sig/engine/protocols.py) — no inheritance, duck-typed by field name.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BrokerPositionSnapshot:
    contract_id: str
    side: str               # "long" | "short" | "flat"
    qty: float
    avg_entry_price: float


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    order_id: str
    contract_id: str
    side: str
    qty: float
    filled_qty: float
    avg_fill_price: float
    status: str


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    buying_power: float
    equity: float


def _as_rows(resp: Any) -> list[dict]:
    """SIG read endpoints return either a bare list or {'data': [...]} (sometimes {'positions': [...]})."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for k in ("data", "positions", "orders", "results"):
            v = resp.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _first(row: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _positions_market_value(resp: Any) -> float:
    """Total mark-to-market value of open positions from a /portfolio/positions response.

    Prefers the venue's own `summary.totalMarketValue`; else sums each row's signed `marketValue`
    (the venue value, correct for NO/short legs — never qty×price, whose sign is ambiguous). An
    unrecognised shape yields 0.0, so NAV degrades to free cash — conservative, never inflated."""
    if isinstance(resp, dict):
        summ = resp.get("summary")
        if isinstance(summ, dict):
            mv = _first(summ, "totalMarketValue", "total_market_value", "marketValue")
            if mv is not None:
                return _f(mv)
    total = 0.0
    for r in _as_rows(resp):
        total += _f(_first(r, "marketValue", "market_value", "value", default=0.0))
    return total


def _position_from_row(row: dict) -> BrokerPositionSnapshot:
    cid = str(_first(row, "exchangeId", "exchange_id", "contractId", "marketId", "id", default=""))
    raw_qty = _f(_first(row, "quantity", "qty", "size", "netQuantity", default=0.0))
    side = str(_first(row, "side", "direction", default="")).lower()
    if side not in ("long", "short", "flat"):
        side = "long" if raw_qty > 0 else "short" if raw_qty < 0 else "flat"
    # *** avgCost IS THE SIG FIELD NAME, and it was missing from this list. ***
    # The Position object is {avgCost, costBasis, currentPrice, exchangeId, lots, marketId,
    # marketTitle, marketValue, moneyEarned, option, quantity, settled, unrealizedPnl,
    # unrealizedPnlPct} -- no averagePrice/avgPrice/price/cost. So every lookup missed and this
    # fell through to 0.0, and since fill_handler / reconciliation / _sync_venue_positions all
    # persist this value, EVERY row in the positions table carried avg_price = 0.0.
    #
    # Two things broke downstream, both silently:
    #   * the orchestrator's price-stream exit path guards `if avg <= 0: continue`, so it skipped
    #     every position -- the fallback trailing stop never evaluated anything.
    #   * StateStore.mark_to_market computes quantity * (best_bid - avg_price), which against a
    #     zero avg reports the entire market value as unrealised profit.
    # costBasis/quantity is kept as a derivation fallback: cost basis is the money actually spent,
    # so it reconstructs the true average even if avgCost is ever absent.
    avg = _f(_first(row, "avgCost", "avg_cost", "averagePrice", "avgPrice", "avgEntryPrice",
                    "avg_entry_price", "price", "cost", default=0.0))
    if avg <= 0.0:
        basis = _f(_first(row, "costBasis", "cost_basis", default=0.0))
        if basis > 0.0 and abs(raw_qty) > 0.0:
            avg = basis / abs(raw_qty)
    return BrokerPositionSnapshot(
        contract_id=cid, side=side, qty=abs(raw_qty), avg_entry_price=avg,
    )


def _order_from_row(row: dict) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        order_id=str(_first(row, "id", "orderId", "order_id", default="")),
        contract_id=str(_first(row, "exchangeId", "exchange_id", "contractId", "marketId", default="")),
        side=str(_first(row, "side", "option", "direction", default="")).lower(),
        qty=_f(_first(row, "quantity", "qty", "size", default=0.0)),
        filled_qty=_f(_first(row, "filledQuantity", "filled", "filled_qty", "filledQty", default=0.0)),
        avg_fill_price=_f(_first(row, "averagePrice", "avgPrice", "fillPrice", "avg_fill_price", default=0.0)),
        status=norm_status(_first(row, "status", "state", default="open")),
    )


# --------------------------------------------------------------------------- #
# Outbound write-gate — async token bucket, 30/min per key, priority queue
# --------------------------------------------------------------------------- #
class WriteGate:
    """Async per-key outbound write limiter. Sliding-window token bucket (default 30 writes / 60 s).
    When the window is full, `acquire()` QUEUES (awaits); a `priority=True` acquirer jumps ahead of all
    routine acquirers (arb before quoting). Fair within a priority class by arrival order."""

    def __init__(self, writes_per_min: int = 30, window_s: float = 60.0) -> None:
        self.capacity = int(writes_per_min)
        self.window = float(window_s)
        self._sent: deque[float] = deque()
        self._cond = asyncio.Condition()
        self._heap: list[tuple[int, int]] = []   # (priority_rank, seq) — rank 0 = priority, 1 = routine
        self._seq = 0

    def _avail(self, now: float) -> int:
        while self._sent and self._sent[0] <= now - self.window:
            self._sent.popleft()
        return self.capacity - len(self._sent)

    def _next_free(self, now: float) -> float:
        return (self._sent[0] + self.window) if self._sent else now

    async def acquire(self, *, priority: bool = False) -> None:
        self._seq += 1
        me = (0 if priority else 1, self._seq)
        async with self._cond:
            heapq.heappush(self._heap, me)
            while True:
                now = time.monotonic()
                if self._heap[0] == me and self._avail(now) > 0:
                    heapq.heappop(self._heap)
                    self._sent.append(now)
                    self._cond.notify_all()      # wake the next front-of-heap waiter
                    return
                # wait until a slot frees (bounded by when the oldest send ages out) or a notify arrives
                timeout = max(self._next_free(now) - now, 0.0) if self._avail(now) == 0 else None
                try:
                    if timeout is None:
                        await self._cond.wait()
                    else:
                        await asyncio.wait_for(self._cond.wait(), timeout + 0.001)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    if me in self._heap:
                        self._heap.remove(me); heapq.heapify(self._heap)
                    self._cond.notify_all()
                    raise

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {"used": len(self._sent), "capacity": self.capacity, "available": self._avail(now),
                "queued": len(self._heap)}


# --------------------------------------------------------------------------- #
# Retry config
# --------------------------------------------------------------------------- #
@dataclass
class RetryConfig:
    retry_after_default_s: float = 60.0      # 429 with no Retry-After header
    backoff_multiplier: float = 2.0          # exponential growth on repeat 429s
    max_backoff_s: float = 600.0
    tx_conflict_backoff_s: tuple = (1.0, 2.0, 4.0, 8.0)   # 503 TX_CONFLICT
    max_retries: int = 5


# --------------------------------------------------------------------------- #
# BrokerAPI Protocol (SimulatedBroker will implement the same)
# --------------------------------------------------------------------------- #
@runtime_checkable
class BrokerAPI(Protocol):
    async def place_order(self, *, strategy_id: str, market_id: str, exchange_id: str, side: str,
                          action: str, quantity: int, price: float | None = None,
                          priority: bool = False) -> OrderResult: ...
    async def place_multi_leg(self, *, strategy_id: str, market_id: str, legs: list[dict],
                              priority: bool = True) -> OrderResult: ...
    async def cancel_order(self, order_id: str) -> OrderResult: ...
    async def cancel_all(self) -> OrderResult: ...
    async def get_order(self, order_id: str) -> OrderResult: ...
    # venue-truth reads for reconciliation (sourced from the SIG READ client; 100 reads/min, not writes)
    async def get_positions(self) -> list[BrokerPositionSnapshot]: ...
    async def get_open_orders(self) -> list[BrokerOrderSnapshot]: ...
    async def get_account(self) -> BrokerAccountSnapshot: ...


# transport signature: async (method, path, json_body|None, params|None) -> HttpResult
Transport = Callable[[str, str, dict | None, dict | None], Awaitable[HttpResult]]


def _sig_outcome(x: Any) -> str:
    """SIG OrderInput.side is the OUTCOME: 'yes' | 'no'. MM/arb set it from the venue book; default
    'yes' only as a last resort (callers always provide it now)."""
    s = str(x or "").strip().lower()
    return s if s in ("yes", "no") else "yes"


def _sig_direction(x: Any) -> str:
    """SIG OrderInput.action is the DIRECTION: 'buy' | 'sell'. The pipeline carries the direction as
    the leg/intent `side`; map defensively from open/close/long/short too."""
    s = str(x or "").strip().lower()
    if s in ("buy", "sell"):
        return s
    if s in ("open", "long", "add", "bid"):
        return "buy"
    if s in ("close", "short", "reduce", "ask"):
        return "sell"
    return "buy"


class SigBroker:
    """Async SIG order broker implementing BrokerAPI. HTTP is injectable for tests (`transport=`);
    the default transport is an httpx.AsyncClient with Bearer auth + the Cloudflare-safe User-Agent."""

    def __init__(self, *, key_env: str = "SIG_API_KEY_WRITE", base_url: str | None = None,
                 gate: WriteGate | None = None, retry: RetryConfig | None = None,
                 breaker: RateLimitBreaker | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None, timeout: float = 45.0,
                 read_timeout: float = 60.0, read_client: Any | None = None,
                 tournament_id: str | None = None, tournament_slug: str | None = None) -> None:
        load_env()
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # Routes every order to the tournament's isolated orderbook + balance (SIG OrderInput
        # .tournamentId). Omitting it sends orders to the key's default/global context — the wrong
        # book. Resolved once at startup (GET /tournaments/{slug}.id) and passed down.
        self._tournament_id = tournament_id
        # Slug for the tournament-SCOPED portfolio reads (positions/fills). None => unscoped.
        self._tournament_slug = tournament_slug
        self.gate = gate or WriteGate()
        self.retry = retry or RetryConfig()
        self.breaker = breaker or RateLimitBreaker(k_consecutive=3)
        self._sleep = sleep or asyncio.sleep
        # SIG's API is slow (/account, /orders, /portfolio can take 10–15s+ under load); the old 15s
        # default ReadTimeouts and (in LIVE) fails the startup credential check + every per-cycle
        # get_account, crash-looping the trader. Widen both the write and read timeouts.
        self._timeout = timeout
        self._read_timeout = read_timeout
        self._key_env = key_env
        self._transport = transport           # None → lazily build the real httpx client
        self._client = None
        self._key: str | None = None          # resolved lazily; never logged
        # READ client for venue-truth reconciliation reads (positions/orders/account). Injected in tests;
        # lazily built as a SigClient (its own SIG_API_KEY_READ auth) in production — reads are NOT
        # write-gated (separate 100 reads/min budget), so they never queue behind the 30/min write-gate.
        self._read_client = read_client
        # NAV (equity) = free cash + marked positions. get_account() computes it and stashes it here so
        # the frozen build-time balances can be refreshed to venue-truth equity each cycle (last_equity()).
        # The positions leg is TTL-cached so the per-cycle get_account stays cheap on the hot path.
        self._last_equity: float | None = None
        # ATTRIBUTION BUFFER: (venue_order_id -> strategy) captured HERE — the one universal placement
        # chokepoint — because SIG never echoes our idempotencyKey back on the order or the fill, so the
        # fill ledger (orderId only) otherwise can't be tied to a strategy. Zero hot-path cost: place_order
        # appends a tuple; a background loop drains it to the durable order_attribution table. Best-effort /
        # forward-only (a crash loses < one flush interval of mappings).
        self._attr_buf: list[tuple[str, str, str]] = []
        self._nav_mv_cache: float = 0.0
        self._nav_mv_ts: float = 0.0
        self._nav_mv_ttl_s: float = 60.0
        # RAW positions TTL cache: get_positions / get_positions_valued / NAV all read the SAME
        # /portfolio/positions endpoint; without sharing, one cycle hit it 3+ times and it became the
        # top /portfolio/positions 429 source (position-sync failures that stale the gate + exits). A
        # short shared TTL collapses the within-window duplicates to ONE read. Short enough (< the exit
        # pass cadence) that exits still size off near-fresh holdings; the venue clamps the close anyway.
        self._pos_raw_cache: Any = None
        self._pos_raw_ts: float = 0.0
        self._pos_raw_ttl_s: float = 20.0

    # ---- auth / transport (reuses the read client's auth + UA patterns) ----
    def _auth_headers(self) -> dict:
        if self._key is None:
            self._key = require(self._key_env)     # from gitignored .env; raises (redacted) if missing
        return {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT, "Accept": "application/json"}

    async def _http(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> HttpResult:
        if self._transport is not None:
            return await self._transport(method, path, json_body, params)
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base, timeout=self._timeout, headers=self._auth_headers())
        try:
            resp = await self._client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as e:
            # transport failure = AMBIGUOUS (status 0) → caller reconciles, never blind-retries
            return HttpResult(status=0, body=f"transport:{type(e).__name__}")
        ra = resp.headers.get("retry-after")
        retry_after = None
        if ra is not None:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = None
        return HttpResult(status=resp.status_code, body=resp.text, retry_after_s=retry_after)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---- the 429 / Retry-After + backoff engine (same idempotency key across retries) ----
    async def _send_write(self, make_request: Callable[[], Awaitable[HttpResult]], *, priority: bool,
                          label: str, retry_429: bool = True) -> tuple[RetryAction, HttpResult]:
        """Run a WRITE through the gate + the SIG retry policy. `make_request` re-issues the SAME request
        (same idempotencyKey) each attempt. Returns (final_action, last_result).
        retry_429=False: a 429 returns at once (no in-call backoff) — used for cancels: SIG answers a DELETE of an
        order that already left the book with a (late) 429, and backing off in-call blocked the trading cycle for
        38 min on 2026-10-10 (cycle_complete duration_s=2316). Callers already retry unconfirmed cancels later."""
        attempt = 0
        conflict_i = 0
        while True:
            await self.gate.acquire(priority=priority)   # QUEUE here when the 30/min window is full
            res = await make_request()
            act = classify(res.status, res.body)
            if act == RetryAction.OK:
                self.breaker.record_success()
                return act, res
            if act == RetryAction.NO_RETRY:
                return act, res
            if act == RetryAction.RECONCILE:             # 502 / transport(0): ambiguous — caller re-queries
                return act, res
            if act == RetryAction.RETRY_AFTER:           # 429
                self.breaker.record_rate_limit()
                if self.breaker.tripped:
                    log.error("sig_write_breaker_tripped", label=label, note="sustained 429 — STOP + surface")
                    return act, res
                if not retry_429:
                    log.warning("sig_write_429_no_retry", label=label)
                    return act, res
                attempt += 1
                if attempt > self.retry.max_retries:
                    return act, res
                base = res.retry_after_s if res.retry_after_s is not None else self.retry.retry_after_default_s
                backoff = min(base * (self.retry.backoff_multiplier ** (attempt - 1)), self.retry.max_backoff_s)
                log.warning("sig_write_429_backoff", label=label, attempt=attempt, backoff_s=backoff, same_key=True)
                await self._sleep(backoff)
                continue                                 # retry the SAME request (same idempotencyKey)
            if act == RetryAction.BACKOFF:               # 503 TX_CONFLICT
                attempt += 1
                if attempt > self.retry.max_retries:
                    return act, res
                w = self.retry.tx_conflict_backoff_s[min(conflict_i, len(self.retry.tx_conflict_backoff_s) - 1)]
                conflict_i += 1
                await self._sleep(w)
                continue

    # ---- query-before-submit ----
    async def _find_matching_order(self, *, exchange_id: str, action: str, quantity: int,
                                   price: float | None, side: str | None = None) -> dict | None:
        """Best-effort lookup of an order we may already have placed, for AMBIGUITY RECOVERY only.

        *** DO NOT use this as a pre-submit duplicate check. *** That is exactly the bug this
        replaces: the old `_find_by_key` sent GET /orders?idempotencyKey=... , but that parameter
        DOES NOT EXIST (the documented filters are status, exchangeId, marketId, limit, cursor,
        tournamentId) and SIG silently ignores unknown query params. So it fetched the whole order
        list and returned rows[0] — the same order for every key — making every order after the
        first a false "duplicate" that was never submitted.

        There is no server-side way to look an order up by idempotency key: the Order object carries
        no such field. So this matches on the attributes that DO come back, filtered server-side to
        the one exchange, and it is a HEURISTIC: two genuinely identical orders on one exchange are
        indistinguishable here. It is therefore only safe where the alternative is worse — after a
        502/transport ambiguity, where the question is "did my order land?" and a wrong answer in
        the cautious direction (assume it landed) merely skips a replacement.
        """
        res = await self._http("GET", "/orders",
                               params={"exchangeId": str(exchange_id), "status": "all", "limit": 200})
        if res.status != 200 or not res.body:
            return None
        try:
            data = json.loads(res.body)
        except json.JSONDecodeError:
            return None
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = [rows] if rows else []
        want_px = None if price is None else round(float(price), 3)
        for r in rows:
            if not isinstance(r, dict):
                continue
            if str(r.get("exchangeId")) != str(exchange_id):
                continue
            if str(r.get("action", "")).lower() != str(action).lower():
                continue
            if side is not None and str(r.get("side", "")).lower() != str(side).lower():
                continue                       # YES@p and NO@p with the same size are different orders
            if int(_as_float(r.get("quantity"))) != int(quantity):
                continue
            if want_px is not None:
                rp = r.get("priceLimit")
                if rp is None or round(_as_float(rp), 3) != want_px:
                    continue
            return r
        return None

    def _note_attr(self, order_id: Any, strategy_id: Any, exchange_id: Any, action: Any = None) -> None:
        """Buffer a (venue order id -> strategy) mapping for fill attribution. Pure in-memory append on the
        hot order path; NEVER raises (attribution must never be able to fail a placement)."""
        try:
            if order_id:
                self._attr_buf.append((str(order_id), str(strategy_id or "unknown"), str(exchange_id or ""),
                                       (str(action).lower() if action in ("buy", "sell") else None)))
                known = getattr(self, "_known_ids", None)
                if known is None:
                    known = self._known_ids = deque(maxlen=50000)
                    self._known_set = set()
                if len(known) == known.maxlen:
                    self._known_set.discard(known[0])
                known.append(str(order_id))
                self._known_set.add(str(order_id))
        except Exception:  # noqa: BLE001
            pass

    # ── MM multi-leg leg-id recovery ─────────────────────────────────────────────────────────────────
    # SIG's POST /orders/multi-leg response carries NO per-leg venue order id, so MM's batched quotes (the
    # bulk of MM fills) were UNATTRIBUTED and MM's P&L was unmeasurable. Fix: remember each leg's signature at
    # placement; the attribution flush loop then does ONE GET /orders (open orders, newest first) and claims
    # the matching open order for the strategy. Legs that filled before they were ever seen open stay
    # unattributed (rare for MM, which rests at/inside the touch). Cost: one read per flush interval.
    _LEG_SIG_TTL_S = 600.0

    def note_pending_legs(self, strategy_id: str, legs: list[dict]) -> None:
        """Remember placed legs [{exchange_id, outcome, side(direction), qty, price}] for id recovery."""
        try:
            buf = getattr(self, "_leg_sigs", None)
            if buf is None:
                buf = self._leg_sigs = []
            now = time.time()
            for l in legs or []:
                buf.append({"strategy": str(strategy_id),
                            "ex": str(l.get("contract_id", l.get("exchange_id", ""))),
                            "side": _sig_outcome(l.get("outcome")),
                            "action": _sig_direction(l.get("side")),
                            "qty": int(round(float(l.get("qty", l.get("quantity", 0)) or 0))),
                            "px": round(float(l.get("price") or 0.0), 3), "ts": now})
        except Exception:  # noqa: BLE001 — bookkeeping must never fail a placement
            pass

    async def resolve_pending_legs(self) -> int:
        """Match remembered leg signatures to open venue orders and attribute them. Returns # claimed.
        No-op (no read) when nothing is pending."""
        buf = getattr(self, "_leg_sigs", None) or []
        now = time.time()
        buf = [s for s in buf if now - s["ts"] <= self._LEG_SIG_TTL_S]
        self._leg_sigs = buf
        if not buf:
            return 0
        try:
            resp = await asyncio.to_thread(lambda: self._read().get("/orders", params={"limit": 100}))
        except Exception as exc:  # noqa: BLE001
            log.debug("leg_id_recovery_read_failed", error=type(exc).__name__)
            return 0
        known = getattr(self, "_known_set", set())
        from datetime import datetime as _dt
        claimed = 0
        rows = [r for r in _as_rows(resp) if isinstance(r, dict) and r.get("open", True)]
        for r in rows:
            oid = r.get("id", r.get("orderId"))
            if oid is None or str(oid) in known:
                continue
            try:
                created = _dt.fromisoformat(str(r.get("createdAt", "")).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                created = None
            for i, s in enumerate(buf):
                if (str(r.get("exchangeId")) == s["ex"] and str(r.get("side", "")).lower() == s["side"]
                        and str(r.get("action", "")).lower() == s["action"]
                        and int(_f(r.get("quantity"))) == s["qty"]
                        and round(_f(r.get("priceLimit")), 3) == s["px"]
                        and (created is None or created >= s["ts"] - 10.0)):
                    self._note_attr(oid, s["strategy"], s["ex"])
                    known = getattr(self, "_known_set", known)
                    buf.pop(i)
                    claimed += 1
                    break
        self._leg_sigs = buf
        if claimed:
            log.info("leg_ids_recovered", claimed=claimed, pending=len(buf))
        return claimed

    def drain_attribution(self) -> list[tuple[str, str, str]]:
        """Atomically take + clear the buffered (order_id, strategy, exchange_id) tuples for batch persist."""
        buf = self._attr_buf
        self._attr_buf = []
        return buf

    def _result_from_body(self, idem_key: str, res: HttpResult, *, was_noop=False, reconciled=False) -> OrderResult:
        body = {}
        try:
            body = json.loads(res.body) if res.body else {}
        except json.JSONDecodeError:
            body = {}
        d = body.get("data", body) if isinstance(body, dict) else {}
        if isinstance(d, list):
            d = d[0] if d else {}
        # `orderId` is the placement-response field; `id` is the Order-object field. Accept both.
        oid = d.get("orderId") if d.get("orderId") is not None else d.get("id")
        return OrderResult(idempotency_key=idem_key, status=status_from_order(d),
                           order_id=(str(oid) if oid is not None else None),
                           filled_qty=filled_qty_from_order(d),
                           avg_price=avg_price_from_order(d),
                           was_noop=was_noop, reconciled=reconciled, legs=d.get("legs") or [], raw=body)

    # ---- the public BrokerAPI surface ----
    async def place_order(self, *, strategy_id: str, market_id: str, exchange_id: str, side: str,
                          action: str, quantity: int, price: float | None = None,
                          priority: bool = False, outcome: str | None = None,
                          expiration_s: float | None = None, send_deadline_s: float | None = None) -> OrderResult:
        """expiration_s: venue-side expiry (OrderInput.expirationDate, limit orders only) — the venue cancels the
        order itself if we are not around to. send_deadline_s: refuse to SEND if the write waited longer than this
        in the gate queue (the order was priced off a book that is now stale)."""
        _deadline = (time.monotonic() + float(send_deadline_s)) if send_deadline_s else None
        # SIG OrderInput: side=OUTCOME (yes/no), action=DIRECTION (buy/sell). The pipeline carries the
        # direction as `side` and the outcome in `outcome` (from the strategy). Map both to SIG here.
        denied = _ownership_denies(strategy_id, [exchange_id])
        if denied:
            return OrderResult(idempotency_key="", status="rejected", raw={"reason": denied})
        sig_side = _sig_outcome(outcome)
        sig_action = _sig_direction(side)
        ih = intent_hash(exchange_id=exchange_id, action=sig_action, quantity=quantity, price=price)
        idem = idempotency_key(strategy_id, market_id, sig_side, ih)
        log.info("place_order_called", sig_side=sig_side, sig_action=sig_action,
                 exchange=str(exchange_id), price=price, qty=int(quantity))
        # NO PRE-FLIGHT DEDUP GET. *** SIG DEDUPES SERVER-SIDE ON THE POSTED KEY. ***
        # Spec, SingleOrderInput.idempotencyKey (REQUIRED): "Reuse this key only when retrying the
        # same logical request. An identical resolved payload replays the exact stored response;
        # reuse with a different resolved payload returns 409." So submitting is already idempotent
        # and a client-side pre-check buys nothing.
        #
        # The pre-check it replaces was not merely redundant, it was fatal: it queried
        # GET /orders?idempotencyKey=... , a parameter that does not exist. SIG ignored it, returned
        # the whole order list, and the code took rows[0] — so once ANY order existed, every later
        # order matched it, returned was_noop=True, and was never submitted. The bot placed exactly
        # one order and then silently stopped trading while still reporting success.
        body: dict[str, Any] = {"exchangeId": str(exchange_id), "side": sig_side,
                                "action": sig_action, "quantity": int(quantity), "idempotencyKey": idem}
        if price is not None:
            body["price"] = round(float(price), 3)     # 0.005 tick; SIG validates [0.005, 0.995]
            if expiration_s:
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                body["expirationDate"] = (_dt.now(_tz.utc) + _td(seconds=float(expiration_s))).strftime(
                    "%Y-%m-%dT%H:%M:%S.") + "000Z"
        if self._tournament_id:
            body["tournamentId"] = self._tournament_id

        try:
            act, res = await self._send_write(_guarded(lambda: self._http("POST", "/orders", body), strategy_id,
                                                       [exchange_id], _deadline), priority=priority,
                                              label="place_order")
        except _OwnedAtSend as exc:
            return OrderResult(idempotency_key=idem, status="rejected", raw={"reason": str(exc), "at": "send"})
        log.info("place_order_result", action=str(act), http_status=getattr(res, "status", None),
                 req_side=body.get("side"), req_action=body.get("action"), req_exchange=body.get("exchangeId"),
                 req_price=body.get("price"), req_qty=body.get("quantity"),
                 resp_body=str(getattr(res, "body", ""))[:300])
        if act == RetryAction.OK:
            out = self._result_from_body(idem, res)
            self._note_attr(out.order_id, strategy_id, exchange_id, sig_action)
            _ownership_note(strategy_id, out.order_id, exchange_id)
            return out
        if act == RetryAction.RECONCILE:
            # Ambiguous (502 ORDER_STATUS_UNKNOWN / transport). The spec's own resolution: "retry the
            # identical request with the same idempotency key; that retry is safe and will not
            # double-place." A replayed key returns the STORED response, so this resolves the
            # ambiguity without any risk of a second order — strictly better than the old
            # GET-by-key, which could not work at all.
            try:
                act2, res2 = await self._send_write(
                    _guarded(lambda: self._http("POST", "/orders", body), strategy_id, [exchange_id]),
                    priority=True, label="place_order_replay")
            except _OwnedAtSend as exc:
                act2, res2 = None, None
                _refused = str(exc)
            else:
                _refused = None
            if act2 == RetryAction.OK:
                out = self._result_from_body(idem, res2, reconciled=True)
                self._note_attr(out.order_id, strategy_id, exchange_id)
                _ownership_note(strategy_id, out.order_id, exchange_id)
                log.info("place_order_ambiguity_resolved_by_key_replay",
                         exchange=str(exchange_id), order_id=out.order_id, status=out.status)
                return out
            # Replay did not resolve it either: fall back to attribute matching, which is a
            # heuristic but better than reporting "unknown" and letting a caller re-place.
            found = await self._find_matching_order(exchange_id=str(exchange_id), action=sig_action,
                                                    quantity=int(quantity), price=body.get("price"),
                                                    side=sig_side)
            if found is not None:
                log.warning("place_order_ambiguity_resolved_by_attribute_match",
                            exchange=str(exchange_id), order_id=found.get("id"))
                self._note_attr(found.get("id"), strategy_id, exchange_id)
                _ownership_note(strategy_id, found.get("id"), exchange_id)
                return OrderResult(idempotency_key=idem, status=status_from_order(found),
                                   order_id=(str(found.get("id")) if found.get("id") is not None else None),
                                   filled_qty=filled_qty_from_order(found), reconciled=True, raw=found)
            return OrderResult(idempotency_key=idem, status="unknown", ambiguous=True,
                               raw={"reason": _refused or "ambiguous_unresolved", "last_status": res.status})
        # NO_RETRY (business 4xx) / breaker-tripped / 429-exhausted → rejected (never crash, never mislabel)
        reason = ("rate_limit_exhausted_or_breaker" if act == RetryAction.RETRY_AFTER else "no_retry_4xx")
        return OrderResult(idempotency_key=idem, status="rejected",
                           raw={"reason": reason, "last_status": res.status, "body": (res.body or "")[:160]})

    async def place_multi_leg(self, *, strategy_id: str, market_id: str, legs: list[dict],
                              priority: bool = True) -> OrderResult:
        """Atomic multi-leg (≤10 legs). SIG fills ALL legs or NONE; this never reports a partial. One
        idempotencyKey covers the whole batch (dedupes the batch; a retry can't double-place it)."""
        if not legs or len(legs) > 10:
            return OrderResult(idempotency_key="", status="rejected",
                               raw={"reason": "multi_leg_requires_1..10_legs", "n": len(legs)})
        denied = _ownership_denies(strategy_id, [l.get("exchange_id", l.get("exchangeId")) for l in legs])
        if denied:
            return OrderResult(idempotency_key="", status="rejected", raw={"reason": denied})
        ih = intent_hash(exchange_id="MULTI", action="multi",
                         quantity=sum(int(l.get("quantity", 0)) for l in legs),
                         price=None) + hashlib.sha256(json.dumps(legs, sort_keys=True, default=str).encode()).hexdigest()[:8]
        idem = idempotency_key(strategy_id, market_id, "multi", ih)
        # No pre-flight dedup GET here either — same reason as place_order: POST /orders/multi-leg
        # also requires a client-supplied idempotencyKey and replays the stored response on an
        # identical retry. The pre-check was the bug, not the protection.
        # Each leg is a SIG OrderInput: side=OUTCOME (yes/no), action=DIRECTION (buy/sell). The OMS
        # passes exchange_id (snake) + the direction as `side` + the outcome as `outcome`.
        norm_legs = [{"exchangeId": str(l.get("exchange_id", l.get("exchangeId"))),
                      "side": _sig_outcome(l.get("outcome")),
                      "action": _sig_direction(l.get("side", l.get("action"))),
                      "quantity": int(l["quantity"]),
                      **({"price": round(float(l["price"]), 3)} if l.get("price") is not None else {}),
                      **({"tournamentId": self._tournament_id} if self._tournament_id else {})}
                     for l in legs]
        body = {"legs": norm_legs, "idempotencyKey": idem}
        _ml_ex = [l.get("exchange_id", l.get("exchangeId")) for l in legs]
        try:
            act, res = await self._send_write(_guarded(lambda: self._http("POST", "/orders/multi-leg", body),
                                                       strategy_id, _ml_ex), priority=priority, label="place_multi_leg")
        except _OwnedAtSend as exc:
            return OrderResult(idempotency_key=idem, status="rejected", raw={"reason": str(exc), "at": "send"})
        if act == RetryAction.OK:
            out = self._result_from_body(idem, res)
            # atomic guarantee: if the server did not fully accept, report ABORTED (no naked legs)
            if out.status in ("rejected", "canceled") or (out.raw.get("atomic") is False):
                return OrderResult(idempotency_key=idem, status="rejected",
                                   raw={"reason": "atomic_abort", **(out.raw or {})})
            # attribute each leg's venue order id (a basket's fills reference per-leg ids, not the top id)
            for lg in (out.legs or []):
                if isinstance(lg, dict):
                    self._note_attr(lg.get("id") or lg.get("orderId"), strategy_id,
                                    lg.get("exchangeId") or lg.get("exchange_id"))
            self._note_attr(out.order_id, strategy_id, market_id)
            return out
        if act == RetryAction.RECONCILE:
            # Same key-replay resolution as the single-order path. Attribute matching is NOT used
            # here: a multi-leg basket spans exchanges, so there is no single-exchange query that
            # identifies it, and guessing a partial match could make us believe a hedge landed when
            # only part of it did. Unresolved stays unresolved — never assume legs landed.
            try:
                act2, res2 = await self._send_write(
                    _guarded(lambda: self._http("POST", "/orders/multi-leg", body), strategy_id, _ml_ex),
                    priority=True, label="place_multi_leg_replay")
            except _OwnedAtSend as exc:
                return OrderResult(idempotency_key=idem, status="unknown", ambiguous=True,
                                   raw={"reason": str(exc), "at": "replay"})
            if act2 == RetryAction.OK:
                out = self._result_from_body(idem, res2, reconciled=True)
                for lg in (out.legs or []):
                    if isinstance(lg, dict):
                        self._note_attr(lg.get("id") or lg.get("orderId"), strategy_id,
                                        lg.get("exchangeId") or lg.get("exchange_id"))
                self._note_attr(out.order_id, strategy_id, market_id)
                log.info("place_multi_leg_ambiguity_resolved_by_key_replay", order_id=out.order_id,
                         status=out.status, n_legs=len(norm_legs))
                return out
            return OrderResult(idempotency_key=idem, status="unknown", ambiguous=True,
                               raw={"reason": "atomic_ambiguous_unresolved"})
        return OrderResult(idempotency_key=idem, status="rejected", raw={"reason": "atomic_abort",
                           "last_status": res.status, "body": (res.body or "")[:160]})

    async def cancel_order(self, order_id: str, *, strategy_id: str | None = None) -> OrderResult:
        """Cancel one order. Orders placed by an OWNING strategy (sig/engine/ownership.py) can only be cancelled
        by that strategy (pass strategy_id) — MM requote/dedupe, OMS timeouts and chase can never touch them."""
        try:
            from sig.engine.ownership import registry as _own
            if not _own().allows_cancel(strategy_id, order_id):
                log.warning("cancel_refused_owned_order", order_id=str(order_id), caller=str(strategy_id),
                            owner=_own().order_owner(order_id))
                return OrderResult(idempotency_key="", order_id=str(order_id), status="rejected",
                                   raw={"reason": "order_owned_by:" + str(_own().order_owner(order_id))})
        except Exception:  # noqa: BLE001 — registry trouble must never block ordinary cancels
            pass
        act, res = await self._send_write(lambda: self._http("DELETE", f"/orders/{order_id}"),
                                          priority=True, label="cancel_order",   # cancels are urgent
                                          retry_429=False)     # never back off in-call: callers retry later
        ok = act == RetryAction.OK
        return OrderResult(idempotency_key="", order_id=str(order_id),
                           status="canceled" if ok else norm_status("failed"),
                           raw={"http": res.status})

    async def cancel_all(self, *, force: bool = False) -> OrderResult:
        """Venue-wide cancel of EVERY open order (all strategies). Refused while an isolated strategy has registered
        orders (it would strip their quotes without their knowledge) unless force=True (operator emergency)."""
        if not force:
            try:
                from sig.engine.ownership import registry as _own
                if any(True for _ in _own()._orders):
                    log.warning("cancel_all_refused_isolated_orders_present")
                    return OrderResult(idempotency_key="", status="rejected",
                                       raw={"reason": "isolated_strategy_orders_present"})
            except Exception:  # noqa: BLE001
                pass
        act, res = await self._send_write(lambda: self._http("POST", "/orders/cancel-all"),
                                          priority=True, label="cancel_all")
        return OrderResult(idempotency_key="", status="canceled" if act == RetryAction.OK else "failed",
                           raw={"http": res.status})

    async def get_order(self, order_id: str) -> OrderResult:
        # reads are not write-gated (separate 100/min read budget); simple GET
        res = await self._http("GET", f"/orders/{order_id}")
        if res.status != 200:
            return OrderResult(idempotency_key="", order_id=str(order_id), status="unknown",
                               ambiguous=(res.status in (0, 502, 503)), raw={"http": res.status})
        return self._result_from_body("", res)

    # ---- venue-truth READS for reconciliation (sig/oms/reconciliation.py) -------------------------- #
    # Sourced from the SIG READ client (reusing sig_client's auth/base-url/UA — not rewritten). The read
    # client is sync (httpx.Client), so each call runs in a worker thread via asyncio.to_thread — it never
    # blocks the event loop and never contends the 30/min WRITE gate (reads have their own 100/min budget).
    def _read(self) -> Any:
        if self._read_client is None:
            from sig.sig_client import SigClient
            # generous read timeout — SIG reads are slow; a tight timeout crash-loops the trader
            self._read_client = SigClient(base_url=self._base, timeout=self._read_timeout)
        return self._read_client

    def _read_positions_raw(self, fresh: bool = False):
        """Call the read client's positions endpoint, scoped when we have a slug.

        The slug is only PASSED when set, so a read client whose portfolio_positions() predates the
        scoping parameter keeps working unchanged (several test doubles and scripts construct one).
        Requiring the new signature unconditionally would break them for no behavioural gain.
        """
        # Defensive on the cache attrs: several test doubles build the broker via __new__ (no __init__),
        # so a missing attr means "no cache" (ttl 0 ⇒ always a fresh read), not an AttributeError.
        now = time.monotonic()
        cache = getattr(self, "_pos_raw_cache", None)
        ttl = getattr(self, "_pos_raw_ttl_s", 0.0)
        if not fresh and cache is not None and ttl > 0 and (now - getattr(self, "_pos_raw_ts", 0.0)) < ttl:
            return cache
        read = self._read()
        try:
            resp = (read.portfolio_positions(self._tournament_slug) if self._tournament_slug
                    else read.portfolio_positions())
        except Exception as exc:  # noqa: BLE001
            # 429 / transient: serve the last good read if it's recent rather than failing the caller —
            # an uncaught 429 here failed ~20 trading cycles/hour (fill_handler's broker-authority refresh).
            if not fresh and cache is not None and (now - getattr(self, "_pos_raw_ts", 0.0)) < self._POS_STALE_OK_S:
                log.warning("positions_read_failed_using_cache", error=type(exc).__name__,
                            age_s=round(now - getattr(self, "_pos_raw_ts", 0.0), 1))
                return cache
            raise
        self._pos_raw_cache = resp
        self._pos_raw_ts = now
        return resp

    _POS_STALE_OK_S = 180.0

    async def get_positions(self, *, fresh: bool = False) -> list[BrokerPositionSnapshot]:
        """Current venue positions, TOURNAMENT-SCOPED. Shape = BrokerPosition protocol.

        Scoped to mirror what orders already do (they carry tournamentId). Per the spec the
        UNSCOPED /portfolio/positions "resolves to its organization's default tournament" for an
        org-bound key and reads GLOBAL positions for an unbound one -- so which book it reports
        depends on how the key happens to be bound, while our orders always go to this tournament.
        Both currently return identical rows here, so this is a correctness/robustness change
        rather than the fix for anything presently broken; the scoped endpoint also values at the
        tournament's valuation_price.
        """
        # fresh=True bypasses the shared TTL cache and raises on a failed read (no stale fallback) — for callers
        # that pair it with an open-orders read and must not miss a fill between the two.
        resp = await asyncio.to_thread(self._read_positions_raw, fresh)
        return [_position_from_row(r) for r in _as_rows(resp)]

    async def get_positions_valued(self) -> list[dict]:
        """Current venue positions WITH the venue's own mark + PnL (GET /portfolio/positions, RAW rows).

        Unlike get_positions (lossy BrokerPositionSnapshot: contract_id/side/qty/avg only), this keeps
        currentPrice / unrealizedPnlPct / option — everything a take-profit exit needs to value and
        close a held position WITHOUT depending on the local price stream (which only covers the
        WS-subscribed market subset). Returns a list of plain dicts; defensive across field names."""
        resp = await asyncio.to_thread(self._read_positions_raw)
        out: list[dict] = []
        for r in _as_rows(resp):
            if not isinstance(r, dict):
                continue
            ex = r.get("exchangeId", r.get("exchange_id", r.get("id")))
            if ex is None:
                continue
            out.append({
                "exchange_id": str(ex),
                "quantity": _f(r.get("quantity", r.get("qty", 0.0))),
                "avg_cost": _f(_first(r, "avgCost", "avg_cost", "avgPrice", "avg_entry_price", default=0.0)),
                "current_price": _f(_first(r, "currentPrice", "current_price", "mark", "price", default=0.0)),
                "pnl_pct": _f(_first(r, "unrealizedPnlPct", "unrealized_pnl_pct", "pnlPct", default=0.0)),
                "option": str(_first(r, "option", "side", "outcome", default="yes")).lower(),
                "settled": bool(r.get("settled", False)),
            })
        return out

    async def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        """Current OPEN orders (GET /orders?status=open). Shape = BrokerOrderSnapshot protocol. Any
        terminal rows the server still returns are filtered out so reconciliation sees only live orders."""
        resp = await asyncio.to_thread(lambda: self._read().get("/orders", params={"status": "open"}))
        snaps = [_order_from_row(r) for r in _as_rows(resp)]
        return [o for o in snaps if o.status not in _TERMINAL]

    async def get_open_orders_valued(self) -> list[dict]:
        """Current OPEN orders as RAW dicts WITH the resting limit price (priceLimit) + order id — what
        cancel-replace needs to tell a drifted quote from a current one. BrokerOrderSnapshot drops the
        price, so this returns plain dicts straight from GET /orders?status=open. Defensive on names."""
        resp = await asyncio.to_thread(lambda: self._read().get("/orders", params={"status": "open"}))
        out: list[dict] = []
        for r in _as_rows(resp):
            if not isinstance(r, dict) or not r.get("open", True):
                continue
            oid = r.get("id", r.get("orderId", r.get("order_id")))
            ex = r.get("exchangeId", r.get("exchange_id"))
            if oid is None or ex is None:
                continue
            out.append({
                "order_id": str(oid),
                "exchange_id": str(ex),
                "side": str(r.get("side", "")).lower(),            # yes/no outcome
                "action": str(r.get("action", "")).lower(),        # buy/sell direction
                "price": _f(_first(r, "priceLimit", "price_limit", "price", default=0.0)),
                "quantity": _f(r.get("quantity", r.get("qty", 0.0))),
            })
        return out

    async def get_open_orders_all(self, max_pages: int = 25) -> list[dict] | None:
        """EVERY open order (cursor-paginated GET /orders, 200/page), same dict shape as
        get_open_orders_valued. None if any page fails — a partial list must never be mistaken for the
        full book (MM de-dupes against this; a short read would re-open stacking)."""
        out: list[dict] = []
        cursor = None
        for _ in range(max(1, int(max_pages))):
            params = {"limit": 200, "status": "open", **({"cursor": cursor} if cursor else {})}
            try:
                resp = await asyncio.to_thread(lambda: self._read().get("/orders", params=params))
            except Exception:  # noqa: BLE001
                return None
            for r in _as_rows(resp):
                if not isinstance(r, dict) or not r.get("open", True):
                    continue
                oid = r.get("id", r.get("orderId"))
                ex = r.get("exchangeId", r.get("exchange_id"))
                if oid is None or ex is None:
                    continue
                out.append({"order_id": str(oid), "exchange_id": str(ex),
                            "side": str(r.get("side", "")).lower(),
                            "action": str(r.get("action", "")).lower(),
                            "price": _f(_first(r, "priceLimit", "price_limit", "price", default=0.0)),
                            "quantity": _f(r.get("quantity", r.get("qty", 0.0)))})
            pag = (resp or {}).get("pagination", {}) if isinstance(resp, dict) else {}
            cursor = pag.get("nextCursor")
            if not pag.get("hasMore") or not cursor:
                return out
        return None                                   # too many pages to trust as complete

    async def get_account(self) -> BrokerAccountSnapshot:
        """Account snapshot (GET /account) for the capital ledger and the exposure/deploy caps.

        buying_power = FREE CASH — what a NEW order can actually spend. equity = NAV = free cash + the
        mark-to-market value of open positions — the real capital at risk the 0.85 deploy cap and the
        aggregate-exposure cap must size off. The venue's /account returns only `balance` (free cash),
        with no equity field, so the old `equity ← balance` fallback computed every cap off HALF the
        real equity once ~half the book was deployed, falsely tripping no_room_to_add_exposure. We now
        prefer a TRUE venue equity field when present, else compute NAV = cash + marked positions (the
        positions leg TTL-cached so this per-cycle call stays cheap). buying_power stays free cash —
        capital tied up in positions cannot fund a new order, so new-order affordability is cash-bound."""
        d = await asyncio.to_thread(self._read().account)
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            d = d["data"]
        d = d if isinstance(d, dict) else {}
        cash = _f(_first(d, "buyingPower", "availableBalance", "available", "cash", "balance", default=0.0))
        # Prefer a TRUE venue NAV field. "balance" is deliberately NOT in this list — it is free cash,
        # and the equity←balance mapping is exactly the bug this fixes.
        venue_equity = _first(d, "equity", "portfolioValue", "totalValue", "netLiquidation")
        if venue_equity is not None:
            equity = _f(venue_equity)
        else:
            now = time.monotonic()
            if now - self._nav_mv_ts >= self._nav_mv_ttl_s:
                try:
                    resp = await asyncio.to_thread(self._read_positions_raw)
                    self._nav_mv_cache = _positions_market_value(resp)
                    self._nav_mv_ts = now
                except Exception:  # noqa: BLE001 — keep last-known MV; NAV must never crash the cycle
                    pass
            equity = cash + float(self._nav_mv_cache)
        self._last_equity = equity
        return BrokerAccountSnapshot(buying_power=cash, equity=equity)

    def last_equity(self) -> float | None:
        """NAV (free cash + marked positions) as of the most recent get_account(); None before the
        first call. The live risk-gate adapter reads this to refresh its frozen build-time balance to
        venue-truth equity each cycle, with no extra API round-trip."""
        return self._last_equity
