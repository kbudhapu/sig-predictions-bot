"""Election cash-reserve sell-down selection (tools/sell_down.select_markets)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.sell_down import select_markets  # noqa: E402

ROWS = [{"ex": "a", "edge": 0.08, "usd": 5000, "owner": None},
        {"ex": "b", "edge": 0.04, "usd": 3000, "owner": None},
        {"ex": "c", "edge": 0.05, "usd": 4000, "owner": "parity"},        # owned by a pilot: never
        {"ex": "d", "edge": None, "usd": 900, "owner": None},             # no fair: never
        {"ex": "e", "edge": 0.06, "usd": 2000, "owner": None},
        {"ex": "f", "edge": 0.12, "usd": 9000, "owner": None}]            # too much edge left: never


def test_lowest_edge_first_until_target():
    assert select_markets(ROWS, cash=1000, target=5500, min_edge_skip=0.10) == ["b", "e"]


def test_stops_before_high_edge_and_skips_owned_and_unknown():
    assert select_markets(ROWS, cash=0, target=1e9, min_edge_skip=0.10) == ["b", "e", "a"]


def test_nothing_when_cash_already_covers_target():
    assert select_markets(ROWS, cash=40000, target=40000) == []
