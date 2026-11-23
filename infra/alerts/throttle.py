"""PORTED from mbappe src/alerts/throttle.py (verbatim) — ThrottledNotifier: dedup + exponential
backoff for the PUSH channels, keyed by (component, kind). The durable LogNotifier row is NEVER wrapped
by this — every alert always lands in operator_alerts; throttling governs only what pages a human.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import structlog

log = structlog.get_logger()

_BACKOFF_SCHEDULE_S = (300.0, 900.0, 3600.0)   # first send immediate; then 5m, 15m, 60m, then hourly


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class ThrottledNotifier:
    """Wrap a push notifier with dedup + exponential backoff, keyed by (component, kind)."""

    def __init__(self, inner, *, clock: Callable[[], datetime] | None = None) -> None:
        self._inner = inner
        self._clock = clock or _default_clock
        self._state: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(alert: dict) -> tuple[str, str]:
        detail = alert.get("detail") or {}
        return (str(detail.get("component", "")), str(alert.get("kind", "")))

    def _required_wait(self, sends: int) -> float:
        return _BACKOFF_SCHEDULE_S[min(sends - 1, len(_BACKOFF_SCHEDULE_S) - 1)]

    def notify(self, alert: dict) -> None:
        key = self._key(alert)
        now = self._clock()
        st = self._state.get(key)
        if st is None:
            self._state[key] = {"last_sent": now, "sends": 1}
            self._inner.notify(alert)
            return
        elapsed = (now - st["last_sent"]).total_seconds()
        wait = self._required_wait(st["sends"])
        if elapsed >= wait:
            st["last_sent"] = now
            st["sends"] += 1
            self._inner.notify(alert)
        else:
            log.debug("alert_throttled", component=key[0], kind=key[1], elapsed_s=round(elapsed, 1),
                      required_wait_s=wait, sends=st["sends"])

    def resolve(self, *, component: str, kind: str) -> None:
        key = (str(component), str(kind))
        if key not in self._state:
            return
        del self._state[key]
        try:
            self._inner.notify({"kind": kind, "severity": "info", "message": f"{component} {kind} RECOVERED",
                                "detail": {"component": component, "recovered": True}})
        except Exception as e:
            log.warning("recovered_notify_failed", component=component, kind=kind, error=str(e))
