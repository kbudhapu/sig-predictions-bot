"""Relationship engine — self-computed market relationships for the SIG Predictions Cup.

Pure logic over already-fetched dicts: no network, no broker, no I/O, no blocking call anywhere, so
it is safe to await-free call from inside the asyncio fast loop.

WHY THIS MODULE EXISTS
SIG publishes 0 multi-outcome and 0 composite markets and an EMPTY /relationships surface
(/relationships = {data:[]}, /relationships/graph = 400, /relationships/constraints = 0 pre-open).
Every relationship is implicit in the titles, so `strategies/arb_detector.py` — which consumes
SIG's published constraints — has nothing to consume unless SIG starts publishing. This engine is the
other half: it derives the relationships ourselves and works whether or not SIG ever publishes.

    graph = build_relationship_graph(markets)                 # markets: /markets data[]
    hard  = find_hard_arbs(graph, prices)                      # prices: /exchanges/prices
    soft  = find_soft_mispricings(graph, prices, chamber_models={...})
    report = reconcile_with_published(hard, sig_constraints)    # no-op while SIG publishes nothing

Each opportunity renders to the executor contract via `.to_dict()`:
    {legs:[{exchange_id, side, action, price, quantity}], type, edge_amount, confidence}

THE ONE THING TO KNOW: group size is DISCOVERED from the data, never assumed to be 2. Three races in
the live universe are 3-way (Rhode Island Governor, Montana Senate, Nebraska Senate each list an
Independent). Scoring one of those as a D/R pair produces a phantom arb that is a guaranteed loss, not
a bad estimate — see hard_arbs.py for the three guards that prevent it.
"""

from __future__ import annotations

from .contracts import (
    HARD_ARB,
    SOFT_MISPRICING,
    TICK,
    Leg,
    Opportunity,
    index_prices,
)
from .graph import (
    ControlRelation,
    RaceGroup,
    RelationshipGraph,
    build_relationship_graph,
)
from .hard_arbs import (
    DEFAULT_FEES,
    DEFAULT_MAX_CONTRACTS,
    DEFAULT_MIN_EDGE,
    DEFAULT_MISSING_OUTCOME_GAP,
    find_hard_arbs,
)
from .parsing import (
    MAJOR_PARTIES,
    ParsedMarket,
    RaceKey,
    parse_markets,
    parse_title,
)
from .published import reconcile_with_published
from .soft import (
    DEFAULT_MIN_DIVERGENCE,
    DEFAULT_RHO,
    MAX_SOFT_CONFIDENCE,
    ChamberModel,
    find_soft_mispricings,
    implied_control_probability,
)

__all__ = [
    # entry points
    "build_relationship_graph",
    "find_hard_arbs",
    "find_soft_mispricings",
    "reconcile_with_published",
    # contract
    "Leg",
    "Opportunity",
    "HARD_ARB",
    "SOFT_MISPRICING",
    "TICK",
    "index_prices",
    # graph types
    "RelationshipGraph",
    "RaceGroup",
    "ControlRelation",
    "RaceKey",
    "ParsedMarket",
    "parse_markets",
    "parse_title",
    "MAJOR_PARTIES",
    # soft model
    "ChamberModel",
    "implied_control_probability",
    # tunables
    "DEFAULT_FEES",
    "DEFAULT_MIN_EDGE",
    "DEFAULT_MAX_CONTRACTS",
    "DEFAULT_MISSING_OUTCOME_GAP",
    "DEFAULT_RHO",
    "DEFAULT_MIN_DIVERGENCE",
    "MAX_SOFT_CONFIDENCE",
]
