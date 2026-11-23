"""PORTED/trimmed from mbappe src/persistence/db.py — the REUSABLE operational core only.

mbappe's db.py (1793L) also carries its research/attribution vault (shadow_ml_features, regime_champions,
ai_policy_*, …) and coordinator deps that SIG does not need — those are intentionally DROPPED. What ports
is the operational, exchange-agnostic schema (positions/orders/fills/pnl/audit) plus the stores the ported
dead-man + degradation ladder depend on (loop_heartbeats, governance_state, operator_alerts). SQLite is the
source of truth. Only the symbol/status VALUES are domain-flavored; the shapes carry over directly.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from infra.config import DB_PATH, DATA_DIR, ensure_db_writable

_now = lambda: datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path = DB_PATH) -> None:
    """Create the operational tables if they do not exist (idempotent)."""
    ensure_db_writable(db_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL, direction TEXT NOT NULL, qty REAL NOT NULL,
                entry_price REAL, exit_price REAL, pnl REAL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
                pnl REAL NOT NULL, equity REAL NOT NULL, PRIMARY KEY (date, symbol, strategy_id));
            CREATE TABLE IF NOT EXISTS portfolio_state (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, environment TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT, cycle_ms INTEGER, equity REAL, drawdown_pct REAL,
                halted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, status TEXT NOT NULL,
                reject_reason TEXT, expected_price REAL, filled_price REAL,
                client_order_id TEXT);
            CREATE TABLE IF NOT EXISTS strategy_runtime_state (
                strategy_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
                avg_entry_price REAL NOT NULL, bars_in_trade INTEGER NOT NULL DEFAULT 0,
                max_bars_in_trade INTEGER NOT NULL DEFAULT 40, open_order_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, PRIMARY KEY (strategy_id, symbol));
            CREATE TABLE IF NOT EXISTS strategy_open_order_liability (
                strategy_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, open_order_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, metadata TEXT);
            -- stores the ported dead-man + degradation ladder depend on:
            CREATE TABLE IF NOT EXISTS loop_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT NOT NULL, cycle_ts TEXT,
                gate TEXT, escalation_level TEXT, status_json TEXT);
            -- governance_state is owned by infra/persistence/governance_state_store.py (full schema there)
            CREATE TABLE IF NOT EXISTS operator_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT NOT NULL, kind TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info', message TEXT NOT NULL, metadata TEXT);
            -- string key/value meta (engine heartbeat timestamp, etc.) — portfolio_state is REAL-only
            CREATE TABLE IF NOT EXISTS meta_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_utc TEXT NOT NULL);
            """
        )


ENGINE_HEARTBEAT_KEY = "last_successful_cycle_at"


def set_meta(key: str, value: str, db_path: Path = DB_PATH) -> None:
    ensure_db_writable(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO meta_kv (key, value, updated_utc) VALUES (?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_utc=excluded.updated_utc",
                     (key, value, _now()))


def get_meta(key: str, db_path: Path = DB_PATH) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM meta_kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_engine_heartbeat_timestamp(iso_ts: str, db_path: Path = DB_PATH, feed_health: dict | None = None) -> None:
    """Record the last successful cycle timestamp (the dead-man's read target). feed_health is optional
    additive telemetry stored on a loop_heartbeats row."""
    set_meta(ENGINE_HEARTBEAT_KEY, iso_ts, db_path=db_path)
    if feed_health is not None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO loop_heartbeats (created_utc, cycle_ts, status_json) VALUES (?,?,?)",
                         (_now(), iso_ts, json.dumps(feed_health, default=str)))


def get_engine_heartbeat_timestamp(db_path: Path = DB_PATH) -> str | None:
    return get_meta(ENGINE_HEARTBEAT_KEY, db_path=db_path)


def persist_alert(alert: dict, db_path: Path = DB_PATH) -> None:
    """Append an operator alert to the durable operator_alerts table (LogNotifier's leg — this row
    ALWAYS lands regardless of any push channel). Never raises into the caller."""
    init_db(db_path)
    detail = alert.get("detail")
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO operator_alerts (created_utc, kind, severity, message, metadata) VALUES (?,?,?,?,?)",
                (_now(), str(alert.get("kind", "alert")), str(alert.get("severity", "info")),
                 str(alert.get("message", "")), json.dumps(detail, default=str) if detail else None))
    except Exception:
        pass


def recent_alerts(limit: int = 20, db_path: Path = DB_PATH) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM operator_alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_system_event(event_type: str, message: str, severity: str = "info",
                     db_path: Path = DB_PATH, metadata: dict | None = None) -> None:
    """Insert a system event for the ops log. Error paths must never throw (ported discipline): the
    column set is additive and the insert tolerates a pre-metadata schema.

    init_db() first, for the same reason persist_alert() does it: this is an ERROR path, and it is
    reached by processes that may never have created the operational DB. It previously called only
    ensure_db_writable(), so against an uninitialised trading.db it raised `no such table:
    system_events` — and because dispatch_critical_page writes this row BEFORE its push legs, the
    raise aborted the ENTIRE critical page, losing the Discord/webhook notification as well as the
    row. init_db is idempotent (CREATE TABLE IF NOT EXISTS), so this costs one cheap no-op call.
    """
    ensure_db_writable(db_path)
    init_db(db_path)
    metadata_json = json.dumps(metadata, default=str) if metadata else None
    with sqlite3.connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO system_events (timestamp, event_type, severity, message, metadata) VALUES (?,?,?,?,?)",
                (_now(), event_type, severity, message, metadata_json))
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE system_events ADD COLUMN metadata TEXT")
            conn.execute(
                "INSERT INTO system_events (timestamp, event_type, severity, message, metadata) VALUES (?,?,?,?,?)",
                (_now(), event_type, severity, message, metadata_json))


def get_system_events(limit: int = 50, db_path: Path = DB_PATH) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_portfolio_state(key: str, value: float, db_path: Path = DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO portfolio_state (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, float(value)))


def get_portfolio_state(key: str, default: float = 0.0, db_path: Path = DB_PATH) -> float:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM portfolio_state WHERE key=?", (key,)).fetchone()
    return float(row[0]) if row else default


def record_run(environment: str, status: str, *, message: str | None = None, cycle_ms: int | None = None,
               equity: float | None = None, drawdown_pct: float | None = None, halted: bool = False,
               db_path: Path = DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (timestamp, environment, status, message, cycle_ms, equity, drawdown_pct, halted) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_now(), environment, status, message, cycle_ms, equity, drawdown_pct, 1 if halted else 0))


def record_order(symbol: str, strategy_id: str, side: str, qty: float, status: str, *,
                 reject_reason: str | None = None, expected_price: float | None = None,
                 filled_price: float | None = None, client_order_id: str | None = None,
                 db_path: Path = DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO orders (timestamp, symbol, strategy_id, side, qty, status, reject_reason, "
            "expected_price, filled_price, client_order_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), symbol, strategy_id, side, qty, status, reject_reason, expected_price, filled_price, client_order_id))
