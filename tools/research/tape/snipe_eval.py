"""Re-check the SNIPER: score its recorded would-be takes. Episodes = first record of each run (same market+side, gaps
<= 2 min apart). Taking at take_price (held-side); markout vs SIG mid at +5m/+60m/+4h/+24h, edge vs Polymarket.
'Fresh' = Polymarket moved >= 1c (toward the take) in the 5 minutes before the episode — a real stale-quote snipe."""
import gzip, json, warnings
import numpy as np, pandas as pd
from common import minute_series
warnings.filterwarnings("ignore")

R = pd.DataFrame([json.loads(l) for l in gzip.open("snipes.jsonl.gz", "rt") if l.strip()])
R["ts"] = pd.to_datetime(R.ts, utc=True, format="ISO8601")
R = R.sort_values(["exch", "side", "ts"])
new = (R.exch != R.exch.shift()) | (R.side != R.side.shift()) | ((R.ts - R.ts.shift()) > pd.Timedelta("2min"))
E = R[new].copy()
print(f"records {len(R)} -> episodes {len(E)} in {E.exch.nunique()} markets, {R.ts.min():%m-%d %H:%M} .. {R.ts.max():%m-%d %H:%M}")
M = minute_series("mid_history", "mid")
S = minute_series("sharp_history", "sharp")


def at(grid, ex, t):
    t = t.floor("min")
    return grid.at[t, ex] if (ex in grid.columns and t in grid.index) else np.nan


yes = E.side == "yes"
for h in (5, 60, 240, 1440):
    m = np.array([at(M, ex, t + pd.Timedelta(minutes=h)) for ex, t in zip(E.exch, E.ts)])
    held = np.where(yes, m, 1 - m)                         # value of the bought side at the later SIG mid
    E[f"mo{h}"] = 100 * (held - E.take_price)              # c/share vs our take price (includes the spread we paid)
E["edge_c"] = 100 * E.edge
pre = np.array([at(S, ex, t - pd.Timedelta(minutes=5)) for ex, t in zip(E.exch, E.ts)])
now = np.array([at(S, ex, t) for ex, t in zip(E.exch, E.ts)])
move = (now - pre) * np.where(yes, 1, -1)                  # + = Poly moved toward our take in the last 5 min
E["poly_known"] = ~np.isnan(move)
E["fresh"] = move >= 0.01
E["eb"] = pd.cut(E.edge_c, [0, 6, 8, 10, 15, 100])


def rep(g, lab):
    print(f"  {lab:34s} n={len(g):5d} edge {g.edge_c.mean():5.1f}c | markout 5m {g.mo5.mean():+.2f} 60m {g.mo60.mean():+.2f} "
          f"4h {g.mo240.mean():+.2f} 24h {g.mo1440.mean():+.2f}c | share +ve @60m {(g.mo60 > 0).mean():.2f}")


print("\nmarkout = value of what we took, at the later SIG mid, minus the take price (c/share; spread already paid)")
rep(E, "all episodes")
for k, g in E.groupby("reason"):
    rep(g, k)
for k, g in E.groupby("eb", observed=True):
    rep(g, f"edge {k}")
P = E[E.poly_known]
print(f"\nwith Polymarket history (n={len(P)}):")
rep(P[P.fresh], "FRESH (Poly moved >=1c toward us, 5m)")
rep(P[~P.fresh], "persistent gap (Poly didn't move)")
days = (R.ts.max() - R.ts.min()).total_seconds() / 86400
f = P[P.fresh]
print(f"\nfresh snipes per day ~{len(f) / max((P.ts.max() - P.ts.min()).total_seconds() / 86400, 1e-9):.0f}; "
      f"at $1k each and the 60m markout: ~${(f.mo60.mean() / 100) * 1000 / max(f.take_price.mean(), .01) * len(f) / max((P.ts.max() - P.ts.min()).total_seconds() / 86400, 1e-9):,.0f}/day")
