"""
Strategy registry — the pluggable strategy table the cycle iterates.

Strategies register once at startup (spec §3); the cycle asks which are DUE and runs them.
Adding a strategy requires no changes to the loop, gate, OMS, or broker.

Cadence-gating: arb has short cadence_s → due on most ticks.
                directional has long cadence_s → due every N ticks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sig.engine.protocols import Strategy, EdgePerf, Intent


@dataclass
class _StrategyEntry:
    strategy: Strategy
    last_run_at: float = 0.0        # monotonic timestamp
    consecutive_failures: int = 0
    disabled: bool = False


class StrategyRegistry:
    """
    Stores registered strategies and tracks cadence / failure state per strategy.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _StrategyEntry] = {}

    def register(self, strategy: Strategy, now: float | None = None) -> None:
        # Anchor last_run_at to registration time (monotonic), NOT epoch 0.0 — otherwise
        # `due()` compares against seconds-since-boot and any finite cadence is instantly
        # "due" (a long-cadence strategy must wait a full cadence after registration).
        self._entries[strategy.name] = _StrategyEntry(
            strategy=strategy,
            last_run_at=now if now is not None else time.monotonic(),
        )

    def due(self, now: float | None = None) -> list[Strategy]:
        """Return strategies whose cadence has elapsed since last run."""
        t = now if now is not None else time.monotonic()
        result = []
        for entry in self._entries.values():
            if entry.disabled:
                continue
            if (t - entry.last_run_at) >= entry.strategy.cadence_s:
                result.append(entry.strategy)
        return result

    def mark_ran(self, name: str, now: float | None = None) -> None:
        entry = self._entries.get(name)
        if entry:
            entry.last_run_at = now if now is not None else time.monotonic()
            entry.consecutive_failures = 0

    def mark_failure(self, name: str) -> int:
        entry = self._entries.get(name)
        if entry is None:
            return 0
        entry.consecutive_failures += 1
        return entry.consecutive_failures

    def disable(self, name: str) -> None:
        entry = self._entries.get(name)
        if entry:
            entry.disabled = True

    def all_strategies(self) -> list[Strategy]:
        return [e.strategy for e in self._entries.values()]

    def get(self, name: str) -> Strategy | None:
        entry = self._entries.get(name)
        return entry.strategy if entry else None
