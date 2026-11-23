"""Kalshi second benchmark (2026-10-07): dollar-field parsing, party-verified mapping, shadow rows + storage."""
from __future__ import annotations

import asyncio
import sqlite3

from signals.kalshi_client import parse_quote
from tools.build_kalshi_map import match, sig_race


def test_parse_quote_dollar_fields_and_cent_fallback():
    q = parse_quote({"yes_bid_dollars": "0.3400", "yes_ask_dollars": "0.3500", "last_price_dollars": "0.3400",
                     "volume_fp": "11347727.00", "volume_24h_fp": "276382.10"})
    assert abs(q["mid"] - 0.345) < 1e-9 and q["volume"] == 11347727.0 and q["volume_24h"] == 276382.1
    old = parse_quote({"yes_bid": 34, "yes_ask": 35, "last_price": 34})            # legacy integer cents
    assert abs(old["mid"] - 0.345) < 1e-9
    one_sided = parse_quote({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0500", "last_price_dollars": "0.0300"})
    assert one_sided["mid"] == 0.03                                                   # no bid -> last price


def _mkt(ticker, sub, rules):
    return {"ticker": ticker, "subtitle": sub, "rules_primary": rules, "yes_sub_title": "X"}


EVENTS = {
    "SENATETX-26": {"markets": [
        _mkt("SENATETX-26-R", "Republican party:: Republican party", "If a representative of the Republican party is sworn in"),
        _mkt("SENATETX-26-D", "Democratic party:: Democratic party", "If a representative of the Democratic party is sworn in")]},
    "GOVPARTYGA-26": {"markets": [
        _mkt("GOVPARTYGA-26-D", "Democratic:: Democratic party", "If a representative of the Democratic party is inaugurated"),
        _mkt("GOVPARTYGA-26-R", "Republican:: Republican party", "If a representative of the Republican party is inaugurated")]},
    "KXHOUSERACE-SC01-26": {"markets": [
        _mkt("KXHOUSERACE-SC01-26-D", "Democratic party:: Democratic party", "is a member of the Democratic Party, the"),
        _mkt("KXHOUSERACE-SC01-26-R", "Republican party:: Republican party", "is a member of the Republican Party, the")]},
    "SENATEDE-26": {"markets": [
        _mkt("SENATEDE-26-R", "::", "If a representative of the Republican party is sworn in"),
        _mkt("SENATEDE-26-D", ":: Current incumbent: Chris Coons", "If a representative of the Democratic party is sworn in")]},
    "SENATEOH-26": {"markets": []}, "SENATEOHS-26": {"markets": []},
}


def test_sig_race_parsing():
    assert sig_race("texas_senate_dem") == ("senate", "TX", None, "dem")
    assert sig_race("sc_01_house_rep") == ("house", "SC", "01", "rep")
    assert sig_race("us_senate_control_rep") is None


def test_match_is_party_verified_across_formats():
    assert match("texas_senate_dem", EVENTS)["ticker"] == "SENATETX-26-D"
    assert match("texas_senate_rep", EVENTS)["ticker"] == "SENATETX-26-R"
    assert match("georgia_governor_rep", EVENTS)["ticker"] == "GOVPARTYGA-26-R"     # "Republican::" subtitle form
    assert match("sc_01_house_dem", EVENTS)["ticker"] == "KXHOUSERACE-SC01-26-D"    # "Democratic Party" in rules
    assert match("delaware_senate_dem", EVENTS)["ticker"] == "SENATEDE-26-D"       # blank subtitle, rules decide
    assert match("ohio_senate_dem", EVENTS)["status"] == "ambiguous_events"        # regular + special -> never guess
    assert match("kansas_governor_dem", EVENTS)["status"] == "no_kalshi_event"


def test_match_rejects_contradicting_ticker_suffix():
    ev = {"SENATEXX-26": {"markets": [_mkt("SENATEXX-26-R", "Democratic party:: x", "Democratic party sworn in")]}}
    ev["SENATEZZ-26"] = ev.pop("SENATEXX-26")
    assert match("zz_unknown", ev)["status"] == "unparsed"
    ev2 = {"SENATETX-26": {"markets": [_mkt("SENATETX-26-R", "Democratic party:: x", "Democratic party sworn in")]}}
    assert match("texas_senate_dem", ev2)["status"] == "party_markets_0"           # -R suffix contradicts Dem


def test_kalshi_rows_and_store(tmp_path):
    import main_sig
    from infra.state.store import StateStore
    legs = [{"exchange_id": 982, "ticker": "SENATETX-26-D"}, {"exchange_id": 1, "ticker": "MISSING"}]
    mk = {"SENATETX-26-D": {"yes_bid_dollars": "0.65", "yes_ask_dollars": "0.66", "volume_24h_fp": "5"}}
    rows = main_sig._kalshi_rows(legs, mk, "2026-10-07T13:00:00+00:00")
    assert len(rows) == 1 and rows[0]["exchange_id"] == "982" and abs(rows[0]["mid"] - 0.655) < 1e-9

    async def _run():
        st = await StateStore(tmp_path / "k.db").open()
        try:
            await st.append_kalshi_history(rows)
            await st.append_kalshi_history(rows)                                   # idempotent
            return await st.prune_kalshi_history("2026-10-07T12:00:00+00:00")
        finally:
            await st.close()
    assert asyncio.run(_run()) == 0
    assert sqlite3.connect(tmp_path / "k.db").execute("select count(*), mid from kalshi_history").fetchone()[0] == 1
