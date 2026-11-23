"""Order-flow inference strategy (SIGNAL tier, spec §2.3) — read the trade tape, detect
one-sided crowd extremes, and fade them ONLY when they disagree with the informed view
(fair-value / real-money cross-market). Flow that agrees with the real-money markets is
informed — not faded (followed or stood aside). Pure logic on tape/state; the executor
places any orders.

Modules:
  flow    — tape ingestion + rolling buy/sell imbalance + one-sided-extreme detection
  signal  — fade-vs-informed classification, the edge signal, and the market-making
            informed_flow_detected() hook (MM calls it to pull quotes on informed flow)

All thresholds/windows are PLACEHOLDERS to tune at open (organic flow unknown pre-open).
"""
from .flow import Trade, FlowConfig, FlowState, FlowBook, pressure_sign
from .signal import (
    ReferenceView, SignalConfig, OrderFlowSignal, OrderFlowEngine,
    QUIET, DUMB, INFORMED, UNCONFIRMED, EDGE_TAG,
)

__all__ = [
    "Trade", "FlowConfig", "FlowState", "FlowBook", "pressure_sign",
    "ReferenceView", "SignalConfig", "OrderFlowSignal", "OrderFlowEngine",
    "QUIET", "DUMB", "INFORMED", "UNCONFIRMED", "EDGE_TAG",
]
