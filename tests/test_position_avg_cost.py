"""Position parsing: avg entry price must come from SIG's real `avgCost`, never 0.0.

Live failure this pins: `_position_from_row` looked for averagePrice / avgPrice / avgEntryPrice /
price / cost — none of which SIG sends. The actual Position object is

    {avgCost, costBasis, currentPrice, exchangeId, lots, marketId, marketTitle, marketValue,
     moneyEarned, option, quantity, settled, unrealizedPnl, unrealizedPnlPct}

so every lookup missed and it fell through to 0.0. All 104 live positions were parsed with
avg_entry_price = 0.0, and because fill_handler / reconciliation / _sync_venue_positions persist
that value, every row in the positions table carried 0.0. Two silent consequences:

  * the orchestrator's price-stream exit guards `if avg <= 0: continue`, so it skipped EVERY
    position and the fallback trailing stop never evaluated anything;
  * StateStore.mark_to_market computes quantity * (best_bid - avg_price), which against a zero avg
    reports the whole market value as unrealised profit.

Note what was NOT broken: the positions READ itself. `_as_rows` already falls through to the
`positions` response key, so 104 rows were being returned all along.
"""
from __future__ import annotations

import pytest

from sig.broker import _as_rows, _position_from_row

# A real row, copied from the live /tournaments/{slug}/portfolio/positions response.
LIVE_ROW = {
    "exchangeId": "879", "marketId": "190",
    "marketTitle": "Will the Republican Party win the Pennsylvania Governor?",
    "option": "YES", "settled": False, "quantity": 100, "avgCost": 0.04,
    "currentPrice": 0.239048, "marketValue": 23.9, "costBasis": 4.0,
    "moneyEarned": 0.0, "unrealizedPnl": 19.9, "unrealizedPnlPct": 497.62, "lots": [],
}


def test_avg_entry_price_comes_from_avgCost():
    """THE fix. One missing field name zeroed every position in the book."""
    p = _position_from_row(LIVE_ROW)
    assert p.avg_entry_price == 0.04, "must read SIG's avgCost"
    assert p.contract_id == "879" and p.qty == 100.0 and p.side == "long"


def test_no_live_shaped_row_parses_to_a_zero_average():
    """Every live row has a nonzero avgCost; none may parse to 0.0."""
    rows = [
        {"exchangeId": "840", "quantity": 100, "avgCost": 0.905},
        {"exchangeId": "851", "quantity": 100, "avgCost": 0.03},
        {"exchangeId": "1072", "quantity": 548, "avgCost": 0.044791},
        {"exchangeId": "963", "quantity": 100, "avgCost": 0.015},
    ]
    parsed = [_position_from_row(r) for r in rows]
    assert all(p.avg_entry_price > 0.0 for p in parsed)
    assert [p.avg_entry_price for p in parsed] == [0.905, 0.03, 0.044791, 0.015]


def test_cost_basis_is_the_derivation_fallback():
    """If avgCost is ever absent, costBasis/quantity reconstructs the true average — cost basis is
    the money actually spent, so it cannot disagree with reality."""
    p = _position_from_row({"exchangeId": "1", "quantity": 400, "costBasis": 50.0})
    assert p.avg_entry_price == pytest.approx(0.125)


def test_a_genuinely_empty_row_still_yields_zero_not_a_crash():
    """Zero is only correct when there is genuinely nothing to derive from."""
    p = _position_from_row({"exchangeId": "1", "quantity": 0})
    assert p.avg_entry_price == 0.0 and p.side == "flat"


def test_legacy_field_names_still_work():
    """Other venues/fixtures use averagePrice; don't regress them."""
    assert _position_from_row({"exchangeId": "1", "quantity": 10,
                               "averagePrice": 0.5}).avg_entry_price == 0.5
    assert _position_from_row({"exchangeId": "1", "quantity": 10,
                               "avg_entry_price": 0.25}).avg_entry_price == 0.25


def test_avgCost_wins_over_a_stale_legacy_field():
    """avgCost is the authoritative SIG field, so it must be consulted first."""
    p = _position_from_row({"exchangeId": "1", "quantity": 10,
                            "avgCost": 0.4, "averagePrice": 0.9})
    assert p.avg_entry_price == 0.4


# --------------------------------------------------------------------------- #
# The response shape — what was NOT broken, guarded so it cannot become broken
# --------------------------------------------------------------------------- #
def test_positions_response_shape_is_positions_not_data():
    """SIG returns {"positions": [...], "summary": {...}} for this endpoint — NOT {"data": [...]}
    like most reads. Anything assuming `data` silently sees ZERO positions, which is exactly the
    mistake that made the venue look empty while 105 real holdings existed."""
    payload = {"positions": [LIVE_ROW], "summary": {"totalCostBasis": 4.0}}
    assert payload.get("data") is None, "the premise: there is no `data` key"
    rows = _as_rows(payload)
    assert len(rows) == 1 and rows[0]["exchangeId"] == "879"


def test_as_rows_still_handles_the_data_shape_for_other_endpoints():
    assert len(_as_rows({"data": [{"a": 1}, {"b": 2}]})) == 2
    assert len(_as_rows([{"a": 1}])) == 1
    assert _as_rows({"summary": {}}) == []


# --------------------------------------------------------------------------- #
# Scoped reads
# --------------------------------------------------------------------------- #
def test_client_builds_the_tournament_scoped_path():
    """Orders already carry tournamentId; the portfolio read should name the same book rather than
    relying on how the API key happens to be bound."""
    from sig.sig_client import SigClient

    captured = {}

    class FakeClient(SigClient):
        def __init__(self):  # bypass auth/env
            pass
        def get(self, path, params=None):
            captured["path"] = path
            return {"positions": []}

    c = FakeClient()
    c.portfolio_positions("midterm-elections")
    assert captured["path"] == "/tournaments/midterm-elections/portfolio/positions"
    c.portfolio_positions()
    assert captured["path"] == "/portfolio/positions", "no slug => unscoped (backwards compatible)"


def test_broker_passes_its_slug_to_the_position_read():
    import asyncio

    from sig.broker import SigBroker

    seen = {}

    class FakeRead:
        def portfolio_positions(self, slug=None):
            seen["slug"] = slug
            return {"positions": [LIVE_ROW]}

    b = SigBroker.__new__(SigBroker)             # no env/auth
    b._tournament_slug = "midterm-elections"
    b._read = lambda: FakeRead()
    pos = asyncio.run(SigBroker.get_positions(b))
    assert seen["slug"] == "midterm-elections"
    assert len(pos) == 1 and pos[0].avg_entry_price == 0.04
