"""PORTED/consolidated from mbappe src/alerts/* + src/control/heartbeat_watchdog.py — the notifier set.

Keeps the named safety properties:
- LogNotifier is the durable leg (operator_alerts row) and is NEVER throttled — every alert lands.
- NtfyNotifier is phone push over a PUBLIC topic, so a STRUCTURAL money-redaction fence strips financial
  keys from detail and redacts dollar-amount patterns from the body (SIG prices are 0.005–0.995 contracts
  — still redacted, conservative). Guarded by the shared OutboundCap (reserved priority lane) + the test
  send-interlock. Never raises; self-disables without NTFY_TOPIC.
- CompositeNotifier fans out; one channel failing never blocks the others and none can raise into the loop.
- build_log_and_push ships CompositeNotifier(LogNotifier, ThrottledNotifier(ntfy)); add Telegram/Discord/
  email channels inside the throttle the same way mbappe does (independent off-Firebase paths) when wanted.
Safety actions NEVER depend on any channel.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import structlog

from infra.alerts.send_interlock import log_blocked_once, running_under_test
from infra.persistence.db import persist_alert

log = structlog.get_logger()

NTFY_BASE = "https://ntfy.sh"


@runtime_checkable
class Notifier(Protocol):
    def notify(self, alert: dict) -> None: ...


class LogNotifier:
    """Durable notifier: structured log line AND an append to operator_alerts. Never throttled."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = db_path

    def notify(self, alert: dict) -> None:
        log.critical("operator_alert", **{k: v for k, v in alert.items() if k != "detail"}, detail=alert.get("detail", {}))
        persist_alert(alert, self._db_path)


# ── ntfy phone push + the STRUCTURAL money-redaction fence ──
class NtfyRateLimited(Exception):
    """ntfy.sh returned HTTP 429 (the only signal — httpx.post does not raise on 429)."""


_FORBIDDEN_KEY_SUBSTR = ("balance", "equity", "pnl", "position", "price", "qty", "quantity", "symbol",
                         "notional", "cash", "api_key", "secret", "token", "account", "fill", "order", "notion")
_MONEY_RE = re.compile(r"[-+]?\$\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*\.\d{2}\b")

PostFn = Callable[[str, str, str, str], None]


def _forbidden(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _FORBIDDEN_KEY_SUBSTR)


def build_ntfy_payload(alert: dict) -> tuple[str, str, str]:
    """Return (title, body, priority). SECURITY: forbidden financial keys stripped + dollar-amounts
    redacted — structurally, so a stray money value can never reach the public topic."""
    sev = str(alert.get("severity", "info")).upper()
    title = f"[sig {sev}] {alert.get('kind', 'alert')}"[:120]
    safe_detail = {k: v for k, v in (alert.get("detail") or {}).items() if not _forbidden(str(k))}
    body = str(alert.get("message", ""))
    if safe_detail:
        body += " | " + ", ".join(f"{k}={v}" for k, v in safe_detail.items())
    body = _MONEY_RE.sub("[redacted]", body)
    priority = "urgent" if sev in {"CRITICAL", "ERROR"} else "default"
    return title, body[:400], priority


def _http_post_ntfy(topic: str, title: str, body: str, priority: str, *, timeout: float = 5.0) -> None:
    if running_under_test():
        log_blocked_once("ntfy")
        return
    import httpx
    resp = httpx.post(f"{NTFY_BASE}/{topic}", content=body.encode("utf-8"),
                      headers={"Title": title, "Priority": priority}, timeout=timeout)
    if resp.status_code == 429:
        raise NtfyRateLimited("ntfy rate-limited (HTTP 429)")
    if resp.status_code >= 400:
        raise RuntimeError(f"ntfy non-2xx (HTTP {resp.status_code})")


class NtfyNotifier:
    """Push liveness alerts to ntfy.sh/{topic}. Never raises; self-disables without NTFY_TOPIC."""

    def __init__(self, *, topic: str | None = None, post_fn: PostFn | None = None, cap: object | None = None) -> None:
        self._topic = topic if topic is not None else os.getenv("NTFY_TOPIC", "")
        self._post = post_fn or _http_post_ntfy
        self._cap = cap
        self._cap_resolved = cap is not None

    def _get_cap(self):
        if self._cap is None and not self._cap_resolved:
            self._cap_resolved = True
            try:
                from infra.alerts.outbound_cap import OutboundCap
                self._cap = OutboundCap()
            except Exception as e:
                log.warning("outbound_cap_unavailable", error=str(e))
                self._cap = None
        return self._cap

    def enabled(self) -> bool:
        return bool(self._topic)

    def notify(self, alert: dict) -> None:
        if not self.enabled():
            return
        if self._post is _http_post_ntfy and running_under_test():
            log_blocked_once("ntfy")
            return
        cap = self._get_cap()
        if cap is not None:
            allowed, reason = cap.allow(alert.get("kind"), alert.get("severity"))
            if not allowed:
                st = cap.stats()
                log.warning("ntfy_cap_exceeded", kind=alert.get("kind"), reason=reason,
                            sends_last_hour=st.get("sends_last_hour"), sends_today=st.get("sends_today"),
                            last_429_at=st.get("last_429_at"),
                            note="suppressed from the phone; durable operator_alerts row is unaffected")
                return
        try:
            title, body, priority = build_ntfy_payload(alert)
            self._post(self._topic, title, body, priority)
        except NtfyRateLimited as e:
            if cap is not None:
                cap.note_429(alert.get("kind"))
            log.error("ntfy_channel_incident_429", error=str(e),
                      note="routine lane backs off 1h; durable operator_alerts row is unaffected")
        except Exception as e:
            log.warning("ntfy_post_failed", error=str(e))


class CompositeNotifier:
    """Fan an alert out to several notifiers. One channel failing never blocks the others; none raise."""

    def __init__(self, *notifiers: Notifier) -> None:
        self._notifiers = notifiers

    def notify(self, alert: dict) -> None:
        for n in self._notifiers:
            try:
                n.notify(alert)
            except Exception as e:
                log.error("composite_notifier_channel_failed", channel=type(n).__name__, error=str(e))

    def resolve(self, *, component: str, kind: str) -> None:
        for n in self._notifiers:
            fn = getattr(n, "resolve", None)
            if callable(fn):
                try:
                    fn(component=component, kind=kind)
                except Exception as e:
                    log.error("composite_notifier_resolve_failed", channel=type(n).__name__, error=str(e))


#: Which push channels build_log_and_push may construct, if config/sig.yaml asks for them. Keyed by
#: the name used in ops.alert_channels. "webhook" is the generic PagerDuty/Opsgenie leg and lives in
#: infra/control/alerts.py (dispatch_critical_page), not here, so it has no builder.
_PUSH_CHANNELS = ("ntfy", "discord")


def _configured_channels() -> tuple[str, ...]:
    """ops.alert_channels from config/sig.yaml, lowercased. Falls back to all known push channels.

    Before this, ops.alert_channels was declarative only — nothing read it, so a channel could be
    listed and silently not exist (or exist and not be listed). It now actually selects the push legs,
    which is the only way the config line can be trusted.
    """
    try:
        from sig.config import load_yaml
        raw = (load_yaml().get("ops") or {}).get("alert_channels") or []
        names = tuple(str(c).strip().lower() for c in raw)
        return names or _PUSH_CHANNELS
    except Exception as e:      # config unreadable must never disable alerting
        log.warning("alert_channels_config_unreadable", error=str(e), fallback="all push channels")
        return _PUSH_CHANNELS


def build_log_and_push(db_path: str | Path, *, channels: tuple[str, ...] | None = None) -> CompositeNotifier:
    """The shipped notifier: CompositeNotifier(LogNotifier, ThrottledNotifier(ntfy, discord, ...)).

    LogNotifier is never throttled (the durable operator_alerts row ALWAYS lands, whatever the push
    channels do); the push legs are deduped + exponentially backed off together. Which push legs get
    built is read from ops.alert_channels in config/sig.yaml, and each leg additionally self-disables
    when its own secret is absent — so a listed-but-unconfigured channel degrades to silence rather
    than to a boot error.
    """
    from infra.alerts.throttle import ThrottledNotifier
    wanted = channels if channels is not None else _configured_channels()
    log_notifier = LogNotifier(db_path)

    push: list[Notifier] = []
    if "ntfy" in wanted:
        ntfy = NtfyNotifier()
        if ntfy.enabled():
            push.append(ntfy)
        else:
            log.warning("alert_channel_unconfigured", channel="ntfy", reason="no NTFY_TOPIC")
    if "discord" in wanted:
        from infra.alerts.discord_notifier import DiscordNotifier
        discord = DiscordNotifier()
        if discord.enabled():
            push.append(discord)
        else:
            log.warning("alert_channel_unconfigured", channel="discord", reason="no DISCORD_WEBHOOK_URL")

    if not push:
        log.warning("push_alerting_disabled", configured=list(wanted),
                    fallback="LogNotifier durable row only")
    log.info("alert_channels_active", channels=[type(n).__name__ for n in push])
    throttled_push = ThrottledNotifier(CompositeNotifier(*push))
    return CompositeNotifier(log_notifier, throttled_push)
