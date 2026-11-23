"""TEST 1c — how fast would a RESTING YES offer fill in the qualifying markets (underdog, SIG >= Poly + 2c)?"""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, lookup
warnings.filterwarnings("ignore")

t = load_tape(); t = t[~t.ours].copy()
S = minute_series("sharp_history", "sharp"); M = minute_series("mid_history", "mid")
A = minute_series("mid_history", "ask", "AND ask IS NOT NULL")
t["sharp"] = lookup(S, t.ex, t.ts); t["mid"] = lookup(M, t.ex, t.ts, -1); t["ask"] = lookup(A, t.ex, t.ts, -1)
x = t[t.sharp.notna() & t.mid.notna()]
q = x[x.agg_yes & (x.mid >= x.sharp + 0.02) & (x.price < 0.30)].copy()
hours = (x.ts.max() - x.ts.min()).total_seconds() / 3600
print(f"window {hours:.1f}h; YES-buyer trades in qualifying state (underdog <30c, SIG >= Poly+2c): {len(q)} in {q.ex.nunique()} markets")
per = q.groupby("ex").agg(trades=("qty", "size"), yes_shares=("qty", "sum"), usd=("usd", "sum"))
per["trades_per_h"] = per.trades / hours
per["shares_per_h"] = per.yes_shares / hours
print("per market: YES-buy trades/hour p25/p50/p75 =", per.trades_per_h.quantile([.25, .5, .75]).round(2).tolist(),
      "; YES shares bought/hour p25/p50/p75 =", per.shares_per_h.quantile([.25, .5, .75]).round(0).tolist())
# a $4.4k NO position on a 10c market = selling ~4,900 YES shares. How long at our share of flow?
for share in (0.1, 0.25, 0.5):
    need = 4400 / 0.9
    hrs = need / (per.shares_per_h * share)
    print(f"  to sell {need:,.0f} YES (=$4.4k NO at ~10c) capturing {share:.0%} of YES buying: hours needed "
          f"p25 {hrs.quantile(.25):.1f} median {hrs.median():.1f} p75 {hrs.quantile(.75):.1f}")
# did trades print AT the ask (i.e. a resting offer at the touch would have been hit)?
qa = q[q.ask.notna()]
if len(qa):
    print(f"\nwith a recorded ask (since 10-06 10:00): {len(qa)} trades; printed at >= the pre-trade ask: "
          f"{(qa.price >= qa.ask - 1e-9).mean():.2f}; inside the spread: {(qa.price < qa.ask - 1e-9).mean():.2f}")
    print(f"  median price above sharp at fill: {100*(qa.price - qa.sharp).median():.2f}c")
