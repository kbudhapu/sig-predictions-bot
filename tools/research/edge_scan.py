"""EDGE SCAN (2026-10-09) — systematic search for short-horizon TRADING edges on SIG (not settlement bias).

Per market, per minute: SIG mid/bid/ask/sizes (mid_history), taker flow (trade_tape, ours excluded by time+price
match is skipped — our flow is small vs $15M/day), Polymarket sharp (sharp_history).
Features (all known at t): past returns r1/r5/r15/r60/r240, breakout distance vs 4h/24h high-low, signed taker flow
5/15/60m ($ and trade counts), flow acceleration, touch imbalance, spread, Poly move 5/15/60m, gap to Poly and its
change, volatility, hour of day, trade burst.
Target: forward SIG mid change over h in {5,15,60,240} min, in cents, signed by the bucket's direction.
Method: choose direction + bucket (top/bottom 5% and 10%) on TRAIN (first 60% of time), report TEST (last 40%) mean
move, hit rate, n, and NET = move - cost, where cost = the actual spread paid round trip (half-spread in + out,
measured at t and t+h). A real edge must be positive NET on TEST with the same sign as TRAIN.
Run: SNAP_DB=<snapshot> python tools/research/edge_scan.py
"""
import os
import sqlite3
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
DB = os.environ.get("SNAP_DB", "data/state.db")
HS = (5, 15, 60, 240)


def load():
    c = sqlite3.connect(DB)
    m = pd.read_sql("select exchange_id ex, ts, mid, bid, ask, bid_size bs, ask_size az from mid_history", c)
    m["t"] = pd.to_datetime(m.ts.str[:16], utc=True)
    m = m.drop(columns="ts").groupby(["ex", "t"]).last()
    t = pd.read_sql("select exchange_id ex, created_at, side, price, size from trade_tape", c)
    t["t"] = pd.to_datetime(t.created_at, utc=True, format="ISO8601").dt.floor("min")
    t["usd"] = t.price * t["size"]
    t["sgn"] = np.where(t.side.str.upper() == "YES", 1.0, -1.0) * t.usd
    f = t.groupby(["ex", "t"]).agg(flow=("sgn", "sum"), vol=("usd", "sum"), ntr=("usd", "size"))
    s = pd.read_sql("select exchange_id ex, ts, sharp from sharp_history where sharp is not null", c)
    s["t"] = pd.to_datetime(s.ts.str[:16], utc=True)
    s = s.drop(columns="ts").groupby(["ex", "t"]).last()
    return m, f, s


def panel(m, f, s):
    out = []
    for ex, g in m.groupby(level=0):
        g = g.droplevel(0)
        idx = pd.date_range(g.index.min(), g.index.max(), freq="min")
        g = g.reindex(idx).ffill(limit=3)
        try:
            fl = f.loc[ex].reindex(idx).fillna(0.0)
        except KeyError:
            fl = pd.DataFrame(0.0, index=idx, columns=["flow", "vol", "ntr"])
        try:
            sh = s.loc[ex].reindex(idx).ffill(limit=3)
        except KeyError:
            sh = pd.DataFrame(np.nan, index=idx, columns=["sharp"])
        d = pd.concat([g, fl, sh], axis=1)
        d["ex"] = ex
        mid = d.mid * 100
        d["spread"] = (d.ask - d.bid) * 100
        for k in (1, 5, 15, 60, 240):
            d[f"r{k}"] = mid - mid.shift(k)
        for w in (240, 1440):
            hi, lo = mid.rolling(w, min_periods=w // 2).max().shift(1), mid.rolling(w, min_periods=w // 2).min().shift(1)
            d[f"brk_hi{w}"] = mid - hi
            d[f"brk_lo{w}"] = mid - lo
        for w in (5, 15, 60):
            d[f"flow{w}"] = d.flow.rolling(w).sum()
            d[f"ntr{w}"] = d.ntr.rolling(w).sum()
        d["flow_acc"] = d.flow5 - d.flow60 / 12
        d["burst"] = d.ntr15 / (d.ntr.rolling(1440, min_periods=120).mean() * 15 + 1e-9)
        d["imb"] = (d.bs - d.az) / (d.bs + d.az)
        sp = d.sharp * 100
        for k in (5, 15, 60):
            d[f"poly{k}"] = sp - sp.shift(k)
        d["gap"] = sp - mid
        d["gap_chg60"] = d.gap - d.gap.shift(60)
        d["vol60"] = d.r1.rolling(60).std()
        d["hour"] = d.index.hour
        for h in HS:
            d[f"fwd{h}"] = mid.shift(-h) - mid
            d[f"cost{h}"] = (d.spread + d.spread.shift(-h)) / 2
        out.append(d)
    P = pd.concat(out)
    P.index.name = "t"
    return P.reset_index()


FEATS = ["r1", "r5", "r15", "r60", "r240", "brk_hi240", "brk_lo240", "brk_hi1440", "brk_lo1440", "flow5", "flow15",
         "flow60", "ntr15", "flow_acc", "burst", "imb", "spread", "poly5", "poly15", "poly60", "gap", "gap_chg60",
         "vol60"]


def scan(P):
    P = P.sort_values("t")
    cut = P.t.quantile(0.6)
    tr, te = P[P.t <= cut], P[P.t > cut]
    rows = []
    for ft in FEATS:
        x = tr[ft].dropna()
        if x.nunique() < 20:
            continue
        for q in (0.05, 0.10):
            lo, hi = x.quantile(q), x.quantile(1 - q)
            for side, sel_tr, sel_te in (("low", tr[ft] <= lo, te[ft] <= lo), ("high", tr[ft] >= hi, te[ft] >= hi)):
                for h in HS:
                    a = tr.loc[sel_tr, f"fwd{h}"].dropna()
                    b = te.loc[sel_te, [f"fwd{h}", f"cost{h}"]].dropna()
                    if len(a) < 200 or len(b) < 200:
                        continue
                    sgn = np.sign(a.mean()) or 1.0
                    mv = sgn * b[f"fwd{h}"]
                    cost = b[f"cost{h}"].fillna(0.6).mean() if b[f"cost{h}"].notna().any() else 0.6
                    rows.append(dict(feat=ft, bucket=f"{side}{int(q*100)}", h=h, dir="+" if sgn > 0 else "-",
                                     train=round(sgn * a.mean(), 3), test=round(mv.mean(), 3),
                                     hit=round((mv > 0).mean(), 3), n_te=len(b), cost=round(cost, 3),
                                     net=round(mv.mean() - cost, 3)))
    return pd.DataFrame(rows)


def main():
    m, f, s = load()
    P = panel(m, f, s)
    print(f"panel rows {len(P):,}, markets {P.ex.nunique()}, {P.t.min()} .. {P.t.max()}")
    R = scan(P)
    R.to_csv(os.path.join(os.path.dirname(DB) or ".", "edge_scan_results.csv"), index=False)
    ok = R[(R.train > 0) & (R.test > 0)].sort_values("net", ascending=False)
    pd.set_option("display.width", 200)
    print("\nTOP by NET (test move - round-trip spread), same sign train & test:")
    print(ok.head(30).to_string(index=False))
    print("\nTOP by raw TEST move (maker could avoid the spread):")
    print(ok.sort_values("test", ascending=False).head(15).to_string(index=False))
    P.to_pickle(os.path.join(os.path.dirname(DB) or ".", "edge_panel.pkl"))


if __name__ == "__main__":
    main()
