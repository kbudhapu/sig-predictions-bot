"""rebalance.py — cadenced, step-capped, phase-aware rebalancing (SIG_DYNAMIC_ALLOCATION_SPEC §4).

- CADENCED: rebalance on a clock (`rebalance_interval_s`), NOT per-trade (churn is noise + write-budget burn).
- STEP-CAP (PAD's mechanism — the fix for mbappe's un-smoothed 0.84↔0.20 oscillation): move at most
  `max_step` (0.20) of the gap toward the new target per rebalance; freed budget goes to RESERVE (not
  instantly redeployed — the step-capped move simply leaves Σ below max_deploy until later rebalances).
- PHASE-AWARE: endgame-if-BEHIND DEFERS to the Browne concentration (shift to the best directional edge);
  endgame-if-AHEAD tightens to the low-variance CORE. Grind uses the target as-is.

Seeded at the mechanical-core PRIOR (cold-start) so the first rebalances step *gradually* away from the
starved-directional default — never a jump to a hot target.
"""
from __future__ import annotations

import time
from enum import Enum

from strategies.allocator.priors import Priors


class Phase(str, Enum):
    GRIND = "GRIND"
    ENDGAME = "ENDGAME"


class Standing(str, Enum):
    BEHIND = "BEHIND"
    AHEAD = "AHEAD"
    NEUTRAL = "NEUTRAL"


class Rebalancer:
    def __init__(self, cfg: dict, priors: Priors, *, clock=time.monotonic) -> None:
        a = cfg["allocator"]
        self.interval = float(a["rebalance_interval_s"])
        self.max_step = float(a["max_step"])
        self.priors = priors
        self._clock = clock
        self._last: float | None = None
        self.current: dict[str, float] | None = None

    def seed(self, weights: dict[str, float] | None = None, *, now: float | None = None) -> None:
        self.current = dict(weights) if weights is not None else self.priors.cold_start_weights()
        self._last = now if now is not None else self._clock()

    def due(self, now: float | None = None) -> bool:
        now = now if now is not None else self._clock()
        return self._last is None or (now - self._last) >= self.interval

    # ---- phase deferral (endgame agrees with Browne by construction) ----
    def _phase_adjust(self, target: dict[str, float], scores: dict, phase: Phase, standing: Standing) -> dict[str, float]:
        if phase == Phase.ENDGAME and standing == Standing.BEHIND:
            directional = [nm for nm in target if self.priors.tier(nm) in ("SIGNAL", "TAIL")]
            if directional:
                best = max(directional, key=lambda nm: scores.get(nm, {}).get("score_mult", 0.0))
                dir_budget = sum(target[nm] for nm in directional)
                adj = dict(target)
                for nm in directional:
                    adj[nm] = 0.0
                adj[best] = min(dir_budget, self.priors.max_edge_frac)   # concentrate the best directional (capped)
                return adj
        if phase == Phase.ENDGAME and standing == Standing.AHEAD:
            total = sum(target.values())
            core = {nm: target[nm] for nm in target if self.priors.is_core(nm)}
            core_tot = sum(core.values()) or 1.0
            return {nm: (target[nm] / core_tot * total if self.priors.is_core(nm) else 0.0) for nm in target}
        return dict(target)

    async def step(self, target: dict[str, float], scores: dict | None = None, *,
                   phase: Phase = Phase.GRIND, standing: Standing = Standing.NEUTRAL,
                   now: float | None = None, force: bool = False) -> dict[str, float]:
        """Move `current` at most max_step toward (phase-adjusted) `target`, but only when a rebalance is
        DUE (cadence). Returns the new current allocation. `force=True` bypasses the cadence (for tests/ops)."""
        now = now if now is not None else self._clock()
        if self.current is None:
            self.seed(now=now)                 # cold-start at the mechanical prior, then step from there
        if not force and not self.due(now):
            return dict(self.current)           # cadence: not due → unchanged (no churn)
        tgt = self._phase_adjust(target, scores or {}, phase, standing)
        new = {nm: self.current.get(nm, 0.0) + self.max_step * (tgt[nm] - self.current.get(nm, 0.0)) for nm in tgt}
        # any edge not in target decays toward 0 at the same step-cap
        for nm in self.current:
            if nm not in new:
                new[nm] = self.current[nm] * (1.0 - self.max_step)
        self.current = new
        self._last = now
        return dict(new)
