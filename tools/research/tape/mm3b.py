import warnings, sys
warnings.filterwarnings("ignore")
import pandas as pd
import mm3_twosided as M
from collections import defaultdict

def run2(univ, BAL, Q_USD, INV_USD, SKEW, pess, split=None):
    inv = defaultdict(float); cash = defaultdict(float); used = defaultdict(float); seen = defaultdict(float)
    vol = 0.0; conc = []; gross = 0.0; lastt = None; fills = 0
    tape = M.tape if split is None else M.tape[(M.tape.ts >= split[0]) & (M.tape.ts < split[1])]
    days = (tape.ts.max() - tape.ts.min()).total_seconds() / 86400
    mids = {}
    for tr in tape.itertuples():
        ex, t = tr.ex, tr.ts.floor("min")
        b = M.book.get((ex, t - pd.Timedelta(minutes=1)))
        if b is None or not (0 < b["bid"] < b["ask"] < 1):
            continue
        sh = M.sharp.get(ex, {}).get(t - pd.Timedelta(minutes=1))
        mid = (b["bid"] + b["ask"]) / 2; mids[ex] = mid
        if univ == "balanced" and (sh is None or abs(sh - mid) > BAL):
            continue
        if (not tr.agg_yes) and tr.price <= b["bid"] + 1e-9:
            which, px, depth, dq = "bid", b["bid"], b["bid_size"], +1
        elif tr.agg_yes and tr.price >= b["ask"] - 1e-9:
            which, px, depth, dq = "ask", b["ask"], b["ask_size"], -1
        else:
            continue
        adding = (dq > 0 and inv[ex] >= 0) or (dq < 0 and inv[ex] <= 0)
        if adding and abs(inv[ex]) * (mid if inv[ex] >= 0 else 1 - mid) >= INV_USD:
            continue
        if adding and SKEW is not None and sh is not None and ((dq > 0 and sh < mid - SKEW) or (dq < 0 and sh > mid + SKEW)):
            continue
        q_quote = Q_USD / max(px if dq > 0 else 1 - px, 0.01)
        key = (ex, t, which)
        if pess:
            seen[key] += tr.qty; avail = max(seen[key] - depth, 0.0) - used[key]
        else:
            avail = tr.qty * q_quote / (depth + q_quote)
        q = max(min(q_quote - used[key], avail), 0.0)
        if q <= 0:
            continue
        used[key] += q; fills += 1
        inv[ex] += dq * q; cash[ex] -= dq * q * px; vol += q * (px if dq > 0 else 1 - px)
        if lastt is None or (tr.ts - lastt).total_seconds() >= 300:
            conc.append(sum(abs(v) * (mids.get(e, .5) if v >= 0 else 1 - mids.get(e, .5)) for e, v in inv.items()))
            lastt = tr.ts
    lm = {e: mids.get(e, .5) for e in inv}
    pnl_mid = sum(cash[e] + inv[e] * lm[e] for e in inv)
    pnl_poly = sum(cash[e] + inv[e] * M.last_sharp.get(e, lm[e]) for e in inv)
    return dict(mid=pnl_mid / days, poly=pnl_poly / days, vol=vol / days, maxcap=max(conc or [0]),
                avgcap=sum(conc) / max(len(conc), 1), fills=fills / days, days=days)

mid_t = M.tape.ts.quantile(0.5)
print(f"{'config':44s} | {'@mid/d':>7} {'@poly/d':>7} {'vol/d':>9} {'max conc cap':>12} {'avg cap':>8} {'%/day@poly on max cap':>22}")
for name, kw in (("balanced 2c, inv $2k, skew 1c", dict(univ="balanced", BAL=0.02, INV_USD=2000.0, SKEW=0.01)),
                 ("all, inv $2k, skew 1c", dict(univ="all", BAL=0, INV_USD=2000.0, SKEW=0.01)),
                 ("all, inv $1k, skew 1c", dict(univ="all", BAL=0, INV_USD=1000.0, SKEW=0.01)),
                 ("all, inv $2k, skew 0.5c", dict(univ="all", BAL=0, INV_USD=2000.0, SKEW=0.005))):
    for Q in (300.0, 1000.0):
        for pess in (True, False):
            r = run2(Q_USD=Q, pess=pess, **kw)
            print(f"{name+f' Q${Q:.0f} '+('pess' if pess else 'opt'):44s} | {r['mid']:7,.0f} {r['poly']:7,.0f} {r['vol']:9,.0f} {r['maxcap']:12,.0f} {r['avgcap']:8,.0f} {100*r['poly']/max(r['maxcap'],1):22.2f}")
print("\nrobustness (all, inv $2k, skew 1c, Q$1000, pess) by half:")
for lab, sp in (("first half", (M.tape.ts.min(), mid_t)), ("second half", (mid_t, M.tape.ts.max() + pd.Timedelta(seconds=1)))):
    r = run2("all", 0, 1000.0, 2000.0, 0.01, True, split=sp)
    print(f"  {lab}: @mid {r['mid']:,.0f}/d  @poly {r['poly']:,.0f}/d  max cap {r['maxcap']:,.0f}")
