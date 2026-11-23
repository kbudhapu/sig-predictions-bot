"""Kalshi vs Polymarket vs SIG on 2026 races, compared via each race's FAVOURITE (Kalshi lists candidates, not parties)."""
import json, re
import numpy as np, pandas as pd, yaml

ST = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
      "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
      "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
      "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
      "montana": "MT", "nebraska": "NE", "nevada": "NV", "new_hampshire": "NH", "new_jersey": "NJ", "new_mexico": "NM",
      "new_york": "NY", "north_carolina": "NC", "north_dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
      "pennsylvania": "PA", "rhode_island": "RI", "south_carolina": "SC", "south_dakota": "SD", "tennessee": "TN",
      "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west_virginia": "WV",
      "wisconsin": "WI", "wyoming": "WY"}
B = json.load(open("book_now.json"))
mid, sharp = B["mid"], B["sharp"]
races = yaml.safe_load(open("race_mapping.yaml", encoding="utf-8"))["races"]
sig = {}
for key, r in races.items():
    if not key.endswith(("_dem", "_rep")) or "control" in key:
        continue
    ex = str((r.get("sig") or {}).get("exchange_id"))
    base = key.rsplit("_", 1)[0]
    m = re.match(r"(.+)_(senate|governor)$", base)
    if m:
        st = ST.get(m.group(1)); k = (m.group(2), st, None)
    else:
        h = re.match(r"([a-z]{2})_(\d{2}|al)_house", base)
        if not h:
            continue
        k = ("house", h.group(1).upper(), h.group(2).upper())
    if not k[1]:
        continue
    sig.setdefault(k, {})[key.rsplit("_", 1)[1]] = ex
K = pd.DataFrame(json.load(open("kalshi_race_markets.json")))


def kkey(ev):
    m = re.match(r"^(?:KX)?SENATE([A-Z]{2})S?-26", ev)
    if m:
        return ("senate", m.group(1), None)
    m = re.match(r"^KXGOV([A-Z]{2})-26", ev) or re.match(r"^GOVPARTY([A-Z]{2})-26", ev)
    if m:
        return ("governor", m.group(1), None)
    m = re.match(r"^KXHOUSERACE-([A-Z]{2})(\d{2}|AL)-26", ev)
    if m:
        return ("house", m.group(1), m.group(2))
    return None


K["key"] = K.event.map(kkey)
K = K[K.key.notna()]
K["kmid"] = np.where(K.bid.notna() & K.ask.notna() & (K.ask > K.bid), (K.bid + K.ask) / 2, K["last"])
kfav = K.sort_values("kmid").groupby("key").tail(1).set_index("key")
rows = []
for k, legs in sig.items():
    pd_, pr = legs.get("dem"), legs.get("rep")
    poly = {p: sharp.get(ex) for p, ex in legs.items() if sharp.get(ex) is not None}
    sm = {p: mid.get(ex) for p, ex in legs.items() if mid.get(ex) is not None}
    if not sm:
        continue
    fav_party = max(sm, key=sm.get)                         # SIG's favourite party
    kr = kfav.loc[[k]].iloc[0] if k in kfav.index else None
    rows.append(dict(office=k[0], st=k[1], dist=k[2], fav=fav_party, sig=sm[fav_party],
                     poly=poly.get(fav_party), kalshi=(None if kr is None else kr.kmid),
                     k_vol=(None if kr is None else kr.vol), k_spread=(None if kr is None else (kr.ask - kr.bid) if kr.ask and kr.bid else None),
                     k_who=(None if kr is None else kr["sub"])))
D = pd.DataFrame(rows)
print(f"SIG races (D/R) {len(D)}: with Polymarket {D.poly.notna().sum()}, with Kalshi {D.kalshi.notna().sum()}, "
      f"Kalshi but NOT Poly {(D.kalshi.notna() & D.poly.isna()).sum()}, neither {(D.kalshi.isna() & D.poly.isna()).sum()}")
both = D[D.poly.notna() & D.kalshi.notna()].copy()
both["k_minus_p"] = 100 * (both.kalshi - both.poly)
print(f"\nboth venues (n={len(both)}): |Kalshi - Poly| on the favourite: median {both.k_minus_p.abs().median():.1f}c, "
      f"p90 {both.k_minus_p.abs().quantile(.9):.1f}c; >5c apart in {(both.k_minus_p.abs() > 5).sum()} races")
both["sig_vs_p"] = 100 * (both.sig - both.poly); both["sig_vs_k"] = 100 * (both.sig - both.kalshi)
print(f"  SIG favourite vs Poly: median {both.sig_vs_p.median():+.1f}c | SIG vs Kalshi: median {both.sig_vs_k.median():+.1f}c "
      f"(- = SIG prices favourites lower)")
dis = both[both.k_minus_p.abs() > 5].sort_values("k_minus_p")
print("\nraces where Kalshi and Poly disagree by >5c (favourite price, c):")
print(dis[["office", "st", "dist", "fav", "sig", "poly", "kalshi", "k_vol"]].round(3).to_string(index=False))
only = D[D.kalshi.notna() & D.poly.isna()].copy()
only["gap_c"] = 100 * (only.kalshi - only.sig)
print(f"\nKalshi-only races (no Polymarket benchmark): {len(only)}; SIG vs Kalshi gap >=5c in {(only.gap_c.abs() >= 5).sum()}; "
      f"with Kalshi volume >= $20k: {(only.k_vol >= 20000).sum()}")
print(only.sort_values("gap_c", key=abs, ascending=False).head(12)[["office", "st", "dist", "fav", "sig", "kalshi", "gap_c", "k_vol", "k_spread"]].round(3).to_string(index=False))
