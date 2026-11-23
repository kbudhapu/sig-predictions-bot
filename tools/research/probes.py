"""Read-only probes for new microstructure ideas (local snapshot)."""
import sqlite3, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
M = pd.read_pickle("M.pkl"); meta = pd.read_pickle("meta.pkl")
pairs = pd.read_pickle("pairs.pkl")["pairs"]
db = sqlite3.connect("snap.db")
THREE = {"nebraska_senate", "south_dakota_senate"}


def sel(df, mask):
    return df.where(mask).stack().dropna()


print("== P1. When D+R parity breaks, WHICH leg moved, and which leg fixes it? ==")
rows = []
for race, r, d in pairs:
    if race in THREE:
        continue
    s = (M[r] + M[d] - 1) * 100
    brk = (s.abs() >= 2) & (s.shift(1).abs() < 2)
    for t in s.index[brk.values]:
        i = s.index.get_loc(t)
        if i < 15 or i + 240 >= len(s):
            continue
        t0 = s.index[i - 15]
        dr, dd = M[r].iloc[i] - M[r].iloc[i - 15], M[d].iloc[i] - M[d].iloc[i - 15]
        mover, other = (r, d) if abs(dr) >= abs(dd) else (d, r)
        sgn = np.sign(s.iloc[i])
        h = 240
        mv_back = -(M[mover].iloc[i + h] - M[mover].iloc[i]) * sgn * 100   # mover giving back
        ot_fix = -(M[other].iloc[i + h] - M[other].iloc[i]) * sgn * 100    # other leg adjusting
        clean = min(abs(dr), abs(dd)) < 0.0025                              # only one leg moved
        rows.append(dict(race=race, dev=s.iloc[i], mover_back=mv_back, other_fix=ot_fix, clean=clean,
                         hour=t.hour, mover_move=max(abs(dr), abs(dd)) * 100))
B = pd.DataFrame(rows)
print(f"  breaks (|sum-1| crosses 2c): {len(B)}  one-leg-only breaks: {B.clean.mean():.2f}")
for lab, g in (("all", B), ("one leg moved", B[B.clean])):
    print(f"  [{lab:13s}] within 4h: mover gives back {g.mover_back.mean():+.2f}c (share>0 {(g.mover_back>0).mean():.2f}); "
          f"other leg adjusts {g.other_fix.mean():+.2f}c (share>0 {(g.other_fix>0).mean():.2f})")
print("  -> single-leg trade: fade the MOVER only, taker cost 0.5c (vs 1.0c for the pair)")

print("\n== P2. Our own footprint: SIG mid around our cross-market fills ==")
f = pd.read_sql("SELECT exchange_id ex, filled_at, signed_qty, outcome, price, mid_at_fill FROM strategy_fills "
                "WHERE strategy='cross_market_divergence' AND signed_qty IS NOT NULL", db)
f["t"] = pd.to_datetime(f.filled_at, utc=True).dt.floor("min")
out = []
for _, x in f.iterrows():
    if x.ex not in M or x.t not in M.index:
        continue
    i = M.index.get_loc(x.t)
    if i < 5 or i + 240 >= len(M):
        continue
    yes_dir = np.sign(x.signed_qty if x.outcome == "yes" else -x.signed_qty)   # + = we bought YES-exposure
    s = M[x.ex]
    out.append(dict(pre=(s.iloc[i] - s.iloc[i - 5]) * yes_dir * 100,
                    p15=(s.iloc[i + 15] - s.iloc[i]) * yes_dir * 100,
                    p60=(s.iloc[i + 60] - s.iloc[i]) * yes_dir * 100,
                    p240=(s.iloc[i + 240] - s.iloc[i]) * yes_dir * 100))
O = pd.DataFrame(out)
print(f"  fills {len(O)}: price move in OUR direction  5m before {O.pre.mean():+.2f}c | +15m {O.p15.mean():+.2f}c | "
      f"+60m {O.p60.mean():+.2f}c | +240m {O.p240.mean():+.2f}c")
print("  (negative after = we pushed the price and it came back = we pay impact; positive = we were early)")

print("\n== P3. Do jumps cluster across markets (one trader sweeping many markets)? ==")
J = (M.diff(5).abs() >= 0.02)
J = J & ~J.shift(1, fill_value=False)
per_min = J.sum(axis=1)
lam = per_min.mean()
obs = (per_min >= 3).mean(); pois = 1 - np.exp(-lam) * (1 + lam + lam ** 2 / 2)
print(f"  2c jumps/min avg {lam:.3f}; minutes with >=3 simultaneous jumps: observed {obs:.4f} vs random {pois:.6f}")
burst = per_min[per_min >= 3].index
if len(burst):
    sg = np.sign(M.diff(5))
    gb = []
    for t in burst:
        i = M.index.get_loc(t)
        if i + 60 < len(M):
            cols = J.columns[J.loc[t].values]
            gb.extend(list((-(M[cols].iloc[i + 60] - M[cols].iloc[i]) * sg.loc[t, cols] * 100).values))
    gb = np.array(gb)
    print(f"  burst minutes {len(burst)}, jumps in bursts {len(gb)}: next-60m give-back {np.nanmean(gb):+.2f}c "
          f"(share {np.nanmean(gb>0):.2f}) vs all jumps ~+0.6c")

print("\n== P5. Parity breaks and closes by UTC hour ==")
print("  breaks by hour:", B.groupby("hour").size().to_dict())
print("  4h fix (mover back + other) by hour block:",
      B.assign(fix=B.mover_back + B.other_fix, blk=pd.cut(B.hour, [-1, 5, 11, 17, 23], labels=["00-05", "06-11", "12-17", "18-23"]))
       .groupby("blk").fix.mean().round(2).to_dict())

print("\n== P6. Big-move fade WHEN it breaks parity vs when it doesn't ==")
part = {}
for race, r, d in pairs:
    if race not in THREE:
        part[r], part[d] = d, r
J5 = M.diff(5)
first = (J5.abs() >= 0.02) & ~(J5.abs() >= 0.02).shift(1, fill_value=False)
res = []
for ex in first.columns:
    if ex not in part:
        continue
    for t in M.index[first[ex].values]:
        i = M.index.get_loc(t)
        if i + 60 >= len(M) or i < 5:
            continue
        partner_move = (M[part[ex]].iloc[i] - M[part[ex]].iloc[i - 5])
        compensated = np.sign(partner_move) == -np.sign(J5[ex].iloc[i]) and abs(partner_move) >= 0.01
        gb = -(M[ex].iloc[i + 60] - M[ex].iloc[i]) * np.sign(J5[ex].iloc[i]) * 100
        res.append(dict(comp=compensated, gb=gb))
R = pd.DataFrame(res)
for lab, g in (("partner did NOT follow (parity broken)", R[~R.comp]), ("partner moved opposite (consistent)", R[R.comp])):
    print(f"  {lab:40s}: n={len(g):3d} next-60m give-back {g.gb.mean():+.2f}c share {(g.gb>0).mean():.2f}  net of 0.5c {g.gb.mean()-0.5:+.2f}c")

