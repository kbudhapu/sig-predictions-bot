"""GDELT per-race news-VOLUME trigger — the justified-vs-overdone discriminator (PART 2).

We do NOT read the content or direction of the news (that is the error-prone trap). We only ask, per
race: did news VOLUME spike here? GDELT DOC 2.0 `timelinevol` is free, needs no key, is reachable from
the droplet, and returns a volume-over-time series for an arbitrary query. So each of the 237 races maps
to a DISAMBIGUATED query — state + office + the candidate names (e.g. `("Georgia Senate" OR "Jon Ossoff"
OR "Mike Collins") (election OR race OR campaign)`, never bare "Georgia") — and a recent spike in that
series means "news hit this race, go check SIG." PART 3 combines: SIG abnormal move + news spike ⇒
JUSTIFIED (follow); move + no spike ⇒ OVERDONE (fade).

RATE LIMIT. GDELT asks for ≤ 1 request / 5s, so a full 237-race sweep is ~20 min — a periodic (hourly)
cadence, cached. The pacer (`sleep`) and the fetch are injected so tests run instantly + offline.
READ-ONLY.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_RATE_LIMIT_S = 5.0
USER_AGENT = "sig-bot/0.1 research-signal (+https://github.com/kbudhapu/sig-bot; prediction-cup)"

_OFFICE_WORD = {"senate": "Senate", "governor": "Governor", "house": "House"}


@dataclass(frozen=True)
class RaceQuery:
    race_key: str                        # "senate:georgia"
    state: str                           # normalized, e.g. "georgia"
    office: str                          # "senate" | "governor" | "house"
    query: str                           # the disambiguated GDELT query string


@dataclass(frozen=True)
class NewsVolume:
    race_key: str
    latest: float                        # most-recent (complete) volume bucket
    baseline: float                      # mean of the prior buckets
    ratio: float                         # latest / baseline
    spike: bool                          # latest ≥ mult × baseline (news hit this race)
    n_points: int
    timestamp: str = ""
    source: str = "gdelt_timelinevol"


def _title(state: str) -> str:
    return " ".join(w.capitalize() for w in state.replace("_", " ").split())


def build_query(state: str, office: str, candidates: list[str] | None = None) -> str:
    """Disambiguated GDELT query: the state+office phrase OR each candidate name, gated by election
    context so unrelated "{State} {Office}" chatter (e.g. a sports "state senate") is filtered out."""
    terms = [f'"{_title(state)} {_OFFICE_WORD.get(office, office.capitalize())}"']
    for c in (candidates or []):
        c = c.strip()
        if c:
            terms.append(f'"{c}"')
    return "(" + " OR ".join(terms) + ") (election OR race OR campaign OR Senate OR Governor)"


def _candidates_from_race(rm: Any) -> list[str]:
    """Pull candidate names out of a RaceMapping's externals (Kalshi `yes_outcome` carries them as e.g.
    'Republican (Mike Collins)' / 'Democratic (Jon Ossoff)')."""
    import re
    names: list[str] = []
    for e in (getattr(rm, "externals", {}) or {}).values():
        yo = getattr(e, "yes_outcome", "") or ""
        m = re.search(r"\(([^)]+)\)", yo)
        if m:
            names.append(m.group(1).strip())
    return names


_RACE_KEY_RE = __import__("re").compile(r"^(?P<state>[a-z_]+?)_(?P<office>senate|governor|house)_(?:rep|dem)$")


def queries_from_race_mapping(race_mapping: dict) -> list[RaceQuery]:
    """Build one GDELT query per (office, state) race from a loaded race_mapping, de-duplicating the
    per-party entries and merging their candidate names. Extend the mapping → full 237 as the SIG board
    + candidate rosters are loaded; the query builder is the stable part."""
    import re
    by_race: dict[tuple[str, str], list[str]] = {}
    for key, rm in race_mapping.items():
        m = _RACE_KEY_RE.match(key)
        if not m:
            continue
        rk = (m.group("office"), m.group("state"))
        by_race.setdefault(rk, [])
        for c in _candidates_from_race(rm):
            if c not in by_race[rk]:
                by_race[rk].append(c)
    out: list[RaceQuery] = []
    for (office, state), cands in sorted(by_race.items()):
        out.append(RaceQuery(race_key=f"{office}:{state}", state=state, office=office,
                             query=build_query(state, office, cands)))
    return out


def parse_timelinevol(resp: dict | None) -> list[tuple[str, float]]:
    """GDELT timelinevol JSON → [(date, value)] oldest→newest."""
    if not isinstance(resp, dict):
        return []
    tl = resp.get("timeline") or []
    data = (tl[0].get("data") if tl and isinstance(tl[0], dict) else None) or []
    out: list[tuple[str, float]] = []
    for p in data:
        if isinstance(p, dict):
            try:
                out.append((str(p.get("date", "")), float(p.get("value", 0) or 0)))
            except (TypeError, ValueError):
                continue
    return out


def detect_news_spike(
    race_key: str, series: list[tuple[str, float]], *, mult: float = 3.0, min_points: int = 4
) -> NewsVolume:
    """Is the most-recent COMPLETE volume bucket abnormally high vs the race's own baseline?

    Trailing zero buckets (an incomplete current day) are dropped so a partial bucket is not read as
    'news went quiet'. `latest` is then the last non-zero bucket; `baseline` the mean of the rest. Spike
    iff latest ≥ mult × baseline (and we have ≥ min_points) — a VOLUME spike, nothing about direction.
    """
    pts = [v for _, v in series]
    while pts and pts[-1] == 0.0:                         # drop incomplete trailing bucket(s)
        pts = pts[:-1]
    if len(pts) < min_points:
        return NewsVolume(race_key, 0.0, 0.0, 0.0, False, len(pts),
                          timestamp=(series[-1][0] if series else ""))
    latest = pts[-1]
    prior = pts[:-1]
    baseline = sum(prior) / len(prior) if prior else 0.0
    ratio = (latest / baseline) if baseline > 1e-12 else (latest and float("inf") or 0.0)
    spike = baseline > 1e-12 and latest >= mult * baseline
    return NewsVolume(race_key, round(latest, 6), round(baseline, 6),
                      (round(ratio, 4) if ratio != float("inf") else ratio), bool(spike),
                      len(pts), timestamp=(series[-1][0] if series else ""))


def _default_fetch_timelinevol(query: str, *, timespan: str = "2w", timeout: float = 25.0) -> dict:
    """GET GDELT timelinevol JSON for a query. urllib only; READ-ONLY."""
    import json
    import urllib.request
    url = (f"{GDELT_DOC}?query={urllib.parse.quote(query)}"
           f"&mode=timelinevol&format=json&timespan={timespan}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed https host
        return json.loads(r.read().decode("utf-8", "replace"))


def sweep_news_volume(
    race_queries: list[RaceQuery],
    fetch_timelinevol: Callable[[str], Any] = _default_fetch_timelinevol,
    *,
    mult: float = 3.0,
    rate_limit_s: float = GDELT_RATE_LIMIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, NewsVolume]:
    """Sweep every race's GDELT query at ≤ 1 req / `rate_limit_s`, returning {race_key: NewsVolume}.

    The pacer (`sleep`) + `fetch_timelinevol` are injected, so a test passes `sleep=lambda *_: None` and
    a canned fetch to run instantly offline. One query's error is skipped, never aborts the sweep.
    """
    out: dict[str, NewsVolume] = {}
    for i, rq in enumerate(race_queries):
        if i > 0 and rate_limit_s > 0:
            sleep(rate_limit_s)                          # respect GDELT's 1-req/5s ask
        try:
            series = parse_timelinevol(fetch_timelinevol(rq.query))
        except Exception:  # noqa: BLE001
            continue
        out[rq.race_key] = detect_news_spike(rq.race_key, series, mult=mult)
    return out
