"""FAVOURED-SIDE ROUND-TRIP MM (2026-10-09) — does recycling maker flow on the Polymarket-favoured side pay?

Evidence that motivated it: leaders trade 6-53x their equity in volume at ~1-2c per $ (round trips, not holds);
taker flow AGAINST Poly hands makers ~$1.1M/day of EV; WITH-Poly takers ($24M over the window) buy the favoured side.

Per market, per minute, held-side F = the side Polymarket favours (YES if sharp > SIG mid else NO), if |gap| >= G:
  BID  on F at the touch for Q_USD (if inventory cost < CAP_USD)      <- filled by takers SELLING F (retail flow)
  ASK  on F at the touch for min(inventory, Q)  [policy 'any' or only at/above avg cost 'nolose']   <- takers BUYING F
Fills from the real tape (ours excluded): an aggressor trade at our price or through it fills us.
Queue: OPTIMISTIC pro-rata Q/(displayed+Q); PESSIMISTIC we are behind the displayed size (only volume beyond it).
Reported: realized round-trip P&L (cash from matched buys/sells), leftover inventory marked at SIG mid AND at Poly,
peak/avg capital, volume, turnover (volume / avg capital per day), P&L per $ of volume.
Run: SNAP_DB=<snapshot> python frt_mm.py
"""
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

from common import db, load_tape, minute_series

warnings.filterwarnings("ignore")
c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, mid, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
d = d.drop_duplicates(["ex", "t"], keep="last").set_index(["ex", "t"]).sort_index()
S = minute_series("sharp_history", "sharp")
t0 = d.index.get_level_values("t").min()
tape = load_tape()
tape = tape[(tape.ts >= t0 + pd.Timedelta(minutes=1)) & ~tape.ours].sort_values("ts")
book = d[["bid", "ask", "bid_size", "ask_size", "mid"]].to_dict("index")
sharp = {ex: S[ex].dropna().to_dict() for ex in S.columns}
last_mid = d.groupby(level="ex").mid.last().to_dict()
last_sharp = {ex: S[ex].dropna().iloc[-1] for ex in S.columns if S[ex].notna().any()}
days = (tape.ts.max() - tape.ts.min()).total_seconds() / 86400
print(f"window {tape.ts.min():%m-%d %H:%M} .. {tape.ts.max():%m-%d %H:%M} ({days:.2f}d), {len(tape)} trades")


def run(G=0.01, Q_USD=500.0, CAP_USD=3000.0, sell_policy="any", pessimistic=True):
    held = defaultdict(float)          # held-side shares (F of that market at entry; if F flips we only sell)
    side = {}                          # ex -> 'yes'/'no' side we hold
    cost = defaultdict(float)          # $ cost basis of held shares
    realized = 0.0
    vol = 0.0
    used = defaultdict(float)
    seen = defaultdict(float)
    cap_series = []
    last_cap_t = None
    for tr in tape.itertuples():
        ex, t = tr.ex, tr.ts.floor("min")
        b = book.get((ex, t - pd.Timedelta(minutes=1)))
        if b is None or not (0 < b["bid"] < b["ask"] < 1):
            continue
        sh = sharp.get(ex, {}).get(t - pd.Timedelta(minutes=1))
        if sh is None:
            continue
        mid = (b["bid"] + b["ask"]) / 2
        gap = sh - mid
        F = side.get(ex) if held[ex] > 0 else ("yes" if gap > 0 else "no")
        favoured_now = abs(gap) >= G and ((gap > 0) == (F == "yes"))
        # held-side touch prices and which aggressor fills which quote
        if F == "yes":
            bid_px, ask_px = b["bid"], b["ask"]
            bid_depth, ask_depth = b["bid_size"], b["ask_size"]
            fills_bid = (not tr.agg_yes) and tr.price <= bid_px + 1e-9          # YES sold into our YES bid
            fills_ask = tr.agg_yes and tr.price >= ask_px - 1e-9                # YES bought from our YES ask
        else:
            bid_px, ask_px = 1 - b["ask"], 1 - b["bid"]                         # NO bid = YES ask; NO ask = YES bid
            bid_depth, ask_depth = b["ask_size"], b["bid_size"]
            fills_bid = tr.agg_yes and tr.price >= b["ask"] - 1e-9              # YES bought = NO sold into our NO bid
            fills_ask = (not tr.agg_yes) and tr.price <= b["bid"] + 1e-9        # YES sold = NO bought from our NO ask
        for which, ok in (("bid", fills_bid), ("ask", fills_ask)):
            if not ok:
                continue
            key = (ex, t, which)
            px = bid_px if which == "bid" else ask_px
            if which == "bid":
                if not favoured_now or cost[ex] >= CAP_USD:
                    continue
                q_quote = min(Q_USD, CAP_USD - cost[ex]) / max(px, 0.01)
            else:
                if held[ex] <= 0:
                    continue
                if sell_policy == "nolose" and px < cost[ex] / held[ex] - 1e-9:
                    continue
                q_quote = min(held[ex], Q_USD / max(px, 0.01))
            depth = bid_depth if which == "bid" else ask_depth
            if pessimistic:
                seen[key] += tr.qty
                avail = max(seen[key] - depth, 0.0) - used[key]
            else:
                avail = tr.qty * q_quote / (depth + q_quote) - 0.0
            q = max(min(q_quote - used[key], avail), 0.0)
            if q <= 0:
                continue
            used[key] += q
            vol += q * px
            if which == "bid":
                held[ex] += q
                cost[ex] += q * px
                side[ex] = F
            else:
                avg = cost[ex] / held[ex]
                realized += q * (px - avg)
                cost[ex] -= q * avg
                held[ex] -= q
        if last_cap_t is None or (tr.ts - last_cap_t).total_seconds() >= 600:
            cap_series.append(sum(cost.values()))
            last_cap_t = tr.ts
    inv_mid = inv_poly = 0.0
    for ex, q in held.items():
        if q <= 0:
            continue
        m = last_mid.get(ex)
        s = last_sharp.get(ex)
        if m is None:
            continue
        val_mid = m if side[ex] == "yes" else 1 - m
        inv_mid += q * val_mid - cost[ex]
        if s is not None:
            inv_poly += q * (s if side[ex] == "yes" else 1 - s) - cost[ex]
    capm = np.mean(cap_series) if cap_series else 0.0
    return dict(realized=realized, inv_mid=inv_mid, inv_poly=inv_poly, vol=vol, peak=max(cap_series or [0]),
                avg_cap=capm, left=sum(cost.values()), days=days)


if __name__ == "__main__":
    print(f"{'G':>4} {'cap':>6} {'sell':>6} {'queue':>5} | {'realized/d':>10} {'inv@mid':>8} {'inv@poly':>8} "
          f"{'TOTAL@mid/d':>11} {'avg cap':>8} {'peak':>8} {'left':>8} {'vol/d':>9} {'turn/d':>6} {'c/$vol':>6}")
    for G in (0.01, 0.02, 0.03):
        for CAP in (2000.0, 5000.0):
            for sp in ("any", "nolose"):
                for pess in (True, False):
                    r = run(G=G, CAP_USD=CAP, sell_policy=sp, pessimistic=pess)
                    dd = r["days"]
                    tot = (r["realized"] + r["inv_mid"]) / dd
                    print(f"{G:4.2f} {CAP:6.0f} {sp:>6} {'pess' if pess else 'opt':>5} | {r['realized']/dd:10,.0f} "
                          f"{r['inv_mid']:8,.0f} {r['inv_poly']:8,.0f} {tot:11,.0f} {r['avg_cap']:8,.0f} {r['peak']:8,.0f} "
                          f"{r['left']:8,.0f} {r['vol']/dd:9,.0f} {r['vol']/dd/max(r['avg_cap'],1):6.1f} "
                          f"{100*(r['realized']+r['inv_mid'])/max(r['vol'],1):6.2f}")
