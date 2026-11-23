"""Market-impact adjustment (§2.1b): your own fill moves the price, so a big bet destroys its own edge.

Recon measured SIG's books as a SINGLE seeded level of ~1000 contracts at a uniform 0.08 spread — depth
is a wall, not a curve. On a book like that, "size at the top-of-book edge" is not a small error: the
whole position fills at or worse than the quoted price, and anything past level 1 has no price at all.

So sizing is done against the AVERAGE fill price through the depth actually consumed, never the best
quote, and never beyond the quantity at which the impact-adjusted edge dies. Benter and Woods — the two
people who demonstrably won at scale — both sized with explicit own-impact limits; this is that.
"""

from __future__ import annotations

from dataclasses import dataclass

from .kelly import YES, kelly_fraction_star

TICK = 0.005


@dataclass(frozen=True, slots=True)
class Fill:
    """What `quantity` contracts would actually cost, walking the book."""

    quantity: int           # contracts actually available/consumed
    avg_price: float        # volume-weighted average fill price
    worst_price: float      # the deepest level touched -> the limit price that lets it all fill
    levels_used: int


def _levels(book: dict | None, side: str, action: str = "buy") -> list[tuple[float, int]]:
    """The side of the book we would hit, as [(price, qty)] in the order consumed.

    Buying YES hits asks. Buying NO is, on SIG, a separate YES-exchange order on the complementary
    market, so the caller passes that market's book — from this function's point of view it is still
    'hit the asks'. Selling hits bids, best (highest) first.
    """
    if not book:
        return []
    key = "asks" if action == "buy" else "bids"
    raw = book.get(key) or []
    out: list[tuple[float, int]] = []
    for lvl in raw:
        if isinstance(lvl, dict):
            px, qty = lvl.get("price"), lvl.get("quantity")
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            px, qty = lvl[0], lvl[1]
        else:
            continue
        try:
            px_f, qty_i = float(px), int(float(qty))
        except (TypeError, ValueError):
            continue
        if qty_i > 0 and 0.0 < px_f < 1.0:
            out.append((px_f, qty_i))
    out.sort(key=lambda t: t[0], reverse=(key == "bids"))
    return out


def fill_through(book: dict | None, quantity: int, *, action: str = "buy", side: str = YES) -> Fill | None:
    """Walk the book for `quantity` contracts. None if the book is empty.

    If the book cannot supply the full quantity, the Fill reports what IS available — the caller then
    sizes to that, rather than submitting an order that would half-fill (§5: an unfillable "optimal"
    size is reduced to the fillable size).
    """
    levels = _levels(book, side, action)
    if not levels:
        return None
    want = max(int(quantity), 0)
    if want == 0:
        return Fill(0, levels[0][0], levels[0][0], 0)
    taken = 0
    cost = 0.0
    worst = levels[0][0]
    used = 0
    for px, qty in levels:
        if taken >= want:
            break
        take = min(qty, want - taken)
        taken += take
        cost += take * px
        worst = px
        used += 1
    if taken == 0:
        return None
    return Fill(taken, cost / taken, worst, used)


def impact_adjusted_f(
    book: dict | None, quantity: int, p: float, side: str, *, action: str = "buy"
) -> tuple[float, Fill | None]:
    """f* recomputed at the AVERAGE fill price for `quantity` — the edge that actually survives."""
    fill = fill_through(book, quantity, action=action, side=side)
    if fill is None or fill.quantity == 0:
        return 0.0, fill
    # avg_price is the realised cost per contract on the side being bought, i.e. the effective entry.
    # Feed it back as p_m on the YES axis so kelly_fraction_star's algebra is unchanged.
    effective_pm = fill.avg_price if side == YES else 1.0 - fill.avg_price
    return kelly_fraction_star(p, effective_pm, side), fill


def max_fillable_with_edge(
    book: dict | None,
    p: float,
    side: str,
    *,
    min_f: float,
    action: str = "buy",
    hard_cap: int | None = None,
) -> int:
    """Largest integer quantity whose IMPACT-ADJUSTED f* still exceeds `min_f`.

    Monotonicity makes this a clean binary search: walking deeper can only raise the average buy price,
    which can only lower f*. So f*(q) is non-increasing in q and the feasible set is a prefix.

    Returns 0 when even one contract fails the test — i.e. the top of book already has no edge, so
    there is nothing to size.
    """
    levels = _levels(book, side, action)
    if not levels:
        return 0
    available = sum(q for _, q in levels)
    hi = min(available, hard_cap) if hard_cap is not None else available
    if hi <= 0:
        return 0
    f_one, _ = impact_adjusted_f(book, 1, p, side, action=action)
    if f_one <= min_f:
        return 0
    f_hi, _ = impact_adjusted_f(book, hi, p, side, action=action)
    if f_hi > min_f:
        return hi
    lo = 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        f_mid, _ = impact_adjusted_f(book, mid, p, side, action=action)
        if f_mid > min_f:
            lo = mid
        else:
            hi = mid - 1
    return lo


def round_limit_price(price: float, action: str) -> float:
    """Snap the limit to the 0.005 tick, AGAINST us (buy up, sell down), so tick rounding can only
    make the order more likely to fill and the computed edge more conservative — never the reverse."""
    steps = round(float(price) / TICK, 6)
    snapped = (-(-steps // 1) if action == "buy" else (steps // 1)) * TICK
    return round(min(max(snapped, TICK), 1.0 - TICK), 4)
