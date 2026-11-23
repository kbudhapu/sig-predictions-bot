"""RECYCLING fair-band maker on the real tape + top-of-book (2026-10-08). Polymarket = fair.

Question: can a maker strategy RECYCLE capital (enter below fair, exit near fair, re-enter) instead of accumulating
inventory to settlement like cross_market / the current pilot?

Per market each minute (previous minute's book + fair):
  ENTRY  (flat, or adding within caps): YES bid at min(floor(fair - w_in), best_bid)  [join-only]
                                        NO  side: YES ask at max(ceil(fair + w_in), best_ask)  [buy NO]
  EXIT   (holding): long YES with avg cost a -> YES ask at max(ceil(max(fair - w_out, a + min_profit)), best_bid+tick)
                    long NO  (short YES)    -> YES bid at min(floor(min(fair + w_out, a_yes - min_profit)), best_ask-tick)
         the exit may sit INSIDE the spread (then first in queue) — it only ever sells inventory we hold.
  TIME STOP (optional): held > T hours -> close at the touch if that is no worse than cost (breakeven stop).
Fills: a YES-buyer trade at >= our ask / YES-seller at <= our bid fills us: inside spread -> first in queue; at the
touch -> back of queue (pessimistic) or pro-rata; behind -> behind the displayed touch size.
Capital: total exposure (cost of inventory) <= CAP_TOTAL, per market <= CAP_MKT.
Reported: realized round-trip P&L/day (banked), round trips, mean hold (h), capital-days, realized return per
capital-day, stuck inventory at the end (marked at SIG mid and at fair), turnover multiple.
Universes: balanced (median |gap| <= 1.5c), liquid (top third $/day), osc = top-N by gap ZERO-CROSSINGS measured
on the first TRAIN_FRAC of the window. Every universe is scored on the TEST part (after TRAIN_FRAC) as well.
Run: SNAP_DB=/tmp/snap.db python recycle_sim.py
"""
import collections
import os
import warnings

import numpy as np
import pandas as pd

from common import db, load_tape, minute_series

warnings.filterwarnings("ignore")
TICK = 0.005
CAP_TOTAL, CAP_MKT, Q_USD = 5000.0, 1000.0, 400.0
TRAIN_FRAC = 0.6

c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, mid, bid, ask, bid_size, ask_size FROM mid_history "
                "WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
d = d.drop_duplicates(["ex", "t"], keep="last")
book = {(r.ex, r.t): r for r in d.itertuples(index=False)}
S = minute_series("sharp_history", "sharp")
M = minute_series("mid_history", "mid")
tape = load_tape()
t0, t1 = d.t.min(), d.t.max()
tape = tape[(tape.ts >= t0 + pd.Timedelta(minutes=1)) & (tape.ts <= t1) & ~tape.ours & tape.ex.isin(S.columns)]
tape = tape.sort_values("ts")
split = t0 + (t1 - t0) * TRAIN_FRAC
cols = [x for x in S.columns if x in M.columns]
gap = (S[cols] - M.reindex(S.index)[cols])
train_gap = gap[gap.index < split]
balanced = set(train_gap.columns[train_gap.abs().median() <= 0.015])
sgn = np.sign(train_gap.where(train_gap.abs() >= 0.005))
crossings = (sgn.ffill().diff().abs() > 0).sum()
days_all = max((tape.ts.max() - tape.ts.min()).total_seconds() / 86400, 1e-9)
vol = tape.groupby("ex").usd.sum() / days_all
liquid = set(vol[vol >= vol.quantile(2 / 3)].index)
osc = set(crossings[crossings.index.isin(vol[vol >= vol.quantile(0.5)].index)].sort_values(ascending=False)
          .head(12).index)
fair = {ex: S[ex].dropna() for ex in S.columns}
last_mid = d.sort_values("t").groupby("ex").mid.last()
last_fair = S.ffill().iloc[-1]
print(f"window {t0:%m-%d %H:%M} .. {t1:%m-%d %H:%M} ({(t1 - t0).total_seconds() / 3600:.1f}h), train/test split "
      f"{split:%m-%d %H:%M}; trades {len(tape)}; balanced {len(balanced)} liquid {len(liquid)} osc {len(osc)}",
      flush=True)
print("osc markets (crossings in train):", {k: int(crossings[k]) for k in sorted(osc, key=lambda k: -crossings[k])},
      flush=True)


def fl(x):
    return np.floor(x / TICK + 1e-9) * TICK


def ce(x):
    return np.ceil(x / TICK - 1e-9) * TICK


def run(w_in, w_out, stop_h, universe, pess=True, min_profit=0.01, period="test", sides="both", stop_tol=0.0):
    tp = tape[tape.ts >= split] if period == "test" else tape[tape.ts < split] if period == "train" else tape
    inv = collections.defaultdict(float)      # signed YES qty
    avg = {}                                   # avg cost of the HELD leg (YES price if long, NO price if short)
    opened = {}
    realized = 0.0
    trips = 0
    holds = []
    used, seen = {}, {}
    cap_days = 0.0
    last_t = None
    expo = collections.defaultdict(float)
    buys = 0.0
    hourly = []
    next_h = None
    for r in tp.itertuples(index=False):
        ex = r.ex
        if universe == "balanced" and ex not in balanced:
            continue
        if universe == "liquid" and ex not in liquid:
            continue
        if universe == "osc" and ex not in osc:
            continue
        if last_t is not None:
            cap_days += sum(expo.values()) * (r.ts - last_t).total_seconds() / 86400
        last_t = r.ts
        if next_h is None:
            next_h = r.ts
        if r.ts >= next_h:
            hourly.append(round(sum(expo.values())))
            next_h = r.ts + pd.Timedelta(hours=1)
        snap_t = r.ts.floor("min") - pd.Timedelta(minutes=1)
        b = book.get((ex, snap_t))
        f = fair.get(ex)
        if b is None or f is None or not (0 < b.bid < b.ask < 1):
            continue
        fi = f.index.searchsorted(snap_t, side="right") - 1
        if fi < 0 or (snap_t - f.index[fi]) > pd.Timedelta(minutes=5):
            continue
        fv = float(f.iloc[fi])
        i = inv[ex]
        # time stop (breakeven): close at the touch once held too long, if no worse than cost
        if stop_h and i != 0 and (r.ts - opened[ex]).total_seconds() > stop_h * 3600:
            if i > 0 and b.bid >= avg[ex] - stop_tol:
                realized += i * (b.bid - avg[ex]); trips += 1; holds.append((r.ts - opened[ex]).total_seconds())
                inv[ex] = 0.0; expo[ex] = 0.0; i = 0.0
            elif i < 0 and (1 - b.ask) >= avg[ex] - stop_tol:
                realized += -i * ((1 - b.ask) - avg[ex]); trips += 1
                holds.append((r.ts - opened[ex]).total_seconds()); inv[ex] = 0.0; expo[ex] = 0.0; i = 0.0
        # quotes
        if i > 0:      # holding YES: exit ask near fair; no NO-side entry
            ask = max(ce(max(fv - w_out, avg[ex] + min_profit)), b.bid + TICK)
            bid = min(fl(fv - w_in), b.bid) if expo[ex] < CAP_MKT else None
        elif i < 0:    # holding NO: exit bid near fair (buy YES = sell NO)
            bid = min(fl(min(fv + w_out, (1 - avg[ex]) - min_profit)), b.ask - TICK)
            ask = max(ce(fv + w_in), b.ask) if expo[ex] < CAP_MKT else None
        else:
            bid = min(fl(fv - w_in), b.bid) if sides in ("both", "yes") else None
            ask = max(ce(fv + w_in), b.ask) if sides in ("both", "no") else None
        if r.agg_yes:
            side, px = "ask", ask
            if px is None or r.price < px - 1e-9:
                continue
        else:
            side, px = "bid", bid
            if px is None or r.price > px + 1e-9:
                continue
        if not (0.0 < px < 1.0):
            continue
        reduces = (side == "ask" and i > 0) or (side == "bid" and i < 0)
        unit = px if side == "bid" else 1 - px
        if reduces:
            q_sh = abs(i)
        else:
            room = min(CAP_MKT - expo[ex], CAP_TOTAL - sum(expo.values()))
            if room <= 1:
                continue
            q_sh = min(Q_USD, room) / max(unit, 0.01)
        key = (ex, snap_t, side)
        touch, depth = (b.bid, b.bid_size) if side == "bid" else (b.ask, b.ask_size)
        inside = (side == "bid" and px > b.bid + 1e-9) or (side == "ask" and px < b.ask - 1e-9)
        if inside:
            avail = r.qty
        elif abs(px - touch) < 1e-9 and not pess:
            avail = r.qty * q_sh / ((depth or 0) + q_sh)
        else:
            before = seen.get(key, 0.0); after = before + r.qty; seen[key] = after
            avail = max(0.0, after - max(depth or 0, before))
        q = min(avail, q_sh - used.get(key, 0.0))
        if q <= 0.5:
            continue
        used[key] = used.get(key, 0.0) + q
        if reduces:
            leg_px = px if i > 0 else 1 - px                 # sale price of the held leg
            realized += q * (leg_px - avg[ex])
            inv[ex] = i - q if i > 0 else i + q
            if abs(inv[ex]) < 0.5:
                inv[ex] = 0.0; trips += 1; holds.append((r.ts - opened[ex]).total_seconds())
        else:
            leg_px = px if side == "bid" else 1 - px
            held = abs(i)
            avg[ex] = (avg.get(ex, 0.0) * held + leg_px * q) / (held + q) if held else leg_px
            if held == 0:
                opened[ex] = r.ts
            inv[ex] = i + (q if side == "bid" else -q)
            buys += q * leg_px
        expo[ex] = abs(inv[ex]) * (avg.get(ex, 0.0) if inv[ex] != 0 else 0.0)
    hours = max((tp.ts.max() - tp.ts.min()).total_seconds() / 3600, 1e-9)
    k = 24 / hours
    stuck_cost = sum(abs(v) * avg.get(e, 0) for e, v in inv.items() if v)
    stuck_mid = sum((v * last_mid.get(e, np.nan) if v > 0 else -v * (1 - last_mid.get(e, np.nan))) - abs(v) * avg[e]
                    for e, v in inv.items() if v and e in last_mid.index)
    stuck_fair = sum((v * last_fair[e] if v > 0 else -v * (1 - last_fair[e])) - abs(v) * avg[e]
                     for e, v in inv.items() if v and e in last_fair.index and not np.isnan(last_fair[e]))
    cd = max(cap_days, 1e-9)
    # FULL ACCOUNTING: liquidate everything still held AT THE BID of the held leg at the end
    liq = 0.0
    lastb = d.sort_values("t").groupby("ex").last()
    for e, v in inv.items():
        if not v or e not in lastb.index:
            continue
        sell = lastb.loc[e, "bid"] if v > 0 else 1 - lastb.loc[e, "ask"]
        liq += abs(v) * (sell - avg[e])
    return dict(liq=liq, hourly=hourly, total_full=realized + liq, trips=trips, hold_h=(np.mean(holds) / 3600 if holds else float("nan")), realized_day=realized * k,
                ret_capday=100 * realized / cd, cap_avg=cd / (hours / 24), turnover_x=buys / max(cd / (hours / 24), 1),
                stuck_cost=stuck_cost, stuck_mid=stuck_mid, stuck_fair=stuck_fair, hours=hours)


print("FULL ACCOUNTING (stuck positions liquidated at the bid) | hourly exposure $", flush=True)
for universe in ("liquid", "osc", "balanced"):
    for w_in, stop_h in ((0.03, 2), (0.03, 4), (0.02, 2), (0.02, 4)):
        for period in ("train", "test", "all"):
            r = run(w_in, 0.01, stop_h, universe, period=period)
            k = 24 / r["hours"]
            print(f"{universe:8s} w{w_in*100:.0f} {stop_h}h {period:5s} | realized {r['realized_day']:+7,.0f}/d  liquidation {r['liq']*k:+7,.0f}/d "
                  f"=> FULL {r['total_full']*k:+7,.0f}/d | trips {r['trips']:3d} | exposure by hour: {r['hourly'][::4]}", flush=True)
