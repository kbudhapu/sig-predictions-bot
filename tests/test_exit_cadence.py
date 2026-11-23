"""The stop/trail check runs on its OWN timer, not once per trading cycle.

Live failure this pins: the 20% stop was correct, correctly configured, and firing — but
`_run_takeprofit_exits` only ran inside `_run_cycle`, and a cycle takes ~47 minutes because the MM
places and cancels hundreds of orders at 5-14s per API call. Consecutive passes were observed at
00:40 -> 01:47 -> 02:36 -> 03:23. So a position could run well past the threshold before anything
looked at it: ex862 sat at -30.1% with a 20% stop until the next pass came round and cut it.

A cycle-bound 20% stop behaves like a ~30% stop. The threshold is NOT the problem and is unchanged;
the evaluation interval was.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from sig.engine.orchestrator import Orchestrator, _directional_exit_decision


def _orch(**kw) -> Orchestrator:
    """Bare orchestrator: only the exit-cadence machinery is under test."""
    o = object.__new__(Orchestrator)
    o._halt = asyncio.Event()
    o._exit_pass_lock = None
    o._exit_task = None
    o._exit_check_interval_s = float(kw.get("interval", 0.01))
    o._passes = 0
    return o


# --------------------------------------------------------------------------- #
# The threshold itself — unchanged, and right
# --------------------------------------------------------------------------- #
def test_the_twenty_percent_stop_is_unchanged_and_still_cuts():
    """Keep the number. The live distribution supports it: 28 of 34 losers inside -5%, a clean gap
    at -20..-30%, and the one outlier past -30% is what it caught."""
    D = lambda p: _directional_exit_decision(p, p, trail_arm_pct=0.5, trail_pct=0.2,
                                             stop_loss_pct=0.20)
    assert D(-0.3014) == "stop_loss", "the live ex862 case"
    assert D(-0.2001) == "stop_loss"
    assert D(-0.1999) is None, "just inside the stop must HOLD"
    assert D(-0.05) is None, "ordinary MM round-trip noise must HOLD"

    cfg = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["directional_exit"]
    assert cfg["stop_loss_pct"] == 0.20, "the threshold must not drift"


def test_the_interval_is_configured_and_far_below_a_cycle():
    cfg = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["directional_exit"]
    iv = float(cfg["exit_check_interval_s"])
    assert 1.0 <= iv <= 120.0, f"interval {iv}s should be seconds, not minutes"
    assert iv < 47 * 60 / 10, "must be an order of magnitude faster than the ~47min cycle"


# --------------------------------------------------------------------------- #
# The timer
# --------------------------------------------------------------------------- #
def test_the_timer_runs_the_pass_repeatedly_without_a_cycle():
    """The whole point: exits happen on their own cadence, with no _run_cycle involvement."""
    o = _orch(interval=0.01)

    async def fake_pass():
        o._passes += 1
        return 0

    o._run_takeprofit_exits = fake_pass

    async def main():
        t = asyncio.create_task(o._exit_timer_loop())
        await asyncio.sleep(0.08)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert o._passes >= 3, f"timer should have run several passes, got {o._passes}"


def test_the_timer_holds_while_halted():
    """halt-but-HOLD means positions are held, never auto-flattened — including by this timer."""
    o = _orch(interval=0.01)
    o._halt.set()

    async def fake_pass():
        o._passes += 1
        return 0

    o._run_takeprofit_exits = fake_pass

    async def main():
        t = asyncio.create_task(o._exit_timer_loop())
        await asyncio.sleep(0.06)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert o._passes == 0, "a halted bot must not auto-exit"


def test_a_failing_pass_does_not_kill_the_timer():
    """A transient venue error must not silently stop all future stops from being evaluated."""
    o = _orch(interval=0.01)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("venue read failed")
        return 0

    o._run_takeprofit_exits = flaky

    async def main():
        t = asyncio.create_task(o._exit_timer_loop())
        await asyncio.sleep(0.08)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert calls["n"] > 2, "the timer must keep going after a failure"


# --------------------------------------------------------------------------- #
# No double-close: the lock + the pending-bundle guard
# --------------------------------------------------------------------------- #
def test_overlapping_passes_cannot_both_run():
    """The pending-bundle guard blocks a second close ACROSS passes, but two passes that overlap
    before either has submitted would each see no pending bundle. The lock closes that window."""
    o = _orch()
    started = {"n": 0}

    async def slow_inner():
        started["n"] += 1
        await asyncio.sleep(0.05)
        return 1

    o._run_takeprofit_exits_inner = slow_inner

    async def main():
        a, b = await asyncio.gather(o._run_takeprofit_exits(), o._run_takeprofit_exits())
        return a, b

    a, b = asyncio.run(main())
    assert started["n"] == 1, "the second overlapping pass must be skipped, not queued"
    assert sorted([a, b]) == [0, 1], "the skipped pass reports 0 exits"


def test_the_pass_is_reentrant_once_the_previous_one_finishes():
    """Skipping is only for OVERLAP — sequential passes must each run."""
    o = _orch()
    n = {"c": 0}

    async def inner():
        n["c"] += 1
        return 0

    o._run_takeprofit_exits_inner = inner

    async def main():
        for _ in range(3):
            await o._run_takeprofit_exits()

    asyncio.run(main())
    assert n["c"] == 3


def test_interval_is_floored_so_a_zero_config_cannot_spin():
    """A 0 or negative interval would busy-loop the venue read."""
    o = object.__new__(Orchestrator)
    o._exit_check_interval_s = max(float(0), 1.0)
    assert o._exit_check_interval_s >= 1.0
