"""FAIR-CENTRED market making on the real tape + top-of-book (since depth recording), Polymarket price = fair.

Each minute (using the PREVIOUS minute's book + fair): YES bid at floor(fair - w), YES ask at ceil(fair + w), on the
0.5c grid, never crossing (bid <= best_ask - tick, ask >= best_bid + tick). improve=True lets a quote sit INSIDE the
spread (then we're first in queue); improve=False only joins/behinds the touch. Inventory cap per market on the
$ exposure (YES value for longs, NO value for shorts) blocks the side that adds exposure.
Fills: a YES-buyer trade at price >= our ask (a YES-seller trade at <= our bid) fills us:
  inside the spread -> first in queue (min(trade, remaining quote));  at the touch -> pro-rata or back-of-queue;
  behind the touch -> back-of-queue behind the displayed touch size.
Scored: P&L marked to SIG mid at the end, value at Polymarket (EV), edge at fill vs fair, 60-min markout,
turnover, peak exposure."""
import collections, sys, warnings
import numpy as np, pandas as pd
from common import load_tape, minute_series, db
warnings.filterwarnings("ignore")

TICK = 0.005
c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, mid, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
d = d.drop_duplicates(["ex", "t"], keep="last")
S = minute_series("sharp_history", "sharp")
t0, t1 = d.t.min(), d.t.max()
tape = load_tape()
days_all = (tape.ts.max() - tape.ts.min()).total_seconds() / 86400
vol = tape[~tape.ours].groupby("ex").usd.sum() / days_all
liquid = set(vol[vol >= vol.quantile(2 / 3)].index)
tape = tape[(tape.ts >= t0 + pd.Timedelta(minutes=1)) & (tape.ts <= t1) & ~tape.ours & tape.ex.isin(S.columns)].sort_values("ts")
book = {(r.ex, r.t): r for r in d.itertuples(index=False)}
fair = {ex: S[ex].dropna() for ex in S.columns}
M60 = minute_series("mid_history", "mid")
last_mid = d.sort_values("t").groupby("ex").mid.last()
last_fair = S.ffill().iloc[-1]
hours = (tape.ts.max() - t0).total_seconds() / 3600
print(f"window {t0:%m-%d %H:%M} .. {tape.ts.max():%m-%d %H:%M} ({hours:.1f}h), {len(tape)} trades in {tape.ex.nunique()} "
      f"markets with a Polymarket fair; liquid = {len(liquid)} markets", flush=True)


def fl(x):
    return np.floor(x / TICK + 1e-9) * TICK


def ce(x):
    return np.ceil(x / TICK - 1e-9) * TICK


def run(w, improve, cap, q_usd, pess, universe):
    inv = collections.defaultdict(float); cash = collections.defaultdict(float)
    used, seen = {}, {}
    turnover = edge_fill = mo60 = 0.0; n = 0; peak = 0.0; expo = {}
    for r in tape.itertuples(index=False):
        ex = r.ex
        if universe == "liquid" and ex not in liquid:
            continue
        snap_t = r.ts.floor("min") - pd.Timedelta(minutes=1)
        b = book.get((ex, snap_t))
        f = fair.get(ex)
        if b is None or f is None:
            continue
        fi = f.index.searchsorted(snap_t, side="right") - 1
        if fi < 0 or (snap_t - f.index[fi]) > pd.Timedelta(minutes=5):
            continue
        fv = float(f.iloc[fi])
        bq = min(fl(fv - w), b.ask - TICK); aq = max(ce(fv + w), b.bid + TICK)
        if not improve:
            bq = min(bq, b.bid); aq = max(aq, b.ask)
        if r.agg_yes:                                     # YES buyer: may lift our ASK
            side, px = "ask", aq
            if r.price < px - 1e-9:
                continue
        else:                                             # YES seller: may hit our BID
            side, px = "bid", bq
            if r.price > px + 1e-9:
                continue
        if not (0.0 < px < 1.0):
            continue
        i = inv[ex]
        e_now = i * b.mid if i > 0 else -i * (1 - b.mid)
        adds = (side == "bid" and i >= 0) or (side == "ask" and i <= 0)
        if adds and e_now >= cap:
            continue
        unit = px if side == "bid" else 1 - px
        q_sh = q_usd / max(unit, 0.01)
        key = (ex, snap_t, side)
        touch = b.bid if side == "bid" else b.ask
        depth = b.bid_size if side == "bid" else b.ask_size
        inside = (side == "bid" and px > b.bid + 1e-9) or (side == "ask" and px < b.ask - 1e-9)
        if inside:
            avail = r.qty
        elif abs(px - touch) < 1e-9 and not pess:
            avail = r.qty * q_sh / (depth + q_sh)
        else:                                             # at the touch (pessimistic) or behind it
            before = seen.get(key, 0.0); after = before + r.qty; seen[key] = after
            avail = max(0.0, after - max(depth, before))
        q = min(avail, q_sh - used.get(key, 0.0))
        if adds:
            q = min(q, (cap - e_now) / max(unit, 0.01))
        if q <= 0.5:
            continue
        used[key] = used.get(key, 0.0) + q
        sgn = 1.0 if side == "bid" else -1.0
        inv[ex] = i + sgn * q
        cash[ex] -= sgn * q * px
        turnover += q * unit; n += 1
        edge_fill += q * (fv - px if side == "bid" else px - fv)
        m60 = M60[ex].get(snap_t + pd.Timedelta(minutes=61), np.nan) if ex in M60 else np.nan
        if not np.isnan(m60):
            mo60 += q * (m60 - px if side == "bid" else px - m60)
        expo[ex] = abs(inv[ex]) * (b.mid if inv[ex] > 0 else 1 - b.mid)
        peak = max(peak, sum(expo.values()))
    pm = sum(cash[e] + inv[e] * float(last_mid.get(e, np.nan)) for e in inv if e in last_mid.index)
    pf = sum(cash[e] + inv[e] * float(last_fair[e]) for e in inv if e in last_fair.index and not np.isnan(last_fair[e]))
    k = 24 / hours
    return dict(fills=n, turnover_day=turnover * k, peak=peak, pnl_mid_day=pm * k, ev_day=pf * k,
                edge_c=100 * edge_fill / max(turnover, 1), mo60_c=100 * mo60 / max(turnover, 1))


print(f"\n{'w':>4s} {'impr':>4s} {'cap':>6s} {'Q':>5s} {'queue':>5s} {'univ':>6s} | {'fills':>6s} {'turn/day':>9s} "
      f"{'peak $':>8s} | {'P&L@mid/day':>11s} {'EV@Poly/day':>11s} | {'edge@fill':>9s} {'mo60':>6s}  (c per $ turned)", flush=True)
grid = [(w, imp, cap, q, pess, uni) for uni in ("all", "liquid") for w in (0.005, 0.01, 0.02) for imp in (True, False)
        for cap, q in ((4400, 500), (20000, 2000)) for pess in (False, True)]
for w, imp, cap, q, pess, uni in grid:
    r = run(w, imp, cap, q, pess, uni)
    print(f"{w*100:4.1f} {'yes' if imp else 'no':>4s} {cap:6,d} {q:5,d} {'back' if pess else 'pro':>5s} {uni:>6s} | "
          f"{r['fills']:6d} {r['turnover_day']:9,.0f} {r['peak']:8,.0f} | {r['pnl_mid_day']:11,.0f} {r['ev_day']:11,.0f} | "
          f"{r['edge_c']:9.2f} {r['mo60_c']:6.2f}", flush=True)
