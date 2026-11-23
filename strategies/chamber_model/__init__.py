"""ChamberModel — seat baselines (FACT) and the joint-control model (HYPOTHESIS), kept apart.

Two layers, deliberately in separate modules because they deserve completely different trust:

  baselines.py  FACTUAL. The 2026 seat structure from config/chamber_baselines.yaml: composition,
                which seats are up, who holds what regardless, and the control thresholds. Every
                number is re-derived on load and a mismatch raises. Safe to rely on.

  joint.py      HYPOTHESIS. Converts the market's individual-race marginals into a distribution
                over seat counts using ONE national-swing factor, and compares the implied control
                probability to the control market. Dominated by `rho`, which is a guess. rho->0 and
                rho->1 give OPPOSITE signals from identical prices, and with ~35 correlated
                binaries settling on a single day rho is not estimable from this competition.

  signal.py     The gate between them: produce, LOG, and only much later authorise sizing. Three
                independent brakes (observation floor, degradation gate, hard size ceiling).

Usage:
    b    = load_baselines()                     # raises if the facts do not close
    cfg  = load_chamber_model_config()          # raises unless tier == TAIL and ceiling <= 0.25
    edge = JointStructureEdge(b, cfg, logger=log)
    sig  = edge.emit("senate", "republican", race_prices=px, market_control_price=0.62)
    # sig.tradeable is False until the edge has proven itself; sig is logged either way.

Known structural limit, measured not assumed: SIG does not list the Ohio Senate special, so only
34 of the 35 Senate up-seats are priced. A chamber with an unpriced seat cannot have its control
honestly priced, so such estimates come back `degraded` and are log-only by construction.
"""
from __future__ import annotations

from .baselines import (
    DEMOCRATIC,
    PARTIES,
    REPUBLICAN,
    BaselineError,
    Baselines,
    ChamberBaseline,
    Seat,
    load_baselines,
)
from .joint import (
    ControlEstimate,
    SeatDistribution,
    conditional_win_probs,
    estimate_control,
    poisson_binomial,
    rho_sensitivity,
    seat_distribution,
)
from .signal import (
    ChamberModelConfig,
    JointStructureEdge,
    JointStructureSignal,
    load_chamber_model_config,
)

__all__ = [
    # factual layer
    "load_baselines",
    "Baselines",
    "ChamberBaseline",
    "Seat",
    "BaselineError",
    "REPUBLICAN",
    "DEMOCRATIC",
    "PARTIES",
    # hypothesis layer
    "estimate_control",
    "ControlEstimate",
    "seat_distribution",
    "SeatDistribution",
    "poisson_binomial",
    "conditional_win_probs",
    "rho_sensitivity",
    # the prove-then-size gate
    "JointStructureEdge",
    "JointStructureSignal",
    "ChamberModelConfig",
    "load_chamber_model_config",
]
