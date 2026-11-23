"""PORTED from mbappe src/alerts/send_interlock.py — HARD INTERLOCK: the test suite must NEVER page a
human. Blocks only the REAL outbound transports; injected mock transports are untouched. SIG adaptation:
the escape-hatch env var is SIG_ALLOW_REAL_NOTIFY.
"""
from __future__ import annotations

import os
import sys

import structlog

log = structlog.get_logger()

_ALLOW_ENV = "SIG_ALLOW_REAL_NOTIFY"
_blocked_logged: set[str] = set()


def running_under_test() -> bool:
    """True when a real outbound notification MUST be suppressed (test context). Broad on purpose."""
    if os.environ.get(_ALLOW_ENV) == "1":
        return False
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def log_blocked_once(channel: str) -> None:
    if channel not in _blocked_logged:
        _blocked_logged.add(channel)
        log.warning("real_notify_blocked_under_test", channel=channel,
                    reason="test context detected; refusing real outbound send",
                    override=f"set {_ALLOW_ENV}=1 to allow")
