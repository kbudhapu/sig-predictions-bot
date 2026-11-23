"""Election night 2024 on Polymarket (state races, 10-min path) -> what a 'buy the called winner on a lagging venue'
play is worth. Clock: 23:00Z = 6pm ET (first polls close). SIG 2026 trades until noon ET Nov 4 = 17:00Z."""
import json
import numpy as np, pandas as pd

legs = json.load(open("night2024.json"))
T0 = pd.Timestamp("2024-11-05T23:00Z"); END = pd.Timestamp("2024-11-06T17:00Z")
rows = []
for L in legs:
    h = pd.Series({pd.Timestamp(p["t"], unit="s", tz="UTC"): float(p["p"]) for p in L["hist"]}).sort_index()
    rows.append(dict(event=L["event"], q=L["question"], yes=L["yes"], vol=L["volume"], h=h))
D = pd.DataFrame(rows)


def at(h, t):
    s = h[h.index <= t]
    return float(s.iloc[-1]) if len(s) else np.nan


D["p0"] = [at(h, T0) for h in D.h]
win = D[D.yes == 1].copy()
print(f"legs {len(D)} (races {D.event.nunique()}); winners {len(win)}")
# decision time: first time the WINNER trades >= theta after 6pm ET
for th in (0.80, 0.90, 0.95):
    win[f"t{int(th*100)}"] = [next((t for t, p in h[h.index >= T0].items() if p >= th), pd.NaT) for h in win.h]
for th in (90, 95):
    hrs = (win[f"t{th}"] - T0).dt.total_seconds() / 3600
    pre = (win.p0 >= th / 100).sum()
    print(f"\nwinner >= {th}%: already before polls closed {pre}; first reached after 6pm ET in {hrs.notna().sum() - pre}; "
          f"never (by noon next day) {hrs.isna().sum()}")
    h2 = hrs[(win.p0 < th / 100) & hrs.notna()]
    if len(h2):
        print(f"  hours after 6pm ET when decided: p25 {h2.quantile(.25):.1f} median {h2.median():.1f} p75 {h2.quantile(.75):.1f} max {h2.max():.1f}")
        for cut in (3, 6, 12, 18):
            print(f"    decided within {cut:2d}h (by {(T0 + pd.Timedelta(hours=cut)):%H:%M}Z): {(h2 <= cut).sum()}")
# false calls: losers that touched >= 0.90 after 6pm ET
lose = D[D.yes == 0]
fc = [(r.event, round(max(r.h[r.h.index >= T0].max(), 0), 3)) for r in lose.itertuples() if r.h[r.h.index >= T0].max() >= 0.90]
print(f"\nFALSE CALLS (a loser traded >= 90% after polls closed): {len(fc)} {fc[:6]}")
fc80 = sum(1 for r in lose.itertuples() if r.h[r.h.index >= T0].max() >= 0.80)
print(f"losers that touched >= 80%: {fc80}")
# remaining move: winners not yet decided at 6pm ET
und = win[win.p0 < 0.90]
print(f"\nwinners below 90% at 6pm ET: {len(und)}; their 6pm price median {und.p0.median():.2f} -> remaining move median "
      f"{100 * (1 - und.p0).median():.0f}c, mean {100 * (1 - und.p0).mean():.0f}c")

# play: when the winner first hits THETA, buy on a venue lagging Poly by LAG minutes (price = Poly(t - LAG)) + 0.5c
print("\nPLAY: buy the called winner on a venue that still shows Poly's price from LAG min earlier (+0.5c spread).")
print("profit per race = shares x (1 - entry). (2024 had", D.event.nunique(), "races; SIG 2026 has ~117 D/R races)")
for th in (0.90, 0.95):
    col = f"t{int(th*100)}"
    cand = win[(win.p0 < th) & win[col].notna()]
    for lag in (10, 30, 60, 120):
        entry = np.array([at(h, t - pd.Timedelta(minutes=lag)) for h, t in zip(cand.h, cand[col])]) + 0.005
        edge = 1 - entry
        for usd in (1000, 3000):
            prof = (usd / entry) * edge
            print(f"  theta {th:.2f} lag {lag:3d}m size ${usd:,}: races {len(cand):2d} | avg entry {entry.mean():.3f} "
                  f"edge {100*edge.mean():4.1f}c | total ${prof.sum():8,.0f} | x117/45 races ~${prof.sum()*117/45:8,.0f}")
# what if the lag is zero? (SIG keeps up) and what a FALSE call costs
print("\nsanity: lag 0 (venue keeps up with Poly):",
      round(float(np.mean([1 - (at(h, t) + 0.005) for h, t in zip(win[(win.p0 < .9) & win.t90.notna()].h,
                                                                   win[(win.p0 < .9) & win.t90.notna()].t90)])) * 100, 2), "c avg edge at theta 0.90")

print("\n== HONEST PLAY: per race, buy the FIRST leg (winner or loser) that crosses THETA after 6pm ET; settle at outcome ==")
for th in (0.90, 0.95, 0.97):
    for lag in (0, 30, 60, 120):
        pnl = []; n_false = 0
        for ev, g in D.groupby("event"):
            best = None
            for r in g.itertuples():
                if r.p0 >= th:
                    continue                                  # already decided before polls closed: no trade
                after = r.h[r.h.index >= T0]
                hit = next((t for t, p in after.items() if p >= th), None)
                if hit is not None and (best is None or hit < best[0]):
                    best = (hit, r)
            if best is None:
                continue
            t, r = best
            entry = at(r.h, t - pd.Timedelta(minutes=lag)) + 0.005
            pnl.append((3000 / entry) * (r.yes - entry))
            n_false += r.yes == 0
        pnl = np.array(pnl)
        print(f"  theta {th:.2f} lag {lag:3d}m: trades {len(pnl):2d} false calls {n_false} | $3k/race total ${pnl.sum():8,.0f} "
              f"| per trade ${pnl.mean() if len(pnl) else 0:7,.0f} | x117/45 ~${pnl.sum()*117/45:8,.0f}")
