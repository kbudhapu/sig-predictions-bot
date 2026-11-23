"""
Multi-key write gate — routes each broker write to one of N API keys by strategy/urgency.

The one trader process holds all 3 API keys (30 writes/min each = 90 writes/min total).
Routing by strategy class gives per-class throughput isolation without multiple processes.

The real sig/broker.py accepts a key_slot param; in tests we just track call counts.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


_SLOT_NAMES = ("arb", "market_making", "directional")
_STRATEGY_TYPE_TO_SLOT: dict[str, str] = {
    "arb": "arb",
    "market_making": "market_making",
    "directional": "directional",
    "resolution": "directional",   # resolution strategies share the directional key
    "order_flow": "market_making", # order flow rides the MM key (it's MM-adjacent)
}

_WRITES_PER_MIN = 30
_WINDOW_SECONDS = 60.0


@dataclass
class _SlotBucket:
    writes: list[float] = field(default_factory=list)  # timestamps of recent writes

    def is_allowed(self, now: float) -> bool:
        self.writes = [t for t in self.writes if now - t < _WINDOW_SECONDS]
        return len(self.writes) < _WRITES_PER_MIN

    def record(self, now: float) -> None:
        self.writes.append(now)


class WriteGate:
    """
    Routes broker writes to one of three API key slots by strategy type.

    Usage::
        gate = WriteGate()
        slot = gate.acquire_slot("arb")
        result = await broker.place_order(..., key_slot=slot)
        gate.record_write(slot)
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _SlotBucket] = {
            name: _SlotBucket() for name in _SLOT_NAMES
        }

    def slot_for(self, strategy_type: str) -> str:
        return _STRATEGY_TYPE_TO_SLOT.get(strategy_type, "directional")

    def is_allowed(self, strategy_type: str) -> bool:
        slot = self.slot_for(strategy_type)
        return self._buckets[slot].is_allowed(time.monotonic())

    def record_write(self, strategy_type: str) -> None:
        slot = self.slot_for(strategy_type)
        self._buckets[slot].record(time.monotonic())

    def writes_remaining(self, strategy_type: str) -> int:
        slot = self.slot_for(strategy_type)
        bucket = self._buckets[slot]
        now = time.monotonic()
        bucket.writes = [t for t in bucket.writes if now - t < _WINDOW_SECONDS]
        return max(_WRITES_PER_MIN - len(bucket.writes), 0)
