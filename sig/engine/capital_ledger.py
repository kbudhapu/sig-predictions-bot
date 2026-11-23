"""
Cycle-local capital reservation ledger — ported from mbappe ReservedCapitalLedger.

The single-process trader's lock-free capital arbiter:
  - begin_cycle() snapshots buying power ONCE per cycle.
  - reserve(strategy_id, amount) reduces available balance before the next strategy is sized.
  - available_buying_power() = snapshot − reserved_total
  - No mutex, no distributed lock — the sequential-single-loop structure removes the need.

Ported from: mbappe src/engine/reserved_capital_ledger.py
Stripped: CoordinatedLegPlan coupling, Alpaca Account model (replaced with plain float snapshot).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CapitalSnapshot:
    buying_power: float
    equity: float


@dataclass
class CapitalLedger:
    """Cycle-local buying-power reservation ledger.

    Ported from mbappe ReservedCapitalLedger.  Stripped of mbappe's Account/RiskConfig
    coupling — operates on plain floats so it has no dependency on mbappe models.
    """

    _snapshot: CapitalSnapshot | None = None
    _reserved_total: float = 0.0
    _reserved_by_strategy: dict[str, float] = field(default_factory=dict)
    # Hard carve-outs for ISOLATED strategies that run outside this ledger (e.g. the fair-band maker): each
    # callable returns $ of free cash that must stay untouched by every strategy sized here. Summed and taken
    # off the venue snapshot at begin_cycle, so no orchestrator strategy can ever spend it. A failing callable
    # reuses its last value (never silently drops the carve-out).
    external_reserves: dict[str, Callable[[], float]] = field(default_factory=dict)
    _last_reserve: dict[str, float] = field(default_factory=dict)

    def external_reserve_total(self) -> float:
        tot = 0.0
        for name, fn in list(self.external_reserves.items()):
            try:
                v = max(float(fn()), 0.0)
                self._last_reserve[name] = v
            except Exception:  # noqa: BLE001
                v = self._last_reserve.get(name, 0.0)
            tot += v
        return tot

    def begin_cycle(self, buying_power: float, equity: float) -> None:
        """Snapshot broker account once at the top of a cycle.  Resets all reservations.
        External carve-outs are subtracted from buying power before anything is sized."""
        self._snapshot = CapitalSnapshot(
            buying_power=max(float(buying_power) - self.external_reserve_total(), 0.0),
            equity=max(float(equity), 0.0),
        )
        self._reserved_total = 0.0
        self._reserved_by_strategy = {}

    @property
    def snapshot_buying_power(self) -> float:
        return self._snapshot.buying_power if self._snapshot else 0.0

    @property
    def reserved_total(self) -> float:
        return float(self._reserved_total)

    def available_buying_power(self) -> float:
        if self._snapshot is None:
            return 0.0
        return max(self._snapshot.buying_power - self._reserved_total, 0.0)

    def reserve(self, strategy_id: str, amount: float) -> float:
        """Reserve capital for one strategy order.

        If the strategy already has a reservation, replaces it (idempotent re-size).
        Returns the actual reserved amount (capped at available).
        """
        if self._snapshot is None:
            return 0.0
        prior = float(self._reserved_by_strategy.get(strategy_id, 0.0))
        self._reserved_total = max(self._reserved_total - prior, 0.0)
        available = self.available_buying_power()
        capped = min(max(float(amount), 0.0), available)
        self._reserved_by_strategy[strategy_id] = capped
        self._reserved_total += capped
        return capped

    def release(self, strategy_id: str) -> float:
        """Release a strategy's reservation (e.g. after order placed or skipped)."""
        released = float(self._reserved_by_strategy.pop(strategy_id, 0.0))
        self._reserved_total = max(self._reserved_total - released, 0.0)
        return released

    def release_all(self) -> None:
        """Drop all reservations without resetting the snapshot.

        Called on cycle preemption so stale reservations don't carry over.
        Ported from mbappe ReservedCapitalLedger.release_all().
        """
        self._reserved_total = 0.0
        self._reserved_by_strategy.clear()
