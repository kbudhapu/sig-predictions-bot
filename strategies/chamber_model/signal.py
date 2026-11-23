"""The PROVE-THEN-SIZE gate: produce and LOG the joint-structure signal before it can be sized.

The posture this enforces, and the reason it exists as code rather than as a note in a doc:

    A wrong joint-structure model is not a small loss. It produces systematically WRONG signals
    across the entire board at once -- every race shares the one national-swing assumption, so a
    bad rho biases all of them the same way simultaneously. That is the opposite of a diversified
    book, and it is indistinguishable from a working model until the single resolution day.

So `emit()` always returns a record for the log, and separately decides whether that record is
allowed to become a trade. Three independent brakes, all of which must release:

  1. OBSERVATION FLOOR  — `min_observations_before_sizing` logged observations must exist first.
     Until then every signal is log-only. There is no way to "just try it small on day one".
  2. DEGRADATION GATE   — any structural defect (unpriced seat, three-way race, missing control
     market) marks the estimate degraded, and a degraded estimate is never tradeable.
  3. SIZE CEILING       — even when released, the size multiplier is `max_size_multiplier`
     (tail-tier, small). The allocator holds joint_structure at TAIL with a small prior, and this
     is a second, independent ceiling so loosening one does not loosen both.

`would_have_been_right()` is the whole point of the logging: it scores a past record against the
realised outcome so the edge can be evaluated on its own history instead of on confidence.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .joint import ControlEstimate, estimate_control, rho_sensitivity


@dataclass(frozen=True)
class JointStructureSignal:
    """One logged observation. `tradeable` is False for every signal until the edge has proven out."""

    at_utc: str
    chamber: str
    party: str
    model_probability: float
    market_probability: float | None
    divergence: float | None
    direction: str                  # "buy" | "sell" | "none" (none = no actionable direction)
    tradeable: bool
    tier: str
    size_multiplier: float
    hypothesis: bool
    reason: str
    rho: float
    rho_sensitivity: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True, default=str)

    def would_have_been_right(self, party_won_control: bool) -> bool | None:
        """Did this signal point the right way? None when it had no direction to be right about.

        Scored against the DIRECTION, not the probability: the signal's claim is "the market price
        is wrong on this side", so being right means the outcome moved the way it pointed.
        """
        if self.direction == "none" or self.market_probability is None:
            return None
        if self.direction == "buy":
            return bool(party_won_control)
        return not bool(party_won_control)


@dataclass(frozen=True)
class ChamberModelConfig:
    """Loaded from config/sig.yaml `chamber_model:`. Every value is an UNPROVEN assumption except
    the tier/size ceilings, which are deliberate policy."""

    national_swing_rho: float
    min_divergence_to_flag: float
    min_observations_before_sizing: int
    max_size_multiplier: float
    tier: str
    enabled_for_sizing: bool
    assume_unpriced_held_by_incumbent_party: bool
    quadrature_nodes: int

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ChamberModelConfig":
        block = (cfg or {}).get("chamber_model")
        if not isinstance(block, dict):
            raise ValueError("config/sig.yaml is missing the 'chamber_model:' section")
        try:
            out = cls(
                national_swing_rho=float(block["national_swing_rho"]),
                min_divergence_to_flag=float(block["min_divergence_to_flag"]),
                min_observations_before_sizing=int(block["min_observations_before_sizing"]),
                max_size_multiplier=float(block["max_size_multiplier"]),
                tier=str(block["tier"]),
                enabled_for_sizing=bool(block["enabled_for_sizing"]),
                assume_unpriced_held_by_incumbent_party=bool(
                    block["assume_unpriced_held_by_incumbent_party"]),
                quadrature_nodes=int(block["quadrature_nodes"]),
            )
        except KeyError as e:
            raise ValueError(f"chamber_model is missing required key {e.args[0]!r}") from e
        out.validate()
        return out

    def validate(self) -> None:
        errs = []
        if not (0.0 <= self.national_swing_rho < 1.0):
            errs.append(f"national_swing_rho={self.national_swing_rho} must be in [0, 1)")
        if self.min_divergence_to_flag <= 0.0:
            errs.append("min_divergence_to_flag must be > 0")
        if self.min_observations_before_sizing < 1:
            # Zero would authorise sizing on the very first signal, which is the posture this
            # module exists to prevent.
            errs.append("min_observations_before_sizing must be >= 1 (prove-then-size)")
        if not (0.0 < self.max_size_multiplier <= 0.25):
            # A hard ceiling in CODE, not just config: this edge may not be made large by editing
            # one number. Raising it past 0.25 has to be a deliberate code change under review.
            errs.append(f"max_size_multiplier={self.max_size_multiplier} must be in (0, 0.25]")
        if str(self.tier).upper() != "TAIL":
            errs.append(f"tier={self.tier!r} must be TAIL while the model is unproven")
        if errs:
            raise ValueError("invalid chamber_model config: " + "; ".join(errs))


def load_chamber_model_config(cfg: dict | None = None, path: str | None = None) -> ChamberModelConfig:
    if cfg is None:
        import yaml

        from .baselines import _ROOT
        import os

        with open(path or os.path.join(_ROOT, "config", "sig.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    return ChamberModelConfig.from_cfg(cfg)


class JointStructureEdge:
    """Produces, logs, and (only much later) authorises sizing of the joint-structure signal."""

    EDGE_NAME = "joint_structure"

    def __init__(self, baselines, cfg: ChamberModelConfig, *, logger: Any = None) -> None:
        self._b = baselines
        self._cfg = cfg
        self._log = logger
        self._observations: list[JointStructureSignal] = []

    @property
    def observation_count(self) -> int:
        return len(self._observations)

    def observations(self) -> tuple[JointStructureSignal, ...]:
        return tuple(self._observations)

    def _sizing_released(self) -> tuple[bool, str]:
        if not self._cfg.enabled_for_sizing:
            return False, "sizing_disabled_in_config"
        if self.observation_count < self._cfg.min_observations_before_sizing:
            return False, (f"observation_floor:{self.observation_count}/"
                           f"{self._cfg.min_observations_before_sizing}")
        return True, "released"

    def emit(self, chamber_name: str, party: str, *, race_prices: dict[str, float],
             market_control_price: float | None, now: datetime | None = None
             ) -> JointStructureSignal:
        """Compute, LOG, and return one signal. Logging happens unconditionally — including for
        degraded and untradeable signals, which are the ones you most need a record of."""
        chamber = self._b.chamber(chamber_name)
        est: ControlEstimate = estimate_control(
            chamber, party,
            race_prices=race_prices,
            market_control_price=market_control_price,
            rho=self._cfg.national_swing_rho,
            assume_unpriced_held_by_incumbent_party=self._cfg.assume_unpriced_held_by_incumbent_party,
            quadrature_nodes=self._cfg.quadrature_nodes,
        )
        sens = rho_sensitivity(
            chamber, party, race_prices=race_prices,
            market_control_price=market_control_price,
            assume_unpriced_held_by_incumbent_party=self._cfg.assume_unpriced_held_by_incumbent_party,
            quadrature_nodes=self._cfg.quadrature_nodes,
        )

        direction = "none"
        reason = "no_divergence"
        if est.divergence is not None and abs(est.divergence) >= self._cfg.min_divergence_to_flag:
            direction = "buy" if est.divergence > 0 else "sell"
            reason = "divergence_flagged"

        released, release_reason = self._sizing_released()
        tradeable = bool(est.tradeable and released and direction != "none")
        if est.degraded:
            reason = "degraded:" + ",".join(est.degraded_reasons[:3])
        elif not released:
            reason = release_reason

        sig = JointStructureSignal(
            at_utc=(now or datetime.now(timezone.utc)).isoformat(),
            chamber=est.chamber,
            party=est.party,
            model_probability=est.implied_control_probability,
            market_probability=est.market_control_probability,
            divergence=est.divergence,
            direction=direction,
            tradeable=tradeable,
            tier=self._cfg.tier,
            # Zero when not tradeable, so a consumer that ignores `tradeable` still cannot size it.
            size_multiplier=(self._cfg.max_size_multiplier if tradeable else 0.0),
            hypothesis=True,
            reason=reason,
            rho=est.rho,
            rho_sensitivity={str(k): round(v, 6) for k, v in sens.items()},
            detail=dict(est.meta, degraded_reasons=list(est.degraded_reasons),
                        n_priced_races=est.n_priced_races,
                        n_expected_races=est.n_expected_races,
                        expected_seats=round(est.expected_seats, 4),
                        seats_needed_from_priced=est.seats_needed_from_priced),
        )
        self._observations.append(sig)
        self._emit_log(sig)
        return sig

    def _emit_log(self, sig: JointStructureSignal) -> None:
        """Observability must never break the cycle, and the record must land even when the signal
        is useless — an edge can only be evaluated from its refusals as well as its calls."""
        if self._log is None:
            return
        try:
            self._log.info(
                "joint_structure_signal",
                chamber=sig.chamber, party=sig.party,
                model_p=round(sig.model_probability, 6),
                market_p=sig.market_probability,
                divergence=(round(sig.divergence, 6) if sig.divergence is not None else None),
                direction=sig.direction, tradeable=sig.tradeable, tier=sig.tier,
                size_multiplier=sig.size_multiplier, hypothesis=True,
                reason=sig.reason, rho=sig.rho, rho_sensitivity=sig.rho_sensitivity,
            )
        except Exception:
            pass

    def scorecard(self, outcomes: dict[tuple[str, str], bool]) -> dict:
        """Score the logged history against realised outcomes, keyed by (chamber, party).

        This is the null test. Until it has been run on real data and come back positive, the
        honest description of this edge is "unproven", and the observation floor keeps it that way.
        """
        scored = right = 0
        for s in self._observations:
            truth = outcomes.get((s.chamber, s.party))
            if truth is None:
                continue
            verdict = s.would_have_been_right(truth)
            if verdict is None:
                continue
            scored += 1
            right += int(verdict)
        return {
            "observations": self.observation_count,
            "scored": scored,
            "correct": right,
            "hit_rate": (right / scored) if scored else None,
            "proven": False,     # never set True by code; a human promotes this edge deliberately
        }
