"""Order-book pressure (queue-imbalance) decision core — pure logic, no I/O.

Microstructure fact: when the resting depth is lopsided — deep bids, thin asks — the thin side gets
consumed first, so the short-horizon price drifts TOWARD the thin side (bids-heavy ⇒ price up; asks-heavy
⇒ price down). This is queue/order-book imbalance, one of the most robust short-horizon return predictors
in real books (Cont–Kukanov–Stoikov and many since). On this thin bot venue the counterparties are MMs
leaving lopsided ladders, so the signal should be strong — AND it needs NO external sharp, so it covers the
pure-SIG races cross-market and the sniper cannot (no Polymarket comparable).

    QI = (bid_depth − ask_depth) / (bid_depth + ask_depth)   ∈ [−1, +1]   (top-N levels)

  QI > 0  (bids deeper)  → pressure UP   → back YES (buy YES)
  QI < 0  (asks deeper)  → pressure DOWN → fade YES (buy NO)

Optional tape confirmation: require recent taker flow (signals/trade_flow imbalance) to agree, so we only
act when BOTH the resting book AND the aggressor flow point the same way. No deception — we react to real
resting liquidity and real prints. SHADOW-FIRST: this only produces a signal; whether it predicts the next
move is measured from recorded signals before a cent is risked.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookPressureConfig:
    enabled: bool = False
    levels: int = 5              # how many book levels deep to sum for the imbalance
    min_imbalance: float = 0.33  # |QI| must exceed this to signal (0.33 ≈ 2:1 depth ratio)
    min_depth: float = 100.0     # total top-N depth (bid+ask) below this = too thin to trust
    tape_confirm: bool = False   # require the taker-flow tape to agree with the book pressure
    tape_min: float = 0.20       # if confirming, |tape imbalance| must be at least this, same sign
    tick: float = 0.005


@dataclass(frozen=True)
class BookPressureSignal:
    exchange_id: str
    option_side: str             # "yes" (pressure up, back YES) | "no" (pressure down, fade YES)
    qi: float                    # signed queue imbalance in [-1, 1]
    bid_depth: float
    ask_depth: float
    tape_imbalance: float | None
    reason: str                  # "bid_heavy" | "ask_heavy"


def _depth(levels, n: int) -> float:
    """Sum the quantity over the first n price levels of one side of the book."""
    total = 0.0
    for lv in (levels or [])[: max(0, int(n))]:
        try:
            q = float(lv.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
        if q > 0:
            total += q
    return total


def evaluate(exchange_id: str, bids, asks, cfg: BookPressureConfig,
             *, tape_imbalance: float | None = None) -> BookPressureSignal | None:
    """Return a book-pressure signal when the top-N queue imbalance is extreme (and, if configured, the
    taker tape confirms), else None. bids/asks are L2 ladders of {price, quantity}, best-first."""
    if not cfg.enabled:
        return None
    bd = _depth(bids, cfg.levels)
    ad = _depth(asks, cfg.levels)
    tot = bd + ad
    if tot < cfg.min_depth:
        return None                       # too thin → imbalance is noise, not information
    qi = (bd - ad) / tot
    if abs(qi) < cfg.min_imbalance:
        return None
    direction_up = qi > 0                 # bids deeper → pressure UP → back YES
    if cfg.tape_confirm and tape_imbalance is not None:
        # the tape must agree AND be meaningfully one-sided; a contradicting/flat tape vetoes.
        if direction_up and tape_imbalance < cfg.tape_min:
            return None
        if (not direction_up) and tape_imbalance > -cfg.tape_min:
            return None
    return BookPressureSignal(
        exchange_id=exchange_id,
        option_side="yes" if direction_up else "no",
        qi=round(qi, 4), bid_depth=bd, ask_depth=ad,
        tape_imbalance=(None if tape_imbalance is None else round(float(tape_imbalance), 4)),
        reason="bid_heavy" if direction_up else "ask_heavy",
    )
