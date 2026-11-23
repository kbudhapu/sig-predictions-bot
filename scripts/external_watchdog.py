"""PORTED from mbappe scripts/external_watchdog.py — dead-man LAYER 3b (out-of-process).

Runs independently of the bot, fired by sig-bot-watchdog.timer every 5 min. Imports NO orchestrator code;
only reads the SQLite heartbeat and calls the alert dispatcher. Catches the failure the in-process monitor
cannot: a crash at startup (before the monitor thread is spawned) means the heartbeat is never written and
the in-process dead-man never fires. Grace logic reads systemd InactiveExitTimestamp so a fresh deploy is
not paged. SIG adaptations: imports -> infra.*; SERVICE_NAME -> sig-bot.service.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.config import DB_PATH
from infra.control.alerts import DEAD_MAN_CYCLE_MULTIPLIER, IncidentType, dispatch_critical_page, heartbeat_age_seconds

log = logging.getLogger("external_watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TICK_SECONDS = 60
STALE_THRESHOLD_SECONDS = TICK_SECONDS * DEAD_MAN_CYCLE_MULTIPLIER   # 120 s
WATCHDOG_INTERVAL_SECONDS = 300
GRACE_SECONDS = WATCHDOG_INTERVAL_SECONDS
FALLBACK_GRACE_SECONDS = GRACE_SECONDS
SERVICE_NAME = "sig-bot.service"


def _service_inactive_exit_seconds_ago() -> float | None:
    try:
        result = subprocess.run(["systemctl", "show", SERVICE_NAME, "--property=InactiveExitTimestamp"],
                                capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("InactiveExitTimestamp="):
            ts_str = line.split("=", 1)[1].strip()
            if not ts_str or ts_str == "n/a":
                return None
            try:
                ts = datetime.strptime(ts_str, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                return max((datetime.now(timezone.utc) - ts).total_seconds(), 0.0)
            except ValueError:
                return None
    return None


def _is_stale(db_path: Path) -> tuple[bool, str]:
    age = heartbeat_age_seconds(db_path=db_path)
    if age is not None:
        if age > STALE_THRESHOLD_SECONDS:
            return True, f"heartbeat age {age:.0f}s exceeds threshold {STALE_THRESHOLD_SECONDS:.0f}s"
        return False, f"heartbeat age {age:.0f}s within threshold"
    service_age = _service_inactive_exit_seconds_ago()
    if service_age is None:
        log.warning("systemd not available; using fallback grace of %ds for None-heartbeat check", FALLBACK_GRACE_SECONDS)
        service_age = FALLBACK_GRACE_SECONDS + 1
    if service_age > GRACE_SECONDS:
        return True, f"heartbeat never written; service up {service_age:.0f}s (grace {GRACE_SECONDS:.0f}s)"
    return False, f"heartbeat never written but within grace (service age {service_age:.0f}s / grace {GRACE_SECONDS:.0f}s)"


def main() -> int:
    db_path = DB_PATH
    log.info("watchdog_check db_path=%s", db_path)
    stale, reason = _is_stale(db_path)
    if not stale:
        log.info("watchdog_ok reason=%s", reason)
        return 0
    log.warning("watchdog_stale reason=%s", reason)
    result = asyncio.run(dispatch_critical_page(
        IncidentType.DEAD_MAN_HEARTBEAT, f"[external watchdog] {reason}",
        {"watchdog": "external_watchdog.py", "db_path": str(db_path),
         "stale_threshold_seconds": STALE_THRESHOLD_SECONDS, "grace_seconds": GRACE_SECONDS,
         "environment": os.getenv("ENVIRONMENT", "paper")},
        cooldown_seconds=WATCHDOG_INTERVAL_SECONDS))
    log.info("watchdog_page_dispatched delivered=%s channel=%s reason=%s", result.delivered, result.channel, result.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
