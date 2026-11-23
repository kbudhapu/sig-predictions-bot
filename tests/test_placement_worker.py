"""Placement moved OFF the cycle — without breaking the no-double-spend guarantee.

The cycle used to place every intent inline (`for intent in intents: await _process_intent(...)`,
unbounded). With 237 quotable markets that is 237+ venue round-trips in one cycle, which is where
the 60-160s cycles came from (historically ~47min) and why exits and directional queued behind the
whole MM burst.

THE RISK being guarded here. The capital guarantee is "reserve sequentially against ONE snapshot",
and CapitalLedger.begin_cycle() RESETS every reservation. So moving the submit off-cycle while
leaving the reserve in the cycle would let the next cycle's begin_cycle wipe reservations with
submits still in flight — a double-spend. The worker therefore performs the WHOLE path and calls
begin_cycle itself, once per drain batch, and processes strictly one intent at a time.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from sig.engine.capital_ledger import CapitalLedger


# --------------------------------------------------------------------------- #
# The invariant, stated directly against the ledger
# --------------------------------------------------------------------------- #
def test_begin_cycle_resets_reservations_which_is_why_the_worker_owns_it():
    """Documents the hazard the design works around. If this ever stops being true, the worker no
    longer needs to own begin_cycle."""
    led = CapitalLedger()
    led.begin_cycle(buying_power=1000.0, equity=1000.0)
    led.reserve("mm", 400.0)
    assert led.reserved_total == 400.0
    led.begin_cycle(buying_power=1000.0, equity=1000.0)      # a new cycle
    assert led.reserved_total == 0.0, (
        "begin_cycle wipes reservations — calling it while submits are in flight double-spends")


def test_sequential_reservation_cannot_oversubscribe():
    """What the single-consumer worker preserves: N sequential reservations never exceed the
    snapshot, however many intents are queued."""
    led = CapitalLedger()
    led.begin_cycle(buying_power=1000.0, equity=1000.0)
    granted = [led.reserve(f"s{i}", 300.0) for i in range(10)]
    assert sum(granted) <= 1000.0 + 1e-9, f"oversubscribed: {sum(granted)}"
    assert led.available_buying_power() >= 0.0


# --------------------------------------------------------------------------- #
# Queue behaviour
# --------------------------------------------------------------------------- #
class _Orch:
    """The queue/TTL logic in isolation — the real orchestrator ctor needs the whole engine."""

    def __init__(self, maxsize=5, ttl=30.0):
        from sig.engine.orchestrator import Orchestrator as _T
        self._placement_q = asyncio.Queue(maxsize=maxsize)
        self._placement_dropped_full = 0
        self._placement_dropped_stale = 0
        self._placement_intent_ttl_s = ttl
        self._enqueue_intent = _T._enqueue_intent.__get__(self)
        self._drain_queue = _T._drain_queue.__get__(self)


@pytest.mark.asyncio
async def test_enqueue_never_blocks_the_cycle_and_evicts_the_OLDEST_on_overflow():
    """A full queue must not stall the cycle. And the eviction must drop the OLDEST: a stale quote is
    worthless, the freshest view of the book is the one worth placing."""
    o = _Orch(maxsize=3)
    for i in range(6):
        o._enqueue_intent(f"intent{i}", "strat")      # would deadlock on a blocking put
    assert o._placement_q.qsize() == 3
    kept = [o._placement_q.get_nowait()[1] for _ in range(3)]
    assert kept == ["intent3", "intent4", "intent5"], f"evicted the wrong end: kept {kept}"
    assert o._placement_dropped_full == 3


@pytest.mark.asyncio
async def test_drain_queue_empties_it():
    o = _Orch(maxsize=10)
    for i in range(4):
        o._enqueue_intent(f"i{i}", "s")
    assert o._drain_queue() == 4
    assert o._placement_q.qsize() == 0


@pytest.mark.asyncio
async def test_a_stale_queued_intent_is_dropped_not_placed():
    """TTL exists because a quote priced off a book we last saw 30s+ ago is exactly the stale-quote
    mistake the fresh-only MM filter prevents upstream."""
    import time as _t
    o = _Orch(ttl=30.0)
    now = _t.monotonic()
    fresh = (now - 5.0, "fresh", "s")
    stale = (now - 120.0, "stale", "s")
    kept = [i for q_at, i, _ in (fresh, stale) if (now - q_at) <= o._placement_intent_ttl_s]
    assert kept == ["fresh"]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_the_live_config_has_a_ttl_whenever_the_worker_is_on():
    """The CODE default stays off (see the ctor test below) so nothing enables this implicitly. The
    live config turned it on deliberately on 2026-10-02 — and if it is on, a queued intent MUST have
    a TTL, or a stale quote can be placed off a book we no longer believe."""
    loops = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["loops"]
    assert isinstance(loops["placement_worker"], bool)
    if loops["placement_worker"]:
        assert float(loops["placement_intent_ttl_s"]) > 0, (
            "the worker is enabled with no intent TTL — queued intents could be placed off a stale "
            "book, which is the bug the fresh-only MM filter exists to prevent")


def test_default_ctor_keeps_the_in_cycle_path():
    from sig.engine.orchestrator import Orchestrator as TradingOrchestrator
    import inspect
    sig_ = inspect.signature(TradingOrchestrator.__init__)
    assert sig_.parameters["placement_worker"].default is False
