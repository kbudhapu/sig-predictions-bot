"""Build signals/kalshi_map.yaml: SIG D/R race legs -> Kalshi party markets, VERIFIED (2026-10-07).

Kalshi lists 2026 races as party-resolved markets:
  Senate    SENATE{ST}-26-{D|R}  (special elections: SENATE{ST}S-26-...)
  Governor  GOVPARTY{ST}-26-{D|R}   (some states: KXGOV{ST}-26-...)
  House     KXHOUSERACE-{ST}{DD|AL}-26-{D|R}
A leg is accepted only if three checks agree: the event matches state+office, the market subtitle names the party
("Democratic party:: ..."), and the market rules name the same party. Anything else (no event, both a regular and a
special Senate race, party mismatch) is recorded as unmatched with the reason — never guessed.
Run locally (Kalshi is reachable):  python tools/build_kalshi_map.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request

import yaml

API = "https://api.elections.kalshi.com/trade-api/v2"
ST = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
      "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
      "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
      "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
      "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
      "new_hampshire": "NH", "new_jersey": "NJ", "new_mexico": "NM", "new_york": "NY", "north_carolina": "NC",
      "north_dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
      "rhode_island": "RI", "south_carolina": "SC", "south_dakota": "SD", "tennessee": "TN", "texas": "TX",
      "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west_virginia": "WV",
      "wisconsin": "WI", "wyoming": "WY"}
PARTY = {"dem": "Democratic", "rep": "Republican"}


def sig_race(key: str):
    """'texas_senate_dem' -> ('senate', 'TX', None, 'dem'); 'pa_07_house_rep' -> ('house', 'PA', '07', 'rep')."""
    if not key.endswith(("_dem", "_rep")) or "control" in key:
        return None
    base, party = key.rsplit("_", 1)
    m = re.match(r"(.+)_(senate|governor)$", base)
    if m and m.group(1) in ST:
        return (m.group(2), ST[m.group(1)], None, party)
    h = re.match(r"([a-z]{2})_(\d{2}|al)_house$", base)
    if h:
        return ("house", h.group(1).upper(), h.group(2).upper(), party)
    return None


def candidate_events(office: str, st: str, dist: str | None, events: dict) -> list[str]:
    if office == "senate":
        names = [f"SENATE{st}-26", f"SENATE{st}S-26"]
    elif office == "governor":
        names = [f"GOVPARTY{st}-26", f"KXGOV{st}-26"]
    else:
        names = [f"KXHOUSERACE-{st}{dist}-26"]
    return [n for n in names if n in events]


def match(key: str, events: dict) -> dict:
    """events: {event_ticker: event dict with nested markets}. Returns a mapping record (matched or not, with reason)."""
    r = sig_race(key)
    if r is None:
        return {"sig_key": key, "status": "unparsed"}
    office, st, dist, party = r
    evs = candidate_events(office, st, dist, events)
    if not evs:
        return {"sig_key": key, "status": "no_kalshi_event"}
    if len(evs) > 1:
        return {"sig_key": key, "status": "ambiguous_events", "events": evs}
    word = PARTY[party]
    other = PARTY["rep" if party == "dem" else "dem"]
    suffix = {"dem": ("-D", "-DEM"), "rep": ("-R", "-REP", "-GOP")}
    hits = []
    for m in events[evs[0]].get("markets", []):
        sub = (m.get("subtitle") or "").lower()
        rules = (m.get("rules_primary") or "").lower()
        tick = m.get("ticker") or ""
        if f"{word.lower()} party" not in rules or f"{other.lower()} party" in rules:
            continue                                  # the rules (what Kalshi settles on) must name our party only
        if other.lower() in sub:
            continue                                  # subtitle must not name the other party
        other_suffix = suffix["rep" if party == "dem" else "dem"]
        if tick.upper().endswith(other_suffix):
            continue                                  # ticker suffix must not contradict
        hits.append(m)
    if len(hits) != 1:
        return {"sig_key": key, "status": f"party_markets_{len(hits)}", "event": evs[0]}
    m = hits[0]
    return {"sig_key": key, "status": "matched", "event": evs[0], "ticker": m["ticker"], "party": party,
            "candidate": m.get("yes_sub_title"), "rules": (m.get("rules_primary") or "")[:140]}


def _get(path: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "sig-bot research"}),
                                timeout=40) as r:
        return json.loads(r.read())


def main() -> None:
    events, cur = {}, ""
    while True:
        d = _get("/events?status=open&limit=200&with_nested_markets=true" + (f"&cursor={cur}" if cur else ""))
        for e in d.get("events", []):
            events[e["event_ticker"]] = e
        cur = d.get("cursor")
        if not cur:
            break
    races = yaml.safe_load(open("signals/race_mapping.yaml", encoding="utf-8"))["races"]
    out, misses = [], []
    for key, r in races.items():
        ex = (r.get("sig") or {}).get("exchange_id")
        rec = match(key, events)
        if ex is None or rec["status"] == "unparsed":
            continue
        rec["exchange_id"] = str(ex)
        (out if rec["status"] == "matched" else misses).append(rec)
    yaml.safe_dump({"generated": "tools/build_kalshi_map.py", "legs": out, "unmatched": misses},
                   open("signals/kalshi_map.yaml", "w", encoding="utf-8"), sort_keys=False, width=200)
    by = {}
    for m in misses:
        by[m["status"]] = by.get(m["status"], 0) + 1
    print(f"matched {len(out)} SIG legs to Kalshi; unmatched {len(misses)} {by}")


if __name__ == "__main__":
    sys.exit(main())
