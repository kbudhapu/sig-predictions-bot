"""The relationship graph: same-race complementary groups + chamber-control identities.

SIG publishes 0 multi-outcome and 0 composite markets (recon §2) — every relationship in this
tournament is IMPLICIT and must be reconstructed here:

  (a) same-race groups — every listed party for one (region, chamber). Exactly one party wins, so
      the YES prices of ALL of them sum to 1. Group SIZE IS DISCOVERED, never assumed: three races
      in the live universe are 3-way (Rhode Island Governor, Montana Senate, Nebraska Senate each
      list an Independent).
  (b) chamber-control identities — "Will the {Party} win the U.S. Senate/House?" against the state
      races that compose that chamber.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parsing import MAJOR_PARTIES, ParsedMarket, RaceKey, parse_markets


@dataclass(frozen=True, slots=True)
class RaceGroup:
    """One contest and every listed outcome of it.

    `is_exhaustive` is the single most important field in this module. It answers: "do the markets
    in this group cover the whole outcome space?" ONLY an exhaustive group can carry a hard arb,
    because buy-all-legs pays exactly 1.0 if and only if some leg must win.
    """

    race: RaceKey
    members: tuple[ParsedMarket, ...]

    @property
    def parties(self) -> frozenset[str]:
        return frozenset(m.party for m in self.members)

    @property
    def size(self) -> int:
        """DISCOVERED from the data — 2 for a straight D/R race, 3 where an Independent is listed."""
        return len(self.members)

    @property
    def is_exhaustive(self) -> bool:
        """True when both major parties are present.

        Rationale: a group missing a major party cannot cover the outcome space, so its legs do not
        sum to 1 and no basket over them is riskless. Note what this does NOT claim — that SIG has
        listed every real-world candidate. A race genuinely contested by an unlisted third candidate
        would still look 'exhaustive' here, which is why hard_arbs.py applies a second, independent
        missing-outcome check against the prices themselves.
        """
        return MAJOR_PARTIES <= self.parties

    @property
    def duplicate_parties(self) -> bool:
        """Two markets claiming the same party in one race. Double-counts an outcome and inflates the
        basket, so it disqualifies the group rather than being silently de-duplicated."""
        return len(self.parties) != len(self.members)


@dataclass(frozen=True, slots=True)
class ControlRelation:
    """A chamber-control market and the component races of that chamber.

    Held SEPARATELY from same-race groups and never hard-arbed. Chamber control is not a
    complementary pair: a 50-50 split, or an independent caucusing either way, can mean neither
    party "wins", so buy-both on D-control + R-control is NOT provably worth 1.0.
    """

    chamber: str
    party: str
    control_market: ParsedMarket
    components: tuple[ParsedMarket, ...]


@dataclass(slots=True)
class RelationshipGraph:
    race_groups: dict[RaceKey, RaceGroup] = field(default_factory=dict)
    control_relations: tuple[ControlRelation, ...] = ()
    unparsed: tuple[dict, ...] = ()
    #: groups rejected for hard-arb use, with the reason — surfaced, not swallowed
    rejected: tuple[tuple[RaceKey, str], ...] = ()

    # -- convenience views -------------------------------------------------- #
    @property
    def arbitrable_groups(self) -> tuple[RaceGroup, ...]:
        """Groups eligible for a hard-arb test: a real contest, exhaustive, no duplicate outcome."""
        return tuple(
            g
            for g in self.race_groups.values()
            if not g.race.is_control and g.is_exhaustive and not g.duplicate_parties and g.size >= 2
        )

    def group_sizes(self) -> dict[int, int]:
        """{group size: count} — a cheap sanity read of how many 3-way races were discovered."""
        out: dict[int, int] = {}
        for g in self.race_groups.values():
            if not g.race.is_control:
                out[g.size] = out.get(g.size, 0) + 1
        return out

    def exchange_index(self) -> dict[object, ParsedMarket]:
        out: dict[object, ParsedMarket] = {}
        for g in self.race_groups.values():
            for m in g.members:
                out[m.exchange_id] = out[str(m.exchange_id)] = m
        return out


def build_relationship_graph(markets: list[dict]) -> RelationshipGraph:
    """Group a raw /markets payload into same-race sets and chamber-control identities.

    Pure: no network, no broker, no mutation of the input.
    """
    parsed, unparsed = parse_markets(markets)

    by_race: dict[RaceKey, list[ParsedMarket]] = {}
    for pm in parsed:
        by_race.setdefault(pm.race, []).append(pm)

    race_groups: dict[RaceKey, RaceGroup] = {}
    for race, members in by_race.items():
        # deterministic ordering so emitted legs are stable across runs
        ordered = tuple(sorted(members, key=lambda m: (m.party, str(m.exchange_id))))
        race_groups[race] = RaceGroup(race=race, members=ordered)

    # (b) control identities: a control market's components are the state races of the same chamber.
    controls: list[ControlRelation] = []
    for race, group in race_groups.items():
        if not race.is_control:
            continue
        components = tuple(
            m
            for other, g in race_groups.items()
            if (not other.is_control) and other.chamber == race.chamber
            for m in g.members
        )
        for cm in group.members:
            same_party = tuple(c for c in components if c.party == cm.party)
            controls.append(
                ControlRelation(
                    chamber=race.chamber,
                    party=cm.party,
                    control_market=cm,
                    components=same_party,
                )
            )

    rejected: list[tuple[RaceKey, str]] = []
    for race, g in race_groups.items():
        if race.is_control:
            rejected.append((race, "chamber-control market: outcome space not provably closed"))
        elif g.duplicate_parties:
            rejected.append((race, f"duplicate party markets in one race: {sorted(g.parties)}"))
        elif g.size < 2:
            rejected.append((race, f"only {g.size} outcome listed — cannot form a complementary set"))
        elif not g.is_exhaustive:
            missing = sorted(MAJOR_PARTIES - g.parties)
            rejected.append((race, f"not exhaustive — missing major party {missing}"))

    return RelationshipGraph(
        race_groups=race_groups,
        control_relations=tuple(controls),
        unparsed=tuple(unparsed),
        rejected=tuple(rejected),
    )
