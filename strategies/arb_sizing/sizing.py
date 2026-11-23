"""Arb basket sizing: FLOOR from cost efficiency, CEILING from exit liquidity, CAP from the budget.

    quantity = min( entry-fillable , per-arb cap , remaining total budget , exit-depth ceiling )
    then REJECT if that quantity is too small to clear the cost floor.

Each of the four is a different kind of limit and the minimum is the only safe combination:

  * entry-fillable     — you cannot buy what is not offered (comes in from the detector)
  * per-arb cap        — `arb_max_per_arb_frac`: the knob that decides HOW MANY fit
  * remaining budget   — `arb_total_max_frac x bankroll - already deployed in arb`
  * exit-depth ceiling — `arb_max_frac_of_exit_depth x bid depth`, per leg

Direction of every bound is DOWNWARD, and the rejection at the end is a rejection rather than a
clamp-upward, because the floor is a statement about whether the trade is worth doing at all.

ORDER OF OPERATIONS matters: the cost floor is checked AFTER the ceilings, against the quantity we
would actually trade. Checking it against the requested quantity would approve a basket on the
economics of a size the book cannot support.

Liquidity logic is REUSED from strategies/exit (`evaluate_leg_exit`) and, beneath it, the sizing
module's `fill_through` book-walker — not reimplemented. The exit path is what will have to sell
this position, so sizing it against a second, slightly different notion of depth is how you get a
position that passes entry sizing and then cannot be recycled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategies.exit import evaluate_leg_exit
from strategies.sizing.impact import fill_through

from .config import ArbSizingConfig

# Reasons (machine-readable; the operator reads these)
R_SIZED = "sized"
R_NO_LEGS = "no_legs"
R_NO_EDGE = "no_edge"
R_DEGENERATE_COST = "degenerate_basket_cost"
R_NO_ENTRY_DEPTH = "no_entry_depth"
R_BUDGET_EXHAUSTED = "arb_budget_exhausted"
R_NO_EXIT_DEPTH = "no_exit_depth"
R_BELOW_MIN_NOTIONAL = "below_min_notional"
R_EDGE_TO_COST_TOO_LOW = "edge_to_cost_too_low"

# Which constraint bound the size (diagnostic; this is what you tune against)
B_ENTRY = "entry_fillable"
B_PER_ARB = "per_arb_cap"
B_BUDGET = "total_arb_budget"
B_EXIT = "exit_depth"


@dataclass(frozen=True)
class ArbSize:
    """The sized basket, plus every number that produced it."""

    quantity: int                  # contracts PER LEG (a complementary basket is 1:1 across legs)
    reason: str
    binding: str                   # which ceiling bound the size, "" when rejected
    per_unit_cost: float           # cash out per basket unit
    basket_notional: float
    captured_edge: float
    est_round_trip_cost: float
    legs: list[dict] = field(default_factory=list)   # the legs at the final quantity
    meta: dict = field(default_factory=dict)

    @property
    def sized(self) -> bool:
        return self.quantity > 0 and self.reason == R_SIZED

    def to_dict(self) -> dict:
        return {
            "quantity": self.quantity, "reason": self.reason, "binding": self.binding,
            "per_unit_cost": self.per_unit_cost, "basket_notional": self.basket_notional,
            "captured_edge": self.captured_edge,
            "est_round_trip_cost": self.est_round_trip_cost, "meta": dict(self.meta),
        }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _leg_price(leg: dict) -> float:
    try:
        return float(leg.get("price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def basket_unit_cost(legs: list[dict]) -> float:
    """Cash required per basket unit.

    For the buy-all direction this is the sum of the leg prices (a complementary basket costs the
    sum of its parts and pays exactly 1.0 at settlement). A SELL leg is counted at its price too,
    which is a deliberate OVER-statement of the capital it ties up: shorting is collateralised
    rather than paid for, but SIG lists only YES exchanges, so a sell-all basket is not executable
    here anyway. Over-stating cost can only make the arb smaller, never larger.
    """
    return sum(abs(_leg_price(l)) for l in legs)


def exit_depth_ceiling(legs: list[dict], *, exit_books: dict | None,
                       cfg: ArbSizingConfig) -> tuple[int, dict]:
    """Largest per-leg quantity whose EXIT the bids could absorb, honouring the safety fraction.

    Depth is read with the exit path's own walker so entry and exit agree about what "absorbable"
    means. Unknown depth yields 0 -- fail CLOSED: an un-exitable arb is a hold-to-settlement
    position wearing a velocity trade's clothes, which is exactly the trap being fixed.
    """
    trace: dict[str, Any] = {}
    if not exit_books:
        return 0, {"exit_depth": "no_books"}
    ceiling: int | None = None
    for leg in legs:
        eid = str(leg.get("exchange_id") or "")
        book = exit_books.get(eid)
        if not book:
            return 0, {"exit_depth": f"no_book:{eid}"}
        # A deliberately huge ask tells fill_through to report what IS available on the bids.
        probe = fill_through(book, 10**9, action="sell", side=str(leg.get("side") or "yes"))
        depth = int(probe.quantity) if probe is not None else 0
        allowed = int(depth * cfg.arb_max_frac_of_exit_depth)
        trace[f"bid_depth:{eid}"] = depth
        trace[f"allowed:{eid}"] = allowed
        ceiling = allowed if ceiling is None else min(ceiling, allowed)
    return max(int(ceiling or 0), 0), trace


def estimate_round_trip_cost(legs: list[dict], quantity: int, *, exit_books: dict | None,
                             prices: dict | None, cfg: ArbSizingConfig) -> tuple[float, dict]:
    """Fees (both sides) + the slippage of selling this quantity back.

    Entry slippage is already inside the detector's quoted leg prices (it quotes the level it would
    hit), so double-counting it here would understate every arb. What is NOT yet counted anywhere is
    the EXIT: the bids walked on the way out. That is estimated with the exit module's own
    `evaluate_leg_exit`, so the number the floor is checked against is the number the exit path will
    actually produce.
    """
    notional = quantity * basket_unit_cost(legs)
    fees = cfg.arb_fees * notional * 2.0          # entry + exit
    slippage = 0.0
    trace: dict[str, Any] = {"fees": round(fees, 6)}
    for leg in legs:
        eid = str(leg.get("exchange_id") or "")
        book = (exit_books or {}).get(eid)
        row = (prices or {}).get(eid)
        probe = dict(leg)
        probe["quantity"] = quantity
        le = evaluate_leg_exit(probe, price_row=row, book=book)
        if le is None:
            continue
        slippage += max(float(le.slippage), 0.0)
        trace[f"exit_slip:{eid}"] = round(float(le.slippage), 6)
    trace["slippage"] = round(slippage, 6)
    return fees + slippage, trace


def size_arb_basket(
    legs: list[dict],
    *,
    edge_per_contract: float,
    bankroll: float,
    arb_deployed: float = 0.0,
    exit_books: dict | None = None,
    prices: dict | None = None,
    cfg: ArbSizingConfig,
) -> ArbSize:
    """Size ONE arb basket. Pure: dicts in, ArbSize out.

    `legs` carry the detector's capture-fillable quantity and quoted price; `edge_per_contract` is
    the basket's edge per unit (e.g. 1.00 - gross for a buy-all basket, net of the detector's fee
    assumption). `arb_deployed` is the notional already open in arb, which is what makes the total
    budget a real constraint rather than a per-order one.
    """
    legs = [dict(l) for l in (legs or [])]
    if not legs:
        return ArbSize(0, R_NO_LEGS, "", 0.0, 0.0, 0.0, 0.0)
    edge = float(edge_per_contract or 0.0)
    if edge <= 0.0:
        return ArbSize(0, R_NO_EDGE, "", 0.0, 0.0, 0.0, 0.0)

    per_unit = basket_unit_cost(legs)
    if per_unit <= 0.0:
        return ArbSize(0, R_DEGENERATE_COST, "", per_unit, 0.0, 0.0, 0.0)

    # --- the four ceilings -------------------------------------------------- #
    q_entry = min((_as_int(l.get("quantity")) for l in legs), default=0)
    if q_entry <= 0:
        return ArbSize(0, R_NO_ENTRY_DEPTH, "", per_unit, 0.0, 0.0, 0.0)

    bank = max(float(bankroll), 0.0)
    q_per_arb = int((cfg.arb_max_per_arb_frac * bank) / per_unit)

    budget_room = cfg.arb_total_max_frac * bank - max(float(arb_deployed), 0.0)
    q_budget = int(max(budget_room, 0.0) / per_unit)

    q_exit, exit_trace = exit_depth_ceiling(legs, exit_books=exit_books, cfg=cfg)

    candidates = {B_ENTRY: q_entry, B_PER_ARB: q_per_arb, B_BUDGET: q_budget, B_EXIT: q_exit}
    binding = min(candidates, key=lambda k: candidates[k])
    quantity = max(candidates[binding], 0)

    meta: dict[str, Any] = {
        "candidates": dict(candidates), "per_unit_cost": round(per_unit, 6),
        "edge_per_contract": round(edge, 6), "bankroll": bank,
        "arb_deployed": round(float(arb_deployed), 4),
        "budget_room": round(budget_room, 4),
        "max_concurrent_at_full_size": cfg.max_concurrent_at_full_size(),
        **exit_trace,
    }

    if quantity <= 0:
        reason = {B_BUDGET: R_BUDGET_EXHAUSTED, B_EXIT: R_NO_EXIT_DEPTH,
                  B_ENTRY: R_NO_ENTRY_DEPTH}.get(binding, R_BELOW_MIN_NOTIONAL)
        return ArbSize(0, reason, "", per_unit, 0.0, 0.0, 0.0, meta=meta)

    # --- the cost-efficiency floor, checked at the size we would ACTUALLY trade --- #
    notional = quantity * per_unit
    captured = quantity * edge
    cost, cost_trace = estimate_round_trip_cost(
        legs, quantity, exit_books=exit_books, prices=prices, cfg=cfg)
    meta.update(cost_trace)
    meta["basket_notional"] = round(notional, 4)
    meta["captured_edge"] = round(captured, 6)

    if notional < cfg.arb_min_notional:
        return ArbSize(0, R_BELOW_MIN_NOTIONAL, "", per_unit, notional, captured, cost, meta=meta)
    if cost > 0.0 and captured < cfg.arb_min_edge_to_cost_ratio * cost:
        return ArbSize(0, R_EDGE_TO_COST_TOO_LOW, "", per_unit, notional, captured, cost, meta=meta)

    final_legs = []
    for l in legs:
        d = dict(l)
        d["quantity"] = int(quantity)
        final_legs.append(d)
    return ArbSize(int(quantity), R_SIZED, binding, per_unit, notional, captured, cost,
                   legs=final_legs, meta=meta)
