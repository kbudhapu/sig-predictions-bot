"""FORWARD TEST (shadow) of the recycling candidate — settings PRE-REGISTERED 2026-10-09 before seeing this data:
  liquid markets (by $/day BEFORE the forward window), entry fair-3c join-only, exit at COST+1c (fair ignored),
  breakeven stop 24h (variant 12h); baseline = HOLD (no exits). Scored with FULL ACCOUNTING (anything still held at
  the end is liquidated at the bid) ONLY on data after FWD_FROM (default 2026-10-08T13:17Z = end of research data).
Also: the live pilot's ACTUAL fills (strategy_fills fair_band): would the +1c exit have triggered, and P&L vs hold.
Decision rule (pre-registered): deploy only if, over >= 3 forward days, tp+1c/24h FULL >= 1.5x HOLD FULL and the
capital it holds at the end is below HOLD's.
--- original header ---
RECYCLING fair-band maker on the real tape + top-of-book (2026-10-08). Polymarket = fair.

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
FWD_FROM = pd.Timestamp(os.environ.get("FWD_FROM", "2026-10-08T13:17Z"))
_pre = tape[(tape.ts < FWD_FROM) & ~tape.ours]
_pre_days = max((_pre.ts.max() - _pre.ts.min()).total_seconds() / 86400, 1e-9) if len(_pre) else 1.0
_pre_vol = _pre.groupby("ex").usd.sum() / _pre_days
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
liquid = set(_pre_vol[_pre_vol >= _pre_vol.quantile(2 / 3)].index)   # chosen BEFORE the window
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
    tp = tape[tape.ts >= FWD_FROM] if period == "fwd" else (tape[tape.ts >= split] if period == "test" else tape[tape.ts < split] if period == "train" else tape)
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


print(f"FORWARD window {FWD_FROM:%m-%d %H:%M} .. {t1:%m-%d %H:%M} (unseen by the research); liquid set fixed beforehand: {len(liquid)} markets", flush=True)
res = {}
for name, w_in, w_out, mp, stop_h in (("HOLD (no exits)", 0.03, -1.0, 0.99, 0),
                                      ("tp+1c stop24h  ", 0.03, 1.0, 0.01, 24),
                                      ("tp+1c stop12h  ", 0.03, 1.0, 0.01, 12)):
    r = run(w_in, w_out, stop_h, "liquid", period="fwd", min_profit=mp)
    k = 24 / r["hours"]
    res[name] = r["total_full"] * k
    print(f"{name} | FULL {r['total_full']*k:+7,.0f}/d (realized {r['realized_day']:+6,.0f} liq {r['liq']*k:+6,.0f}) | trips {r['trips']:3d} "
          f"hold {r['hold_h']:5.1f}h | avg capital {r['cap_avg']:6,.0f} | held at end ${r['stuck_cost']:,.0f} | {r['hours']:.1f}h", flush=True)
h = res["HOLD (no exits)"]
print(f"ratio tp+1c/24h vs HOLD: {(res['tp+1c stop24h  '] / h) if h > 0 else float('nan'):.2f}x  (rule: >= 1.5x over >= 3 forward days)", flush=True)

# --- the LIVE pilot's actual fills: would cost+1c have been reached, and what would it have earned vs holding ---
F = pd.read_sql("SELECT exchange_id ex, outcome, price, quantity, filled_at FROM strategy_fills WHERE strategy='fair_band'", c)
if len(F):
    F["ts"] = pd.to_datetime(F.filled_at, utc=True, format="ISO8601")
    BIDS = d.pivot_table(index="t", columns="ex", values="bid", aggfunc="last").sort_index()
    ASKS = d.pivot_table(index="t", columns="ex", values="ask", aggfunc="last").sort_index()
    hit = gain = held_val = 0.0
    n_hit = 0
    for rr in F.itertuples(index=False):
        if rr.ex not in BIDS:
            continue
        s_ = (BIDS[rr.ex] if rr.outcome == "yes" else 1 - ASKS[rr.ex]).dropna()
        s_ = s_[s_.index >= rr.ts.floor("min")]
        if not len(s_):
            continue
        q = abs(rr.quantity)
        if (s_ >= rr.price + 0.01 - 1e-9).any():
            n_hit += 1
            gain += q * 0.01
        held_val += q * (float(s_.iloc[-1]) - rr.price)
    print(f"LIVE pilot fills: {len(F)} | reached cost+1c: {n_hit} ({n_hit/len(F):.0%}) -> +1c exits would bank ${gain:,.2f} "
          f"| holding all of them is worth ${held_val:+,.2f} at the bid now", flush=True)
