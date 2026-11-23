"""Arb-specific position sizing — many smaller concurrent arbs instead of ~3 maximal ones.

The directional per-market cap (`capital.max_per_market_frac`, 0.15) bounds how much one VIEW ABOUT
THE WORLD may lose. A hard arb is not a view; it is a bought spread whose payoff is contracted at
entry. Sizing it with a directional limit put 30% of the bankroll into a 2-leg basket, so the deploy
cap admitted about THREE concurrent arbs and every further edge was discarded for want of capital.

What actually bounds an arb is cost efficiency below and exit liquidity above:

    quantity = min( entry-fillable, per-arb cap, remaining arb budget, exit-depth ceiling )
    rejected if that quantity cannot clear the fee/slippage floor

    cfg  = load_arb_sizing_config()
    size = size_arb_basket(legs, edge_per_contract=0.04, bankroll=100_000,
                           arb_deployed=open_arb_notional, exit_books=books, cfg=cfg)
    if size.sized: place_atomically(size.legs)      # else size.reason says why not

Paired with strategies/exit (capital recycling), this is the velocity side of the competition: many
small, cost-efficient, exitable positions turning over, rather than three that cannot move until the
Nov-4 wall.
"""
from __future__ import annotations

from .config import ArbSizingConfig, load_arb_sizing_config, load_raw_config
from .sizing import (
    B_BUDGET,
    B_ENTRY,
    B_EXIT,
    B_PER_ARB,
    R_BELOW_MIN_NOTIONAL,
    R_BUDGET_EXHAUSTED,
    R_DEGENERATE_COST,
    R_EDGE_TO_COST_TOO_LOW,
    R_NO_EDGE,
    R_NO_ENTRY_DEPTH,
    R_NO_EXIT_DEPTH,
    R_NO_LEGS,
    R_SIZED,
    ArbSize,
    basket_unit_cost,
    estimate_round_trip_cost,
    exit_depth_ceiling,
    size_arb_basket,
)

__all__ = [
    "size_arb_basket",
    "ArbSize",
    "ArbSizingConfig",
    "load_arb_sizing_config",
    "load_raw_config",
    # building blocks
    "basket_unit_cost",
    "exit_depth_ceiling",
    "estimate_round_trip_cost",
    # reasons
    "R_SIZED",
    "R_NO_LEGS",
    "R_NO_EDGE",
    "R_DEGENERATE_COST",
    "R_NO_ENTRY_DEPTH",
    "R_BUDGET_EXHAUSTED",
    "R_NO_EXIT_DEPTH",
    "R_BELOW_MIN_NOTIONAL",
    "R_EDGE_TO_COST_TOO_LOW",
    # binding-constraint labels
    "B_ENTRY",
    "B_PER_ARB",
    "B_BUDGET",
    "B_EXIT",
]
