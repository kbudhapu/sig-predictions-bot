"""Rolling-buffer persistence: dump→load must preserve samples, re-age them across the restart gap using
wall-clock, map them onto the NEW monotonic clock, and drop anything now beyond the window."""
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infra.state.rolling_buffer as rb


def test_dump_load_roundtrip_preserves_values_and_order():
    now = 1000.0  # a monotonic reading
    hist = {"840": deque([(now - 30.0, 0.70), (now - 10.0, 0.72), (now - 1.0, 0.73)])}
    blob = rb.dump(hist, now)
    # restore on a DIFFERENT monotonic clock (as a new process would have)
    new_now = 5.0
    out = rb.load(blob, new_now, window_s=3600.0)
    vals = [v for _, v in out["840"]]
    assert vals == [0.70, 0.72, 0.73]                       # order + values preserved
    ts = [t for t, _ in out["840"]]
    assert ts == sorted(ts)                                 # ascending (oldest-first) on the new clock


def test_ages_increase_across_the_restart_gap(monkeypatch):
    now = 1000.0
    hist = {"m": deque([(now - 10.0, 0.5)])}               # one sample, 10s old at save
    saved_wall = 2_000_000.0
    monkeypatch.setattr(rb.time, "time", lambda: saved_wall)
    blob = rb.dump(hist, now)
    # 50s of wall time passes during the "restart"
    monkeypatch.setattr(rb.time, "time", lambda: saved_wall + 50.0)
    out = rb.load(blob, now_mono=0.0, window_s=3600.0)
    # new age should be ~10 (age at save) + 50 (gap) = 60 → ts ≈ 0.0 - 60 = -60
    (t, v), = list(out["m"])
    assert abs(t - (-60.0)) < 1e-6 and v == 0.5


def test_samples_beyond_window_are_dropped(monkeypatch):
    now = 1000.0
    hist = {"m": deque([(now - 3500.0, 0.4), (now - 10.0, 0.6)])}  # one near window edge, one fresh
    saved_wall = 10.0
    monkeypatch.setattr(rb.time, "time", lambda: saved_wall)
    blob = rb.dump(hist, now)
    # 200s gap: the 3500s-old sample becomes 3700s > 3600 window → dropped; the 10s one → 210s, kept
    monkeypatch.setattr(rb.time, "time", lambda: saved_wall + 200.0)
    out = rb.load(blob, now_mono=0.0, window_s=3600.0)
    vals = [v for _, v in out["m"]]
    assert vals == [0.6]


def test_empty_and_garbage_blobs_are_safe():
    assert rb.load(None, 0.0, 60.0) == {}
    assert rb.load({}, 0.0, 60.0) == {}
    assert rb.load({"buffers": {"m": [["x", "y"], [1.0]]}}, 0.0, 60.0) == {}  # bad rows skipped → empty


def test_dump_skips_empty_markets():
    blob = rb.dump({"m": deque()}, 100.0)
    assert blob["buffers"] == {}
