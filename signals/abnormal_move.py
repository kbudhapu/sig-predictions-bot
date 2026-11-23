"""SIG abnormal-move detection — the FREE, all-237 event trigger (PART 1 of the event-trigger edge).

Every SIG market is densely traded (students trade even the obscure races: 200-1360 trades in the
first ~11h), and GET /exchanges/{id}/price-history exposes OHLCV + tradeCount + vwap candles for ALL
237. So we can detect, per race and with no external feed, WHEN something moved a market: a price jump
that exceeds the market's own recent wobble, and/or a burst of trade activity. That `abnormal_move`
event is the trigger; PART 2 (news volume) then tells us whether it was JUSTIFIED (follow) or OVERDONE
(fade) — direction comes from the move + that discriminator, never from classifying news content.

THIN-HISTORY DISCIPLINE. The competition is young (hourly candles are few), so the default resolution is
5m — ~12 candles/hour means a usable baseline almost immediately — and we REFUSE to fire with fewer than
`min_candles` (no false-firing on the first few buckets). Because SIG omits empty buckets (sparse series
on a very thin market), the detector keys off the candle COUNT it actually has, not a wall-clock window.

PURITY. `detect_abnormal_move` is pure: candles in, event out. The only I/O is `fetch_price_history`,
injected so tests run fully offline. READ-ONLY — this module only GETs price history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV+count bucket from /exchanges/{id}/price-history (prices YES-normalized)."""
    time: str
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    trade_count: int

    @classmethod
    def from_api(cls, d: dict) -> "Candle":
        f = lambda k, dv=0.0: float(d.get(k, dv) or 0.0)  # noqa: E731
        return cls(
            time=str(d.get("time") or d.get("t") or ""),
            open=f("open"), high=f("high"), low=f("low"), close=f("close"),
            vwap=f("vwap", f("close")), volume=f("volume"),
            trade_count=int(d.get("tradeCount", d.get("trade_count", 0)) or 0),
        )


@dataclass(frozen=True)
class AbnormalMoveConfig:
    """Tunable thresholds. Defaults chosen for 5m candles on a young competition."""
    resolution: str = "5m"               # "1m"|"5m"|"1h"|"1d" — 5m gives a baseline on ~11h of history
    baseline_window: int = 12            # candles used for the rolling baseline (EXCLUDES the latest)
    min_candles: int = 6                 # thin-history guard: never fire with fewer than this
    move_mult: float = 3.0               # |Δclose| ≥ move_mult × baseline range ⇒ a price anomaly
    min_move: float = 0.03               # absolute floor (a 3¢ SIG tick-ish) — ignore sub-noise moves
    tradecount_mult: float = 3.0         # latest tradeCount ≥ mult × recent median ⇒ an activity spike
    revert_frac: float = 0.5             # a move that undoes ≥ this fraction of the prior move = reverting


@dataclass(frozen=True)
class AbnormalMove:
    """One detected anomaly on one market — the trigger PART 3 consumes."""
    exchange_id: str
    direction: str                       # "up" | "down"  (sign of Δclose)
    move_size: float                     # |latest.close − prev.close|
    baseline: float                      # rolling mean candle range (the 'normal wobble')
    move_ratio: float                    # move_size / baseline
    tradecount_spike: bool
    tradecount_ratio: float
    reverting: bool                      # latest move reverses a large prior move (overdone/whipsaw tell)
    last_close: float
    prev_close: float
    timestamp: str
    n_candles: int
    reasons: tuple[str, ...] = field(default_factory=tuple)   # ("move",) and/or ("tradecount",)


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def detect_abnormal_move(
    exchange_id: str, candles: list[Candle], cfg: AbnormalMoveConfig | None = None
) -> AbnormalMove | None:
    """Pure detector. Returns an AbnormalMove iff the latest candle is anomalous vs the market's own
    recent baseline, else None. Fires on a price jump (Δclose ≥ move_mult × baseline range, and ≥
    min_move absolute) and/or a tradeCount burst (≥ tradecount_mult × recent median). Refuses to fire
    with fewer than `min_candles` (thin-history guard)."""
    cfg = cfg or AbnormalMoveConfig()
    if len(candles) < cfg.min_candles:
        return None                                       # too little history — do NOT guess
    latest, prev = candles[-1], candles[-2]
    window = candles[-(cfg.baseline_window + 1):-1]       # baseline EXCLUDES the latest candle
    ranges = [max(c.high - c.low, 0.0) for c in window]
    baseline = (sum(ranges) / len(ranges)) if ranges else 0.0

    move = abs(latest.close - prev.close)
    move_ratio = (move / baseline) if baseline > 1e-9 else (move / cfg.min_move if cfg.min_move > 0 else 0.0)
    move_trigger = move >= cfg.min_move and (
        move >= cfg.move_mult * baseline if baseline > 1e-9 else move >= 2.0 * cfg.min_move
    )

    counts = [c.trade_count for c in window]
    med = _median([float(c) for c in counts])
    tc_ratio = (latest.trade_count / med) if med > 0 else (float(latest.trade_count) if latest.trade_count else 0.0)
    tc_trigger = med > 0 and latest.trade_count >= cfg.tradecount_mult * med

    if not (move_trigger or tc_trigger):
        return None

    # reverting: the latest move undoes a large fraction of the PREVIOUS candle's move (a spike that is
    # already snapping back — a strong 'students overreacted' / whipsaw tell for the fade branch).
    prev_move = prev.close - (candles[-3].close if len(candles) >= 3 else prev.open)
    cur_move = latest.close - prev.close
    reverting = (
        abs(prev_move) >= cfg.min_move
        and (cur_move * prev_move) < 0
        and abs(cur_move) >= cfg.revert_frac * abs(prev_move)
    )
    reasons = tuple(r for r, ok in (("move", move_trigger), ("tradecount", tc_trigger)) if ok)
    return AbnormalMove(
        exchange_id=str(exchange_id),
        direction="up" if latest.close >= prev.close else "down",
        move_size=round(move, 6),
        baseline=round(baseline, 6),
        move_ratio=round(move_ratio, 4),
        tradecount_spike=tc_trigger,
        tradecount_ratio=round(tc_ratio, 4),
        reverting=reverting,
        last_close=latest.close,
        prev_close=prev.close,
        timestamp=latest.time,
        n_candles=len(candles),
        reasons=reasons,
    )


# ── live scan (the only I/O; injected in tests) ─────────────────────────────────────────────────── #
def candles_from_price_history(resp: dict | list) -> list[Candle]:
    """Normalize a /exchanges/{id}/price-history response into Candles (oldest→newest)."""
    rows = resp if isinstance(resp, list) else (resp.get("candles") or resp.get("data") or [])
    return [Candle.from_api(r) for r in rows if isinstance(r, dict)]


def scan_markets(
    exchange_ids: list[str],
    fetch_price_history: Callable[[str], Any],
    cfg: AbnormalMoveConfig | None = None,
) -> list[AbnormalMove]:
    """Detect abnormal moves across many markets (all 237). `fetch_price_history(exchange_id)` returns
    the raw price-history response (dict/list); injected so tests are offline + the live caller owns the
    SIG read client, resolution, and the GDELT-style rate budget. One bad market never drops the rest."""
    cfg = cfg or AbnormalMoveConfig()
    out: list[AbnormalMove] = []
    for ex in exchange_ids:
        try:
            candles = candles_from_price_history(fetch_price_history(ex))
        except Exception:  # noqa: BLE001 — a single market's fetch error must not abort the sweep
            continue
        mv = detect_abnormal_move(ex, candles, cfg)
        if mv is not None:
            out.append(mv)
    return out
