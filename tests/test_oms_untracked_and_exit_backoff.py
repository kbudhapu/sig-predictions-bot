"""(1) id-less MM multi-leg bundles are not tracked forever; (2) exits back off on an empty book."""
from __future__ import annotations

import asyncio

from sig.engine.orchestrator import Orchestrator
from sig.oms.models import FillPolicy


# ── (1) OMS ───────────────────────────────────────────────────────────────────────────────────────
class _Broker:
    def __init__(self, with_ids: bool):
        self._ids = with_ids

    async def place_multi_leg(self, **kw):
        return [{"order_id": (f"o{i}" if self._ids else None), "status": "open", "filled_qty": 0}
                for i, _ in enumerate(kw["legs"])]

    async def place_order(self, **kw):
        return {"order_id": ("o1" if self._ids else None), "status": "open", "filled_qty": 0}


class _State:
    async def write_order(self, o):
        pass


def _oms(with_ids):
    from sig.oms.manager import OrderManagementSystem
    m = OrderManagementSystem.__new__(OrderManagementSystem)
    m._broker = _Broker(with_ids)
    m._state = _State()
    m._pending = {}
    m._lock = asyncio.Lock()
    m._hard_locked = False
    m._policies = {}
    return m


_LEGS = [{"contract_id": "1", "side": "buy", "qty": 10, "price": 0.5, "outcome": "yes"},
         {"contract_id": "2", "side": "buy", "qty": 10, "price": 0.5, "outcome": "yes"}]


def test_idless_mm_bundle_is_not_tracked():
    m = _oms(with_ids=False)
    asyncio.run(m.submit(strategy_id="market_making", legs=_LEGS, fill_policy=FillPolicy.for_market_making()))
    assert m._pending == {}


def test_mm_bundle_with_ids_is_still_tracked():
    m = _oms(with_ids=True)
    asyncio.run(m.submit(strategy_id="market_making", legs=_LEGS, fill_policy=FillPolicy.for_market_making()))
    assert len(m._pending) == 1


# ── (2) exit backoff ──────────────────────────────────────────────────────────────────────────────
def test_exit_backoff_waits_after_free_attempts_and_resets_on_progress():
    o = Orchestrator.__new__(Orchestrator)
    assert not o._exit_backoff_skip("951", 411)
    o._exit_backoff_note("951", 411)               # attempt 1: free
    assert not o._exit_backoff_skip("951", 411)
    o._exit_backoff_note("951", 411)               # attempt 2: free
    assert not o._exit_backoff_skip("951", 411)
    o._exit_backoff_note("951", 411)               # attempt 3, no progress → 60s wait
    assert o._exit_backoff_skip("951", 411)
    assert o._exit_backoff["951"]["n"] == 3
    assert not o._exit_backoff_skip("951", 300)    # held qty fell → progress → reset, allowed
    assert "951" not in o._exit_backoff


def test_exit_backoff_caps():
    o = Orchestrator.__new__(Orchestrator)
    for _ in range(20):
        o._exit_backoff_note("9", 100)
    import time
    assert o._exit_backoff["9"]["until"] - time.monotonic() <= Orchestrator.EXIT_BACKOFF_MAX_S + 1
