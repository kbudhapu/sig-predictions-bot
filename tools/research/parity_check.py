"""Is the D+R parity deviation real, or a stale-price artefact? Use cross-market's FRESH fetched bid/ask."""
import sqlite3
import numpy as np, pandas as pd
db = sqlite3.connect("snap.db")
pairs = pd.read_pickle("pairs.pkl")["pairs"]
THREE_WAY = {"nebraska_senate", "south_dakota_senate"}

pr = pd.read_sql("SELECT exchange_id ex, updated_at FROM prices", db)
age = (pd.Timestamp("2026-10-06T08:06:15Z") - pd.to_datetime(pr.updated_at, utc=True)).dt.total_seconds()
print("prices-table age at snapshot (s):", age.quantile([.5,.75,.9,1]).round(0).to_dict())

sh = pd.read_sql("SELECT exchange_id ex, ts, sig_bid b, sig_ask a FROM sharp_history WHERE sig_bid IS NOT NULL", db)
sh["t"] = pd.to_datetime(sh.ts.str[:16], utc=True)
B = sh.pivot_table(index="t", columns="ex", values="b", aggfunc="last")
A = sh.pivot_table(index="t", columns="ex", values="a", aggfunc="last")
print(f"fresh bid/ask: {B.shape[1]} markets, {B.index.min()} .. {B.index.max()} ({len(B)} minutes)")
rows = []
for race, r, d in pairs:
    if race in THREE_WAY or r not in B or d not in B:
        continue
    nos = (B[r] + B[d] - 1) * 100       # executable edge for buying BOTH NOs at their asks: 2 - cost
    yes = (1 - A[r] - A[d]) * 100       # executable edge for buying BOTH YES at their asks
    rows.append(pd.DataFrame({"race": race, "nos_edge": nos, "yes_edge": yes}))
X = pd.concat(rows)
print(f"pairs with fresh data: {X.race.nunique()}")
for col in ("nos_edge", "yes_edge"):
    v = X[col].dropna()
    print(f"  {col}: share of minutes with EXECUTABLE edge >0: {(v>0).mean():.3f}, >=1c: {(v>=1).mean():.3f}, "
          f">=2c: {(v>=2).mean():.3f}, max {v.max():.2f}c")
top = X.groupby("race")[["nos_edge", "yes_edge"]].max().max(axis=1).sort_values(ascending=False).head(8)
print("  best executable edge by race (c):", top.round(2).to_dict())
# Same window, mid_history-based deviation, for comparison
mh = pd.read_sql("SELECT exchange_id ex, ts, mid FROM mid_history WHERE ts >= '2026-10-06T05:15'", db)
mh["t"] = pd.to_datetime(mh.ts.str[:16], utc=True)
Mm = mh.pivot_table(index="t", columns="ex", values="mid", aggfunc="last")
dv = pd.concat([((Mm[r] + Mm[d] - 1) * 100) for race, r, d in pairs if race not in THREE_WAY and r in Mm and d in Mm], axis=1)
print(f"  same window, mid_history |sum-1|>=2c share: {(dv.abs()>=2).stack().mean():.3f}, >=3c: {(dv.abs()>=3).stack().mean():.3f}")
