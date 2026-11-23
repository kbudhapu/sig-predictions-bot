"""Stale-quote sniper decision core — pure logic, no I/O.

The venue is bot-vs-bot and liquidity is thin, so resting MM quotes go STALE whenever fair value moves
(slow re-quote cadence). The fair value here is the SIG-aligned sharp (Polymarket). When a SIG resting
order is mispriced vs the sharp beyond `min_edge`, it is a stale quote we can TAKE immediately — the
counterparty is the mispriced order itself, so the fill is not hypothetical. This solves the fill problem
cross-market's resting limits had: don't wait to be hit, LIFT the stale order before the bot re-quotes.

  * SIG ASK  <  sharp − min_edge   → someone is SELLING YES too cheap  → BUY YES at the ask (lift it).
  * SIG BID  >  sharp + min_edge   → someone is BUYING YES too dear    → BUY NO at (1 − bid) (sell YES to them).

Both are marketable TAKES of a specific resting order; hold to convergence/settlement. No deception — we
only react faster to a real price move than the bot that left the stale quote. Edge must clear min_edge
to cover being wrong about the sharp + the tick.

DEPTH SWEEP (re-quote racing): we cannot win a latency race (venue serializes orders at ~5-9s each), so the
practical "race" is to take the WHOLE stale ladder the moment we detect it, not just the touch. A take
priced at the TOUCH only crosses the best level; a take priced at the edge BOUNDARY (sharp ∓ min_edge)
is a marketable limit that sweeps EVERY level the slow bot left mispriced, in one order, while still
guaranteeing every filled unit clears min_edge vs the sharp. Quantity is still sized downstream from
capital (never a fixed share count) — the sweep only sets the marketable price so capital can actually
deploy into the full stale depth. When the L2 book is supplied we also measure the stale depth (how much
size is actually sitting mispriced) to rank the snipe; without a book we still sweep on price alone.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SniperConfig:
    enabled: bool = False
    min_edge: float = 0.03       # |sharp − touch| must exceed this to snipe (covers sharp error + tick)
    max_sharp_age_s: float = 20.0  # ignore a sharp older than this (stale sharp = no snipe)
    tick: float = 0.005
    sweep_to_edge: bool = True   # sweep the stale ladder (take more than the touch); False = touch only.
    max_cross: float = 0.02      # the LEASH: most we'll pay BEYOND the best touch when sweeping. Bounds the
                                 #   spread we cross so a thin-book gap can't drag the fill up to the edge
                                 #   boundary for only min_edge of edge. Sweep price = min(sharp∓min_edge,
                                 #   touch ± max_cross). Small (a few ticks) = take near-touch depth only.


@dataclass(frozen=True)
class SniperTake:
    exchange_id: str
    option_side: str             # "yes" (lift a cheap ask) | "no" (hit a dear bid)
    price: float                 # marketable price we pay (touch, or the edge-boundary sweep price, snapped)
    edge: float                  # |sharp − touch|, the captured gap AT THE BEST LEVEL (the ranking edge)
    reason: str
    stale_depth: float | None = None  # L2 size sitting mispriced beyond min_edge (None when no book given)


def _snap(p: float, tick: float) -> float:
    return min(max(round(round(p / tick) * tick, 3), 0.005), 0.995)


def _mispriced_depth(levels, *, cheaper_than: float | None = None,
                     dearer_than: float | None = None) -> float:
    """Total resting quantity across L2 levels whose price clears the edge boundary.

    levels = [{"price": p, "quantity": q}, ...]. For an ASK sweep pass cheaper_than=sharp−min_edge (count
    asks priced at/below it); for a BID sweep pass dearer_than=sharp+min_edge (count bids at/above it)."""
    total = 0.0
    for lv in levels or []:
        try:
            p = float(lv.get("price"))
            q = float(lv.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        if cheaper_than is not None and p <= cheaper_than + 1e-9:
            total += q
        elif dearer_than is not None and p >= dearer_than - 1e-9:
            total += q
    return total


def evaluate(exchange_id: str, sig_bid: float | None, sig_ask: float | None,
             aligned_sharp: float | None, sharp_age_s: float | None,
             cfg: SniperConfig, *, bids=None, asks=None) -> SniperTake | None:
    """Return a snipe take when a SIG touch is stale vs the sharp by >= min_edge, else None.

    aligned_sharp is the SIG-aligned Polymarket YES probability (fair value). Requires a fresh sharp.
    bids/asks (optional L2 ladders of {price, quantity}) only MEASURE the stale depth for ranking — the
    trigger is the NBBO touch and the sweep price is derived from the sharp, so the snipe works with or
    without a book."""
    if not cfg.enabled or aligned_sharp is None:
        return None
    if not (0.0 < aligned_sharp < 1.0):
        return None
    if sharp_age_s is not None and sharp_age_s > cfg.max_sharp_age_s:
        return None
    # underpriced ask → lift it (buy YES cheap). When sweeping, pay up to min(sharp−min_edge, touch+max_cross)
    # — the leash caps the spread crossed so a thin-book gap can't drag us to the edge boundary; clamped
    # >= the touch so it stays marketable. Every filled unit still clears min_edge (price <= sharp−min_edge).
    if sig_ask is not None and 0.0 < sig_ask < 1.0:
        edge = aligned_sharp - sig_ask
        if edge >= cfg.min_edge:
            if cfg.sweep_to_edge:
                boundary = min(aligned_sharp - cfg.min_edge, sig_ask + cfg.max_cross)
                price = max(_snap(boundary, cfg.tick), _snap(sig_ask, cfg.tick))
            else:
                price = _snap(sig_ask, cfg.tick)
            depth = _mispriced_depth(asks, cheaper_than=price) if asks else None
            return SniperTake(exchange_id, "yes", price, round(edge, 4), "stale_ask_underpriced", depth)
    # overpriced bid → hit it (buy NO / sell YES dear). When sweeping, sell YES down to max(sharp+min_edge,
    # bid−max_cross) — the leash again caps the crossed spread — i.e. buy NO at 1 − that floor; clamped
    # >= the touch's NO price so it stays marketable.
    if sig_bid is not None and 0.0 < sig_bid < 1.0:
        edge = sig_bid - aligned_sharp
        if edge >= cfg.min_edge:
            if cfg.sweep_to_edge:
                sell_floor = max(aligned_sharp + cfg.min_edge, sig_bid - cfg.max_cross)
                price = max(_snap(1.0 - sell_floor, cfg.tick), _snap(1.0 - sig_bid, cfg.tick))
            else:
                price = _snap(1.0 - sig_bid, cfg.tick)
            # depth taken = bids we'd sell into, i.e. bid levels priced at/above our sell floor (1 − price).
            depth = _mispriced_depth(bids, dearer_than=1.0 - price) if bids else None
            return SniperTake(exchange_id, "no", price, round(edge, 4), "stale_bid_overpriced", depth)
    return None
