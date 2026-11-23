"""Tests for the market-making strategy (strategies/market_making/).

Covers the three required behaviours:
  1. the quote respects tick + spread + price bounds (and rests strictly inside the touch);
  2. inventory skew triggers on repeated one-sided fills and the cap pulls the adding side;
  3. the adverse-selection guard pulls the picked-off side on a fast mid move.
Plus tick rounding, one-sided-book skip, tight-spread skip, and the executor output contract.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from strategies.market_making import (
    TICK, PRICE_MIN, PRICE_MAX, QuoterConfig, compute_quote, round_to_tick,
    InventoryConfig, InventoryBook,
    AdverseConfig, AdverseSelectionMonitor,
    MarketMakerConfig, MarketMaker, to_contract,
)


def _on_tick(p: float) -> bool:
    return abs(round(p / TICK) - p / TICK) < 1e-6


# ── 1. quoter: tick + spread + bounds ────────────────────────────────────────
def test_quote_inside_spread_on_tick_and_in_bounds():
    q = compute_quote("1075", 0.46, 0.54)            # spread 0.08, mid 0.50
    assert q.post_bid and q.post_ask
    assert 0.46 < q.bid_price < q.ask_price < 0.54   # strictly inside the touch
    assert _on_tick(q.bid_price) and _on_tick(q.ask_price)
    assert PRICE_MIN <= q.bid_price <= PRICE_MAX and PRICE_MIN <= q.ask_price <= PRICE_MAX


def test_quote_respects_bounds_near_edges():
    lo = compute_quote("a", 0.015, 0.095)            # low-priced market (recon has these)
    assert lo.bid_price >= PRICE_MIN and 0.015 < lo.bid_price < lo.ask_price < 0.095
    hi = compute_quote("b", 0.915, 0.995)            # high-priced market
    assert hi.ask_price <= PRICE_MAX and 0.915 < hi.bid_price < hi.ask_price < 0.995


def test_round_to_tick_snaps_and_clamps():
    assert round_to_tick(0.5123) == 0.51
    assert round_to_tick(0.4988) == 0.5
    assert round_to_tick(1.5) == PRICE_MAX           # clamp high
    assert round_to_tick(0.0) == PRICE_MIN           # clamp low


def test_one_sided_book_is_skipped():
    q = compute_quote("c", None, 0.05)               # pre-open one-sided (recon §4)
    assert not q.post_bid and not q.post_ask and q.reason == "one_sided_or_crossed_book"


def test_tight_spread_joins_touch_by_default():
    # 2-tick spread — no room to rest inside; default join_when_tight posts AT the touch (the fill unlock)
    q = compute_quote("d", 0.50, 0.51)
    assert q.post_bid and q.post_ask and q.reason == "joined_touch"
    assert q.bid_price == 0.50 and q.ask_price == 0.51


def test_tight_spread_skipped_when_join_disabled():
    q = compute_quote("d", 0.50, 0.51, QuoterConfig(join_when_tight=False))
    assert not q.post_bid and not q.post_ask and q.reason == "spread_too_tight"


def test_larger_k_is_capped_just_inside_touch():
    q = compute_quote("e", 0.46, 0.54, QuoterConfig(k=0.5))   # k way bigger than half-spread
    assert 0.46 < q.bid_price < q.ask_price < 0.54            # still strictly inside, not crossing


# ── 2. inventory: skew on over-fill + cap ────────────────────────────────────
def test_inventory_skew_triggers_on_overfill():
    book = InventoryBook(InventoryConfig(cap=500, skew_ticks_at_cap=4.0))
    base = compute_quote("m", 0.46, 0.54)            # bid 0.485 / ask 0.515
    book.record_fill("m", "buy", 300)                # one side kept filling → long 300
    skewed = book.apply("m", base)
    assert book.position("m") == 300
    # long inventory → quotes shift DOWN to offload (both sides lower than the neutral quote)
    assert skewed.bid_price < base.bid_price and skewed.ask_price < base.ask_price
    assert skewed.post_bid and skewed.post_ask       # not yet at cap → both sides live


def test_inventory_cap_pulls_the_adding_side():
    book = InventoryBook(InventoryConfig(cap=500))
    base = compute_quote("m", 0.46, 0.54)
    book.record_fill("m", "buy", 500)                # at the long cap
    skewed = book.apply("m", base)
    assert book.at_cap("m")
    assert not skewed.post_bid                        # buying would breach the cap → bid pulled
    assert skewed.post_ask                            # selling reduces the long → still quoted
    assert "inventory_cap_long" in skewed.reason


def test_short_inventory_skews_up():
    book = InventoryBook(InventoryConfig(cap=500, skew_ticks_at_cap=4.0))
    base = compute_quote("m", 0.46, 0.54)
    book.record_fill("m", "sell", 300)               # short 300
    skewed = book.apply("m", base)
    assert skewed.bid_price > base.bid_price and skewed.ask_price > base.ask_price


# ── 3. adverse selection: pull on a fast mid move ────────────────────────────
def test_adverse_pull_fires_on_fast_up_move():
    mon = AdverseSelectionMonitor(AdverseConfig(move_threshold=0.03, window_seconds=30))
    mon.observe("x", 0.50, now=0.0)
    mon.observe("x", 0.54, now=10.0)                 # +0.04 over 10s (> 0.03)
    sig = mon.evaluate("x", now=10.0)
    assert sig.triggered and sig.pull_ask and not sig.pull_bid   # rising → ask picked off


def test_adverse_pull_fires_on_fast_down_move():
    mon = AdverseSelectionMonitor(AdverseConfig(move_threshold=0.03, window_seconds=30))
    mon.observe("x", 0.50, now=0.0)
    mon.observe("x", 0.45, now=5.0)                  # −0.05 → bid picked off
    sig = mon.evaluate("x", now=5.0)
    assert sig.triggered and sig.pull_bid and not sig.pull_ask


def test_adverse_quiet_market_does_not_trigger():
    mon = AdverseSelectionMonitor(AdverseConfig(move_threshold=0.03, window_seconds=30))
    mon.observe("x", 0.50, now=0.0)
    mon.observe("x", 0.505, now=10.0)                # +0.005 < threshold
    sig = mon.evaluate("x", now=10.0)
    assert not sig.triggered


def test_adverse_stale_samples_pruned_out_of_window():
    mon = AdverseSelectionMonitor(AdverseConfig(move_threshold=0.03, window_seconds=30))
    mon.observe("x", 0.50, now=0.0)
    mon.observe("x", 0.90, now=100.0)                # old sample (0.50@t0) is now out of window
    sig = mon.evaluate("x", now=100.0)
    assert sig.reason == "insufficient_history" and not sig.triggered


def test_adverse_widen_mode_pushes_side_out():
    mon = AdverseSelectionMonitor(AdverseConfig(move_threshold=0.03, mode="widen", widen_ticks=4))
    base = compute_quote("x", 0.46, 0.54)            # ask 0.515
    mon.observe("x", 0.50, now=0.0)
    mon.observe("x", 0.56, now=5.0)
    sig = mon.evaluate("x", now=5.0)
    widened = mon.apply(sig, base)
    assert sig.widen_ask and widened.post_ask and widened.ask_price > base.ask_price


# ── output contract + composition ────────────────────────────────────────────
def test_output_contract_shape():
    q = compute_quote("1075", 0.46, 0.54)
    orders, cancels = to_contract(q, side="yes")
    assert len(orders) == 2 and not cancels
    for o in orders:
        assert set(o) == {"exchange_id", "side", "action", "price", "quantity",
                          "order_type", "strategy"}
        assert o["order_type"] == "limit" and o["strategy"] == "mm" and o["side"] == "yes"
        assert isinstance(o["quantity"], int)
    assert {o["action"] for o in orders} == {"buy", "sell"}


def test_market_maker_end_to_end_pull_emits_cancel():
    mm = MarketMaker(MarketMakerConfig(adverse=AdverseConfig(move_threshold=0.03, window_seconds=30)),
                     equity=100_000.0, active_markets=1)
    mm.build("1075", 0.48, 0.52, now=0.0)            # seed mid history at 0.50
    out = mm.build("1075", 0.52, 0.56, now=10.0)     # mid 0.54 → +0.04 → pull ask
    actions = {o["action"] for o in out["orders"]}
    assert "sell" not in actions and "buy" in actions          # ask pulled, bid still resting
    assert any(c["quote_side"] == "ask" for c in out["cancels"])
    assert out["adverse"].pull_ask


def test_market_maker_is_pure_idempotent_on_same_state():
    mm = MarketMaker(equity=100_000.0, active_markets=1)
    a = mm.build("1075", 0.46, 0.54, now=0.0)
    b = mm.build("1075", 0.46, 0.54, now=0.1)        # same touch → same desired quote
    assert a["orders"] == b["orders"]                 # lets the executor diff & skip re-writes (rate-friendly)


def test_async_entrypoint_matches_sync():
    mm = MarketMaker(equity=100_000.0, active_markets=1)
    out = asyncio.run(mm.on_market_state("1075", 0.46, 0.54, now=0.0))
    assert out["orders"] and out["exchange_id"] == "1075"


# ── sell side is bounded to held inventory (round-trips, never a naked short) ─────────────────
def test_flat_book_posts_no_ask_only_a_bid():
    """FLAT inventory ⇒ nothing to sell ⇒ the ask is suppressed (a naked short the gate rejects),
    and the bid is still posted so we can accumulate. The one-sided accumulation starts here."""
    book = InventoryBook(InventoryConfig(cap=500))
    base = compute_quote("m", 0.46, 0.54)                # quoter wants BOTH sides
    q = book.apply("m", base)                            # position == 0
    assert q.post_bid and not q.post_ask                 # may buy; may NOT naked-short
    assert q.ask_size == 0 and "no_inventory_to_sell" in q.reason
    orders, cancels = to_contract(q, side="yes")
    assert {o["action"] for o in orders} == {"buy"}      # only a buy rests
    assert any(c["quote_side"] == "ask" for c in cancels)


def test_ask_size_is_bounded_to_held_inventory():
    """The ask quantity never exceeds the held long position (sell only what we hold)."""
    book = InventoryBook(InventoryConfig(cap=500))
    base = compute_quote("m", 0.46, 0.54)                # quoter size 100 per side
    book.record_fill("m", "buy", 40)                     # long 40 < one clip
    q = book.apply("m", base)
    assert q.post_ask and q.ask_size == 40               # bounded DOWN to the 40 we hold
    book.record_fill("m", "buy", 260)                    # now long 300 > one clip
    q2 = book.apply("m", base)
    # Holding MORE than enough ⇒ held_long is NOT the binding bound (it was `== 100` when the size
    # was a fixed constant; the size is now capital-% x inventory skew, so assert the INVARIANT).
    assert q2.post_ask and 0 < q2.ask_size <= 400
    assert q2.ask_size > q.ask_size, "a bigger holding must not shrink the ask"
    assert q2.ask_size >= q2.bid_size, "long inventory ⇒ the reducing (ask) side is not smaller"


def test_short_book_posts_no_ask():
    """A (hypothetical) short position still posts no ask — selling would deepen the short, which
    the gate blocks. Arithmetic only; the live gate prevents ever reaching a short via MM."""
    book = InventoryBook(InventoryConfig(cap=500))
    base = compute_quote("m", 0.46, 0.54)
    book.record_fill("m", "sell", 50)                    # pos -50
    q = book.apply("m", base)
    assert not q.post_ask and q.ask_size == 0


def test_market_maker_round_trip_buy_then_sell_captures_spread():
    """The core behaviour: the MM accumulates via bid fills, then rests an ASK against the HELD
    inventory to sell it back INSIDE the spread (bid < ask) — a spread-capturing round-trip, not
    monotonic long accumulation."""
    mm = MarketMaker(equity=100_000.0, active_markets=1)
    flat = mm.build("YES_X", 0.46, 0.54, now=0.0)
    assert {o["action"] for o in flat["orders"]} == {"buy"}      # flat ⇒ bid only

    mm.record_fill("YES_X", "buy", 120)                          # a bid filled → long 120
    held = mm.build("YES_X", 0.46, 0.54, now=0.1)
    buys = [o for o in held["orders"] if o["action"] == "buy"]
    sells = [o for o in held["orders"] if o["action"] == "sell"]
    assert buys and sells                                        # NOW two-sided
    assert sells[0]["quantity"] <= 120                           # sell only what we hold
    assert buys[0]["price"] < sells[0]["price"]                  # ask above bid ⇒ positive spread

    mm.record_fill("YES_X", "sell", int(sells[0]["quantity"]))   # the ask fills → inventory reduced
    assert mm.position("YES_X") == 120 - int(sells[0]["quantity"])  # round-trip unwound the long


def test_adverse_pull_still_fires_while_holding_inventory():
    """Adverse-selection pull is preserved: holding inventory does NOT force the ask to rest into a
    fast up-move. A long book that would otherwise quote an ask still PULLS it when picked off."""
    mm = MarketMaker(MarketMakerConfig(adverse=AdverseConfig(move_threshold=0.03, window_seconds=30)),
                     equity=100_000.0, active_markets=1)
    mm.record_fill("YES_X", "buy", 300)                          # long ⇒ would normally post an ask
    mm.build("YES_X", 0.48, 0.52, now=0.0)                       # seed mid history at 0.50
    out = mm.build("YES_X", 0.56, 0.60, now=5.0)                 # +0.08 fast up-move → pull the ask
    assert out["adverse"].pull_ask is True
    assert "sell" not in {o["action"] for o in out["orders"]}    # ask pulled despite holding inventory
    assert any(c["quote_side"] == "ask" for c in out["cancels"])


def test_live_strategy_bounds_ask_to_venue_truth_positions():
    """CoreMarketMakingStrategy syncs its held-inventory view from the venue-truth positions in the
    state snapshot (the SAME source the gate clamps against), so it emits a SELL intent only once we
    hold YES, sized to the holding — turning accumulation into round-trips in the live path too."""
    from sig.engine.live_strategies import CoreMarketMakingStrategy

    # equity is REQUIRED now: capital-% sizing has no fallback share count.
    mm = CoreMarketMakingStrategy(store=None, config={"sizing": {"k_reference_sigma": 0.1}},
                                  exchange_ids=["YES_X"], equity=100_000.0)
    # `updated_at` is REQUIRED now: the MM quotes only FRESH books, and a row whose age cannot be
    # established reads as stale (fail closed). See tests/test_mm_order_volume.py.
    mm.set_prices({"YES_X": {"best_bid": 0.46, "best_ask": 0.54,
                             "updated_at": datetime.now(timezone.utc).isoformat()}})

    flat = mm.evaluate({"positions": {}})                        # no holdings ⇒ bid only
    assert flat and {i.side for i in flat} == {"buy"}

    held = mm.evaluate({"positions": {"YES_X": {"quantity": 300}}})   # long 300 ⇒ ask appears
    sides = {i.side for i in held}
    assert "buy" in sides and "sell" in sides
    sell = next(i for i in held if i.side == "sell")
    assert 0 < int(sell.metadata["mm_quote_qty"]) <= 300         # sell intent sized to the holding
