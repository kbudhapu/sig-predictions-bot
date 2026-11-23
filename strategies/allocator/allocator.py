"""allocator.py — the dynamic capital allocator core (SIG_DYNAMIC_ALLOCATION_SPEC).

For each edge it computes a budget weight w_i from:
  - a RISK-ADJUSTED realized-performance score over a rolling window with exponential decay (not raw P&L),
  - SHRUNK toward the edge's prior for small samples:  score = (n·measured + k·prior)/(n + k)
    (the lucky-streak guard — a 2-trade streak can't grab the book),
  - times a LOSS-ATTRIBUTION penalty 1 − min(loss_share, cap) (stolen from mbappe — penalize the edge
    bleeding NOW),
  - starting from the mechanical-core-heavy PRIOR (CORE ~70 / SIGNAL ~25 / TAIL ~5),
then normalized so Σ w_i ≤ max_deploy_frac, with the CORE floor applied (holds while the edge works, lifts
when it dies), then PROPOSE-THEN-CAP against the risk envelope (copied from mbappe's live flow — the
allocator only ever gets reduced downstream; it never overrides the caps).

Pure computation; the public entry points are async (non-blocking) to compose with the bot's async loops.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from strategies.allocator.priors import Priors


@dataclass
class EdgeState:
    """What the allocator knows about an edge right now.
    - returns: per-observation realized return-on-capital, OLDEST→NEWEST (decay weights the newest most).
    - loss_share: this edge's share of the book's CURRENT realized losses, in [0, 1] (the loss-attribution
      penalty input — catches a just-turned-bad edge before the rolling window fully updates)."""
    name: str
    returns: list[float] = field(default_factory=list)
    loss_share: float = 0.0


class Allocator:
    def __init__(self, cfg: dict, priors: Priors | None = None) -> None:
        a = cfg["allocator"]
        self.priors = priors or Priors(cfg)
        self.k = float(a["shrinkage_k"])
        self.decay = float(a["decay"])
        self.window = int(a["rolling_window"])
        self.gamma = float(a["gamma"])
        self.mult_min = float(a["mult_min"])
        self.mult_max = float(a["mult_max"])
        self.loss_cap = float(a["loss_attribution_cap"])
        self.release_eps = float(a["core_floor_release_eps"])
        self.release_min_n = int(a["core_floor_release_min_n"])
        # STATIC mode: bypass the dynamic performance-weighting entirely and allocate strictly by the
        # configured priors (normalized to max_deploy_frac). The dynamic path shrinks weight toward
        # REALIZED performance, which starves an edge whose winners are still UNREALIZED (cross-market's
        # big shorts) — operator opted to size manually via priors instead. score_mult≡1, floors≡0.
        self.static = bool(a.get("static", False))

    # ---- risk-adjusted, decayed performance ----
    def _decayed_stats(self, returns: list[float]) -> tuple[float, float, int]:
        r = returns[-self.window:]
        n = len(r)
        if n == 0:
            return 0.0, 0.0, 0
        w = [self.decay ** (n - 1 - j) for j in range(n)]   # newest weight 1.0, older decays
        sw = sum(w)
        mean = sum(wj * rj for wj, rj in zip(w, r)) / sw
        var = sum(wj * (rj - mean) ** 2 for wj, rj in zip(w, r)) / sw
        return mean, math.sqrt(max(var, 0.0)), n

    def _measured_mult(self, mean: float, std: float) -> float:
        sharpe = mean / (std + 1e-9)                          # risk-adjusted: lucky high-variance ⇒ low sharpe
        # clamp the EXPONENT (not the result) so a zero-variance streak can't overflow exp()
        x = min(max(self.gamma * sharpe, math.log(self.mult_min)), math.log(self.mult_max))
        return math.exp(x)

    def score(self, st: EdgeState) -> dict:
        if self.static:   # manual sizing: every edge sits exactly at its prior (no perf shrink/penalty)
            return {"n": 0, "mean": 0.0, "std": 0.0, "measured": 1.0, "shrunk": 1.0,
                    "penalty": 1.0, "score_mult": 1.0}
        mean, std, n = self._decayed_stats(st.returns)
        measured = self._measured_mult(mean, std) if n > 0 else 1.0
        # shrink toward the prior's NEUTRAL performance (1.0): n=0 ⇒ shrunk=1.0 ⇒ edge sits at its prior weight
        # (the dataless-to-prior guard); small n ⇒ ≈1.0 (lucky streak can't move it); large n ⇒ ≈measured.
        shrunk = (n * measured + self.k * 1.0) / (n + self.k)
        penalty = 1.0 - min(max(st.loss_share, 0.0), self.loss_cap)
        return {"n": n, "mean": mean, "std": std, "measured": measured, "shrunk": shrunk,
                "penalty": penalty, "score_mult": shrunk * penalty}

    def _floor_active(self, name: str, sc: dict) -> float:
        """The CORE floor is a MINIMUM that holds WHILE the edge works, and LIFTS when the edge dies
        (enough data AND decayed realized performance ~zero/negative)."""
        if self.static:   # manual sizing: weights come straight from priors, floors are moot
            return 0.0
        fl = self.priors.floor(name)
        if fl <= 0.0:
            return 0.0
        if sc["n"] >= self.release_min_n and sc["mean"] <= self.release_eps:
            return 0.0                                       # dead mechanical edge → release the floor
        return fl

    # ---- normalization with CORE floors as minimums ----
    def _normalize_with_floors(self, raw: dict[str, float], floors: dict[str, float]) -> dict[str, float]:
        md = self.priors.max_deploy_frac
        tot = sum(raw.values()) or 1.0
        w = {nm: raw[nm] / tot * md for nm in raw}            # proportional, Σ = md
        w = {nm: max(w[nm], floors.get(nm, 0.0)) for nm in w}  # floors as minimums
        excess = sum(w.values()) - md
        if excess > 1e-12:
            headroom = {nm: w[nm] - floors.get(nm, 0.0) for nm in w if w[nm] > floors.get(nm, 0.0)}
            tot_head = sum(headroom.values())
            if tot_head >= excess and tot_head > 0:
                for nm, h in headroom.items():
                    w[nm] -= excess * h / tot_head           # take the excess only from above-floor headroom
            else:                                            # (rare) floors exceed budget → scale all to fit
                s = sum(w.values())
                w = {nm: v * md / s for nm, v in w.items()}
        return w

    def apply_risk_caps(self, w: dict[str, float]) -> dict[str, float]:
        """PROPOSE-THEN-CAP (copied from mbappe): clamp each edge to max_edge_frac and Σ to max_deploy_frac.
        The caps WIN — the allocator operates inside the risk envelope and never overrides it."""
        me = self.priors.max_edge_frac
        md = self.priors.max_deploy_frac
        capped = {nm: min(v, me) for nm, v in w.items()}     # per-edge ceiling
        s = sum(capped.values())
        if s > md:
            capped = {nm: v * md / s for nm, v in capped.items()}   # aggregate ceiling
        return capped

    async def propose(self, states: dict[str, EdgeState] | None = None) -> dict:
        """Compute proposed per-edge budgets. Edges absent from `states` are DATALESS → sit at their prior
        (the guard). Returns {proposed, weights (risk-capped), scores}."""
        states = states or {}
        names = self.priors.names()
        scores = {nm: self.score(states.get(nm, EdgeState(nm))) for nm in names}
        raw = {nm: self.priors.prior_weight(nm) * scores[nm]["score_mult"] for nm in names}
        floors = {nm: self._floor_active(nm, scores[nm]) for nm in names}
        proposed = self._normalize_with_floors(raw, floors)
        weights = self.apply_risk_caps(proposed)             # propose → cap
        return {"proposed": proposed, "weights": weights, "scores": scores}
