"""Regression guards for two live P&L leaks found 2026-10-05.

1. The venue-valued exit pass must tell the cross-market thesis override the held side from the SIGN of
   quantity. The venue reports option="YES" on EVERY position row, so keying off `option` judged every
   held-NO short as a long → thesis_reversed on any adverse-looking gap → sell NO → cross re-entered.
2. Market-making must not quote markets cross-market owns: MM sizes inventory off the whole venue
   position, so it unloaded cross's (over-MM-cap) convergence positions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sig.engine.live_strategies import CoreMarketMakingStrategy
from sig.engine.orchestrator import Orchestrator


class _Broker:
    def __init__(self, rows):
        self._rows = rows

    async def get_positions_valued(self):
        return self._rows


class _OMS:
    def __init__(self):
        self.submitted = []

    async def has_pending_bundle(self, sid):
        return False

    async def submit(self, **kw):
        self.submitted.append(kw)


class _State:
    async def read_prices(self):
        return {}

    async def read_open_baskets(self):
        return []


def _orch(rows):
    o = Orchestrator.__new__(Orchestrator)
    o._broker = _Broker(rows)
    o._oms = _OMS()
    o._state = _State()
    o._pos_peak = {}
    o._exit_trail_arm_pct, o._exit_trail_pct, o._exit_stop_loss_pct = 0.5, 0.2, 0.2
    return o


def test_exit_override_gets_is_long_from_quantity_sign_not_option():
    seen = {}

    def override(ex, pnl, is_long):
        seen[ex] = is_long
        return (True, None)                      # HOLD — we only check what the override was told

    rows = [  # venue rows: option is "YES" on BOTH, exactly as the live feed reports
        {"exchange_id": "967", "quantity": -9588, "current_price": 0.133, "pnl_pct": -1.5, "option": "YES"},
        {"exchange_id": "1000", "quantity": 13735, "current_price": 0.868, "pnl_pct": 5.3, "option": "YES"},
    ]
    o = _orch(rows)
    o._directional_exit_override = override
    asyncio.run(o._run_takeprofit_exits_inner())
    assert seen == {"967": False, "1000": True}
    assert o._oms.submitted == []                # HOLD → nothing sent


def _row(bid=0.86, ask=0.87):
    return {"best_bid": bid, "best_ask": ask, "updated_at": datetime.now(timezone.utc).isoformat()}


def _mm(mode=None):
    mmc = {"max_quote_age_s": 60}
    if mode:
        mmc["cross_overlap"] = mode
    cfg = {"sizing": {"k_reference_sigma": 0.1}, "market_making": mmc}
    s = CoreMarketMakingStrategy(store=None, config=cfg, exchange_ids=["1000", "2000", "3000"],
                                 equity=100_000.0)
    s.set_prices({"1000": _row(), "2000": _row(), "3000": _row()})
    s.set_cross_owned(["1000", "2000"])
    return s


# 1000: cross holds 13,735 YES @~0.865 ≈ $11.9k  > MM cap 0.05×100k = $5k   → conflict
# 2000: cross race, small 1,000 YES ≈ $865      < cap                          → MM quotes it
# 3000: not a cross race                                                      → MM quotes it
_POS = {"1000": {"quantity": 13735}, "2000": {"quantity": 1000}}


def test_mm_dynamic_stands_aside_only_where_cross_position_exceeds_mm_cap():
    s = _mm()
    ex = {i.exchange_id for i in s.evaluate({"positions": _POS})}
    assert "1000" not in ex and {"2000", "3000"} <= ex
    assert s.conflicted_exchanges() == {"1000"}


def test_mm_dynamic_short_cross_position_uses_no_price():
    s = _mm()
    # held NO: 9,588 × (1 − 0.865) ≈ $1.3k < cap → MM may quote; 60,000 × 0.135 ≈ $8.1k > cap → conflict
    assert "1000" in {i.exchange_id for i in s.evaluate({"positions": {"1000": {"quantity": -9588}}})}
    s.evaluate({"positions": {"1000": {"quantity": -60000}}})
    assert s.conflicted_exchanges() == {"1000"}


def test_mm_exclude_and_allow_modes():
    s = _mm("exclude")
    ex = {i.exchange_id for i in s.evaluate({"positions": {}})}
    assert ex == {"3000"}
    s = _mm("allow")
    ex = {i.exchange_id for i in s.evaluate({"positions": _POS})}
    assert {"1000", "2000", "3000"} <= ex and s.conflicted_exchanges() == set()


class _CancelBroker:
    def __init__(self, orders, attr_buf=()):
        self._orders = orders
        self._attr_buf = list(attr_buf)
        self.cancelled = []

    async def get_open_orders_valued(self):
        return self._orders

    async def cancel_order(self, oid):
        self.cancelled.append(oid)


class _AttrState(_State):
    async def read_attribution_map(self):
        return {"77": "cross_market_divergence", "78": "market_making"}


class _Bundle:
    def __init__(self, sid, oid):
        self.strategy_id = sid
        self.slices = [type("S", (), {"order_id": oid})()]


def test_requote_vacates_only_mm_orders_on_conflicted_markets():
    orders = [
        {"order_id": "1", "exchange_id": "1000", "action": "sell", "side": "yes", "price": 0.85},  # MM (no id known)
        {"order_id": "77", "exchange_id": "1000", "action": "buy", "side": "no", "price": 0.13},   # cross (attributed)
        {"order_id": "78", "exchange_id": "1000", "action": "buy", "side": "yes", "price": 0.86},  # MM (attributed)
        {"order_id": "90", "exchange_id": "1000", "action": "sell", "side": "yes", "price": 0.85}, # exit, in-flight
        {"order_id": "91", "exchange_id": "1000", "action": "buy", "side": "yes", "price": 0.86},  # cross, unflushed buf
        {"order_id": "5", "exchange_id": "2000", "action": "sell", "side": "yes", "price": 0.87},  # not conflicted
    ]
    o = Orchestrator.__new__(Orchestrator)
    o._mm_requote_threshold = 0.1
    o._broker = _CancelBroker(orders, attr_buf=[("91", "cross_market_divergence", "1000")])
    o._state = _AttrState()
    o._oms = type("O", (), {"_pending": {"b": _Bundle("exit:1000", "90")}})()
    o._stale_markets = set()
    o._mm_conflict_source = lambda: {"1000"}
    asyncio.run(o._run_mm_requote())
    assert sorted(o._broker.cancelled) == ["1", "78"]
