"""Volatility-aware MM half-spread — pure logic, no I/O. SHADOW research input (not wired into the live
quoter yet): compute what the quote half-width WOULD be if we scaled it by recent realized volatility —
tighter in calm markets (more fills, spread is safe), wider in volatile ones (don't get run over).

k_vol = clamp( base_k * (vol / vol_ref), k_min, k_max )

vol is the stdev of successive mid changes over a recent window (price units). vol_ref is the "normal"
vol at which we quote base_k. We validate in shadow (does vol-scaling reduce adverse fills / raise fills)
before ever touching compute_quote.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VolSpreadConfig:
    base_k: float = 0.015      # the current fixed half-width (quoter.QuoterConfig.k)
    vol_ref: float = 0.01      # realized vol (per-step stdev) at which we'd quote base_k
    k_min: float = 0.005       # 1 tick — tightest we'd ever rest inside
    k_max: float = 0.04        # widest, so a vol spike can't blow the quote out absurdly


def realized_vol(mids) -> float:
    """Stdev of successive mid changes (price units). 0 when < 3 samples or flat."""
    xs = [float(m) for m in (mids or []) if m is not None]
    if len(xs) < 3:
        return 0.0
    diffs = [xs[i] - xs[i - 1] for i in range(1, len(xs))]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / n
    return var ** 0.5


def vol_scaled_k(vol: float, cfg: VolSpreadConfig | None = None) -> float:
    """The vol-aware half-width. Scales base_k by vol/vol_ref, clamped to [k_min, k_max]."""
    cfg = cfg or VolSpreadConfig()
    if cfg.vol_ref <= 0 or vol <= 0:
        return cfg.base_k
    scaled = cfg.base_k * (vol / cfg.vol_ref)
    return min(max(scaled, cfg.k_min), cfg.k_max)
