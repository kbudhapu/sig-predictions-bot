"""MM-1 — what did our REAL market making do? Signed fills (since 10-05 08:27): spread captured, markouts, P&L."""
import warnings
import numpy as np, pandas as pd
from common import db, minute_series, lookup
warnings.filterwarnings("ignore")

c = db()
f = pd.read_sql("SELECT strategy, exchange_id ex, outcome, price, signed_qty, filled_at, mid_at_fill FROM strategy_fills "
                "WHERE signed_qty IS NOT NULL AND strategy IN ('market_making','UNATTRIBUTED')", c)
f["ts"] = pd.to_datetime(f.filled_at, utc=True, format="ISO8601")
f["yes_px"] = np.where(f.outcome == "yes", f.price, 1 - f.price)
f["yes_delta"] = np.where(f.outcome == "yes", f.signed_qty, -f.signed_qty)     # + = we bought YES exposure
f["usd"] = (f.price * f.signed_qty.abs())
M = minute_series("mid_history", "mid")
S = minute_series("sharp_history", "sharp")
f["mid0"] = lookup(M, f.ex, f.ts, -1)
for h in (5, 60, 240, 1440):
    f[f"m{h}"] = lookup(M, f.ex, f.ts, h)
f["sharp"] = lookup(S, f.ex, f.ts)
last = M.ffill().iloc[-1]
f["mid_now"] = f.ex.map(last)
d = np.sign(f.yes_delta)
f["spread_c"] = d * (f.mid0 - f.yes_px) * 100                 # + = we bought below / sold above the mid (edge captured)
for h in (5, 60, 240, 1440):
    f[f"mo{h}"] = d * (f[f"m{h}"] - f.yes_px) * 100           # markout vs our fill price (spread + drift)
f["vs_sharp"] = d * (f.sharp - f.yes_px) * 100                # EV edge vs Polymarket at fill
f["pnl_now"] = f.yes_delta * (f.mid_now - f.yes_px)
days = (f.ts.max() - f.ts.min()).total_seconds() / 86400
print(f"MM/unattributed signed fills: {len(f)}  ${f.usd.sum():,.0f} traded over {days:.2f} days "
      f"({f.ts.min():%m-%d %H:%M} .. {f.ts.max():%m-%d %H:%M}); mostly before MM was switched off")


def w(col, g):
    v = g[col]; m = v.notna()
    return np.average(v[m], weights=g.usd[m]) if m.any() else np.nan


print("\n$-weighted c/share (+ good for us):")
print(f"  spread captured vs pre-fill mid: {w('spread_c', f):+.3f}")
for h in (5, 60, 240, 1440):
    print(f"  markout after {h:4d}m (spread + subsequent drift): {w(f'mo{h}', f):+.3f}   n={f[f'mo{h}'].notna().sum()}")
print(f"  edge vs Polymarket at fill (where known, n={f.vs_sharp.notna().sum()}): {w('vs_sharp', f):+.3f}")
print(f"  P&L of these fills marked to current mid: ${f.pnl_now.sum():,.0f}  (${f.pnl_now.sum()/days:,.0f}/day)")
f["agrees"] = np.sign(f.sharp - f.mid0) == d                  # fill was on the side Polymarket favours
g = f[f.sharp.notna()]
for lab, s in (("fills on Poly-favoured side", g[g.agrees]), ("fills against Poly", g[~g.agrees])):
    print(f"  {lab:28s} n={len(s):4d} ${s.usd.sum():>8,.0f} spread {w('spread_c', s):+.2f} "
          f"mo1h {w('mo60', s):+.2f} mo4h {w('mo240', s):+.2f} mo24h {w('mo1440', s):+.2f} vs_sharp {w('vs_sharp', s):+.2f}")
f["lvl"] = pd.cut(f.yes_px, [0, .1, .3, .7, .9, 1])
print("\nby price level:")
print(f.groupby("lvl", observed=True).apply(lambda g: pd.Series({"n": len(g), "usd": round(g.usd.sum()),
      "spread": round(w("spread_c", g), 2), "mo1h": round(w("mo60", g), 2), "mo4h": round(w("mo240", g), 2),
      "pnl_now": round(g.pnl_now.sum())})).to_string())
