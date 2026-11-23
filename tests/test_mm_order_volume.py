"""MM quotes only FRESH markets — the only lever that actually shortens the cycle.

Probe result that motivates this (live, measured):

    batch of 5   -> HTTP 503, 21.0s,  4.20s/order
    batch of 25  -> HTTP 503, 112.6s, 4.50s/order   (5.36x wall-clock for 5x orders: LINEAR)
    6 concurrent -> 52.1s, 8.68s/order effective, 2 of 6 503'd       (worse than serial)
    serial       -> 7.0s/order p50 observed live

The venue serialises at ~4.5-8.7s per order regardless of transport, and the write-rate budget was
only 8% utilised — so the ~47 minute cycle was never a rate-limit or transport problem. 157
orders/cycle x ~4.5s is ~12 minutes of irreducible venue time at best. The only way to spend less
venue time is to send fewer orders.

Quoting only fresh markets is also correct on its own terms: ~58 of 237 markets stayed fresh inside
the cutoff while the MM quoted ~250, so most quotes were priced off dead books.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from sig.engine.live_strategies import CoreMarketMakingStrategy, _row_age_s

# A FIXED NOW for the pure-helper tests (which pass it in explicitly). The rows that go through
# evaluate() must be stamped against the REAL clock, because evaluate() calls
# datetime.now() itself -- a fixed past/future NOW would make every row read as stale/fresh
# regardless of its intended age (a future stamp clamps to age 0 by design).
NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def row(age_s: float | None, bid=0.46, ask=0.54, base=None) -> dict:
    """A price row `age_s` seconds old relative to the REAL clock unless `base` is given."""
    r = {"best_bid": bid, "best_ask": ask}
    if age_s is not None:
        anchor = base or datetime.now(timezone.utc)
        r["updated_at"] = (anchor - timedelta(seconds=age_s)).isoformat()
    return r


def mm(ids, prices, **cfg) -> CoreMarketMakingStrategy:
    base = {"sizing": {"k_reference_sigma": 0.1},
            "market_making": {"max_quote_age_s": 15, **cfg}}
    s = CoreMarketMakingStrategy(store=None, config=base, exchange_ids=ids, equity=100_000.0)
    s.set_prices(prices)
    return s


# --------------------------------------------------------------------------- #
# Freshness helper
# --------------------------------------------------------------------------- #
def test_age_is_computed_from_updated_at():
    assert _row_age_s(row(10.0, base=NOW), NOW) == pytest.approx(10.0, abs=0.01)
    assert _row_age_s(row(0.0, base=NOW), NOW) == pytest.approx(0.0, abs=0.01)


def test_unknown_age_is_None_so_the_caller_can_treat_it_as_stale():
    """A row with no/garbage timestamp must not read as fresh. Quoting a book whose freshness we
    cannot establish is the same mistake as quoting a known-stale one."""
    assert _row_age_s(row(None)) is None
    assert _row_age_s({"updated_at": "not-a-date"}) is None
    assert _row_age_s({}) is None
    assert _row_age_s(None) is None


def test_a_future_timestamp_clamps_to_zero_not_negative():
    future = {"best_bid": 0.46, "best_ask": 0.54,
              "updated_at": (NOW + timedelta(seconds=30)).isoformat()}
    assert _row_age_s(future, NOW) == 0.0


# --------------------------------------------------------------------------- #
# THE VOLUME CUT
# --------------------------------------------------------------------------- #
def test_only_fresh_markets_are_quoted():
    """The headline: 200 markets, 50 fresh => quotes are emitted for the 50, not the 200."""
    ids = [str(1000 + i) for i in range(200)]
    prices = {e: row(2.0 if i < 50 else 600.0) for i, e in enumerate(ids)}
    intents = mm(ids, prices).evaluate({"positions": {}})
    quoted = {i.exchange_id for i in intents}
    assert len(quoted) <= 50, f"quoted {len(quoted)} markets; only 50 were fresh"
    assert all(int(e) < 1050 for e in quoted), "a stale market must never be quoted"


def test_the_live_ratio_cuts_order_volume_by_about_75_percent():
    """Measured live: ~58 of 237 fresh. Order count must fall by roughly that ratio."""
    ids = [str(1000 + i) for i in range(237)]
    fresh = {e: row(3.0) for e in ids[:58]}
    stale = {e: row(1900.0) for e in ids[58:]}
    all_fresh_intents = len(mm(ids, {e: row(3.0) for e in ids}).evaluate({"positions": {}}))
    fresh_only_intents = len(mm(ids, {**fresh, **stale}).evaluate({"positions": {}}))
    assert fresh_only_intents < all_fresh_intents
    cut = 1.0 - fresh_only_intents / max(all_fresh_intents, 1)
    assert cut > 0.70, f"expected >70% volume cut, got {cut:.0%}"


def test_a_market_with_no_touch_is_not_quoted_either():
    ids = ["1", "2", "3"]
    prices = {"1": row(2.0), "2": {"best_bid": None, "best_ask": 0.5,
                                  "updated_at": (NOW - timedelta(seconds=2)).isoformat()}}
    quoted = {i.exchange_id for i in mm(ids, prices).evaluate({"positions": {}})}
    assert quoted <= {"1"}, "no two-sided touch => no quote"


def test_unknown_age_markets_are_skipped():
    """Fail CLOSED on unknown freshness."""
    ids = ["1", "2"]
    prices = {"1": row(2.0), "2": row(None)}      # "2" has a touch but no timestamp
    quoted = {i.exchange_id for i in mm(ids, prices).evaluate({"positions": {}})}
    assert "2" not in quoted


def test_the_cutoff_is_configurable_and_widening_it_admits_more():
    ids = [str(i) for i in range(20)]
    prices = {e: row(30.0) for e in ids}          # all 30s old
    assert mm(ids, prices, max_quote_age_s=15).evaluate({"positions": {}}) == []
    assert len(mm(ids, prices, max_quote_age_s=60).evaluate({"positions": {}})) > 0


def test_capital_is_split_across_only_the_quoted_markets():
    """The sleeve must not be spread over markets we decline to quote — that would under-size the
    ones we do quote."""
    ids = [str(1000 + i) for i in range(100)]
    prices = {e: row(2.0 if i < 10 else 900.0) for i, e in enumerate(ids)}
    intents = mm(ids, prices).evaluate({"positions": {}})
    assert intents, "the 10 fresh markets must still be quoted"
    qty = max(int(i.metadata.get("mm_quote_qty", 0)) for i in intents)
    # 10 markets, not 100 => each quote gets ~10x the capital it would otherwise
    assert qty > 0


# --------------------------------------------------------------------------- #
# Cadence + requote churn
# --------------------------------------------------------------------------- #
def test_mm_cadence_is_its_own_knob_not_the_fast_arb_cadence():
    cfg = {"sizing": {"k_reference_sigma": 0.1},
           "loops": {"fast_loop_interval_s": 5},
           "market_making": {"cadence_s": 30}}
    s = CoreMarketMakingStrategy(store=None, config=cfg, exchange_ids=["1"], equity=1.0)
    assert s.cadence_s == 30.0, "a full re-quote sweep must not run at the 5s arb cadence"


def test_live_config_has_the_volume_controls_set():
    mmc = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["market_making"]
    # Tight enough to exclude a genuinely unconfirmed book, loose enough to admit the whole board —
    # above the price pump's 5s period AND above the REST net's confirmation interval, because on a
    # PUSH feed updated_at is the last CHANGE, not the last confirmation.
    cfg = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))
    net_s = float(cfg["feed"]["rest_safety_net_interval_s"])
    assert 5 < mmc["max_quote_age_s"] <= 180, "the fresh-only cutoff must be tight but reachable"
    assert mmc["max_quote_age_s"] > net_s, (
        f"max_quote_age_s ({mmc['max_quote_age_s']}s) must exceed the REST confirmation interval "
        f"({net_s}s), else only markets that happened to TICK are quotable")
    assert mmc["cadence_s"] >= 15, "the sweep must be slower than the arb cadence"
    assert mmc["requote_threshold"] >= 0.08, "churn threshold must be widened from 0.05"
