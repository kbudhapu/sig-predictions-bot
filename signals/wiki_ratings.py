"""Wikipedia race-ratings scraper → a COARSE, rating-derived benchmark ACROSS THE BOARD.

WHY THIS EXISTS. The real-money markets (Polymarket) only price ~15 marquee 2026 races; the other ~200
SIG markets — individual Senate seats, most governors, and the obscure House districts where SIG's
students are sloppiest — have no market comparable. The professional raters (Cook, Sabato, Inside
Elections, RCP, DDHQ, Economist, Fox, …) rate EVERY competitive race, but their sites 403 a datacenter
IP. Wikipedia's 2026 election pages AGGREGATE those ratings in clean tables and are freely reachable via
the action API — so we scrape Wikipedia and derive a probability from the consensus rating.

QUALITY TIER — READ THIS. This benchmark is `rating_derived`: a fuzzy map from a categorical rating to a
probability, aggregated across raters. It is DELIBERATELY LOWER QUALITY than a real-money price and MUST
be tagged so (every estimate carries `confidence="rating_derived"`). The consuming strategy must size it
SMALLER than a Polymarket estimate and act only on LARGE divergences: a 5-point SIG-vs-rating gap is
benchmark noise; a 20-point gap on an obscure race is real edge. It is also NOT an election-night signal
— Wikipedia ratings refresh on a multi-day cadence (see `WIKI_STALE_SECONDS`), fine for a structural
edge, useless for live results.

PURITY / TESTABILITY. The parse + probability math are pure (wikitext in, estimates out). The only I/O is
`fetch_wikitext`, injected so tests run fully offline against a captured fixture (no network, same as
signals/poly_client.py's transport injection). READ-ONLY: this module only ever GETs the action API.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "sig-bot/0.1 research-signal (+https://github.com/kbudhapu/sig-bot; prediction-cup)"

CONFIDENCE = "rating_derived"          # the quality tag every estimate carries — coarse, size-it-smaller
WIKI_STALE_SECONDS = 6 * 3600.0        # ratings move on a multi-day cadence; 6h cache is plenty + safe

#: The 2026 election pages whose "race ratings" tables this module parses. House is added later (its
#: table uses a per-district format; Senate + Governors extract cleanly with the row parser below).
OFFICE_PAGES = {
    "senate": "2026_United_States_Senate_elections",
    "governor": "2026_United_States_gubernatorial_elections",
    # "house": "2026_United_States_House_of_Representatives_elections",   # TODO: per-district parser
}

#: Rating family → P(the FAVORED party wins). Tunable — calibrate against Polymarket on the overlap
#: (see `calibrate`). A "Lean R" ⇒ P(R)=table["lean"]; a "Lean D" ⇒ P(R)=1−table["lean"]; a Toss-up ⇒
#: 0.50 regardless of party. Keep SYMMETRIC around 0.50 so the R and D sides of a race sum to 1.
DEFAULT_RATING_TO_P: dict[str, float] = {
    "solid": 0.95,
    "safe": 0.95,
    "likely": 0.85,
    "lean": 0.65,
    "tilt": 0.55,
    "tossup": 0.50,
}

# Raters the Wikipedia tables tag per cell (HTML comments). We parse every one present; `raters=` on the
# scrape functions can restrict to a subset. Names are lower-cased on parse.
KNOWN_RATERS = ("cook", "ddhq", "economist", "fpo", "fox", "ie", "rcp", "sabato", "sb", "st")

_STATE_RE = re.compile(r"!\s*\[\[[^\]]*?\|([^\]|]+)\]\]")          # ! [[2026 … in Alabama|Alabama]]
_STATE_RE_BARE = re.compile(r"!\s*\[\[([^\]|]+)\]\]")              # ! [[Alabama]]
_CELL_RE = re.compile(
    r"<!--\s*([A-Za-z]+)\s*-->\s*\|\s*\{\{\s*USRaceRating\s*\|\s*([^}]+?)\s*\}\}", re.I
)


def _norm_state(s: str) -> str:
    """'North Carolina' → 'north_carolina'; strips footnote/ref cruft and the ' (special)' suffix kept
    separately so a special election does not collide with the regular seat."""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[\[|\]\]", "", s)
    s = s.split("|")[-1].strip().lower()
    s = re.sub(r"\(.*?\)", "", s).strip()
    return re.sub(r"\s+", "_", s)


def parse_rating_args(args: str) -> tuple[str, str | None]:
    """'Solid|R' → ('solid','r'); 'Tossup' → ('tossup', None); 'Lean|D|flip' → ('lean','d').

    The 3rd template arg ('flip'/'hold') is cosmetic (does the seat change party) and ignored for the
    probability. Returns (family, party|None); party is None for a toss-up (no favored side)."""
    parts = [p.strip().lower() for p in args.split("|") if p.strip()]
    if not parts:
        return ("", None)
    family = parts[0]
    party = parts[1] if len(parts) > 1 and parts[1] in ("r", "d") else None
    return (family, party)


def rating_to_p_rep(family: str, party: str | None, table: dict[str, float] | None = None) -> float | None:
    """P(Republican wins) implied by ONE rater's categorical rating. None if the family is unknown.

    Toss-up ⇒ 0.50. '(family, R)' ⇒ table[family]; '(family, D)' ⇒ 1 − table[family] (the mirror).
    """
    tbl = table or DEFAULT_RATING_TO_P
    fam = (family or "").strip().lower()
    if fam == "tossup" or fam == "toss-up":
        return 0.50
    base = tbl.get(fam)
    if base is None:
        return None
    if party == "r":
        return base
    if party == "d":
        return 1.0 - base
    return 0.50                                           # rated but no party given → treat as toss-up


@dataclass(frozen=True)
class RaceRating:
    """One race's raw per-rater ratings as scraped (before the probability map)."""
    office: str                                           # "senate" | "governor" | "house"
    state: str                                            # normalized, e.g. "georgia"
    raters: dict[str, tuple[str, str | None]]             # rater → (family, party|None)
    district: str | None = None                           # house only

    @property
    def key(self) -> str:
        base = f"{self.office}:{self.state}"
        return f"{base}:{self.district}" if self.district else base


@dataclass(frozen=True)
class RatingEstimate:
    """The consensus rating-derived benchmark for one race, TAGGED coarse.

    `p_rep` is the mean of the raters' implied P(R-wins); `p_dem = 1 − p_rep`. `confidence` is always
    `rating_derived` so a consumer sizes it smaller + trades only large divergences.
    """
    key: str
    office: str
    state: str
    p_rep: float
    p_dem: float
    n_raters: int
    raters: dict[str, str]                                # rater → "lean r" (audit trail)
    district: str | None = None
    confidence: str = CONFIDENCE
    source: str = "wikipedia_ratings"
    fetched_at: float = 0.0

    def is_stale(self, *, now: float | None = None, max_age_s: float = WIKI_STALE_SECONDS) -> bool:
        return ((now if now is not None else time.time()) - self.fetched_at) > max_age_s

    def conviction(self) -> float:
        """Benchmark conviction of this estimate: distance-from-0.5 of `p_rep`, discounted by the
        rating-derived QUALITY (~0.5). A coarse benchmark must therefore be MORE extreme to earn the
        same conviction as a sharp (Polymarket) price — exactly the 'trust it less' the quality tag
        encodes. Near a toss-up (p_rep ≈ 0.5) conviction ≈ 0: the raters don't know, so don't lean on it."""
        from signals.conviction import conviction as _conv
        return _conv(self.p_rep, "rating_derived")


def parse_ratings_wikitext(wikitext: str, office: str) -> list[RaceRating]:
    """Parse a 2026 election page's wikitext into one RaceRating per race.

    Robust by construction: it keys off the `{{USRaceRating|…}}` template cells (each tagged with an
    `<!--Rater-->` HTML comment) and the race's `! [[…|State]]` row header, so it ignores every other
    table on the page without needing to locate the ratings table by caption.
    """
    out: list[RaceRating] = []
    for block in re.split(r"\n\|-", wikitext):
        cells = _CELL_RE.findall(block)
        if not cells:
            continue
        m = _STATE_RE.search(block) or _STATE_RE_BARE.search(block)
        if not m:
            continue
        state = _norm_state(m.group(1))
        if not state:
            continue
        raters: dict[str, tuple[str, str | None]] = {}
        for rater, args in cells:
            raters[rater.strip().lower()] = parse_rating_args(args)
        if raters:
            out.append(RaceRating(office=office, state=state, raters=raters))
    return out


def estimate_from_rating(
    rr: RaceRating,
    *,
    table: dict[str, float] | None = None,
    raters: tuple[str, ...] | None = None,
    fetched_at: float = 0.0,
) -> RatingEstimate | None:
    """Aggregate a race's raters into a consensus RatingEstimate. None if no rater maps.

    `raters=` optionally restricts to a subset (e.g. just the headline four). Otherwise every rater
    whose family is known contributes equally to the mean implied P(R).
    """
    ps: list[float] = []
    audit: dict[str, str] = {}
    for rater, (family, party) in rr.raters.items():
        if raters is not None and rater not in raters:
            continue
        p = rating_to_p_rep(family, party, table)
        if p is None:
            continue
        ps.append(p)
        audit[rater] = f"{family}{(' ' + party) if party else ''}".strip()
    if not ps:
        return None
    p_rep = round(sum(ps) / len(ps), 4)
    return RatingEstimate(
        key=rr.key, office=rr.office, state=rr.state, district=rr.district,
        p_rep=p_rep, p_dem=round(1.0 - p_rep, 4), n_raters=len(ps), raters=audit,
        fetched_at=fetched_at,
    )


# ── live fetch (the only I/O; injected in tests) ──────────────────────────────────────────────── #
def _default_fetch_wikitext(page_title: str, *, timeout: float = 20.0) -> str:
    """GET the action API's wikitext for a page. urllib only — no new dependency, and the action API is
    a plain JSON GET. READ-ONLY."""
    import json
    import urllib.request

    url = (f"{WIKI_API}?action=parse&page={page_title}&prop=wikitext"
           f"&format=json&formatversion=2")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https host
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return str((data.get("parse") or {}).get("wikitext") or "")


def scrape_office(
    office: str,
    *,
    fetch_wikitext: Callable[[str], str] = _default_fetch_wikitext,
    table: dict[str, float] | None = None,
    raters: tuple[str, ...] | None = None,
    now: float | None = None,
) -> list[RatingEstimate]:
    """Scrape one office's page → consensus RatingEstimates. `fetch_wikitext` is injected in tests."""
    if office not in OFFICE_PAGES:
        raise ValueError(f"office {office!r} not in {sorted(OFFICE_PAGES)} (House parser is TODO)")
    stamp = now if now is not None else time.time()
    wikitext = fetch_wikitext(OFFICE_PAGES[office])
    estimates: list[RatingEstimate] = []
    for rr in parse_ratings_wikitext(wikitext, office):
        est = estimate_from_rating(rr, table=table, raters=raters, fetched_at=stamp)
        if est is not None:
            estimates.append(est)
    return estimates


# ── SIG market mapping ────────────────────────────────────────────────────────────────────────── #
#: Map a race key's office+state+party to its SIG market id. Built from race_mapping (whose keys are
#: like 'georgia_senate_rep'); extend the resolver with the full SIG board when it is loaded.
_RACE_KEY_RE = re.compile(r"^(?P<state>[a-z_]+?)_(?P<office>senate|governor|house)_(?P<party>rep|dem)$")


def sig_resolver_from_race_mapping(race_mapping: dict) -> dict[tuple[str, str], dict[str, str]]:
    """Return {(office, state): {'rep': sig_market_id, 'dem': sig_market_id}} from a loaded race_mapping.

    race_mapping keys look like 'georgia_senate_rep' / 'georgia_governor_rep'. We invert them to
    (office, state) → per-party SIG market id so a rating for 'senate:georgia' lands on the right SIG
    markets. Races the mapping does not yet carry simply resolve to nothing (dormant until added).
    """
    out: dict[tuple[str, str], dict[str, str]] = {}
    for key, rm in race_mapping.items():
        m = _RACE_KEY_RE.match(key)
        if not m or rm.sig_market_id is None:
            continue
        office = m.group("office")
        state = m.group("state")
        party = "rep" if m.group("party") == "rep" else "dem"
        out.setdefault((office, state), {})[party] = str(rm.sig_market_id)
    return out


def to_sig_estimates(
    estimates: list[RatingEstimate],
    resolver: dict[tuple[str, str], dict[str, str]],
) -> dict[str, dict]:
    """Project RatingEstimates onto SIG per-party markets.

    Returns {sig_market_id: {"fair": p, "confidence": "rating_derived", "source": …, "race": key,
    "n_raters": …}}. The Republican SIG market gets `p_rep`, the Democratic one `p_dem`. Only races the
    resolver knows a SIG id for are emitted (the rest stay available as RatingEstimates for later).
    """
    out: dict[str, dict] = {}
    for e in estimates:
        legs = resolver.get((e.office, e.state))
        if not legs:
            continue
        if legs.get("rep"):
            out[legs["rep"]] = {"fair": e.p_rep, "confidence": e.confidence, "source": e.source,
                                "race": e.key, "n_raters": e.n_raters, "fetched_at": e.fetched_at}
        if legs.get("dem"):
            out[legs["dem"]] = {"fair": e.p_dem, "confidence": e.confidence, "source": e.source,
                                "race": e.key, "n_raters": e.n_raters, "fetched_at": e.fetched_at}
    return out


# ── SIG seat-market ↔ Wikipedia rating auto-match (the ~71-race coverage bridge) ────────────────── #
#: SIG titles every state seat market as "Will the {Republican|Democratic} Party win the {State}
#: {Senate|Governor}?" — a clean, unambiguous (party, state, office) triple that maps 1:1 to a
#: RatingEstimate's (office, state) with DIRECT polarity (the SIG YES = that party winning = the party's
#: rating-implied win prob). The chamber-control markets ("…the U.S. Senate/House?") are EXCLUDED here —
#: those are the sharp Polymarket-covered races, not per-state seats, and "U.S." is not a rating state.
_SIG_SEAT_TITLE_RE = re.compile(
    r"^\s*will the (republican|democratic) party win the (.+?) (senate|governor)\s*\??\s*$", re.I)


def parse_sig_seat_title(title: str) -> tuple[str, str, str] | None:
    """'Will the Republican Party win the Rhode Island Senate?' → ('rep','rhode_island','senate').

    Returns (party, normalized_state, office) or None if the title is not a per-STATE Senate/Governor
    seat market. The U.S. chamber-control markets (state → 'u.s.') return None — they are the sharp
    Polymarket races, never a Wikipedia-rated state seat."""
    m = _SIG_SEAT_TITLE_RE.match(str(title or ""))
    if not m:
        return None
    party = "rep" if m.group(1).strip().lower().startswith("rep") else "dem"
    state = _norm_state(m.group(2))
    office = m.group(3).strip().lower()
    if not state or state.replace(".", "").replace("_", "") in ("us", "usa") or "u.s" in state:
        return None                                   # chamber control, not a state seat → leave to Polymarket
    return (party, state, office)


def _market_exchange_id(market: dict) -> str | None:
    """The tradeable SIG exchange id for a binary seat market. Each such market has exactly one exchange
    (the YES/NO book); defensively take the first exchange's id. None if the market carries no exchange."""
    exs = market.get("exchanges") or market.get("exchange") or []
    if isinstance(exs, dict):
        exs = [exs]
    for e in exs:
        if isinstance(e, dict):
            eid = e.get("id") or e.get("exchangeId") or e.get("exchange_id")
            if eid is not None:
                return str(eid)
    return None


def build_sig_exchange_benchmarks(
    sig_markets: list[dict], estimates: list[RatingEstimate],
) -> tuple[dict[str, dict], list[tuple[str, str]]]:
    """Match SIG per-state seat markets to Wikipedia RatingEstimates, by (party, state, office).

    Returns ({sig_exchange_id: {"fair","race","office","state","party","n_raters","source",
    "confidence"}}, unmatched) where `fair` is the SIG-aligned win probability for THAT market's party
    (Republican market → p_rep, Democratic → p_dem; DIRECT polarity, no flip) and `unmatched` lists the
    (office, state) seats SIG had a market for but Wikipedia had no rating (observability — never traded).
    Pure: no network. The consumer tags these `rating_derived` (coarse) so conviction weighting makes them
    trade only on LARGE gaps."""
    by_os: dict[tuple[str, str], RatingEstimate] = {(e.office, e.state): e for e in estimates}
    out: dict[str, dict] = {}
    unmatched: list[tuple[str, str]] = []
    for m in sig_markets:
        parsed = parse_sig_seat_title(str(m.get("title") or m.get("name") or ""))
        if parsed is None:
            continue
        party, state, office = parsed
        est = by_os.get((office, state))
        if est is None:
            unmatched.append((office, state))
            continue
        ex_id = _market_exchange_id(m)
        if not ex_id:
            continue
        fair = est.p_rep if party == "rep" else est.p_dem
        out[ex_id] = {
            "fair": round(float(fair), 4), "race": f"{est.key}:{party}",
            "office": office, "state": state, "party": party,
            "n_raters": est.n_raters, "source": est.source, "confidence": est.confidence,
        }
    return out, unmatched


# ── calibration against the real-money overlap (Polymarket) ─────────────────────────────────────── #
def calibrate(
    estimates: list[RatingEstimate],
    real_money_p_rep: dict[str, float],
    *,
    table: dict[str, float] | None = None,
) -> dict:
    """Compare the rating-derived P(R) to a real-money P(R) on the overlapping races.

    `real_money_p_rep` is {race_key: P(R)} from Polymarket for races both sources cover (e.g. the
    governor races Polymarket lists). Returns the mean absolute error and, per rating family, the
    real-money price observed when the consensus landed in that family's band — the numbers to tune
    DEFAULT_RATING_TO_P with ("if 'lean' races trade at 0.70 on Polymarket, map lean→0.70"). Pure.
    """
    tbl = table or DEFAULT_RATING_TO_P
    pairs = [(e, real_money_p_rep[e.key]) for e in estimates if e.key in real_money_p_rep]
    if not pairs:
        return {"n": 0, "mae": None, "by_band": {}, "note": "no Polymarket overlap"}
    errs = [abs(e.p_rep - rm) for e, rm in pairs]
    # bucket each overlap race into its rating family by DISTANCE FROM 0.5 (symmetric), so a
    # Dem-favored race (p_rep < 0.5) lands in its true family — e.g. a "Safe D" (0.05) groups with
    # "Safe R" (0.95), not lumped into the nearest R-side value. Record the real-money price per band.
    by_band: dict[str, list[float]] = {}
    for e, rm in pairs:
        fam = min(tbl, key=lambda f: abs(abs(tbl[f] - 0.5) - abs(e.p_rep - 0.5)))
        by_band.setdefault(fam, []).append(round(rm, 4))
    return {
        "n": len(pairs),
        "mae": round(sum(errs) / len(errs), 4),
        "by_band": {fam: {"real_money_mean": round(sum(v) / len(v), 4), "n": len(v), "samples": v}
                    for fam, v in by_band.items()},
        "overlap": [(e.key, round(e.p_rep, 4), round(rm, 4)) for e, rm in pairs],
    }
