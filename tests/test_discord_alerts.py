"""Discord alert channel — payload shape, the silent-badge guards, the money fence, the cooldown.

Every test here MOCKS the transport (post_fn injection). Nothing in this file may touch the network:
the send-interlock in infra/alerts/send_interlock.py blocks the real transport under pytest anyway,
but injecting a mock is what lets us assert on the exact bytes we would have sent.
"""
from __future__ import annotations

import asyncio

import pytest

from infra.alerts.discord_notifier import (
    DEFAULT_COOLDOWN_SECONDS,
    DiscordNotifier,
    DiscordRateLimited,
    build_discord_payload,
    configured_cooldown_seconds,
    redact_webhook,
    reset_cooldowns,
    safe_embed_fields,
    severity_rank,
)

FAKE_URL = "https://discord.com/api/webhooks/123456789/abcdefGHIJKLmnop-SECRET-TOKEN"


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    """The cooldown store is module-level (shared by every route into Discord), so it must be reset
    between tests or the second test to page the same incident silently gets suppressed."""
    reset_cooldowns()
    yield
    reset_cooldowns()


class Recorder:
    """Mock transport: records (url, payload) instead of posting."""

    def __init__(self, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_exc

    def __call__(self, url: str, payload: dict) -> None:
        self.calls.append((url, payload))
        if self._raise is not None:
            raise self._raise


def _notifier(rec: Recorder, **kw) -> DiscordNotifier:
    kw.setdefault("cooldown_seconds", 0.0)      # most tests are not about the cooldown
    return DiscordNotifier(webhook_url=FAKE_URL, post_fn=rec, **kw)


def alert(kind="heartbeat_stale", severity="critical", message="Engine heartbeat stale: 180s",
          detail=None) -> dict:
    return {"kind": kind, "severity": severity, "message": message,
            "detail": detail if detail is not None else {"component": "orchestrator", "age_s": 180}}


# --------------------------------------------------------------------------- #
# Payload shape — REQUIRED: the dispatch formats a Discord payload correctly
# --------------------------------------------------------------------------- #
def test_payload_is_valid_discord_json_shape():
    p = build_discord_payload(alert())
    assert set(p) == {"content", "embeds", "allowed_mentions"}
    assert isinstance(p["content"], str) and p["content"]
    assert isinstance(p["embeds"], list) and len(p["embeds"]) == 1
    e = p["embeds"][0]
    # the keys Discord actually accepts on an embed
    assert set(e) <= {"title", "description", "color", "timestamp", "footer", "fields"}
    assert isinstance(e["color"], int)
    assert isinstance(e["fields"], list)
    assert e["footer"]["text"].startswith("sig-bot | ")


def test_incident_structure_survives_into_the_embed():
    """mbappe's incident structure is (type, severity, message, timestamp) — all four must land."""
    p = build_discord_payload(alert(kind="DEAD_MAN_HEARTBEAT", severity="critical",
                                    message="Engine heartbeat stale: 180s (threshold 120s)"))
    e = p["embeds"][0]
    assert "DEAD_MAN_HEARTBEAT" in e["title"]            # type
    assert "CRITICAL" in e["title"]                      # severity
    assert "heartbeat stale" in e["description"]         # message
    assert e["timestamp"].endswith("+00:00")             # timestamp, UTC-aware
    assert "DEAD_MAN_HEARTBEAT" in e["footer"]["text"]


def test_content_line_carries_the_message_for_the_push_preview():
    """A phone push preview renders `content` only — never the embed. So the content line must hold
    the substance, and must not print the severity twice (build_ntfy_payload already prefixes it)."""
    p = build_discord_payload(alert(kind="DEAD_MAN_HEARTBEAT", severity="critical",
                                    message="Engine heartbeat stale: 180s"))
    c = p["content"]
    assert "DEAD_MAN_HEARTBEAT" in c
    assert "Engine heartbeat stale: 180s" in c, "preview must show the message, not just the kind"
    assert c.count("CRITICAL") == 1, f"severity printed twice: {c!r}"
    assert "[sig " not in c, "the ntfy title prefix must not be reused in the content line"


def test_severity_drives_the_embed_colour():
    colours = {sev: build_discord_payload(alert(severity=sev))["embeds"][0]["color"]
               for sev in ("info", "warning", "error", "high", "urgent", "critical")}
    assert len(set(colours.values())) == 6, "each severity band needs a distinguishable colour"
    assert colours["info"] == 0x3498DB
    assert colours["critical"] == 0x992D22


def test_detail_becomes_embed_fields():
    p = build_discord_payload(alert(detail={"component": "orchestrator", "streak": 3,
                                            "environment": "sim"}))
    fields = {f["name"]: f["value"] for f in p["embeds"][0]["fields"]}
    assert fields == {"component": "orchestrator", "streak": "3", "environment": "sim"}


def test_embed_fields_stay_within_discord_limits():
    big = {f"k{i}": "v" * 5000 for i in range(50)}
    fields = safe_embed_fields(alert(detail=big))
    assert len(fields) <= 25, "Discord rejects an embed with more than 25 fields"
    assert all(len(f["value"]) <= 1024 for f in fields)
    assert all(len(f["name"]) <= 256 for f in fields)


# --------------------------------------------------------------------------- #
# The SILENT-BADGE TRAP (mbappe AI1) — the whole point of the channel
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("severity", ["error", "high", "urgent", "critical"])
def test_priority_alert_mentions_in_CONTENT_and_allows_the_mention(severity):
    p = build_discord_payload(alert(severity=severity))
    # (a) the mention is in content, NOT the embed — an embed cannot trigger a notification
    assert p["content"].startswith("@here")
    # (b) allowed_mentions must permit it or Discord silently strips the @here
    assert p["allowed_mentions"] == {"parse": ["everyone"]}
    # (c) and it must NOT be hiding in the embed instead
    e = p["embeds"][0]
    assert "@here" not in e["title"] and "@here" not in e["description"]


@pytest.mark.parametrize("severity", ["info", "warning"])
def test_routine_alert_does_not_ping(severity):
    p = build_discord_payload(alert(severity=severity))
    assert "@here" not in p["content"]
    assert p["allowed_mentions"] == {"parse": []}, "routine must not be able to ping"


def test_unknown_severity_is_treated_as_error_not_as_info():
    """An unrecognised label must not be silently demoted below the delivery floor and swallowed."""
    assert severity_rank("banana") == severity_rank("error")
    p = build_discord_payload(alert(severity="banana"))
    assert p["content"].startswith("@here")


# --------------------------------------------------------------------------- #
# Money fence + credential redaction
# --------------------------------------------------------------------------- #
def test_financial_keys_and_dollar_amounts_never_reach_discord():
    a = alert(message="drawdown breached at $12,500.00 equity",
              detail={"component": "risk", "balance": 88000.0, "pnl": -1200.0,
                      "position_qty": 10, "streak": 3, "note": "lost 1,250.00"})
    p = build_discord_payload(a)
    blob = repr(p)
    for forbidden in ("88000", "balance", "pnl", "position_qty", "12,500.00", "1,250.00"):
        assert forbidden not in blob, f"{forbidden!r} leaked into the Discord payload"
    assert "[redacted]" in p["embeds"][0]["description"]
    assert "streak" in blob, "non-financial detail must still get through"


def test_webhook_url_is_redacted_from_text():
    msg = f"POST {FAKE_URL} failed with 500"
    out = redact_webhook(msg, FAKE_URL)
    assert "SECRET-TOKEN" not in out
    assert FAKE_URL not in out
    # even without being told the URL, the pattern is caught
    assert "SECRET-TOKEN" not in redact_webhook(msg)


def test_transport_failure_never_raises_and_never_logs_the_url(caplog):
    """An HTTP client exception happily prints the full request URL — it must not escape."""
    rec = Recorder(raise_exc=RuntimeError(f"connection to {FAKE_URL} refused"))
    _notifier(rec).notify(alert())              # must not raise
    assert "SECRET-TOKEN" not in caplog.text


def test_rate_limit_is_swallowed():
    rec = Recorder(raise_exc=DiscordRateLimited("discord rate-limited (HTTP 429)"))
    _notifier(rec).notify(alert())              # must not raise


# --------------------------------------------------------------------------- #
# Cooldown (mbappe parity: 300s per incident)
# --------------------------------------------------------------------------- #
def test_cooldown_default_matches_mbappe_300s():
    assert DEFAULT_COOLDOWN_SECONDS == 300.0
    assert configured_cooldown_seconds() == 300.0        # and config/sig.yaml agrees


def test_flapping_condition_sends_once_within_the_cooldown():
    rec = Recorder()
    n = DiscordNotifier(webhook_url=FAKE_URL, post_fn=rec, cooldown_seconds=300.0)
    for _ in range(25):
        n.notify(alert())
    assert len(rec.calls) == 1, "a flapping condition must not spam the channel"


def test_cooldown_is_per_incident_not_global():
    rec = Recorder()
    n = DiscordNotifier(webhook_url=FAKE_URL, post_fn=rec, cooldown_seconds=300.0)
    n.notify(alert(kind="heartbeat_stale"))
    n.notify(alert(kind="halt_but_hold"))                # different incident -> must still get out
    n.notify(alert(kind="heartbeat_stale"))              # repeat -> suppressed
    assert [p["embeds"][0]["footer"]["text"] for _, p in rec.calls] == [
        "sig-bot | heartbeat_stale", "sig-bot | halt_but_hold"]


def test_cooldown_is_shared_across_notifier_instances():
    """Two routes reach Discord (the notifier fan-out AND dispatch_critical_page), each building its
    own notifier. A per-instance cooldown would let one flap page twice."""
    rec = Recorder()
    DiscordNotifier(webhook_url=FAKE_URL, post_fn=rec, cooldown_seconds=300.0).notify(alert())
    DiscordNotifier(webhook_url=FAKE_URL, post_fn=rec, cooldown_seconds=300.0).notify(alert())
    assert len(rec.calls) == 1


# --------------------------------------------------------------------------- #
# Boot safety / gating
# --------------------------------------------------------------------------- #
def test_self_disables_without_a_webhook_url():
    rec = Recorder()
    n = DiscordNotifier(webhook_url="", post_fn=rec)
    assert not n.enabled()
    n.notify(alert())
    assert rec.calls == [], "no webhook configured must mean silence, not a crash"


def test_below_min_severity_is_dropped():
    rec = Recorder()
    n = _notifier(rec, min_severity="warning")
    n.notify(alert(severity="info"))
    assert rec.calls == []
    n.notify(alert(severity="warning"))
    assert len(rec.calls) == 1


def test_posts_to_the_configured_url():
    rec = Recorder()
    _notifier(rec).notify(alert())
    assert rec.calls[0][0] == FAKE_URL


# --------------------------------------------------------------------------- #
# Wiring: the triggers that must reach Discord
# --------------------------------------------------------------------------- #
def test_build_log_and_push_includes_discord_when_configured(monkeypatch, tmp_path):
    from infra.alerts import notifiers

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_URL)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    composite = notifiers.build_log_and_push(tmp_path / "t.db", channels=("ntfy", "discord"))
    names = _channel_names(composite)
    assert "DiscordNotifier" in names
    assert "LogNotifier" in names, "the durable leg must never be displaced by a push channel"


def test_channel_not_listed_in_config_is_not_built(monkeypatch, tmp_path):
    from infra.alerts import notifiers

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_URL)
    composite = notifiers.build_log_and_push(tmp_path / "t.db", channels=("ntfy",))
    assert "DiscordNotifier" not in _channel_names(composite)


def _channel_names(notifier) -> set[str]:
    """Walk the composite/throttle nesting and collect the leaf channel class names."""
    found: set[str] = set()
    stack = [notifier]
    while stack:
        n = stack.pop()
        found.add(type(n).__name__)
        stack.extend(getattr(n, "_notifiers", ()) or ())
        inner = getattr(n, "_inner", None)
        if inner is not None:
            stack.append(inner)
    return found


def test_dead_man_page_reaches_discord(monkeypatch):
    """The dead-man heartbeat is the alert that matters most: it must come out as a Discord payload
    with the @here, not merely a durable row."""
    import infra.alerts.discord_notifier as dn
    import infra.control.alerts as alerts

    rec = Recorder()
    monkeypatch.setattr(alerts.persistence, "log_system_event", lambda *a, **k: None)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_URL)
    # Inject the mock transport rather than patching _http_post_discord: the send-interlock is
    # identity-based on the REAL transport, so patching it would (correctly) still be blocked under
    # pytest. An injected post_fn is the sanctioned test seam.
    real_cls = dn.DiscordNotifier          # capture BEFORE patching, or the factory recurses
    monkeypatch.setattr(dn, "DiscordNotifier", lambda **kw: real_cls(
        webhook_url=FAKE_URL, post_fn=rec, cooldown_seconds=0.0))

    result = asyncio.run(alerts.dispatch_critical_page(
        alerts.IncidentType.DEAD_MAN_HEARTBEAT,
        "Engine heartbeat stale: 180s (threshold 120s)",
        {"heartbeat_age_seconds": 180.0},
    ))

    assert len(rec.calls) == 1
    payload = rec.calls[0][1]
    assert payload["content"].startswith("@here")
    assert "DEAD_MAN_HEARTBEAT" in payload["embeds"][0]["title"]
    # and the result must not claim "log_only" when the operator was in fact reached
    assert result.delivered and result.channel == "discord"


def test_orchestrator_halt_pages_once_per_transition(monkeypatch):
    """Halt-but-HOLD is wired, and re-halting an already-halted engine does not re-page."""
    from sig.engine import orchestrator as orch_mod

    pages: list[tuple[str, str, dict]] = []
    orch = object.__new__(orch_mod.Orchestrator)      # bypass __init__: only halt() is under test
    orch._halt = asyncio.Event()
    orch._halt_reason = ""
    orch._interrupt = None
    orch._environment = "sim"
    monkeypatch.setattr(orch_mod.Orchestrator, "_page",
                        lambda self, i, m, md=None: pages.append((i, m, md or {})))

    orch_mod.Orchestrator.halt(orch, reason="repeated_order_errors")
    orch_mod.Orchestrator.halt(orch, reason="repeated_order_errors")

    assert len(pages) == 1, "a re-halt must not re-page"
    assert pages[0][0] == "HALT_BUT_HOLD"
    assert "repeated_order_errors" in pages[0][1]
