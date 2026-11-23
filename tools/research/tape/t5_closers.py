"""TEST 5 — who closes SIG-vs-Polymarket gaps: trades or quote moves, which side, how big, how fast after Poly moves?"""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series
warnings.filterwarnings("ignore")

M = minute_series("mid_history", "mid")
S = minute_series("sharp_history", "sharp")
cols = [c for c in S.columns if c in M.columns]
S = S[cols]; Ms = M.reindex(S.index)[cols]
G = (S - Ms) * 100                                        # + = SIG below Poly
t = load_tape()
t["min"] = t.ts.dt.floor("min")
ours = t[t.ours]
t["dir"] = np.where(t.agg_yes, 1.0, -1.0)

# convergence events: |gap| shrinks by >=1c over 15m, starting from |gap| >= 3c
W = 15
g0, g1 = G, G.shift(-W)
closing = (g0.abs() >= 3) & ((g0.abs() - g1.abs()) >= 1) & (np.sign(g0) == np.sign(g1).where(g1.abs() > 0.25, np.sign(g0)))
start = closing & ~closing.shift(1, fill_value=False)
ev = [(ex, ts) for ex in cols for ts in G.index[start[ex].values]]
print(f"convergence events (|gap|>=3c shrinking >=1c within {W}m): {len(ev)} across {len({e for e, _ in ev})} markets")

tb = {ex: g.set_index("ts").sort_index() for ex, g in t.groupby("ex")}
rows = []
for ex, ts in ev:
    gap = G.at[ts, ex]
    close_dir = np.sign(gap)                               # +: closing needs YES buying (SIG up)
    tr = tb.get(ex)
    w = tr.loc[ts:ts + pd.Timedelta(minutes=W)] if tr is not None else pd.DataFrame()
    n = len(w)
    usd_close = w.loc[w.dir == close_dir, "usd"].sum() if n else 0.0
    usd_open = w.loc[w.dir != close_dir, "usd"].sum() if n else 0.0
    ours_n = int(w.ours.sum()) if n else 0
    # did Poly just move? last |1-min Poly move| >= 0.5c within the prior 30 min
    sp = S[ex].loc[ts - pd.Timedelta(minutes=30):ts]
    mv = sp.diff().abs()
    poly_lag = (ts - mv[mv >= 0.005].index[-1]).total_seconds() / 60 if (mv >= 0.005).any() else np.nan
    first = (w.index[w.dir == close_dir][0] - ts).total_seconds() / 60 if n and (w.dir == close_dir).any() else np.nan
    rows.append(dict(ex=ex, gap=abs(gap), closed=abs(gap) - abs(G[ex].shift(-W).at[ts]), n_trades=n,
                     usd_close=usd_close, usd_open=usd_open, max_trade=(w.usd.max() if n else 0.0),
                     ours=ours_n, poly_lag=poly_lag, first_close_trade_min=first))
E = pd.DataFrame(rows)
print(f"  events with ANY trade in the window: {(E.n_trades > 0).mean():.2f}  (no trades = quotes moved on their own)")
print(f"  median closing-side $ {E.usd_close.median():,.0f} vs opposing $ {E.usd_open.median():,.0f}; "
      f"closing-side share of $ {E.usd_close.sum() / max(E.usd_close.sum() + E.usd_open.sum(), 1):.2f}")
print(f"  events where WE traded: {(E.ours > 0).mean():.2f}")
print(f"  largest trade in the window: median ${E.max_trade.median():,.0f}, p90 ${E.max_trade.quantile(.9):,.0f}")
pm = E.poly_lag.notna()
print(f"  Polymarket moved >=0.5c in the 30m before: {pm.mean():.2f} of events; median lag {E.poly_lag.median():.1f} min")
print(f"  first closing-side trade after the window opens: median {E.first_close_trade_min.median():.1f} min")
print("\n  closed amount by whether Poly had just moved:")
print(E.groupby(pm).agg(n=("ex", "size"), gap=("gap", "median"), closed=("closed", "median"),
                        trades=("n_trades", "median")).rename(index={True: "Poly moved", False: "Poly flat"}).round(2).to_string())

# reaction speed: after Poly moves >= 1c in one minute, when does the first SIG trade in that direction arrive?
print("\n== Reaction to Polymarket moves >=1c (1-min) ==")
jumps = []
dS = S.diff()
for ex in cols:
    for ts in S.index[(dS[ex].abs() >= 0.01).values]:
        jumps.append((ex, ts, np.sign(dS.at[ts, ex])))
lat, sizes, none = [], [], 0
for ex, ts, s in jumps:
    tr = tb.get(ex)
    w = tr.loc[ts:ts + pd.Timedelta(minutes=30)] if tr is not None else pd.DataFrame()
    w = w[w.dir == s] if len(w) else w
    if len(w):
        lat.append((w.index[0] - ts).total_seconds() / 60); sizes.append(w.usd.iloc[0])
    else:
        none += 1
print(f"  Poly jumps: {len(jumps)}; followed by a same-direction SIG trade within 30m: {len(lat)} "
      f"({len(lat) / max(len(jumps), 1):.2f})")
if lat:
    lat = np.array(lat)
    print(f"  latency to first same-direction SIG trade: <1m {np.mean(lat < 1):.2f}, <2m {np.mean(lat < 2):.2f}, "
          f"<5m {np.mean(lat < 5):.2f}; median {np.median(lat):.1f} min; first trade median ${np.median(sizes):,.0f}")

# the gaps that never close: markets with persistent |gap| >= 3c the whole window
pers = (G.abs() >= 3).mean()
tvol = t.groupby("ex").usd.sum()
print(f"\nmarkets with |gap|>=3c more than 80% of the time: {(pers > .8).sum()}; their median $ traded "
      f"{tvol.reindex(pers[pers > .8].index).median():,.0f} vs others {tvol.reindex(pers[pers <= .8].index).median():,.0f}")
