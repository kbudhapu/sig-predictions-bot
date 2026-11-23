"""What do the players beating us do? Leaderboard sample (top 200 + deciles + around us)."""
import json
import numpy as np, pandas as pd

L = json.load(open("lb.json"))
d = pd.DataFrame(L["rows"]).drop_duplicates("rank").sort_values("rank")
for c in ("pnl", "tradesCount", "volume", "winRate", "roi"):
    d[c] = pd.to_numeric(d[c], errors="coerce")
total, me = L["total"], L["myRank"]
print(f"entrants {total}; our rank {me}; sampled {len(d)} rows; columns {list(d.columns)}")
d["pnl_per_vol"] = 100 * d.pnl / d.volume.replace(0, np.nan)          # cents of P&L per $ traded
d["vol_per_trade"] = d.volume / d.tradesCount.replace(0, np.nan)
cut = round(total * 0.10)
d["band"] = pd.cut(d["rank"], [0, 10, 50, cut, 300, 600, 900, 2000],
                   labels=["top 10", "11-50", f"51-{cut} (top10%)", f"{cut+1}-300", "301-600", "601-900", "900+"])
g = d.groupby("band", observed=True).agg(n=("rank", "size"), pnl_med=("pnl", "median"), trades_med=("tradesCount", "median"),
                                         volume_med=("volume", "median"), vol_per_trade=("vol_per_trade", "median"),
                                         pnl_c_per_vol=("pnl_per_vol", "median"), winrate_med=("winRate", "median"),
                                         roi_med=("roi", "median"))
print(g.round(2).to_string())
us = d[d["rank"] == me]
if len(us):
    print("\nus:", us[["rank", "pnl", "tradesCount", "volume", "winRate", "roi", "pnl_per_vol", "vol_per_trade"]].round(3).to_dict("records"))
print("\nrank correlations within the top 200:")
t = d[d["rank"] <= 200]
print({c: round(t.pnl.corr(t[c], method="spearman"), 2) for c in ("tradesCount", "volume", "winRate", "roi", "vol_per_trade")})
print("\ntop 15:")
print(d.head(15)[["rank", "username", "pnl", "tradesCount", "volume", "winRate", "roi"]].to_string(index=False))
lo = d[(d["rank"] > cut - 15) & (d["rank"] <= cut + 15)]
print(f"\naround the top-10% line (rank ~{cut}):")
print(lo[["rank", "pnl", "tradesCount", "volume", "winRate", "roi"]].to_string(index=False))
