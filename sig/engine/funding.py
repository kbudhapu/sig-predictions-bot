"""FUNDING PLANNER — free a fixed amount of cash from the book at the least expected-value cost (2026-10-08).

Pure (no I/O). Used by tools/fund_pilot.py (dry run by default) to fund an isolated strategy's carve-out.

Per held position the HELD leg is sold into resting bids (YES held: YES bids; NO held: NO bid = 1 - YES ask, the
same convention as tools/trim_positions.py). Every share sold has two costs:
  ev_cost   = leg_fair - price   expected value given up vs the benchmark (Polymarket) — can be negative (a gain)
  mark_cost = leg_mid  - price   immediate leaderboard hit vs the SIG mid mark (>= half a spread at the bid)
Levels are taken cheapest ev_cost-per-dollar-freed first until `need_usd` is reached (the last level partially).

HARD LIMITS (never relaxed): price >= leg_fair - max_ev_cost; price >= leg_mid - max_mark_cost; at most the held
quantity; at most the level's displayed size; total proceeds capped at need_usd (+ < 1 share); no market without
a fresh fair; excluded markets (e.g. owned by an isolated strategy) untouched.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FundingLimits:
    max_ev_cost: float = 0.05        # never give up more than 5c/share of expected value
    max_mark_cost: float = 0.005     # never sell more than one tick below the SIG mid
    min_qty: int = 1


def held_levels(qty: float, book: dict) -> list[tuple[float, float]]:
    """Sellable levels (price per share of the HELD leg, size), best first. book: {'bids': [(px, q)], 'asks': [...]}
    in YES terms."""
    if qty > 0:
        return sorted(((float(p), float(q)) for p, q in book.get("bids") or []), key=lambda x: -x[0])
    return sorted(((round(1.0 - float(p), 3), float(q)) for p, q in book.get("asks") or []), key=lambda x: -x[0])


def plan_funding(positions: dict, books: dict, fairs: dict, need_usd: float,
                 limits: FundingLimits = FundingLimits(), exclude: set | None = None) -> dict:
    """positions: ex -> signed YES qty; books: ex -> {'bids','asks'} (YES); fairs: ex -> YES fair.
    Returns {'orders': [{ex, outcome, qty, limit, proceeds, ev_cost, mark_cost}], 'proceeds', 'ev_cost',
    'mark_cost', 'short_usd'}. Orders are per market: exact qty, limit = the worst level used."""
    exclude = {str(x) for x in (exclude or set())}
    cands = []
    for ex, q in positions.items():
        ex = str(ex)
        if abs(q) < 1 or ex in exclude or fairs.get(ex) is None or ex not in books:
            continue
        b = books[ex]
        bids = [float(p) for p, _ in b.get("bids") or []]
        asks = [float(p) for p, _ in b.get("asks") or []]
        if not bids or not asks:
            continue                                        # one-sided book: no reliable mid -> skip
        mid = (max(bids) + min(asks)) / 2.0
        held_yes = q > 0
        leg_fair = fairs[ex] if held_yes else 1.0 - fairs[ex]
        leg_mid = mid if held_yes else 1.0 - mid
        left = abs(q)
        for px, size in held_levels(q, b):
            if left < 1:
                break
            if px < leg_fair - limits.max_ev_cost - 1e-9 or px < leg_mid - limits.max_mark_cost - 1e-9 or px <= 0:
                break                                       # levels only get worse from here
            n = min(left, size)
            left -= n
            cands.append({"ex": ex, "outcome": "yes" if held_yes else "no", "px": px, "n": n,
                          "ev": leg_fair - px, "mark": leg_mid - px})
    cands.sort(key=lambda c: (c["ev"] / c["px"], -c["px"]))
    acc, picked = 0.0, {}
    for c in cands:
        if acc >= need_usd - 1e-9:
            break
        n = min(c["n"], math.ceil((need_usd - acc) / c["px"]))
        n = int(n)
        if n < limits.min_qty:
            continue
        o = picked.setdefault((c["ex"], c["outcome"]), {"ex": c["ex"], "outcome": c["outcome"], "qty": 0,
                                                       "limit": 1.0, "proceeds": 0.0, "ev_cost": 0.0,
                                                       "mark_cost": 0.0})
        o["qty"] += n
        o["limit"] = min(o["limit"], c["px"])
        o["proceeds"] += n * c["px"]
        o["ev_cost"] += n * c["ev"]
        o["mark_cost"] += n * c["mark"]
        acc += n * c["px"]
    orders = sorted(picked.values(), key=lambda o: -o["proceeds"])
    for o in orders:
        o["limit"] = round(o["limit"], 3)
    return {"orders": orders, "proceeds": sum(o["proceeds"] for o in orders),
            "ev_cost": sum(o["ev_cost"] for o in orders), "mark_cost": sum(o["mark_cost"] for o in orders),
            "short_usd": max(need_usd - acc, 0.0)}


def recheck_order(order: dict, position_qty: float, book: dict, fair: float | None,
                  limits: FundingLimits = FundingLimits()) -> tuple[int, str]:
    """Right before placing: how many shares may still be sold at >= order['limit'] under the SAME limits, given
    the live position, book and fair. Returns (qty, reason); qty 0 means skip."""
    if fair is None:
        return 0, "no_fresh_fair"
    held_yes = order["outcome"] == "yes"
    if (position_qty > 0) != held_yes or abs(position_qty) < 1:
        return 0, "position_gone_or_flipped"
    bids = [float(p) for p, _ in book.get("bids") or []]
    asks = [float(p) for p, _ in book.get("asks") or []]
    if not bids or not asks:
        return 0, "one_sided_book"
    mid = (max(bids) + min(asks)) / 2.0
    leg_fair = fair if held_yes else 1.0 - fair
    leg_mid = mid if held_yes else 1.0 - mid
    lim = order["limit"]
    if lim < leg_fair - limits.max_ev_cost - 1e-9 or lim < leg_mid - limits.max_mark_cost - 1e-9:
        return 0, "limit_now_breaks_cost_bounds"
    depth = sum(q for p, q in held_levels(position_qty, book) if p >= lim - 1e-9)
    n = int(min(order["qty"], abs(position_qty), depth))
    return (n, "ok") if n >= limits.min_qty else (0, "no_depth_at_limit")


async def execute_plan(plan: dict, *, broker, read_book, read_position, read_fair, need_usd: float,
                       limits: FundingLimits = FundingLimits(), wait_s: float = 60.0, poll_s: float = 5.0,
                       sleep=None, strategy_id: str = "fund_pilot", nonce=None, min_interval_s: float = 10.0) -> dict:
    """Execute a funding plan with every guard re-applied per order IMMEDIATELY before placing.

    For each order (in plan order) until proceeds >= need_usd:
      recheck live position / book / fair (recheck_order) -> skip if anything moved past the limits;
      qty = min(rechecked, shares still needed at the limit) -> one SELL LIMIT of the held leg at the plan limit
      (never lower), unique idempotency scope; poll up to wait_s; cancel any residual; NEVER re-place or chase.
    FILLS are measured from the POSITION change (SIG's GET /orders/{id} carries no traded quantity), bounded to
    [0, qty]. An ambiguous placement with no order id, or a cancel the venue does not confirm, STOPS the whole run
    (a possibly-resting order must never be followed by more selling). Returns a report."""
    import asyncio
    import uuid
    sleep = sleep or asyncio.sleep
    nonce = nonce or (lambda: uuid.uuid4().hex[:12])
    rep = {"proceeds": 0.0, "sold": [], "skipped": [], "stopped": None}
    for o in plan.get("orders", []):
        if rep["proceeds"] >= need_usd - 1e-9:
            break
        pos_before = await read_position(o["ex"])
        n, why = recheck_order(o, pos_before, await read_book(o["ex"]), await read_fair(o["ex"]), limits)
        n = min(n, math.ceil((need_usd - rep["proceeds"]) / o["limit"]))
        if n < limits.min_qty:
            rep["skipped"].append((o["ex"], why if why != "ok" else "target_met"))
            continue
        if rep.get("_placed_any"):
            await sleep(min_interval_s)                     # pace: leave the shared 30/min write budget to the bot
        rep["_placed_any"] = True
        r = await broker.place_order(strategy_id=strategy_id, market_id=f"{o['ex']}#{nonce()}", exchange_id=o["ex"],
                                     side="sell", action="close", quantity=int(n), price=o["limit"],
                                     outcome=o["outcome"], expiration_s=wait_s + 60.0)
        st = str(getattr(r, "status", "")).lower()
        oid = getattr(r, "order_id", None)
        if st == "rejected":
            rep["skipped"].append((o["ex"], "rejected"))
            continue
        if not oid:
            # did it land? look it up (a read) and cancel it, so nothing we cannot see keeps resting
            finder = getattr(broker, "_find_matching_order", None)
            found = None
            if callable(finder):
                try:
                    found = await finder(exchange_id=o["ex"], action="sell", quantity=int(n), price=o["limit"],
                                         side=o["outcome"])
                except Exception:  # noqa: BLE001
                    found = None
            if found and found.get("open", True) and found.get("id"):
                await broker.cancel_order(str(found["id"]), strategy_id=strategy_id)
            rep["stopped"] = f"ambiguous_placement_no_id:{o['ex']}:{'cancelled' if found else 'not_found'}"
            break
        waited = 0.0
        while st not in ("filled", "canceled", "cancelled", "rejected", "expired") and waited < wait_s:
            await sleep(poll_s)
            waited += poll_s
            g = await broker.get_order(oid)
            gs = str(getattr(g, "status", "")).lower()
            st = st if gs == "unknown" else gs              # a failed read never ends the wait
        unconfirmed = False
        if st not in ("filled", "canceled", "cancelled", "rejected", "expired"):
            c = await broker.cancel_order(oid, strategy_id=strategy_id)
            unconfirmed = str(getattr(c, "status", "")).lower() not in ("canceled", "cancelled")
        filled = max(0.0, min(float(n), abs(pos_before) - abs(await read_position(o["ex"]))))
        if filled > 0:
            px = float(o["limit"])                          # every fill is at >= the limit: conservative proceeds
            rep["proceeds"] += filled * px
            rep["sold"].append({"ex": o["ex"], "outcome": o["outcome"], "qty": filled, "limit": o["limit"]})
        if unconfirmed:
            rep["stopped"] = f"cancel_unconfirmed:{o['ex']}:{oid}"
            break
    rep.pop("_placed_any", None)
    return rep
