"""StateStore — the async shared-state layer both loops and the feed coordinate through.

CONCURRENCY MODEL (integration spec §5)
One process; a fast arb loop, a slow strategy loop, the realtime feed and the supervisor all touching
one local SQLite file. Three mechanisms, each doing a distinct job:

  1. WAL (schema.py) lets readers run while the writer writes. Without it the feed's continuous price
     writes would block every loop read, which is the whole reason the spec specifies WAL.
  2. EVERY disk call runs via `asyncio.to_thread`, so a slow fsync parks a pool thread and never the
     event loop. sqlite3 is a blocking C extension; calling it directly from a coroutine stalls both
     loops and the feed at once. (aiosqlite would do the same thing with a dependency; requirements.txt
     has none, so to_thread it is.)
  3. ONE asyncio.Lock serialises writes -> the spec's single-writer discipline. WAL already permits
     only one writer at a time; the lock means contenders queue in the event loop instead of racing
     into SQLITE_BUSY and burning the 30s busy_timeout. Reads deliberately do NOT take it.

Connections are thread-local: `to_thread` uses an arbitrary pool thread and a sqlite3 connection must
not be used from two threads at once. Each thread lazily opens its own WAL connection, so "shared
state" means a shared FILE, never a shared connection object.

VERIFY-BY-REREAD (§5)
A long-running loop's in-memory view drifts; disk is truth. Two layers:
  * `read_*` always hits disk — there is no hidden cache to go stale.
  * `reread()` is the explicit cycle-top refresh. It rolls back any open read snapshot (in WAL a
     connection inside a transaction keeps the snapshot it started with) and repopulates the
     `snapshot` the loop may read from within one cycle. A loop that holds a cached snapshot and
     skips `reread()` is reading its own history, which is exactly the drift the discipline exists to
     catch.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .schema import STATE_DB_PATH, connect, init_state_db

log = structlog.get_logger()


def _now() -> str:
    """ISO8601 UTC — matches the house convention in infra/persistence/db.py."""
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_seconds(updated_at: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds since `updated_at`. None when unparseable/absent — the caller must decide, because
    'unknown age' is NOT 'fresh'."""
    ts = _parse_ts(updated_at)
    if ts is None:
        return None
    return ((now or datetime.now(timezone.utc)) - ts).total_seconds()


def unrealized_pnl_for(
    *,
    quantity: float,
    avg_price: float,
    best_bid: float | None,
    best_ask: float | None,
) -> float | None:
    """Mark one position to the side of the book it would actually have to TRADE AGAINST to exit.

    VALUATION CONVENTION (this is the correctness-relevant decision, so it is spelled out)
      * A long (quantity > 0) is closed by SELLING, and a seller is filled at the BID. Its mark is
        therefore `quantity * best_bid` and `unrealized_pnl = quantity * (best_bid - avg_price)`.
      * A short (quantity < 0) is closed by BUYING BACK, and a buyer is filled at the ASK, so
        `unrealized_pnl = quantity * (best_ask - avg_price)`, i.e. `abs(qty) * (avg_price - ask)`.
      * A flat position (quantity == 0) has nothing at risk, so its unrealized pnl is exactly 0.0.

    Using the mid — let alone the opposite side — would book half the spread (or all of it) as
    profit the book cannot realise. On this venue the spread on a thin contract is a large fraction
    of its price, so a mid mark on a basket of four legs can manufacture several points of phantom
    edge, which is precisely the number an exit rule would act on. Marking to the exit side is the
    conservative choice: it can only understate the position, never flatter it.

    Returns None when the side of the book this position needs is absent. None means "UNKNOWN", and
    unknown is NOT zero: a caller must leave the previous mark in place rather than write 0.0, which
    would read as a total loss and could trigger an exit on a gap in the price feed.
    """
    qty = float(quantity or 0.0)
    if qty == 0.0:
        return 0.0
    exit_price = best_bid if qty > 0 else best_ask
    if exit_price is None:
        return None
    return round(qty * (float(exit_price) - float(avg_price or 0.0)), 6)


def _normalise_legs(legs: list[dict] | None) -> list[dict]:
    """Canonicalise leg dicts before they are frozen into JSON.

    Two jobs. First, `exchange_id` is mandatory and is what joins a leg back to `positions`/`prices`,
    so `contract_id` (the name `sig/oms/models.py::OrderSlice` uses for the same identifier) is
    accepted and renamed here rather than leaving every reader to guess which key a given basket
    used. Second, numbers are coerced at the boundary: JSON round-trips a Decimal or a numpy float
    into something a later comparison would choke on, and the failure would surface at exit time
    rather than at entry.
    """
    out: list[dict] = []
    for leg in legs or []:
        leg = dict(leg)
        exchange_id = leg.pop("exchange_id", None)
        if exchange_id is None:
            exchange_id = leg.get("contract_id")
        if exchange_id is None:
            raise ValueError(f"basket leg needs an exchange_id (or contract_id): {leg!r}")
        norm: dict[str, Any] = {
            "exchange_id": str(exchange_id),
            "side": str(leg.pop("side", "") or ""),
            "quantity": float(leg.pop("quantity", 0.0) or 0.0),
            "entry_price": float(leg.pop("entry_price", 0.0) or 0.0),
        }
        norm.update(leg)                        # keep whatever else the caller attached
        out.append(norm)
    return out


def _basket_row_to_dict(row: sqlite3.Row) -> dict:
    """Row -> dict with `legs_json` parsed back into a `legs` list.

    A basket whose JSON is somehow unreadable yields `legs=[]` and keeps `legs_json`, so a caller
    sees an empty leg list it can reject rather than an exception mid-cycle — and still has the raw
    text to diagnose. It is NOT silently dropped: `entry_cost` and `settlement_value` survive, so
    the basket remains visible as open capital.
    """
    d = dict(row)
    try:
        legs = json.loads(d.get("legs_json") or "[]")
    except (TypeError, ValueError):
        legs = []
    d["legs"] = legs if isinstance(legs, list) else []
    return d


@dataclass(frozen=True, slots=True)
class OrderWriteResult:
    """Whether the single order path actually created an order.

    `inserted=False` means the idempotency key was already present, i.e. the dedup fired. The caller
    MUST treat that as "someone already placed this", never retry it as a new order.
    """

    idempotency_key: str
    inserted: bool
    status: str


@dataclass(frozen=True, slots=True)
class Exposure:
    """Exposure against the COMBINED book (§5): caps are global, so the arb loop's fills and the
    directional book's positions are counted together, never per-strategy."""

    gross_notional: float
    net_notional: float
    long_notional: float
    short_notional: float
    n_positions: int
    by_strategy_open_orders: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarkReport:
    """Outcome of one mark-to-market pass — deliberately reports what it REFUSED to value.

    `skipped` is the load-bearing half. An exit rule that only sees `marked` would read a missing
    exchange as "position worth nothing"; seeing it in `skipped` with a reason lets it decline to
    decide instead of liquidating on an absence.
    """

    marked: dict[str, float]            # exchange_id -> unrealized_pnl written
    skipped: dict[str, str]             # exchange_id -> why it was left at its previous value
    marked_at: str


@dataclass(slots=True)
class StateSnapshot:
    """The cycle-top view a loop may read from for the duration of one cycle."""

    taken_at: str
    seq: int
    positions: dict[str, dict]
    prices: dict[str, dict]
    signals: dict[str, dict]
    leaderboard: dict | None
    exposure: Exposure


class StateStore:
    """Async facade over the local WAL SQLite shared state."""

    def __init__(self, db_path: str | Path = STATE_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._write_lock = asyncio.Lock()
        self._reread_seq = 0
        self._snapshot: StateSnapshot | None = None
        self._initialised = False

    # -- lifecycle ---------------------------------------------------------- #
    async def open(self) -> "StateStore":
        if not self._initialised:
            await asyncio.to_thread(init_state_db, self._db_path)
            self._initialised = True
        return self

    async def close(self) -> None:
        await asyncio.to_thread(self._close_thread_conn)

    def _close_thread_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    async def __aenter__(self) -> "StateStore":
        return await self.open()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- plumbing ----------------------------------------------------------- #
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._db_path)
            self._local.conn = conn
        return conn

    def _read_sync(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        # Reads run on whichever pool thread to_thread picks; each thread has its own connection. A connection
        # left inside a transaction keeps its OLD WAL snapshot, and reread()'s rollback only clears the thread it
        # happened to run on — so a later read on another thread could serve stale prices (the intermittent
        # test_reread_clears_a_held_read_snapshot failure). Reads are standalone statements and writes always
        # commit before returning, so dropping a held snapshot here never discards anything.
        conn = self._conn()
        if conn.in_transaction:
            conn.rollback()
        return list(conn.execute(sql, params).fetchall())

    def _write_sync(self, statements: list[tuple[str, tuple]]) -> None:
        conn = self._conn()
        with conn:                                  # one transaction, commit or rollback
            for sql, params in statements:
                conn.execute(sql, params)

    async def set_kv(self, key: str, value: Any) -> None:
        """Durable JSON KV (reuses the state_meta table) — used to persist rolling-buffer snapshots across
        restarts. Best-effort JSON; a non-serialisable value is skipped rather than crashing the caller."""
        import json
        try:
            blob = json.dumps(value)
        except (TypeError, ValueError):
            return
        await self._write([(
            "INSERT INTO state_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), blob),
        )])

    async def get_kv(self, key: str) -> Any | None:
        """Read back a set_kv value (parsed JSON), or None if absent/unparseable."""
        import json
        rows = await self._read("SELECT value FROM state_meta WHERE key=?", (str(key),))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["value"])
        except (ValueError, KeyError, TypeError):
            return None

    async def append_mid_history(self, rows: list[tuple]) -> None:
        """Append a batch of mid samples to the time-series (idempotent per key). Each row is
        (exchange_id, mid, ts) or (exchange_id, mid, ts, bid, ask, bid_size, ask_size) — top-of-book optional."""
        def _f(v):
            return None if v is None else float(v)
        stmts = []
        for r in rows or []:
            try:
                ex, mid, ts = r[0], r[1], r[2]
                bid, ask, bsz, asz = (tuple(r[3:7]) + (None,) * 4)[:4]
                stmts.append((
                    "INSERT INTO mid_history(exchange_id, ts, mid, bid, ask, bid_size, ask_size) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(exchange_id, ts) DO NOTHING",
                    (str(ex), str(ts), float(mid), _f(bid), _f(ask), _f(bsz), _f(asz)),
                ))
            except (TypeError, ValueError, IndexError):
                continue
        if stmts:
            await self._write(stmts)

    async def append_trades(self, exchange_id: str, trades: list[dict], ingested_at: str) -> int:
        """Append venue trades {price,size,side,createdAt[,id]} for one market (idempotent). Returns rows offered."""
        stmts = []
        for t in trades or []:
            try:
                ts = t.get("createdAt") or t.get("created_at")
                if not ts:
                    continue
                stmts.append((
                    "INSERT OR IGNORE INTO trade_tape(exchange_id, created_at, side, price, size, trade_id, ingested_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (str(exchange_id), str(ts), str(t.get("side", "")).upper(), float(t["price"]),
                     float(t.get("size", t.get("quantity", 0)) or 0), (str(t["id"]) if t.get("id") is not None else None),
                     str(ingested_at))))
            except (TypeError, ValueError, KeyError):
                continue
        if stmts:
            await self._write(stmts)
        return len(stmts)

    async def append_kalshi_history(self, rows: list[dict]) -> int:
        """Append Kalshi quotes {exchange_id, ts, ticker, bid, ask, mid, last, volume_24h} (idempotent per key)."""
        stmts = []
        for r in rows or []:
            try:
                stmts.append(("INSERT OR IGNORE INTO kalshi_history(exchange_id, ts, ticker, bid, ask, mid, last, volume_24h) "
                              "VALUES(?,?,?,?,?,?,?,?)",
                              (str(r["exchange_id"]), str(r["ts"]), str(r["ticker"]), r.get("bid"), r.get("ask"),
                               r.get("mid"), r.get("last"), r.get("volume_24h"))))
            except (KeyError, TypeError):
                continue
        if stmts:
            await self._write(stmts)
        return len(stmts)

    async def prune_kalshi_history(self, before_ts: str) -> int:
        def _txn() -> int:
            conn = self._conn()
            with conn:
                cur = conn.execute("DELETE FROM kalshi_history WHERE ts < ?", (str(before_ts),))
            return cur.rowcount
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def latest_trade_ts(self) -> dict[str, str]:
        """{exchange_id: newest created_at recorded} — where each market's tape poll resumes after a restart."""
        rows = await self._read("SELECT exchange_id, MAX(created_at) AS ts FROM trade_tape GROUP BY exchange_id")
        return {r["exchange_id"]: r["ts"] for r in rows}

    async def prune_trade_tape(self, before_ts: str) -> int:
        def _txn() -> int:
            conn = self._conn()
            with conn:
                cur = conn.execute("DELETE FROM trade_tape WHERE created_at < ?", (str(before_ts),))
            return cur.rowcount
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def update_order_by_broker_id(self, broker_order_id: str, *, status: str | None = None,
                                        filled_quantity: int | None = None,
                                        fill_price: float | None = None) -> bool:
        """Update an order row keyed by the VENUE order id (broker_order_id). The OMS/fill-handler learn
        the venue order_id (not our idempotency_key), so status/fill updates must key on it — the adapter
        used to pass it where update_order_status expects idempotency_key, so NO update ever landed (order
        statuses stayed stale forever). COALESCE keeps unchanged fields."""
        def _txn() -> bool:
            conn = self._conn()
            with conn:
                cur = conn.execute(
                    "UPDATE orders SET "
                    "status=COALESCE(?,status), "
                    "filled_quantity=COALESCE(?,filled_quantity), "
                    "fill_price=COALESCE(?,fill_price), updated_at=? "
                    "WHERE broker_order_id=?",
                    (status, filled_quantity, fill_price, _now(), str(broker_order_id)),
                )
            return cur.rowcount > 0
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def append_strategy_fills(self, rows: list[dict]) -> int:
        """Append venue fills tagged by strategy to the cumulative ledger (idempotent per venue fill id).
        rows: dicts with fill_id, order_id, strategy, exchange_id, outcome, price, quantity, filled_at,
        mid_at_fill. Returns how many were newly inserted."""
        now = _now()
        stmts = []
        for r in rows or []:
            try:
                stmts.append((
                    "INSERT INTO strategy_fills(fill_id,order_id,strategy,exchange_id,outcome,price,"
                    "quantity,filled_at,mid_at_fill,ingested_at,signed_qty) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(fill_id) DO NOTHING",
                    (str(r["fill_id"]), (str(r["order_id"]) if r.get("order_id") is not None else None),
                     str(r.get("strategy", "UNATTRIBUTED")), str(r["exchange_id"]),
                     (str(r.get("outcome")) if r.get("outcome") is not None else None),
                     float(r["price"]), float(r["quantity"]),
                     (str(r.get("filled_at")) if r.get("filled_at") is not None else None),
                     (float(r["mid_at_fill"]) if r.get("mid_at_fill") is not None else None), now,
                     (float(r["signed_qty"]) if r.get("signed_qty") is not None else None)),
                ))
            except (TypeError, ValueError, KeyError):
                continue
        if not stmts:
            return 0
        def _txn() -> int:
            conn = self._conn()
            n = 0
            with conn:
                for sql, params in stmts:
                    cur = conn.execute(sql, params)
                    n += cur.rowcount
            return n
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def read_mid_history(self, exchange_id: str, limit: int = 60) -> list[float]:
        """Return the most-recent `limit` mids for one market, oldest->newest (so successive diffs are
        chronological). Local read, no venue call — substrate for the MM vol-aware-spread shadow."""
        rows = await self._read(
            "SELECT mid FROM mid_history WHERE exchange_id=? ORDER BY ts DESC LIMIT ?",
            (str(exchange_id), int(max(1, limit))),
        )
        out = []
        for r in reversed(rows):
            try:
                out.append(float(r["mid"]))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    async def retag_unattributed_fills(self) -> None:
        """Fills ingested BEFORE their order's attribution was flushed (attribution flush and fill ingest run on
        different intervals) were frozen as UNATTRIBUTED. Re-tag them from order_attribution once it has them."""
        await self._write([(
            "UPDATE strategy_fills SET strategy = (SELECT a.strategy FROM order_attribution a "
            "WHERE a.broker_order_id = strategy_fills.order_id) "
            "WHERE strategy = 'UNATTRIBUTED' AND order_id IN (SELECT broker_order_id FROM order_attribution)", ())])

    async def resign_fills_from_actions(self, since_iso: str | None = None) -> None:
        """Fills ingested BEFORE their order's direction (order_attribution.action) was flushed got the venue's raw
        sign, which reads every fill as a BUY. 2026-10-16: three fresh-gap SELLS stored as buys -> loss measure
        -$9,781 for a real ~+$100 -> false halt. Re-sign every fill whose sign contradicts its order's recorded
        action (buy YES +q, sell YES -q, buy NO -q, sell NO +q). since_iso limits it to recent ingests."""
        want = ("(CASE WHEN ((SELECT a.action FROM order_attribution a WHERE a.broker_order_id = strategy_fills.order_id)"
                " = 'buy') = (lower(outcome) = 'yes') THEN abs(quantity) ELSE -abs(quantity) END)")
        sql = (f"UPDATE strategy_fills SET signed_qty = {want} "
               "WHERE order_id IN (SELECT broker_order_id FROM order_attribution WHERE action IN ('buy','sell')) "
               f"AND outcome IN ('yes','no','YES','NO') AND signed_qty != {want}")
        params: tuple = ()
        if since_iso:
            sql += " AND ingested_at >= ?"
            params = (since_iso,)
        await self._write([(sql, params)])

    async def latest_fill_time(self) -> str | None:
        rows = await self._read("SELECT MAX(filled_at) AS m FROM strategy_fills")
        return rows[0]["m"] if rows else None

    async def record_order_attribution(self, rows: list[tuple]) -> int:
        """Persist a batch of (broker_order_id, strategy, exchange_id) from the placement chokepoint.
        Idempotent per venue order id (first writer wins — the id's strategy never changes). Returns the
        number newly inserted. This is the authoritative fill→strategy link; see the schema note."""
        now = _now()
        stmts = []
        for r in rows or []:
            try:
                oid, strat, ex = r[0], r[1], r[2]
                action = r[3] if len(r) > 3 else None
                if not oid:
                    continue
                stmts.append((
                    "INSERT INTO order_attribution(broker_order_id,strategy,exchange_id,created_at,action) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(broker_order_id) DO NOTHING",
                    (str(oid), str(strat or "unknown"), (str(ex) if ex else None), now,
                     (str(action) if action in ("buy", "sell") else None)),
                ))
            except (TypeError, ValueError):
                continue
        if not stmts:
            return 0
        def _txn() -> int:
            conn = self._conn()
            n = 0
            with conn:
                for sql, params in stmts:
                    n += conn.execute(sql, params).rowcount
            return n
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def read_attribution_actions(self) -> dict[str, str]:
        """venue_order_id -> 'buy'|'sell' for orders whose direction was captured at placement."""
        try:
            rows = await self._read("SELECT broker_order_id, action FROM order_attribution WHERE action IS NOT NULL")
        except Exception:  # noqa: BLE001 — pre-migration DB: no directions known
            return {}
        return {str(r["broker_order_id"]): str(r["action"]) for r in rows}

    async def read_attribution_map(self) -> dict[str, str]:
        """venue_order_id -> strategy, the authoritative map for fill attribution."""
        rows = await self._read("SELECT broker_order_id, strategy FROM order_attribution")
        out = {}
        for r in rows:
            try:
                out[str(r["broker_order_id"])] = str(r["strategy"])
            except (KeyError, TypeError):
                continue
        return out

    async def append_nav(self, row: dict) -> None:
        await self._write([(
            "INSERT INTO nav_history(ts, cash, market_value, nav, unrealized, positions, lb_pnl, lb_rank, book_edge) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ts) DO NOTHING",
            (str(row["ts"]), row.get("cash"), row.get("market_value"), float(row["nav"]), row.get("unrealized"),
             row.get("positions"), row.get("lb_pnl"), row.get("lb_rank"), row.get("book_edge")))])

    async def append_sharp_history(self, rows: list[dict]) -> int:
        """Append sharp samples {exchange_id, ts, sharp, raw?, sig_mid?, venue?} (idempotent per key)."""
        stmts = []
        for r in rows or []:
            try:
                stmts.append((
                    "INSERT INTO sharp_history(exchange_id, ts, sharp, raw, sig_mid, venue, sig_bid, sig_ask) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(exchange_id, ts) DO NOTHING",
                    (str(r["exchange_id"]), str(r["ts"]), float(r["sharp"]),
                     (float(r["raw"]) if r.get("raw") is not None else None),
                     (float(r["sig_mid"]) if r.get("sig_mid") is not None else None),
                     str(r.get("venue") or "polymarket"),
                     (float(r["sig_bid"]) if r.get("sig_bid") is not None else None),
                     (float(r["sig_ask"]) if r.get("sig_ask") is not None else None)),
                ))
            except (TypeError, ValueError, KeyError):
                continue
        if stmts:
            await self._write(stmts)
        return len(stmts)

    async def prune_sharp_history(self, before_ts: str) -> int:
        def _txn() -> int:
            conn = self._conn()
            with conn:
                cur = conn.execute("DELETE FROM sharp_history WHERE ts < ?", (str(before_ts),))
            return cur.rowcount
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def prune_mid_history(self, before_ts: str) -> int:
        """Delete mid samples older than before_ts (ISO). Returns rows removed."""
        def _txn() -> int:
            conn = self._conn()
            with conn:
                cur = conn.execute("DELETE FROM mid_history WHERE ts < ?", (str(before_ts),))
            return cur.rowcount
        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._read_sync, sql, params)

    async def _write(self, statements: list[tuple[str, tuple]]) -> None:
        async with self._write_lock:                # single-writer discipline
            await asyncio.to_thread(self._write_sync, statements)

    # -- prices (feed is the primary writer) -------------------------------- #
    async def upsert_price(
        self,
        exchange_id: Any,
        *,
        best_bid: float | None,
        best_ask: float | None,
        spread: float | None = None,
        sequence: int | None = None,
        updated_at: str | None = None,
    ) -> None:
        if spread is None and best_bid is not None and best_ask is not None:
            spread = round(float(best_ask) - float(best_bid), 6)
        await self._write(
            [
                (
                    "INSERT INTO prices(exchange_id,best_bid,best_ask,spread,sequence,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(exchange_id) DO UPDATE SET "
                    "best_bid=excluded.best_bid, best_ask=excluded.best_ask, spread=excluded.spread, "
                    "sequence=excluded.sequence, updated_at=excluded.updated_at",
                    (str(exchange_id), best_bid, best_ask, spread, sequence, updated_at or _now()),
                )
            ]
        )

    async def upsert_prices(self, rows: list[dict]) -> int:
        """Batch the feed's NBBO sweep into ONE transaction. /exchanges/prices returns up to ~50 ids
        per call; committing per row would fsync 50 times for one logical update."""
        statements = []
        now = _now()
        for row in rows or []:
            ex = row.get("exchangeId", row.get("exchange_id", row.get("id")))
            if ex is None:
                continue
            bid, ask = row.get("bestBid", row.get("best_bid")), row.get("bestAsk", row.get("best_ask"))
            spread = row.get("spread")
            if spread is None and bid is not None and ask is not None:
                spread = round(float(ask) - float(bid), 6)
            statements.append(
                (
                    "INSERT INTO prices(exchange_id,best_bid,best_ask,spread,sequence,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(exchange_id) DO UPDATE SET "
                    "best_bid=excluded.best_bid, best_ask=excluded.best_ask, spread=excluded.spread, "
                    "sequence=excluded.sequence, updated_at=excluded.updated_at",
                    (
                        str(ex), bid, ask, spread,
                        row.get("sequence"), row.get("updated_at") or now,
                    ),
                )
            )
        if statements:
            await self._write(statements)
        return len(statements)

    async def read_price(self, exchange_id: Any) -> dict | None:
        rows = await self._read("SELECT * FROM prices WHERE exchange_id=?", (str(exchange_id),))
        return dict(rows[0]) if rows else None

    async def read_prices(self) -> dict[str, dict]:
        return {r["exchange_id"]: dict(r) for r in await self._read("SELECT * FROM prices")}

    # -- signals ------------------------------------------------------------ #
    async def upsert_signal(
        self,
        exchange_id: Any,
        *,
        p: float,
        sigma_p: float | None = None,
        divergence: float | None = None,
        market_id: Any | None = None,
        flags: dict | None = None,
        updated_at: str | None = None,
    ) -> None:
        await self._write(
            [
                (
                    "INSERT INTO signals(exchange_id,market_id,p,sigma_p,divergence,flags,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(exchange_id) DO UPDATE SET "
                    "market_id=excluded.market_id, p=excluded.p, sigma_p=excluded.sigma_p, "
                    "divergence=excluded.divergence, flags=excluded.flags, updated_at=excluded.updated_at",
                    (
                        str(exchange_id),
                        None if market_id is None else str(market_id),
                        float(p), sigma_p, divergence,
                        None if flags is None else json.dumps(flags, sort_keys=True),
                        updated_at or _now(),
                    ),
                )
            ]
        )

    async def read_signal(self, exchange_id: Any) -> dict | None:
        rows = await self._read("SELECT * FROM signals WHERE exchange_id=?", (str(exchange_id),))
        return dict(rows[0]) if rows else None

    async def read_signals(self) -> dict[str, dict]:
        return {r["exchange_id"]: dict(r) for r in await self._read("SELECT * FROM signals")}

    # -- staleness ---------------------------------------------------------- #
    async def price_age(self, exchange_id: Any) -> float | None:
        row = await self.read_price(exchange_id)
        return None if row is None else age_seconds(row.get("updated_at"))

    async def signal_age(self, exchange_id: Any) -> float | None:
        row = await self.read_signal(exchange_id)
        return None if row is None else age_seconds(row.get("updated_at"))

    async def is_price_stale(self, exchange_id: Any, max_age_seconds: float) -> bool:
        """MISSING OR UNPARSEABLE COUNTS AS STALE. 'I have no idea how old this is' must never be
        treated as fresh — that is how a dead feed silently keeps a loop trading on old prices."""
        age = await self.price_age(exchange_id)
        return True if age is None else age > max_age_seconds

    async def is_signal_stale(self, exchange_id: Any, max_age_seconds: float) -> bool:
        age = await self.signal_age(exchange_id)
        return True if age is None else age > max_age_seconds

    async def stale_exchanges(self, max_age_seconds: float, *, table: str = "prices") -> list[str]:
        if table not in ("prices", "signals"):
            raise ValueError(f"table must be 'prices' or 'signals', got {table!r}")
        rows = await self._read(f"SELECT exchange_id, updated_at FROM {table}")
        out = []
        for r in rows:
            age = age_seconds(r["updated_at"])
            if age is None or age > max_age_seconds:
                out.append(r["exchange_id"])
        return sorted(out)

    # -- positions ---------------------------------------------------------- #
    async def upsert_position(
        self,
        exchange_id: Any,
        *,
        quantity: float,
        avg_price: float,
        realized_pnl: float | None = None,
        unrealized_pnl: float | None = None,
        updated_at: str | None = None,
    ) -> None:
        """Write the broker-authoritative quantity/price for one exchange.

        `realized_pnl` / `unrealized_pnl` default to None = LEAVE AS IS (0.0 on a brand-new row).
        They used to default to 0.0, which meant every position update — the OMS fill handler writes
        one on each fill and each reconciliation — silently reset both accumulators. That made the
        mark written by `mark_to_market()` survive only until the next fill, and quietly discarded
        realised cash. Position identity (how much, at what price) and the pnl accumulators are
        updated by different events, so they must be independently settable; pass an explicit number
        to actually change one.
        """
        await self._write(
            [
                (
                    "INSERT INTO positions"
                    "(exchange_id,quantity,avg_price,realized_pnl,unrealized_pnl,updated_at) "
                    "VALUES(?,?,?,COALESCE(?,0.0),COALESCE(?,0.0),?) "
                    "ON CONFLICT(exchange_id) DO UPDATE SET "
                    "quantity=excluded.quantity, avg_price=excluded.avg_price, "
                    "realized_pnl=COALESCE(?,positions.realized_pnl), "
                    "unrealized_pnl=COALESCE(?,positions.unrealized_pnl), "
                    "updated_at=excluded.updated_at",
                    (
                        str(exchange_id), float(quantity), float(avg_price),
                        None if realized_pnl is None else float(realized_pnl),
                        None if unrealized_pnl is None else float(unrealized_pnl),
                        updated_at or _now(),
                        None if realized_pnl is None else float(realized_pnl),
                        None if unrealized_pnl is None else float(unrealized_pnl),
                    ),
                )
            ]
        )

    async def read_position(self, exchange_id: Any) -> dict | None:
        rows = await self._read("SELECT * FROM positions WHERE exchange_id=?", (str(exchange_id),))
        return dict(rows[0]) if rows else None

    async def read_positions(self) -> dict[str, dict]:
        return {r["exchange_id"]: dict(r) for r in await self._read("SELECT * FROM positions")}

    # -- mark-to-market ----------------------------------------------------- #
    async def mark_to_market(
        self,
        *,
        max_price_age_seconds: float | None = None,
        now: datetime | None = None,
    ) -> MarkReport:
        """Join `positions` against `prices` and write a REAL `unrealized_pnl` per position.

        Until this existed `positions.unrealized_pnl` was hardcoded 0.0 by every writer and nothing
        ever recomputed it, so the column read as "every open position is exactly flat" — an exit
        rule reading it would have seen no reason to ever close anything.

        Valuation is `unrealized_pnl_for` (see that function for the bid/ask convention and why the
        mid is wrong). This method only does the I/O and the refusals.

        WHAT IT REFUSES TO VALUE, and why a refusal is not a zero:
          * `no_price_row`   — the feed has never reported this exchange.
          * `null_bid` / `null_ask` — the row exists but the side needed to exit is NULL (one-sided
            book, or the feed wrote a partial quote).
          * `stale_price`    — only when `max_price_age_seconds` is given. A quote too old to trade
            on is the same error class as no quote at all; an UNPARSEABLE/absent timestamp counts as
            stale, matching `is_price_stale`'s rule that "unknown age" is never "fresh".
        In every one of those cases the row is left at its PREVIOUS `unrealized_pnl` and named in
        `MarkReport.skipped`. Writing 0.0 instead would be indistinguishable from "this basket lost
        its entire value", which is the one number most likely to trip a stop.

        `realized_pnl` is never touched — it is a cash fact, not a valuation — and neither is
        `updated_at`, which records when the POSITION last changed. Bumping it on a mere revaluation
        would make a stale, untraded position look freshly moved.
        """
        positions, prices = await asyncio.gather(self.read_positions(), self.read_prices())
        marked: dict[str, float] = {}
        skipped: dict[str, str] = {}
        stamp = _now()

        for exchange_id, pos in positions.items():
            qty = float(pos.get("quantity") or 0.0)
            price_row = prices.get(exchange_id)
            if qty != 0.0:
                if price_row is None:
                    skipped[exchange_id] = "no_price_row"
                    continue
                if max_price_age_seconds is not None:
                    age = age_seconds(price_row.get("updated_at"), now=now)
                    if age is None or age > max_price_age_seconds:
                        skipped[exchange_id] = "stale_price"
                        continue
            value = unrealized_pnl_for(
                quantity=qty,
                avg_price=float(pos.get("avg_price") or 0.0),
                best_bid=(price_row or {}).get("best_bid"),
                best_ask=(price_row or {}).get("best_ask"),
            )
            if value is None:
                skipped[exchange_id] = "null_bid" if qty > 0 else "null_ask"
                continue
            marked[exchange_id] = value

        if marked:
            await self._write(
                [
                    ("UPDATE positions SET unrealized_pnl=? WHERE exchange_id=?", (value, ex))
                    for ex, value in marked.items()
                ]
            )
        return MarkReport(marked=marked, skipped=skipped, marked_at=stamp)

    # -- baskets (the atomic entry, remembered so it can be exited) --------- #
    async def open_basket(
        self,
        basket_id: str,
        *,
        strategy_id: str,
        kind: str,
        legs: list[dict],
        entry_cost: float,
        settlement_value: float,
        opened_at: str | None = None,
    ) -> None:
        """Persist a filled multi-leg basket as ONE open object.

        This is the record the OMS never kept: `manager.py` holds bundles in an in-memory `_pending`
        dict and pops them as soon as they stop needing tracking, which is the instant they become a
        fully-filled position worth exiting. Writing the basket here means a restart — or any other
        component — can still answer "what is open, what did it cost, what is it guaranteed to be
        worth", none of which is recoverable from the netted `positions` table.

        Idempotent on `basket_id`: re-recording the same basket refreshes its terms rather than
        creating a second claim on the same legs, so a retried entry-completion path cannot
        double-count exposure. `legs` is stored as JSON because its arity varies per basket and the
        legs are only ever read back as a whole group (the entry was atomic, so the exit is too).
        Each leg SHOULD carry `exchange_id` so it joins to `positions`/`prices`; `contract_id` is
        accepted as an alias and normalised, because the OMS's `OrderSlice` names that field
        `contract_id` while the state layer keys everything by `exchange_id`.
        """
        if not basket_id:
            raise ValueError("basket_id is required — it IS the basket's identity")
        await self._write(
            [
                (
                    "INSERT INTO baskets(basket_id,strategy_id,kind,legs_json,entry_cost,"
                    "settlement_value,status,opened_at,closed_at,realized_pnl) "
                    "VALUES(?,?,?,?,?,?,'open',?,NULL,NULL) "
                    "ON CONFLICT(basket_id) DO UPDATE SET strategy_id=excluded.strategy_id, "
                    "kind=excluded.kind, legs_json=excluded.legs_json, "
                    "entry_cost=excluded.entry_cost, settlement_value=excluded.settlement_value, "
                    "status='open', opened_at=excluded.opened_at, closed_at=NULL, realized_pnl=NULL",
                    (
                        str(basket_id), str(strategy_id), str(kind),
                        json.dumps(_normalise_legs(legs)),
                        float(entry_cost), float(settlement_value),
                        opened_at or _now(),
                    ),
                )
            ]
        )

    async def read_basket(self, basket_id: str) -> dict | None:
        rows = await self._read("SELECT * FROM baskets WHERE basket_id=?", (str(basket_id),))
        return _basket_row_to_dict(rows[0]) if rows else None

    async def read_open_baskets(self) -> list[dict]:
        """Every basket still open, oldest first, with `legs_json` already parsed into `legs`.

        Oldest-first because capital locked longest is the capital an exit rule should look at first.
        The raw `legs_json` string is kept alongside the parsed `legs` list so a caller that wants
        to log or hash the exact stored bytes does not have to re-serialise and hope the key order
        matches.
        """
        rows = await self._read(
            "SELECT * FROM baskets WHERE status='open' ORDER BY opened_at, basket_id"
        )
        return [_basket_row_to_dict(r) for r in rows]

    async def close_basket(self, basket_id: str, *, realized_pnl: float) -> bool:
        """Mark a basket closed and stamp `closed_at` + the realised result.

        Guarded with `AND status='open'` so the write is a one-way transition: two coroutines that
        both decide to exit the same basket cannot both believe they closed it, and the second one's
        `realized_pnl` cannot overwrite the first's. Returns False when nothing was open under that
        id — the caller must treat that as "someone else already closed this", exactly like
        `write_order`'s `inserted=False`.

        The row is UPDATEd, not deleted: `read_open_baskets` filters on status, and the closed rows
        are the per-basket realised history nothing else records.
        """
        def _txn() -> bool:
            conn = self._conn()
            with conn:
                cur = conn.execute(
                    "UPDATE baskets SET status='closed', closed_at=?, realized_pnl=? "
                    "WHERE basket_id=? AND status='open'",
                    (_now(), float(realized_pnl), str(basket_id)),
                )
            return cur.rowcount > 0

        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    # -- exposure (the combined book) --------------------------------------- #
    async def read_exposure(self) -> Exposure:
        """Exposure over EVERY position, with no strategy filter.

        This is the §5 requirement in one function: the caps are global, so an arb capture and a
        directional conviction draw from the same budget. A per-strategy exposure view would let the
        two loops each believe they had the full cap and jointly breach it — the exact collision the
        shared-state model exists to prevent. Per-strategy numbers ARE reported, but only for
        attribution, and only for open orders.
        """
        rows = await self._read("SELECT exchange_id, quantity, avg_price FROM positions")
        gross = net = longs = shorts = 0.0
        n = 0
        for r in rows:
            qty, px = float(r["quantity"] or 0.0), float(r["avg_price"] or 0.0)
            if qty == 0:
                continue
            n += 1
            notional = qty * px
            gross += abs(notional)
            net += notional
            if qty > 0:
                longs += abs(notional)
            else:
                shorts += abs(notional)
        open_rows = await self._read(
            "SELECT strategy, SUM(ABS((quantity - filled_quantity) * COALESCE(price,0))) AS pending "
            "FROM orders WHERE status IN ('pending','open','partial','ambiguous') GROUP BY strategy"
        )
        return Exposure(
            gross_notional=round(gross, 6),
            net_notional=round(net, 6),
            long_notional=round(longs, 6),
            short_notional=round(shorts, 6),
            n_positions=n,
            by_strategy_open_orders={
                r["strategy"]: round(float(r["pending"] or 0.0), 6) for r in open_rows
            },
        )

    # -- orders (single writer, dedup) -------------------------------------- #
    async def write_order(
        self,
        *,
        idempotency_key: str,
        exchange_id: Any,
        side: str,
        action: str,
        quantity: int,
        price: float | None = None,
        status: str = "pending",
        strategy: str = "unknown",
        broker_order_id: str | None = None,
    ) -> OrderWriteResult:
        """Record an intended order. Idempotent: a repeat key is a NO-OP, not a second order.

        `ON CONFLICT DO NOTHING` plus a changes() read makes the dedup atomic inside one
        transaction, so two coroutines racing the same key cannot both see "absent" and both insert
        — which a read-then-write check would allow.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key is required — it IS the dedup")
        now = _now()

        def _txn() -> OrderWriteResult:
            conn = self._conn()
            with conn:
                cur = conn.execute(
                    "INSERT INTO orders(idempotency_key,exchange_id,side,action,price,quantity,"
                    "status,strategy,broker_order_id,filled_quantity,fill_price,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,0,NULL,?,?) "
                    "ON CONFLICT(idempotency_key) DO NOTHING",
                    (
                        idempotency_key, str(exchange_id), side, action, price, int(quantity),
                        status, strategy, broker_order_id, now, now,
                    ),
                )
                inserted = cur.rowcount == 1
                row = conn.execute(
                    "SELECT status FROM orders WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
            return OrderWriteResult(
                idempotency_key=idempotency_key,
                inserted=inserted,
                status=(row["status"] if row else status),
            )

        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def update_order_status(
        self,
        idempotency_key: str,
        *,
        status: str,
        broker_order_id: str | None = None,
        filled_quantity: int | None = None,
        fill_price: float | None = None,
    ) -> bool:
        def _txn() -> bool:
            conn = self._conn()
            with conn:
                cur = conn.execute(
                    "UPDATE orders SET status=?, "
                    "broker_order_id=COALESCE(?,broker_order_id), "
                    "filled_quantity=COALESCE(?,filled_quantity), "
                    "fill_price=COALESCE(?,fill_price), updated_at=? "
                    "WHERE idempotency_key=?",
                    (status, broker_order_id, filled_quantity, fill_price, _now(), idempotency_key),
                )
            return cur.rowcount > 0

        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def read_order(self, idempotency_key: str) -> dict | None:
        rows = await self._read(
            "SELECT * FROM orders WHERE idempotency_key=?", (idempotency_key,)
        )
        return dict(rows[0]) if rows else None

    async def read_orders(self, *, status: str | None = None, strategy: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM orders", []
        where = []
        if status:
            where.append("status=?")
            params.append(status)
        if strategy:
            where.append("strategy=?")
            params.append(strategy)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, idempotency_key"
        return [dict(r) for r in await self._read(sql, tuple(params))]

    async def read_open_orders(self) -> list[dict]:
        return [
            dict(r)
            for r in await self._read(
                "SELECT * FROM orders WHERE status IN ('pending','open','partial','ambiguous') "
                "ORDER BY created_at"
            )
        ]

    # -- leaderboard / edge performance ------------------------------------- #
    async def write_leaderboard(
        self,
        *,
        rank: int | None,
        balance: float | None,
        third_place_threshold: float | None,
        updated_at: str | None = None,
    ) -> None:
        await self._write(
            [
                (
                    "INSERT INTO leaderboard(id,rank,balance,third_place_threshold,updated_at) "
                    "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET rank=excluded.rank, "
                    "balance=excluded.balance, "
                    "third_place_threshold=excluded.third_place_threshold, "
                    "updated_at=excluded.updated_at",
                    (rank, balance, third_place_threshold, updated_at or _now()),
                )
            ]
        )

    async def read_leaderboard(self) -> dict | None:
        rows = await self._read("SELECT * FROM leaderboard WHERE id=1")
        return dict(rows[0]) if rows else None

    async def read_rho(self) -> float | None:
        """rho = balance / third_place_threshold, the Browne standing ratio. None when either input
        is missing or the threshold is zero — an invented rho would silently mis-scale every
        directional size."""
        lb = await self.read_leaderboard()
        if not lb:
            return None
        bal, t3 = lb.get("balance"), lb.get("third_place_threshold")
        if bal is None or not t3:
            return None
        return float(bal) / float(t3)

    async def upsert_edge_performance(
        self,
        edge: str,
        *,
        realized_pnl: float,
        n_trades: int,
        window: list | None = None,
    ) -> None:
        await self._write(
            [
                (
                    "INSERT INTO edge_performance(edge,realized_pnl,n_trades,window_json,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(edge) DO UPDATE SET "
                    "realized_pnl=excluded.realized_pnl, n_trades=excluded.n_trades, "
                    "window_json=excluded.window_json, updated_at=excluded.updated_at",
                    (
                        edge, float(realized_pnl), int(n_trades),
                        None if window is None else json.dumps(window),
                        _now(),
                    ),
                )
            ]
        )

    async def read_edge_performance(self, edge: str | None = None) -> list[dict]:
        if edge:
            rows = await self._read("SELECT * FROM edge_performance WHERE edge=?", (edge,))
        else:
            rows = await self._read("SELECT * FROM edge_performance ORDER BY edge")
        out = []
        for r in rows:
            d = dict(r)
            if d.get("window_json"):
                try:
                    d["window"] = json.loads(d["window_json"])
                except (TypeError, ValueError):
                    d["window"] = None
            out.append(d)
        return out

    # -- audit log (append-only) -------------------------------------------- #
    async def append_audit(
        self,
        event_type: str,
        *,
        severity: str = "info",
        component: str | None = None,
        exchange_id: Any | None = None,
        payload: dict | None = None,
    ) -> int:
        def _txn() -> int:
            conn = self._conn()
            with conn:
                cur = conn.execute(
                    "INSERT INTO audit_log(event_type,severity,component,exchange_id,payload,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        event_type, severity, component,
                        None if exchange_id is None else str(exchange_id),
                        None if payload is None else json.dumps(payload, sort_keys=True, default=str),
                        _now(),
                    ),
                )
            return int(cur.lastrowid or 0)

        async with self._write_lock:
            return await asyncio.to_thread(_txn)

    async def read_audit(self, *, limit: int = 100, event_type: str | None = None) -> list[dict]:
        if event_type:
            rows = await self._read(
                "SELECT * FROM audit_log WHERE event_type=? ORDER BY id DESC LIMIT ?",
                (event_type, int(limit)),
            )
        else:
            rows = await self._read(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        return [dict(r) for r in rows]

    async def audit_count(self) -> int:
        rows = await self._read("SELECT COUNT(*) AS n FROM audit_log")
        return int(rows[0]["n"]) if rows else 0

    # -- verify-by-reread --------------------------------------------------- #
    async def reread(self) -> StateSnapshot:
        """Cycle-top refresh: drop any read snapshot this thread is holding, then re-read from disk.

        The rollback matters. In WAL a connection that has entered a transaction keeps the snapshot
        it started with, so a loop reusing a connection across a cycle could serve prices the feed
        has already superseded. Rolling back before reading guarantees the next statement opens a
        fresh snapshot — this is the mechanical content of "the disk is truth".
        """
        def _reset() -> None:
            conn = self._conn()
            if conn.in_transaction:
                conn.rollback()

        await asyncio.to_thread(_reset)
        positions, prices, signals, leaderboard, exposure = await asyncio.gather(
            self.read_positions(),
            self.read_prices(),
            self.read_signals(),
            self.read_leaderboard(),
            self.read_exposure(),
        )
        self._reread_seq += 1
        self._snapshot = StateSnapshot(
            taken_at=_now(),
            seq=self._reread_seq,
            positions=positions,
            prices=prices,
            signals=signals,
            leaderboard=leaderboard,
            exposure=exposure,
        )
        return self._snapshot

    @property
    def snapshot(self) -> StateSnapshot | None:
        """The last `reread()` result — the in-memory view, explicitly NOT truth.

        None until the first `reread()`, deliberately: a loop that reads the snapshot without having
        rerun the refresh gets an obvious failure rather than a plausible stale answer.
        """
        return self._snapshot

    @property
    def reread_seq(self) -> int:
        return self._reread_seq
