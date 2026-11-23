"""Title -> RaceKey parsing. This module is where the engine's correctness lives.

Every SIG market in this tournament is a standalone YES binary titled

    "Will the {Party} Party win the {Region} {Chamber}?"          (Governor / Senate)
    "Will the {Party} Party win the {XX-NN} House race?"          (House district)
    "Will the {Party} Party win the U.S. {Chamber}?"              (chamber control)

`/markets` carries NO state or party field (keys are id/title/thumbnailUrl/status/createdAt/
settlementDate/settledWith/settledOn/categories/isComposite/isMultiOutcome/exchanges/creator/
contexts), so the race identity has to come from the title. Two traps make the *method* of parsing
load-bearing rather than cosmetic:

TRAP 1 — SUBSTRING STATE MATCHING MERGES DISTINCT RACES.
    "West Virginia" CONTAINS "Virginia". Any parser that asks "which known state name appears in
    this title?" puts

        Will the Democratic Party win the Virginia Senate?
        Will the Democratic Party win the West Virginia Senate?
        Will the Republican Party win the Virginia Senate?
        Will the Republican Party win the West Virginia Senate?

    into ONE group of four, whose YES prices sum to ~2.0, and reports a fictitious ~1.0 "sell-all"
    arb on two unrelated races. MARKET_RECON.md §6 records exactly this mislabel in the recon
    report's own state column ("West Virginia is mislabeled state Virginia"), so the failure mode is
    observed, not hypothetical.

    DEFENCE: we never search for a state inside the title. We take the ENTIRE remainder between
    "win the " and the trailing chamber token as the region, so the region is "west virginia" or
    "virginia" exactly, and the two can never collide. There is no list of state names in this file
    at all — nothing to substring-match against.

TRAP 2 — CHAMBER-CONTROL MARKETS ARE NOT STATE RACES.
    "Will the Democratic Party win the U.S. Senate?" parses structurally like a state race with
    region "U.S.". Grouped with the state Senate races it would corrupt a real race group; treated as
    its own 2-way complementary pair it would invite a HARD arb claim on a market whose outcome space
    is not provably closed (a 50-50 chamber split, or independents caucusing either way, can mean
    neither party "wins"). We therefore tag control markets and keep them out of hard-arb groups
    entirely — see graph.py and hard_arbs.py.

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "Will the X win the Y?" — both halves captured non-greedily, trailing "?" optional so a title
# stored without it still parses. `re.I` because casing is not guaranteed stable across a feed.
_TITLE_RE = re.compile(r"^\s*will\s+the\s+(?P<party>.+?)\s+win\s+the\s+(?P<subject>.+?)\s*\??\s*$", re.I)

# Chamber suffixes, longest first: " House race" must be tried before " House" or a district title
# would yield region "AL-02 House" with chamber "race".
_CHAMBER_SUFFIXES: tuple[tuple[str, str], ...] = (
    (" house race", "House"),
    (" governor", "Governor"),
    (" senate", "Senate"),
    (" house", "House"),
)

# Region spellings that mean "the whole chamber", i.e. a control market rather than a state race.
_CONTROL_REGIONS = frozenset({"us", "usa", "united states", "national"})

MAJOR_PARTIES = frozenset({"Democratic", "Republican"})


def normalise_party(raw: str) -> str:
    """'Democratic Party' / 'democratic' -> 'Democratic'. Unknown parties pass through title-cased
    rather than being dropped: an unrecognised party is still an OUTCOME of the race, and silently
    discarding it is precisely how a 3-way becomes a phantom 2-way."""
    s = re.sub(r"\s+", " ", (raw or "")).strip()
    s = re.sub(r"\s+party$", "", s, flags=re.I).strip()
    if not s:
        return ""
    low = s.casefold()
    for known in ("democratic", "republican", "independent", "libertarian", "green"):
        if low == known:
            return known.capitalize()
    return s.title()


def normalise_region(raw: str) -> str:
    """Canonical region key. Periods and case are stripped so 'U.S.' == 'us', but the string is
    otherwise preserved WHOLE — 'west virginia' never reduces to 'virginia'."""
    s = re.sub(r"\s+", " ", (raw or "")).strip().casefold()
    return s.replace(".", "").strip()


@dataclass(frozen=True, slots=True)
class RaceKey:
    """Identity of one contest. `region` is already normalised; `is_control` separates
    chamber-control markets from state races so they can never share a group."""

    region: str
    chamber: str
    is_control: bool

    def __str__(self) -> str:
        scope = "CONTROL" if self.is_control else self.region
        return f"{scope}|{self.chamber}"


@dataclass(frozen=True, slots=True)
class ParsedMarket:
    market_id: object
    exchange_id: object
    title: str
    party: str
    race: RaceKey


def parse_title(title: str) -> tuple[str, RaceKey] | None:
    """('Democratic', RaceKey('alabama','Governor',False)) — or None if the title is not a
    party-win market. Returning None (rather than guessing) keeps unparseable titles OUT of every
    group: a market we cannot identify must never be treated as covering an outcome."""
    m = _TITLE_RE.match(title or "")
    if not m:
        return None
    party = normalise_party(m.group("party"))
    if not party:
        return None
    subject = re.sub(r"\s+", " ", m.group("subject")).strip()
    low = subject.casefold()
    for suffix, chamber in _CHAMBER_SUFFIXES:
        if low.endswith(suffix):
            region_raw = subject[: len(subject) - len(suffix)]
            region = normalise_region(region_raw)
            if not region:
                return None
            return party, RaceKey(region, chamber, region in _CONTROL_REGIONS)
    return None


def _exchange_id_of(market: dict) -> object | None:
    """Single YES exchange per market in this tournament (recon §2: 237 markets / 237 exchanges).
    Accepts the id under either spelling and tolerates a bare id instead of a dict."""
    exchanges = market.get("exchanges") or []
    if isinstance(exchanges, dict):
        exchanges = [exchanges]
    for ex in exchanges:
        if isinstance(ex, dict):
            for key in ("id", "exchangeId", "exchange_id"):
                if ex.get(key) is not None:
                    return ex[key]
        elif ex is not None:
            return ex
    for key in ("exchangeId", "exchange_id"):
        if market.get(key) is not None:
            return market[key]
    return None


def parse_markets(markets: list[dict]) -> tuple[list[ParsedMarket], list[dict]]:
    """Split a raw /markets payload into parsed party-win markets and unparsed leftovers.

    The leftovers are RETURNED rather than swallowed so a caller can see what the engine declined to
    reason about — an unexpected title shape is a signal to re-read the feed, not a thing to hide.
    """
    parsed: list[ParsedMarket] = []
    unparsed: list[dict] = []
    for mkt in markets or []:
        if not isinstance(mkt, dict):
            continue
        got = parse_title(mkt.get("title", ""))
        exchange_id = _exchange_id_of(mkt)
        if got is None or exchange_id is None:
            unparsed.append(mkt)
            continue
        party, race = got
        parsed.append(
            ParsedMarket(
                market_id=mkt.get("id"),
                exchange_id=exchange_id,
                title=mkt.get("title", ""),
                party=party,
                race=race,
            )
        )
    return parsed, unparsed
