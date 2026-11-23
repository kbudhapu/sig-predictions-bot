"""
Orchestrator-facing protocols — the interfaces the orchestrator calls across modules.

Everything the orchestrator uses is defined here as a Protocol so the orchestrator
can be tested without live broker/state/feed dependencies.  The real implementations
(sig/broker.py, infra/state/, sig/feed/) satisfy these structurally.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Account snapshot
# ---------------------------------------------------------------------------

class AccountSnapshot(Protocol):
    buying_power: float
    equity: float


# ---------------------------------------------------------------------------
# Broker (subset the orchestrator touches)
# ---------------------------------------------------------------------------

class OrchestratorBroker(Protocol):
    async def get_account(self) -> AccountSnapshot: ...
    async def get_positions(self) -> list[Any]: ...


# ---------------------------------------------------------------------------
# State store (subset the orchestrator touches)
# ---------------------------------------------------------------------------

class OrchestratorState(Protocol):
    async def read_open_orders(self) -> list[dict[str, Any]]: ...
    async def read_exposure(self) -> dict[str, Any]: ...
    async def append_audit(self, entry: dict[str, Any]) -> None: ...
    async def set_halt(self, reason: str, metadata: dict[str, Any]) -> None: ...
    async def clear_halt(self) -> None: ...
    async def get_halt(self) -> dict[str, Any] | None: ...
    async def write_heartbeat(self, ts_iso: str) -> None: ...


# ---------------------------------------------------------------------------
# Strategy registry Protocol  (spec §3)
# ---------------------------------------------------------------------------

class Intent:
    """One order intent produced by a strategy's evaluate()."""
    def __init__(
        self,
        *,
        strategy_id: str,
        exchange_id: str,         # contract/market id
        side: str,                # "buy" | "sell"
        action: str,              # "open" | "close" | "quote"
        strategy_type: str,       # "arb" | "market_making" | "directional"
        signal_strength: float = 1.0,  # sizing hint for directional
        price: float | None = None,
        metadata: dict[str, Any] | None = None,
        legs: list[dict[str, Any]] | None = None,  # MULTI-LEG (arb): [{exchange_id, side, action, price, quantity}]
    ) -> None:
        self.strategy_id = strategy_id
        self.exchange_id = exchange_id
        self.side = side
        self.action = action
        self.strategy_type = strategy_type
        self.signal_strength = signal_strength
        self.price = price
        self.metadata = metadata or {}
        # When `legs` is set the intent is ATOMIC MULTI-LEG (arb): the orchestrator routes it to the
        # OMS atomic place_multi_leg path (all-fill-or-unwind), reserves the TOTAL once, and uses the
        # arb strategy's capture-fillable leg quantities (NOT the Browne sizer).
        self.legs = legs

    @property
    def is_multi_leg(self) -> bool:
        return bool(self.legs)


class EdgePerf:
    """Realized performance summary for the allocator."""
    def __init__(self, *, realized_pnl: float = 0.0, n_trades: int = 0) -> None:
        self.realized_pnl = realized_pnl
        self.n_trades = n_trades


@runtime_checkable
class Strategy(Protocol):
    """Registration interface every strategy must satisfy (spec §3)."""
    name: str
    tier: str          # "CORE" | "SIGNAL" | "TAIL"
    cadence_s: float   # minimum seconds between evaluate() calls

    def evaluate(self, state: Any) -> list[Intent]: ...
    def on_fill(self, fill: Any) -> None: ...
    def realized_performance(self) -> EdgePerf: ...


# ---------------------------------------------------------------------------
# OMS (subset the orchestrator calls)
# ---------------------------------------------------------------------------

class OrchestratorOMS(Protocol):
    async def startup(self) -> Any: ...
    async def submit(self, *, strategy_id: str, legs: list[dict[str, Any]],
                     fill_policy: Any, metadata: dict[str, Any] | None) -> Any: ...
    async def advance_pending_bundles(self) -> int: ...
    async def sweep_unfilled_timeouts(self) -> int: ...
    async def reconcile(self, trigger: str) -> Any: ...
    async def has_pending_bundle(self, strategy_id: str) -> bool: ...

    @property
    def is_hard_locked(self) -> bool: ...


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------

class RiskGateVerdict:
    def __init__(self, *, approved: bool, clamped_qty: float | None = None,
                 reason: str = "") -> None:
        self.approved = approved
        self.clamped_qty = clamped_qty
        self.reason = reason


class RiskGate(Protocol):
    def validate(self, intent: Intent, sized_qty: float,
                 available_capital: float) -> RiskGateVerdict: ...


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

class SizingResult:
    def __init__(self, *, qty: float, capital_commitment: float) -> None:
        self.qty = qty
        self.capital_commitment = capital_commitment


class Sizing(Protocol):
    def size(self, intent: Intent, available_capital: float,
             allocator_budget: float) -> SizingResult: ...


# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------

class Allocator(Protocol):
    def budget_for(self, strategy_id: str) -> float: ...
    def on_fill(self, strategy_id: str, pnl: float) -> None: ...
    def seed(self, strategies: list[Any]) -> None: ...
