"""2026-10-06 pipeline cleanup: capped-intent skip, exchange-id discovery, order hygiene stale-row selection."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from sig.engine.orchestrator import Orchestrator
from sig.engine.protocols import Intent


def _intent(ex="1061", side="no", action="open", stype="directional", sid="cross_market_divergence"):
    return Intent(strategy_id=sid, exchange_id=ex, side="buy", action=action, strategy_type=stype,
                  signal_strength=0.5, price=0.85, metadata={"option_side": side})


def _orch():
    return Orchestrator.__new__(Orchestrator)            # helpers only touch __dict__


def test_capped_intent_is_skipped_then_retried(monkeypatch):
    import sig.engine.orchestrator as om
    o = _orch()
    now = [1000.0]
    monkeypatch.setattr(om.time, "monotonic", lambda: now[0])
    it = _intent()
    assert not o._skip_capped(it)
    o._note_capped(it)
    assert o._skip_capped(it)                              # same market+side: skipped
    assert not o._skip_capped(_intent(side="yes"))         # other side / other market unaffected
    assert not o._skip_capped(_intent(ex="999"))
    now[0] += Orchestrator.CAPPED_SKIP_S + 1
    assert not o._skip_capped(it)                          # retried after the window


def test_capped_skip_never_applies_to_mm_arb_or_closes():
    o = _orch()
    for it in (_intent(stype="market_making", sid="market_making"), _intent(stype="arb", sid="arb"),
               _intent(action="close")):
        o._note_capped(it)
        assert not o._skip_capped(it)


def test_extract_exchange_ids_skips_market_id_when_row_has_exchanges():
    from main_sig import _extract_exchange_ids
    market = {"id": "387", "question": "Will the Democratic Party win X?",
              "exchanges": [{"id": "1076"}, {"id": "1077"}]}
    assert _extract_exchange_ids(market) == ["1076", "1077"]
    assert _extract_exchange_ids({"id": "1080"}) == ["1080"]            # a bare exchange row keeps its id
    assert _extract_exchange_ids({"exchangeId": "5", "exchanges": [{"id": "6"}]}) == ["5", "6"]


def test_order_hygiene_stale_rows(tmp_path):
    from tools.order_hygiene import stale_rows
    db = sqlite3.connect(tmp_path / "o.db")
    db.execute("CREATE TABLE orders (idempotency_key TEXT, broker_order_id TEXT, strategy TEXT, status TEXT, "
               "created_at TEXT, updated_at TEXT)")
    rows = [("a", "100", "market_making", "open", "2026-10-05T00:00:00+00:00"),     # gone from venue -> stale
            ("b", "200", "market_making", "open", "2026-10-05T00:00:00+00:00"),     # still open on venue
            ("c", None, "dutch_book", "pending", "2026-10-05T00:00:00+00:00"),      # never got an id -> stale
            ("d", "300", "cross_market_divergence", "open", "2026-10-06T09:59:00+00:00"),  # too recent
            ("e", "400", "market_making", "filled", "2026-10-05T00:00:00+00:00")]   # not open
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,NULL)", rows)
    now = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    assert sorted(r[0] for r in stale_rows(db, {"200"}, 60.0, now)) == ["a", "c"]
