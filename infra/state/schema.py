"""Shared-state schema + connection setup for the SIG bot.

WHY A SEPARATE DB FILE FROM infra/persistence/db.py
`infra/persistence/db.py` is the mbappe-inherited operational DB (trades / daily_pnl /
portfolio_state / runs) and it ALREADY defines an `orders` table with an incompatible shape
(symbol, strategy_id, qty, no idempotency_key, no exchange_id). The integration spec §5 requires a
single idempotency-keyed order path, so reusing that table would mean either breaking the inherited
schema or giving up the UNIQUE dedup that makes double-placement impossible. The shared state
therefore lives in its own file, `state.db`, derived from the same `infra.config.DATA_DIR` seam so
tests redirect it with SIG_DATA_DIR exactly like every other path.

PRAGMAS — ported from mbappe `src/persistence/db_queue.py::_connect_wal`
    journal_mode=WAL    readers do not block the writer and the writer does not block readers, which
                        is the whole reason the feed can stream prices while both loops read (§5).
    busy_timeout=30000  mbappe's comment is the rationale verbatim: concurrent producers plus a
                        draining writer must not raise 'database is locked' and DROP writes under a
                        high-volume storm. 30s is generous on purpose.
    synchronous=NORMAL  safe under WAL (a crash can lose the tail of the last transaction, never
                        corrupt the file) and much faster than FULL. The feed writes constantly;
                        FULL would fsync on every price tick.
    foreign_keys=ON     so an audit row cannot reference a vanished order.

APPEND-ONLY IS ENFORCED, NOT CONVENTIONAL
mbappe's `AppendOnlyStore` documents append-only as a discipline ("a table that is only ever
INSERTed into"). Here the audit log carries TRIGGERs that RAISE(ABORT) on UPDATE and DELETE, so
immutability is a property of the database rather than of whoever writes the next store. That is the
one place this schema deliberately goes further than the reference.

MIGRATION — THERE IS NONE, AND FOR ADDED TABLES NONE IS NEEDED
`SCHEMA_VERSION` is only ever STAMPED into `state_meta`; nothing reads it back and no ALTER/upgrade
path exists. That is sound for this schema's one evolution mode: `init_state_db` re-runs the whole
DDL script on every process start and every statement is `CREATE ... IF NOT EXISTS`, so an existing
`state.db` simply gains any newly-added table (v2 added `baskets`) on the next open, with its old
tables and rows untouched. The version stamp is therefore documentation of intent rather than a gate
— bump it when the DDL changes so the file on disk says which shape it was created/last opened
under. The limit worth knowing before the next change: adding a COLUMN to an EXISTING table is NOT
covered by this, because `CREATE TABLE IF NOT EXISTS` is a no-op against an older table and SQLite
has no `ADD COLUMN IF NOT EXISTS`. That change would need real migration code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from infra.config import DATA_DIR, ensure_db_writable

#: The shared-state DB. Separate from infra.config.DB_PATH — see the module docstring.
STATE_DB_PATH = DATA_DIR / "state.db"

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=30000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)

SCHEMA_VERSION = 3

DDL = """
-- positions: per exchange, the COMBINED book. Both loops size against this (§5): an arb fill
-- consumes exposure the directional book then cannot deploy, and vice versa.
CREATE TABLE IF NOT EXISTS positions (
    exchange_id     TEXT PRIMARY KEY,
    quantity        REAL NOT NULL DEFAULT 0,
    avg_price       REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    unrealized_pnl  REAL NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

-- baskets: BASKET IDENTITY, which `positions` deliberately cannot carry.
--
-- WHY A SECOND TABLE AND NOT A bundle_id COLUMN ON positions
-- `positions` is keyed by exchange_id BECAUSE it is the netted combined book both loops size
-- against (see above). Adding bundle_id to its key would permit N rows per exchange, and then the
-- arb loop and the directional loop would each see only "their" slice of an exchange and could each
-- believe the full cap was free — the precise collision §5 exists to prevent. So netting stays in
-- `positions` and basket identity lives here, joined back by exchange_id when needed.
--
-- WHY IT HAS TO BE PERSISTED AT ALL
-- A multi-leg basket is entered ATOMICALLY, so it must be exited as one object. The only record of
-- that grouping used to be sig/oms/manager.py's in-memory `_pending` dict, which POPS a bundle the
-- moment it stops needing tracking — i.e. exactly when it becomes a fully-filled open position
-- worth exiting. After entry completed, nothing anywhere knew the basket existed or what it cost.
CREATE TABLE IF NOT EXISTS baskets (
    basket_id        TEXT PRIMARY KEY,       -- stable id; the OMS bundle_id when there is one
    strategy_id      TEXT NOT NULL,
    kind             TEXT NOT NULL,          -- hard_arb | soft_mispricing | directional | ...
    legs_json        TEXT NOT NULL,          -- [{exchange_id, side, quantity, entry_price}, ...]
    entry_cost       REAL NOT NULL,          -- total paid to open (negative = net credit received)
    settlement_value REAL NOT NULL,          -- GUARANTEED value at settlement; the caller supplies
                                             -- it (for a closed complementary basket of N legs each
                                             -- paying $1 on exactly one winner, = the contract count)
    status           TEXT NOT NULL,          -- open | closed
    opened_at        TEXT NOT NULL,
    closed_at        TEXT,
    realized_pnl     REAL
);
CREATE INDEX IF NOT EXISTS ix_baskets_status   ON baskets(status);
CREATE INDEX IF NOT EXISTS ix_baskets_strategy ON baskets(strategy_id);

-- orders: THE single order path (§5). idempotency_key is UNIQUE, so a double submit from either
-- loop collapses to one row by construction rather than by careful calling.
CREATE TABLE IF NOT EXISTS orders (
    idempotency_key TEXT PRIMARY KEY,
    exchange_id     TEXT NOT NULL,
    side            TEXT NOT NULL,          -- yes | no
    action          TEXT NOT NULL,          -- buy | sell
    price           REAL,
    quantity        INTEGER NOT NULL,
    status          TEXT NOT NULL,          -- pending|open|filled|partial|rejected|cancelled|ambiguous
    strategy        TEXT NOT NULL,          -- which edge/loop produced it
    broker_order_id TEXT,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    fill_price      REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_status   ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_exchange ON orders(exchange_id);
CREATE INDEX IF NOT EXISTS ix_orders_strategy ON orders(strategy);
CREATE INDEX IF NOT EXISTS ix_orders_updated  ON orders(updated_at);

-- prices: written continuously by the realtime feed; read by both loops.
CREATE TABLE IF NOT EXISTS prices (
    exchange_id TEXT PRIMARY KEY,
    best_bid    REAL,
    best_ask    REAL,
    spread      REAL,
    sequence    INTEGER,                    -- orderbook asOf.sequence, monotonic staleness guard
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prices_updated ON prices(updated_at);

-- mid_history: a periodic TIME-SERIES of per-market mids (the `prices` table is latest-only). The venue
-- has no usable price history beyond ~24h and a dead price-history endpoint, so we self-collect forward:
-- this is the substrate for validating shadow signals against actual forward moves, measuring realized
-- edge, and any time-series research. Written by the mid-history recorder loop; pruned to a retention.
CREATE TABLE IF NOT EXISTS mid_history (
    exchange_id TEXT NOT NULL,
    ts          TEXT NOT NULL,                -- ISO-8601 UTC capture time
    mid         REAL NOT NULL,
    PRIMARY KEY (exchange_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_mid_history_ts ON mid_history(ts);

-- kalshi_history: Kalshi party-market quotes per mapped SIG leg (signals/kalshi_map.yaml), sampled each minute by
-- main_sig._kalshi_shadow_loop. Record-only second benchmark (2026-10-07).
CREATE TABLE IF NOT EXISTS kalshi_history (
    exchange_id TEXT NOT NULL,                -- SIG exchange id of the mapped leg (same party as the Kalshi market)
    ts          TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    bid         REAL,
    ask         REAL,
    mid         REAL,
    last        REAL,
    volume_24h  REAL,
    PRIMARY KEY (exchange_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_kalshi_history_ts ON kalshi_history(ts);

-- trade_tape: SIG public trades per market (GET /exchanges/{id}/trades), polled slowly by main_sig._trade_tape_loop.
-- price is YES-normalized; side = the AGGRESSOR's outcome (YES = bought YES, NO = bought NO ≡ sold YES).
CREATE TABLE IF NOT EXISTS trade_tape (
    exchange_id TEXT NOT NULL,
    created_at  TEXT NOT NULL,                -- venue trade time (ISO)
    side        TEXT NOT NULL,
    price       REAL NOT NULL,
    size        REAL NOT NULL,
    trade_id    TEXT,                         -- venue id if the payload carries one
    ingested_at TEXT,
    PRIMARY KEY (exchange_id, created_at, side, price, size)
);
CREATE INDEX IF NOT EXISTS ix_trade_tape_ts ON trade_tape(created_at);

-- sharp_history: the SIG-aligned real-money benchmark (Polymarket) per cross-market race, sampled each minute,
-- alongside SIG's mid at the same instant. The venue keeps no history and the provider only caches the latest
-- quote, so without this nothing can be backtested against the sharp (exit thresholds, sniper, MM anchor).
CREATE TABLE IF NOT EXISTS sharp_history (
    exchange_id  TEXT NOT NULL,
    ts           TEXT NOT NULL,                -- ISO-8601 UTC capture time
    sharp        REAL NOT NULL,                -- SIG-aligned benchmark YES prob (after the polarity flip)
    raw          REAL,                         -- venue-native price before alignment
    sig_mid      REAL,                         -- SIG mid at capture (None if unknown)
    venue        TEXT,                         -- 'polymarket'
    PRIMARY KEY (exchange_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_sharp_history_ts ON sharp_history(ts);

-- nav_history: account value over time (cash + marked positions) — the only honest P&L-per-day series.
CREATE TABLE IF NOT EXISTS nav_history (
    ts           TEXT PRIMARY KEY,             -- ISO-8601 UTC
    cash         REAL,
    market_value REAL,
    nav          REAL NOT NULL,
    unrealized   REAL,
    positions    INTEGER,
    lb_pnl       REAL,                         -- venue leaderboard P&L (authoritative total), if read
    lb_rank      INTEGER
);

-- strategy_fills: a CUMULATIVE, durable ledger of venue fills tagged by the strategy that placed them.
-- The venue's portfolio_fills returns only the most-recent ~200 fills (history slides off), so we ingest
-- them over time into this table (dedup by the venue fill id) and attribute each to a strategy via the
-- local orders table (broker_order_id -> strategy). strategy='UNATTRIBUTED' when the order_id wasn't
-- captured locally (a separate placement-capture gap). mid_at_fill is snapped from mid_history at ingest.
CREATE TABLE IF NOT EXISTS strategy_fills (
    fill_id      TEXT PRIMARY KEY,           -- venue fill id (dedup)
    order_id     TEXT,
    strategy     TEXT NOT NULL,
    exchange_id  TEXT NOT NULL,
    outcome      TEXT,                        -- yes | no (venue side)
    price        REAL NOT NULL,
    quantity     REAL NOT NULL,
    filled_at    TEXT,
    mid_at_fill  REAL,
    ingested_at  TEXT NOT NULL,
    signed_qty   REAL                         -- RAW venue fill quantity, SIGNED (v3; NULL on older rows)
);
CREATE INDEX IF NOT EXISTS ix_strategy_fills_strategy ON strategy_fills(strategy);
CREATE INDEX IF NOT EXISTS ix_strategy_fills_filled_at ON strategy_fills(filled_at);

-- order_attribution: the AUTHORITATIVE venue_order_id -> strategy map, captured at the single placement
-- chokepoint (SigBroker) the instant we hold both the strategy and the venue order id. This exists because
-- SIG never echoes our idempotencyKey back on the order or the fill, and the local `orders` table's
-- broker_order_id backfill is unreliable on high-rate paths (esp. market_making: ~44% NULL) — so a fill
-- (which carries only orderId) otherwise can't be tied to a strategy. fill ingest attributes via THIS
-- table first, then falls back to orders.broker_order_id. Forward-only (pre-existing fills stay UNATTRIBUTED
-- because the link was never captured and can't be reconstructed).
CREATE TABLE IF NOT EXISTS order_attribution (
    broker_order_id TEXT PRIMARY KEY,           -- venue order id (== strategy_fills.order_id / fill.orderId)
    strategy        TEXT NOT NULL,
    exchange_id     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_order_attribution_strategy ON order_attribution(strategy);

-- signals: written by the research/signal component, read by sizing. Staleness via updated_at —
-- a stale signal is shrunk, not trusted (§7).
CREATE TABLE IF NOT EXISTS signals (
    exchange_id TEXT PRIMARY KEY,
    market_id   TEXT,
    p           REAL NOT NULL,              -- fair value
    sigma_p     REAL,                       -- uncertainty
    divergence  REAL,                       -- p vs impact-adjusted market price
    flags       TEXT,                       -- JSON
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_signals_updated ON signals(updated_at);

-- leaderboard: single row (id=1) for the rho = B / T3 computation.
CREATE TABLE IF NOT EXISTS leaderboard (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    rank                    INTEGER,
    balance                 REAL,
    third_place_threshold   REAL,
    updated_at              TEXT NOT NULL
);

-- edge_performance: per-edge realised results for the allocator.
CREATE TABLE IF NOT EXISTS edge_performance (
    edge          TEXT PRIMARY KEY,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    n_trades      INTEGER NOT NULL DEFAULT 0,
    window_json   TEXT,                     -- rolling-window samples, JSON
    updated_at    TEXT NOT NULL
);

-- audit_log: append-only event journal. Every decision, order and halt.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'info',
    component   TEXT,
    exchange_id TEXT,
    payload     TEXT,                       -- JSON
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS ix_audit_type    ON audit_log(event_type);

-- Immutability enforced by the DB, not by convention (see module docstring).
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is forbidden');
END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is forbidden');
END;

CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(db_path: str | Path = STATE_DB_PATH) -> sqlite3.Connection:
    """Open a WAL connection with the ported pragma set.

    `check_same_thread=False` because `asyncio.to_thread` hands work to an arbitrary pool thread;
    connections are still held thread-locally (store.py) so no single connection is ever used
    concurrently — the flag relaxes sqlite3's ownership assertion, it does not share a connection.
    """
    ensure_db_writable(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


#: Columns added to EXISTING tables after first release: (table, column, decl). `CREATE TABLE IF NOT EXISTS`
#: can't add a column to an older table, so each is ADDed here when missing (idempotent, cheap PRAGMA check).
_ADDED_COLUMNS = (
    # v3: the venue fill quantity WITH its sign. `quantity` stays abs() for existing readers. Convention
    # (verified live): sign = direction of the trade in the fill's own `outcome`; YES-space delta is
    # +signed_qty for an outcome='yes' fill and −signed_qty for an outcome='no' fill.
    ("strategy_fills", "signed_qty", "REAL"),
    # SIG touch at capture, so backtests use REAL per-minute spreads (not today's spread as a constant).
    ("sharp_history", "sig_bid", "REAL"),
    ("sharp_history", "sig_ask", "REAL"),
    # Σ held YES-qty × (sharp − SIG mid) over mapped races: what the book is worth ABOVE its mark if the sharp
    # is right. nav + book_edge = sharp-valued NAV; its daily change is profit/day incl. not-yet-converged edge.
    ("nav_history", "book_edge", "REAL"),
    # top-of-book per minute (depth research): bid/ask and the shares resting at each, from the prices table.
    ("mid_history", "bid", "REAL"),
    ("mid_history", "ask", "REAL"),
    ("mid_history", "bid_size", "REAL"),
    ("mid_history", "ask_size", "REAL"),
    # 2026-10-11: the order's DIRECTION (buy|sell) captured at placement. SIG fills carry no action and their quantity
    # sign follows the OUTCOME (no -> negative) for buys AND sells, so a NO sale looked like a NO buy (pilot loss
    # measure read -$4,015 for a real +$16). Fill ingest derives the true YES-delta from this.
    ("order_attribution", "action", "TEXT"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, col, decl in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_state_db(db_path: str | Path = STATE_DB_PATH) -> None:
    """Idempotent schema create. Safe to call from every process start."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        with conn:
            conn.executescript(DDL)
            _migrate_columns(conn)
            conn.execute(
                "INSERT INTO state_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
    finally:
        conn.close()
