"""MM improvements (2026-10-05): sharp-anchored quoting, MM cash carve-out, MM leg-id recovery, signed fills."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from sig.engine.live_strategies import CoreMarketMakingStrategy
from sig.engine.orchestrator import Orchestrator
from sig.engine.capital_ledger import CapitalLedger


def _row(bid, ask):
    return {"best_bid": bid, "best_ask": ask, "updated_at": datetime.now(timezone.utc).isoformat()}


def _mm(mode, tmp_path, sharp=0.70):
    cfg = {"sizing": {"k_reference_sigma": 0.1},
           "market_making": {"max_quote_age_s": 60, "sharp_anchor": mode,
                             "sharp_anchor_record_path": str(tmp_path / "sa.jsonl")}}
    s = CoreMarketMakingStrategy(store=None, config=cfg, exchange_ids=["1"], equity=100_000.0)
    s.set_prices({"1": _row(0.60, 0.61)})   # 1-tick spread → MM joins the touch: bid 0.60 / ask 0.61
    s.set_cross_owned(["1"])
    s.set_sharp_provider(lambda ex: sharp)
    return s


_POS = {"1": {"quantity": 500}}             # small long so an ask exists; well under MM's cap


def test_sharp_anchor_live_drops_ask_below_fair_and_leans_bid(tmp_path):
    base = {i.side: i for i in _mm("off", tmp_path).evaluate({"positions": _POS})}
    live = {i.side: i for i in _mm("live", tmp_path).evaluate({"positions": _POS})}
    assert "sell" in base and "sell" not in live          # ask 0.61 < sharp 0.70 + margin → dropped
    assert live["buy"].metadata["mm_quote_qty"] > base["buy"].metadata["mm_quote_qty"]   # SIG cheap → bid bigger


def test_sharp_anchor_shadow_records_but_changes_nothing(tmp_path):
    base = sorted((i.side, i.price, i.metadata["mm_quote_qty"])
                  for i in _mm("off", tmp_path).evaluate({"positions": _POS}))
    sh = _mm("shadow", tmp_path)
    got = sorted((i.side, i.price, i.metadata["mm_quote_qty"]) for i in sh.evaluate({"positions": _POS}))
    assert got == base
    lines = (tmp_path / "sa.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and '"action": "sell"' in lines[0]


def test_sharp_anchor_drops_bid_above_fair(tmp_path):
    live = {i.side for i in _mm("live", tmp_path, sharp=0.50).evaluate({"positions": _POS})}
    assert "buy" not in live                               # bid 0.60 > sharp 0.50 − margin → dropped


def _orch(reserve, frac, eligible=("cross_market_divergence",)):
    o = Orchestrator.__new__(Orchestrator)
    o._capital_edge_reserve = reserve
    o._capital_pool_eligible = set(eligible)
    o._mm_cash_reserve_frac = frac
    o._ledger = CapitalLedger()
    o._ledger.begin_cycle(buying_power=20_000.0, equity=100_000.0)
    return o


def test_pooled_budget_carves_mm_reserve():
    o = _orch(15_000.0, 0.05)                              # MM carve-out = 5% × 100k = 5,000
    assert o._pooled_budget("cross_market_divergence", 20_000.0) == 15_000.0   # edges stop short of it
    assert o._pooled_budget("market_making", 20_000.0) == 5_000.0              # 5k carve + 0 above
    assert o._pooled_budget("mean_reversion", 20_000.0) == 0.0                # 20k − 15k − 5k
    assert o._pooled_budget("market_making", 3_000.0) == 3_000.0              # never above real cash


def test_pooled_budget_unchanged_without_mm_reserve():
    o = _orch(15_000.0, 0.0)
    assert o._pooled_budget("cross_market_divergence", 20_000.0) == 20_000.0
    assert o._pooled_budget("market_making", 20_000.0) == 5_000.0
    assert o._pooled_budget("order_flow", 20_000.0) == 5_000.0


def test_leg_id_recovery_claims_matching_open_order():
    from sig.broker import SigBroker

    b = SigBroker.__new__(SigBroker)
    b._attr_buf = []
    now = datetime.now(timezone.utc).isoformat()

    class _R:
        def get(self, path, params=None):
            return {"data": [
                {"id": 11, "exchangeId": "1", "side": "yes", "action": "buy", "quantity": 40,
                 "priceLimit": 0.6, "open": True, "createdAt": now},
                {"id": 12, "exchangeId": "1", "side": "yes", "action": "buy", "quantity": 99,
                 "priceLimit": 0.6, "open": True, "createdAt": now},
            ]}

    b._read = lambda: _R()
    b._note_attr(12, "cross_market_divergence", "1")     # already known → must not be re-claimed
    b._attr_buf = []
    b.note_pending_legs("market_making",
                        [{"contract_id": "1", "outcome": "yes", "side": "buy", "qty": 40, "price": 0.6}])
    n = asyncio.run(b.resolve_pending_legs())
    assert n == 1 and b.drain_attribution() == [("11", "market_making", "1", None)]
    assert asyncio.run(b.resolve_pending_legs()) == 0     # nothing pending → no read, no claim


def test_strategy_fills_gains_signed_qty_column_on_old_db(tmp_path):
    from infra.state.schema import init_state_db
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE strategy_fills (fill_id TEXT PRIMARY KEY, order_id TEXT, strategy TEXT NOT NULL,"
              " exchange_id TEXT NOT NULL, outcome TEXT, price REAL NOT NULL, quantity REAL NOT NULL,"
              " filled_at TEXT, mid_at_fill REAL, ingested_at TEXT NOT NULL)")
    c.commit(); c.close()
    init_state_db(db)
    init_state_db(db)                                      # idempotent
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(strategy_fills)")}
    assert "signed_qty" in cols


def test_price_pump_pushes_live_nav_into_mm():
    """MM was sized off the startup cash balance forever; the pump must refresh it with live NAV."""
    import main_sig

    class _Store:
        async def read_prices(self):
            return {}

    class _MM:
        equity = None

        def set_prices(self, p):
            pass

        def set_equity(self, e):
            self.equity = e

    mm = _MM()

    async def _run():
        t = asyncio.create_task(main_sig._price_and_watchdog_pump(
            store=_Store(), mm=mm, client=None, exchange_ids=[], interval_s=1.0, max_ids_per_call=50,
            equity_fn=lambda: 88_000.0))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert mm.equity == 88_000.0
