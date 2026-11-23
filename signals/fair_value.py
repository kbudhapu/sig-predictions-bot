"""Deterministic fair-value aggregation for the SIG research signal — NO LLM, pure math.

Pipeline (SIG_RESEARCH_SIGNAL_SPEC §2–3):
  (a) each source probability → log-odds     ℓ = ln(p / (1 − p))
  (b) equal-weight mean across DISTINCT source classes (each real-money venue is its own class;
      the correlated model+poll cluster must be collapsed to ONE class by the CALLER — §1 effective-N)
  (c) EXTREMIZE away from 0.5                 ℓ_cal = a · ℓ̄            (a > 1, default 1.2)
  (d) σ_p from source DISPERSION in log-odds, inflated for shared-information + thinness + staleness,
      then propagated to probability space via the delta method  dp/dℓ = p·(1 − p).

Why log-odds and not probability-space averaging: 0.9 and 0.1 average to 0.5 in probability space,
destroying the information; log-odds is the correct geometry (§2). A single source has no computable
dispersion → treated as maximally uncertain (large σ_p) so the sizing layer shrinks it toward zero (§3).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Clamp probabilities away from 0/1 so the log-odds transform stays finite.
_EPS = 1e-6


def _clamp_p(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, float(p)))


def to_logodds(p: float) -> float:
    """ℓ = ln(p / (1 − p)), with p clamped to (ε, 1−ε)."""
    p = _clamp_p(p)
    return math.log(p / (1.0 - p))


def from_logodds(l: float) -> float:
    """Inverse logit: p = 1 / (1 + e^{−ℓ}); overflow-safe for large |ℓ|."""
    if l >= 0:
        z = math.exp(-l)
        return 1.0 / (1.0 + z)
    z = math.exp(l)
    return z / (1.0 + z)


@dataclass(frozen=True)
class SourceEstimate:
    """One source's probability for a race's YES/target outcome.

    `source_class` is the DISTINCT class the source belongs to — e.g. "polymarket", "kalshi",
    "models_polls". Correlated sources (538 + Cook + Sabato + polls) must share ONE class so the
    aggregate counts them once (§1 effective-N). `updated_at` is tz-aware; None ⇒ unknown age ⇒ stale.
    """

    source_class: str
    p: float
    updated_at: datetime | None = None


@dataclass(frozen=True)
class FairValue:
    """The signal's per-race output (spec §5 write schema is {p, σ_p, sources_used, updated_at})."""

    p: float
    sigma_p: float
    sources_used: list[str]
    updated_at: datetime
    # extras (useful to the sizing/risk layers and for debugging; harmless to ignore)
    n_source_classes: int = 0
    logodds: float = 0.0
    sigma_logodds: float = 0.0
    extremize_factor: float = 1.0
    stale: bool = False
    max_source_age_hours: float | None = None


def _age_hours(ua: datetime | None, now: datetime, unknown_age_hours: float) -> float:
    if ua is None:
        return unknown_age_hours
    if ua.tzinfo is None:  # be forgiving about naive datetimes → assume UTC
        ua = ua.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ua).total_seconds() / 3600.0)


def aggregate_fair_value(
    estimates: list[SourceEstimate],
    *,
    extremize_factor: float = 1.2,
    now: datetime | None = None,
    staleness_halflife_hours: float = 24.0,
    stale_after_hours: float = 12.0,
    staleness_mult_cap: float = 3.0,
    unknown_age_hours: float = 48.0,
    shared_info_inflation: float = 1.25,
    single_class_sigma_logodds: float = 1.5,
    min_sigma_logodds: float = 0.15,
) -> FairValue:
    """Aggregate per-source probabilities into a calibrated fair value `p` and confidence `σ_p`.

    Deterministic; no randomness, no LLM. `extremize_factor` (a) and `shared_info_inflation` are the
    calibration knobs (spec §6 — start literature-standard, tune against live SIG calibration later).
    """
    if not estimates:
        raise ValueError("aggregate_fair_value requires at least one SourceEstimate")
    if extremize_factor <= 0:
        raise ValueError("extremize_factor must be > 0")
    now = now or datetime.now(timezone.utc)

    # (a)+(b) group by DISTINCT class; average within a class in log-odds, then equal-weight across classes.
    by_class: dict[str, list[float]] = {}
    ages: list[float] = []
    for e in estimates:
        if not math.isfinite(e.p):
            raise ValueError(f"non-finite probability from source_class={e.source_class!r}")
        by_class.setdefault(e.source_class, []).append(to_logodds(e.p))
        ages.append(_age_hours(e.updated_at, now, unknown_age_hours))

    classes = sorted(by_class)
    class_logodds = [statistics.fmean(by_class[c]) for c in classes]
    n = len(classes)

    # (c) extremize away from 0.5.
    lbar = statistics.fmean(class_logodds)
    lcal = extremize_factor * lbar
    p = _clamp_p(from_logodds(lcal))

    # (d) σ_p from dispersion across classes, in log-odds units.
    if n >= 2:
        disp = statistics.stdev(class_logodds)            # sample std across distinct classes
        se = disp / math.sqrt(n)                           # SE of the equal-weight mean (effective-N, §3)
        sigma_l = max(se, min_sigma_logodds) * shared_info_inflation
    else:
        # single class → no computable dispersion → maximally uncertain (§3)
        sigma_l = single_class_sigma_logodds

    # staleness inflation: the OLDEST input drives it (conservative); None age already mapped to unknown_age.
    max_age = max(ages)
    staleness_mult = 1.0 + max(0.0, max_age - stale_after_hours) / staleness_halflife_hours
    staleness_mult = min(staleness_mult, staleness_mult_cap)
    sigma_l *= staleness_mult

    # the calibrated log-odds is a·ℓ̄, so its uncertainty scales by the extremize factor too.
    sigma_l_cal = extremize_factor * sigma_l

    # propagate log-odds σ to probability space: dp/dℓ = p(1−p).
    sigma_p = p * (1.0 - p) * sigma_l_cal
    sigma_p = min(0.5, max(0.0, sigma_p))  # σ of a prob in [0, 0.5]

    return FairValue(
        p=p,
        sigma_p=sigma_p,
        sources_used=classes,
        updated_at=now,
        n_source_classes=n,
        logodds=lcal,
        sigma_logodds=sigma_l_cal,
        extremize_factor=extremize_factor,
        stale=max_age > stale_after_hours,
        max_source_age_hours=max_age,
    )
