"""
OMS broker and state protocols — thin interfaces so the OMS can be tested
without a live broker or database.

The live implementations (sig/broker.py, infra/state/) satisfy these protocols
structurally; no inheritance needed.
"""

from __future__ import annotations

from typing import Any, Protocol


class BrokerPosition(Protocol):
    contract_id: str
    side: str       # "long" | "short" | "flat"
    qty: float
    avg_entry_price: float


class BrokerOrderSnapshot(Protocol):
    order_id: str
    contract_id: str
    side: str
    qty: float
    filled_qty: float
    avg_fill_price: float
    status: str     # open, filled, partially_filled, canceled, …


class SigBroker(Protocol):
    """Canonical broker interface — matches sig/broker.py `BrokerAPI`.

    The broker OWNS idempotency: it derives a DETERMINISTIC key from
    (strategy_id, market_id, side, intent_hash) internally. The OMS does NOT mint or pass
    idempotency keys (the duplicate layer was collapsed). `place_multi_leg` returns ONE
    result carrying per-leg detail in `.legs`.

    `get_positions`/`get_open_orders` provide VENUE-TRUTH for reconciliation (believed-state
    vs broker-truth). The live SigBroker currently LACKS these — source them from the SIG read
    client (sig_client); SimulatedBroker provides them for tests. See composition.py.
    """

    async def place_order(
        self, *, strategy_id: str, market_id: str, exchange_id: str, side: str,
        action: str, quantity: int, price: float | None = None, priority: bool = False,
        outcome: str | None = None,
    ) -> Any: ...

    async def place_multi_leg(
        self, *, strategy_id: str, market_id: str, legs: list[dict[str, Any]],
        priority: bool = True,
    ) -> Any: ...

    async def cancel_order(self, order_id: str) -> Any: ...

    async def cancel_all(self) -> Any: ...

    async def get_order(self, order_id: str) -> Any: ...

    async def get_positions(self) -> list[BrokerPosition]: ...

    async def get_open_orders(self) -> list[BrokerOrderSnapshot]: ...


class SigStateStore(Protocol):
    """Subset of infra/state/ used by the OMS."""

    async def write_order(self, order: dict[str, Any]) -> None: ...

    async def update_order_status(
        self,
        order_id: str,
        status: str,
        *,
        filled_qty: float | None = None,
        avg_fill_price: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def upsert_position(self, position: dict[str, Any]) -> None: ...

    async def read_open_orders(self) -> list[dict[str, Any]]: ...

    async def read_exposure(self) -> dict[str, Any]: ...

    async def append_audit(self, entry: dict[str, Any]) -> None: ...

    async def set_halt(self, reason: str, metadata: dict[str, Any]) -> None: ...

    async def clear_halt(self) -> None: ...

    async def get_halt(self) -> dict[str, Any] | None: ...
