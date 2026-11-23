"""Order-book pressure (queue imbalance): bids-heavy => back YES, asks-heavy => fade YES, only past the
imbalance threshold, only on enough depth, with optional tape confirmation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.book_pressure import BookPressureConfig, evaluate


def _cfg(**kw):
    return BookPressureConfig(enabled=True, levels=5, min_imbalance=0.33, min_depth=100.0, **kw)


def _book(bid_q, ask_q):
    # single level each at plausible prices; quantities drive the imbalance
    return [{"price": 0.50, "quantity": bid_q}], [{"price": 0.51, "quantity": ask_q}]


def test_bid_heavy_backs_yes():
    bids, asks = _book(9000, 1000)  # QI = +0.8 → pressure up
    s = evaluate("840", bids, asks, _cfg())
    assert s and s.option_side == "yes" and s.reason == "bid_heavy" and s.qi > 0.33


def test_ask_heavy_fades_yes():
    bids, asks = _book(1000, 9000)  # QI = -0.8 → pressure down
    s = evaluate("841", bids, asks, _cfg())
    assert s and s.option_side == "no" and s.reason == "ask_heavy" and s.qi < -0.33


def test_balanced_book_no_signal():
    bids, asks = _book(5000, 5000)  # QI = 0
    assert evaluate("x", bids, asks, _cfg()) is None


def test_below_min_imbalance_no_signal():
    bids, asks = _book(1200, 1000)  # QI ≈ 0.09 < 0.33
    assert evaluate("x", bids, asks, _cfg()) is None


def test_too_thin_no_signal():
    bids, asks = _book(60, 10)  # lopsided but total 70 < min_depth 100 → noise
    assert evaluate("x", bids, asks, _cfg()) is None


def test_disabled_no_signal():
    bids, asks = _book(9000, 1000)
    assert evaluate("x", bids, asks, BookPressureConfig(enabled=False)) is None


def test_levels_sum_depth():
    # 3 bid levels vs 1 ask level; with levels=5 all count → bids 6000 vs ask 1000 → QI +0.71
    bids = [{"price": 0.5, "quantity": 2000}, {"price": 0.495, "quantity": 2000},
            {"price": 0.49, "quantity": 2000}]
    asks = [{"price": 0.51, "quantity": 1000}]
    s = evaluate("840", bids, asks, _cfg())
    assert s and s.option_side == "yes" and abs(s.bid_depth - 6000) < 1e-9


def test_tape_confirms_up():
    bids, asks = _book(9000, 1000)  # pressure up
    # tape agrees (YES-buying) → signal stands
    s = evaluate("840", bids, asks, _cfg(tape_confirm=True, tape_min=0.20), tape_imbalance=0.5)
    assert s and s.option_side == "yes"


def test_tape_contradiction_vetoes():
    bids, asks = _book(9000, 1000)  # book says up
    # tape runs the other way (heavy NO-buying) → veto
    assert evaluate("840", bids, asks, _cfg(tape_confirm=True, tape_min=0.20), tape_imbalance=-0.5) is None


def test_tape_flat_vetoes_when_confirm_required():
    bids, asks = _book(1000, 9000)  # book says down
    # flat tape, confirmation required → veto (no agreeing flow)
    assert evaluate("840", bids, asks, _cfg(tape_confirm=True, tape_min=0.20), tape_imbalance=0.0) is None
