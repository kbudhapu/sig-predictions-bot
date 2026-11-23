"""MM-2 — market-making simulation on the REAL tape + top-of-book (since depth recording, 10-06 10:00).

Each minute we (virtually) rest Q shares at the touch (best bid and/or best ask from the previous minute's snapshot).
A trade fills us only if it hits our side at our price or through it:
  YES-aggressor trade with price >= our ask fills our ask;   NO-aggressor (sold YES) with price <= our bid fills our bid.
Queue models:  OPTIMISTIC  pro-rata: we get trade_qty * Q / (displayed_depth + Q)
               PESSIMISTIC we are BEHIND the displayed depth: only volume beyond it (cumulative in that minute) fills us.
Policies:
  symmetric      quote both sides (old MM), inventory cap per market
  anchored-m     quote ONLY the side Polymarket favours when |sharp - mid| >= m (bid if SIG below Poly, ask if above)
  anchored+exit  anchored, plus quote the REDUCING side at the touch once the gap has closed to <= 1c (recycle)
P&L = cash + YES inventory marked at the end (mid, and sharp = EV). Capital = peak sum of |position cost|."""
import warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, db
warnings.filterwarnings("ignore")

Q_USD = 500.0            # $ notional per quote refresh per side
CAP_USD = 4400.0         # per-market inventory cap (cost)
c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, mid, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
d = d.drop_duplicates(["ex", "t"], keep="last").set_index(["ex", "t"]).sort_index()
S = minute_series("sharp_history", "sharp")
t0 = d.index.get_level_values("t").min()
tape = load_tape()
tape = tape[(tape.ts >= t0 + pd.Timedelta(minutes=1)) & ~tape.ours].sort_values("ts")
print(f"window {t0:%m-%d %H:%M} .. {tape.ts.max():%m-%d %H:%M}, {len(tape)} trades, {tape.ex.nunique()} markets")
book = d[["bid", "ask", "bid_size", "ask_size", "mid"]].to_dict("index")
sharp_at = {}
for ex in S.columns:
    sharp_at[ex] = S[ex]
last_mid = d.groupby(level="ex").mid.last()
last_sharp = S.ffill().iloc[-1]


def run(policy, m=0.02, pessimistic=False):
    inv, cash, cost = {}, {}, {}           # YES shares, cash flow, signed cost basis per market
    used = {}                              # (ex, minute, side) -> qty already filled from our Q this minute
    seen = {}                              # (ex, minute, side) -> cumulative volume at the level this minute
    turnover = peak = 0.0
    fills = 0
    for r in tape.itertuples(index=False):
        snap_t = r.ts.floor("min") - pd.Timedelta(minutes=1)
        b = book.get((r.ex, snap_t))
        if b is None:
            continue
        mid = b["mid"]
        sh = sharp_at.get(r.ex)
        sharp = None if sh is None else sh.get(snap_t, np.nan)
        sharp = None if sharp is None or np.isnan(sharp) else float(sharp)
        side = "ask" if r.agg_yes else "bid"           # which of OUR quotes this trade could hit
        px = b["ask"] if side == "ask" else b["bid"]
        if (side == "ask" and r.price < px - 1e-9) or (side == "bid" and r.price > px + 1e-9):
            continue                                     # printed inside the spread: our touch quote not hit
        # policy: do we have a quote on this side right now?
        i = inv.get(r.ex, 0.0)
        if policy == "symmetric":
            want = True
        else:
            if sharp is None:
                continue
            gap = sharp - mid                            # + : SIG below Poly -> want YES (bid)
            fav = "bid" if gap > 0 else "ask"
            want = (abs(gap) >= m and side == fav)
            if policy == "anchored+exit" and abs(gap) <= 0.01 and i != 0:
                want = (side == "ask" and i > 0) or (side == "bid" and i < 0)    # unwind passively
        if not want:
            continue
        # inventory cap (only blocks increasing exposure)
        cur_cost = abs(cost.get(r.ex, 0.0))
        increases = (side == "bid" and i >= 0) or (side == "ask" and i <= 0)
        if increases and cur_cost >= CAP_USD:
            continue
        q_shares = Q_USD / max(px if side == "bid" else (1 - px), 0.01)
        key = (r.ex, snap_t, side)
        depth = b["bid_size"] if side == "bid" else b["ask_size"]
        if pessimistic:
            before = seen.get(key, 0.0); after = before + r.qty; seen[key] = after
            avail = max(0.0, after - max(depth, before))
        else:
            avail = r.qty * q_shares / (depth + q_shares)
        q = min(avail, q_shares - used.get(key, 0.0))
        if increases:
            q = min(q, (CAP_USD - cur_cost) / max(px if side == "bid" else 1 - px, 0.01))
        if q <= 0.5:
            continue
        used[key] = used.get(key, 0.0) + q
        sgn = 1.0 if side == "bid" else -1.0             # bid fill = we BUY YES; ask fill = we SELL YES
        inv[r.ex] = i + sgn * q
        cash[r.ex] = cash.get(r.ex, 0.0) - sgn * q * px
        cost[r.ex] = cost.get(r.ex, 0.0) + sgn * q * px
        turnover += q * (px if side == "bid" else 1 - px)
        fills += 1
        peak = max(peak, sum(abs(v) for v in cost.values()))
    pnl_mid = sum(cash[e] + inv[e] * float(last_mid.get(e, np.nan)) for e in inv if e in last_mid)
    pnl_sharp = sum(cash[e] + inv[e] * float(last_sharp[e]) for e in inv if e in last_sharp.index and not np.isnan(last_sharp[e]))
    open_cost = sum(abs(v) for v in cost.values())
    return dict(fills=fills, turnover=turnover, peak_capital=peak, open_cost=open_cost, pnl_mid=pnl_mid,
                pnl_sharp=pnl_sharp, markets=len(inv))


hours = (tape.ts.max() - t0).total_seconds() / 3600
print(f"quote ${Q_USD:.0f}/side/minute at the touch, inventory cap ${CAP_USD:,.0f}/market; window {hours:.1f}h\n")
print(f"{'policy':22s} {'queue':11s} {'fills':>6s} {'turnover':>10s} {'peak cap':>9s} {'mkts':>4s} | "
      f"{'P&L@mid':>8s} {'/day':>7s} | {'EV@Poly':>8s} {'/day':>7s}")
for pol, m in (("symmetric", 0), ("anchored", 0.01), ("anchored", 0.02), ("anchored", 0.03), ("anchored+exit", 0.02)):
    for pess in (False, True):
        r = run(pol, m, pess)
        name = pol if pol == "symmetric" else f"{pol} m={m*100:.0f}c"
        f = 24 / hours
        print(f"{name:22s} {'pessimistic' if pess else 'pro-rata':11s} {r['fills']:6d} {r['turnover']:10,.0f} "
              f"{r['peak_capital']:9,.0f} {r['markets']:4d} | {r['pnl_mid']:8,.0f} {r['pnl_mid']*f:7,.0f} | "
              f"{r['pnl_sharp']:8,.0f} {r['pnl_sharp']*f:7,.0f}")
