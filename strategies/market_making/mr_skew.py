"""Mean-reversion ALPHA skew for the market-maker — pure logic, no I/O.

Backtest (sig-hft-edge-suite): on ~1 day of minute data the z-score of mid vs a ~60-min rolling mean
predicts short-horizon reversion (W≈60, k≈2σ, ~70% hit). Run as a TAKER it dies at the spread; run as a
MAKER quote-SKEW it is ~free — the MM is already resting two-sided quotes, so we just tilt the center.

  z > 0  (mid overshot UP, expect revert DOWN)  -> tilt the quote center DOWN (negative ticks):
          lower bid + lower ask => more likely to SELL YES into the overshoot, less likely to buy it.
  z < 0  (overshot DOWN, expect revert UP)       -> tilt UP: more likely to BUY the dip.

Safety by construction: (1) the tilt is HARD-capped at `max_ticks` (a lean, never a bet); (2) the caller
keeps the shifted quote INSIDE the touch (still a maker, never crossing); (3) the ask is already bounded
to held inventory upstream, so the skew can never open a naked short. Disabled by default.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

from .quoter import TICK, PRICE_MIN, PRICE_MAX


@dataclass(frozen=True)
class MRSkewConfig:
    enabled: bool = False          # OFF by default — committed config is unchanged behaviour
    window_seconds: float = 3600.0 # ~60-min lookback (the backtest sweet spot)
    min_samples: int = 20          # need this many mids before trusting the z-score
    k_enter: float = 2.0           # |z| threshold to start tilting (backtest sweet spot ~2σ)
    max_ticks: int = 1             # HARD cap on the tilt in ticks — keeps it a lean, not a bet
    z_per_tick: float = 1.0        # each extra σ beyond k_enter adds one tick, up to max_ticks
    tick: float = TICK
    price_min: float = PRICE_MIN
    price_max: float = PRICE_MAX


class MeanReversionSkew:
    """Per-market rolling mid buffer -> a signed tick tilt. Stateful across ticks (like the
    inventory book and adverse monitor); time injected for determinism."""

    def __init__(self, cfg: MRSkewConfig | None = None) -> None:
        self._cfg = cfg or MRSkewConfig()
        self._hist: dict[str, deque[tuple[float, float]]] = {}

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def snapshot(self) -> dict:
        """JSON-able snapshot of the per-market mid history (for restart persistence)."""
        from infra.state.rolling_buffer import dump
        return dump(self._hist, time.monotonic())

    def restore(self, blob: dict) -> None:
        """Rehydrate the mid history from a snapshot(), re-aged onto the current clock + pruned to window."""
        from infra.state.rolling_buffer import load
        self._hist = load(blob, time.monotonic(), self._cfg.window_seconds)

    def observe(self, market: str, mid: float | None, now: float | None = None) -> None:
        if mid is None or not self._cfg.enabled:
            return
        now = time.monotonic() if now is None else now
        dq = self._hist.setdefault(market, deque())
        dq.append((now, float(mid)))
        cutoff = now - self._cfg.window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def tilt_ticks(self, market: str, now: float | None = None) -> tuple[int, float | None]:
        """Return (signed_tick_tilt, z). tilt<0 shifts the quote DOWN (fade an up-overshoot)."""
        if not self._cfg.enabled:
            return 0, None
        dq = self._hist.get(market)
        if not dq or len(dq) < self._cfg.min_samples:
            return 0, None
        vals = [v for _, v in dq]
        n = len(vals)
        m = sum(vals) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in vals) / n)
        if sd < 1e-6:
            return 0, None
        z = (vals[-1] - m) / sd
        if abs(z) < self._cfg.k_enter:
            return 0, round(z, 3)
        mag = 1 + int((abs(z) - self._cfg.k_enter) / max(self._cfg.z_per_tick, 1e-9))
        mag = min(mag, self._cfg.max_ticks)
        return (-mag if z > 0 else mag), round(z, 3)
