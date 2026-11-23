"""Soft mispricings: chamber-control price vs the price implied by its component races.

SOFT means STATISTICAL, not riskless. A hard arb is an identity — exactly one party wins a race. This
is a model: "given the component races are priced at p_i, control should be worth P(seats >= needed)".
If the model is wrong the trade is wrong, so these are emitted with sub-1.0 confidence and are never
labelled hard_arb.

THE AGGREGATION, and why correlation is not optional
----------------------------------------------------
Seats won S = seats_held_not_up + sum of Bernoulli(p_i) over the contested races. Treating the races
as INDEPENDENT would understate Var(S) badly, because midterm races share a national swing: they move
together, and the tails that decide chamber control are exactly where that co-movement matters. We use
the standard single-factor form, with one shared correlation rho:

    E[S]   = held + sum(p_i)
    Var(S) = (1 - rho) * sum(sigma_i^2)  +  rho * (sum(sigma_i))^2        sigma_i^2 = p_i(1 - p_i)

rho = 0 collapses to independence; rho = 1 makes every race one coin flip. P(S >= needed) then comes
from a normal approximation with a continuity correction.

WHY THIS NEEDS CALLER-SUPPLIED SEAT ARITHMETIC
----------------------------------------------
Only ~36 of 100 Senate seats appear in this tournament, so the contested races alone cannot say who
controls the chamber — the seats NOT up for election dominate the count. `seats_held_not_up` and
`seats_needed` are therefore REQUIRED per chamber and have no defaults. A control market with no
supplied model is SKIPPED and reported, never scored against invented numbers: a fabricated baseline
would produce a confident, completely meaningless divergence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    Leg,
    Opportunity,
    SOFT_MISPRICING,
    best_ask,
    best_bid,
    index_prices,
    mid,
    round_to_tick,
)
from .graph import RelationshipGraph

#: Shared national-swing correlation across component races. Literature-ish placeholder; the sizing
#: spec's n_eff / correlated-cluster machinery is the proper home for a fitted value. [TUNE-LIVE]
DEFAULT_RHO = 0.35
#: Minimum |model - market| worth acting on. Wider than a hard-arb threshold because the model itself
#: carries error; a 2-cent "divergence" is inside the noise of rho and the seat baseline.
DEFAULT_MIN_DIVERGENCE = 0.05
#: Divergence at which confidence saturates.
DEFAULT_FULL_CONFIDENCE_DIVERGENCE = 0.25
#: Ceiling on soft confidence. Capped well under 1.0 so a soft signal can never be sized like an
#: identity, whatever the divergence.
MAX_SOFT_CONFIDENCE = 0.6
DEFAULT_MAX_CONTRACTS = 1000


@dataclass(frozen=True, slots=True)
class ChamberModel:
    """Seat arithmetic for one chamber. Supplied by the caller; no defaults on purpose."""

    seats_needed: int
    seats_held_not_up: int = 0


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def implied_control_probability(
    component_probs: list[float],
    model: ChamberModel,
    *,
    rho: float = DEFAULT_RHO,
) -> float | None:
    """P(seats >= seats_needed) under the single-factor correlated model. None if unusable."""
    ps = [p for p in component_probs if p is not None and 0.0 <= p <= 1.0]
    if not ps:
        return None
    mean = model.seats_held_not_up + sum(ps)
    sigmas = [math.sqrt(max(p * (1.0 - p), 0.0)) for p in ps]
    r = min(max(float(rho), 0.0), 1.0)
    var = (1.0 - r) * sum(s * s for s in sigmas) + r * (sum(sigmas) ** 2)
    threshold = model.seats_needed - 0.5                       # continuity correction
    if var <= 1e-12:
        return 1.0 if mean >= threshold else 0.0
    return 1.0 - _normal_cdf((threshold - mean) / math.sqrt(var))


def find_soft_mispricings(
    graph: RelationshipGraph,
    prices,
    *,
    chamber_models: dict[str, ChamberModel] | None = None,
    rho: float = DEFAULT_RHO,
    min_divergence: float = DEFAULT_MIN_DIVERGENCE,
    full_confidence_divergence: float = DEFAULT_FULL_CONFIDENCE_DIVERGENCE,
    max_contracts: int = DEFAULT_MAX_CONTRACTS,
) -> list[Opportunity]:
    """Control-market price vs component-implied probability.

    `chamber_models` maps chamber name ("Senate"/"House") -> ChamberModel. Control markets without a
    model are skipped (see `skipped` in each returned meta for the reason). Pure: dicts only.
    """
    price_index = index_prices(prices)
    models = chamber_models or {}
    out: list[Opportunity] = []

    for rel in graph.control_relations:
        model = models.get(rel.chamber)
        ctrl_row = price_index.get(rel.control_market.exchange_id) or price_index.get(
            str(rel.control_market.exchange_id)
        )
        market_mid = mid(ctrl_row)
        if market_mid is None or not rel.components:
            continue
        if model is None:
            # Reported, not silently dropped — a missing seat model is a config gap worth seeing.
            out.append(
                Opportunity(
                    legs=[],
                    type=SOFT_MISPRICING,
                    edge_amount=0.0,
                    confidence=0.0,
                    meta={
                        "chamber": rel.chamber,
                        "party": rel.party,
                        "skipped": (
                            f"no ChamberModel for {rel.chamber}: seats_needed / seats_held_not_up are "
                            f"required because only {len(rel.components)} seats of this chamber are "
                            f"contested in this tournament"
                        ),
                    },
                )
            )
            continue

        comp_probs: list[float] = []
        for comp in rel.components:
            row = price_index.get(comp.exchange_id) or price_index.get(str(comp.exchange_id))
            p = mid(row)
            if p is not None:
                comp_probs.append(p)
        implied = implied_control_probability(comp_probs, model, rho=rho)
        if implied is None:
            continue

        divergence = implied - market_mid
        if abs(divergence) < min_divergence:
            continue

        # Model says control is worth MORE than the market -> buy control, and vice versa.
        action = "buy" if divergence > 0 else "sell"
        quote = best_ask(ctrl_row) if action == "buy" else best_bid(ctrl_row)
        if quote is None:
            continue    # cannot execute the side the model wants

        # Re-measure the edge against the EXECUTABLE price, not the mid: buying at the ask gives up
        # half the spread, and on an 0.08 spread that is most of a small divergence.
        executable_edge = (implied - quote) if action == "buy" else (quote - implied)
        if executable_edge < min_divergence:
            continue

        confidence = min(
            MAX_SOFT_CONFIDENCE,
            MAX_SOFT_CONFIDENCE * abs(divergence) / max(full_confidence_divergence, 1e-9),
        )
        quantity = int(max_contracts)
        leg = Leg(
            exchange_id=rel.control_market.exchange_id,
            side="yes",
            action=action,
            price=round_to_tick(quote, action=action),
            quantity=quantity,
        )
        out.append(
            Opportunity(
                legs=[leg],
                type=SOFT_MISPRICING,
                edge_amount=executable_edge * quantity,
                confidence=confidence,
                meta={
                    "chamber": rel.chamber,
                    "party": rel.party,
                    "n_components": len(comp_probs),
                    "implied_from_components": round(implied, 6),
                    "market_mid": round(market_mid, 6),
                    "divergence_vs_mid": round(divergence, 6),
                    "executable_price": leg.price,
                    "per_contract_edge": round(executable_edge, 6),
                    "rho": rho,
                    "seats_needed": model.seats_needed,
                    "seats_held_not_up": model.seats_held_not_up,
                    "basis": "STATISTICAL — correlated-component model, not an identity",
                },
            )
        )

    out.sort(key=lambda o: abs(o.edge_amount), reverse=True)
    return out
