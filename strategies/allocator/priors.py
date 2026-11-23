"""priors.py — the mechanical-core-heavy starting allocation (SIG_EDGE_PORTFOLIO_SPEC tiers) + the
dataless-defaults-to-PRIOR guard (SIG_DYNAMIC_ALLOCATION_SPEC §3 / §0a — the structural fix for mbappe's
cold-start 4× bug).

CRITICAL GUARD: the backbone is performance × prior, NOT inverse-vol. So an edge with NO data sits at its
PRIOR (arb/MM funded, directional starved) — the safe default — never at a vol-derived "lowest-vol → max
weight". Any inverse-vol term is a within-edge secondary that is INERT with no data. This module encodes the
priors/floors/caps; the allocator realizes the guard (n=0 ⇒ neutral multiplier ⇒ weight == prior).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgePrior:
    name: str
    tier: str            # CORE | SIGNAL | TAIL
    prior: float         # pre-data allocation weight (fraction; priors across edges sum to ~1.0)
    floor: float         # CORE income floor this edge won't drop below WHILE WORKING (0 for non-CORE)


class Priors:
    """Priors / floors / caps loaded from config. All values come from config/sig.yaml (no hardcoding)."""

    def __init__(self, cfg: dict) -> None:
        a = cfg["allocator"]
        cap = cfg["capital"]
        self.edges: dict[str, EdgePrior] = {
            name: EdgePrior(name=name, tier=str(e["tier"]), prior=float(e["prior"]), floor=float(e.get("floor", 0.0)))
            for name, e in a["edges"].items()
        }
        self.max_deploy_frac = float(cap["max_deploy_frac"])
        self.max_edge_frac = float(cap.get("max_edge_frac", self.max_deploy_frac))

    def names(self) -> list[str]:
        return list(self.edges)

    def prior_weight(self, name: str) -> float:
        return self.edges[name].prior

    def floor(self, name: str) -> float:
        return self.edges[name].floor

    def tier(self, name: str) -> str:
        return self.edges[name].tier

    def is_core(self, name: str) -> bool:
        return self.edges[name].tier == "CORE"

    def cold_start_weights(self) -> dict[str, float]:
        """The dataless / cold-start allocation: priors normalized to Σ = max_deploy_frac. This is EXACTLY
        what a fully-dataless portfolio allocates to (the guard) — mechanical-core-heavy, directional starved,
        NEVER a vol-derived spike."""
        tot = sum(e.prior for e in self.edges.values()) or 1.0
        return {name: e.prior / tot * self.max_deploy_frac for name, e in self.edges.items()}
