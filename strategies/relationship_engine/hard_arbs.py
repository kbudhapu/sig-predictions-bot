"""Hard arbs: same-race complementary baskets that are near-riskless by construction.

Exactly one party wins a race, so for an EXHAUSTIVE group the YES contracts settle to exactly 1.0 in
total, whatever happens:

    buy  every leg for  sum(ask) < 1 - fees  ->  pay sum(ask), receive 1.0
    sell every leg for  sum(bid) > 1 + fees  ->  receive sum(bid), pay out 1.0

"Exactly one party wins" is the entire basis of the trade, so everything in this module exists to
avoid asserting it when it is not true. Three independent guards, because this is the one place in
the bot where being wrong is not a bad estimate but a guaranteed loss:

G1  GROUP SIZE IS DISCOVERED, NOT ASSUMED (graph.py).
    A 3-way race scored as 2-way is the canonical phantom. Rhode Island Governor lists D, R AND an
    Independent. Buying only D+R for 0.85 does not buy 1.0 of settlement — if the Independent wins,
    both legs settle 0 and the "arb" is a 100% loss on a position entered as riskless. The basket
    here always spans EVERY listed member of the group.

G2  EVERY LEG MUST BE EXECUTABLE ON THE SIDE WE NEED.
    A buy-all basket needs a live ask on all N legs; a sell-all needs a live bid on all N. Many
    pre-open books are one-sided (recon §4), and a missing quote is not a zero — it means that leg
    cannot be filled, so the basket cannot be completed and the remainder is an outright directional
    bet. A partial basket is NEVER emitted.

G3  PRICE-SIDE MISSING-OUTCOME CHECK (independent of G1).
    G1 can only see outcomes SIG has listed. If a race is really 3-way but SIG lists just D and R,
    the group looks exhaustive and the basket looks cheap — because it genuinely is cheap, for the
    sound reason that neither listed party is certain to win. The signature is a large shortfall in
    the sum of MIDS (not of asks): a market-maker pricing error shows up as a thin edge around a
    ~1.0 mid-sum, whereas an unlisted outcome shows up as a mid-sum well below 1. Such a group is
    DOWNGRADED to a soft mispricing rather than dropped, so the information survives without being
    labelled riskless.
"""

from __future__ import annotations

from .contracts import (
    HARD_ARB,
    Leg,
    Opportunity,
    SOFT_MISPRICING,
    best_ask,
    best_bid,
    depth_for,
    index_prices,
    mid,
    round_to_tick,
)
from .graph import RaceGroup, RelationshipGraph

#: One tick. Below this an "edge" is rounding noise, not money.
DEFAULT_MIN_EDGE = 0.005
#: Total fee per 1.0 of basket. SIG exposes no fee anywhere in recon, so the default is 0.0 — but it
#: is a REQUIRED-TO-REVIEW parameter rather than a silent assumption: if fees appear, an unset fee
#: turns a losing basket into a reported arb.
DEFAULT_FEES = 0.0
#: Mid-sum shortfall beyond which G3 suspects an unlisted outcome. 0.08 is the uniform seeded spread,
#: so a correctly-quoted 2-way sits at mid-sum ~1.00; 0.15 is comfortably outside quote noise.
DEFAULT_MISSING_OUTCOME_GAP = 0.15
#: Recon §4: a single seeded level of 1000 contracts is the entire book.
DEFAULT_MAX_CONTRACTS = 1000


def _basket(
    group: RaceGroup,
    price_index: dict,
    *,
    action: str,
    fees: float,
    min_edge: float,
    max_contracts: int,
    depths,
) -> tuple[float, list[Leg], dict] | None:
    """Build a complete all-legs basket for one direction, or None if it cannot be completed."""
    quote_of = best_ask if action == "buy" else best_bid
    prices: list[float] = []
    legs: list[Leg] = []
    sizes: list[int] = []

    for member in group.members:
        row = price_index.get(member.exchange_id) or price_index.get(str(member.exchange_id))
        quote = quote_of(row)
        if quote is None:                                   # G2
            return None
        prices.append(quote)
        avail = depth_for(depths, member.exchange_id, action)
        sizes.append(max_contracts if avail is None else min(avail, max_contracts))

    gross = sum(prices)
    edge = (1.0 - fees - gross) if action == "buy" else (gross - 1.0 - fees)
    if edge < min_edge:
        return None

    quantity = min(sizes) if sizes else 0
    quantity = int(max(quantity, 0))
    if quantity <= 0:
        return None

    for member, quote in zip(group.members, prices):
        legs.append(
            Leg(
                exchange_id=member.exchange_id,
                side="yes",
                action=action,
                price=round_to_tick(quote, action=action),
                quantity=quantity,
            )
        )

    # Re-price the edge at the TICK-SNAPPED prices actually sent. round_to_tick snaps against us, so
    # this can only shrink the edge — and a basket that survives on raw quotes but not on sendable
    # prices must not be emitted.
    snapped = sum(leg.price for leg in legs)
    snapped_edge = (1.0 - fees - snapped) if action == "buy" else (snapped - 1.0 - fees)
    if snapped_edge < min_edge:
        return None

    meta = {
        "race": str(group.race),
        "group_size": group.size,
        "parties": sorted(group.parties),
        "direction": "buy_all" if action == "buy" else "sell_all",
        "gross_raw": round(gross, 6),
        "gross_at_tick": round(snapped, 6),
        "fees": fees,
        "per_contract_edge": round(snapped_edge, 6),
        "depth_known": depths is not None,
    }
    return snapped_edge, legs, meta


def find_hard_arbs(
    graph: RelationshipGraph,
    prices,
    *,
    fees: float = DEFAULT_FEES,
    min_edge: float = DEFAULT_MIN_EDGE,
    max_contracts: int = DEFAULT_MAX_CONTRACTS,
    missing_outcome_gap: float = DEFAULT_MISSING_OUTCOME_GAP,
    depths: dict | None = None,
) -> list[Opportunity]:
    """Near-riskless complementary baskets, plus any G3-downgraded soft candidates.

    Returns a list of Opportunity. `type` is "hard_arb" for a provably-closed basket and
    "soft_mispricing" for one that G3 flagged as possibly missing an outcome. Pure: reads dicts only.
    """
    price_index = index_prices(prices)
    out: list[Opportunity] = []

    for group in graph.arbitrable_groups:
        for action in ("buy", "sell"):
            built = _basket(
                group,
                price_index,
                action=action,
                fees=fees,
                min_edge=min_edge,
                max_contracts=max_contracts,
                depths=depths,
            )
            if built is None:
                continue
            edge, legs, meta = built

            # G3 — only a BUY basket can be faked by an unlisted outcome. An unlisted outcome makes
            # the listed legs CHEAP (mid-sum < 1), which cannot create a sell-all overpricing.
            suspect = False
            if action == "buy":
                mids = [
                    mid(price_index.get(m.exchange_id) or price_index.get(str(m.exchange_id)))
                    for m in group.members
                ]
                if all(x is not None for x in mids):
                    mid_sum = sum(mids)          # type: ignore[arg-type]
                    meta["mid_sum"] = round(mid_sum, 6)
                    suspect = mid_sum < (1.0 - missing_outcome_gap)

            if suspect:
                meta["downgraded"] = (
                    f"mid-sum {meta['mid_sum']} is more than {missing_outcome_gap} below 1.0 — "
                    f"consistent with an outcome SIG has not listed, so this is NOT riskless"
                )
                out.append(
                    Opportunity(
                        legs=legs,
                        type=SOFT_MISPRICING,
                        edge_amount=edge * legs[0].quantity,
                        confidence=0.25,
                        meta=meta,
                    )
                )
            else:
                out.append(
                    Opportunity(
                        legs=legs,
                        type=HARD_ARB,
                        # Total, not per-contract: the executor sizes in contracts.
                        edge_amount=edge * legs[0].quantity,
                        # An identity rather than an estimate. Execution risk (partial fill, a quote
                        # vanishing between read and send) belongs to the executor's atomic-or-abort,
                        # not to the confidence of the relationship.
                        confidence=1.0,
                        meta=meta,
                    )
                )

    out.sort(key=lambda o: o.edge_amount, reverse=True)
    return out
