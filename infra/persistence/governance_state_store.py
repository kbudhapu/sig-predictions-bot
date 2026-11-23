"""PORTED/adapted from mbappe src/persistence/governance_state_store.py — the single-row degradation
latch that lets a genuine degrade SURVIVE a restart (else restarting silently overrides the safety
system). Owns its own schema (full field set the DegradationManager persists). SQLite source-of-truth.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from infra.config import DB_PATH, ensure_db_writable

_SINGLETON = "SINGLETON"


@dataclass(frozen=True)
class GovernanceStateRecord:
    degradation_mode: str
    trigger_reason: str
    entered_at: str | None
    flatten_latched: bool
    recovery_streak: int


def ensure_governance_state_schema(db_path: Path = DB_PATH) -> None:
    ensure_db_writable(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS governance_state (
                key TEXT PRIMARY KEY,
                degradation_mode TEXT NOT NULL DEFAULT 'NORMAL',
                trigger_reason TEXT NOT NULL DEFAULT '',
                entered_at TEXT,
                flatten_latched INTEGER NOT NULL DEFAULT 0,
                recovery_streak INTEGER NOT NULL DEFAULT 0,
                updated_utc TEXT NOT NULL
            );
            """
        )


def load_governance_state(db_path: Path = DB_PATH) -> GovernanceStateRecord | None:
    ensure_governance_state_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM governance_state WHERE key=?", (_SINGLETON,)).fetchone()
    if row is None:
        return None
    return GovernanceStateRecord(
        degradation_mode=row["degradation_mode"], trigger_reason=row["trigger_reason"],
        entered_at=row["entered_at"], flatten_latched=bool(row["flatten_latched"]),
        recovery_streak=int(row["recovery_streak"]))


def persist_governance_state(*, degradation_mode: str, trigger_reason: str, entered_at: str | None,
                             flatten_latched: bool, recovery_streak: int, db_path: Path = DB_PATH) -> None:
    ensure_governance_state_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO governance_state (key, degradation_mode, trigger_reason, entered_at, "
            "flatten_latched, recovery_streak, updated_utc) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET degradation_mode=excluded.degradation_mode, "
            "trigger_reason=excluded.trigger_reason, entered_at=excluded.entered_at, "
            "flatten_latched=excluded.flatten_latched, recovery_streak=excluded.recovery_streak, "
            "updated_utc=excluded.updated_utc",
            (_SINGLETON, degradation_mode, trigger_reason, entered_at, 1 if flatten_latched else 0,
             int(recovery_streak), now))
