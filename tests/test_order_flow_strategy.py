"""CoreOrderFlowStrategy — the live order-flow fade wrapper. No network.

Proves the wiring + the module's safety survive the adapter:
  • a one-sided book extreme that OPPOSES the informed view (realized momentum) → DUMB → a directional
    fade Intent (correct side, strategy_type="order_flow", sigma_p attached for the Browne sizer);
  • an extreme that AGREES with the view → INFORMED → NO intent (don't-fade-informed safety);
  • an extreme with no informed reference → UNCONFIRMED → NO intent;
  • the enable flag and the min_confidence gate;
  • the order-book depth/mid/touch helpers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.engine.live_strategies import (
    CoreOrderFlowStrategy, _book_depth, _book_mid, _book_touch_price,
)


def _cfg(**overrides):
    of = {
        "enabled": True, "k_reference_sigma": 0.25, "extreme_threshold": 0.6,
        "window_seconds": 60.0, "min_volume": 10.0, "min_trades": 1,
        "momentum_window_s": 45.0, "momentum_min_move": 0.01, "min_confidence": 0.3,
        "max_markets": 20,
    }
    of.update(overrides)
    return {"order_flow": of}


def _book(bid_qty, ask_qty, *, bid=0.49, ask=0.51):
    return {"bids": [{"price": bid, "quantity": bid_qty}],
            "asks": [{"price": ask, "quantity": ask_qty}],
            "bestBid": bid, "bestAsk": ask}


# ── helpers ──────────────────────────────────────────────────────────────────
def test_book_depth_handles_dict_and_pair_levels():
    assert _book_depth({"bids": [{"price": 0.4, "quantity": 100}, [0.39, 50]],
                        "asks": [{"price": 0.6, "size": 20}]}) == (150.0, 20.0)
    assert _book_depth({"bids": [], "asks": []}) == (0.0, 0.0)


def test_book_mid_and_touch():
    b = _book(100, 100, bid=0.48, ask=0.52)
    assert _book_mid(b, None) == 0.50
    assert _book_touch_price(b, "buy", 0.50) == 0.52   # fade-buy lifts the ask
    assert _book_touch_price(b, "sell", 0.50) == 0.48  # fade-sell hits the bid


# ── fade / informed / unconfirmed ────────────────────────────────────────────
def test_fade_buy_on_dumb_flow_opposing_a_rising_market():
    """Down-pressure book (asks >> bids) WHILE the mid is rising (informed up) ⇒ DUMB ⇒ fade = BUY."""
    of = CoreOrderFlowStrategy(store=None, config=_cfg(), exchange_ids=["X"])
    of.set_books({"X": _book(100, 2000, bid=0.48, ask=0.52)})   # imbalance ≈ -0.90 (down pressure)
    assert of.evaluate({}) == []                                # 1st pass: only 1 mid sample ⇒ UNCONFIRMED
    of.set_books({"X": _book(100, 2000, bid=0.52, ask=0.56)})   # mid 0.50 → 0.54 (rising = informed +1)
    intents = of.evaluate({})
    assert len(intents) == 1
    it = intents[0]
    assert it.side == "buy" and it.strategy_type == "order_flow" and it.exchange_id == "X"
    assert it.metadata["sigma_p"] == 0.25                       # conviction wired for the Browne sizer
    assert it.metadata["option_side"] == "yes"
    assert it.metadata["of_classification"] == "DUMB"
    assert it.price == 0.56                                     # marketable: lifts the ask


def test_no_fade_on_informed_flow_agreeing_with_the_move():
    """Down-pressure book WHILE the mid is also falling (informed down) ⇒ INFORMED ⇒ no fade."""
    of = CoreOrderFlowStrategy(store=None, config=_cfg(), exchange_ids=["X"])
    of.set_books({"X": _book(100, 2000, bid=0.52, ask=0.56)})   # mid 0.54
    assert of.evaluate({}) == []
    of.set_books({"X": _book(100, 2000, bid=0.48, ask=0.52)})   # mid 0.54 → 0.50 (falling = informed -1)
    assert of.evaluate({}) == []                                # flow down == informed down ⇒ INFORMED ⇒ hold


def test_no_fade_when_unconfirmed_no_reference():
    """Extreme book but a FLAT mid (no informed direction) ⇒ UNCONFIRMED ⇒ no fade (needs confirmation)."""
    of = CoreOrderFlowStrategy(store=None, config=_cfg(), exchange_ids=["X"])
    b = _book(100, 2000, bid=0.48, ask=0.52)                    # mid never moves
    of.set_books({"X": b}); assert of.evaluate({}) == []
    of.set_books({"X": b}); assert of.evaluate({}) == []


def test_disabled_emits_nothing():
    of = CoreOrderFlowStrategy(store=None, config=_cfg(enabled=False), exchange_ids=["X"])
    of.set_books({"X": _book(100, 2000, bid=0.48, ask=0.52)})
    of.set_books({"X": _book(100, 2000, bid=0.52, ask=0.56)})
    assert of.evaluate({}) == []


def test_min_confidence_gate_blocks_weak_fades():
    """Same DUMB setup, but a high confidence floor drops the signal (it still classifies, just no intent)."""
    of = CoreOrderFlowStrategy(store=None, config=_cfg(min_confidence=0.99), exchange_ids=["X"])
    of.set_books({"X": _book(100, 2000, bid=0.48, ask=0.52)}); of.evaluate({})
    of.set_books({"X": _book(100, 2000, bid=0.52, ask=0.56)})
    assert of.evaluate({}) == []                                # conf ≈ 0.54 < 0.99 ⇒ no intent


def test_not_quoted_until_books_pumped():
    of = CoreOrderFlowStrategy(store=None, config=_cfg(), exchange_ids=["X"])
    assert of.evaluate({}) == []                                # no books ⇒ nothing
    assert of.tier == "SIGNAL" and of.name == "order_flow"
