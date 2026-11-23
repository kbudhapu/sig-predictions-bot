"""D+R parity reversion shadow (2026-10-06): NO-pair entry when YES bids sum >= 1 + edge, exit on reversion."""
from __future__ import annotations

import json

from sig.engine.parity import OpenPair, PairQuote, open_from_events, step
from tools.parity_shadow_validate import summarize


def _q(db, da, rb, ra, race="race:x"):
    return PairQuote(race, "1", "2", db, da, rb, ra)


def test_entry_edge_and_threshold():
    assert abs(_q(0.53, 0.535, 0.50, 0.505).entry_edge - 0.03) < 1e-9     # buy both NOs for 0.97, pays >= 1
    book = {}
    assert step([_q(0.505, 0.51, 0.505, 0.51)], book, now_iso="t0") == []   # 1c < 2c entry edge
    ev = step([_q(0.53, 0.535, 0.50, 0.505)], book, now_iso="t1")
    assert len(ev) == 1 and ev[0]["event"] == "entry" and ev[0]["cost_per_pair"] == 0.97 and "race:x" in book


def test_exit_on_reversion_only_when_profitable():
    book = {"race:x": OpenPair("race:x", "t1", 0.53, 0.50, 0.03)}
    assert step([_q(0.52, 0.525, 0.50, 0.505)], book, now_iso="t2") == []    # 1.03 - 1.03 = 0 < 1c
    ev = step([_q(0.505, 0.51, 0.505, 0.51)], book, now_iso="t3")           # 1.03 - 1.02 = +1c
    assert ev[0]["event"] == "exit" and abs(ev[0]["exit_pnl"] - 0.01) < 1e-9 and book == {}


def test_never_trades_yes_pair_side_or_bad_books():
    book = {}
    assert step([_q(0.46, 0.465, 0.48, 0.485)], book, now_iso="t") == []    # sum < 1: YES-pair side not traded
    assert step([_q(0.60, 0.55, 0.50, 0.505)], book, now_iso="t") == []     # crossed book
    assert book == {}


def test_restart_rebuilds_open_book():
    evs = [{"event": "entry", "race": "a", "ts": "t", "d_bid": .53, "r_bid": .5, "entry_edge": .03},
           {"event": "entry", "race": "b", "ts": "t", "d_bid": .52, "r_bid": .5, "entry_edge": .02},
           {"event": "exit", "race": "a", "ts": "t2"}]
    assert set(open_from_events(evs)) == {"b"}


def test_parity_quotes_require_both_legs_fresh():
    import main_sig
    from datetime import datetime
    now = datetime.fromisoformat("2026-10-06T08:00:00+00:00").timestamp()
    prices = {"1": {"best_bid": 0.53, "best_ask": 0.535, "updated_at": "2026-10-06T07:59:30+00:00"},
              "2": {"best_bid": 0.50, "best_ask": 0.505, "updated_at": "2026-10-06T07:59:00Z"},
              "3": {"best_bid": 0.40, "best_ask": 0.41, "updated_at": "2026-10-06T07:50:00+00:00"},
              "4": {"best_bid": None, "best_ask": 0.41, "updated_at": "2026-10-06T07:59:50+00:00"}}
    qs = main_sig._parity_quotes([("r", "1", "2"), ("s", "1", "3"), ("t", "1", "9"), ("u", "1", "4")],
                                 prices, now, 120.0)
    assert [(q.race, q.d_bid, q.r_ask) for q in qs] == [("r", 0.53, 0.505)]  # s stale, t missing, u no bid


def test_validator_summary():
    evs = [{"event": "entry", "race": "a", "ts": "2026-10-06T00:00:00+00:00", "entry_edge": 0.03},
           {"event": "exit", "race": "a", "ts": "2026-10-06T02:00:00+00:00", "entry_ts": "2026-10-06T00:00:00+00:00",
            "exit_pnl": 0.015},
           {"event": "entry", "race": "b", "ts": "2026-10-07T00:00:00+00:00", "entry_edge": 0.02}]
    s = summarize(evs, 1000)
    assert s["entries"] == 2 and s["exits"] == 1 and s["exit_pnl_c_mean"] == 1.5 and s["hold_min_median"] == 120
    assert s["open"] == 1 and s["open_locked_c_mean"] == 2.0 and s["realized_usd_per_day"] == 15.0
