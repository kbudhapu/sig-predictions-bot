"""Cross-market divergence execution engine (SIGNAL tier) — trade SIG's lag behind sharp external
prices (Polymarket/Kalshi). Reads the already-correct sharp price; sizes by how STALE SIG is vs it;
NEVER forecasts where the sharp price will go. Dormant until the real race_mapping lands with verified
high-confidence, liquid mappings (RaceMapping.is_active)."""
from .strategy import (
    CrossMarketDivergenceStrategy, DivergenceConfig, ExternalQuote, skepticism_multiplier,
)

__all__ = [
    "CrossMarketDivergenceStrategy", "DivergenceConfig", "ExternalQuote", "skepticism_multiplier",
]
