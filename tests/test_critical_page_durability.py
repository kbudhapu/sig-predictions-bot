"""The durable alert leg must work against an UNINITIALISED operational DB.

Live failure this pins: `data/trading.db` was 0 bytes with zero tables, because the trader never
called `init_db()` (only scripts/arb_monitor.py did). `log_system_event` called only
`ensure_db_writable`, so it raised `no such table: system_events` — and because
`dispatch_critical_page` writes that row BEFORE its push legs, the raise aborted the ENTIRE page.
Every dead-man page lost its record AND its Discord/webhook notification, silently, as an
unretrieved asyncio task exception.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from infra.persistence import db as opsdb


@pytest.fixture(autouse=True)
def _clear_page_cooldowns():
    """Clear BOTH module-level cooldown stores around every test in this file.

    `dispatch_critical_page` keeps a 300s per-incident cooldown in `alerts._recent_pages`, and the
    Discord notifier keeps its own in `discord_notifier._recent_sends`. Both are process-global, so
    a test here that pages DEAD_MAN_HEARTBEAT would otherwise suppress the SAME incident in
    tests/test_discord_alerts.py — which passed alone and failed in the full suite until this
    fixture existed. Cleaning up after ourselves is this file's responsibility, not theirs.
    """
    import infra.alerts.discord_notifier as dn
    import infra.control.alerts as alerts

    def clear():
        with alerts._recent_pages_lock:
            alerts._recent_pages.clear()
        dn.reset_cooldowns()

    clear()
    yield
    clear()


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type=:t", {"t": "table"}).fetchall()}


# --------------------------------------------------------------------------- #
# FIX 1 — log_system_event self-heals, like persist_alert already did
# --------------------------------------------------------------------------- #
def test_log_system_event_works_on_a_virgin_db(tmp_path):
    """The exact live condition: a DB file that exists but has no tables."""
    p = tmp_path / "trading.db"
    sqlite3.connect(p).close()                       # 0-byte file, zero tables
    assert _tables(p) == set()

    opsdb.log_system_event("CRITICAL_PAGE", "dead man stale", severity="critical", db_path=p)

    assert "system_events" in _tables(p)
    with sqlite3.connect(p) as c:
        rows = c.execute("SELECT event_type, severity, message FROM system_events").fetchall()
    assert rows == [("CRITICAL_PAGE", "critical", "dead man stale")]


def test_log_system_event_works_when_the_file_does_not_exist_at_all(tmp_path):
    p = tmp_path / "nested" / "trading.db"
    opsdb.log_system_event("BOOT", "hello", db_path=p)
    assert "system_events" in _tables(p)


def test_persist_alert_still_works_on_a_virgin_db(tmp_path):
    """The sibling path that was already self-healing — guard against regressing it."""
    p = tmp_path / "trading.db"
    opsdb.persist_alert({"kind": "heartbeat_stale", "severity": "critical",
                         "message": "stale", "detail": {"component": "orchestrator"}}, p)
    with sqlite3.connect(p) as c:
        rows = c.execute("SELECT kind, severity FROM operator_alerts").fetchall()
    assert rows == [("heartbeat_stale", "critical")]


def test_init_db_is_idempotent(tmp_path):
    p = tmp_path / "trading.db"
    opsdb.init_db(p)
    before = _tables(p)
    opsdb.init_db(p)
    assert _tables(p) == before and "system_events" in before


# --------------------------------------------------------------------------- #
# FIX 2 — a failed durable row must NOT abort the push legs
# --------------------------------------------------------------------------- #
def test_a_persistence_failure_does_not_swallow_the_page(monkeypatch):
    """A page that reaches nobody is worse than a page that is not filed. If the durable write
    fails, the Discord/webhook legs must still run."""
    import infra.alerts.discord_notifier as dn
    import infra.control.alerts as alerts

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such table: system_events")

    monkeypatch.setattr(alerts.persistence, "log_system_event", boom)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL",
                       "https://discord.com/api/webhooks/1/TESTTOKEN")
    dn.reset_cooldowns()

    sent: list[dict] = []
    real_cls = dn.DiscordNotifier
    monkeypatch.setattr(dn, "DiscordNotifier", lambda **kw: real_cls(
        webhook_url="https://discord.com/api/webhooks/1/TESTTOKEN",
        post_fn=lambda url, payload, **k: sent.append(payload),
        cooldown_seconds=0.0))

    res = asyncio.run(alerts.dispatch_critical_page(
        alerts.IncidentType.DEAD_MAN_HEARTBEAT, "heartbeat stale 180s"))

    assert len(sent) == 1, "the push leg must still fire when the durable row fails"
    assert sent[0]["content"].startswith("@here")
    assert res.delivered and res.channel == "discord"


def test_the_durable_row_is_written_when_persistence_is_healthy(monkeypatch, tmp_path):
    """And the row must still land in the normal case — fix 2 must not have made it optional."""
    import infra.control.alerts as alerts

    p = tmp_path / "trading.db"
    captured = {}
    # `alerts.persistence` IS this module, so the real function must be captured BEFORE patching —
    # otherwise the spy calls itself.
    real_log = opsdb.log_system_event

    def spy(event_type, message, severity="info", **kw):
        captured["event"] = (event_type, severity)
        real_log(event_type, message, severity=severity, db_path=p)

    monkeypatch.setattr(alerts.persistence, "log_system_event", spy)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    asyncio.run(alerts.dispatch_critical_page("BOOT_BLOCKING_ERROR", "boot failed"))
    assert captured["event"] == ("CRITICAL_PAGE", "critical")
    with sqlite3.connect(p) as c:
        assert c.execute("SELECT COUNT(*) FROM system_events").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# FIX 3 — the trader initialises the operational DB at startup
# --------------------------------------------------------------------------- #
def test_main_sig_initialises_the_operational_db():
    """Root cause: the trader opened infra/state/state.db (a SEPARATE store) and never created the
    operational tables, so trading.db stayed 0 bytes. Pinned as source, because the alternative is
    booting a live trader to find out."""
    src = open("main_sig.py", encoding="utf-8").read()
    assert "init_db()" in src, "main_sig.py must initialise the operational DB"
    assert src.count("operational_db_ready") >= 1
    # every StateStore() open site should be preceded by the init
    assert src.count("init_db()") >= src.count("store = StateStore()")


def test_the_operational_db_and_the_state_db_are_different_files():
    """They are separate on purpose; conflating them is what hid this. If they ever became the same
    path, `init_db` would start clobbering the state schema."""
    from infra.config import DB_PATH
    from infra.state.schema import STATE_DB_PATH

    assert str(DB_PATH) != str(STATE_DB_PATH)
    assert DB_PATH.name == "trading.db" and STATE_DB_PATH.name == "state.db"
