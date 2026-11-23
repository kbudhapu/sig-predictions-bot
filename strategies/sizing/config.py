"""Load sizing parameters from config/sig.yaml (never hardcode — SIG_CONFIG_SCHEMA.md is the source
of truth).

The Browne constants come from the `risk:` block, NOT from `sizing:`. That block's own comment says the
gate "RE-DERIVES the sizing module's own ceiling from shared (ρ, τ)" — so there has to be exactly one
copy of ρ_min/ρ_max/τ_floor/M_0/M_full/all_in_cap_frac. Two copies would drift, the gate would
re-derive a ceiling the sizer never used, and the authorized-endgame-skyrocket-vs-runaway distinction
(risk §3) would silently break.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO / "config" / "sig.yaml"


def load_config(path: str | pathlib.Path | None = None) -> dict:
    p = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for section in ("sizing", "risk", "capital"):
        if section not in cfg:
            raise ValueError(f"{p} is missing required '{section}' section")
    return cfg


@dataclass(frozen=True)
class SizingConfig:
    """Every tunable the sizer uses. Constructed from the yaml — nothing is defaulted in code."""

    # base
    kelly_base_fraction: float
    M_cap: float
    # k(σ_p)
    sigma_grid: tuple[float, ...]
    k_grid: tuple[float, ...]
    k_reference_sigma: float
    k_missing_sigma: float
    # reality gate
    min_edge_f: float
    max_sigma_to_act: float
    min_impact_adjusted_f: float
    min_quantity: int
    # correlation / endgame
    default_factor_loading: float
    endgame_tau: float
    endgame_min_confidence_sigma: float
    # shared Browne constants (from `risk:`)
    rho_min: float
    rho_max: float
    tau_floor: float
    M_0: float
    M_full: float
    all_in_cap_frac: float
    # shared exposure caps (from `capital:`)
    max_per_market_frac: float
    max_cluster_frac: float
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_cfg(cls, cfg: dict) -> "SizingConfig":
        s, r, c = cfg["sizing"], cfg["risk"], cfg["capital"]
        sigma_grid = tuple(float(x) for x in s["sigma_grid"])
        k_grid = tuple(float(x) for x in s["k_grid"])
        if len(sigma_grid) != len(k_grid) or len(sigma_grid) < 2:
            raise ValueError("sigma_grid and k_grid must be the same length and have >= 2 points")
        if list(sigma_grid) != sorted(sigma_grid):
            raise ValueError("sigma_grid must be ascending")
        if list(k_grid) != sorted(k_grid, reverse=True):
            # k must DECREASE in sigma — a non-monotone grid would shrink a confident estimate
            # harder than a shaky one, inverting the whole robustness argument of §2.1a.
            raise ValueError("k_grid must be non-increasing (k decreases as sigma rises)")
        return cls(
            kelly_base_fraction=float(s["kelly_base_fraction"]),
            M_cap=float(s["M_cap"]),
            sigma_grid=sigma_grid,
            k_grid=k_grid,
            k_reference_sigma=float(s["k_reference_sigma"]),
            k_missing_sigma=float(s["k_missing_sigma"]),
            min_edge_f=float(s["min_edge_f"]),
            max_sigma_to_act=float(s["max_sigma_to_act"]),
            min_impact_adjusted_f=float(s["min_impact_adjusted_f"]),
            min_quantity=int(s["min_quantity"]),
            default_factor_loading=float(s["default_factor_loading"]),
            endgame_tau=float(s["endgame_tau"]),
            endgame_min_confidence_sigma=float(s["endgame_min_confidence_sigma"]),
            rho_min=float(r["rho_min"]),
            rho_max=float(r["rho_max"]),
            tau_floor=float(r["tau_floor"]),
            M_0=float(r["M_0"]),
            M_full=float(r["M_full"]),
            all_in_cap_frac=float(r["all_in_cap_frac"]),
            max_per_market_frac=float(c["max_per_market_frac"]),
            max_cluster_frac=float(c["max_cluster_frac"]),
            raw=cfg,
        )


def load_sizing_config(path: str | pathlib.Path | None = None) -> SizingConfig:
    return SizingConfig.from_cfg(load_config(path))
