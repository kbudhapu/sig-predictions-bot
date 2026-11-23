"""FULL Polymarket coverage matcher: SIG races <-> Polymarket "<Place> <Office> Election Winner" events
for SENATE, GOVERNOR, and HOUSE. Each Polymarket event carries per-party sub-markets
("Will the Democrats/Republicans win the <place> <office> race in 2026?") with Yes/No prices — the SAME
structure the governor matcher used, so this just generalizes the office filter and enumerates via the
SEARCH endpoint (the /events feed only returns a ~100 featured subset, which is why senate/house were
missed before). READ-ONLY: prints coverage + divergences and writes PROPOSED race_mapping entries to a
file for review (never auto-edits the live mapping).

Run on the droplet:
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/poly_coverage.py
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from sig.sig_client import SigClient

GAMMA = "https://gamma-api.polymarket.com"
OFFICES = ("senate", "governor", "house")
PARTY = re.compile(r"will the (democratic|republican) party win the (.+)", re.I)
# Polymarket sub-market party phrasing varies by office: senate/gov say "Will the Democrats/Republicans
# win the X race"; HOUSE says "Will the Republican Party win the OR-05 House seat". Detect party by
# substring (robust to both) and ignore candidate-name / "another party" legs.


def _poly_party(q: str) -> str | None:
    ql = q.lower()
    if "democrat" in ql:
        return "D"
    if "republican" in ql:
        return "R"
    return None


def _retry(fn, n=10):
    for i in range(n):
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001
            if any(s in str(e) for s in ("429", "503", "SERVICE_UNAVAILABLE", "TX_CONFLICT")) and i < n - 1:
                time.sleep(5)
                continue
            raise


def _loc(office: str, text: str) -> str | None:
    """Normalize '<place> <office> ...' -> a '<office>:<place>' key. Place is the state or district
    token (e.g. 'maine', 'or-05'); office words and filler are stripped."""
    t = text.lower()
    if office not in t:
        return None
    place = t.split(office)[0]
    place = re.sub(r"\b(the|party|win|race|election|winner|2026|will|gubernatorial)\b", " ", place)
    place = re.sub(r"[^a-z0-9\- ]", " ", place)
    place = re.sub(r"\s+", " ", place).strip()
    return f"{office}:{place}" if place else None


async def _poly_office(c: httpx.AsyncClient, office: str) -> dict[str, dict]:
    """key '<office>:<place>' -> {R:(mid,token), D:(mid,token)} from Polymarket winner events."""
    q = "Governor Election Winner" if office == "governor" else f"{office.title()} Election Winner"
    s = (await c.get(f"{GAMMA}/public-search", params={"q": q, "limit_per_type": 200})).json()
    ev = s.get("events", []) if isinstance(s, dict) else (s or [])
    out: dict[str, dict] = {}
    for e in ev:
        title = e.get("title") or ""
        if "election winner" not in title.lower() or office not in title.lower():
            continue
        key = _loc(office, title)
        if not key:
            continue
        legs: dict[str, tuple] = {}
        for sm in (e.get("markets") or []):
            party = _poly_party(sm.get("question") or "")
            if party is None:
                continue
            pr = sm.get("outcomePrices")
            if not pr:
                continue
            try:
                yes = float(json.loads(pr)[0] if isinstance(pr, str) else pr[0])
            except Exception:                        # noqa: BLE001
                continue
            toks = sm.get("clobTokenIds")
            tok = (json.loads(toks)[0] if isinstance(toks, str) else (toks or [None])[0])
            legs[party] = (yes, tok, str(sm.get("id")))     # (yes_price, yes_token_id, poly_market_id)
        if "R" in legs and "D" in legs:
            out[key] = legs
    return out


def _sig_races(sig: SigClient):
    """key '<office>:<place>' -> {R:exch, D:exch} from SIG titles, + mid prices per exch."""
    races: dict[str, dict] = {}
    ids: list[str] = []
    for m in _retry(lambda: sig.markets_all()):
        if not isinstance(m, dict) or m.get("status") != "open":
            continue
        mm = PARTY.search(m.get("question") or m.get("title") or "")
        exs = m.get("exchanges") or []
        if not mm or not exs:
            continue
        rest = mm.group(2).lower()
        office = next((o for o in OFFICES if o in rest), None)
        if office is None:
            continue
        key = _loc(office, rest)
        if not key:
            continue
        party = "D" if mm.group(1).lower().startswith("dem") else "R"
        ex = str(exs[0].get("id"))
        races.setdefault(key, {})[party] = (ex, str(m.get("id")))   # (exchange_id, sig_market_id)
        ids.append(ex)
    px: dict[str, float] = {}
    for i in range(0, len(ids), 90):
        for r in _retry(lambda: sig.exchange_prices(ids[i:i + 90])).get("data", []):
            b, a = r.get("bestBid"), r.get("bestAsk")
            if b is not None and a is not None:
                px[str(r.get("exchangeId"))] = (float(b) + float(a)) / 2.0
    return races, px


async def main():
    sig = SigClient(key_env="SIG_API_KEY_READ", timeout=30.0)
    sig_races, sig_px = _sig_races(sig)
    by_office = {}
    for o in OFFICES:
        by_office[o] = sum(1 for k in sig_races if k.startswith(o + ":"))
    print("SIG races parsed by office:", by_office, "| total", len(sig_races))

    async with httpx.AsyncClient(timeout=30.0) as c:
        poly = {}
        for o in OFFICES:
            try:
                po = await _poly_office(c, o)
            except Exception as e:                   # noqa: BLE001
                print("poly", o, "ERR", str(e)[:80]); po = {}
            poly.update(po)
            print(f"Polymarket {o} winner markets: {len(po)}")

    matched = 0
    proposed = []
    for key, sg in sorted(sig_races.items()):
        pg = poly.get(key)
        if not pg or "R" not in sg or "D" not in sg:
            continue
        sr, sd = sig_px.get(sg["R"][0]), sig_px.get(sg["D"][0])
        if sr is None or sd is None:
            continue
        matched += 1
        compl_s, compl_p = sr + sd, pg["R"][0] + pg["D"][0]
        ok = abs(compl_s - 1) < 0.08 and abs(compl_p - 1) < 0.08
        divR = sr - pg["R"][0]
        flag = "" if ok else "  <<CHECK compl S=%.2f P=%.2f" % (compl_s, compl_p)
        print("%-26s SIGr=%.3f POLYr=%.3f divR=%+.3f%s" % (key, sr, pg["R"][0], divR, flag))
        if ok:
            proposed.append((key, sg, pg))
    print(f"\nMATCHED {matched} races; {len(proposed)} polarity-clean (compl~1.0) and mapping-ready.")

    # emit READY-TO-PASTE race_mapping.yaml entries (rep + dem per race, polarity=direct)
    def _name(key):  # 'senate:new hampshire' -> 'new_hampshire_senate'; 'house:or-05' -> 'or_05_house'
        office, place = key.split(":", 1)
        return re.sub(r"[^a-z0-9]+", "_", place).strip("_") + "_" + office

    out = "data/poly_coverage_proposed.yaml"
    with open(out, "w") as fh:
        for key, sg, pg in proposed:
            base = _name(key)
            for party, sigleg, polyleg in (("rep", sg["R"], pg["R"]), ("dem", sg["D"], pg["D"])):
                exch, sig_mkt = sigleg
                _yes, tok, poly_mkt = polyleg
                fh.write(
                    f"{base}_{party}:\n"
                    f'  description: "{party.upper()} wins {key} (2026); auto-matched via poly_coverage."\n'
                    f"  resolution_match: true\n"
                    f"  confidence: high\n"
                    f"  sig: {{market_id: {sig_mkt}, exchange_id: {exch}}}\n"
                    f"  polymarket:\n"
                    f'    market_id: "{poly_mkt}"\n'
                    f'    yes_token_id: "{tok}"\n'
                    f'    yes_outcome: "Yes"\n'
                    f"    polarity: direct\n"
                    f"    liquidity_ok: true\n")
    print(f"{len(proposed)*2} YAML entries ({len(proposed)} races x rep/dem) written to {out}")


asyncio.run(main())
