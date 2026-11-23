"""MR taker decision core: enter-only-when-flat, reduce-only exits, spread gate, time stop."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.mr_taker import MRTakerEngine, MRTakerConfig


def _eng(**kw):
    cfg = MRTakerConfig(enabled=True, min_samples=10, k_enter=2.0, k_exit=0.5,
                        window_seconds=1e9, max_spread=0.012, **kw)
    return MRTakerEngine(cfg)


def _feed(e, m, vals, t0=0.0, dt=30.0):
    for i, v in enumerate(vals):
        e.observe(m, v, now=t0 + i * dt)


def test_disabled_does_nothing():
    e = MRTakerEngine()  # disabled
    _feed(e, "m", [0.5] * 20 + [0.9])
    assert e.decide("m", position=0, spread=0.005, now=1e6).kind == "none"


def test_enter_short_on_up_overshoot():
    e = _eng()
    _feed(e, "m", [0.50] * 30 + [0.70])
    d = e.decide("m", position=0, spread=0.005, now=1e6)
    assert d.kind == "enter_short" and d.option_side == "no" and d.z > 2.0


def test_enter_long_on_down_overshoot():
    e = _eng()
    _feed(e, "m", [0.50] * 30 + [0.30])
    d = e.decide("m", position=0, spread=0.005, now=1e6)
    assert d.kind == "enter_long" and d.option_side == "yes" and d.z < -2.0


def test_no_entry_when_spread_too_wide():
    e = _eng()
    _feed(e, "m", [0.50] * 30 + [0.70])
    assert e.decide("m", position=0, spread=0.05, now=1e6).reason == "spread_too_wide"


def test_no_add_when_already_holding():
    e = _eng()
    _feed(e, "m", [0.50] * 30 + [0.70])        # still a strong short signal
    d = e.decide("m", position=-50, spread=0.005, now=1e6)   # already short
    assert d.kind in ("none", "exit")          # never "enter_*" while holding


def test_exit_reduce_only_on_revert():
    e = _eng()
    _feed(e, "m", [0.48, 0.52] * 15 + [0.50])  # oscillates around 0.50, last == mean -> z ~ 0 -> reverted
    d = e.decide("m", position=-50, spread=0.005, now=1e6)
    assert d.kind == "exit" and d.option_side == "no" and d.reason == "reverted"


def test_exit_on_flip_through_mean():
    e = _eng()
    _feed(e, "m", [0.50] * 30 + [0.40])        # was short (pos<0) but z now strongly negative -> flipped
    d = e.decide("m", position=-50, spread=0.005, now=1e6)
    assert d.kind == "exit" and d.reason == "flipped"


def test_time_stop_exits_even_without_signal():
    e = _eng(hold_max_s=100.0)
    _feed(e, "m", [0.50] * 30 + [0.70], t0=0.0, dt=30.0)
    e._entry_t["m"] = 0.0
    d = e.decide("m", position=-50, spread=0.005, now=10_000.0)
    assert d.kind == "exit" and d.reason == "time_stop"
