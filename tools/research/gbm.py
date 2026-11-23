"""Numerai-style GBM on SIG: can past SIG (and Poly) data predict the next 1h / 4h SIG move?
Rows: market x 30-min stamp. Eras: 2-hour blocks. Time split with a purge gap = horizon.
Score: per-era Spearman corr (mean, sharpe), vs one-feature baselines; then tradability in cents."""
import warnings
import numpy as np, pandas as pd, lightgbm as lgb
from scipy.stats import spearmanr
warnings.filterwarnings("ignore")
M = pd.read_pickle("M.pkl"); P = pd.read_pickle("P.pkl"); meta = pd.read_pickle("meta.pkl")
pairs = pd.read_pickle("pairs.pkl")["pairs"]

partner = {}
for race, r, d in pairs:
    partner[r] = d; partner[d] = r
parsum = pd.DataFrame({ex: M[ex] + M[partner[ex]] for ex in M.columns if ex in partner})

feats = {}
for k in (5, 15, 60, 240, 720):
    feats[f"r{k}"] = M.diff(k) * 100
roll60 = M.rolling(60, min_periods=30)
feats["z60"] = (M - roll60.mean()) / (roll60.std() + 1e-4)
feats["dev240"] = (M - M.rolling(240, min_periods=120).mean()) * 100
feats["dev1440"] = (M - M.rolling(1440, min_periods=600).mean()) * 100
feats["p"] = M
feats["pq"] = M * (1 - M)
feats["act60"] = (M.diff().abs() > 1e-9).rolling(60, min_periods=30).sum()
feats["vol240"] = M.diff().abs().rolling(240, min_periods=120).sum() * 100
feats["parity"] = (parsum.reindex(columns=M.columns) - 1) * 100
feats["par_chg60"] = feats["parity"].diff(60)
gap = ((P.reindex(M.index) - M) * 100).reindex(columns=M.columns)
feats["gap"] = gap
feats["gap_chg60"] = gap.diff(60)

STEP = 30
idx = M.index[1440 // 2::STEP]                      # skip the first 12h so long features exist
rows = []
for name, F in feats.items():
    s = F.reindex(idx).stack(future_stack=True).rename(name)
    rows.append(s)
X = pd.concat(rows, axis=1)
X.index.names = ["t", "ex"]
X["hour"] = X.index.get_level_values("t").hour
off = meta.office.astype("category").cat.codes; par = meta.party.astype("category").cat.codes
X["office"] = X.index.get_level_values("ex").map(off).astype(float)
X["party"] = X.index.get_level_values("ex").map(par).astype(float)
for h in (60, 240):
    X[f"y{h}"] = ((M.shift(-h) - M) * 100).reindex(idx).stack(future_stack=True).reindex(X.index).values
X = X[X.p.notna()]
X["era"] = X.index.get_level_values("t").floor("2h")
print(f"rows {len(X)}, eras {X.era.nunique()}, period {X.index.get_level_values('t').min()} .. {X.index.get_level_values('t').max()}")

FEATS = [c for c in X.columns if not c.startswith("y") and c != "era"]
SIG_ONLY = [f for f in FEATS if not f.startswith("gap")]


def era_corr(df, pred, y):
    out = []
    for _, g in df.groupby("era"):
        g = g[[pred, y]].dropna()
        if len(g) > 30 and g[pred].std() > 0 and g[y].std() > 0:
            out.append(spearmanr(g[pred], g[y]).correlation)
    out = np.array(out)
    return out.mean(), out.mean() / (out.std() + 1e-9), len(out)


def run(feat_list, y, data, label):
    d = data[data[y].notna()].copy()
    ts = d.index.get_level_values("t")
    cut = ts.min() + (ts.max() - ts.min()) * 0.6
    tr = d[ts < cut - pd.Timedelta(minutes=int(y[1:]))]
    te = d[ts >= cut]
    model = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=200,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=5, verbose=-1)
    model.fit(tr[feat_list], tr[y])
    te = te.assign(pred=model.predict(te[feat_list]))
    mc, sh, n = era_corr(te, "pred", y)
    print(f"\n[{label}] target {y}: train {len(tr)} rows / test {len(te)} rows ({n} test eras)")
    print(f"   GBM   era-corr mean {mc:+.3f}  sharpe {sh:+.2f}")
    for b, sign in (("r60", -1), ("dev240", -1), ("z60", -1), ("parity", -1), ("gap", +1)):
        if b in feat_list:
            te2 = te.assign(base=sign * te[b])
            bm, bs, _ = era_corr(te2, "base", y)
            print(f"   base {'-' if sign < 0 else '+'}{b:7s} era-corr {bm:+.3f}  sharpe {bs:+.2f}")
    imp = pd.Series(model.booster_.feature_importance("gain"), index=feat_list)
    print("   top features (gain %):", (100 * imp / imp.sum()).sort_values(ascending=False).head(8).round(1).to_dict())
    # tradability: realized move of the top/bottom 10% predictions, in cents, held-side
    q = te.pred.quantile([.1, .9])
    longs, shorts = te[te.pred >= q[.9]], te[te.pred <= q[.1]]
    edge = pd.concat([longs[y], -shorts[y]])
    print(f"   top/bottom-decile trades: mean realized {edge.mean():+.3f}c/share (n={len(edge)}), "
          f"hit {(edge > 0).mean():.2f}; round-trip cost at 1-tick spread = 0.50c")
    return model


for y in ("y60", "y240"):
    run(SIG_ONLY, y, X, "SIG-only features, 3 days")
Xg = X[X.gap.notna()]
for y in ("y60", "y240"):
    run(FEATS, y, Xg, "with Polymarket gap, 16h only")
