"""TEST 2 — do large trades carry information (price keeps going their way) or get given back?"""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, lookup
warnings.filterwarnings("ignore")

t = load_tape()
t = t[~t.ours].copy()
M = minute_series("mid_history", "mid")
S = minute_series("sharp_history", "sharp")
t["pre"] = lookup(M, t.ex, t.ts, -1)
t["post"] = lookup(M, t.ex, t.ts, 1)
for h in (5, 60, 240, 1440):
    t[f"m{h}"] = lookup(M, t.ex, t.ts, h)
t = t[t.pre.notna() & t.post.notna()]
d = np.where(t.agg_yes, 1.0, -1.0)                       # + = aggressor's direction on the YES price
t["impact"] = d * (t.post - t.pre) * 100                 # immediate move around the trade
for h in (5, 60, 240, 1440):
    t[f"tot{h}"] = d * (t[f"m{h}"] - t.pre) * 100        # total move from before the trade
    t[f"drift{h}"] = d * (t[f"m{h}"] - t.post) * 100     # after-impact continuation (+) or give-back (-)
cut = t.usd.quantile([.5, .9, .99]).values
t["szb"] = pd.cut(t.usd, [0, cut[0], cut[1], cut[2], 1e9],
                  labels=[f"<${cut[0]:.0f}", f"${cut[0]:.0f}-{cut[1]:.0f}", f"${cut[1]:.0f}-{cut[2]:.0f}", f">${cut[2]:.0f}"])
print(f"trades {len(t)}  size cuts p50/p90/p99 = ${cut[0]:.0f} / ${cut[1]:.0f} / ${cut[2]:.0f}")


def summ(g):
    r = {"n": len(g), "usd": round(g.usd.sum()), "impact": round(g.impact.mean(), 3)}
    for h in (5, 60, 240, 1440):
        v = g[f"drift{h}"].dropna()
        r[f"drift{h}"] = round(v.mean(), 3) if len(v) else np.nan
    v = g["tot1440"].dropna()
    r["tot24h"] = round(v.mean(), 3) if len(v) else np.nan
    r["share_cont_4h"] = round((g.drift240.dropna() > 0).mean(), 2)
    return pd.Series(r)


print("\nc/share in the AGGRESSOR's direction. impact = move from minute before to minute after.")
print("driftH = further move after the impact (+ continues = informed, - given back = liquidity)\n")
print(t.groupby("szb", observed=True).apply(summ).to_string())

big = t[t.usd >= cut[2]].copy()
print(f"\n== Large trades (>= ${cut[2]:.0f}, n={len(big)}) by price level ==")
big["lvl"] = pd.cut(big.price, [0, .1, .3, .7, .9, 1])
print(big.groupby("lvl", observed=True).apply(summ).to_string())

# Polymarket context (only where we have the sharp)
t["sh_pre60"] = lookup(S, t.ex, t.ts, -60)
t["sh0"] = lookup(S, t.ex, t.ts, 0)
t["sh240"] = lookup(S, t.ex, t.ts, 240)
w = t[t.sh0.notna() & t.sh_pre60.notna()].copy()
dd = np.where(w.agg_yes, 1.0, -1.0)
w["poly_before"] = dd * (w.sh0 - w.sh_pre60) * 100           # Poly moved the trader's way in the hour before
w["poly_after"] = dd * (w.sh240 - w.sh0) * 100               # Poly moves the trader's way after (knew something?)
w["toward_poly"] = np.sign(w.sh0 - w.pre) == dd              # trade pushes SIG toward Polymarket
w["big"] = w.usd >= cut[1]
print(f"\n== With Polymarket context (n={len(w)}; big = top 10% by $) ==")
for (b, tp), g in w.groupby(["big", "toward_poly"]):
    print(f"  {'BIG  ' if b else 'small'} {'toward Poly' if tp else 'away from Poly'}: n={len(g):5d} "
          f"poly_before {g.poly_before.mean():+.3f}c  poly_after4h {g.poly_after.mean():+.3f}c  "
          f"SIG drift4h {g.drift240.mean():+.3f}c (cont {(g.drift240 > 0).mean():.2f})")
print("\n== Clusters: same market, same direction, >=3 trades within 10 min (sweeps / persistent buyers) ==")
t = t.sort_values(["ex", "ts"])
t["dirn"] = np.where(t.agg_yes, 1, -1)
grp = (t.ex != t.ex.shift()) | (t.dirn != t.dirn.shift()) | ((t.ts - t.ts.shift()) > pd.Timedelta("10min"))
t["run"] = grp.cumsum()
runs = t.groupby("run").agg(n=("usd", "size"), usd=("usd", "sum"), d240=("drift240", "last"), impact=("impact", "sum"))
for lab, g in (("runs of 1-2", runs[runs.n < 3]), ("runs of 3-9", runs[(runs.n >= 3) & (runs.n < 10)]), ("runs of 10+", runs[runs.n >= 10])):
    print(f"  {lab:12s}: n={len(g):5d} median ${g.usd.median():,.0f}  after-run drift4h {g.d240.mean():+.3f}c "
          f"(cont {(g.d240 > 0).mean():.2f})")
