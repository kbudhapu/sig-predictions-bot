"""PORTED from mbappe src/control/heartbeat_emitter.py — dead-man LAYER 2 (decoupled emitter).

Runs as its own asyncio task and emits on schedule REGARDLESS of whether consumption is gated, carrying a
status payload {gate, escalation_level, last_cycle_ts} so the watchdog can tell HALTED-BY-DESIGN from a
DEAD ENGINE (the incident where a halted-by-design soak looked identical to a dead process). Owns no
network I/O; a heartbeat write can never be blocked by a poll. `resilient_supabase_call` is retained as a
reusable timeout+backoff wrapper (SIG can wrap any flaky remote call with it); the emitter never calls it.
SIG adaptation: imports -> infra.*.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import structlog

from infra.persistence.heartbeat_store import write_heartbeat_status

log = structlog.get_logger()

GATE_OPEN = "OPEN"
GATE_BLOCKED = "BLOCKED"


class RemotePollMetrics:
    def __init__(self) -> None:
        self.attempts = 0
        self.failures = 0
        self.timeouts = 0

    def as_dict(self) -> dict[str, int]:
        return {"attempts": self.attempts, "failures": self.failures, "timeouts": self.timeouts}


async def resilient_remote_call(fn: Callable[[], Any], *, timeout_s: float = 10.0, attempts: int = 3,
                                backoff_s: float = 1.0, metrics: RemotePollMetrics | None = None,
                                label: str = "remote_poll") -> Any:
    """Run `fn` (sync or async) with a per-attempt timeout + exponential backoff, counting failures.
    Returns the result or None once attempts are exhausted (never raises) — so a dropped connection
    neither stalls the loop nor is mistaken for death. NEVER call from the heartbeat emitter."""
    metrics = metrics or RemotePollMetrics()
    for attempt in range(1, max(1, int(attempts)) + 1):
        metrics.attempts += 1
        try:
            coro = fn() if asyncio.iscoroutinefunction(fn) else asyncio.to_thread(fn)
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            metrics.timeouts += 1; metrics.failures += 1
            log.warning("remote_poll_timeout", label=label, attempt=attempt, timeout_s=timeout_s)
        except Exception as exc:
            metrics.failures += 1
            log.warning("remote_poll_failed", label=label, attempt=attempt, error=str(exc))
        if attempt < attempts and backoff_s > 0:
            await asyncio.sleep(backoff_s * (2 ** (attempt - 1)))
    return None


class HeartbeatEmitter:
    """Independent heartbeat task. Emits every `interval_seconds` regardless of consumption state."""

    def __init__(self, *, db_path: str | Path, component: str = "sig-bot", interval_seconds: float = 30.0,
                 gate_state_fn: Callable[[], tuple[str, str]], escalation_level_fn: Callable[[], str],
                 last_cycle_ts_fn: Callable[[], datetime | None]) -> None:
        self._db_path = db_path
        self._component = component
        self._interval = float(interval_seconds)
        self._gate_state_fn = gate_state_fn
        self._escalation_level_fn = escalation_level_fn
        self._last_cycle_ts_fn = last_cycle_ts_fn

    def _status(self) -> dict[str, Any]:
        gate, reason = self._gate_state_fn()
        lct = self._last_cycle_ts_fn()
        return {"gate": gate, "gate_reason": reason or "", "escalation_level": self._escalation_level_fn(),
                "last_cycle_ts": lct.isoformat() if lct is not None else None}

    def emit_once(self, *, beat_utc: datetime | None = None) -> dict[str, Any]:
        status = self._status()
        write_heartbeat_status(self._component, self._db_path, status=status, beat_utc=beat_utc)
        return status

    async def run(self, *, iterations: int | None = None) -> None:
        count = 0
        while iterations is None or count < iterations:
            try:
                self.emit_once()
            except Exception:
                log.warning("heartbeat_emit_failed", component=self._component, exc_info=True)
            count += 1
            if iterations is not None and count >= iterations:
                break
            await asyncio.sleep(self._interval)
