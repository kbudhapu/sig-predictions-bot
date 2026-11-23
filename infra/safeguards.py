"""Minimal cost circuit-breaker for the write path: K consecutive rate-limit (429) responses trip it, so
the driver STOPS + surfaces rather than hammering a key into a Cloudflare/API lockout. A success resets the
consecutive counter. Duck-typed by sig.write_gate.submit_with_policy (record_success/record_rate_limit/.tripped).
"""
from __future__ import annotations

import time


def is_rate_limit_error(exc: object) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s


class RateLimitBreaker:
    def __init__(self, k_consecutive: int = 3, window_s: float = 300.0) -> None:
        self.k = int(k_consecutive)
        self.window_s = float(window_s)
        self._consecutive = 0
        self._recent: list[float] = []
        self.tripped = False

    def record_success(self) -> None:
        self._consecutive = 0

    def record_rate_limit(self) -> None:
        now = time.monotonic()
        self._consecutive += 1
        self._recent.append(now)
        self._recent = [t for t in self._recent if now - t <= self.window_s]
        if self._consecutive >= self.k or len(self._recent) >= self.k:
            self.tripped = True
