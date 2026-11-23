"""BREAK-TEST of the recycling idea: what happens to EVERY maker entry, not just the ones that recycle.

1) CONVERGENCE: for every simulated entry fill (YES bought by joining the bid at <= fair - w_in, or NO by joining the
   ask side), track the HELD leg's bid afterwards (YES: best_bid; NO: 1 - best_ask) minute by minute. Report, per
   horizon (30m, 1h, 2h, 4h, 12h, 24h): P(bid >= cost), P(bid >= cost+1c), P(bid >= fair_at_entry-1c), and the
   distribution of (bid - cost) at the horizon. "Never recovered" = bid < cost for the rest of the data.
2) FULL ACCOUNTING of the recycle strategy: every position still open at the end is LIQUIDATED AT THE BID (not
   marked at mid), so losers kept by the breakeven stop are counted. Also: exposure over time (does stuck capital
   keep growing?), share of capital-hours spent in positions under water.
"""
import collections
import warnings

import numpy as np
import pandas as pd

from common import db, load_tape, minute_series

warnings.filterwarnings("ignore")
TICK = 0.005
c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, mid, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
d = d.drop_duplicates(["ex", "t"], keep="last")
BID = d.pivot_table(index="t", columns="ex", values="bid", aggfunc="last").sort_index().asfreq("1min").ffill(limit=5)
ASK = d.pivot_table(index="t", columns="ex", values="ask", aggfunc="last").sort_index().asfreq("1min").ffill(limit=5)
book = {(r.ex, r.t): r for r in d.itertuples(index=False)}
S = minute_series("sharp_history", "sharp")
tape = load_tape()
t0, t1 = d.t.min(), d.t.max()
tape = tape[(tape.ts >= t0 + pd.Timedelta(minutes=1)) & (tape.ts <= t1) & ~tape.ours & tape.ex.isin(S.columns)].sort_values("ts")
days = (tape.ts.max() - tape.ts.min()).total_seconds() / 86400
vol = tape.groupby("ex").usd.sum() / days
liquid = set(vol[vol >= vol.quantile(2 / 3)].index)
fair = {ex: S[ex].dropna() for ex in S.columns}


def fl(x):
    return np.floor(x / TICK + 1e-9) * TICK


def ce(x):
    return np.ceil(x / TICK - 1e-9) * TICK


def entries(w_in, universe):
    """Every maker ENTRY fill opportunity (pessimistic back-of-queue), independent of inventory limits — the raw
    population whose fate we want: (ex, ts, leg 'yes'/'no', cost, fair_leg)."""
    seen = {}
    out = []
    for r in tape.itertuples(index=False):
        ex = r.ex
        if universe == "liquid" and ex not in liquid:
            continue
        snap = r.ts.floor("min") - pd.Timedelta(minutes=1)
        b = book.get((ex, snap))
        f = fair.get(ex)
        if b is None or f is None or not (0 < b.bid < b.ask < 1):
            continue
        fi = f.index.searchsorted(snap, side="right") - 1
        if fi < 0 or (snap - f.index[fi]) > pd.Timedelta(minutes=5):
            continue
        fv = float(f.iloc[fi])
        if r.agg_yes:                                   # YES buyer lifts our ASK => we BUY NO at 1-ask
            px = max(ce(fv + w_in), b.ask)
            if r.price < px - 1e-9:
                continue
            key, depth = (ex, snap, "a"), b.ask_size or 0
            leg, cost, fair_leg = "no", 1 - px, 1 - fv
        else:                                           # YES seller hits our BID => we BUY YES
            px = min(fl(fv - w_in), b.bid)
            if r.price > px + 1e-9:
                continue
            key, depth = (ex, snap, "b"), b.bid_size or 0
            leg, cost, fair_leg = "yes", px, fv
        before = seen.get(key, 0.0); after = before + r.qty; seen[key] = after
        if after - max(depth, before) <= 0.5:          # back of queue: not filled yet
            continue
        if any(e[0] == ex and e[2] == leg and (r.ts - e[1]).total_seconds() < 300 for e in out[-50:]):
            continue                                    # one entry per market/leg per 5 min (dedupe sweeps)
        out.append((ex, r.ts, leg, round(cost, 3), fair_leg))
    return out


def held_bid(ex, t, leg):
    """The held leg's bid series from t on (YES: best bid; NO: 1 - best ask)."""
    if leg == "yes":
        s = BID[ex] if ex in BID else None
    else:
        s = (1 - ASK[ex]) if ex in ASK else None
    if s is None:
        return None
    return s[s.index >= t.floor("min")].dropna()


H = [30, 60, 120, 240, 720, 1440]
for universe, w_in in (("liquid", 0.03), ("liquid", 0.02), ("all", 0.03)):
    E = entries(w_in, universe)
    rows = []
    for ex, t, leg, cost, fl_ in E:
        s = held_bid(ex, t, leg)
        if s is None or len(s) < 2:
            continue
        avail = (s.index[-1] - s.index[0]).total_seconds() / 60
        rec = {"ex": ex, "t": t, "cost": cost, "avail_min": avail}
        for h in H:
            w = s[s.index <= t + pd.Timedelta(minutes=h)]
            rec[f"be_{h}"] = bool((w >= cost - 1e-9).any())
            rec[f"p1_{h}"] = bool((w >= cost + 0.01 - 1e-9).any())
            rec[f"fair_{h}"] = bool((w >= fl_ - 0.01 - 1e-9).any())
            at = s[s.index <= t + pd.Timedelta(minutes=h)]
            rec[f"pnl_{h}"] = (float(at.iloc[-1]) - cost) if len(at) and avail >= h else np.nan
        rec["ever_be"] = bool((s >= cost - 1e-9).any())
        rec["end_pnl"] = float(s.iloc[-1]) - cost
        rows.append(rec)
    R = pd.DataFrame(rows)
    print(f"\n=== {universe} w_in={w_in*100:.0f}c: {len(R)} entry fills ({R.ex.nunique()} markets) ===")
    print("horizon | P(bid>=cost) P(bid>=cost+1c) P(bid>=fair-1c) | bid-cost at horizon: mean  p10  p50  p90 (n)")
    for h in H:
        sub = R[R.avail_min >= h]                    # only entries with the full horizon observed
        if not len(sub):
            continue
        x = sub[f"pnl_{h}"].dropna() * 100
        print(f"{h/60:5.1f}h  | {sub[f'be_{h}'].mean():9.2f} {sub[f'p1_{h}'].mean():14.2f} {sub[f'fair_{h}'].mean():14.2f} | "
              f"{x.mean():+6.2f}c {x.quantile(.1):+6.2f} {x.quantile(.5):+6.2f} {x.quantile(.9):+6.2f}  ({len(sub)})")
    print(f"NEVER back to cost (within the data): {(~R.ever_be).mean():.2f}   |   at end of data: bid-cost mean "
          f"{100*R.end_pnl.mean():+.2f}c, share under water {(R.end_pnl < 0).mean():.2f}")
