"""Minimal SLO / signal type shapes the ported degradation ladder consumes. In mbappe these come from
src/engine/slo_monitor.py (a large data-integrity monitor) and src/models.SignalAction; SIG only needs
the small enums/verdict shape the DegradationManager reads, so they are defined here self-contained. A
SIG SLO monitor (feed freshness / orderbook staleness) emits DataIntegrityVerdict; the ladder is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IntegritySeverity(str, Enum):
    OK = "OK"
    SOFT_BREACH = "SOFT_BREACH"
    HARD_BREACH = "HARD_BREACH"


@dataclass(frozen=True)
class DataIntegrityVerdict:
    severity: IntegritySeverity
    reasons: list[str] = field(default_factory=list)


class SignalAction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"
    HOLD = "HOLD"
