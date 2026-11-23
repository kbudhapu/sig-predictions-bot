"""2026-10-11: fresh-gap's edge halves in the 2nd half of the data. (A) WHY: per-day signal count / pnl / hold.
New round-trip candidates, all scored at REAL bid/ask on a 10-min grid, one open trade per market, half-split:
 B  'own-level band': deviation of the gap (poly - sig mid) from ITS OWN trailing 24h median >= k -> trade toward
    that level (gaps are persistent biases that oscillate; anchor = the market's usual gap, not zero). Exit when the
    deviation halves / flips, 24h timeout.
 C  fresh-gap with KALSHI confirmation: Kalshi mid on Poly's side of SIG (|kalshi - poly| <= 1.5c).
 E  Poly-led lag: Poly moved >= m over L hours, SIG moved < m/3 the other way -> trade SIG toward Poly.
taker in/out; 'maker' variant enters at our side's bid (optimistic: assumes the fill)."""
import os, sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings("ignore")
c = sqlite3.connect(os.environ["SNAP_DB"])
s = pd.read_sql("SELECT exchange_id ex, ts, sharp, sig_bid b, sig_ask a FROM sharp_history WHERE sharp IS NOT NULL "
                "AND sig_bid IS NOT NULL AND sig_ask IS NOT NULL", c)
s["t"] = pd.to_datetime(s.ts.str[:16], utc=True)
s = s.drop_duplicates(["ex", "t"]).sort_values(["ex", "t"])
s = s[s.t.dt.minute % 10 == 0].copy()
s["mid"] = (s.b + s.a) / 2
s["gap"] = s.sharp - s.mid
k = pd.read_sql("SELECT exchange_id ex, ts, mid kmid FROM kalshi_history WHERE mid IS NOT NULL", c)
k["t"] = pd.to_datetime(k.ts.str[:16], utc=True).dt.floor("10min")
k = k.drop_duplicates(["ex", "t"])[["ex", "t", "kmid"]]
s = s.merge(k, on=["ex", "t"], how="left")
T0, T1 = s.t.min(), s.t.max()
DAYS = (T1 - T0).total_seconds() / 86400
MID_T = T0 + (T1 - T0) / 2
print(f"data {T0:%m-%d %H:%M} .. {T1:%m-%d %H:%M} ({DAYS:.1f}d), {s.ex.nunique()} markets, kalshi rows {s.kmid.notna().sum()}")
G = {ex: d.reset_index(drop=True) for ex, d in s.groupby("ex")}


def run(entry_fn, exit_fn, max_h, maker):
    out = []
    for ex, d in G.items():
        cols = entry_fn(d)
        i, n = 0, len(d)
        while i < n:
            sgn = cols[i]
            if sgn == 0 or (d.a[i] - d.b[i]) > 0.02:
                i += 1
                continue
            yes = sgn > 0
            ent = (d.b[i] if maker else d.a[i]) if yes else (1 - d.a[i] if maker else 1 - d.b[i])
            j_end = min(i + int(max_h * 6), n - 1)
            j, why = j_end, ("open" if i + max_h * 6 > n - 1 else "timeout")
            for kk in range(i + 1, j_end + 1):
                if exit_fn(d, i, kk, yes):
                    j, why = kk, "closed"
                    break
            px = d.b[j] if yes else 1 - d.a[j]
            out.append(dict(ex=ex, t=d.t[i], pnl=px - ent, why=why, h=(j - i) / 6))
            i = j + 1
    return pd.DataFrame(out)


def summ(name, R):
    if R.empty:
        return dict(sig=name, n=0)
    h1, h2 = R[R.t <= MID_T], R[R.t > MID_T]
    return dict(sig=name, n=len(R), per_day=round(len(R) / DAYS, 1), c=round(100 * R.pnl.mean(), 2),
                hit=round((R.pnl > 0).mean(), 2), h1=round(100 * h1.pnl.mean(), 2) if len(h1) else None,
                h2=round(100 * h2.pnl.mean(), 2) if len(h2) else None, med_h=round(R.h.median(), 1),
                c_per_day=round(100 * R.pnl.sum() / DAYS, 1))


rows = []
# ---- A: fresh-gap (pilot setting 12h/3c/3c), and per-day breakdown
def fg(lb, tm, tg, kal=False):
    L = int(lb * 6)

    def f(d):
        mid0, sh0 = d.mid.shift(L), d.sharp.shift(L)
        mv, g0, g = d.mid - mid0, sh0 - mid0, d.gap
        ok = (mv.abs() >= tm) & (g.abs() >= tg) & ((mv > 0) != (g0 > 0)) & ((g > 0) == (g0 > 0))
        if kal:
            ok &= (d.kmid - d.sharp).abs() <= 0.015
        return np.where(ok, np.sign(g), 0)
    return f


def fg_exit(d, i, kk, yes):
    g0 = d.gap[i]
    sg = d.gap[kk] if yes else -d.gap[kk]
    return sg <= 0 or sg <= 0.5 * abs(g0)


for mk in (False, True):
    tag = "maker" if mk else "taker"
    R = run(fg(12, 0.03, 0.03), fg_exit, 48, mk)
    rows.append(summ(f"A fresh 12/3/3 {tag}", R))
    if not mk:
        R["day"] = R.t.dt.strftime("%m-%d")
        print("\nA per day (taker, 12h/3c/3c): n, mean c, median hold h, closed share")
        print(R.groupby("day").agg(n=("pnl", "size"), c=("pnl", lambda x: round(100 * x.mean(), 2)),
                                   med_h=("h", "median"), closed=("why", lambda x: round((x == "closed").mean(), 2))).to_string())
    for kal in (True,):
        rows.append(summ(f"C fresh 12/3/3 +kalshi {tag}", run(fg(12, 0.03, 0.03, kal=True), fg_exit, 48, mk)))
        rows.append(summ(f"C fresh 12/2/2 +kalshi {tag}", run(fg(12, 0.02, 0.02, kal=True), fg_exit, 48, mk)))


# ---- B: own-level band
def band(kdev, win_h=24):
    W = int(win_h * 6)

    def f(d):
        lvl = d.gap.rolling(W, min_periods=W // 2).median().shift(1)
        dev = d.gap - lvl
        d["_lvl"] = lvl
        return np.where(dev.abs() >= kdev, np.sign(dev), 0)
    return f


def band_exit(d, i, kk, yes):
    dev0 = d.gap[i] - d._lvl[i]
    dev = d.gap[kk] - d._lvl[i]                    # vs the level at entry
    sd = dev if yes else -dev
    return sd <= 0.5 * abs(dev0)


for kdev in (0.02, 0.03, 0.04):
    for win in (12, 24):
        for mk in (False, True):
            rows.append(summ(f"B band dev{int(kdev*100)}c lvl{win}h {'maker' if mk else 'taker'}",
                             run(band(kdev, win), band_exit, 24, mk)))


# ---- E: Poly-led lag
def lag(lb, m):
    L = int(lb * 6)

    def f(d):
        dp, ds = d.sharp - d.sharp.shift(L), d.mid - d.mid.shift(L)
        ok = (dp.abs() >= m) & ((ds * np.sign(dp)) < m / 3) & (np.sign(d.gap) == np.sign(dp)) & (d.gap.abs() >= m / 2)
        return np.where(ok, np.sign(dp), 0)
    return f


for lb in (1, 3, 6):
    for m in (0.02, 0.03):
        for mk in (False, True):
            rows.append(summ(f"E polylead {lb}h/{int(m*100)}c {'maker' if mk else 'taker'}", run(lag(lb, m), fg_exit, 24, mk)))

pd.set_option("display.width", 220)
print()
print(pd.DataFrame(rows).to_string(index=False))
