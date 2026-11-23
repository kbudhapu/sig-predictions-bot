"""MM must hold at most ONE resting order per (exchange, side) — the stacking incident of 2026-10-05."""
from __future__ import annotations

import asyncio

from sig.engine.orchestrator import Orchestrator
from sig.engine.protocols import Intent


class _B:
    def __init__(self):
        self.cancelled = []

    async def cancel_order(self, oid):
        self.cancelled.append(oid)

    async def get_open_orders_all(self):
        return []


def _o(snap):
    o = Orchestrator.__new__(Orchestrator)
    o._broker = _B()
    o._open_snapshot = snap
    return o


def _i(ex, side, px):
    return Intent(strategy_id="market_making", exchange_id=ex, side=side, action="open",
                  strategy_type="market_making", price=px, metadata={"mm_quote_qty": 10, "option_side": "yes"})


def _r(oid, ex, action, px):
    return {"order_id": oid, "exchange_id": ex, "side": "yes", "action": action, "price": px, "quantity": 10}


def test_same_quote_already_resting_is_skipped():
    o = _o([_r("1", "954", "buy", 0.55)])
    assert asyncio.run(o._mm_dedupe([_i("954", "buy", 0.55)], set())) == []
    assert o._broker.cancelled == []


def test_moved_quote_cancels_old_then_posts():
    o = _o([_r("1", "954", "buy", 0.50)])
    out = asyncio.run(o._mm_dedupe([_i("954", "buy", 0.55)], set()))
    assert len(out) == 1 and o._broker.cancelled == ["1"]


def test_stack_is_collapsed_and_non_mm_orders_untouched():
    snap = [_r(str(k), "977", "buy", 0.30) for k in range(3)] + [_r("cx", "977", "buy", 0.30)]
    o = _o(snap)
    out = asyncio.run(o._mm_dedupe([_i("977", "buy", 0.30)], {"cx"}))
    assert sorted(o._broker.cancelled) == ["0", "1", "2"] and len(out) == 1


def test_recent_post_not_duplicated_before_next_snapshot():
    o = _o([])
    first = asyncio.run(o._mm_dedupe([_i("1", "buy", 0.4)], set()))
    o._mm_note_posted(first)
    assert asyncio.run(o._mm_dedupe([_i("1", "buy", 0.4)], set())) == []


def test_no_snapshot_means_no_mm_quotes():
    o = _o(None)
    assert asyncio.run(o._mm_dedupe([_i("1", "buy", 0.4)], set())) == []


def test_cancel_budget_exhausted_skips_instead_of_stacking():
    snap = [_r(str(k), "5", "buy", 0.30) for k in range(10)]   # 10 > MM_MAX_CANCELS_PER_DRAIN
    o = _o(snap)
    assert asyncio.run(o._mm_dedupe([_i("5", "buy", 0.40)], set())) == []
    assert o._broker.cancelled == []


def test_resting_bid_notional_is_tracked_for_budget():
    o = _o([_r("1", "954", "buy", 0.50), _r("2", "955", "buy", 0.20), _r("3", "956", "sell", 0.90)])
    asyncio.run(o._mm_dedupe([], set()))
    assert abs(o._mm_resting_bid_notional - (0.50 * 10 + 0.20 * 10)) < 1e-9   # sells don't commit cash


def test_hung_cancel_times_out_and_does_not_post_over_it():
    class _Slow(_B):
        async def cancel_order(self, oid):
            await asyncio.sleep(60)

    o = _o([_r("1", "954", "buy", 0.50)])
    o._broker = _Slow()
    o.MM_CANCEL_TIMEOUT_S = 0.05
    assert asyncio.run(o._mm_dedupe([_i("954", "buy", 0.55)], set())) == []
