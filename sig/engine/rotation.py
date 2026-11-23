"""Cross-market CAPITAL ROTATION — pure decision logic (no I/O).

When the book is fully deployed, a new divergence can only be funded by closing something. The weakest
holding is the one with the least REMAINING favourable gap to the sharp (it has mostly converged: little edge
left). Swap it for the best new candidate only when the candidate's gap beats that remaining gap by a margin
PLUS both round-trip spreads — so a swap is never just churn.

Safety by construction (the wiki lesson): a swap never ADDS to a race (the candidate is a race we don't hold),
the new position is sized to the same per-market cap, and the sold leg is an existing holding — so rotation
can only move capital from a converged bet to a fresher one, never concentrate or average down.

Backtest (2026-10-06, 13.4h, 98 races): +$500–1,500 EV (~+10%) vs no rotation across sharp-error scenarios,
2–7 swaps/13h, best margin ≈ 4c.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Held:
    exchange_id: str
    qty: float            # signed: + YES, − NO
    sig_mid: float        # SIG YES mid
    sharp: float          # SIG-aligned sharp YES prob
    half_spread: float
    market_value: float

    @property
    def remaining_gap(self) -> float:
        """Favourable gap still left to capture (0 if the sharp now sits against us)."""
        g = (self.sharp - self.sig_mid) if self.qty > 0 else (self.sig_mid - self.sharp)
        return max(g, 0.0)


@dataclass(frozen=True)
class Candidate:
    exchange_id: str
    sig_mid: float
    sharp: float
    half_spread: float

    @property
    def gap(self) -> float:
        return abs(self.sharp - self.sig_mid)

    @property
    def side(self) -> str:
        return "yes" if self.sharp > self.sig_mid else "no"


@dataclass(frozen=True)
class Swap:
    sell: Held
    buy: Candidate
    advantage: float      # candidate gap − held remaining gap − 2×(both half-spreads)


def entry_threshold(sharp: float, cost_floor: float = 0.05) -> float:
    """Mirror of cross-market's conviction-weighted entry threshold."""
    conv = abs(sharp - 0.5) * 2.0
    return float("inf") if conv <= 1e-9 else max(cost_floor, cost_floor / conv)


def decide_rotation(held: list[Held], candidates: list[Candidate], *, cash: float, position_size: float,
                    margin: float = 0.04, max_swaps: int = 1, cost_floor: float = 0.05,
                    max_gap: float = 0.25, min_held_value: float = 500.0) -> list[Swap]:
    """Return up to `max_swaps` swaps (best first). No swap when free cash can already fund a position
    (cross-market will just buy) or when nothing clears margin + spreads."""
    if cash >= position_size:
        return []
    held_ids = {h.exchange_id for h in held}
    pool = sorted((h for h in held if abs(h.market_value) >= min_held_value), key=lambda h: h.remaining_gap)
    cands = sorted((c for c in candidates
                    if c.exchange_id not in held_ids and entry_threshold(c.sharp, cost_floor) <= c.gap < max_gap),
                   key=lambda c: -c.gap)
    out: list[Swap] = []
    used: set[str] = set()
    for c in cands:
        if len(out) >= max_swaps:
            break
        for h in pool:
            if h.exchange_id in used:
                continue
            adv = c.gap - h.remaining_gap - 2.0 * (c.half_spread + h.half_spread)
            if adv >= margin:
                out.append(Swap(sell=h, buy=c, advantage=round(adv, 4)))
                used.add(h.exchange_id)
            break                       # pool is sorted weakest-first: if the weakest fails, all fail
    return out
