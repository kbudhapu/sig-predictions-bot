"""Persist time-stamped rolling buffers (per-market mid histories) across restarts.

The MM's mr-skew (60-min window) and adverse monitor, and order_flow's momentum buffer, are
`dict[market -> deque[(monotonic_ts, value)]]`. A restart wipes them, so an edge that needs ~an hour of
history (mr-skew) sits dormant for an hour after every restart. These helpers snapshot such a buffer to a
JSON-able blob and restore it later.

THE CLOCK PROBLEM: the deques key on `time.monotonic()`, which resets to ~0 each process. A persisted
monotonic timestamp is meaningless in the next process. So we store each sample's AGE at save time plus a
wall-clock reference; on restore we re-age every sample by the real elapsed wall time across the restart
gap and map it back onto the NEW monotonic clock, dropping anything now older than the window. Result: the
buffer picks up where it left off, correctly aged, as if the restart never happened.
"""
from __future__ import annotations

import time
from collections import deque


def dump(hist: dict, now_mono: float) -> dict:
    """Snapshot {market: deque[(mono_ts, value)]} → a JSON-able blob of per-sample AGE + a wall reference."""
    buffers: dict[str, list] = {}
    for market, dq in (hist or {}).items():
        rows: list[list[float]] = []
        for ts, val in dq:
            try:
                age = float(now_mono) - float(ts)
            except (TypeError, ValueError):
                continue
            if age >= 0:
                rows.append([round(age, 3), float(val)])   # oldest-first order preserved from the deque
        if rows:
            buffers[str(market)] = rows
    return {"saved_wall": time.time(), "buffers": buffers}


def load(blob: dict, now_mono: float, window_s: float) -> dict:
    """Rehydrate a dump() blob into {market: deque[(mono_ts, value)]} on the CURRENT monotonic clock,
    re-aging by the wall time elapsed since the snapshot and dropping samples now beyond window_s."""
    hist: dict[str, deque] = {}
    if not isinstance(blob, dict):
        return hist
    try:
        saved = float(blob.get("saved_wall", 0) or 0)
    except (TypeError, ValueError):
        saved = 0.0
    elapsed = max(0.0, time.time() - saved) if saved else 0.0
    for market, rows in (blob.get("buffers") or {}).items():
        dq: deque = deque()
        for row in rows:
            try:
                age, val = float(row[0]), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            new_age = age + elapsed
            if 0.0 <= new_age <= float(window_s):
                dq.append((now_mono - new_age, val))        # still oldest-first → ascending ts
        if dq:
            hist[str(market)] = dq
    return hist
