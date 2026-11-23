"""Cross-market resolver: polarity flip + confidence/resolution/liquidity gate. No network.

Proves the two properties the task hinges on:
  1. an INVERTED-polarity pair has the external price FLIPPED (1 − p) before the divergence compare — so
     the cross-market divergence has the CORRECT sign (mapping it raw would be backwards);
  2. an unmapped / low-confidence / resolution-mismatched / ILLIQUID race yields NO cross-market signal.
Plus a guard that the shipped race_mapping.yaml parses and currently activates NOTHING (all externals are
price-dead at build time — a WRONG mapping is worse than NO mapping).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import pytest

from signals.mapping import (
    RaceMapping,
    ExternalRef,
    load_race_mapping,
    _DEFAULT_PATH,
)
from signals.cross_market import (
    apply_polarity,
    resolve_external_prices,
    cross_market_divergence_for_race,
)


def _race(*, confidence, resolution_match, externals):
    return RaceMapping(
        key="r", description="", resolution_match=resolution_match,
        confidence=confidence, externals=externals,
        sig_market_id="1", sig_exchange_id="2",
    )


def _ext(venue, polarity, liquidity_ok, **kw):
    return ExternalRef(venue=venue, polarity=polarity, liquidity_ok=liquidity_ok,
                       ticker=kw.get("ticker", f"{venue.upper()}-X"), **{k: v for k, v in kw.items() if k != "ticker"})


# ── 1. polarity primitive ────────────────────────────────────────────────────
def test_apply_polarity_direct_and_inverted():
    assert apply_polarity(0.30, "direct") == 0.30
    assert math.isclose(apply_polarity(0.30, "inverted"), 0.70)
    assert math.isclose(apply_polarity(0.72, "inverted"), 0.28)


def test_apply_polarity_rejects_bad_flag_and_non_probability():
    with pytest.raises(ValueError):
        apply_polarity(0.5, "flip")
    with pytest.raises(ValueError):
        apply_polarity(1.3, "direct")


# ── 2. INVERTED polarity flips the external price BEFORE the compare ──────────
def test_inverted_polarity_flips_external_before_divergence():
    # SIG market = "Republican wins X" priced at p_m_sig = 0.68. The external venue only quotes the
    # DEMOCRAT leg at 0.30 (so the implied Republican price is 1 − 0.30 = 0.70). polarity: inverted.
    race = _race(confidence="high", resolution_match=True,
                 externals={"kalshi": _ext("kalshi", "inverted", True)})

    real = resolve_external_prices(race, {"kalshi": 0.30})
    assert math.isclose(real["kalshi"], 0.70)                 # flipped, NOT 0.30

    d = cross_market_divergence_for_race(race, p=0.69, p_m_sig=0.68, raw_external_prices={"kalshi": 0.30})
    # cross = p_m_sig − external_YES = 0.68 − 0.70 = −0.02  (SIG a hair BELOW the venue)
    assert math.isclose(d.cross_market["kalshi"], -0.02, abs_tol=1e-9)
    # if the flip had NOT been applied it would be 0.68 − 0.30 = +0.38 — opposite sign, a fake 38pt edge.
    assert d.cross_market["kalshi"] < 0


def test_direct_polarity_passes_price_through():
    race = _race(confidence="high", resolution_match=True,
                 externals={"kalshi": _ext("kalshi", "direct", True)})
    d = cross_market_divergence_for_race(race, p=0.61, p_m_sig=0.60, raw_external_prices={"kalshi": 0.71})
    assert math.isclose(d.cross_market["kalshi"], 0.60 - 0.71, abs_tol=1e-9)


# ── 3. the gate: unmapped / low-confidence / mismatch / illiquid → NO comparable ──
def test_low_confidence_race_yields_no_cross_signal():
    race = _race(confidence="low", resolution_match=True,
                 externals={"kalshi": _ext("kalshi", "direct", True)})
    assert resolve_external_prices(race, {"kalshi": 0.71}) == {}
    d = cross_market_divergence_for_race(race, p=0.70, p_m_sig=0.60, raw_external_prices={"kalshi": 0.71})
    assert d.cross_market == {}                               # no venue comparable
    assert d.real_money_consensus_outlier is False            # cannot be an outlier with no venues


def test_resolution_mismatch_yields_no_cross_signal():
    # looks mappable and is liquid, but the two contracts resolve on DIFFERENT events (e.g. margin-of-victory)
    race = _race(confidence="high", resolution_match=False,
                 externals={"kalshi": _ext("kalshi", "direct", True)})
    assert resolve_external_prices(race, {"kalshi": 0.71}) == {}
    assert race.is_active() is False


def test_illiquid_external_is_excluded_even_at_high_confidence():
    race = _race(confidence="high", resolution_match=True,
                 externals={"kalshi": _ext("kalshi", "direct", False)})   # liquidity_ok False
    assert resolve_external_prices(race, {"kalshi": 0.71}) == {}
    assert race.is_active() is False


def test_missing_or_none_raw_price_is_dropped():
    race = _race(confidence="high", resolution_match=True,
                 externals={"kalshi": _ext("kalshi", "direct", True),
                            "polymarket": _ext("polymarket", "direct", True)})
    # kalshi present, polymarket price None (fetch failed) → only kalshi survives
    real = resolve_external_prices(race, {"kalshi": 0.71, "polymarket": None})
    assert real == {"kalshi": 0.71}


def test_active_pair_produces_a_cross_signal():
    race = _race(confidence="high", resolution_match=True,
                 externals={"kalshi": _ext("kalshi", "direct", True),
                            "polymarket": _ext("polymarket", "direct", True)})
    assert race.is_active() is True
    d = cross_market_divergence_for_race(
        race, p=0.70, p_m_sig=0.60, raw_external_prices={"kalshi": 0.70, "polymarket": 0.71})
    assert set(d.cross_market) == {"kalshi", "polymarket"}
    assert d.real_money_consensus_outlier is True             # both venues agree, SIG is the outlier


# ── 4. loader validation + shipped-file guard ─────────────────────────────────
def test_loader_rejects_bad_polarity(tmp_path):
    bad = tmp_path / "m.yaml"
    bad.write_text(
        "version: 2\nraces:\n  x:\n    confidence: low\n    resolution_match: true\n"
        "    kalshi: {ticker: K, polarity: sideways, liquidity_ok: false}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_race_mapping(str(bad))


def test_shipped_mapping_parses_and_activates_the_live_polymarket_races():
    """The real race_mapping.yaml parses, carries the verified SIG ids + Kalshi tickers, and ACTIVATES
    the 5 races whose Polymarket leg is live + polarity-verified (re-mapped 2026-10-02 from the droplet);
    the senate-seat races (no Polymarket comparable) and every Kalshi leg (still price-dead) stay dormant."""
    m = load_race_mapping(_DEFAULT_PATH)
    assert m, "mapping is empty"
    # headline entries present with their verified SIG ids + Kalshi tickers
    assert m["us_senate_control_rep"].sig_market_id == "154"
    assert m["us_senate_control_rep"].kalshi.ticker == "CONTROLS-2026-R"
    assert m["us_senate_control_rep"].kalshi.polarity == "direct"
    # live Polymarket legs carry the gamma market id + a verified polarity
    assert m["us_senate_control_rep"].polymarket.market_id == "562794"
    assert m["us_senate_control_rep"].polymarket.polarity == "direct"
    assert m["us_senate_control_rep"].polymarket.liquidity_ok is True
    # the inverted exemplar: SIG Democrat-controls-House ↔ the Republican-House market, read INVERTED
    assert m["us_house_control_dem"].polymarket.polarity == "inverted"
    assert m["us_house_control_dem"].polymarket.market_id == "562803"
    # ACTIVE includes the 5 control/GA races + the 24 Polymarket governor races (subset check: the set grows
    # as more polarity-verified races are added).
    active = {k for k, r in m.items() if r.is_active()}
    assert {"us_senate_control_rep", "us_senate_control_dem", "us_house_control_rep",
            "us_house_control_dem", "georgia_governor_rep"} <= active
    assert "alabama_governor_rep" in active and "new_hampshire_governor_rep" in active   # governor expansion
    # DORMANT: no liquid external (Polymarket has no senate-seat market; Kalshi empty)
    assert not m["georgia_senate_rep"].is_active()
    assert not m["texas_senate_rep"].is_active()
    # SAFETY: every Kalshi leg is still illiquid (price-dead) → never the sole thing activating a race
    assert all(e.liquidity_ok is False for r in m.values()
               for v, e in r.externals.items() if v == "kalshi")
    # Resolver gate: an ACTIVE race surfaces ONLY its liquid Polymarket leg (Kalshi illiquid → dropped);
    # a DORMANT race surfaces nothing at all.
    assert resolve_external_prices(
        m["us_senate_control_rep"], {"kalshi": 0.5, "polymarket": 0.365}) == {"polymarket": 0.365}
    assert resolve_external_prices(
        m["georgia_senate_rep"], {"kalshi": 0.5, "polymarket": 0.5}) == {}
