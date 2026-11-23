"""Arb-sizing parameters, loaded from config/sig.yaml `arb_sizing:`. Nothing here is hardcoded.

Registered in sig/config.py's strict `_SCHEMA`, so a typo'd key fails the whole config load rather
than silently defaulting.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # strategies/arb_sizing/ -> repo root
DEFAULT_CONFIG_PATH = os.path.join(_ROOT, "config", "sig.yaml")


def load_raw_config(path: str | None = None) -> dict:
    import yaml

    with open(path or DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True, slots=True)
class ArbSizingConfig:
    """Why arb gets its own numbers at all.

    `capital.max_per_market_frac` (0.15) is a DIRECTIONAL risk limit: it bounds how much of the
    bankroll a single *view about the world* can lose. A hard arb is not a view — it is a bought
    spread whose payoff is contracted at entry, so sizing it with a directional limit is a category
    error. The cost of that error is concrete: at 15%/leg a 2-leg basket is 30% of the bankroll, so
    the deploy cap allows about THREE concurrent arbs and every further edge found is discarded for
    want of capital.

    What actually bounds an arb is not downside — it is
      (1) COST EFFICIENCY: below some size, fees + spread + slippage eat the captured edge, and
      (2) EXIT LIQUIDITY: a leg bigger than the bids can absorb cannot be recycled, which turns a
          velocity trade back into a hold-to-settlement one (see strategies/exit).
    Those two set the floor and the ceiling; `arb_max_per_arb_frac` then sets how many fit.
    """

    # --- the per-arb cap that replaces the directional per-market cap for arb orders ---
    arb_max_per_arb_frac: float          # basket notional ceiling, as a fraction of bankroll
    arb_total_max_frac: float            # ALL open arb notional, as a fraction of bankroll

    # --- cost-efficiency floor: too small and the costs eat the edge ---
    arb_min_notional: float              # absolute floor on basket notional
    arb_min_edge_to_cost_ratio: float    # captured edge must be >= ratio x estimated round-trip cost
    arb_fees: float                      # fractional fee per 1.0 of basket notional, per side

    # --- exit-liquidity ceiling: never size beyond what you could sell back ---
    arb_max_frac_of_exit_depth: float    # a leg may be at most this fraction of its bid depth

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ArbSizingConfig":
        block = (cfg or {}).get("arb_sizing")
        if not isinstance(block, dict):
            raise ValueError("config/sig.yaml is missing the 'arb_sizing:' section")
        try:
            out = cls(
                arb_max_per_arb_frac=float(block["arb_max_per_arb_frac"]),
                arb_total_max_frac=float(block["arb_total_max_frac"]),
                arb_min_notional=float(block["arb_min_notional"]),
                arb_min_edge_to_cost_ratio=float(block["arb_min_edge_to_cost_ratio"]),
                arb_fees=float(block["arb_fees"]),
                arb_max_frac_of_exit_depth=float(block["arb_max_frac_of_exit_depth"]),
            )
        except KeyError as e:
            raise ValueError(f"arb_sizing is missing required key {e.args[0]!r}") from e
        out.validate()
        return out

    def validate(self) -> None:
        errs: list[str] = []
        if not (0.0 < self.arb_max_per_arb_frac <= 1.0):
            errs.append(f"arb_max_per_arb_frac={self.arb_max_per_arb_frac} must be in (0, 1]")
        if not (0.0 < self.arb_total_max_frac <= 1.0):
            errs.append(f"arb_total_max_frac={self.arb_total_max_frac} must be in (0, 1]")
        if self.arb_max_per_arb_frac > self.arb_total_max_frac:
            # Otherwise one basket may exceed the budget for ALL baskets, and "many smaller arbs"
            # silently becomes one big one again.
            errs.append(
                f"arb_max_per_arb_frac={self.arb_max_per_arb_frac} must be <= "
                f"arb_total_max_frac={self.arb_total_max_frac}")
        if self.arb_min_notional < 0.0:
            errs.append(f"arb_min_notional={self.arb_min_notional} must be >= 0")
        if self.arb_min_edge_to_cost_ratio < 1.0:
            # A ratio below 1 authorises trades whose costs exceed their edge.
            errs.append(f"arb_min_edge_to_cost_ratio={self.arb_min_edge_to_cost_ratio} must be >= 1")
        if not (0.0 < self.arb_max_frac_of_exit_depth <= 1.0):
            errs.append(
                f"arb_max_frac_of_exit_depth={self.arb_max_frac_of_exit_depth} must be in (0, 1]")
        if not (0.0 <= self.arb_fees < 1.0):
            errs.append(f"arb_fees={self.arb_fees} must be in [0, 1)")
        if errs:
            raise ValueError("invalid arb_sizing config: " + "; ".join(errs))

    def max_concurrent_at_full_size(self) -> int:
        """How many full-size arbs the budget admits. Reported in logs/tests as the headline number
        this whole module exists to move."""
        if self.arb_max_per_arb_frac <= 0.0:
            return 0
        return int(self.arb_total_max_frac / self.arb_max_per_arb_frac)


def load_arb_sizing_config(path: str | None = None, cfg: dict | None = None) -> ArbSizingConfig:
    return ArbSizingConfig.from_cfg(cfg if cfg is not None else load_raw_config(path))
