"""TEST 3 — price impact per $ of net aggressive flow (Kyle-lambda style), per market and overall."""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series
warnings.filterwarnings("ignore")

t = load_tape(exclude_ours=False)                    # our own flow moves prices too; keep it for impact
t["min"] = t.ts.dt.floor("min")
t["signed"] = np.where(t.agg_yes, 1.0, -1.0) * t.usd
F = t.pivot_table(index="min", columns="ex", values="signed", aggfunc="sum").fillna(0.0)
M = minute_series("mid_history", "mid")
idx = M.index.intersection(F.index.union(M.index))
F = F.reindex(M.index).fillna(0.0)
cols = [c for c in M.columns if c in F.columns]
dM0 = (M[cols].shift(-1) - M[cols].shift(1)) * 100     # mid move across the trade minute (t-1 -> t+1), cents
dM60 = (M[cols].shift(-60) - M[cols].shift(1)) * 100   # does it stick an hour later?
Fk = F[cols] / 1000.0                                  # $k of net YES-aggressor flow

rows = []
vol = t.groupby("ex").usd.sum()
days = (t.ts.max() - t.ts.min()).total_seconds() / 86400
for ex in cols:
    f, a, b = Fk[ex], dM0[ex], dM60[ex]
    m = (f != 0) & a.notna()
    if m.sum() < 20:
        continue
    lam0 = np.polyfit(f[m], a[m], 1)[0]
    mb = m & b.notna()
    lam60 = np.polyfit(f[mb], b[mb], 1)[0] if mb.sum() >= 20 else np.nan
    rows.append(dict(ex=ex, n_min=int(m.sum()), vol_day=vol.get(ex, 0) / days, lam0=lam0, lam60=lam60,
                     price=float(M[ex].dropna().iloc[-1])))
R = pd.DataFrame(rows)
print(f"markets with >=20 trading minutes: {len(R)}  (window {days:.1f} days)")
print("lambda = cents of mid move per $1k of net aggressive buying (same minute), and what remains 60m later")
print(f"  median lam0 {R.lam0.median():.3f}c/$1k   median lam60 {R.lam60.median():.3f}c/$1k   "
      f"(IQR lam0 {R.lam0.quantile(.25):.3f}..{R.lam0.quantile(.75):.3f})")
R["liq"] = pd.qcut(R.vol_day, 3, labels=["thin", "mid", "thick"])
R["lvl"] = pd.cut(R.price, [0, .15, .85, 1], labels=["<15c", "15-85c", ">85c"])
print("\nby liquidity tercile ($/day traded):")
print(R.groupby("liq", observed=True).agg(n=("ex", "size"), vol_day=("vol_day", "median"), lam0=("lam0", "median"),
                                         lam60=("lam60", "median")).round(3).to_string())
print("\nby price level:")
print(R.groupby("lvl", observed=True).agg(n=("ex", "size"), lam0=("lam0", "median"), lam60=("lam60", "median")).round(3).to_string())

# pooled regression as a robustness check
f = Fk.values.ravel(); a = dM0.values.ravel(); b = dM60.values.ravel()
m = (f != 0) & np.isfinite(a)
print(f"\npooled: lam0 {np.polyfit(f[m], a[m], 1)[0]:.3f}c/$1k (n={m.sum()})", end="")
mb = m & np.isfinite(b)
print(f"   lam60 {np.polyfit(f[mb], b[mb], 1)[0]:.3f}c/$1k")
for usd, lab in ((4400, "cross entry $4.4k"), (980, "parity 1,000 pairs (~$490/leg)"), (490, "")):
    if not lab:
        continue
print("\nImplied average price move caused by OUR order (linear model, median market):")
for usd, lab in ((4400, "cross-market entry, $4.4k in one go"), (2200, "cross-market entry, half ($2.2k)"),
                 (490, "one parity leg (1,000 pairs ~ $490/leg)")):
    print(f"  {lab:40s}: immediate {R.lam0.median()*usd/1000:.2f}c, after 60m {R.lam60.median()*usd/1000:.2f}c "
          f"| thin markets {R[R.liq=='thin'].lam0.median()*usd/1000:.2f}c")
