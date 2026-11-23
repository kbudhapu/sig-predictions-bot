"""TEST 1 — do takers (especially YES buyers) overpay vs Polymarket, and could a resting seller collect it?"""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, lookup
warnings.filterwarnings("ignore")

t = load_tape()
print(f"tape {len(t)} trades, ours matched {int(t.ours.sum())} ({t[t.ours].usd.sum():,.0f}$) -> excluded")
t = t[~t.ours]
S = minute_series("sharp_history", "sharp")
M = minute_series("mid_history", "mid")
t["sharp"] = lookup(S, t.ex, t.ts)
t["mid"] = lookup(M, t.ex, t.ts, -1)               # mid the minute BEFORE the trade (pre-trade fair on SIG)
for h in (60, 240, 1440):
    t[f"mid{h}"] = lookup(M, t.ex, t.ts, h)
x = t[t.sharp.notna() & t.mid.notna()].copy()
print(f"trades with a Polymarket price at the time: {len(x)} (${x.usd.sum():,.0f}) "
      f"{x.ts.min():%m-%d %H:%M} .. {x.ts.max():%m-%d %H:%M}")
days = (x.ts.max() - x.ts.min()).total_seconds() / 86400

# aggressor's outcome space: YES buyer paid p for YES (fair = sharp); NO buyer paid 1-p for NO (fair = 1-sharp)
sgn = np.where(x.agg_yes, 1.0, -1.0)
x["over_sharp"] = sgn * (x.price - x.sharp) * 100          # c/share paid above Polymarket fair
x["over_mid"] = sgn * (x.price - x.mid) * 100              # c/share paid above SIG pre-trade mid
for h in (60, 240, 1440):                                  # maker's mark: what the resting side earns vs later SIG mid
    x[f"mk{h}"] = sgn * (x.price - x[f"mid{h}"]) * 100
x["lvl"] = pd.cut(x.price, [0, .1, .3, .7, .9, 1], labels=["<10c", "10-30", "30-70", "70-90", ">90c"])
x["szb"] = pd.cut(x.usd, [0, 25, 250, 2500, 1e9], labels=["<$25", "$25-250", "$250-2.5k", ">$2.5k"])


def wavg(g, col):
    w = g.usd.where(g[col].notna(), 0)
    return np.average(g[col].fillna(0), weights=w) if w.sum() > 0 else np.nan


def table(by):
    rows = []
    for k, g in x.groupby(by, observed=True):
        rows.append(dict(group=k, n=len(g), usd=round(g.usd.sum()), over_sharp=round(wavg(g, "over_sharp"), 2),
                         share_above_fair=round((g.over_sharp > 0).mean(), 2), over_mid=round(wavg(g, "over_mid"), 2),
                         maker_1h=round(wavg(g, "mk60"), 2), maker_4h=round(wavg(g, "mk240"), 2),
                         maker_24h=round(wavg(g, "mk1440"), 2)))
    return pd.DataFrame(rows).to_string(index=False)


print("\n$-weighted, c/share. over_sharp = taker paid above Polymarket fair (= a maker's EV edge at settlement).")
print("maker_h = what the resting side earns marked to SIG mid h later (includes adverse selection).")
print("\nBy aggressor side (YES = bought YES):"); print(table("agg_yes"))
print("\nBy price level of the market:"); print(table("lvl"))
print("\nBy trade size:"); print(table("szb"))
print("\nYES buyers only, by price level:"); x0 = x; x = x0[x0.agg_yes]; print(table("lvl")); x = x0

# The strategy candidate: rest a YES ASK (sell YES) only where SIG >= sharp, at a price >= sharp + edge.
print("\n== Candidate: resting YES offer only where SIG mid >= Polymarket, filled by YES buyers ==")
for edge in (0.0, 0.01, 0.02, 0.03):
    q = x[x.agg_yes & (x.mid >= x.sharp) & (x.price >= x.sharp + edge)]
    ev = (q.price - q.sharp) * q.qty                      # $ EV at settlement (seller of YES at p, fair sharp)
    mk = {h: ((q.price - q[f"mid{h}"]) * q.qty).sum() for h in (60, 240, 1440)}
    print(f"  ask >= fair+{edge*100:.0f}c: {len(q):5d} trades ${q.usd.sum():>10,.0f} ({q.usd.sum()/days:,.0f}$/day of flow)"
          f" | EV@sharp ${ev.sum():>8,.0f} ({100*ev.sum()/max(q.usd.sum(),1):.2f}c per $) | marked to SIG mid "
          f"1h ${mk[60]:,.0f} 4h ${mk[240]:,.0f} 24h ${mk[1440]:,.0f}")
print(f"  (window {days:.2f} days; capturing all of it is unrealistic — we'd compete with existing resting sellers)")

print("\n== Hour of day (UTC): taker overpay vs Poly, $-weighted ==")
x["hr"] = x.ts.dt.hour
print(x.groupby(x.hr // 3 * 3).apply(lambda g: pd.Series({"n": len(g), "usd": round(g.usd.sum()),
                                                           "over_sharp": round(wavg(g, "over_sharp"), 2)})).to_string())
