"""THE NET-NEW PIECE (does NOT exist in mbappe) — a true OUTBOUND write-gate + an order-path
429/Retry-After retry handler. Written fresh per the dispatch; the retry logic elsewhere in the repo
(alert-channel 429, WS reconnect) does NOT cover the order path.

Two parts:
 A. OutboundRateGate — a true per-KEY sliding-window rate GATE (not mbappe's entry-suppression model)
    enforcing SIG's real limits: 100 reads/min, 30 writes/min PER KEY. Writes are budgeted across the
    (eventually 3) keys by URGENCY: a "fast" key reserves write budget for arb/urgent so routine quoting
    can never starve it. All numbers are CONFIG (SigGateConfig).
 B. submit_with_policy — order-path retry honoring the documented SIG semantics:
      429 + Retry-After          -> wait Retry-After (default 60s) then retry same idempotencyKey
      409 REQUEST_IN_FLIGHT      -> retry SAME key after 90s (the prior submit is still resolving)
      502 ORDER_STATUS_UNKNOWN   -> AMBIGUOUS: do NOT blind-retry; reconcile (check positions / query the
                                    idempotencyKey) — it may have gone through
      503 TX_CONFLICT            -> exponential backoff, then retry
      other 4xx                  -> do NOT auto-retry (surface)
    A ported RateLimitBreaker (Safeguard 4) can be injected: sustained 429s trip it -> STOP + surface,
    never silently hammer the key into a lockout.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


@dataclass
class SigGateConfig:
    reads_per_min: int = 100
    writes_per_min: int = 30
    window_s: float = 60.0
    fast_key_reserved_writes_per_min: int = 10   # reserved on the fast key for urgent/arb only
    retry_after_default_s: float = 60.0          # 429 when no Retry-After header
    in_flight_retry_s: float = 90.0              # 409 REQUEST_IN_FLIGHT
    tx_conflict_backoff_s: tuple = (1.0, 2.0, 4.0, 8.0)   # 503 TX_CONFLICT
    max_retries: int = 5


class OutboundRateGate:
    """Per-key sliding-window rate gate. key_id is a logical lane ('read'|'write'|'fast'). Thread-safe
    enough for a single-process bot (GIL-guarded deque ops); a multi-process deploy would back this with
    the same shared-SQLite approach as OutboundCap."""

    def __init__(self, cfg: SigGateConfig | None = None, *, now_fn: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg or SigGateConfig()
        self._now = now_fn
        self._hits: dict[tuple[str, str], deque] = {}

    def _bucket(self, key_id: str, kind: str) -> deque:
        return self._hits.setdefault((key_id, kind), deque())

    def _prune(self, dq: deque, now: float) -> None:
        cutoff = now - self.cfg.window_s
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def _limit(self, key_id: str, kind: str, urgent: bool) -> int:
        if kind == "read":
            return self.cfg.reads_per_min
        # writes: on the fast key, routine traffic may only use (writes_per_min - reserved); urgent gets all
        if key_id == "fast" and not urgent:
            return max(0, self.cfg.writes_per_min - self.cfg.fast_key_reserved_writes_per_min)
        return self.cfg.writes_per_min

    def check(self, key_id: str, kind: str, *, urgent: bool = False) -> tuple[bool, float]:
        """(allowed, wait_s). Does NOT consume — use allow() to consume on success."""
        now = self._now()
        dq = self._bucket(key_id, kind)
        self._prune(dq, now)
        if len(dq) < self._limit(key_id, kind, urgent):
            return True, 0.0
        wait = max(0.0, (dq[0] + self.cfg.window_s) - now)
        return False, wait

    def allow(self, key_id: str, kind: str, *, urgent: bool = False) -> tuple[bool, float]:
        """Try to consume one slot. (allowed, wait_s_if_denied)."""
        ok, wait = self.check(key_id, kind, urgent=urgent)
        if ok:
            self._bucket(key_id, kind).append(self._now())
        return ok, wait

    def pick_write_key(self, *, urgent: bool, available_keys: tuple[str, ...] = ("write", "fast")) -> str | None:
        """Choose a key with spare WRITE budget. Urgent prefers 'fast' (its reserved lane); routine prefers
        'write' and only spills to 'fast''s non-reserved portion. Returns None if every key is saturated."""
        order = (("fast", "write") if urgent else ("write", "fast"))
        for k in order:
            if k in available_keys and self.check(k, "write", urgent=urgent)[0]:
                return k
        return None


class RetryAction(str, Enum):
    OK = "OK"
    RETRY_AFTER = "RETRY_AFTER"           # 429
    RETRY_IN_FLIGHT = "RETRY_IN_FLIGHT"   # 409
    RECONCILE = "RECONCILE"               # 502 ambiguous
    BACKOFF = "BACKOFF"                   # 503
    NO_RETRY = "NO_RETRY"                 # other 4xx


@dataclass
class HttpResult:
    status: int
    body: str = ""
    retry_after_s: float | None = None    # parsed from Retry-After header


def classify(status: int, body: str = "") -> RetryAction:
    b = (body or "").upper()
    if 200 <= status < 300:
        return RetryAction.OK
    if status == 429:
        return RetryAction.RETRY_AFTER
    if status == 409:                     # REQUEST_IN_FLIGHT
        return RetryAction.RETRY_IN_FLIGHT
    if status == 502:                     # ORDER_STATUS_UNKNOWN — may have landed
        return RetryAction.RECONCILE
    if status == 503:                     # TX_CONFLICT
        return RetryAction.BACKOFF
    return RetryAction.NO_RETRY


class SigWriteError(RuntimeError):
    def __init__(self, status: int, action: RetryAction, body: str = "") -> None:
        self.status = status
        self.action = action
        super().__init__(f"SIG write failed status={status} action={action.value}: {body[:160]}")


@dataclass
class PolicyOutcome:
    ok: bool
    result: HttpResult | None
    action: RetryAction
    attempts: int
    reconcile_needed: bool = False
    retries: list[str] = field(default_factory=list)


def submit_with_policy(do_request: Callable[[], HttpResult], cfg: SigGateConfig | None = None, *,
                       breaker=None, sleep_fn: Callable[[float], None] = time.sleep) -> PolicyOutcome:
    """Run an order write with the SIG retry policy. `do_request` performs ONE attempt (same idempotencyKey
    across retries — the caller holds the key fixed). `breaker` (optional RateLimitBreaker) trips on
    sustained 429s -> STOP+surface. On 502 we STOP and flag reconcile_needed (never blind-retry an
    ambiguous order)."""
    cfg = cfg or SigGateConfig()
    retries: list[str] = []
    conflict_i = 0
    for attempt in range(1, cfg.max_retries + 1):
        res = do_request()
        act = classify(res.status, res.body)
        if act == RetryAction.OK:
            if breaker is not None:
                breaker.record_success()
            return PolicyOutcome(True, res, act, attempt, retries=retries)
        if act == RetryAction.NO_RETRY:
            raise SigWriteError(res.status, act, res.body)
        if act == RetryAction.RECONCILE:
            # 502 ORDER_STATUS_UNKNOWN: ambiguous — the order may have gone through. Do NOT retry blindly;
            # hand back for reconcile (check positions / query the idempotencyKey) before any resubmit.
            return PolicyOutcome(False, res, act, attempt, reconcile_needed=True, retries=retries)
        if act == RetryAction.RETRY_AFTER:
            if breaker is not None:
                breaker.record_rate_limit()
                if breaker.tripped:
                    raise SigWriteError(res.status, act, "rate-limit breaker tripped (sustained 429) — STOP+surface")
            wait = res.retry_after_s if res.retry_after_s is not None else cfg.retry_after_default_s
            retries.append(f"429->wait{wait:.0f}s"); sleep_fn(wait); continue
        if act == RetryAction.RETRY_IN_FLIGHT:
            retries.append(f"409->wait{cfg.in_flight_retry_s:.0f}s"); sleep_fn(cfg.in_flight_retry_s); continue
        if act == RetryAction.BACKOFF:
            w = cfg.tx_conflict_backoff_s[min(conflict_i, len(cfg.tx_conflict_backoff_s) - 1)]
            conflict_i += 1
            retries.append(f"503->backoff{w:.0f}s"); sleep_fn(w); continue
    # retries exhausted
    return PolicyOutcome(False, res, classify(res.status, res.body), cfg.max_retries, retries=retries)
