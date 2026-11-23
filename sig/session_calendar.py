"""SIG session calendar (Phase 3) — replaces mbappe's US-equities RTH MarketSessionCalendar that the
watchdogs key off. SIG Predictions Cup trades effectively 24/7 over the competition window
(Oct 1 – Nov 4, 2026), so inside the window every moment is "open" and the dead-man treats quiet as
NORMAL, not a halt. Outside the window the market is closed (loop idle by design — no false stale-page).
Implements infra.session.SessionCalendar (is_within_rth), injected into the watchdogs in place of the
24/7 default.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Confirmed from the live SIG API (/tournaments/midterm-elections): startDate/endDate (UTC).
COMPETITION_START = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
COMPETITION_END = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)


class SigSessionCalendar:
    """24/7 within the competition window; closed outside it. Optional maintenance windows can be added
    as (start,end) pairs later — a stale heartbeat inside one would then be halted-by-design, not dead."""

    def __init__(self, *, start: datetime = COMPETITION_START, end: datetime = COMPETITION_END,
                 maintenance_windows: list[tuple[datetime, datetime]] | None = None) -> None:
        self._start = start
        self._end = end
        self._maint = maintenance_windows or []

    def is_within_rth(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if not (self._start <= now <= self._end):
            return False
        for a, b in self._maint:
            if a <= now <= b:
                return False
        return True

    def is_market_open(self, now: datetime | None = None) -> bool:
        return self.is_within_rth(now)
