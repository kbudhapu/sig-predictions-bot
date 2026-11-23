"""The multi-source sharp-benchmark agreement gate: only trade a SIG-vs-benchmark divergence when >=2
independent REAL-MONEY sources agree. Pins the rule that would have blocked the New Hampshire loss."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.sharp_benchmark import evaluate, SourceQuote as Q


def test_two_sharp_sources_agree_with_edge_is_tradeable():
    v = evaluate(0.70, [Q("polymarket", 0.84), Q("kalshi", 0.85)], min_divergence=0.05)
    assert v.tradeable and v.reason == "tradeable"
    assert abs(v.agreed_sharp - 0.845) < 1e-6 and v.divergence > 0   # SIG too low -> buy toward sharp


def test_wiki_only_is_blocked_the_nh_failure():
    # Wikipedia is NOT a sharp source, so a lone wiki benchmark yields zero sharp sources -> no trade.
    v = evaluate(0.84, [Q("wikipedia", 0.72)], min_divergence=0.05)
    assert not v.tradeable and v.reason == "insufficient_sources"


def test_single_sharp_source_is_blocked():
    v = evaluate(0.70, [Q("polymarket", 0.84)], min_sources=2)
    assert not v.tradeable and v.reason == "insufficient_sources"


def test_sharp_sources_disagree_stand_down():
    v = evaluate(0.70, [Q("polymarket", 0.84), Q("kalshi", 0.60)], agree_tol=0.03)
    assert not v.tradeable and v.reason == "sources_disagree" and v.spread > 0.03


def test_agree_but_below_min_divergence():
    v = evaluate(0.82, [Q("polymarket", 0.84), Q("kalshi", 0.83)], min_divergence=0.05)
    assert not v.tradeable and v.reason == "below_min_divergence"


def test_stale_source_dropped():
    v = evaluate(0.70, [Q("polymarket", 0.84, age_s=5), Q("kalshi", 0.85, age_s=999)], max_age_s=120)
    assert not v.tradeable and v.reason == "insufficient_sources" and v.n_sources == 1


def test_nh_realistic_both_sharp_agree_with_sig_no_trade():
    # The true NH outcome: sharp sources ~0.82-0.83, SIG 0.84 -> no material edge -> no trade.
    v = evaluate(0.84, [Q("polymarket", 0.82), Q("kalshi", 0.83)], min_divergence=0.05)
    assert not v.tradeable


def test_missing_sig_price_not_tradeable():
    v = evaluate(None, [Q("polymarket", 0.84), Q("kalshi", 0.85)])
    assert not v.tradeable and v.reason == "no_sig_price"
