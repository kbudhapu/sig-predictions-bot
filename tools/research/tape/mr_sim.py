"""Mean reversion on LIQUID markets — maker vs taker, on the real tape + top-of-book (since 10-06 10:00).

Signal: z = (mid - mean60) / max(std60, 0.25c). |z| >= K: fade the move (z>0 -> short YES via a YES ask; z<0 -> long YES
via a YES bid). MAKER: rest at the touch, fills only from tape trades hitting us (pro-rata / back-of-queue), $500 per
minute up to $2k per market. Exit: rest the reducing quote at the touch once |z| <= 0.5 (back at the mean); forced
taker exit after 4h. TAKER baseline: cross the spread at signal and at exit. Filters: all moves / only moves AWAY
from Polymarket / only moves TOWARD Polymarket (where a sharp exists)."""
import warnings
import collections
import numpy as np, pandas as pd
from common import load_tape, minute_series, db
warnings.filterwarnings("ignore")

Q_USD, CAP_USD, HOLD_MAX = 500.0, 2000.0, 240
c = db()
d = pd.read_sql("SELECT exchange_id ex, ts, mid, bid, ask, bid_size, ask_size FROM mid_history WHERE bid_size IS NOT NULL", c)
d["t"] = pd.to_datetime(d.ts.str[:16], utc=True)
d = d.drop_duplicates(["ex", "t"], keep="last")
tape = load_tape()
days_t = (tape.ts.max() - tape.ts.min()).total_seconds() / 86400
vol = tape[~tape.ours].groupby("ex").usd.sum() / days_t
liquid = list(vol[vol >= vol.quantile(2 / 3)].index)
M = minute_series("mid_history", "mid")                      # full history for the 60m stats
S = minute_series("sharp_history", "sharp")
t0 = d.t.min()
grid = {k: g.set_index("t").sort_index() for k, g in d[d.ex.isin(liquid)].groupby("ex")}
tp = tape[(tape.ts >= t0) & ~tape.ours & tape.ex.isin(liquid)].copy()
tp["min"] = tp.ts.dt.floor("min")
trades = collections.defaultdict(list)
for r in tp.itertuples(index=False):
    trades[(r.ex, r.min)].append((r.agg_yes, r.price, r.qty))
print(f"liquid markets {len(liquid)} (>= ${vol.quantile(2/3):,.0f}/day); window {t0:%m-%d %H:%M} .. {tp.ts.max():%m-%d %H:%M}; "
      f"{len(tp)} trades")


def maker_fill(ex, minute, side, px, depth, q_sh, pess):
    got, seen = 0.0, 0.0
    for agg_yes, p, q in trades.get((ex, minute), ()):
        if side == "ask" and not (agg_yes and p >= px - 1e-9):
            continue
        if side == "bid" and not ((not agg_yes) and p <= px + 1e-9):
            continue
        if pess:
            after = seen + q; f = max(0.0, after - max(depth, seen)); seen = after
        else:
            f = q * q_sh / (depth + q_sh)
        got = min(q_sh, got + f)
    return got


def run(K, mode, filt, pess=True):
    pnl = 0.0; n_trades = 0; turnover = 0.0; open_marks = 0.0; peak = 0.0; holding_now = 0.0
    for ex in liquid:
        g = grid.get(ex)
        if g is None or ex not in M:
            continue
        m = M[ex]
        mean = m.rolling(60, min_periods=30).mean(); sd = m.rolling(60, min_periods=30).std().clip(lower=0.0025)
        z = ((m - mean) / sd).reindex(g.index)
        sh = S[ex].reindex(g.index) if ex in S else pd.Series(np.nan, index=g.index)
        pos = 0.0; cost = 0.0; entered = None
        idx = list(g.index)
        for i, t in enumerate(idx[:-1]):
            row = g.loc[t]; zt = z.get(t, np.nan)
            if np.isnan(zt):
                continue
            nxt = idx[i + 1]
            if pos == 0:
                if abs(zt) < K:
                    continue
                side = "ask" if zt > 0 else "bid"                      # fade: sell YES after a rise, buy after a fall
                if filt != "all":
                    s = sh.get(t, np.nan)
                    if np.isnan(s):
                        continue
                    move_dir = np.sign(zt)                              # + = price rose
                    toward = np.sign(s - row.mid) == move_dir           # the move went toward Poly
                    if (filt == "away" and toward) or (filt == "toward" and not toward):
                        continue
                px = row.ask if side == "ask" else row.bid
                hp = px if side == "bid" else 1 - px
                q_sh = Q_USD / max(hp, 0.01)
                if mode == "taker":
                    q = CAP_USD / max((row.ask if side == "bid" else 1 - row.bid), 0.01)
                    fill_px = row.ask if side == "bid" else row.bid      # crossing: buy YES at ask / sell YES at bid
                else:
                    q = maker_fill(ex, nxt, side, px, row.ask_size if side == "ask" else row.bid_size, q_sh, pess)
                    fill_px = px
                if q <= 0.5:
                    continue
                sgn = 1.0 if side == "bid" else -1.0
                pos = sgn * q; cost = sgn * q * fill_px; entered = i
                turnover += q * (fill_px if sgn > 0 else 1 - fill_px); n_trades += 1
            else:
                done = abs(zt) <= 0.5 or (i - entered) >= HOLD_MAX
                if not done:
                    continue
                forced = (i - entered) >= HOLD_MAX
                if mode == "taker" or forced:
                    exit_px = row.bid if pos > 0 else row.ask           # cross to exit
                    q = abs(pos)
                else:
                    side = "ask" if pos > 0 else "bid"
                    px = row.ask if side == "ask" else row.bid
                    q = min(abs(pos), maker_fill(ex, nxt, side, px, row.ask_size if side == "ask" else row.bid_size,
                                                 abs(pos), pess))
                    exit_px = px
                    if q <= 0.5:
                        continue
                frac = q / abs(pos)
                pnl += (pos * frac) * exit_px - cost * frac
                turnover += q * (exit_px if pos > 0 else 1 - exit_px)
                pos -= pos * frac; cost -= cost * frac
                if abs(pos) < 0.5:
                    pos = 0.0; cost = 0.0
        if pos != 0:
            last = g.iloc[-1]
            open_marks += pos * last.mid - cost
    hours = (tp.ts.max() - t0).total_seconds() / 3600
    return dict(trades=n_trades, turnover=turnover, realized=pnl, open=open_marks, per_day=(pnl + open_marks) * 24 / hours)


print(f"\n{'mode':6s} {'K':>3s} {'filter':7s} {'queue':6s} | {'entries':>7s} {'turnover':>9s} {'realized':>9s} {'open mark':>9s} {'total/day':>9s}")
for K in (2.0, 3.0):
    for filt in ("all", "away", "toward"):
        r = run(K, "taker", filt)
        print(f"{'taker':6s} {K:3.0f} {filt:7s} {'-':6s} | {r['trades']:7d} {r['turnover']:9,.0f} {r['realized']:9,.0f} "
              f"{r['open']:9,.0f} {r['per_day']:9,.0f}")
        for pess in (False, True):
            r = run(K, "maker", filt, pess)
            print(f"{'maker':6s} {K:3.0f} {filt:7s} {'back' if pess else 'pro':6s} | {r['trades']:7d} {r['turnover']:9,.0f} "
                  f"{r['realized']:9,.0f} {r['open']:9,.0f} {r['per_day']:9,.0f}")
