"""PORTED from mbappe src/control/heartbeat_watchdog.py — dead-man LAYER 3 (independent watchdog).

Reads the decoupled emitter's beats and distinguishes: (a) DEAD ENGINE — fresh beat but stale
last_cycle_ts -> CRITICAL + ENTRY_GATE_HALT; (b) HALTED-BY-DESIGN — fresh beat + current cycle but gate
BLOCKED -> WARNING, no escalation; (c) healthy. Startup grace avoids paging on the predecessor's beat.
Safety actions never depend on any channel. SIG adaptations: imports -> infra.*; the calendar defaults to
AlwaysOpenCalendar (24/7 competition) so quiet is normal; auto-de-escalation OFF in live.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

from infra.escalation import RiskEscalationEngine, RiskEscalationLevel
from infra.session import AlwaysOpenCalendar, SessionCalendar
from infra.alerts.notifiers import LogNotifier, Notifier  # noqa: F401  (LogNotifier re-exported for convenience)
from infra.persistence.heartbeat_store import read_latest_heartbeat, read_latest_heartbeat_status

log = structlog.get_logger()


@dataclass
class WatchdogResult:
    tripped: bool
    age_seconds: float | None
    market_open: bool
    reason: str = ""


class HeartbeatWatchdog:
    """Independent liveness watchdog for the main loop."""

    def __init__(self, *, db_path: str | Path, escalation: RiskEscalationEngine, notifier: Notifier,
                 stale_threshold_seconds: float = 120.0, component: str = "sig-bot",
                 calendar: SessionCalendar | None = None, started_at: datetime | None = None,
                 environment: str = "paper", auto_deescalation_enabled: bool = True,
                 deescalation_fresh_streak: int = 10) -> None:
        self._db_path = db_path
        self._escalation = escalation
        self._notifier = notifier
        self._threshold = float(stale_threshold_seconds)
        self._component = component
        self._calendar = calendar or AlwaysOpenCalendar()
        self._allow_deesc = bool(auto_deescalation_enabled) and str(environment).lower() != "live"
        self._deesc_streak_required = int(deescalation_fresh_streak)
        self._fresh_streak = 0
        self._started_at = started_at or datetime.now(timezone.utc)
        self._logged_prior_age = False

    def check(self, now: datetime | None = None) -> WatchdogResult:
        now = now or datetime.now(timezone.utc)
        market_open = self._calendar.is_within_rth(now)
        latest = read_latest_heartbeat(self._component, self._db_path)

        if not market_open:
            return WatchdogResult(False, None, market_open=False, reason="market_closed")

        if latest is None:
            age = None
        else:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            age = (now - latest).total_seconds()

        if not self._logged_prior_age:
            self._logged_prior_age = True
            log.info("heartbeat_watchdog_start", component=self._component, prior_heartbeat_age_seconds=age,
                     grace_seconds=self._threshold)

        elapsed_since_start = (now - self._started_at).total_seconds()
        if 0.0 <= elapsed_since_start < self._threshold:
            return WatchdogResult(False, age, market_open=True, reason="startup_grace")

        if age is None or age > self._threshold:
            self._fresh_streak = 0
            self._escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT, commanded_by="heartbeat_watchdog")
            self._notifier.notify({"kind": "heartbeat_stale", "severity": "critical",
                                   "message": f"{self._component} heartbeat stale",
                                   "detail": {"age_seconds": age, "threshold_seconds": self._threshold, "component": self._component}})
            return WatchdogResult(True, age, market_open=True, reason="stale_heartbeat")

        status = read_latest_heartbeat_status(self._component, self._db_path)
        status_dict = status[1] if status else {}
        cycle_age = None
        lct_raw = status_dict.get("last_cycle_ts")
        if lct_raw:
            try:
                lct = datetime.fromisoformat(str(lct_raw))
                if lct.tzinfo is None:
                    lct = lct.replace(tzinfo=timezone.utc)
                cycle_age = (now - lct).total_seconds()
            except (ValueError, TypeError):
                cycle_age = None

        def _resolve() -> None:
            resolve = getattr(self._notifier, "resolve", None)
            if callable(resolve):
                try:
                    resolve(component=self._component, kind="heartbeat_stale")
                except Exception:
                    log.warning("heartbeat_resolve_notify_failed", component=self._component, exc_info=True)

        # (a) DEAD ENGINE: emitter alive (fresh beat) but consumption last_cycle_ts stale -> CRITICAL.
        if cycle_age is not None and cycle_age > self._threshold:
            self._fresh_streak = 0
            self._escalation.transition(RiskEscalationLevel.ENTRY_GATE_HALT, commanded_by="heartbeat_watchdog")
            self._notifier.notify({"kind": "heartbeat_stale", "severity": "critical",
                                   "message": f"{self._component} consumption stalled (heartbeat task alive)",
                                   "detail": {"cycle_age_seconds": cycle_age, "threshold_seconds": self._threshold, "component": self._component}})
            return WatchdogResult(True, age, market_open=True, reason="engine_cycle_stale")

        # (b) HALTED-BY-DESIGN: fresh beat + current cycle but gate BLOCKED -> WARNING, no escalation.
        if str(status_dict.get("gate", "")).upper() == "BLOCKED":
            self._fresh_streak = 0
            _resolve()
            self._notifier.notify({"kind": "gate_blocked", "severity": "warning",
                                   "message": f"{self._component} halted by design ({status_dict.get('gate_reason', '')})",
                                   "detail": {"gate_reason": status_dict.get("gate_reason", ""),
                                              "escalation_level": status_dict.get("escalation_level"), "component": self._component}})
            return WatchdogResult(False, age, market_open=True, reason="gate_blocked")

        # (c) Healthy: resolve + bounded auto-de-escalation of a watchdog-commanded ENTRY_GATE_HALT only.
        _resolve()
        self._fresh_streak += 1
        snapshot_fn = getattr(self._escalation, "snapshot", None)
        if self._allow_deesc and callable(snapshot_fn) and self._fresh_streak >= self._deesc_streak_required:
            snap = snapshot_fn()
            if (snap.level == RiskEscalationLevel.ENTRY_GATE_HALT and snap.commanded_by == "heartbeat_watchdog"
                    and not snap.portfolio_liquidation_commanded and not snap.engine_shutdown_latched):
                self._escalation.transition(RiskEscalationLevel.NOMINAL, commanded_by="heartbeat_watchdog_recovery")
                log.critical("heartbeat_watchdog_deescalation", fresh_streak=self._fresh_streak,
                             component=self._component, commanded_by="heartbeat_watchdog_recovery")
                self._fresh_streak = 0
        return WatchdogResult(False, age, market_open=True, reason="healthy")

    async def run(self, *, poll_interval_seconds: float = 30.0, iterations: int | None = None):
        count = 0
        while iterations is None or count < iterations:
            self.check()
            count += 1
            if iterations is not None and count >= iterations:
                break
            await asyncio.sleep(poll_interval_seconds)
