"""Fix 1: the LIVE broker's venue-truth reads (get_positions / get_open_orders / get_account) used by
OMS reconciliation. The SIG read client HTTP is mocked (a fake sync read client) — NO live calls.
Confirms the returned objects structurally match sig/oms/protocols.py (BrokerPosition / BrokerOrderSnapshot)
and the orchestrator's AccountSnapshot (buying_power / equity), incl. defensive SIG field-name mapping and
the open-order terminal filter.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.broker import SigBroker, BrokerPositionSnapshot, BrokerOrderSnapshot, BrokerAccountSnapshot


class FakeReadClient:
    """Stands in for sig_client.SigClient — the sync read client. Records calls; returns canned SIG JSON."""

    def __init__(self):
        self.calls = []

    def portfolio_positions(self):
        self.calls.append("portfolio_positions")
        return {"data": [
            {"exchangeId": "MKT_A", "quantity": 10, "averagePrice": 0.42},
            {"exchangeId": "MKT_B", "quantity": -5, "avgPrice": 0.30},   # signed → short, abs qty
        ]}

    def account(self):
        self.calls.append("account")
        return {"buyingPower": 1234.5, "equity": 5000.0, "balance": 9999.0}

    def get(self, path, params=None):
        self.calls.append((path, params))
        assert path == "/orders"
        assert params == {"status": "open"}
        return {"data": [
            {"id": "o1", "exchangeId": "MKT_A", "side": "buy", "quantity": 10,
             "filledQuantity": 4, "averagePrice": 0.41, "status": "open"},
            {"id": "o2", "exchangeId": "MKT_B", "side": "sell", "quantity": 3,
             "filled": 3, "avgPrice": 0.5, "status": "filled"},   # terminal → must be filtered out
        ]}


def broker():
    # read_client injected → no network; transport unused by reads; no write key resolved (lazy).
    return SigBroker(read_client=FakeReadClient())


@pytest.mark.asyncio
async def test_get_positions_shape_and_mapping():
    pos = await broker().get_positions()
    assert len(pos) == 2
    a, b = pos
    assert isinstance(a, BrokerPositionSnapshot)
    assert (a.contract_id, a.side, a.qty, a.avg_entry_price) == ("MKT_A", "long", 10.0, 0.42)
    assert (b.contract_id, b.side, b.qty, b.avg_entry_price) == ("MKT_B", "short", 5.0, 0.30)
    # structural match to sig/oms/protocols.BrokerPosition
    for attr in ("contract_id", "side", "qty", "avg_entry_price"):
        assert hasattr(a, attr)


@pytest.mark.asyncio
async def test_get_open_orders_filters_terminal_and_maps_fields():
    orders = await broker().get_open_orders()
    assert len(orders) == 1          # the "filled" (terminal) order is filtered out
    o = orders[0]
    assert isinstance(o, BrokerOrderSnapshot)
    assert (o.order_id, o.contract_id, o.side, o.qty, o.filled_qty, o.avg_fill_price, o.status) == \
        ("o1", "MKT_A", "buy", 10.0, 4.0, 0.41, "open")
    for attr in ("order_id", "contract_id", "side", "qty", "filled_qty", "avg_fill_price", "status"):
        assert hasattr(o, attr)


@pytest.mark.asyncio
async def test_get_account_shape():
    acct = await broker().get_account()
    assert isinstance(acct, BrokerAccountSnapshot)
    assert acct.buying_power == 1234.5 and acct.equity == 5000.0
    assert hasattr(acct, "buying_power") and hasattr(acct, "equity")


class CashOnlyClient(FakeReadClient):
    """The REAL SIG /account shape: only `balance` (free cash), NO equity field. Positions carry the
    venue's own mark via summary.totalMarketValue — so NAV = cash + marked positions."""

    def account(self):
        self.calls.append("account")
        return {"balance": 54010.82}

    def portfolio_positions(self, slug=None):
        self.calls.append("portfolio_positions")
        return {
            "positions": [
                {"exchangeId": "MKT_A", "quantity": 100, "marketValue": 40000.0},
                {"exchangeId": "MKT_B", "quantity": -50, "marketValue": 11489.11},
            ],
            "summary": {"totalMarketValue": 51489.11, "totalCostBasis": 48654.37},
        }


@pytest.mark.asyncio
async def test_get_account_equity_is_nav_when_venue_gives_cash_only():
    # buying_power = FREE CASH; equity = NAV = cash + marked positions (summary.totalMarketValue).
    # This is the bug fix: the venue returns only `balance`, and equity must NOT collapse to it.
    rc = CashOnlyClient()
    b = SigBroker(read_client=rc)
    acct = await b.get_account()
    assert acct.buying_power == pytest.approx(54010.82)
    assert acct.equity == pytest.approx(54010.82 + 51489.11)
    assert b.last_equity() == pytest.approx(acct.equity)
    assert "portfolio_positions" in rc.calls          # fetched only because /account had no equity field


@pytest.mark.asyncio
async def test_get_account_nav_sums_rows_without_summary():
    class NoSummaryClient(CashOnlyClient):
        def portfolio_positions(self, slug=None):
            self.calls.append("portfolio_positions")
            return {"positions": [{"exchangeId": "A", "marketValue": 1000.0},
                                  {"exchangeId": "B", "marketValue": 250.5}]}

    acct = await SigBroker(read_client=NoSummaryClient()).get_account()
    assert acct.equity == pytest.approx(54010.82 + 1250.5)


@pytest.mark.asyncio
async def test_get_account_prefers_true_equity_field_and_skips_positions_fetch():
    # When the venue DOES provide equity, use it verbatim and pay no extra positions round-trip.
    rc = FakeReadClient()      # returns equity: 5000.0
    acct = await SigBroker(read_client=rc).get_account()
    assert acct.equity == 5000.0 and acct.buying_power == 1234.5
    assert "portfolio_positions" not in rc.calls


@pytest.mark.asyncio
async def test_get_account_nav_failsafe_to_cash_on_positions_error():
    class BoomClient(CashOnlyClient):
        def portfolio_positions(self, slug=None):
            raise RuntimeError("positions endpoint down")

    acct = await SigBroker(read_client=BoomClient()).get_account()
    assert acct.equity == pytest.approx(54010.82)     # degrades to free cash; never crashes the cycle


@pytest.mark.asyncio
async def test_reads_use_the_read_client_not_the_write_path():
    rc = FakeReadClient()
    b = SigBroker(read_client=rc)
    await b.get_positions()
    await b.get_open_orders()
    await b.get_account()
    # all three sourced from the READ client (never the write transport)
    assert "portfolio_positions" in rc.calls
    assert ("/orders", {"status": "open"}) in rc.calls
    assert "account" in rc.calls


@pytest.mark.asyncio
async def test_defensive_parsing_bare_list_and_alt_fields():
    class AltClient(FakeReadClient):
        def portfolio_positions(self):
            # bare list + alternative field names (contractId / qty / avgEntryPrice) + explicit side
            return [{"contractId": "X", "side": "long", "qty": 7, "avgEntryPrice": 0.6}]

    pos = await SigBroker(read_client=AltClient()).get_positions()
    assert (pos[0].contract_id, pos[0].side, pos[0].qty, pos[0].avg_entry_price) == ("X", "long", 7.0, 0.6)


@pytest.mark.asyncio
async def test_satisfies_oms_reconciliation_consumption():
    # reconciliation accesses .contract_id/.qty/.side/.avg_entry_price on positions and
    # .contract_id/.order_id on orders — exercise exactly that access pattern.
    b = broker()
    for p in await b.get_positions():
        _ = (p.contract_id.upper(), float(p.qty), p.side, float(p.avg_entry_price))
    for o in await b.get_open_orders():
        _ = (str(o.contract_id).upper(), o.order_id, o.status)
