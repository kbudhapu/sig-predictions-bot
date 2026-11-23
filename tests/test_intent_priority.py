"""Placement priority: the scarce ~10-13 orders/min pipe must rank exits > arb/dutch-book >
cross-market > order-flow > MR > MM, refined by edge size — so a flood of MM quotes can never bury a
high-value edge behind it (the FIFO starvation that forced cutting MM)."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.engine.orchestrator import _intent_priority


def _i(**kw):
    kw.setdefault("metadata", {})
    kw.setdefault("strategy_type", "")
    kw.setdefault("strategy_id", "")
    kw.setdefault("action", "open")
    kw.setdefault("signal_strength", 0.0)
    return NS(**kw)


def test_tier_ordering():
    exit_i = _i(action="close", strategy_id="exit:840")
    dutch = _i(strategy_type="arb", strategy_id="dutch_book", metadata={"dutch_book": True, "edge": 0.03})
    cross = _i(strategy_id="cross_market_divergence", strategy_type="directional", metadata={"divergence": 0.08})
    oflow = _i(strategy_id="order_flow", metadata={"of_confidence": 0.5})
    mr = _i(strategy_id="mean_reversion", metadata={"mr_z": 2.1})
    mm = _i(strategy_type="market_making", metadata={"mm_quote_qty": 500})
    p = [_intent_priority(x) for x in (exit_i, dutch, cross, oflow, mr, mm)]
    assert p == sorted(p, reverse=True), f"tiers out of order: {p}"
    assert p[0] > p[1] > p[2] > p[3] > p[4] > p[5]


def test_higher_edge_ranks_higher_within_tier():
    small = _i(strategy_type="arb", strategy_id="dutch_book", metadata={"dutch_book": True, "edge": 0.01})
    big = _i(strategy_type="arb", strategy_id="dutch_book", metadata={"dutch_book": True, "edge": 0.05})
    assert _intent_priority(big) > _intent_priority(small)
    # cross-market: bigger divergence ranks higher
    cs = _i(strategy_id="cross_market_divergence", metadata={"divergence": 0.02})
    cb = _i(strategy_id="cross_market_divergence", metadata={"divergence": 0.10})
    assert _intent_priority(cb) > _intent_priority(cs)


def test_mm_requote_below_new_quote():
    newq = _i(strategy_type="market_making", metadata={"mm_quote_qty": 100})
    requote = _i(strategy_type="market_making", metadata={"mm_quote_qty": 100, "mm_requote": True})
    assert _intent_priority(newq) > _intent_priority(requote)


def test_mm_never_outranks_an_edge():
    mm_big = _i(strategy_type="market_making", metadata={"mm_quote_qty": 1_000_000})
    cross_tiny = _i(strategy_id="cross_market_divergence", metadata={"divergence": 0.0})
    assert _intent_priority(cross_tiny) > _intent_priority(mm_big)


def test_bad_metadata_does_not_raise():
    assert _intent_priority(_i(metadata=None)) > 0            # defensive default
