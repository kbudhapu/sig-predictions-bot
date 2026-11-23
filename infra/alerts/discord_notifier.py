"""Discord webhook alert channel — PORTED from mbappe src/alerts/discord_notifier.py.

*** THE SILENT-BADGE TRAP (mbappe AI1) — get this wrong and the channel is decorative: *** a plain
Discord webhook message produces a SILENT UNREAD BADGE, not a notification. Detection works and
delivery reaches nobody — mbappe's July-7 failure shape. Two things are therefore mandatory on an
alert that must actually wake someone:

  1. The mention goes in the top-level ``content`` field. An EMBED CANNOT TRIGGER A MENTION — putting
     "@here" inside embed text renders the literal characters and notifies no one. So the embed here
     is strictly ADDITIVE (colour, title, fields); it never replaces the content line.
  2. ``allowed_mentions: {"parse": ["everyone"]}`` must be set, or Discord silently strips the @here
     and you are back to a silent badge with no error to tell you.

SECURITY — the webhook URL is a CREDENTIAL (anyone holding it can post FAKE alerts into the operator's
dead-man channel). It is redacted from every log line and every error string, because an HTTP client
exception happily prints the full request URL.

MONEY FENCE: reuses notifiers.py's forbidden-key strip and dollar-amount redaction. One transport,
one rule — a stray balance must not reach a chat channel any more than a public ntfy topic.

DIVERGENCE FROM MBAPPE (deliberate): mbappe's Discord leg is PRIORITY-ONLY, because there it is a
third redundant path behind ntfy and its silence has to mean something. In sig-bot Discord is the
operator's PRIMARY channel, so it carries everything at or above ``min_severity`` (default
``warning``) and reserves the @here mention for error-and-above. A warning still earns a badge; only
error/urgent/critical/high actually ring the phone.

Boot-safety: never raises, self-disables without DISCORD_WEBHOOK_URL, opens no connection at
construction, and the durable ``operator_alerts`` row (LogNotifier) is never contingent on it.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import structlog

from infra.alerts.notifiers import _MONEY_RE, _forbidden, build_ntfy_payload
from infra.alerts.outbound_cap import _is_priority
from infra.alerts.send_interlock import log_blocked_once, running_under_test

log = structlog.get_logger()

#: (webhook_url, payload) -> None. Injectable so tests never touch the network.
DiscordPostFn = Callable[[str, dict], None]

#: Matches a Discord webhook URL anywhere in free text, for belt-and-braces redaction.
_WEBHOOK_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+")

#: Per-incident cooldown. Matches mbappe's DEFAULT_PAGE_COOLDOWN_SECONDS (300s) so a flapping
#: condition cannot spam the channel. This is INDEPENDENT of ThrottledNotifier on purpose: alerts
#: reach Discord by two routes (the notifier fan-out AND dispatch_critical_page, which is not
#: wrapped by the throttle), and both must share one cooldown or a flap that trips both sends twice.
DEFAULT_COOLDOWN_SECONDS = 300.0

#: Severity ordering. Anything below `min_severity` is dropped; error-and-above earns the @here.
_SEVERITY_RANK = {
    "debug": 0, "info": 0, "notice": 0, "default": 0,
    "warning": 1, "warn": 1,
    "error": 2,
    "high": 3,
    "urgent": 4,
    "critical": 5,
}
_MENTION_AT_OR_ABOVE = 2        # "error"

#: Discord embed colours (decimal RGB) by severity band.
_SEVERITY_COLOUR = {
    0: 0x3498DB,    # info      - blue
    1: 0xF1C40F,    # warning   - amber
    2: 0xE67E22,    # error     - orange
    3: 0xE74C3C,    # high      - red
    4: 0xC0392B,    # urgent    - dark red
    5: 0x992D22,    # critical  - darkest red
}

# Shared cooldown state. Module-level (not per-instance) so every route into Discord shares it.
_recent_sends: dict[tuple[str, str, str], float] = {}
_recent_sends_lock = threading.Lock()


def configured_cooldown_seconds() -> float:
    """ops.alert_cooldown_seconds from config/sig.yaml, defaulting to mbappe's 300s.

    Unreadable config must never DISABLE the cooldown (that would turn a flap into a spam storm), so
    the fallback is the default rather than zero.
    """
    try:
        from sig.config import load_yaml
        raw = (load_yaml().get("ops") or {}).get("alert_cooldown_seconds")
        return DEFAULT_COOLDOWN_SECONDS if raw is None else max(float(raw), 0.0)
    except Exception as e:
        log.warning("alert_cooldown_config_unreadable", error=str(e),
                    fallback=DEFAULT_COOLDOWN_SECONDS)
        return DEFAULT_COOLDOWN_SECONDS


def reset_cooldowns() -> None:
    """Clear the shared cooldown state. For tests and for a deliberate operator re-page."""
    with _recent_sends_lock:
        _recent_sends.clear()


def severity_rank(severity: object) -> int:
    """Unknown severities rank as `error`, not `info` — an unrecognised label must not be
    silently demoted below the delivery floor and swallowed."""
    return _SEVERITY_RANK.get(str(severity or "").strip().lower(), 2)


def redact_webhook(text: str, url: str = "") -> str:
    """Strip the webhook credential out of any string bound for a log or an exception message."""
    out = text.replace(url, "<DISCORD_WEBHOOK_URL redacted>") if url else text
    return _WEBHOOK_RE.sub("<discord_webhook redacted>", out)


def safe_embed_fields(alert: dict, *, limit: int = 20) -> list[dict]:
    """Embed fields from alert['detail'], through the SAME money fence as the ntfy body.

    Forbidden financial keys are dropped by key, and any dollar-amount that slipped into a value is
    pattern-redacted. Discord caps an embed at 25 fields; we stop well short of it.
    """
    detail = alert.get("detail") or {}
    if not isinstance(detail, dict):
        return []
    fields: list[dict] = []
    for k, v in detail.items():
        if len(fields) >= limit:
            break
        if _forbidden(str(k)):
            continue
        value = _MONEY_RE.sub("[redacted]", str(v))
        fields.append({
            "name": str(k)[:256],
            "value": (value or "-")[:1024],
            "inline": True,
        })
    return fields


def build_discord_payload(alert: dict) -> dict:
    """Render an alert into a Discord webhook JSON body.

    `content` carries the human line and, for error-and-above, the @here that makes Discord actually
    NOTIFY (an embed cannot do this — see the module docstring). The embed carries the severity
    colour, the title and the detail fields.
    """
    title, body, _priority = build_ntfy_payload(alert)       # reuse the money redaction wholesale
    sev_label = str(alert.get("severity", "info")).strip().lower()
    rank = severity_rank(sev_label)

    payload: dict = {
        "embeds": [{
            "title": title[:256],
            "description": body[:4096],
            "color": _SEVERITY_COLOUR.get(rank, _SEVERITY_COLOUR[2]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"sig-bot | {alert.get('kind', 'alert')}"},
            "fields": safe_embed_fields(alert),
        }]
    }

    # The content line is what a phone's push PREVIEW shows (an embed is not rendered there), so it
    # carries the severity, the incident kind and the message itself -- not just a bare title. Note
    # `title` already begins with "[sig SEV]" from build_ntfy_payload, so it is deliberately NOT
    # reused here; doing so prints the severity twice, which is the shape mbappe ships.
    kind = str(alert.get("kind", "alert"))
    headline = f"**[SIG {sev_label.upper()}]** {kind}"
    if body:
        headline += f" — {body}"

    if rank >= _MENTION_AT_OR_ABOVE or _is_priority(alert.get("kind"), alert.get("severity")):
        # @here in CONTENT (never the embed) + allowed_mentions, or Discord drops the mention.
        payload["content"] = f"@here {headline}"[:2000]
        payload["allowed_mentions"] = {"parse": ["everyone"]}
    else:
        payload["content"] = headline[:2000]
        payload["allowed_mentions"] = {"parse": []}           # routine: badge only, no ping

    return payload


class DiscordRateLimited(Exception):
    """Discord returned HTTP 429."""


def _http_post_discord(webhook_url: str, payload: dict, *, timeout: float = 8.0) -> None:
    if running_under_test():
        log_blocked_once("discord")
        return
    import httpx       # lazy: importing a client must never happen at construction
    resp = httpx.post(webhook_url, json=payload, timeout=timeout)
    if resp.status_code == 429:
        raise DiscordRateLimited("discord rate-limited (HTTP 429)")
    if resp.status_code >= 400:
        # NB: no URL in the message — the webhook is a credential.
        raise RuntimeError(f"discord non-2xx (HTTP {resp.status_code})")


class DiscordNotifier:
    """Push alerts to a Discord webhook. Never raises; self-disables without DISCORD_WEBHOOK_URL."""

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        post_fn: DiscordPostFn | None = None,
        min_severity: str = "warning",
        cooldown_seconds: float | None = None,
    ) -> None:
        self._url = webhook_url if webhook_url is not None else os.getenv("DISCORD_WEBHOOK_URL", "")
        self._post = post_fn or _http_post_discord        # injectable; NOT called at construction
        self._min_rank = severity_rank(min_severity)
        self._cooldown = (configured_cooldown_seconds() if cooldown_seconds is None
                          else max(float(cooldown_seconds), 0.0))

    def enabled(self) -> bool:
        return bool(self._url)

    @staticmethod
    def _cooldown_key(alert: dict) -> tuple[str, str, str]:
        detail = alert.get("detail") or {}
        component = str(detail.get("component", "")) if isinstance(detail, dict) else ""
        return (component, str(alert.get("kind", "")), str(alert.get("severity", "")).lower())

    def _cooled_down(self, alert: dict) -> bool:
        """True when this incident was sent too recently. Claims the slot when it returns False."""
        if self._cooldown <= 0.0:
            return False
        key = self._cooldown_key(alert)
        now = time.monotonic()
        with _recent_sends_lock:
            if now - _recent_sends.get(key, -1e9) < self._cooldown:
                return True
            _recent_sends[key] = now
        return False

    def notify(self, alert: dict) -> None:
        if not self.enabled():
            return
        if severity_rank(alert.get("severity")) < self._min_rank:
            return
        if self._post is _http_post_discord and running_under_test():
            log_blocked_once("discord")
            return
        if self._cooled_down(alert):
            log.debug("discord_cooldown_active", kind=alert.get("kind"),
                      severity=alert.get("severity"), cooldown_s=self._cooldown,
                      note="durable operator_alerts row is unaffected")
            return
        try:
            self._post(self._url, build_discord_payload(alert))
        except DiscordRateLimited as e:
            log.warning("discord_rate_limited", error=redact_webhook(str(e), self._url),
                        note="durable operator_alerts row unaffected")
        except Exception as e:          # a push failure must never escape into the trading loop
            log.warning("discord_post_failed", error=redact_webhook(str(e), self._url))
