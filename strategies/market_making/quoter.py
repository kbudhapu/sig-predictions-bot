"""Market-making quoter — pure logic, no I/O (a separate executor places the orders).

Given a market's current touch (bestBid / bestAsk), compute symmetric resting limit
prices INSIDE the spread: bid = mid − k, ask = mid + k, both snapped to the 0.005 tick
and kept strictly inside the touch and within the [0.005, 0.995] price bounds.

SIG market facts (recon/MARKET_RECON.md):
  * price tick = 0.005, price bounds [0.005, 0.995]; quantities are integer contracts.
  * spreads are uniformly 0.08 pre-open (wide → lots of room to rest inside).
  * liquidity is a single seeded level of ≤1000 contracts; organic depth is unknown
    until open — so every size / liquidity parameter here is a PLACEHOLDER to re-tune
    at open (see QuoterConfig).
"""
from __future__ import annotations

from dataclasses import dataclass

TICK = 0.005
PRICE_MIN = 0.005
PRICE_MAX = 0.995
_EPS = 1e-9


def round_to_tick(price: float, *, tick: float = TICK,
                  lo: float = PRICE_MIN, hi: float = PRICE_MAX) -> float:
    """Snap a price to the nearest tick and clamp to the tradeable band. Result is
    rounded to 3 dp so it is an exact multiple of 0.005 (no float dust)."""
    n = round(price / tick)
    snapped = round(n * tick, 3)
    return min(max(snapped, lo), hi)


@dataclass(frozen=True)
class QuoterConfig:
    # k = how far inside the mid each side rests (the half-width we TRY to quote). The
    # effective offset is capped so both quotes stay strictly inside the live touch.
    k: float = 0.015            # PLACEHOLDER — tune at open vs realized fill rate / spread.
    size: int = 100             # contracts per side. PLACEHOLDER — depth is a ≤1000 wall pre-open; organic depth unknown.
    tick: float = TICK
    price_min: float = PRICE_MIN
    price_max: float = PRICE_MAX
    side: str = "yes"           # single YES exchange per market (recon §2); we quote YES both ways.
    join_when_tight: bool = True  # when the spread is too tight to rest INSIDE, JOIN the touch (post at
                                  #   best_bid/best_ask) instead of suppressing — else MM posts nothing on
                                  #   tight-spread markets (the 1%-fill-rate cause) where most trading is.


@dataclass
class Quote:
    """A two-sided resting quote. A side with post_* = False should NOT be rested
    (no room inside the spread, or a one-sided/again absent book)."""
    exchange_id: str
    mid: float | None
    bid_price: float | None
    ask_price: float | None
    bid_size: int
    ask_size: int
    post_bid: bool
    post_ask: bool
    reason: str = ""            # why a side (or both) is suppressed — for the executor's log


def compute_quote(exchange_id: str, best_bid: float | None, best_ask: float | None,
                  cfg: QuoterConfig | None = None) -> Quote:
    """Symmetric resting limits inside the spread, tick- and bound-respecting.

    Requires a two-sided touch to form a mid; a one-sided/empty book is skipped
    (pre-open many books are one-sided — recon §4; synthesising a mid from a single
    side is deliberately left to a later, depth-aware revision)."""
    cfg = cfg or QuoterConfig()
    tick = cfg.tick

    if best_bid is None or best_ask is None or best_ask <= best_bid:
        return Quote(exchange_id, None, None, None, cfg.size, cfg.size,
                     post_bid=False, post_ask=False, reason="one_sided_or_crossed_book")

    mid = (best_bid + best_ask) / 2.0
    # The band of prices strictly inside the touch (at least one tick off each side).
    band_lo = round_to_tick(best_bid + tick, tick=tick, lo=cfg.price_min, hi=cfg.price_max)
    band_hi = round_to_tick(best_ask - tick, tick=tick, lo=cfg.price_min, hi=cfg.price_max)

    # Need room for two distinct inside ticks (bid < ask): spread must be ≥ ~3 ticks. When there is NOT
    # room to rest strictly inside, JOIN the touch (post AT best_bid/best_ask) rather than post nothing —
    # on this venue most spreads are 1-2 ticks, so suppressing here is why MM barely filled (1% fill rate,
    # yet +exec_edge when it did). Joining becomes the best quote and captures the touch spread when both
    # sides trade; the adverse-selection monitor + inventory skew (applied upstream) manage the risk.
    if band_hi - band_lo < tick - _EPS:
        if cfg.join_when_tight and (best_ask - best_bid) >= tick - _EPS:
            jb = round_to_tick(best_bid, tick=tick, lo=cfg.price_min, hi=cfg.price_max)
            ja = round_to_tick(best_ask, tick=tick, lo=cfg.price_min, hi=cfg.price_max)
            if ja - jb >= tick - _EPS:
                return Quote(exchange_id, round(mid, 5), jb, ja, cfg.size, cfg.size,
                             post_bid=True, post_ask=True, reason="joined_touch")
        return Quote(exchange_id, round(mid, 5), None, None, cfg.size, cfg.size,
                     post_bid=False, post_ask=False, reason="spread_too_tight")

    bid = round_to_tick(mid - cfg.k, tick=tick, lo=cfg.price_min, hi=cfg.price_max)
    ask = round_to_tick(mid + cfg.k, tick=tick, lo=cfg.price_min, hi=cfg.price_max)
    # Clamp into the inside band and guarantee bid < ask by at least one tick.
    bid = min(max(bid, band_lo), round(band_hi - tick, 3))
    ask = max(min(ask, band_hi), round(bid + tick, 3))

    return Quote(exchange_id, round(mid, 5), bid, ask, cfg.size, cfg.size,
                 post_bid=True, post_ask=True, reason="")
