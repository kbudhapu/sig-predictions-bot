"""Dynamic capital allocator (SIG_DYNAMIC_ALLOCATION_SPEC): allocate capital across the edge portfolio by
risk-adjusted REALIZED performance, from a mechanical-core-heavy prior, shrunk for small samples, loss-
penalized, cadenced + step-capped + phase-aware. Sets each edge's BUDGET; proposes weights the risk gate caps."""
from strategies.allocator.config import load_config
from strategies.allocator.priors import Priors, EdgePrior
from strategies.allocator.allocator import Allocator, EdgeState
from strategies.allocator.rebalance import Rebalancer, Phase, Standing

__all__ = ["load_config", "Priors", "EdgePrior", "Allocator", "EdgeState", "Rebalancer", "Phase", "Standing"]
