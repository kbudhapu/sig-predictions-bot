"""Mean-reversion TAKER decision core — pure logic, no I/O, no order placement.

The operator's bet: run MR as a taker (cross the spread to enter), scoped to tight-spread markets
where the 1-day backtest cleared the toll (W≈60, k≈2σ). This module ONLY decides; the adapter turns
decisions into Intents and the gate/sizer/broker place them.

SAFETY BY CONSTRUCTION (this is the whole point — the last blowup was a non-netting exit doubling a
position):
  * ENTER only when FLAT (no open MR position on that market). Never add to a position.
  * EXIT is reduce-only: SELL the held leg. The risk gate clamps sells to holdings and rejects a sell
    with no holding ("sell_without_holding"), so an exit can neither overshoot past zero NOR double.
  * Position is tracked from OUR OWN fills (per-strategy), so MM/other strategies on the same market
    don't confuse it.
A decision is one of: None (do nothing), EnterLong (buy YES), EnterShort (buy NO == sell YES), or
Exit (reduce-only sell of the held leg).
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class MRTakerConfig:
    enabled: bool = False
    window_seconds: float = 3600.0   # ~60-min lookback (backtest sweet spot)
    min_samples: int = 20
    k_enter: float = 2.0             # |z| to OPEN
    k_exit: float = 0.5             # |z| to CLOSE (reverted toward the mean)
    hold_max_s: float = 3600.0       # hard time stop — exit regardless after this long
    max_spread: float = 0.012        # skip markets whose spread exceeds this (toll too high)


@dataclass(frozen=True)
class MRDecision:
    kind: str            # "enter_long" | "enter_short" | "exit" | "none"
    z: float | None
    reason: str
    option_side: str | None = None   # "yes"/"no" for enters; held leg for exits


class MRTakerEngine:
    """Per-market rolling mid buffer + flat/held state machine. Deterministic (time injected)."""

    def __init__(self, cfg: MRTakerConfig | None = None) -> None:
        self._cfg = cfg or MRTakerConfig()
        self._hist: dict[str, deque[tuple[float, float]]] = {}
        self._entry_t: dict[str, float] = {}

    def observe(self, market: str, mid: float | None, now: float | None = None) -> None:
        if mid is None:
            return
        now = time.monotonic() if now is None else now
        dq = self._hist.setdefault(market, deque())
        dq.append((now, float(mid)))
        cutoff = now - self._cfg.window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _z(self, market: str) -> float | None:
        dq = self._hist.get(market)
        if not dq or len(dq) < self._cfg.min_samples:
            return None
        vals = [v for _, v in dq]
        n = len(vals)
        m = sum(vals) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in vals) / n)
        if sd < 1e-6:
            return None
        return (vals[-1] - m) / sd

    def decide(self, market: str, *, position: int, spread: float | None,
               now: float | None = None) -> MRDecision:
        """position: our own signed YES-equivalent (>0 long YES, <0 short YES, 0 flat)."""
        now = time.monotonic() if now is None else now
        if not self._cfg.enabled:
            return MRDecision("none", None, "disabled")
        z = self._z(market)

        if position != 0:
            held_leg = "yes" if position > 0 else "no"
            entered = self._entry_t.get(market, now)
            if now - entered >= self._cfg.hold_max_s:
                return MRDecision("exit", z, "time_stop", option_side=held_leg)
            if z is None:
                return MRDecision("none", z, "holding_no_signal", option_side=held_leg)
            # reverted toward the mean, OR overshot through it against us -> reduce-only exit
            reverted = abs(z) <= self._cfg.k_exit
            flipped = (position > 0 and z >= 0) or (position < 0 and z <= 0)
            if reverted or flipped:
                return MRDecision("exit", z, "reverted" if reverted else "flipped",
                                  option_side=held_leg)
            return MRDecision("none", z, "holding", option_side=held_leg)

        # flat: consider entry (skip wide-spread markets — the toll kills it)
        if z is None:
            return MRDecision("none", z, "no_signal")
        if spread is not None and spread > self._cfg.max_spread:
            return MRDecision("none", z, "spread_too_wide")
        if z >= self._cfg.k_enter:                 # overshot UP -> fade -> SHORT YES == buy NO
            self._entry_t[market] = now
            return MRDecision("enter_short", z, "overshoot_up", option_side="no")
        if z <= -self._cfg.k_enter:                # overshot DOWN -> buy YES
            self._entry_t[market] = now
            return MRDecision("enter_long", z, "overshoot_down", option_side="yes")
        return MRDecision("none", z, "within_band")

    def clear_entry(self, market: str) -> None:
        self._entry_t.pop(market, None)
