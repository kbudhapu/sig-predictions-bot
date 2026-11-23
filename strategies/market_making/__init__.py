"""Market-making strategy (CORE tier, spec §1.2) — spread capture via symmetric resting
limits. Pure logic on market state; a separate executor places the orders.

Modules:
  quoter             — symmetric limits inside the spread (0.005 tick, [0.005,0.995] bounds)
  inventory          — per-market net position, quote skew, inventory cap
  adverse_selection  — pull/widen the side being picked off on a fast mid move
  market_maker       — composes the above and emits the executor's order/cancel contract

All liquidity / size / volatility params are PLACEHOLDERS: organic depth and volatility are
unknown pre-open (recon: a single seeded ≤1000-contract level, uniform 0.08 spread). Re-tune
at open.
"""
from .quoter import (
    TICK, PRICE_MIN, PRICE_MAX, QuoterConfig, Quote, compute_quote, round_to_tick,
)
from .inventory import InventoryConfig, InventoryBook, SkewDecision
from .adverse_selection import AdverseConfig, AdverseSelectionMonitor, AdverseSignal
from .sizing import BaseSize, SizingConfig, base_quote_size
from .market_maker import MarketMakerConfig, MarketMaker, to_contract, STRATEGY_TAG

__all__ = [
    "TICK", "PRICE_MIN", "PRICE_MAX", "QuoterConfig", "Quote", "compute_quote", "round_to_tick",
    "InventoryConfig", "InventoryBook", "SkewDecision",
    "AdverseConfig", "AdverseSelectionMonitor", "AdverseSignal",
    "SizingConfig", "BaseSize", "base_quote_size",
    "MarketMakerConfig", "MarketMaker", "to_contract", "STRATEGY_TAG",
]
