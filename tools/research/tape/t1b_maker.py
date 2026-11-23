"""TEST 1b — the resting-YES-seller candidate, normalized by the SELLER's capital and robust to bad gaps."""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, lookup
warnings.filterwarnings("ignore")

t = load_tape(); t = t[~t.ours].copy()
S = minute_series("sharp_history", "sharp"); M = minute_series("mid_history", "mid")
t["sharp"] = lookup(S, t.ex, t.ts); t["mid"] = lookup(M, t.ex, t.ts, -1)
for h in (60, 240, 1440):
    t[f"mid{h}"] = lookup(M, t.ex, t.ts, h)
x = t[t.sharp.notna() & t.mid.notna() & t.agg_yes].copy()          # YES buyers = who a resting YES seller meets
days = (x.ts.max() - x.ts.min()).total_seconds() / 86400
x["gap"] = x.price - x.sharp
print(f"YES-buyer trades with a sharp: {len(x)}  ({days:.2f} days)")
print("  |price - sharp| distribution (c): p50 %.1f p90 %.1f p99 %.1f max %.1f" %
      tuple(100 * x.gap.abs().quantile([.5, .9, .99, 1]).values))
big = x[x.gap.abs() > 0.25].groupby("ex").agg(n=("qty", "size"), med_gap=("gap", "median"))
print(f"  trades with |gap| > 25c (likely mapping/benchmark issues, excluded): {int(big.n.sum())} in markets {big.index.tolist()}")
x = x[x.gap.abs() <= 0.25]

# Seller of YES at p: capital at risk per share = 1 - p; EV at sharp = p - sharp; mark at later mid = p - mid_h
x["cap"] = (1 - x.price) * x.qty
x["ev"] = x.gap * x.qty
for h in (60, 240, 1440):
    x[f"mk{h}"] = (x.price - x[f"mid{h}"]) * x.qty
x["lvl"] = pd.cut(x.price, [0, .1, .3, .5, .7, .9, 1], labels=["<10c", "10-30", "30-50", "50-70", "70-90", ">90"])


def rep(q, lab):
    if q.empty:
        print(f"  {lab}: none"); return
    cap = q.cap.sum()
    s = (f"  {lab:34s} n={len(q):5d} sold ${q.usd.sum():>9,.0f} capital ${cap:>10,.0f} | EV@sharp {100*q.ev.sum()/cap:+.2f}% "
         f"| marked 1h {100*q.mk60.sum()/cap:+.3f}% 4h {100*q.mk240.sum()/cap:+.3f}% 24h {100*q.mk1440.sum()/cap:+.3f}%")
    print(s)


print("\nReturns as % of the SELLER's capital (1-p per share). EV@sharp = settlement value if Polymarket is right.")
print("== All YES buyers (a seller who filled every one) ==")
for k, g in x.groupby("lvl", observed=True):
    rep(g, f"price {k}")
print("\n== Candidate rule: only where SIG mid >= sharp + edge (we'd only offer YES that's overpriced vs Poly) ==")
for edge in (0.0, 0.01, 0.02, 0.03):
    q = x[(x.mid >= x.sharp + edge) & (x.price >= x.sharp + edge)]
    rep(q, f"edge >= {edge*100:.0f}c, all prices")
    rep(q[q.price < 0.3], f"edge >= {edge*100:.0f}c, price < 30c")
q = x[(x.mid >= x.sharp + 0.02) & (x.price >= x.sharp + 0.02)]
by = q.groupby("ex").agg(n=("qty", "size"), usd=("usd", "sum"), ev=("ev", "sum"), cap=("cap", "sum"), mk24=("mk1440", "sum"))
by["ev_pct"] = 100 * by.ev / by.cap
print(f"\nconcentration (edge>=2c): {len(by)} markets; top 5 markets = {by.ev.nlargest(5).sum()/by.ev.sum():.0%} of EV")
print(by.sort_values("ev", ascending=False).head(8).round(1).to_string())
flow = q.usd.sum() / days
print(f"\nflow meeting the edge>=2c rule: ${flow:,.0f}/day of YES bought. If we were the resting seller on 10% of it:"
      f" ~${0.1*q.cap.sum()/days:,.0f}/day of capital deployed, EV ~${0.1*q.ev.sum()/days:,.0f}/day,"
      f" marked-to-24h ~${0.1*q.mk1440.sum()/days:,.0f}/day")
