"""Cross-market's THESIS-BASED exit (distinct from MM's price stop):
  • HOLD a losing position while the sharp benchmark still backs it (price drop ≠ mistake);
  • TAKE PROFIT when SIG converges to the sharp price (the gap closes — thesis realized);
  • cut only on a WIDE hard backstop (≥ MM's 0.20), never a tight trailing stop (noise > gap);
  • positions on races it does NOT own are left to the caller's default (MM) stop.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.mapping import load_race_mapping
from signals.wiki_ratings import RatingEstimate
from sig.engine.cross_market_provider import CrossMarketProvider

HOUSE_D = "840"   # inverted race: RAW poly ~0.075 -> SIG-aligned ~0.925


def _provider():
    mapping = load_race_mapping()
    p = CrossMarketProvider(mapping=mapping, store=None, sig_client=None,
                            convergence_tol=0.01, exit_hard_stop_pct=0.25, exit_support_conviction=0.30)
    return p


def _seed(p, sig_exchange_id, *, raw_poly, sig_mid):
    now = time.monotonic()
    p._poly_cache[sig_exchange_id] = {"polymarket": (raw_poly, now)}       # RAW venue YES (resolver flips)
    half = 0.0025
    p._fresh_sig[sig_exchange_id] = (sig_mid - half, sig_mid + half, now)  # fresh SIG bid/ask


def test_aligned_benchmark_is_flipped_for_inverted_race():
    p = _provider()
    _seed(p, HOUSE_D, raw_poly=0.075, sig_mid=0.89)
    aligned = p.aligned_benchmark(HOUSE_D)
    assert aligned is not None and abs(aligned - 0.925) < 1e-6   # 1 - 0.075, flipped ONCE


def test_hold_while_benchmark_backs_despite_loss():
    """Down -12% but the sharp benchmark (0.925) still strongly backs the long -> HOLD, do not cut."""
    p = _provider()
    _seed(p, HOUSE_D, raw_poly=0.075, sig_mid=0.88)   # gap still open (aligned 0.925 vs 0.88)
    handled, reason = p.exit_decision(HOUSE_D, pnl_frac=-0.12, is_long=True)
    assert handled is True
    assert reason is None   # benchmark_still_backs -> hold through the noise


def test_convergence_take_profit():
    """SIG reached the sharp price (gap within tol) -> take profit, free the capital."""
    p = _provider()
    _seed(p, HOUSE_D, raw_poly=0.075, sig_mid=0.92)   # aligned 0.925, |0.92-0.925|=0.005 <= 0.01
    handled, reason = p.exit_decision(HOUSE_D, pnl_frac=0.03, is_long=True)
    assert handled is True and reason == "convergence_take_profit"


def test_wide_hard_backstop_cuts_catastrophe():
    """Below the WIDE -25% backstop, cut regardless of what the benchmark says."""
    p = _provider()
    _seed(p, HOUSE_D, raw_poly=0.075, sig_mid=0.80)
    handled, reason = p.exit_decision(HOUSE_D, pnl_frac=-0.26, is_long=True)
    assert handled is True and reason == "hard_stop_backstop"


def test_mm_stop_would_have_fired_but_thesis_holds():
    """At -22% (past MM's -20% but inside the wide -25% backstop), the thesis exit HOLDS while the
    benchmark backs it — the whole point: a tighter MM stop would have cut this winner-in-waiting."""
    p = _provider()
    _seed(p, HOUSE_D, raw_poly=0.075, sig_mid=0.78)   # aligned 0.925 still backs the long
    handled, reason = p.exit_decision(HOUSE_D, pnl_frac=-0.22, is_long=True)
    assert handled is True and reason is None


def test_long_thesis_reversed_cuts():
    """Long YES but SIG has risen ABOVE the sharp price (gap flipped negative) -> thesis broken -> cut."""
    p = _provider()
    _seed(p, "843", raw_poly=0.52, sig_mid=0.60)   # aligned 0.52, SIG 0.60 -> gap -0.08 (reversed for a long)
    handled, reason = p.exit_decision("843", pnl_frac=-0.10, is_long=True)
    assert handled is True and reason == "thesis_reversed"


# ── SHORT side (buy-NO ≡ sell-YES): held NO, thesis = SIG above sharp, room to FALL ──
def test_short_holds_while_sig_above_sharp():
    """Held NO (short YES) because SIG (0.11) is ABOVE sharp (0.075); gap<0 still backs the short -> HOLD."""
    p = _provider()
    _seed(p, "843", raw_poly=0.075, sig_mid=0.11)   # aligned 0.075, SIG 0.11 -> gap -0.035 (short-backed)
    handled, reason = p.exit_decision("843", pnl_frac=-0.08, is_long=False)
    assert handled is True and reason is None


def test_short_convergence_take_profit():
    """Short YES and SIG fell to the sharp price (gap closed) -> take profit."""
    p = _provider()
    _seed(p, "843", raw_poly=0.075, sig_mid=0.08)   # |0.075-0.08| = 0.005 <= tol
    handled, reason = p.exit_decision("843", pnl_frac=0.04, is_long=False)
    assert handled is True and reason == "convergence_take_profit"


def test_short_thesis_reversed_cuts():
    """Short YES but SIG fell BELOW sharp (gap flipped positive) -> overshot/reversed -> cut."""
    p = _provider()
    # aligned 0.075, SIG 0.015 -> gap +0.060: reversed for a short AND beyond the 0.05 thesis-reversal noise
    # band (a gap inside the band now HOLDs — the whipsaw fix; this test predated the band).
    _seed(p, "843", raw_poly=0.075, sig_mid=0.015)
    handled, reason = p.exit_decision("843", pnl_frac=0.02, is_long=False)
    assert handled is True and reason == "thesis_reversed"


def test_unowned_exchange_not_handled():
    """A race cross-market does not trade -> (False, None) so the caller uses its default MM stop."""
    p = _provider()
    handled, reason = p.exit_decision("999999", pnl_frac=-0.30, is_long=True)
    assert handled is False and reason is None


# ── Wikipedia SECONDARY benchmark injection (the ~71-seat coverage bridge) ──
def _est(office, state, p_rep):
    return RatingEstimate(key=f"{office}:{state}", office=office, state=state,
                          p_rep=round(p_rep, 4), p_dem=round(1 - p_rep, 4), n_raters=4, raters={})


def test_wiki_injection_adds_active_races_and_respects_polymarket_primary():
    p = _provider()
    assert p._by_sig_ex, "mapping should have active Polymarket races"
    markets = [
        {"title": "Will the Republican Party win the Montana Senate?", "exchanges": [{"id": "7001"}]},
        {"title": "Will the Democratic Party win the Kansas Governor?", "exchanges": [{"id": "7002"}]},
        # SAME exchange id as an existing Polymarket race (840) → must be SKIPPED (Polymarket PRIMARY)
        {"title": "Will the Republican Party win the Montana Senate?", "exchanges": [{"id": HOUSE_D}]},
    ]
    estimates = [_est("senate", "montana", 0.80), _est("governor", "kansas", 0.30)]
    p._inject_wiki_races(markets, estimates)
    # new wiki races injected, ACTIVE, with a usable wikipedia venue
    assert p._by_sig_ex["7001"].key.startswith("wiki:") and p._by_sig_ex["7001"].is_active()
    assert "wikipedia" in p._by_sig_ex["7001"].usable_externals()
    assert p._wiki_benchmarks["7001"]["fair"] == 0.80                 # R leg → p_rep (DIRECT, no flip)
    assert p._wiki_benchmarks["7002"]["fair"] == pytest.approx(0.70)  # D leg → p_dem = 1 − 0.30
    # overlap: 840 stays the ORIGINAL Polymarket race, never overwritten by wiki
    assert not p._by_sig_ex[HOUSE_D].key.startswith("wiki:")
    assert HOUSE_D not in p._wiki_benchmarks


def test_wiki_quote_served_direct_polarity_and_exit_uses_coarse_quality():
    p = _provider()
    p._inject_wiki_races(
        [{"title": "Will the Republican Party win the Montana Senate?", "exchanges": [{"id": "7001"}]}],
        [_est("senate", "montana", 0.80)])
    p._poly_cache.setdefault("7001", {})["wikipedia"] = (0.80, time.monotonic())  # what _refresh_wiki re-stamps
    q = p.external_provider("7001")
    assert q and "wikipedia" in q and abs(q["wikipedia"].yes_price - 0.80) < 1e-9
    assert p.aligned_benchmark("7001") == pytest.approx(0.80)         # direct polarity → no flip
    # a wiki race IS owned → exit routes here (coarse quality drives conviction_gated_stop)
    handled, _ = p.exit_decision("7001", pnl_frac=-0.05, is_long=True)
    assert handled is True
