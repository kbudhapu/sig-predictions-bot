"""TEST 4 — could the parity shadow's NO-pair trades actually be filled at size? (top-of-book depth)"""
import json, warnings
import numpy as np, pandas as pd
from common import db, minute_series
warnings.filterwarnings("ignore")

c = db()
dep = pd.read_sql("SELECT exchange_id ex, ts, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
print(f"depth rows {len(dep)}  {dep.ts.min()[:16]} .. {dep.ts.max()[:16]}  markets {dep.ex.nunique()}")
print("top-of-book shares, all markets:  bid_size p10/p50/p90 =",
      dep.bid_size.quantile([.1, .5, .9]).round(0).tolist(), "  ask_size p10/p50/p90 =",
      dep.ask_size.quantile([.1, .5, .9]).round(0).tolist())
print("  in $ (size x price): bid", (dep.bid_size * dep.bid).quantile([.1, .5, .9]).round(0).tolist(),
      " ask", (dep.ask_size * dep.ask).quantile([.1, .5, .9]).round(0).tolist())
lvl = pd.cut((dep.bid + dep.ask) / 2, [0, .1, .3, .7, .9, 1])
print("  median shares by price level (bid / ask):",
      {str(k): (round(g.bid_size.median()), round(g.ask_size.median())) for k, g in dep.groupby(lvl, observed=True)})

B = minute_series("mid_history", "bid_size", "AND bid_size IS NOT NULL")
A = minute_series("mid_history", "ask_size", "AND ask_size IS NOT NULL")
ev = [json.loads(l) for l in open("parity_events.jsonl") if l.strip()]
print(f"\nparity shadow events: {len(ev)} ({sum(e['event']=='entry' for e in ev)} entries, {sum(e['event']=='exit' for e in ev)} exits)")


def at(grid, ex, ts):
    t = pd.Timestamp(ts).floor("min")
    if t not in grid.index or ex not in grid.columns:
        return np.nan
    return grid.at[t, ex]


rows = []
for e in ev:
    if e["event"] == "entry":                      # buy NO on both legs = hit both YES bids
        cap = min(at(B, e["d_ex"], e["ts"]), at(B, e["r_ex"], e["ts"]))
    else:                                          # sell both NOs = hit both YES asks
        cap = min(at(A, e["d_ex"], e["ts"]), at(A, e["r_ex"], e["ts"]))
    rows.append(dict(event=e["event"], race=e["race"], cap=cap, edge=e.get("entry_edge"), pnl=e.get("exit_pnl")))
P = pd.DataFrame(rows)
for evn, g in P.groupby("event"):
    v = g.cap.dropna()
    print(f"  {evn:5s}: n={len(g)} with depth {len(v)} | fillable pairs at top: p10 {v.quantile(.1):.0f} "
          f"median {v.median():.0f} p90 {v.quantile(.9):.0f} | share >=1000 pairs {(v >= 1000).mean():.2f}, "
          f">=250 {(v >= 250).mean():.2f}")

ent = P[P.event == "entry"].dropna(subset=["cap"])
exi = P[P.event == "exit"].dropna(subset=["cap"])
days = (pd.Timestamp(ev[-1]["ts"]) - pd.Timestamp(ev[0]["ts"])).total_seconds() / 86400
if len(exi):
    for size in (250, 500, 1000):
        realized = (exi.pnl * np.minimum(exi.cap, size)).sum()
        print(f"  realized at target {size:4d} pairs, capped by top-of-book: ${realized:,.0f} over {days:.2f}d "
              f"(${realized/days:,.0f}/day) vs uncapped ${exi.pnl.sum()*size/days:,.0f}/day")
locked = (ent.edge * np.minimum(ent.cap, 1000)).sum()
print(f"  edge locked at entry (target 1000, capped by depth): ${locked:,.0f} across {len(ent)} entries")
