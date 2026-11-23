"""Capital ROTATION v2 shadow — pure logic (no I/O). Runs beside the v1 shadow (sig/engine/rotation.py).

v1 post-mortem (2026-10-07): 29 records were ~5 distinct swaps re-recorded every 30 min; it was
scored on 1-24h SIG marks, which show the 1.2c up-front cost but not the benefit (gaps are persistent biases
that close over days / at settlement); and it bought the biggest gap, which is often one that just opened and
keeps widening for ~1h. v2 changes exactly those three things:

  1. VIRTUAL BOOK — each swap is "executed" virtually: the sold market leaves the virtual holdings and the
     bought market enters them, so the same swap can never be recorded twice and a sold holding is never
     sold again. Real holdings are the starting point; virtual swaps are layered on top.
  2. PERSISTENCE — a candidate is only eligible after its gap has been live (>= entry threshold, same side)
     continuously for `persist_s` (default 60 min). Fresh gaps that are still widening are skipped.
  3. EXECUTION PRICES RECORDED — the sold leg at its bid, the bought leg at its ask (held-side), so the
     validator can value the swap at the sharp vs simply having held (tools/rotation_v2_validate.py).
The swap choice itself reuses v1's decide_rotation (margin + both round trips must be beaten).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sig.engine.rotation import Candidate, Held, decide_rotation, entry_threshold


@dataclass
class VirtualSwap:
    swap_id: str
    ts: str
    sell_ex: str
    sold_qty: float          # signed held qty given up (+ YES, - NO)
    sell_px: float           # held-side exit price (YES bid, or NO bid = 1 - YES ask)
    buy_ex: str
    buy_side: str            # "yes" | "no"
    buy_px: float            # held-side entry price (YES ask, or NO ask = 1 - YES bid)
    new_shares: float        # proceeds / buy_px


@dataclass
class PersistenceTracker:
    """When did each candidate's gap become continuously live on its current side?"""
    since: dict = field(default_factory=dict)      # ex -> (side, first_live_epoch)

    def update(self, ex: str, sharp: float, mid: float, now: float, cost_floor: float = 0.05) -> float:
        """Record this observation; return how long (s) the gap has been live on this side (0 if not live)."""
        gap = abs(sharp - mid)
        side = "yes" if sharp > mid else "no"
        if not (entry_threshold(sharp, cost_floor) <= gap):
            self.since.pop(ex, None)
            return 0.0
        prev = self.since.get(ex)
        if prev is None or prev[0] != side:
            self.since[ex] = (side, now)
            return 0.0
        return now - prev[1]


def held_side_prices(qty_or_side, bid: float, ask: float) -> tuple[float, float]:
    """(exit_px, entry_px) for the held side: YES trades at bid/ask; NO exits at 1-ask and enters at 1-bid."""
    yes = (qty_or_side > 0) if isinstance(qty_or_side, (int, float)) else (qty_or_side == "yes")
    return (bid, ask) if yes else (1.0 - ask, 1.0 - bid)


def _epoch(ts: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001 — unparseable stamp: treat as "now" (stays frozen)
        return float("inf")


def apply_virtual(held: list[Held], swaps: list[VirtualSwap], *, now: float | None = None,
                  freeze_s: float | None = None) -> tuple[list[Held], set[str]]:
    """Real holdings adjusted by the virtual swaps. Returns (sellable holdings, markets frozen for v2).
    freeze_s None (v2 default): every swapped market is frozen forever and dropped from the sellable book.
    freeze_s set (retune 2026-10-09): a market is frozen only for freeze_s after its swap, and holdings carry the
    VIRTUAL quantity (real - virtually sold + virtually bought) so partially-sold shares are never sold twice."""
    if freeze_s is None:
        frozen = {s.sell_ex for s in swaps} | {s.buy_ex for s in swaps}
        return [h for h in held if h.exchange_id not in frozen], frozen
    import time as _t
    now = _t.time() if now is None else now
    frozen = {x for s in swaps if now - _epoch(s.ts) < freeze_s for x in (s.sell_ex, s.buy_ex)}
    delta: dict = {}
    for s in swaps:
        delta[s.sell_ex] = delta.get(s.sell_ex, 0.0) - s.sold_qty
        delta[s.buy_ex] = delta.get(s.buy_ex, 0.0) + (s.new_shares if s.buy_side == "yes" else -s.new_shares)
    out = []
    for h in held:
        q = h.qty + delta.get(h.exchange_id, 0.0)
        if h.exchange_id in frozen or abs(q) < 1 or (q > 0) != (h.qty > 0):
            continue
        out.append(Held(h.exchange_id, q, h.sig_mid, h.sharp, h.half_spread, h.market_value * abs(q) / abs(h.qty)))
    return out, frozen


def cap_for(gap: float, position_size: float, big_position_size: float | None = None, big_gap: float = 0.07) -> float:
    """Per-market cap: the bigger cap (operator 2026-10-16: 10% of NAV) only where the edge vs Poly is >= big_gap."""
    if big_position_size and big_position_size > position_size and gap >= big_gap - 1e-9:
        return big_position_size
    return position_size


def decide_v2(held: list[Held], candidates: list[Candidate], swaps: list[VirtualSwap], *, persisted: dict,
              persist_s: float, cash: float, position_size: float, margin: float = 0.04,
              cost_floor: float = 0.05, min_held_value: float = 500.0, max_open: int = 20,
              topup: bool = False, freeze_s: float | None = None, now: float | None = None,
              min_room_usd: float = 200.0, big_position_size: float | None = None, big_gap: float = 0.07):
    """One v2 decision: v1's rule on the VIRTUAL book, restricted to candidates live for >= persist_s.

    topup=True (retune 2026-10-09, tools/research/rotation_replay.py): the candidate may be a market we already
    hold ON THE SAME SIDE with room under the per-market cap (position_size) — every >= 8c edge on SIG was already in
    the book, so new-only rotation starved (~1 swap/day). Never trades against a holding, never sells the market it
    buys. max_open is not applied (the freeze window + per-hour cap bound the rate)."""
    if not topup:
        if len(swaps) >= max_open:
            return []
        sellable, frozen = apply_virtual(held, swaps, now=now, freeze_s=freeze_s)
        held_ids = {h.exchange_id for h in held}
        eligible = [c for c in candidates
                    if c.exchange_id not in frozen and c.exchange_id not in held_ids
                    and persisted.get(c.exchange_id, 0.0) >= persist_s]
        return decide_rotation(sellable, eligible, cash=cash, position_size=position_size, margin=margin,
                               max_swaps=1, cost_floor=cost_floor, min_held_value=min_held_value)
    from sig.engine.rotation import Swap
    if cash >= position_size:
        return []
    book, frozen = apply_virtual(held, swaps, now=now, freeze_s=freeze_s if freeze_s is not None else 86400.0)
    by_ex = {h.exchange_id: h for h in book}
    pool = sorted((h for h in book if abs(h.market_value) >= min_held_value), key=lambda h: h.remaining_gap)
    if not pool:
        return []
    held_any = {h.exchange_id for h in held}
    cands = []
    for c in candidates:
        if c.exchange_id in frozen or persisted.get(c.exchange_id, 0.0) < persist_s:
            continue
        if not (entry_threshold(c.sharp, cost_floor) <= c.gap < 0.25):
            continue
        h = by_ex.get(c.exchange_id)
        if h is None and c.exchange_id in held_any:
            continue                                   # held but not in the usable book (frozen / flipped): skip
        if h is not None and ((h.qty > 0) != (c.side == "yes")
                              or cap_for(c.gap, position_size, big_position_size, big_gap) - abs(h.market_value)
                              < min_room_usd):
            continue                                   # never against our holding; must have room under the cap
        cands.append(c)
    for c in sorted(cands, key=lambda c: -c.gap):
        h = pool[0]
        if h.exchange_id == c.exchange_id:
            continue
        adv = c.gap - h.remaining_gap - 2.0 * (c.half_spread + h.half_spread)
        if adv < margin:
            break
        return [Swap(sell=h, buy=c, advantage=round(adv, 4))]
    return []


def book_from_events(events: list[dict]) -> list[VirtualSwap]:
    """Rebuild the open virtual swaps from the event log (restart-safe)."""
    out = []
    for e in events:
        if e.get("event") == "swap":
            out.append(VirtualSwap(e["swap_id"], e["ts"], e["sell"]["exchange_id"], float(e["sell"]["qty"]),
                                   float(e["sell"]["exit_px"]), e["buy"]["exchange_id"], e["buy"]["side"],
                                   float(e["buy"]["entry_px"]), float(e["buy"]["new_shares"])))
    return out
