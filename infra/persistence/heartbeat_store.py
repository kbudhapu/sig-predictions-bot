"""PORTED/adapted from mbappe src/persistence/heartbeat_store.py — per-component heartbeat rows written
by the DECOUPLED emitter and read by the watchdog. A status payload (gate / escalation / last_cycle_ts)
lets the watchdog tell HALTED-BY-DESIGN from a DEAD ENGINE. Own schema; SQLite source-of-truth.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from infra.config import DB_PATH, ensure_db_writable


def _ensure(db_path: str | Path) -> None:
    ensure_db_writable(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS heartbeat_status ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, component TEXT NOT NULL, "
                     "beat_utc TEXT NOT NULL, status_json TEXT)")


def write_heartbeat_status(component: str, db_path: str | Path = DB_PATH, *, status: dict | None = None,
                           beat_utc: datetime | None = None) -> None:
    _ensure(db_path)
    beat = (beat_utc or datetime.now(timezone.utc)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO heartbeat_status (component, beat_utc, status_json) VALUES (?,?,?)",
                     (component, beat, json.dumps(status, default=str) if status is not None else None))


def _latest_row(component: str, db_path: str | Path):
    _ensure(db_path)
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT beat_utc, status_json FROM heartbeat_status WHERE component=? "
                            "ORDER BY id DESC LIMIT 1", (component,)).fetchone()


def read_latest_heartbeat(component: str, db_path: str | Path = DB_PATH) -> datetime | None:
    row = _latest_row(component, db_path)
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def read_latest_heartbeat_status(component: str, db_path: str | Path = DB_PATH):
    """Return (beat_utc: datetime, status: dict) for the latest beat carrying a payload, else None."""
    row = _latest_row(component, db_path)
    if not row or not row[1]:
        return None
    try:
        beat = datetime.fromisoformat(row[0])
        return (beat, json.loads(row[1]))
    except (ValueError, json.JSONDecodeError):
        return None
