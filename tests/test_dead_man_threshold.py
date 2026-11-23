"""The dead-man threshold must cover the worst legitimate CYCLE, not the tick interval.

The engine heartbeat beats once per cycle (run_forever, after _run_cycle returns). Deriving the
threshold as tick x 2 assumed a cycle costs about one tick; it does not. A cycle that places orders
is paced by the VENUE, which serialises writes at ~4.5-8.7s each, so a busy cycle legitimately runs
to ~160s — measured in production: cycle_complete p50 2.8s, max 161.3s.

At tick=5s the threshold was 10s, so dead_man_heartbeat_stale fired continuously (age climbing
monotonically, 14 times in 12 minutes). A dead-man that always cries wolf is not a safety device:
it cannot distinguish a hung engine from a busy one.
"""
from __future__ import annotations

import yaml

from sig.engine.orchestrator import _DeadManWatchdog, DEAD_MAN_CYCLE_MULTIPLIER


def _wd(**kw):
    return _DeadManWatchdog(cycle_interval_seconds=5.0, get_heartbeat_age_seconds=lambda: 0.0,
                            on_stale=lambda age: None, **kw)


def test_default_still_derives_from_the_tick():
    """Unchanged behaviour when nothing is configured."""
    assert _wd()._threshold == 5.0 * DEAD_MAN_CYCLE_MULTIPLIER


def test_an_explicit_threshold_overrides_the_derived_one():
    assert _wd(stale_threshold_seconds=300.0)._threshold == 300.0


def test_the_live_threshold_exceeds_the_worst_measured_cycle():
    """161.3s was a REAL cycle, not a hang — it placed orders the venue serialised."""
    loops = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["loops"]
    thr = float(loops["dead_man_threshold_s"])
    assert thr > 161.3, (
        f"threshold {thr}s is below the worst measured legitimate cycle (161.3s): the watchdog "
        "would fire on a healthy busy cycle")
    assert thr <= 900.0, "a threshold this loose would not catch a genuinely hung engine"


def test_the_threshold_is_not_merely_the_tick_multiple():
    """Guard against the regression: tick x 2 = 10s is what fired continuously."""
    loops = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["loops"]
    tick = float(loops["fast_loop_interval_s"])
    assert float(loops["dead_man_threshold_s"]) > tick * DEAD_MAN_CYCLE_MULTIPLIER


def test_a_genuinely_stale_heartbeat_still_fires():
    """The device must still work — only the threshold changed."""
    seen = []
    wd = _DeadManWatchdog(cycle_interval_seconds=5.0,
                          get_heartbeat_age_seconds=lambda: 10_000.0,
                          on_stale=seen.append, stale_threshold_seconds=300.0,
                          check_interval_seconds=0.01)
    age = wd._get_age()
    assert age > wd._threshold
    wd._on_stale(age)
    assert seen == [10_000.0]
