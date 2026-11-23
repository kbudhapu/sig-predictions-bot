"""READ-ONLY market reconnaissance -> writes sig-bot/recon/MARKET_RECON.md + prints a summary. No strategy,
no trading logic. Uses the existing read client. Rate-respectful: batches prices via /exchanges/prices?ids,
samples orderbooks with pacing. (Tournament may still be 'draft' pre-open — books can be sparse then.)"""
from __future__ import annotations

import sys, os, time, re, json, statistics
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sig.sig_client import SigClient, SigAPIError

OUT = Path(__file__).resolve().parent.parent / "recon" / "MARKET_RECON.md"
OUT.parent.mkdir(parents=True, exist_ok=True)

STATES = ["Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida",
 "Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland",
 "Massachusetts","Michigan","Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire",
 "New Jersey","New Mexico","New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania",
 "Rhode Island","South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington",
 "West Virginia","Wisconsin","Wyoming"]


def categorize(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("control", "majority", "balance of power", "which party", "net gain", "net seats", "pick up", "flip")):
        return "control/balance-of-power"
    if "senate" in t: return "Senate"
    if "house" in t or "congression" in t or "district" in t: return "House"
    if "governor" in t or "gubernatorial" in t: return "Governor"
    if any(k in t for k in ("ballot", "measure", "proposition", "amendment", "referendum")): return "ballot measure"
    return "other"


def state_of(title: str) -> str:
    for s in STATES:
        if s.lower() in title.lower():
            return s
    return "-"


def w(md: list, s: str = ""): md.append(s)


def main() -> int:
    c = SigClient()
    md = []
    now = datetime.now(timezone.utc)
    w(md, f"# SIG Predictions Cup — Market Reconnaissance"); w(md)
    w(md, f"_Generated {now.isoformat()} (read-only; existing client). Tournament window 2026-10-01T16:00Z – 2026-11-04T17:00Z._"); w(md)

    # tournament meta
    try:
        tour = c.get("/tournaments").get("data", [{}])[0]
        w(md, f"**Tournament:** {tour.get('name')} (`{tour.get('slug')}`) — status **{tour.get('status')}**, "
              f"start {tour.get('startDate')}, end {tour.get('endDate')}, initialBalance {tour.get('initialBalance')} "
              f"{tour.get('currencyName')}, members {tour.get('memberCount')}, markets {tour.get('marketCount')}."); w(md)
    except Exception as e:
        w(md, f"_tournament meta unavailable: {e}_"); w(md)

    # 1. FULL INVENTORY
    markets = c.markets_all(page_limit=100)
    w(md, f"## 1. Full market inventory — {len(markets)} markets"); w(md)
    by_cat = defaultdict(list)
    all_exchanges = []     # (exchange_id, market_id, option, title)
    for m in markets:
        cat = categorize(m.get("title", ""))
        by_cat[cat].append(m)
        for ex in (m.get("exchanges") or []):
            all_exchanges.append((str(ex["id"]), str(m.get("id")), str(ex.get("option", "")), m.get("title", "")))
    w(md, "| category | # markets | # exchanges |"); w(md, "|---|---|---|")
    for cat in sorted(by_cat):
        nex = sum(len(m.get("exchanges") or []) for m in by_cat[cat])
        w(md, f"| {cat} | {len(by_cat[cat])} | {nex} |")
    w(md, f"| **TOTAL** | **{len(markets)}** | **{len(all_exchanges)}** |"); w(md)

    # batch prices for ALL exchanges (cheap: /exchanges/prices?ids in chunks of 50)
    px = {}
    ids = [e[0] for e in all_exchanges]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i+50]
        try:
            res = c.exchange_prices(chunk)
            for row in res.get("data", []):
                px[str(row.get("exchangeId"))] = row
        except SigAPIError as e:
            w(md, f"_price batch {i} error {e.status}_")
        time.sleep(0.4)      # pace: well under 100/min

    def spread_str(eid):
        r = px.get(eid, {})
        bb, ba, sp = r.get("bestBid"), r.get("bestAsk"), r.get("spread")
        return f"bid={bb} ask={ba} spread={sp} last={r.get('latestPrice')}"

    # per-category listing
    for cat in sorted(by_cat):
        w(md); w(md, f"### {cat} ({len(by_cat[cat])})"); w(md)
        w(md, "| market | state | #ex | prices (per exchange) | settles |"); w(md, "|---|---|---|---|---|")
        for m in sorted(by_cat[cat], key=lambda x: x.get("title", "")):
            exs = m.get("exchanges") or []
            pxs = "; ".join(f"{ex.get('option','?')}:{spread_str(str(ex['id']))}" for ex in exs)
            flags = []
            if m.get("isMultiOutcome"): flags.append("MULTI")
            if m.get("isComposite"): flags.append("COMPOSITE")
            name = (m.get("title", "")[:70] + (" ".join(flags) and f" [{','.join(flags)}]" or ""))
            w(md, f"| {name} | {state_of(m.get('title',''))} | {len(exs)} | {pxs[:160]} | {m.get('settlementDate','?')[:10]} |")

    # 2. MARKET STRUCTURE
    w(md); w(md, "## 2. Market structure"); w(md)
    binary = [m for m in markets if not m.get("isMultiOutcome") and not m.get("isComposite") and len(m.get("exchanges") or []) == 1]
    multi = [m for m in markets if m.get("isMultiOutcome") or len(m.get("exchanges") or []) > 1]
    composite = [m for m in markets if m.get("isComposite")]
    w(md, f"- **Simple binary** (one YES/NO exchange): {len(binary)}")
    w(md, f"- **Multi-outcome / multi-exchange** (mutually-exclusive legs — arb-rich): {len(multi)}")
    w(md, f"- **Composite/boolean** (isComposite): {len(composite)}"); w(md)
    # complementary PAIRS heuristic: same state+chamber, opposite party -> R-win + D-win ≈ 1
    pair_index = defaultdict(list)
    for m in markets:
        t = m.get("title", ""); key = (state_of(t), categorize(t))
        party = "R" if "republican" in t.lower() else ("D" if "democrat" in t.lower() else "?")
        if key[0] != "-" and party in ("R", "D"):
            pair_index[key].append((party, m.get("title", ""), [str(e["id"]) for e in (m.get("exchanges") or [])]))
    comp_pairs = {k: v for k, v in pair_index.items() if {p for p, _, _ in v} >= {"R", "D"}}
    w(md, f"**Complementary R/D pairs** (same state+chamber, both parties present — a 2-way market that should "
          f"sum to ~1; the natural arb constraint): {len(comp_pairs)}"); w(md)
    if multi:
        w(md, "Multi-outcome / multi-exchange markets:"); w(md)
        for m in multi[:40]:
            legs = "; ".join(f"{ex.get('option')}({ex['id']})={spread_str(str(ex['id']))}" for ex in (m.get("exchanges") or []))
            w(md, f"- **{m.get('title','')[:80]}** — {legs[:200]}")
    w(md)
    # relationships graph (composite/boolean links)
    for path in ("/relationships/graph", "/relationships"):
        try:
            g = c.get(path)
            n = len(g.get("data", g)) if isinstance(g, dict) else len(g)
            w(md, f"`{path}`: {json.dumps(g, default=str)[:500]}")
        except SigAPIError as e:
            w(md, f"`{path}`: error {e.status}")
    w(md)

    # 3. LIVE ARB STATE
    w(md, "## 3. Live arb state (/relationships/constraints)"); w(md)
    try:
        con = c.relationship_constraints()
        rows = con.get("data", []) if isinstance(con, dict) else con
        statuses = Counter(str(r.get("evaluationStatus", "?")).lower() for r in rows)
        w(md, f"- total constraints returned: {len(rows)} | violationsCount field: {con.get('violationsCount')} | computedAt: {con.get('computedAt')}")
        w(md, f"- status breakdown: {dict(statuses)}")
        viol = [r for r in rows if str(r.get("evaluationStatus","")).lower() == "violated"]
        for r in viol[:20]:
            w(md, f"  - VIOLATED amount={r.get('violationAmount')} trades={json.dumps(r.get('suggestedCorrectiveTrades'), default=str)[:300]}")
        if not rows:
            w(md, "- **0 constraints currently** (tournament not open yet / no live mispricings). The running "
                  "arb monitor will populate violation frequency/size/persistence once trading opens.")
    except SigAPIError as e:
        w(md, f"- constraints error {e.status}")
    w(md)

    # 4. LIQUIDITY — sample full orderbooks (paced)
    w(md, "## 4. Liquidity reality (sampled orderbook depth)"); w(md)
    # sample: prefer exchanges that already have a bid or ask, else first few
    live_ex = [eid for eid, r in px.items() if r.get("bestBid") is not None or r.get("bestAsk") is not None]
    sample = (live_ex[:12] if live_ex else ids[:12])
    w(md, f"- exchanges with a live bid/ask right now: {len(live_ex)} / {len(ids)}")
    w(md, "| exchange | bestBid | bestAsk | spread | bid depth (lvls/qty) | ask depth (lvls/qty) |"); w(md, "|---|---|---|---|---|---|")
    for eid in sample:
        try:
            ob = c.orderbook(eid)
            bd = ob.get("bids") or []; ak = ob.get("asks") or []
            bq = sum(float(x.get("quantity", 0)) for x in bd); aq = sum(float(x.get("quantity", 0)) for x in ak)
            w(md, f"| {eid} | {ob.get('bestBid')} | {ob.get('bestAsk')} | {ob.get('spread')} | {len(bd)}/{bq:.0f} | {len(ak)}/{aq:.0f} |")
        except SigAPIError as e:
            w(md, f"| {eid} | err {e.status} | | | | |")
        time.sleep(0.5)
    w(md)

    # 5. RESOLUTION TIMING
    w(md, "## 5. Resolution timing"); w(md)
    settles = Counter(m.get("settlementDate", "?")[:10] for m in markets)
    cutoff = "2026-11-04"
    before = [m for m in markets if (m.get("settlementDate") or "") < "2026-11-04T12:00:00"]
    w(md, f"- settlementDate distribution (by day): {dict(sorted(settles.items()))}")
    w(md, f"- markets settling BEFORE the Nov-4 noon cutoff: **{len(before)}** "
          + ("(endgame: some resolve pre-cutoff)" if before else "(all settle at/after Nov-4 — endgame is a single settlement wall)"))
    # sample market detail/rules
    for m in markets[:3]:
        try:
            d = c.get(f"/markets/{m.get('id')}")
            keys = list(d.keys()) if isinstance(d, dict) else "?"
            w(md, f"  - /markets/{m.get('id')} keys: {keys}")
        except SigAPIError as e:
            w(md, f"  - /markets/{m.get('id')} error {e.status}")
    w(md)

    # 6. SURPRISES / NOTES
    w(md, "## 6. Notes / surprises for the strategy designer"); w(md)
    notes = []
    notes.append(f"Markets appear to be per-state-chamber PARTY-WIN binaries ('Will the {{Party}} win the {{State}} {{chamber}}?'); "
                 f"R-win and D-win for the same race form a complementary pair that should sum to ~1 — {len(comp_pairs)} such pairs detected (the core arb surface).")
    notes.append(f"Pre-open books are SPARSE: {len(live_ex)}/{len(ids)} exchanges have any bid/ask right now; many show a single ask (e.g. 0.05x1000) and no bid. Thin-book assumption looks CORRECT pre-open; recheck post-open.")
    notes.append("`/exchanges/prices?ids=` (comma-sep) batch-returns NBBO for many exchanges in one call — use it, not per-exchange price calls, to stay under 100 reads/min.")
    notes.append("Orderbook carries `asOf.sequence` (monotonic) — usable for a staleness/sequence guard on a future stream.")
    notes.append("Prices are integer-ish in /account balance but orderbook prices are 0.005-tick floats in [0.005,0.995]; quantities are integer contracts.")
    for n in notes:
        w(md, f"- {n}")
    w(md)

    OUT.write_text("\n".join(md), encoding="utf-8")
    c.close()
    # console summary
    print(f"wrote {OUT}")
    print(f"markets={len(markets)} exchanges={len(all_exchanges)} | categories=" + ", ".join(f"{k}:{len(v)}" for k,v in sorted(by_cat.items())))
    print(f"structure: binary={len(binary)} multi/multi-ex={len(multi)} composite={len(composite)} complementary_RD_pairs={len(comp_pairs)}")
    print(f"liquidity: {len(live_ex)}/{len(ids)} exchanges have a live bid/ask")
    print(f"settles-before-Nov4-noon={len(before)} (else all at Nov-4 settlement wall)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
