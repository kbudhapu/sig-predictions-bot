"""MR alpha skew: direction, cap, disabled-by-default, and thin-history safety."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.market_making.mr_skew import MeanReversionSkew, MRSkewConfig


def _feed(mr, market, vals, t0=0.0, dt=30.0):
    for i, v in enumerate(vals):
        mr.observe(market, v, now=t0 + i * dt)


def test_disabled_by_default_no_tilt():
    mr = MeanReversionSkew()                        # enabled=False
    _feed(mr, "m", [0.5] * 10 + [0.9])
    assert mr.tilt_ticks("m", now=1000.0) == (0, None)


def test_up_overshoot_tilts_down():
    mr = MeanReversionSkew(MRSkewConfig(enabled=True, min_samples=10, k_enter=1.5, max_ticks=2,
                                        window_seconds=1e9))
    _feed(mr, "m", [0.50] * 30 + [0.70])            # big up spike vs a flat mean => z >> 0
    tilt, z = mr.tilt_ticks("m", now=1e6)
    assert z is not None and z > 1.5 and tilt < 0   # fade up-overshoot => shift DOWN


def test_down_overshoot_tilts_up():
    mr = MeanReversionSkew(MRSkewConfig(enabled=True, min_samples=10, k_enter=1.5, max_ticks=2,
                                        window_seconds=1e9))
    _feed(mr, "m", [0.50] * 30 + [0.30])
    tilt, z = mr.tilt_ticks("m", now=1e6)
    assert z is not None and z < -1.5 and tilt > 0


def test_tilt_is_hard_capped():
    mr = MeanReversionSkew(MRSkewConfig(enabled=True, min_samples=5, k_enter=1.0, max_ticks=1,
                                        window_seconds=1e9))
    _feed(mr, "m", [0.50] * 50 + [0.95])            # enormous z
    tilt, _ = mr.tilt_ticks("m", now=1e6)
    assert tilt == -1                               # capped at max_ticks regardless of z size


def test_thin_history_no_tilt():
    mr = MeanReversionSkew(MRSkewConfig(enabled=True, min_samples=20))
    _feed(mr, "m", [0.5, 0.9])
    assert mr.tilt_ticks("m", now=100.0) == (0, None)


def test_flat_series_no_tilt():
    mr = MeanReversionSkew(MRSkewConfig(enabled=True, min_samples=5, window_seconds=1e9))
    _feed(mr, "m", [0.5] * 20)                       # zero std => no signal, never divide-by-zero
    assert mr.tilt_ticks("m", now=1e6) == (0, None)
