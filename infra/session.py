"""Session calendar interface + the 24/7 default. mbappe's watchdogs key off a US-equities
MarketSessionCalendar (RTH-aware so a stale heartbeat overnight is not a false alarm). SIG Predictions
Cup trades ~24/7 over the competition window (Oct 1 – Nov 4), so the DEFAULT here treats every moment as
open: "quiet" is normal, not a halt. Phase 3 replaces this with sig/session_calendar.py (competition
window bounds + any venue maintenance windows), injected into the watchdogs in place of this default.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionCalendar(Protocol):
    def is_within_rth(self, now: datetime | None = None) -> bool: ...


class AlwaysOpenCalendar:
    """24/7 calendar: the market is always 'open', so the dead-man never treats quiet as a halt."""

    def is_within_rth(self, now: datetime | None = None) -> bool:
        return True

    # alias used by some call sites
    def is_market_open(self, now: datetime | None = None) -> bool:
        return True
