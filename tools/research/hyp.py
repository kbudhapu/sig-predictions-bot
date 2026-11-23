"""Simple microstructure hypotheses, costed as a TAKER (buy at ask = mid + half-spread, sell at bid).
Half-spread per market = today's spread / 2 (84% are 1 tick -> 0.25c). Snapshot data, 2.9 days."""
import sqlite3, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
M = pd.read_pickle("M.pkl"); meta = pd.read_pickle("meta.pkl"); P = pd.read_pickle("P.pkl")
pairs = pd.read_pickle("pairs.pkl")["pairs"]
pr = pd.read_sql("SELECT exchange_id ex, best_bid b, best_ask a FROM prices", sqlite3.connect("snap.db")).set_index("ex")
HS = ((pr.a - pr.b) / 2).clip(lower=0.0025).reindex(M.columns).fillna(0.0025)
days = (M.index[-1] - M.index[0]).total_seconds() / 86400
THREE_WAY = {"nebraska_senate", "south_dakota_senate", "nebraska_governor", "south_dakota_governor"}

print("== H0. Why plain reversion doesn't pay: fade every >=1-tick 5m move, hold 60m, taker ==")
d5 = M.diff(5); fwd = M.shift(-60) - M
sig = d5.abs() >= 0.005
gross = (-(np.sign(d5)) * fwd).where(sig).stack().dropna() * 100
cost = (2 * HS * 100)
net = gross - pd.Series(cost.reindex(gross.index.get_level_values(1)).values, index=gross.index)
print(f"  trades {len(gross)}  gross give-back {gross.mean():+.3f}c  cost {cost.median():.2f}c  NET {net.mean():+.3f}c/share")
for th in (0.01, 0.02, 0.03, 0.05):
    s = d5.abs() >= th
    g = (-(np.sign(d5)) * fwd).where(s & ~s.shift(1, fill_value=False)).stack().dropna() * 100
    print(f"  fade 5m moves >= {th*100:.0f}c: n={len(g):5d} gross {g.mean():+.2f}c  net {g.mean() - 50*cost.median()/50:+.2f}c")

print("\n== H1. D+R parity: buy the cheap PAIR when the two legs' YES prices sum away from 100% ==")
print("   (sum>1: buy both NOs -> settles >= 1 always; sum<1: buy both YES -> settles 1 only if D or R wins)")
res = []
for race, r, d in pairs:
    if race in THREE_WAY:
        continue
    s = (M[r] + M[d]).dropna()
    h = HS[r] + HS[d]
    for T, X in ((0.02, 0.005), (0.03, 0.01), (0.04, 0.01)):
        pos = None
        for t, v in s.items():
            dev = v - 1
            if pos is None:
                if abs(dev) >= T:
                    pos = (t, dev)
            else:
                t0, dev0 = pos
                if abs(dev) <= X or np.sign(dev) != np.sign(dev0):
                    res.append(dict(race=race, T=T, side="NOs" if dev0 > 0 else "YESs", t0=t0,
                                    mins=(t - t0).total_seconds() / 60,
                                    pnl=(abs(dev0) - abs(dev)) * 100 - 2 * h * 100, closed=True,
                                    settle=(abs(dev0) - h) * 100))
                    pos = None
        if pos is not None:
            t0, dev0 = pos
            res.append(dict(race=race, T=T, side="NOs" if dev0 > 0 else "YESs", t0=t0,
                            mins=(s.index[-1] - t0).total_seconds() / 60, pnl=np.nan, closed=False,
                            settle=(abs(dev0) - h) * 100))
R = pd.DataFrame(res)
for T, g in R.groupby("T"):
    c = g[g.closed]
    print(f"  enter |sum-1|>={T*100:.0f}c: trades {len(g):4d} ({len(g)/days:.0f}/day), closed {len(c)}, "
          f"median hold {c.mins.median():.0f}m, net per closed trade {c.pnl.mean():+.2f}c/pair "
          f"(hit {(c.pnl>0).mean():.2f}); if HELD to settlement instead: {g.settle.mean():+.2f}c/pair")
    for side, gs in g.groupby("side"):
        cs = gs[gs.closed]
        print(f"      {side:4s}: n={len(gs):4d} closed-trade net {cs.pnl.mean():+.2f}c  settle {gs.settle.mean():+.2f}c")

print("\n== H2. Fade big jumps only when Polymarket did NOT move (noise, not news) ==")
cols = [c for c in P.columns if c in M.columns]
Ms = M.reindex(P.index)[cols]
j = Ms.diff(5).abs() >= 0.02
j = j & ~j.shift(1, fill_value=False)
flat = P[cols].diff(30).abs() < 0.005
for h in (30, 60, 240):
    g = (-(np.sign(Ms.diff(5))) * (Ms.shift(-h) - Ms)).where(j & flat).stack().dropna() * 100
    print(f"  >=2c 5m jump, Poly flat 30m, hold {h:3d}m: n={len(g)} gross {g.mean():+.2f}c, net of 0.5c {g.mean()-0.5:+.2f}c")

print("\n== H3. Fade moves AWAY from Polymarket, hold the move TOWARD it (anchor-aware reversion) ==")
gap = P[cols] - Ms
away = np.sign(Ms.diff(15)) == -np.sign(gap)          # SIG moved away from Poly in the last 15m
mv = Ms.diff(15).abs() >= 0.01
for h in (60, 240):
    g = (np.sign(gap) * (Ms.shift(-h) - Ms)).where(away & mv).stack().dropna() * 100
    print(f"  SIG moved >=1c AWAY from Poly, buy toward Poly, hold {h}m: n={len(g)} gross {g.mean():+.2f}c net {g.mean()-0.5:+.2f}c")
