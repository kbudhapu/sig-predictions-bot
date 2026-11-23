"""Dutch-book complement arb: fires only when YES bids sum >1 by min_edge; legs are buy-NO at the
complement; disabled/one-sided/sub-edge cases produce nothing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.dutch_book import DutchBookConfig, evaluate_pair


def _cfg(**kw):
    return DutchBookConfig(enabled=True, min_edge=0.01, **kw)


def test_short_both_fires_when_bids_sum_over_one():
    a = evaluate_pair("iowa senate", "100", "101", 0.477, 0.587, _cfg())
    assert a is not None and a.edge == round(0.064, 4)
    d, r = a.legs
    assert d.outcome == "no" and r.outcome == "no"            # short both = buy NO on each
    assert abs(d.price - 0.525) < 1e-9 and abs(r.price - 0.415) < 1e-9  # 1 - yes_bid, snapped


def test_no_arb_when_sum_at_or_below_one():
    assert evaluate_pair("x", "1", "2", 0.50, 0.49, _cfg()) is None     # sum 0.99 < 1
    assert evaluate_pair("x", "1", "2", 0.50, 0.505, _cfg()) is None    # edge 0.005 < min_edge 0.01


def test_disabled_produces_nothing():
    assert evaluate_pair("x", "1", "2", 0.6, 0.6, DutchBookConfig(enabled=False, min_edge=0.01)) is None


def test_missing_or_degenerate_bid_skipped():
    assert evaluate_pair("x", "1", "2", None, 0.6, _cfg()) is None
    assert evaluate_pair("x", "1", "2", 0.6, 1.0, _cfg()) is None        # 1.0 not a valid interior bid


def test_edge_scales_with_overpricing():
    small = evaluate_pair("x", "1", "2", 0.52, 0.50, _cfg())             # sum 1.02 -> edge 0.02
    big = evaluate_pair("x", "1", "2", 0.72, 0.72, _cfg())               # sum 1.44 -> edge 0.44
    assert small.edge == 0.02 and big.edge == round(0.44, 4)
    assert big.edge > small.edge
