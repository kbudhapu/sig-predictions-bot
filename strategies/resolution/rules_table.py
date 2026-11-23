"""Per-market resolution-rules table (CORE tier, spec §1.3 — resolution-mechanics).

The factual substrate of the resolution edge: for every market, WHAT resolves it (source),
WHEN (settlement_time), and the CRITERION that decides YES vs NO. Knowing these better than a
careless student field is the edge — it drives both the decided-vs-priced gap (decided_gap.py)
and the single-wall endgame (endgame.py).

What recon actually gives us (recon/MARKET_RECON.md §5, §7):
  • settlement_time — KNOWN for all 237 markets: a single wall at 2026-11-04T17:00Z. `/markets/{id}`
    exposes `settlementDate` (→ settlement_time) plus `settledWith`/`settledOn` (populated only
    AFTER settlement — the actual outcome, not useful pre-resolution).
  • resolution_source / resolution_criterion — NOT exposed by the API. `/markets/{id}` carries only
    title/categories/settlementDate; there is no criterion or rules text. These fields are therefore
    created UNPOPULATED (``None``) and must be filled by hand at open (e.g. source="AP race call",
    criterion="party with the called plurality in the <state> <chamber> race"). `needs_population()`
    lists exactly which markets still lack them.

Pure data structure — no network, no I/O. The caller feeds it market_ids (from the live
`/markets` pull) and, later, the hand-gathered source/criterion text.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

STRATEGY_TAG = "resolution"

# The single settlement wall — recon §7.5: all 237 markets settle here, none earlier. This is also
# the tournament end (2026-11-04T17:00Z) and therefore the pre-settlement trading cutoff the
# endgame exits uncertain positions before.
SETTLEMENT_WALL = datetime(2026, 11, 4, 17, 0, 0, tzinfo=timezone.utc)
TOURNAMENT_CUTOFF = SETTLEMENT_WALL

# Sentinel for a field that is known to exist but has not been gathered yet (source/criterion are
# not API-exposed — recon §5/§7.5). ``None`` means "needs population at open", never "no such rule".
NEEDS_POPULATION: None = None


def settlement_time_from_iso(value: str) -> datetime:
    """Parse a SIG ``settlementDate`` (ISO-8601, possibly ``...Z``) to an aware UTC datetime.

    Recon saw bare dates (``2026-11-04``) and full timestamps; a date-only value is pinned to the
    known 17:00Z wall rather than guessed at midnight."""
    raw = value.strip()
    if len(raw) == 10:  # date only, e.g. "2026-11-04" — pin to the known wall time
        d = datetime.fromisoformat(raw).date()
        return datetime(d.year, d.month, d.day, 17, 0, 0, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ResolutionRule:
    """One market's resolution mechanics.

    ``resolution_source`` / ``resolution_criterion`` default to ``None`` = NEEDS_POPULATION: the
    API does not expose them (recon §5), so they are filled manually at open. ``settlement_time``
    is known from the API for every market.
    """

    market_id: str
    settlement_time: datetime
    resolution_source: str | None = NEEDS_POPULATION
    resolution_criterion: str | None = NEEDS_POPULATION

    @property
    def source_populated(self) -> bool:
        return bool(self.resolution_source)

    @property
    def criterion_populated(self) -> bool:
        return bool(self.resolution_criterion)

    @property
    def fully_populated(self) -> bool:
        return self.source_populated and self.criterion_populated

    def missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.source_populated:
            missing.append("resolution_source")
        if not self.criterion_populated:
            missing.append("resolution_criterion")
        return tuple(missing)


class ResolutionRulesTable:
    """Store/retrieve ResolutionRule rows by market_id. In-memory, deterministic, I/O-free."""

    def __init__(self, rules: Iterable[ResolutionRule] | None = None) -> None:
        self._rules: dict[str, ResolutionRule] = {}
        for rule in rules or ():
            self.upsert(rule)

    # ── store / retrieve ──
    def upsert(self, rule: ResolutionRule) -> ResolutionRule:
        self._rules[rule.market_id] = rule
        return rule

    def get(self, market_id: str) -> ResolutionRule | None:
        return self._rules.get(market_id)

    def all(self) -> tuple[ResolutionRule, ...]:
        return tuple(self._rules.values())

    def market_ids(self) -> tuple[str, ...]:
        return tuple(self._rules.keys())

    def __len__(self) -> int:
        return len(self._rules)

    def __contains__(self, market_id: object) -> bool:
        return market_id in self._rules

    # ── manual population at open (source/criterion are not API-exposed) ──
    def populate(
        self,
        market_id: str,
        *,
        resolution_source: str | None = None,
        resolution_criterion: str | None = None,
    ) -> ResolutionRule:
        """Fill the hand-gathered fields on an existing row. Only the provided fields are changed;
        passing ``None`` leaves that field as-is (use the sentinel semantics: you populate, you
        don't un-populate here). Returns the updated row."""
        current = self._rules.get(market_id)
        if current is None:
            raise KeyError(f"no resolution rule for market_id={market_id!r}")
        updated = replace(
            current,
            resolution_source=(
                resolution_source if resolution_source is not None else current.resolution_source
            ),
            resolution_criterion=(
                resolution_criterion
                if resolution_criterion is not None
                else current.resolution_criterion
            ),
        )
        self._rules[market_id] = updated
        return updated

    def needs_population(self) -> tuple[ResolutionRule, ...]:
        """Rows still missing source and/or criterion — the at-open manual-entry worklist."""
        return tuple(r for r in self._rules.values() if not r.fully_populated)

    def is_fully_populated(self) -> bool:
        return all(r.fully_populated for r in self._rules.values())


def seed_from_recon(
    market_ids: Iterable[str],
    *,
    settlement_time: datetime = SETTLEMENT_WALL,
) -> ResolutionRulesTable:
    """Build a table from the live market-id list, applying the recon finding: every market settles
    at the one wall (default SETTLEMENT_WALL), with source/criterion left UNPOPULATED for manual
    entry at open. ``settlement_time`` can be overridden per the live ``settlementDate`` if it ever
    diverges from the recon wall."""
    return ResolutionRulesTable(
        ResolutionRule(market_id=str(mid), settlement_time=settlement_time) for mid in market_ids
    )


def seed_from_markets(markets: Iterable[Mapping[str, object]]) -> ResolutionRulesTable:
    """Build a table directly from ``/markets`` payload rows, reading each market's own
    ``settlementDate`` (falling back to the recon wall when absent). Source/criterion stay
    unpopulated. ``markets`` items are the dicts the SIG client returns (id + settlementDate)."""
    table = ResolutionRulesTable()
    for m in markets:
        mid = m.get("id")
        if mid is None:
            continue
        raw_date = m.get("settlementDate")
        settlement = (
            settlement_time_from_iso(str(raw_date)) if raw_date else SETTLEMENT_WALL
        )
        table.upsert(ResolutionRule(market_id=str(mid), settlement_time=settlement))
    return table
