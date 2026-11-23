"""PORTED/adapted from mbappe src/config — the single env-aware data-root seam. Every state path
derives from DATA_DIR, which honours the SIG_DATA_DIR env var (the one seam tests redirect to a tmp
dir). In production the env var is unset -> DATA_DIR == <repo>/data (gitignored).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # sig-bot/
DATA_DIR = Path(os.environ.get("SIG_DATA_DIR") or (ROOT / "data"))
DB_PATH = DATA_DIR / "trading.db"


def ensure_db_writable(db_path: str | Path) -> None:
    """Minimal replacement for mbappe's POSIX ownership-guard: make the parent dir exist. (mbappe's
    version also flock-guards cross-process DB ownership on POSIX; on Windows it is a no-op, so the
    mkdir is the portable, sufficient core.)"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
