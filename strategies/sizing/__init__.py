"""Position sizing (SIG_TOURNAMENT_SIZING_SPEC): turn an edge + tournament standing into an order size.

The objective is maximize P(finish top-3), NOT maximize growth — so this is deliberately not standard
Kelly:

    size = k(σ_p)·f*  ×  M(ρ, τ)  ÷  √n_eff   , within the allocator's edge budget, then capped
                                  (√n_eff is INVERTED in the endgame-if-behind override, §2.3)

M(ρ, τ) is a BEHAVIORAL SHAPE reproducing Browne's policy (more aggressive late, more aggressive
behind, de-risking ahead, memoryless), NOT Browne's continuous-time closed form — that formula assumes
a continuously-rebalanced Black–Scholes asset and does not apply to discrete settle-at-end binaries.
See browne.py for exactly what transfers and what does not.

    sizer = Sizer()                                   # params from config/sig.yaml
    d = await sizer.size_edge(edge, standing, budget=2_000.0, book=orderbook)
    if d.sized: place(d.order.to_dict())               # else d.reason says why not
    decisions = await sizer.size_book(edges, standing, budgets=by_exchange, books=books)
"""

from __future__ import annotations

from .browne import (
    BrowneState,
    concentration_fraction,
    is_endgame_behind,
    multiplier,
    standing_to_cdf_argument,
)
from .config import SizingConfig, load_config, load_sizing_config
from .impact import (
    Fill,
    fill_through,
    impact_adjusted_f,
    max_fillable_with_edge,
    round_limit_price,
)
from .kelly import (
    NO,
    YES,
    correlation_from_loadings,
    diversification_divisor,
    effective_n,
    entry_price,
    k_of_sigma,
    kelly_fraction_star,
)
from .sizing import Edge, Sizer, SizedOrder, SizingDecision, Standing

__all__ = [
    # entry points
    "Sizer",
    "Edge",
    "Standing",
    "SizedOrder",
    "SizingDecision",
    # config
    "load_config",
    "load_sizing_config",
    "SizingConfig",
    # Browne shape
    "BrowneState",
    "multiplier",
    "is_endgame_behind",
    "concentration_fraction",
    "standing_to_cdf_argument",
    # Kelly base
    "kelly_fraction_star",
    "k_of_sigma",
    "entry_price",
    "effective_n",
    "diversification_divisor",
    "correlation_from_loadings",
    "YES",
    "NO",
    # impact
    "Fill",
    "fill_through",
    "impact_adjusted_f",
    "max_fillable_with_edge",
    "round_limit_price",
]
