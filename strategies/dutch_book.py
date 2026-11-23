"""Dutch-book (complement) arbitrage decision core — pure logic, no I/O.

The two party legs of one race (D-wins and R-wins) are near-perfect complements: between them they
cover ~all probability, so their YES prices should sum to ~1.0. When the SUM OF THE YES BIDS exceeds 1,
the pair is OVER-priced and you can SHORT BOTH — buy NO on each leg — for a guaranteed profit:

    buy NO_D @ (1 - yesbid_D) + buy NO_R @ (1 - yesbid_R)  costs  2 - (yesbid_D + yesbid_R)
    payoff is >= 1 in EVERY outcome (D wins, R wins, OR a third party wins)
    => locked profit per pair = (yesbid_D + yesbid_R) - 1,  robust to the third-party tail.

This is the robust side only. The sum<1 ("buy both YES") side is NOT traded here — it loses the stake
if a third party wins (Nebraska/Osborn), and the operator wants the near-riskless book.

SAFETY: this is an ARB — both legs must fill together or neither (the caller routes it through the
atomic multi-leg path). A single leg alone is a naked directional NO, which is exactly what this must
never leave open. We only decide; the atomic OMS path guarantees all-or-nothing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DutchBookConfig:
    enabled: bool = False
    min_edge: float = 0.01       # minimum (sum_bid - 1) to bother (covers fees/slippage/odd-lot)
    max_pairs: int = 40          # cap pairs considered per cycle (read/write budget hygiene)
    tick: float = 0.005


@dataclass(frozen=True)
class DutchBookLeg:
    exchange_id: str
    outcome: str                 # always "no" for short-both
    price: float                 # marketable NO price we pay (= 1 - yes_bid, snapped)


@dataclass(frozen=True)
class DutchBookArb:
    contest: str
    edge: float                  # locked profit per pair in prob (sum_bid - 1)
    legs: tuple[DutchBookLeg, DutchBookLeg]


def _snap(p: float, tick: float) -> float:
    return min(max(round(round(p / tick) * tick, 3), 0.005), 0.995)


class DutchBookEngine:
    """Thin stateless wrapper so the live adapter can hold one object. All logic is in evaluate_pair."""

    def __init__(self, cfg: DutchBookConfig | None = None) -> None:
        self._cfg = cfg or DutchBookConfig()

    def evaluate_pair(self, contest, d_exch, r_exch, d_yes_bid, r_yes_bid, cfg=None):
        return evaluate_pair(contest, d_exch, r_exch, d_yes_bid, r_yes_bid, cfg or self._cfg)


def evaluate_pair(contest: str, d_exch: str, r_exch: str,
                  d_yes_bid: float | None, r_yes_bid: float | None,
                  cfg: DutchBookConfig) -> DutchBookArb | None:
    """Return a short-both arb when the two YES bids sum above 1 by at least min_edge, else None.

    Uses the BIDS: shorting YES ≡ buying NO, and buying NO is marketable at (1 - yes_bid) — i.e. we
    lift the NO offer that the resting YES bid implies. Both legs priced that way; equal quantity is
    set by the caller (the pair must be 1:1 to be a complement hedge)."""
    if not cfg.enabled or d_yes_bid is None or r_yes_bid is None:
        return None
    if not (0.0 < d_yes_bid < 1.0 and 0.0 < r_yes_bid < 1.0):
        return None
    edge = (d_yes_bid + r_yes_bid) - 1.0
    if edge < cfg.min_edge:
        return None
    d_leg = DutchBookLeg(d_exch, "no", _snap(1.0 - d_yes_bid, cfg.tick))
    r_leg = DutchBookLeg(r_exch, "no", _snap(1.0 - r_yes_bid, cfg.tick))
    return DutchBookArb(contest, round(edge, 4), (d_leg, r_leg))
