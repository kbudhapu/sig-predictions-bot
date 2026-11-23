"""Market-making adverse-selection guard — pure logic, no I/O.

When the mid moves hard one way, a resting order on the side being hit is getting
picked off right before the market keeps moving (we provide the liquidity that informed
flow runs over). This monitor watches the mid over a short rolling window and, when the
move exceeds a threshold, PULLS (or WIDENS) the side being adversely selected:

  * mid RISING fast  → buyers are lifting our ASK then it keeps rising → pull/widen the ASK.
  * mid FALLING fast → sellers are hitting our BID then it keeps falling → pull/widen the BID.

Standalone version (mid-move only). It is designed to later also consume an order-flow /
tape signal (spec edge #6): if the tape shows informed flow, pull the quotes early. That
hook is left as a TODO so this stays dependency-free now.

Volatility parameters (threshold, window) are PLACEHOLDERS — organic volatility is unknown
pre-open; re-tune at open from realized mid moves.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .quoter import TICK, PRICE_MIN, PRICE_MAX, Quote, round_to_tick


@dataclass(frozen=True)
class AdverseConfig:
    move_threshold: float = 0.03   # |mid move| over the window that triggers a pull. PLACEHOLDER.
    window_seconds: float = 30.0   # rolling lookback. PLACEHOLDER (also: don't re-quote every second — recon §write limits).
    mode: str = "pull"             # "pull" (remove the side) | "widen" (push it out by widen_ticks)
    widen_ticks: float = 4.0       # extra offset when mode == "widen". PLACEHOLDER.
    tick: float = TICK
    price_min: float = PRICE_MIN
    price_max: float = PRICE_MAX


@dataclass
class AdverseSignal:
    market: str
    move: float                    # signed mid move over the window (latest − oldest-in-window)
    pull_bid: bool = False
    pull_ask: bool = False
    widen_bid: bool = False
    widen_ask: bool = False
    reason: str = ""

    @property
    def triggered(self) -> bool:
        return self.pull_bid or self.pull_ask or self.widen_bid or self.widen_ask


class AdverseSelectionMonitor:
    """Rolling mid history per market + a pull/widen decision. Time is injected (now=)
    so it is deterministic and non-blocking; defaults to time.monotonic()."""

    def __init__(self, cfg: AdverseConfig | None = None) -> None:
        self._cfg = cfg or AdverseConfig()
        self._hist: dict[str, deque[tuple[float, float]]] = {}

    def snapshot(self) -> dict:
        """JSON-able snapshot of the per-market mid history (for restart persistence)."""
        from infra.state.rolling_buffer import dump
        return dump(self._hist, time.monotonic())

    def restore(self, blob: dict) -> None:
        """Rehydrate the mid history from a snapshot(), re-aged onto the current clock + pruned to window."""
        from infra.state.rolling_buffer import load
        self._hist = load(blob, time.monotonic(), self._cfg.window_seconds)

    def observe(self, market: str, mid: float | None, now: float | None = None) -> None:
        """Record a mid sample and drop samples older than the window."""
        if mid is None:
            return
        now = time.monotonic() if now is None else now
        dq = self._hist.setdefault(market, deque())
        dq.append((now, float(mid)))
        self._prune(dq, now)

    def _prune(self, dq: "deque[tuple[float, float]]", now: float) -> None:
        cutoff = now - self._cfg.window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def evaluate(self, market: str, now: float | None = None) -> AdverseSignal:
        """Decide whether to pull/widen either side given the mid move over the window."""
        now = time.monotonic() if now is None else now
        dq = self._hist.get(market)
        if dq:
            self._prune(dq, now)
        if not dq or len(dq) < 2:
            return AdverseSignal(market, 0.0, reason="insufficient_history")

        oldest = dq[0][1]
        latest = dq[-1][1]
        move = round(latest - oldest, 6)
        sig = AdverseSignal(market, move)
        widen = self._cfg.mode == "widen"

        if move >= self._cfg.move_threshold:        # rising → ask picked off
            sig.widen_ask, sig.pull_ask = (True, False) if widen else (False, True)
            sig.reason = f"mid_up_{move:+.3f}_pull_ask"
        elif move <= -self._cfg.move_threshold:      # falling → bid picked off
            sig.widen_bid, sig.pull_bid = (True, False) if widen else (False, True)
            sig.reason = f"mid_down_{move:+.3f}_pull_bid"
        else:
            sig.reason = "within_threshold"
        return sig

    def apply(self, signal: AdverseSignal, quote: Quote) -> Quote:
        """Apply a signal to a quote: pull removes the side; widen pushes it further out."""
        tick = self._cfg.tick
        bid, ask = quote.bid_price, quote.ask_price
        post_bid, post_ask = quote.post_bid, quote.post_ask

        if signal.pull_ask:
            post_ask = False
        if signal.pull_bid:
            post_bid = False
        if signal.widen_ask and ask is not None:
            ask = round_to_tick(ask + self._cfg.widen_ticks * tick, tick=tick,
                                lo=self._cfg.price_min, hi=self._cfg.price_max)
        if signal.widen_bid and bid is not None:
            bid = round_to_tick(bid - self._cfg.widen_ticks * tick, tick=tick,
                                lo=self._cfg.price_min, hi=self._cfg.price_max)

        reason = quote.reason
        if signal.triggered:
            reason = f"{reason}+adverse:{signal.reason}" if reason else f"adverse:{signal.reason}"

        return Quote(quote.exchange_id, quote.mid, bid, ask, quote.bid_size, quote.ask_size,
                     post_bid=post_bid, post_ask=post_ask, reason=reason)
