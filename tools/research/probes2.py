"""Probes for ideas 1-4: our entry/exit impact, two-leg-consistent moves, parity entry timing."""
import sqlite3, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
M = pd.read_pickle("M.pkl"); P = pd.read_pickle("P.pkl")
pairs = pd.read_pickle("pairs.pkl")["pairs"]
db = sqlite3.connect("snap.db")
THREE = {"nebraska_senate", "south_dakota_senate"}
part = {}
for race, r, d in pairs:
    if race not in THREE:
        part[r], part[d] = d, r

f = pd.read_sql("SELECT strategy, order_id, exchange_id ex, filled_at, signed_qty, quantity, outcome, price, mid_at_fill "
                "FROM strategy_fills WHERE signed_qty IS NOT NULL AND price IS NOT NULL", db)
f["kind"] = np.where(f.strategy == "cross_market_divergence", "entry",
                     np.where(f.strategy.str.startswith("exit:") | (f.strategy == "trim"), "exit", "other"))
f = f[f.kind != "other"]
f["ts"] = pd.to_datetime(f.filled_at, utc=True)
f["t"] = f.ts.dt.floor("min")
f["s"] = np.sign(np.where(f.outcome == "yes", f.signed_qty, -f.signed_qty))      # YES-delta direction of our trade
f["q"] = f.signed_qty.abs()
# slippage vs mid at fill, in the bought outcome's space (+ = paid above mid)
f["mid_o"] = np.where(f.outcome == "yes", f.mid_at_fill, 1 - f.mid_at_fill)
f["slip_c"] = (f.price - f.mid_o) * 100 * np.sign(f.signed_qty)   # buy: paid above; sell: received below
print(f"signed entry fills {int((f.kind=='entry').sum())}, exit/trim fills {int((f.kind=='exit').sum())}, "
      f"{f.ts.min()} .. {f.ts.max()}")

# group fills into ORDERS (one decision) - order_id may be missing for multi-leg; fall back to (strategy, ex, minute)
f["oid"] = f.order_id.fillna("") + "|" + f.strategy + "|" + f.ex + "|" + f.t.astype(str)
O = f.groupby("oid").agg(kind=("kind", "first"), ex=("ex", "first"), t=("t", "min"), s=("s", "first"),
                         q=("q", "sum"), usd=("price", lambda x: 0), n_fills=("q", "size"),
                         vwap_slip=("slip_c", lambda x: np.average(x, weights=f.loc[x.index, "q"])))
O["usd"] = f.groupby("oid").apply(lambda g: (g.price * g.q).sum())


def path(ex, t, s):
    if ex not in M or t not in M.index:
        return None
    i = M.index.get_loc(t)
    if i < 2 or i + 240 >= len(M):
        return None
    m = M[ex]
    pre = m.iloc[i - 1]
    return {h: (m.iloc[i + h] - pre) * s * 100 for h in (1, 5, 15, 60, 240)}


for kind in ("entry", "exit"):
    sub = O[O.kind == kind]
    rows = []
    for oid, o in sub.iterrows():
        p = path(o.ex, o.t, o.s)
        if p:
            rows.append(dict(q=o.q, usd=o.usd, slip=o.vwap_slip, nf=o.n_fills, **{f"m{h}": v for h, v in p.items()}))
    R = pd.DataFrame(rows)
    print(f"\n== {'1' if kind=='entry' else '2'}. {kind.upper()} orders: n={len(R)}, median ${R.usd.median():.0f}, "
          f"median {R.q.median():.0f} shares, fills/order median {R.nf.median():.0f} ==")
    print(f"  slippage vs mid at fill (VWAP, c/share, + = cost): mean {R.slip.mean():+.2f}  median {R.slip.median():+.2f}")
    print("  mid move IN OUR TRADE'S DIRECTION vs the minute before (c): " +
          "  ".join(f"+{h}m {R[f'm{h}'].mean():+.2f}" for h in (1, 5, 15, 60, 240)))
    R["size"] = pd.qcut(R.usd, 3, labels=["small", "mid", "large"], duplicates="drop")
    print("  by order size  (push at +1m -> where it sits at +60m / +240m):")
    for sz, g in R.groupby("size"):
        print(f"     {sz:5s} n={len(g):3d} ${g.usd.median():6.0f}: push {g.m1.mean():+.2f}c -> {g.m60.mean():+.2f}c / {g.m240.mean():+.2f}c ; slip {g.slip.mean():+.2f}c")
    # what splitting into 2 halves 60m apart would have saved: second half bought at the +60m price instead of
    # walking the book now. Saving per share on that half = push_now - level_at_60m (both vs pre-trade mid).
    save = 0.5 * (R.slip + R.m1 - R.m60).clip(lower=-5, upper=5)
    print(f"  est. saving from doing half the order 60m later: {save.mean():+.2f}c/share on the whole order "
          f"(share of orders where it helps {(save>0).mean():.2f}); x median {R.q.median():.0f} sh = ${save.mean()/100*R.q.median():.0f}/order")

print("\n== 3. Two-leg-consistent moves (D and R move >=1c in OPPOSITE directions within 15m) ==")
ev = []
for r_ex, d_ex in [(r, d) for race, r, d in pairs if race not in THREE]:
    if r_ex not in M or d_ex not in M:
        continue
    dr, dd = M[r_ex].diff(15), M[d_ex].diff(15)
    cons = (dr.abs() >= 0.01) & (dd.abs() >= 0.01) & (np.sign(dr) == -np.sign(dd))
    st = cons & ~cons.shift(1, fill_value=False)
    for t in M.index[st.values]:
        ev.append((t, d_ex, np.sign(dd.loc[t])))      # direction of the D leg's YES move
E = pd.DataFrame(ev, columns=["t", "d_ex", "s"])
print(f"  events: {len(E)} ({len(E) / ((M.index[-1]-M.index[0]).total_seconds()/86400):.0f}/day)")
fw = []
for _, e in E.iterrows():
    i = M.index.get_loc(e.t)
    if i + 240 < len(M):
        fw.append({h: (M[e.d_ex].iloc[i + h] - M[e.d_ex].iloc[i]) * e.s * 100 for h in (60, 240)})
F = pd.DataFrame(fw)
print(f"  SIG continues (D leg, in move direction): +60m {F[60].mean():+.2f}c  +240m {F[240].mean():+.2f}c  "
      f"share continuing {(F[240]>0).mean():.2f}")
inP = E[E.t >= P.index[0]]
pf = []
for _, e in inP.iterrows():
    if e.d_ex not in P or e.t not in P.index:
        continue
    i = P.index.get_loc(e.t)
    if i + 240 < len(P) and i >= 15:
        pf.append(dict(before=(P[e.d_ex].iloc[i] - P[e.d_ex].iloc[i - 15]) * e.s * 100,
                       after=(P[e.d_ex].iloc[i + 240] - P[e.d_ex].iloc[i]) * e.s * 100))
PF = pd.DataFrame(pf)
if len(PF):
    print(f"  [16h with Poly, n={len(PF)}] Poly moved same way in the 15m BEFORE {PF.before.mean():+.2f}c ; "
          f"in the 4h AFTER {PF.after.mean():+.2f}c (share {(PF.after>0).mean():.2f})")
# 3B: cross-signal minutes (gap>=5c) where a consistent move in the last 15m went AGAINST the cross direction
cols = [c for c in P.columns if c in M.columns]
Ms = M.reindex(P.index)[cols]
G = P[cols] - Ms
live = G.abs() >= 0.05
against = pd.DataFrame(False, index=P.index, columns=cols)
for _, e in inP.iterrows():
    for leg, sgn in ((e.d_ex, e.s), (part.get(e.d_ex), -e.s)):
        if leg in cols:
            w = (P.index >= e.t) & (P.index < e.t + pd.Timedelta(minutes=30))
            against.loc[w, leg] = against.loc[w, leg] | (np.sign(G.loc[w, leg]) == -sgn)
fut = (Ms.shift(-240) - Ms) * np.sign(G) * 100
a = fut.where(live & against).stack().dropna(); b = fut.where(live & ~against).stack().dropna()
print(f"  3B cross signal live & a consistent move just went AGAINST it: n={a.size} next-4h SIG toward Poly {a.mean():+.2f}c ; "
      f"otherwise n={b.size} {b.mean():+.2f}c")

print("\n== 4. Parity entries right after a single-leg jump vs other parity entries ==")
rows = []
for race, r, d in pairs:
    if race in THREE:
        continue
    s = (M[r] + M[d] - 1) * 100
    brk = (s.abs() >= 2) & (s.shift(1).abs() < 2)
    for t in s.index[brk.values]:
        i = s.index.get_loc(t)
        if i < 15 or i + 240 >= len(s):
            continue
        jr, jd = abs(M[r].iloc[i] - M[r].iloc[i - 15]), abs(M[d].iloc[i] - M[d].iloc[i - 15])
        single_jump = max(jr, jd) >= 0.02 and min(jr, jd) < 0.005
        rows.append(dict(single=single_jump, close4h=(abs(s.iloc[i]) - abs(s.iloc[i + 240])),
                         close1h=(abs(s.iloc[i]) - abs(s.iloc[i + 60]))))
B = pd.DataFrame(rows)
for lab, g in (("after single-leg 2c+ jump", B[B.single]), ("other breaks", B[~B.single])):
    print(f"  {lab:26s}: n={len(g):4d} |dev| closes {g.close1h.mean():+.2f}c in 1h, {g.close4h.mean():+.2f}c in 4h "
          f"(share closing {(g.close4h>0).mean():.2f})")
