"""More fresh-gap signals? Grid over the move LOOKBACK window (6/12/24/48h) and move/gap thresholds, scored at REAL
bid/ask (taker in, taker out; maker-in variant = enter at our side's bid), exit at half-closed / flip / 48h, one open
trade per market, open trades marked at the last touch. Split into halves for robustness."""
import os, sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings("ignore")
c = sqlite3.connect(os.environ["SNAP_DB"])
s = pd.read_sql("SELECT exchange_id ex, ts, sharp, sig_bid b, sig_ask a FROM sharp_history WHERE sharp IS NOT NULL AND sig_bid IS NOT NULL AND sig_ask IS NOT NULL", c)
s["t"] = pd.to_datetime(s.ts.str[:16], utc=True)
s = s.drop_duplicates(["ex", "t"]).sort_values(["ex", "t"])
s = s[s.t.dt.minute % 10 == 0]                                  # 10-min grid
s["mid"] = (s.b + s.a) / 2
print(f"data {s.t.min():%m-%d %H:%M} .. {s.t.max():%m-%d %H:%M}, {s.ex.nunique()} markets")
def bt(lb_h, th_move, th_gap, maker_in):
    out = []
    L = int(lb_h * 6)
    for ex, d in s.groupby("ex"):
        d = d.reset_index(drop=True)
        mid0 = d.mid.shift(L); sh0 = d.sharp.shift(L)
        i, n = 0, len(d)
        while i < n:
            r = d.loc[i]
            if pd.isna(mid0[i]) or (r.a - r.b) > 0.02:
                i += 1; continue
            mv = r.mid - mid0[i]; g0 = sh0[i] - mid0[i]; g = r.sharp - r.mid
            if abs(mv) >= th_move and abs(g) >= th_gap and (mv > 0) != (g0 > 0) and (g > 0) == (g0 > 0):
                yes = g > 0
                ent = (r.b if maker_in else r.a) if yes else (1 - r.a if maker_in else 1 - r.b)
                j_end = min(i + 48 * 6, n - 1); j = j_end; why = "open" if i + 48 * 6 > n - 1 else "timeout"
                for k in range(i + 1, j_end + 1):
                    gk = d.sharp[k] - d.mid[k]
                    sg = gk if yes else -gk
                    if sg <= 0 or sg <= 0.5 * abs(g):
                        j, why = k, "closed"; break
                ex_px = d.b[j] if yes else 1 - d.a[j]
                out.append(dict(ex=ex, t=r.t, pnl=ex_px - ent, why=why, h=(j - i) / 6))
                i = j + 1
            else:
                i += 1
    return pd.DataFrame(out)
mid_t = s.t.quantile(0.5)
rows = []
for lb in (6, 12, 24, 48):
    for tm in (0.02, 0.03, 0.04):
        for tg in (0.02, 0.03):
            for mk in (False, True):
                R = bt(lb, tm, tg, mk)
                if R.empty: continue
                days = (s.t.max() - s.t.min()).total_seconds() / 86400
                h1, h2 = R[R.t <= mid_t], R[R.t > mid_t]
                rows.append(dict(lb_h=lb, move=tm, gap=tg, entry="maker" if mk else "taker", n=len(R), per_day=round(len(R) / days, 1),
                                 c_share=round(100 * R.pnl.mean(), 2), hit=round((R.pnl > 0).mean(), 2),
                                 half1=round(100 * h1.pnl.mean(), 2) if len(h1) else None, half2=round(100 * h2.pnl.mean(), 2) if len(h2) else None,
                                 med_h=round(R.h.median(), 1)))
G = pd.DataFrame(rows)
pd.set_option("display.width", 200)
print(G.sort_values(["entry", "c_share"], ascending=[True, False]).to_string(index=False))
G.to_csv(os.path.join(os.path.dirname(os.environ["SNAP_DB"]), "fresh_gap_grid.csv"), index=False)
