"""Fair-band maker (2026-10-08): join-only quotes at fair +- w, caps, classification, validator fill matching."""
from datetime import datetime, timedelta, timezone

from sig.engine.fair_band import FairBandConfig, band_quotes, classify
from tools.fair_band_validate import simulate

CFG = FairBandConfig(w=0.03, quote_usd=500.0, cap_usd=2000.0)


def _row(fair, bid, ask, tags=("balanced",)):
    return {"exchange_id": "1", "fair": fair, "best_bid": bid, "best_ask": ask, "bid_size": 900, "ask_size": 700,
            "tags": set(tags)}


def test_join_only_quotes_never_improve_or_cross():
    q = {x["side"]: x for x in band_quotes(_row(0.50, 0.495, 0.50), CFG)}
    assert q["bid"]["px"] == 0.47 and q["ask"]["px"] == 0.53                 # fair +- 3c, behind the touch
    assert not q["bid"]["at_touch"] and not q["ask"]["at_touch"]
    q = {x["side"]: x for x in band_quotes(_row(0.60, 0.55, 0.555), CFG)}  # SIG 5c below fair
    assert q["bid"]["px"] == 0.55 and q["bid"]["at_touch"] and q["bid"]["depth"] == 900   # joins the best bid
    assert q["ask"]["px"] == 0.63                                            # fair + 3c, far from the touch


def test_universe_and_bad_books_skipped():
    assert band_quotes(_row(0.5, 0.49, 0.5, tags=()), CFG) == []
    assert band_quotes(_row(0.5, 0.5, 0.5), CFG) == []                       # locked book


def test_cap_blocks_only_the_side_that_adds_exposure():
    long_q = {x["side"] for x in band_quotes(_row(0.6, 0.55, 0.555), CFG, held_qty=5000)}   # ~$2.8k YES > cap
    assert long_q == {"ask"}
    short_q = {x["side"] for x in band_quotes(_row(0.6, 0.55, 0.555), CFG, held_qty=-5000)}  # ~$2.2k NO > cap
    assert short_q == {"bid"}


def test_classify():
    t = classify({"1": 0.01, "2": 0.05, "3": None}, {"1": 100.0, "2": 50_000.0, "3": 20_000.0, "4": 10.0})
    # volumes sorted [10, 100, 20k, 50k]: the top-third cut is 20k -> markets 2 and 3 are liquid; 3 has no gap data
    assert t["1"] == {"balanced"} and t["2"] == {"liquid"} and t["3"] == {"liquid"} and "4" not in t


def test_validator_fills_and_inventory():
    t0 = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
    q = {"ts": t0.isoformat(), "exchange_id": "1", "side": "bid", "px": 0.55, "shares": 1000.0, "at_touch": True,
         "depth": 0.0, "bid": 0.55, "ask": 0.555, "fair": 0.6, "tags": ["balanced"]}
    trades = {"1": [(t0 + timedelta(seconds=5), "NO", 0.55, 400.0), (t0 + timedelta(seconds=9), "YES", 0.555, 400.0)]}
    r = simulate([q], trades, pessimistic=True, mid_at=lambda ex, t: 0.57)
    assert r["inv"]["1"] == 400.0 and abs(r["cash"]["1"] + 220.0) < 1e-9     # bought 400 YES at 0.55
    assert abs(r["mo"]["1"] - 400 * 0.02) < 1e-9                              # +2c markout at 0.57
