"""How does SIG move? Read-only study on a snapshot of state.db (mid_history 3d, sharp_history 16h)."""
import re, sqlite3, warnings
import numpy as np, pandas as pd, yaml
warnings.filterwarnings("ignore")
pd.set_option("display.width", 200); pd.set_option("display.max_columns", 30)
DB = "snap.db"
db = sqlite3.connect(DB)
TICK = 0.005

# ---------- metadata
races = yaml.safe_load(open("race_mapping.yaml", encoding="utf-8"))["races"]
meta = []
for key, r in races.items():
    ex = (r.get("sig") or {}).get("exchange_id")
    if ex is None:
        continue
    party = "rep" if key.endswith("_rep") else "dem" if key.endswith("_dem") else "other"
    race = re.sub(r"_(rep|dem|ind|other)$", "", key)
    office = ("control" if "control" in key else "senate" if "senate" in key else
              "governor" if "governor" in key else "house" if re.search(r"(house|_\d{2}$|district|_at_large)", race) else "other")
    meta.append(dict(ex=str(ex), key=key, race=race, party=party, office=office))
meta = pd.DataFrame(meta).drop_duplicates("ex").set_index("ex")

# ---------- SIG mids, per minute grid
mh = pd.read_sql("SELECT exchange_id ex, ts, mid FROM mid_history", db)
mh["t"] = pd.to_datetime(mh.ts.str[:16], utc=True)
M = mh.pivot_table(index="t", columns="ex", values="mid", aggfunc="last").sort_index()
M = M.asfreq("1min").ffill(limit=5)
print(f"SIG grid: {M.shape[1]} markets x {M.shape[0]} minutes  {M.index[0]} .. {M.index[-1]}")
meta = meta.reindex(M.columns)
meta["office"] = meta["office"].fillna("unmapped"); meta["party"] = meta["party"].fillna("?")

# ---------- A. universe
print("\n== A. UNIVERSE ==")
last = M.ffill().iloc[-1]
print("markets by office:", meta.office.value_counts().to_dict())
print("price level of markets now:", pd.cut(last, [0, .05, .15, .35, .65, .85, .95, 1]).value_counts().sort_index().to_dict())
pr = pd.read_sql("SELECT exchange_id ex, best_bid, best_ask, bid_size, ask_size FROM prices", db).set_index("ex")
pr["spread_c"] = 100 * (pr.best_ask - pr.best_bid)
print("spread now (c): ", pr.spread_c.describe()[["25%", "50%", "75%", "max"]].round(2).to_dict(),
      " share at 1 tick(0.5c):", round((pr.spread_c <= 0.51).mean(), 2))
print("top-of-book depth (shares) median bid/ask:", pr.bid_size.median(), pr.ask_size.median())

# ---------- B. activity / move sizes
print("\n== B. HOW OFTEN AND HOW MUCH SIG MOVES ==")
D1 = M.diff()
moved = (D1.abs() > 1e-9)
print("share of market-minutes with any mid change:", round(moved.values[~np.isnan(D1.values)].mean(), 3))
sz = (D1.abs()[moved] / TICK).stack()
print("size of 1-min moves in ticks (0.5c):", sz.round().clip(upper=10).value_counts(normalize=True).sort_index().round(3).to_dict())
for h in (60, 240, 1440):
    Dh = M.diff(h).stack()
    print(f"  |change| over {h:4d}m: median {100*Dh.abs().median():.2f}c  p90 {100*Dh.abs().quantile(.9):.2f}c  "
          f"p99 {100*Dh.abs().quantile(.99):.2f}c  kurtosis {Dh.kurt():.1f}")
path = D1.abs().sum(); rng = M.max() - M.min()
print(f"3-day PATH vs RANGE per market (median): path {100*path.median():.1f}c, range {100*rng.median():.1f}c, "
      f"efficiency {(rng/path).median():.3f}  (1.0 = straight line, ~0 = pure back-and-forth)")
act = moved.groupby(moved.index.hour).mean().mean(axis=1)
vol = M.diff(60).abs().groupby(M.index.hour).mean().mean(axis=1) * 100
print("by UTC hour  act%:", (100 * act).round(1).to_dict())
print("by UTC hour 60m |chg| c:", vol.round(2).to_dict())
print("by weekday act%:", (100 * moved.groupby(moved.index.dayofweek).mean().mean(axis=1)).round(1).to_dict())
by_off = pd.DataFrame({"act": moved.mean(), "vol60_c": 100 * M.diff(60).abs().mean(), "office": meta.office})
print("by office:\n", by_off.groupby("office").median().round(3))

# ---------- C. memory: autocorrelation + variance ratio
print("\n== C. DOES SIG TREND OR REVERT? ==")
def pooled_ac(D, lag):
    a, b = D.values[lag:], D.shift(lag).values[lag:]
    m = ~np.isnan(a) & ~np.isnan(b)
    return np.corrcoef(a[m], b[m])[0, 1]
for k in (1, 5, 15, 60):
    Dk = M.diff(k).iloc[::k]
    print(f"  autocorr of {k:3d}m changes vs previous {k:3d}m: {pooled_ac(Dk, 1):+.3f}")
v1 = np.nanvar(D1.values)
for k in (5, 15, 60, 240, 720):
    vk = np.nanvar(M.diff(k).iloc[::k].values)
    print(f"  variance ratio VR({k:3d}) = {vk / (k * v1):.3f}   (1 = random walk, <1 = noise that reverts, >1 = trends)")
# mid bounce: does a 1-tick move undo within 5 min?
nxt = M.shift(-5) - M
up = D1 > 1e-9; dn = D1 < -1e-9
print(f"  after a 1-min UP move, next 5m mean {100*nxt[up].stack().mean():+.3f}c; after DOWN {100*nxt[dn].stack().mean():+.3f}c")

# ---------- D. cross-section: D+R pairs and common factor
print("\n== D. HOW SIG RELATES TO ITSELF ==")
pairs = []
for race, g in meta.reset_index().groupby("race"):
    r = g[g.party == "rep"].ex.tolist(); d = g[g.party == "dem"].ex.tolist()
    if r and d and r[0] in M and d[0] in M:
        pairs.append((race, r[0], d[0]))
S = pd.DataFrame({race: M[r] + M[d] for race, r, d in pairs})
dev = (S - 1) * 100
print(f"D+R pairs: {len(pairs)}. sum-1 (c): mean {dev.stack().mean():+.2f}, median {dev.stack().median():+.2f}, "
      f"|dev|>2c {(dev.abs() > 2).stack().mean():.2f}, >4c {(dev.abs() > 4).stack().mean():.3f}")
d15 = dev.iloc[::15]
ac = pooled_ac(d15 - d15.mean(), 1)
print(f"  parity deviation persistence: 15m autocorr {ac:.3f} -> half-life ~{np.log(.5)/np.log(max(ac,1e-3))*15:.0f} min")
cr = [M[r].diff(15).corr(M[d].diff(15)) for _, r, d in pairs]
print(f"  corr of 15m changes R vs D leg: median {np.nanmedian(cr):+.2f} (efficient = -1.00)")
# lead/lag between legs: does R move first?
R60 = M.diff(60).iloc[::60].dropna(axis=1, how="all")
R60 = R60.loc[:, R60.notna().mean() > .9].fillna(0)
X = (R60 - R60.mean()) / (R60.std() + 1e-9)
u, s, vt = np.linalg.svd(X.values, full_matrices=False)
ve = s ** 2 / (s ** 2).sum()
print(f"  PCA of 60m changes ({X.shape[1]} mkts): PC1 {ve[0]:.2f}, PC2 {ve[1]:.2f}, PC3 {ve[2]:.2f} of variance")
load = pd.Series(vt[0], index=X.columns)
print("  PC1 loading mean by party:", load.groupby(meta.party.reindex(load.index)).mean().round(3).to_dict())
# national-swing check: avg 60m change of all D legs vs all R legs
dl = [c for c in X.columns if meta.party.get(c) == "dem"]; rl = [c for c in X.columns if meta.party.get(c) == "rep"]
print(f"  corr(avg D-leg move, avg R-leg move) over 60m: {R60[dl].mean(1).corr(R60[rl].mean(1)):+.2f}")
# correlation across races (excluding own pair): typical pairwise corr of D legs
cd = R60[dl].corr().values; iu = np.triu_indices_from(cd, 1)
print(f"  typical corr between DIFFERENT races' D legs (60m): median {np.nanmedian(cd[iu]):+.3f}")

# ---------- E. relation to Polymarket
print("\n== E. HOW SIG RELATES TO POLYMARKET (16h) ==")
sh = pd.read_sql("SELECT exchange_id ex, ts, sharp FROM sharp_history", db)
sh["t"] = pd.to_datetime(sh.ts.str[:16], utc=True)
P = sh.pivot_table(index="t", columns="ex", values="sharp", aggfunc="last").sort_index().asfreq("1min").ffill(limit=5)
cols = [c for c in P.columns if c in M.columns]
P = P[cols]; Ms = M.reindex(P.index)[cols]
G = (P - Ms) * 100
g = G.stack()
print(f"gap = Poly - SIG (c): mean {g.mean():+.2f}, median |gap| {g.abs().median():.2f}, p90 |gap| {g.abs().quantile(.9):.2f}")
lvl = pd.cut(Ms.stack(), [0, .1, .3, .7, .9, 1])
print("  mean |gap| by SIG price level:", g.abs().groupby(lvl).mean().round(2).to_dict())
print("  mean signed gap by level (+ = SIG below Poly):", g.groupby(lvl).mean().round(2).to_dict())
print("  mean |gap| by office:", G.abs().mean().groupby(meta.office.reindex(G.columns)).mean().round(2).to_dict())
g15 = G.iloc[::15]
ac = pooled_ac(g15 - g15.mean(), 1)
print(f"  gap persistence: 15m autocorr {ac:.3f} -> half-life ~{np.log(.5)/np.log(max(ac,1e-3))*15:.0f} min")
print(f"  Poly 60m |chg| {100*P.diff(60).abs().stack().mean():.2f}c vs SIG {100*Ms.diff(60).abs().stack().mean():.2f}c")
for lag in (0, 5, 15, 30, 60, 120):
    a = P.diff(15).iloc[::15]; b = Ms.diff(15).shift(-lag // 15 if lag else 0).iloc[::15] if lag else Ms.diff(15).iloc[::15]
    b = Ms.diff(15).shift(-lag).iloc[::15]
    m = a.stack().index.intersection(b.stack().index)
    print(f"  corr(Poly 15m chg, SIG 15m chg {lag:3d}m later): {np.corrcoef(a.stack()[m], b.stack()[m])[0,1]:+.3f}")
# does the gap close via SIG or via Poly?
fut = 240
dS = (Ms.shift(-fut) - Ms) * 100; dP = (P.shift(-fut) - P) * 100
big = G.abs() >= 5
sgn = np.sign(G)
print(f"  when |gap|>=5c: next {fut}m SIG moves toward Poly {(dS*sgn)[big].stack().mean():+.2f}c, "
      f"Poly moves toward SIG {(-dP*sgn)[big].stack().mean():+.2f}c")

# ---------- F. jumps
print("\n== F. JUMPS (|5m move| >= 3c) ==")
J5 = M.diff(5)
jumps = J5.abs() >= 0.03
first = jumps & ~jumps.shift(1, fill_value=False)
print(f"jump events: {int(first.values.sum())} over {M.shape[1]} mkts / {(M.index[-1]-M.index[0]).total_seconds()/86400:.1f} d")
for h in (15, 60, 240):
    after = (M.shift(-h) - M)
    rev = -(after * np.sign(J5))[first].stack() * 100
    print(f"  after a jump, next {h:3d}m: mean reversal {rev.mean():+.2f}c (share reverting {(rev>0).mean():.2f}, n={rev.size})")
# jumps with vs without Poly move (16h window only)
Jp = first.reindex(P.index)[cols]
pm = P.diff(5).abs() >= 0.01
after = (Ms.shift(-60) - Ms); sg = np.sign(Ms.diff(5))
for lab, mask in (("Poly also moved >=1c", Jp & pm), ("Poly flat", Jp & ~pm)):
    rv = -(after * sg)[mask].stack() * 100
    print(f"  {lab:20s}: n={rv.size:3d} next-60m reversal {rv.mean():+.2f}c")

M.to_pickle("M.pkl"); P.to_pickle("P.pkl"); meta.to_pickle("meta.pkl")
pd.Series(dict(pairs=[(a, b, c) for a, b, c in pairs])).to_pickle("pairs.pkl")
