"""Tiny .env loader (no dependency). Reads sig-bot/.env (gitignored) into os.environ if present. Keys
are never printed; callers read them via os.getenv. Idempotent; existing env vars are not overwritten.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_loaded = False


def load_env(path: Path = _ENV_PATH) -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v
    except FileNotFoundError:
        pass


def require(key: str) -> str:
    """Return a secret from the environment (loading .env first), or raise with a REDACTED message."""
    load_env()
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} is not set (put it in sig-bot/.env — gitignored). Value stays secret.")
    return val
