"""MM-3 TWO-SIDED NETTING MM (2026-10-09). Leaders turn 2-15x equity/day -> they must round-trip within the day.
On SIG, buying YES at the bid and buying NO at (1 - ask) NETS to $1 cash per pair instantly: +spread per pair, capital
returned. Quote BOTH sides at the touch; signed YES inventory capped at INV_USD (beyond it only the reducing side quotes).
Universe: 'balanced' (|Poly - SIG mid| <= BAL) or 'all'; 'skew' = never ADD inventory on the side Poly disfavours by
more than SKEW. Fills: real tape, ours excluded; queue pessimistic (behind displayed) or pro-rata.
P&L = cash + inventory marked at end (SIG mid; and at Poly). Capital = peak |inventory cost|. Turnover = volume/capital."""
import warnings
from collections import defaultdict
import numpy as np, pandas as pd
from common import db, load_tape, minute_series
warnings.filterwarnings("ignore")
c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
book = d.drop_duplicates(["ex", "t"], keep="last").set_index(["ex", "t"]).to_dict("index")
S = minute_series("sharp_history", "sharp")
sharp = {ex: S[ex].dropna().to_dict() for ex in S.columns}
t0 = d.t.min()
tape = load_tape()
tape = tape[(tape.ts >= t0 + pd.Timedelta(minutes=1)) & ~tape.ours].sort_values("ts")
days = (tape.ts.max() - tape.ts.min()).total_seconds() / 86400
lastb = d.sort_values("t").groupby("ex").last()
last_mid = ((lastb.bid + lastb.ask) / 2).to_dict()
last_sharp = {ex: S[ex].dropna().iloc[-1] for ex in S.columns if S[ex].notna().any()}
print(f"window {days:.2f}d, {len(tape)} trades")


def run(univ="balanced", BAL=0.015, Q_USD=300.0, INV_USD=1000.0, SKEW=None, pess=True):
    inv = defaultdict(float); cash = defaultdict(float); used = defaultdict(float); seen = defaultdict(float)
    vol = 0.0; peak = defaultdict(float); fills = 0
    for tr in tape.itertuples():
        ex, t = tr.ex, tr.ts.floor("min")
        b = book.get((ex, t - pd.Timedelta(minutes=1)))
        if b is None or not (0 < b["bid"] < b["ask"] < 1):
            continue
        sh = sharp.get(ex, {}).get(t - pd.Timedelta(minutes=1))
        mid = (b["bid"] + b["ask"]) / 2
        if univ == "balanced" and (sh is None or abs(sh - mid) > BAL):
            continue
        # YES-sellers hit our YES bid (we buy YES at bid); YES-buyers lift our YES ask (we sell YES = buy NO at 1-ask)
        if (not tr.agg_yes) and tr.price <= b["bid"] + 1e-9:
            which, px, depth, dq = "bid", b["bid"], b["bid_size"], +1
        elif tr.agg_yes and tr.price >= b["ask"] - 1e-9:
            which, px, depth, dq = "ask", b["ask"], b["ask_size"], -1
        else:
            continue
        cost_now = inv[ex] * mid
        adding = (dq > 0 and inv[ex] >= 0) or (dq < 0 and inv[ex] <= 0)
        if adding and abs(inv[ex]) * (mid if inv[ex] >= 0 else 1 - mid) >= INV_USD:
            continue
        if adding and SKEW is not None and sh is not None:
            if dq > 0 and sh < mid - SKEW:      # buying YES while Poly says YES is overpriced
                continue
            if dq < 0 and sh > mid + SKEW:      # buying NO while Poly says YES is cheap
                continue
        q_quote = Q_USD / max(px if dq > 0 else 1 - px, 0.01)
        key = (ex, t, which)
        if pess:
            seen[key] += tr.qty
            avail = max(seen[key] - depth, 0.0) - used[key]
        else:
            avail = tr.qty * q_quote / (depth + q_quote)
        q = max(min(q_quote - used[key], avail), 0.0)
        if q <= 0:
            continue
        used[key] += q; fills += 1
        inv[ex] += dq * q
        cash[ex] -= dq * q * px                 # buy YES pays px; selling YES (buying NO) receives px net
        vol += q * (px if dq > 0 else 1 - px)
        peak[ex] = max(peak[ex], abs(inv[ex]) * (mid if inv[ex] >= 0 else 1 - mid))
    pnl_mid = sum(cash[ex] + inv[ex] * last_mid.get(ex, 0.5) for ex in inv)
    pnl_poly = sum(cash[ex] + inv[ex] * last_sharp.get(ex, last_mid.get(ex, 0.5)) for ex in inv)
    cap = sum(peak.values())
    return dict(pnl_mid=pnl_mid / days, pnl_poly=pnl_poly / days, vol=vol / days, cap=cap, fills=fills / days,
                mkts=sum(1 for v in peak.values() if v > 0))


print(f"{'universe':10s} {'BAL':>5} {'inv$':>6} {'skew':>5} {'queue':>5} | {'P&L@mid/d':>9} {'P&L@poly/d':>10} {'vol/d':>10} "
      f"{'capital':>8} {'turn/d':>6} {'c/$':>5} {'fills/d':>7} {'mkts':>4}")
for univ, BAL in (("balanced", 0.01), ("balanced", 0.02), ("all", None)):
    for INV in (500.0, 2000.0):
        for SKEW in (None, 0.01):
            for pess in (True, False):
                r = run(univ=univ, BAL=BAL or 0, INV_USD=INV, SKEW=SKEW, pess=pess)
                print(f"{univ:10s} {str(BAL):>5} {INV:6.0f} {str(SKEW):>5} {'pess' if pess else 'opt':>5} | {r['pnl_mid']:9,.0f} "
                      f"{r['pnl_poly']:10,.0f} {r['vol']:10,.0f} {r['cap']:8,.0f} {r['vol']/max(r['cap'],1):6.1f} "
                      f"{100*r['pnl_mid']/max(r['vol'],1):5.2f} {r['fills']:7,.0f} {r['mkts']:4d}")
