"""TEST 6 — where is the liquidity, and do busy markets give better cross-market outcomes (cheaper, faster gaps)?"""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, db
warnings.filterwarnings("ignore")

t = load_tape()
days = (t.ts.max() - t.ts.min()).total_seconds() / 86400
vol = t[~t.ours].groupby("ex").agg(usd=("usd", "sum"), n=("usd", "size"))
vol["usd_day"] = vol.usd / days
tot = vol.usd.sum()
v = vol.sort_values("usd", ascending=False)
cum = v.usd.cumsum() / tot
print(f"window {days:.1f} days, ${tot:,.0f} traded (ex ours) over {len(vol)} markets")
print(f"  top 10 markets = {cum.iloc[9]:.0%} of $ ; top 25 = {cum.iloc[24]:.0%} ; top 50 = {cum.iloc[49]:.0%}")
print("  top 8:", [(ex, f"${u/days:,.0f}/day") for ex, u in v.usd.head(8).items()])

M = minute_series("mid_history", "mid")
S = minute_series("sharp_history", "sharp")
cols = [c for c in S.columns if c in M.columns]
Ms = M.reindex(S.index)[cols]
G = (S[cols] - Ms).abs() * 100
c = db()
dep = pd.read_sql("SELECT exchange_id ex, ask - bid sp, bid_size*bid + ask_size*ask d FROM mid_history WHERE bid_size IS NOT NULL", c)
spd = dep.groupby("ex").agg(spread_c=("sp", lambda s: 100 * s.median()), depth_usd=("d", "median"))

rows = []
for ex in cols:
    g = G[ex].dropna()
    if len(g) < 300:
        continue
    g15 = g.iloc[::15]
    dm = g15 - g15.mean()
    ac = np.corrcoef(dm.values[1:], dm.values[:-1])[0, 1] if len(dm) > 10 else np.nan
    hl = np.log(.5) / np.log(ac) * 15 if 0 < ac < 1 else np.nan
    rows.append(dict(ex=ex, mean_gap=g.mean(), live_share=(g >= 5).mean(), half_life_min=hl,
                     moves=(M[ex].diff().abs() > 1e-9).mean()))
R = pd.DataFrame(rows).set_index("ex").join(vol[["usd_day", "n"]]).join(spd).fillna({"usd_day": 0, "n": 0})
R["liq"] = pd.qcut(R.usd_day.rank(method="first"), 3, labels=["thin", "mid", "busy"])
print("\nCross-market markets (with Polymarket) by liquidity tercile:")
print(R.groupby("liq", observed=True).agg(n=("mean_gap", "size"), usd_day=("usd_day", "median"),
      spread_c=("spread_c", "median"), depth_usd=("depth_usd", "median"), mean_gap_c=("mean_gap", "median"),
      gap5c_share=("live_share", "median"), gap_half_life_min=("half_life_min", "median"),
      mid_moves=("moves", "median")).round(2).to_string())
print("\nRank correlations with $/day traded:",
      {k: round(R.usd_day.corr(R[k], method="spearman"), 2) for k in ("spread_c", "depth_usd", "mean_gap", "half_life_min", "moves")})

# realized outcomes of OUR cross positions by liquidity: signed fills -> P&L marked to now per market
f = pd.read_sql("SELECT exchange_id ex, outcome, price, signed_qty FROM strategy_fills WHERE strategy IN "
                "('cross_market_divergence') AND signed_qty IS NOT NULL", c)
last = M.ffill().iloc[-1]
f["mark"] = np.where(f.outcome == "yes", f.ex.map(last), 1 - f.ex.map(last))
f["pnl"] = f.signed_qty.abs() * np.sign(np.where(f.outcome == "yes", f.signed_qty, -f.signed_qty)) * 0  # placeholder
# YES-delta form: pnl = yes_delta * (mid_now - yes_price)
f["yes_px"] = np.where(f.outcome == "yes", f.price, 1 - f.price)
f["yes_delta"] = np.where(f.outcome == "yes", f.signed_qty, -f.signed_qty)
f["pnl"] = f.yes_delta * (f.ex.map(last) - f.yes_px)
f["cost"] = (f.price * f.signed_qty.abs())
pm = f.groupby("ex").agg(pnl=("pnl", "sum"), cost=("cost", "sum"))
pm = pm.join(R[["liq"]], how="inner")
print("\nOur cross-market ENTRY fills since 10-05 08:27, marked to current SIG mid, by liquidity tercile:")
print(pm.groupby("liq", observed=True).agg(markets=("pnl", "size"), cost=("cost", "sum"), pnl=("pnl", "sum"))
      .assign(pnl_per_100=lambda d: 100 * d.pnl / d.cost).round(1).to_string())
