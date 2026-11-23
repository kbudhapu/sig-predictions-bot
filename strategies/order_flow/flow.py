"""Order-flow ingestion + imbalance — pure logic, no I/O (SIGNAL-tier edge, spec §2.3).

Ingests the trade tape (a stream of executed trades) and computes, per market, the
rolling BUY-vs-SELL pressure imbalance and whether it is a one-sided EXTREME. This is
the raw material for the fade signal (signal.py) and the market-making informed-flow
hook — it does NOT decide to fade here (that needs the other signals; see signal.py).

Pressure convention (single YES exchange per market — recon §2): a trade pushes the YES
price UP (+1) if it is a YES buy or a NO sell, DOWN (−1) if a YES sell or a NO buy.
imbalance ∈ [−1, +1]: +1 = the crowd is all buying (pushing price up), −1 = all selling.

All thresholds (window, what counts as "extreme", min volume) are PLACEHOLDERS — organic
flow is unknown pre-open (recon: books are a single seeded ≤1000-contract level with no
real tape yet). Re-tune at open from the realized tape.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Trade:
    exchange_id: str
    action: str            # "buy" | "sell"
    quantity: float
    price: float | None = None
    timestamp: float | None = None     # seconds (monotonic/epoch); engine injects now if absent
    side: str = "yes"      # SIG option side of the exchange ("yes" | "no")


@dataclass(frozen=True)
class FlowConfig:
    window_seconds: float = 60.0       # rolling tape window. PLACEHOLDER — tune at open.
    extreme_threshold: float = 0.6     # |imbalance| at/above this = one-sided extreme. PLACEHOLDER.
    min_volume: float = 200.0          # need this much windowed contract volume to trust it. PLACEHOLDER.
    min_trades: int = 3                # and at least this many trades (not one fat print). PLACEHOLDER.


@dataclass
class FlowState:
    exchange_id: str
    up_qty: float          # contracts pushing price up (YES buys + NO sells)
    down_qty: float        # contracts pushing price down (YES sells + NO buys)
    total_qty: float
    n_trades: int
    imbalance: float       # (up − down) / total, in [−1, 1]; 0 when no volume
    extreme: bool          # one-sided AND enough volume/trades to trust
    direction: int         # sign of the crowd's price pressure: +1 up, −1 down, 0 flat


def pressure_sign(action: str, side: str = "yes") -> int:
    """+1 if the trade pushes the YES price up (YES buy / NO sell), −1 if down."""
    buy = str(action).lower() == "buy"
    yes = str(side or "yes").lower() != "no"
    return 1 if (buy == yes) else -1


class FlowBook:
    """Rolling per-market tape → imbalance. Time is injected (now=) so it is deterministic
    and non-blocking; defaults to time.monotonic()."""

    def __init__(self, cfg: FlowConfig | None = None) -> None:
        self._cfg = cfg or FlowConfig()
        # per market: deque of (timestamp, pressure_sign, quantity)
        self._tape: dict[str, deque[tuple[float, int, float]]] = {}

    def ingest(self, trade: Trade, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        ts = trade.timestamp if trade.timestamp is not None else now
        dq = self._tape.setdefault(trade.exchange_id, deque())
        dq.append((ts, pressure_sign(trade.action, trade.side), float(trade.quantity)))
        self._prune(dq, now)

    def ingest_many(self, trades, now: float | None = None) -> None:
        for t in trades:
            self.ingest(t, now)

    def _prune(self, dq: "deque[tuple[float, int, float]]", now: float) -> None:
        cutoff = now - self._cfg.window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def state(self, exchange_id: str, now: float | None = None) -> FlowState:
        now = time.monotonic() if now is None else now
        dq = self._tape.get(exchange_id)
        if dq:
            self._prune(dq, now)
        up = down = 0.0
        n = 0
        if dq:
            n = len(dq)
            for _ts, sign, qty in dq:
                if sign > 0:
                    up += qty
                else:
                    down += qty
        total = up + down
        imbalance = round((up - down) / total, 6) if total > 0 else 0.0
        extreme = (total >= self._cfg.min_volume and n >= self._cfg.min_trades
                   and abs(imbalance) >= self._cfg.extreme_threshold)
        direction = 0 if imbalance == 0 else (1 if imbalance > 0 else -1)
        return FlowState(exchange_id, up, down, total, n, imbalance, extreme, direction)
