"""Staged-entry backtest for cross-market. Per signal EPISODE (gap crosses into the live band), compare:
  A      : buy 100% at the ask at t0 (today's behaviour)
  B_h    : 50% at t0, 50% at t0+h if the signal is still live (else that half is never bought)
  C_h    : 100% at t0+h if still live
  F30    : like A, but if a two-leg-consistent move went AGAINST the signal in the prior 30m, wait 30m
           (then buy if still live)
Same $ budget per episode. Valued at t0+8h (or data end): liquidation (SIG mid - half-spread) and at the sharp.
Half-spread per market from fetched bid/ask medians (fallback 0.25c)."""
import sqlite3, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
M = pd.read_pickle("M.pkl"); P = pd.read_pickle("P.pkl")
pairs = pd.read_pickle("pairs.pkl")["pairs"]
db = sqlite3.connect("snap.db")
cols = [c for c in P.columns if c in M.columns]
P = P[cols]; Ms = M.reindex(P.index)[cols]
ba = pd.read_sql("SELECT exchange_id ex, sig_bid b, sig_ask a FROM sharp_history WHERE sig_bid IS NOT NULL AND sig_ask > sig_bid", db)
HS = ((ba.a - ba.b) / 2).groupby(ba.ex).median().clip(lower=0.0025)
hs = lambda ex: float(HS.get(ex, 0.0025))
BUDGET, H_VAL = 4400.0, 480
part = {}
for race, r, d in pairs:
    part[r], part[d] = d, r


def thr(s):
    c = abs(s - 0.5) * 2
    return np.inf if c < 1e-9 else max(0.05, 0.05 / c)


T = P.applymap(thr) if hasattr(P, "applymap") else P.map(thr)
G = P - Ms
live = (G.abs() >= T) & (G.abs() < 0.25)
start = live & ~live.shift(1, fill_value=False)
start.iloc[0] = False                                   # skip gaps already open when data begins

# two-leg-consistent move AGAINST the cross direction within the last 30m (per leg)
against = pd.DataFrame(False, index=P.index, columns=cols)
for ex in cols:
    pe = part.get(ex)
    if pe is None or pe not in M:
        continue
    a, b = M[ex].diff(15).reindex(P.index), M[pe].diff(15).reindex(P.index)
    cons = (a.abs() >= 0.01) & (b.abs() >= 0.01) & (np.sign(a) == -np.sign(b))
    against[ex] = (cons & (np.sign(a) == -np.sign(G[ex]))).rolling(30, min_periods=1).max().astype(bool)

idx = P.index
rows = []
for ex in cols:
    for t0 in idx[start[ex].values]:
        i0 = idx.get_loc(t0)
        side = 1 if G[ex].iloc[i0] > 0 else -1         # +1 buy YES, -1 buy NO
        h = hs(ex)

        def px(i):                                    # held-side ask
            m = Ms[ex].iloc[i]
            return (m if side > 0 else 1 - m) + h

        def ok(i):
            return i < len(idx) and bool(live[ex].iloc[i]) and np.sign(G[ex].iloc[i]) == side

        iv = min(i0 + H_VAL, len(idx) - 1)
        m_v, s_v = Ms[ex].iloc[iv], P[ex].iloc[iv]
        liq = (m_v if side > 0 else 1 - m_v) - h
        ev = (s_v if side > 0 else 1 - s_v)

        def book(buys):                               # list of (i, frac) actually executed
            sh = sum(BUDGET * f / px(i) for i, f in buys)
            cost = sum(BUDGET * f for i, f in buys)
            return dict(dep=cost, sh=sh, liq=sh * liq - cost, ev=sh * ev - cost,
                        avgpx=(cost / sh if sh else np.nan))

        out = {"A": book([(i0, 1.0)])}
        for hh in (60, 120):
            j = i0 + hh
            out[f"B{hh}"] = book([(i0, .5)] + ([(j, .5)] if ok(j) else []))
            out[f"C{hh}"] = book([(j, 1.0)] if ok(j) else [])
        if bool(against[ex].iloc[i0]):
            out["F30"] = book([(i0 + 30, 1.0)] if ok(i0 + 30) else [])
        else:
            out["F30"] = out["A"]
        for k, v in out.items():
            rows.append(dict(ex=ex, t0=t0, pol=k, side=side, filt=bool(against[ex].iloc[i0]), **v))

R = pd.DataFrame(rows)
n_ep = R[R.pol == "A"].shape[0]
print(f"window {idx[0]} .. {idx[-1]} ({(idx[-1]-idx[0]).total_seconds()/3600:.1f}h); signal episodes {n_ep} "
      f"in {R.ex.nunique()} markets; valued at +{H_VAL//60}h or data end")
A = R[R.pol == "A"].set_index(["ex", "t0"])
print(f"{'policy':6s} {'deployed':>9s} {'missed$':>8s} {'avg px vs A (c, on bought)':>27s} {'P&L liq':>9s} {'P&L @sharp':>11s} {'EV per $':>9s}")
for pol, g in R.groupby("pol", sort=False):
    g = g.set_index(["ex", "t0"])
    both = g.avgpx.notna()
    dpx = ((g.avgpx - A.avgpx)[both] * 100)
    w = g.dep[both]
    print(f"{pol:6s} {g.dep.sum():9.0f} {n_ep*BUDGET - g.dep.sum():8.0f} {np.average(dpx, weights=w) if w.sum() else np.nan:+27.2f} "
          f"{g.liq.sum():+9.0f} {g.ev.sum():+11.0f} {g.ev.sum()/max(g.dep.sum(),1)*100:+8.2f}%")
f = R[(R.pol.isin(["A", "F30"])) & R.filt]
print(f"\nF30 filter fired on {int(R[(R.pol=='A') & R.filt].shape[0])} episodes:")
for pol, g in f.groupby("pol"):
    print(f"   {pol}: deployed {g.dep.sum():.0f}  P&L liq {g.liq.sum():+.0f}  @sharp {g.ev.sum():+.0f}")
