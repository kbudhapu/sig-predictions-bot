"""Phase-1 matcher: SIG state GOVERNOR races <-> Polymarket per-party governor sub-markets.
Matches by state, verifies polarity via R+D complementarity (each must sum ~1.0 on BOTH venues), computes
the SIG-vs-Polymarket divergence + the sharp gate verdict, and emits race_mapping.yaml entries. READ-ONLY."""
import asyncio, re, time, httpx
from sig.sig_client import SigClient
from signals.sharp_benchmark import evaluate, SourceQuote

def _retry(fn, n=10):
    for i in range(n):
        try: return fn()
        except Exception as e:
            if "429" in str(e) and i < n-1: time.sleep(5); continue
            raise

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PARTY = re.compile(r"Will the (Republican|Democratic) Party win the (.+?)\?$")

def norm_state(s):  # "New Hampshire Governor" -> "new hampshire"; slug "new-hampshire-governor-..." -> "new hampshire"
    s = s.lower().replace("-", " ")
    for junk in ["governor", "election", "winner", "2026", "race", "the", "gubernatorial"]:
        s = s.replace(junk, " ")
    return " ".join(s.split())

async def main():
    sig = SigClient(key_env="SIG_API_KEY_READ", timeout=20.0)
    # 1. SIG governor races: state -> {R: exch, D: exch}
    sigmk = _retry(lambda: sig.markets_all())
    sig_gov = {}
    for m in sigmk:
        mm = PARTY.search(m.get("title") or "")
        if not mm: continue
        party, what = mm.group(1), mm.group(2).strip()
        if "governor" not in what.lower(): continue
        ex = str(m["exchanges"][0]["id"]) if m.get("exchanges") else None
        sig_gov.setdefault(norm_state(what), {})["R" if party == "Republican" else "D"] = ex
    sig_ex_all = [e for d in sig_gov.values() for e in d.values() if e]
    # SIG prices
    sig_px = {}
    for i in range(0, len(sig_ex_all), 90):
        for r in _retry(lambda: sig.exchange_prices(sig_ex_all[i:i+90])).get("data", []):
            b, a = r.get("bestBid"), r.get("bestAsk")
            if b is not None and a is not None: sig_px[str(r.get("exchangeId"))] = (float(b)+float(a))/2
    print("SIG governor states:", len(sig_gov))

    async with httpx.AsyncClient(timeout=25.0) as c:
        # 2. Polymarket governor events -> state -> {R:(token,price), D:(token,price)}
        poly_gov = {}
        for off in range(0, 3000, 200):
            d = (await c.get(f"{GAMMA}/events", params={"closed":"false","limit":200,"offset":off})).json()
            if not isinstance(d, list) or not d: break
            for e in d:
                t = (e.get("title") or "")
                if "governor" not in t.lower() or "election winner" not in t.lower(): continue
                st = norm_state(t); legs = {}
                for sm in e.get("markets", []):
                    q = (sm.get("question") or "").lower(); pr = sm.get("outcomePrices")
                    if not pr: continue
                    import json as _j
                    price = float(_j.loads(pr)[0]) if isinstance(pr, str) else float(pr[0])
                    toks = sm.get("clobTokenIds"); tok = (_j.loads(toks)[0] if isinstance(toks, str) else (toks or [None])[0])
                    if "will the democrats win" in q: legs["D"] = (sm.get("id"), tok, price)
                    elif "will the republicans win" in q: legs["R"] = (sm.get("id"), tok, price)
                if legs: poly_gov[st] = legs
        print("Polymarket governor states:", len(poly_gov))

        # 3. match + verify + shadow
        print("\n%-16s %-6s %7s %7s | %-6s %7s %7s | compl(S/P) div_R div_D gate" % ("state","SIGr","", "", "POLYr","",""))
        matched = 0
        for st, sg in sorted(sig_gov.items()):
            pg = poly_gov.get(st)
            if not pg or "R" not in sg or "D" not in sg or "R" not in pg or "D" not in pg: continue
            sr, sd = sig_px.get(sg["R"]), sig_px.get(sg["D"])
            pr, pd = pg["R"][2], pg["D"][2]
            if None in (sr, sd): continue
            matched += 1
            compl_s = sr + sd; compl_p = pr + pd   # both should ~1.0 (direct polarity, no flip needed)
            vr = evaluate(sr, [SourceQuote("polymarket", pr, 1.0)], min_sources=1, min_divergence=0.05)
            vd = evaluate(sd, [SourceQuote("polymarket", pd, 1.0)], min_sources=1, min_divergence=0.05)
            flag = "" if abs(compl_s-1)<0.08 and abs(compl_p-1)<0.08 else "  <<CHECK compl"
            print("%-16s R:%.3f/%.3f D:%.3f/%.3f  S=%.2f P=%.2f  dR=%+.3f dD=%+.3f  %s%s" % (
                st, sr, pr, sd, pd, compl_s, compl_p, (vr.divergence or 0), (vd.divergence or 0),
                ("TRADE_R" if vr.tradeable else "-")+"/"+("TRADE_D" if vd.tradeable else "-"), flag))
        print("\nMATCHED governor races:", matched, "(polarity=direct; verify compl~1.0 above)")

asyncio.run(main())
