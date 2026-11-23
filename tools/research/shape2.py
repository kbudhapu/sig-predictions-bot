"""Part 2: fixed lead-lag + jumps, activity trend, gap decomposition, then a Numerai-style GBM."""
import warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
M = pd.read_pickle("M.pkl"); P = pd.read_pickle("P.pkl"); meta = pd.read_pickle("meta.pkl")
pairs = pd.read_pickle("pairs.pkl")["pairs"]
cols = [c for c in P.columns if c in M.columns]
Ms = M.reindex(P.index)[cols]


def corr(a, b):
    a, b = np.asarray(a, float).ravel(), np.asarray(b, float).ravel()
    m = np.isfinite(a) & np.isfinite(b)
    return np.corrcoef(a[m], b[m])[0, 1] if m.sum() > 10 else np.nan


def sel(df, mask):
    return df.where(mask).stack().dropna()


print("== activity by calendar day (share of market-minutes with a change) ==")
moved = M.diff().abs() > 1e-9
print((100 * moved.groupby(moved.index.date).mean().mean(axis=1)).round(1).to_dict())

print("\n== Poly leads SIG? corr(Poly 15m change, SIG 15m change starting k min later) ==")
dP = P[cols].diff(15)
for k in (-15, 0, 15, 30, 60, 120, 240):
    dS = Ms.shift(-k).diff(15) if k >= 0 else Ms.shift(-k).diff(15)
    print(f"  k={k:4d}m  corr {corr(dP.iloc[::15].values, dS.iloc[::15].values):+.3f}")
big = dP.abs() >= 0.01
for k in (15, 60, 240):
    resp = (Ms.shift(-k) - Ms) * np.sign(dP)
    r = sel(resp, big) * 100
    print(f"  after Poly moved >=1c in 15m (n={r.size}): SIG follows {r.mean():+.2f}c within {k}m")

print("\n== Gap decomposition: persistent per-market bias vs transient wiggle ==")
G = (P[cols] - Ms) * 100
lvl = G.mean()                                   # per-market average gap over 16h
tr = G - lvl
print(f"  |per-market mean gap| median {lvl.abs().median():.2f}c ; transient part std {np.nanstd(tr.values):.2f}c")
print(f"  share of gap variance that is persistent per-market: {np.nanvar(np.repeat(lvl.values[None,:], len(G), 0)[np.isfinite(G.values)]) / np.nanvar(G.values):.2f}")
t15 = tr.iloc[::15]
ac = corr(t15.values[1:], t15.values[:-1])
print(f"  transient part: 15m autocorr {ac:.3f} -> half-life ~{np.log(.5)/np.log(max(ac,1e-3))*15:.0f} min")
first_half, second_half = G.iloc[: len(G)//2].mean(), G.iloc[len(G)//2:].mean()
print(f"  per-market mean gap 1st half vs 2nd half of window: corr {first_half.corr(second_half):+.2f} "
      f"(1 = the bias is stable), mean |gap| {first_half.abs().mean():.2f} -> {second_half.abs().mean():.2f}c")
lv = pd.cut(Ms.mean(), [0, .1, .3, .7, .9, 1])
print("  per-market mean gap by price level (+ = SIG too low):", lvl.groupby(lv).mean().round(2).to_dict())

print("\n== Jumps (|5m move| >= 3c), fixed ==")
J5 = M.diff(5)
jm = J5.abs() >= 0.03
first = jm & ~jm.shift(1, fill_value=False)
print(f"  events {int(first.values.sum())}")
for h in (15, 60, 240, 720):
    rev = -sel((M.shift(-h) - M) * np.sign(J5), first) * 100
    print(f"  next {h:4d}m: mean give-back {rev.mean():+.2f}c  median {rev.median():+.2f}c  share giving back {(rev>0).mean():.2f}  n={rev.size}")
Jp = first.reindex(P.index)[cols].fillna(False)
pm = (P[cols].diff(5).abs() >= 0.005)
for lab, mask in (("Poly also moved", Jp & pm), ("Poly flat", Jp & ~pm)):
    rv = -sel((Ms.shift(-60) - Ms) * np.sign(Ms.diff(5)), mask) * 100
    tw = sel(np.sign(Ms.diff(5)) * np.sign(P[cols] - Ms), mask)
    print(f"  {lab:16s}: n={rv.size:3d} next-60m give-back {rv.mean():+.2f}c ; jump was TOWARD Poly in {(tw>0).mean():.2f} of cases")

print("\n== D+R parity: how fast does a deviation close, and is it tradable? ==")
S = pd.DataFrame({race: M[r] + M[d] for race, r, d in pairs})
dev = (S - 1) * 100
for thr in (2, 3, 4):
    e = dev.abs() >= thr
    st = e & ~e.shift(1, fill_value=False)
    for h in (60, 240):
        back = sel((dev - dev.shift(-h)) * np.sign(dev), st)
        print(f"  |sum-1|>={thr}c (starts n={back.size:4d}): closes {back.mean():+.2f}c of it within {h}m, share closing {(back>0).mean():.2f}")
print("  most persistent mispriced races (mean sum-1, c):", dev.mean().sort_values().round(2).iloc[[0,1,2,-3,-2,-1]].to_dict())
