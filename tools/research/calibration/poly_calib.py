"""Is Polymarket calibrated on US state races? 2024 resolved Senate/House/Governor markets, price 28/14/7/1 days out."""
import json, math
import numpy as np, pandas as pd

d = pd.DataFrame(json.load(open("poly2024_markets.json")))
d["kind"] = d.event.str.extract(r"(senate|governor|house|district)", expand=False).fillna("other")
d["third"] = ~d.question.str.contains(r"Democrat|Republican", case=False)
print(f"markets {len(d)}  events {d.event.nunique()}  by kind {d.groupby('kind').event.nunique().to_dict()}  "
      f"third-party/other markets {int(d.third.sum())}  resolved YES {int(d.yes.sum())}")

BINS = [0, .02, .05, .10, .20, .35, .50, .65, .80, .90, .95, .98, 1.0001]


def calib(df, col):
    x = df[df[col].notna()]
    x = x.assign(b=pd.cut(x[col], BINS, right=False))
    rows = []
    for b, g in x.groupby("b", observed=True):
        n = len(g); p = g[col].mean(); r = g.yes.mean()
        se = math.sqrt(max(p * (1 - p), 1e-4) / n)
        rows.append(dict(bucket=str(b), n=n, avg_price=round(p, 3), won=round(r, 3), diff_pts=round(100 * (r - p), 1),
                         z=round((r - p) / se, 2)))
    return pd.DataFrame(rows)


main = d[~d.third]
for col in ("d28", "d7", "d1"):
    x = main[main[col].notna()]
    brier = ((x[col] - x.yes) ** 2).mean()
    print(f"\n== D/R markets, price {col[1:]} days out (n={len(x)}) Brier {brier:.4f} ==")
    print(calib(main, col).to_string(index=False))

# the bet our book makes: NO on underdogs. If Poly prices an underdog at p, how often did it win?
print("\n== Underdog legs (Poly price 2%-35%), 28 days out ==")
u = main[(main.d28 >= .02) & (main.d28 < .35)]
print(f"  n={len(u)}  average price {u.d28.mean():.3f}  won {u.yes.mean():.3f}  "
      f"-> underdogs won {100*(u.yes.mean()-u.d28.mean()):+.1f} pts vs price")
print(f"  expected wins at Poly prices {u.d28.sum():.1f} vs actual {int(u.yes.sum())}")
# a SIG-like price: underdogs ~4c richer than Poly. Was that richer price worse?
for bump in (0.02, 0.04):
    print(f"  if priced {bump*100:.0f}c above Poly (SIG-like): avg price {u.d28.mean()+bump:.3f} vs won {u.yes.mean():.3f}")

# per-race view (D and R legs are the same event; count races, not legs): favourite's price vs did it win
fav = main[main.d28.notna()].sort_values("d28").groupby("event").tail(1)
print(f"\n== Race favourites 28d out (one per race, n={len(fav)}) ==")
print(calib(fav, "d28").to_string(index=False))
print(f"  favourites won {fav.yes.mean():.3f} at average price {fav.d28.mean():.3f}")
big = fav[fav.volume >= fav.volume.median()]
print(f"  liquid half (volume >= ${fav.volume.median():,.0f}): won {big.yes.mean():.3f} at {big.d28.mean():.3f} (n={len(big)})")
print(f"\nupsets 28d out (favourite >= 65% lost): "
      f"{fav[(fav.d28 >= .65) & (fav.yes == 0)][['event','d28']].to_string(index=False) if ((fav.d28>=.65)&(fav.yes==0)).any() else 'none'}")
