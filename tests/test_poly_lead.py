"""Poly-led lag shadow (2026-10-12): pure signal + record-only loop (one book per lookback, restart reload)."""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main_sig  # noqa: E402
from sig.engine.fresh_gap import Row, poly_lead_signal  # noqa: E402

_REAL_SLEEP = asyncio.sleep


def R(sharp, bid, ask, mid_past, sharp_past):
    return Row("1", sharp, bid, ask, mid_past, sharp_past)


def test_signal_fires_when_poly_moves_and_sig_lags():
    # Poly 0.50 -> 0.54 (+4c), SIG mid 0.50 -> 0.505 (didn't follow), gap +3.5c -> buy YES
    assert poly_lead_signal(R(0.54, 0.50, 0.51, 0.50, 0.50), 0.03) == "yes"
    # mirror: Poly down, SIG flat -> buy NO
    assert poly_lead_signal(R(0.46, 0.49, 0.50, 0.50, 0.50), 0.03) == "no"


def test_signal_rejects_followed_small_wide_or_wrong_side():
    assert poly_lead_signal(R(0.52, 0.50, 0.51, 0.50, 0.50), 0.03) is None       # Poly moved only 2c
    assert poly_lead_signal(R(0.54, 0.515, 0.525, 0.50, 0.50), 0.03) is None     # SIG followed +2c (>= 1/3)
    assert poly_lead_signal(R(0.54, 0.50, 0.53, 0.50, 0.50), 0.03) is None       # spread 3c > 2c
    assert poly_lead_signal(R(0.54, 0.525, 0.535, 0.55, 0.50), 0.03) is None     # SIG above Poly already (gap<0)
    assert poly_lead_signal(R(0.54, 0.53, 0.535, 0.53, 0.50), 0.03) is None      # gap 0.75c < half the move
    assert poly_lead_signal(R(0.54, 0.0, 0.51, 0.50, 0.50), 0.03) is None        # broken book


def test_parse_lookbacks():
    assert main_sig._parse_lookbacks([6, 1, 3, 3]) == [1.0, 3.0, 6.0]
    assert main_sig._parse_lookbacks("1, 3,6") == [1.0, 3.0, 6.0]
    assert main_sig._parse_lookbacks(2) == [2.0]


class _Store:
    def __init__(self, past_by_lb):
        self.past_by_lb, self.queries = past_by_lb, []

    async def _read(self, sql, params):
        from datetime import datetime, timezone
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(params[0])).total_seconds() / 3600
        self.queries.append(round(age_h))
        rows = self.past_by_lb.get(round(age_h), {})
        return [{"exchange_id": ex, "ts": params[0], "sharp": sh, "sig_mid": m} for ex, (m, sh) in rows.items()]


class _XM:
    def __init__(self, book):
        self.book = book
        self._sig_prices = {ex: {"best_bid": b, "best_ask": a} for ex, (sh, b, a) in book.items()}

    def active_sig_exchange_ids(self):
        return list(self.book)

    def aligned_benchmark(self, ex):
        return self.book[ex][0]


def _run(monkeypatch, store, xm, cfg, ticks=1):
    stop = asyncio.Event()
    real_sleep = _REAL_SLEEP
    count = {"n": 0}

    async def fast_sleep(s):
        count["n"] += 1
        if count["n"] > ticks:
            stop.set()
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main_sig._poly_lead_loop(store=store, xm_provider=xm, cfg_raw=cfg, stop_event=stop))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


def _events(path):
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


def test_loop_records_per_lookback_books_and_reloads(tmp_path, monkeypatch):
    path = str(tmp_path / "pl.jsonl")
    cfg = {"cross_market": {"poly_lead_mode": "shadow", "poly_lead_lookbacks_h": [1, 3], "poly_lead_path": path}}
    # market 7: Poly moved +4c in the last 1h (lags) but 3h ago Poly was already there (no 3h signal)
    store = _Store({1: {"7": (0.50, 0.50)}, 3: {"7": (0.50, 0.54)}})
    xm = _XM({"7": (0.54, 0.50, 0.51)})
    _run(monkeypatch, store, xm, cfg)
    ev = _events(path)
    assert sorted(store.queries) == [1, 3]
    assert [(e["event"], e["lb_h"], e["side"]) for e in ev] == [("entry", 1.0, "yes")]
    assert ev[0]["prices"]["taker_in"] == 0.51
    # restart: the open 1h trade reloads; SIG catches up -> gap closes -> one exit, no new entry
    xm2 = _XM({"7": (0.54, 0.535, 0.545)})
    _run(monkeypatch, _Store({1: {"7": (0.50, 0.50)}}), xm2, cfg)
    ev = _events(path)
    assert [e["event"] for e in ev] == ["entry", "exit"]
    assert ev[1]["reason"] == "flipped" or ev[1]["reason"] == "closed"
    assert ev[1]["id"] == ev[0]["id"] and ev[1]["lb_h"] == 1.0


def test_loop_off_by_default_and_survives_bad_store(tmp_path, monkeypatch):
    _run(monkeypatch, _Store({}), _XM({}), {"cross_market": {}})            # off: returns immediately

    class Bad:
        async def _read(self, *a):
            raise RuntimeError("db locked")
    path = str(tmp_path / "pl.jsonl")
    _run(monkeypatch, Bad(), _XM({"7": (0.54, 0.50, 0.51)}),
         {"cross_market": {"poly_lead_mode": "shadow", "poly_lead_path": path}}, ticks=2)
    assert not os.path.exists(path)                                          # no crash, nothing recorded
