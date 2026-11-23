"""Chase price-laddering: each attempt walks one tick toward liquidity (sell down, buy up), bounded."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.oms.fill_handler import _chase_price


def test_sell_walks_down_one_tick_per_attempt():
    assert _chase_price(0.85, "sell", 1) == 0.845
    assert _chase_price(0.85, "sell", 2) == 0.84
    assert _chase_price(0.85, "sell", 3) == 0.835


def test_sell_capped_at_max_walk():
    # 10 ticks would be 0.05 but cap is 0.03 → 0.85 - 0.03 = 0.82
    assert _chase_price(0.85, "sell", 10) == 0.82


def test_buy_walks_up_and_caps():
    assert _chase_price(0.10, "buy", 1) == 0.105
    assert _chase_price(0.10, "buy", 10) == 0.13


def test_clamps_to_price_band():
    assert _chase_price(0.01, "sell", 10) == 0.005     # floor
    assert _chase_price(0.99, "buy", 10) == 0.995      # ceiling


def test_none_and_unknown_side():
    assert _chase_price(None, "sell", 1) is None
    assert _chase_price(0.50, "", 3) == 0.50           # unknown direction → unchanged (snapped)
