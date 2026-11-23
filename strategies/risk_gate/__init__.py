"""Deterministic pre-trade risk gate (Layer A, SIG_RISK_GOVERNANCE_SPEC §2–3).

EVERY order (single, and each leg of multi-leg) passes through `RiskGate.check_order` before the broker.
Pure deterministic validation — NO LLM, NO network, NO probabilistic calls. The gate only ever REDUCES
(clamps smaller) or REJECTS; it never increases a size. Strategies PROPOSE; the gate DISPOSES.
"""
from __future__ import annotations

from .gate import RiskGate, GateResult

__all__ = ["RiskGate", "GateResult"]
