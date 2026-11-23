"""PORTED from mbappe src/control/alerts.py — on-call incident paging + the in-process dead-man.

Layer 1 of the three-layer dead-man: SystemHeartbeatMonitor is a daemon thread that pages if no clean
cycle completes within 2x the cycle interval. dispatch_critical_page posts a PagerDuty/Opsgenie-compatible
JSON webhook (ALERT_WEBHOOK_URL) off the event loop, with a per-incident cooldown, and ALWAYS writes a
durable CRITICAL_PAGE row first (the log leg never depends on the webhook). Safety actions never depend on
any channel. SIG adaptations: imports -> infra.*; the feed-health telemetry import is best-effort; and
every page ALSO fans out to Discord (infra/alerts/discord_notifier.py) because that is sig-bot's primary
operator channel -- the generic ALERT_WEBHOOK_URL leg is kept unchanged alongside it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from infra.config import DB_PATH
from infra.persistence import db as persistence

log = logging.getLogger(__name__)

DEFAULT_PAGE_COOLDOWN_SECONDS = 300.0
DEAD_MAN_CYCLE_MULTIPLIER = 2


class IncidentType(str, Enum):
    PRE_FLIGHT_RECON_LOCK = "PRE_FLIGHT_RECON_LOCK"
    DEGRADED_FEED_SUSTAINED = "DEGRADED_FEED_SUSTAINED"
    WAL_BACKLOG_CRITICAL = "WAL_BACKLOG_CRITICAL"
    HARD_CRITICAL_DEGRADE = "HARD_CRITICAL_DEGRADE"
    MAINTENANCE_JOB_FAILED = "MAINTENANCE_JOB_FAILED"
    DEAD_MAN_HEARTBEAT = "DEAD_MAN_HEARTBEAT"
    BOOT_BLOCKING_ERROR = "BOOT_BLOCKING_ERROR"
    RATE_LIMIT_LOCKOUT = "RATE_LIMIT_LOCKOUT"       # SIG addition (write-gate / 429 backoff lockout risk)


@dataclass(frozen=True)
class PageDispatchResult:
    delivered: bool
    channel: str
    status_code: int | None
    reason: str
    incident_type: str


_recent_pages: dict[str, float] = {}
_recent_pages_lock = threading.Lock()


def _blocking_webhook_post(request: urllib.request.Request, incident_key: str) -> PageDispatchResult:
    """Synchronous HTTP POST executed inside a thread executor to avoid blocking the event loop."""
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            status = int(response.status)
        return PageDispatchResult(200 <= status < 300, "webhook", status,
                                  "delivered" if 200 <= status < 300 else "non_2xx", incident_key)
    except urllib.error.HTTPError as exc:
        log.error("critical_page_http_error incident=%s code=%s", incident_key, exc.code)
        return PageDispatchResult(False, "webhook", int(exc.code), str(exc.reason), incident_key)
    except urllib.error.URLError as exc:
        log.error("critical_page_url_error incident=%s error=%s", incident_key, exc.reason)
        return PageDispatchResult(False, "webhook", None, f"url_error:{exc.reason}", incident_key)
    except TimeoutError:
        log.error("critical_page_timeout incident=%s", incident_key)
        return PageDispatchResult(False, "webhook", None, "timeout", incident_key)
    except OSError as exc:
        log.error("critical_page_connection_error incident=%s error=%s", incident_key, exc)
        return PageDispatchResult(False, "webhook", None, f"connection_error:{exc}", incident_key)
    except Exception as exc:
        log.error("critical_page_failed incident=%s error=%s", incident_key, exc)
        return PageDispatchResult(False, "webhook", None, str(exc), incident_key)


def _page_to_discord(payload: dict) -> PageDispatchResult:
    """Fan a critical page out to Discord. Blocking, so callers run it in the executor.

    Never raises: DiscordNotifier swallows its own transport errors, and a missing webhook simply
    self-disables. The durable CRITICAL_PAGE row is written before this is ever called, so losing
    this leg loses a notification, never the record.
    """
    incident = str(payload.get("incident_type", "alert"))
    try:
        from infra.alerts.discord_notifier import DiscordNotifier

        notifier = DiscordNotifier()
        if not notifier.enabled():
            return PageDispatchResult(False, "discord", None, "webhook_not_configured", incident)
        notifier.notify({
            "kind": incident,
            "severity": str(payload.get("severity", "critical")),
            "message": str(payload.get("message", "")),
            # component keys the shared Discord cooldown; the incident type IS the component here.
            "detail": {"component": incident, **dict(payload.get("metadata") or {})},
        })
        return PageDispatchResult(True, "discord", None, "dispatched", incident)
    except Exception as exc:        # a channel must never break the page path
        log.error("critical_page_discord_failed incident=%s error=%s", incident, exc)
        return PageDispatchResult(False, "discord", None, f"discord_error:{exc}", incident)


async def dispatch_critical_page(incident_type: str | IncidentType, message: str,
                                 metadata: Mapping[str, Any] | None = None, *,
                                 cooldown_seconds: float = DEFAULT_PAGE_COOLDOWN_SECONDS) -> PageDispatchResult:
    """Dispatch a critical incident page via webhook (off the event loop).

    Writes a durable CRITICAL_PAGE row first, so the incident is recorded even if every push fails.
    That write is best-effort: if it fails the push legs still run, because a page that reaches
    nobody is a worse failure than a page that is not filed."""
    incident_key = incident_type.value if isinstance(incident_type, IncidentType) else str(incident_type)
    now = time.monotonic()
    with _recent_pages_lock:
        if now - _recent_pages.get(incident_key, 0.0) < max(float(cooldown_seconds), 0.0):
            return PageDispatchResult(False, "suppressed", None, "cooldown_active", incident_key)
        _recent_pages[incident_key] = now

    payload = {"incident_type": incident_key, "severity": "critical", "message": message,
               "timestamp": datetime.now(timezone.utc).isoformat(), "metadata": dict(metadata or {}),
               "source": "sig_bot_alerts"}
    # The durable row is written FIRST so the incident is recorded even if every push fails — but a
    # persistence failure must NOT abort the page. It previously did: an uninitialised trading.db
    # raised out of here, so the Discord and webhook legs below never ran and the page vanished
    # entirely. Losing the record is bad; losing the record AND the notification is how an incident
    # goes unnoticed, so the push is now strictly downstream of this write succeeding or failing.
    try:
        persistence.log_system_event("CRITICAL_PAGE", json.dumps(payload, separators=(",", ":")),
                                     severity="critical")
    except Exception as exc:
        log.error("critical_page_durable_row_failed incident=%s error=%s "
                  "(continuing to the push legs)", incident_key, exc)

    loop = asyncio.get_running_loop()
    # Discord leg FIRST (sig-bot's primary operator channel), off the event loop. Independent of the
    # generic webhook: either can be configured without the other, and neither can break the other.
    discord = await loop.run_in_executor(None, _page_to_discord, payload)

    webhook_url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.critical("CRITICAL_PAGE %s: %s metadata=%s", incident_key, message, metadata)
        # Report the Discord delivery rather than "log_only" -- an operator reading log_only would
        # conclude nothing was paged, which would be false whenever Discord alone is configured.
        if discord.delivered:
            return discord
        return PageDispatchResult(False, "log_only", None, "webhook_not_configured", incident_key)

    headers = {"Content-Type": "application/json"}
    token = os.getenv("ALERT_WEBHOOK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(webhook_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    result = await loop.run_in_executor(None, _blocking_webhook_post, request, incident_key)
    if not result.delivered and discord.delivered:
        # The generic webhook failed but the operator WAS reached. Say so.
        return PageDispatchResult(True, "discord", result.status_code,
                                  f"discord_delivered;webhook_{result.reason}", incident_key)
    return result


def record_successful_cycle_heartbeat(*, db_path: Any = DB_PATH, environment: str = "paper") -> str:
    """Persist the local heartbeat timestamp (the dead-man's read target)."""
    iso_ts = datetime.now(timezone.utc).isoformat()
    feed_health = None
    try:
        from sig.feed_stream_health import build_feed_health_payload  # best-effort SIG telemetry (optional)
        feed_health = build_feed_health_payload()
    except Exception:
        pass
    persistence.set_engine_heartbeat_timestamp(iso_ts, db_path=db_path, feed_health=feed_health)
    return iso_ts


def heartbeat_age_seconds(*, db_path: Any = DB_PATH) -> float | None:
    last = persistence.get_engine_heartbeat_timestamp(db_path=db_path)
    if last is None:
        return None
    try:
        parsed = datetime.fromisoformat(last)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max((datetime.now(timezone.utc) - parsed).total_seconds(), 0.0)


class SystemHeartbeatMonitor:
    """Background dead-man switch: pages if no clean cycle completes within 2x cycle interval."""

    def __init__(self, *, cycle_interval_seconds: float, db_path: Any = DB_PATH, environment: str = "paper",
                 check_interval_seconds: float = 30.0, none_stale_after_seconds: float | None = None) -> None:
        self.cycle_interval_seconds = max(float(cycle_interval_seconds), 1.0)
        self.db_path = db_path
        self.environment = environment
        self.check_interval_seconds = max(float(check_interval_seconds), 1.0)
        self._none_stale_after_seconds = (float(none_stale_after_seconds) if none_stale_after_seconds is not None
                                          else 3.0 * self.cycle_interval_seconds)
        self._started_at = time.monotonic()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, name="system-heartbeat-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def record_successful_cycle(self) -> str:
        return record_successful_cycle_heartbeat(db_path=self.db_path, environment=self.environment)

    def evaluate_dead_man_once(self) -> bool:
        """Run a single dead-man evaluation (used by the monitor loop and tests)."""
        if not self.is_dead_man_stale():
            return False
        age = heartbeat_age_seconds(db_path=self.db_path)
        threshold = self.cycle_interval_seconds * DEAD_MAN_CYCLE_MULTIPLIER
        age_desc = f"{age:.0f}s" if age is not None else "never written"
        asyncio.run(dispatch_critical_page(
            IncidentType.DEAD_MAN_HEARTBEAT,
            f"Engine heartbeat stale: {age_desc} (threshold {threshold:.0f}s)",
            {"heartbeat_age_seconds": age, "threshold_seconds": threshold, "environment": self.environment}))
        return True

    def is_dead_man_stale(self) -> bool:
        age = heartbeat_age_seconds(db_path=self.db_path)
        if age is None:
            return time.monotonic() - self._started_at > self._none_stale_after_seconds
        return age > self.cycle_interval_seconds * DEAD_MAN_CYCLE_MULTIPLIER

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.check_interval_seconds):
            self.evaluate_dead_man_once()
