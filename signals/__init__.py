"""sig-bot research-signal ingestion (package named `signals`, NOT `signal`).

`signal` would shadow Python's stdlib `signal` module; on Windows that breaks asyncio itself
(`AttributeError: module 'signal' has no attribute 'set_wakeup_fd'` inside the proactor loop),
and this package is async by requirement — so it is `signals/`.

Pieces:
  poly_client  — read-only async client for Polymarket's FREE public API (gamma + CLOB), no key.
  fair_value   — deterministic log-odds aggregation + extremizing + dispersion-based σ_p (NO LLM).
  divergence   — deterministic edge detection (p − p_m, cross-market, favorite–longshot flags).
  mapping      — loader for race_mapping.yaml (SIG race → Polymarket contract).

Everything here is PURE DETERMINISTIC MATH on fetched numbers — no LLM anywhere.
"""
from __future__ import annotations

from .fair_value import (
    SourceEstimate,
    FairValue,
    aggregate_fair_value,
    to_logodds,
    from_logodds,
)
from .divergence import (
    DivergenceResult,
    FavoriteLongshotFlag,
    ConfidenceTier,
    aggregate_divergence,
    cross_market_divergence,
    assess_divergence,
)
from .mapping import (
    RaceMapping,
    ExternalRef,
    load_race_mapping,
    VALID_POLARITIES,
    VALID_CONFIDENCE,
)
from .cross_market import (
    apply_polarity,
    resolve_external_prices,
    cross_market_divergence_for_race,
)

__all__ = [
    "SourceEstimate",
    "FairValue",
    "aggregate_fair_value",
    "to_logodds",
    "from_logodds",
    "DivergenceResult",
    "FavoriteLongshotFlag",
    "ConfidenceTier",
    "aggregate_divergence",
    "cross_market_divergence",
    "assess_divergence",
    "RaceMapping",
    "ExternalRef",
    "load_race_mapping",
    "VALID_POLARITIES",
    "VALID_CONFIDENCE",
    "apply_polarity",
    "resolve_external_prices",
    "cross_market_divergence_for_race",
]
