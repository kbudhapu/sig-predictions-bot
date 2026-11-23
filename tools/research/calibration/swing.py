"""How exposed is the live book to a NATIONAL swing? Correlated settlement simulation + chamber-control hedge check.

Each race r has P(Dem wins) = p_r (Polymarket, Dem leg; R leg = 1 - p). Latent X_r = sqrt(rho) Z + sqrt(1-rho) e_r,
Dem wins iff X_r < Phi^-1(p_r): marginals stay exactly Polymarket's; rho = share of variance from one national swing
Z (Z < 0 = Democratic swing). Chamber-control markets: X_c = sqrt(rho_c) Z + ..., rho_c = 0.9 (control ~ the wave).
P&L measured from NOW: settlement payoff - current mark (mid) of each position."""
import json, re
import numpy as np, pandas as pd, yaml
from scipy.stats import norm

B = json.load(open("book_now.json"))
races = yaml.safe_load(open("race_mapping.yaml", encoding="utf-8"))["races"]
meta = {}
for key, r in races.items():
    ex = str((r.get("sig") or {}).get("exchange_id"))
    party = "dem" if key.endswith("_dem") else "rep" if key.endswith("_rep") else None
    base = key.rsplit("_", 1)[0] if party else key
    meta[ex] = dict(race=base, party=party, control="control" in key)
mid, sharp = B["mid"], B["sharp"]
rows, unk = [], []
for p in B["pos"]:
    ex, q = str(p["ex"]), float(p["qty"])
    m = meta.get(ex)
    if not m or m["party"] is None or ex not in mid:
        unk.append((ex, q)); continue
    s = sharp.get(ex)
    p_yes = float(s) if s is not None else float(mid[ex])           # no sharp -> use SIG mid as the probability
    p_dem = p_yes if m["party"] == "dem" else 1 - p_yes
    rows.append(dict(ex=ex, race=m["race"], party=m["party"], control=m["control"], qty=q, mid=float(mid[ex]),
                     p_dem=min(max(p_dem, 1e-4), 1 - 1e-4), has_sharp=s is not None))
P = pd.DataFrame(rows)
P["mark"] = np.where(P.qty > 0, P.qty * P.mid, -P.qty * (1 - P.mid))     # current value of the held side
# Dem-win exposure: $ payoff if the Dem wins this race minus if the Rep wins
P["dem_payoff"] = np.where(P.party == "dem", np.where(P.qty > 0, P.qty, 0.0), np.where(P.qty < 0, -P.qty, 0.0))
P["rep_payoff"] = np.where(P.party == "dem", np.where(P.qty < 0, -P.qty, 0.0), np.where(P.qty > 0, P.qty, 0.0))
P["dem_tilt"] = P.dem_payoff - P.rep_payoff
print(f"book: {len(P)} positions modelled (${P.mark.sum():,.0f} marked), {len(unk)} not modelled "
      f"(no party/price: {sum(1 for e,q in unk)} incl. small leftovers); NAV/cash/lb/rank = {B['nav']}")
print(f"  races {P.race.nunique()}; positions without a sharp (prob = SIG mid): {int((~P.has_sharp).sum())}")
print(f"  NET Dem-win tilt: if every race went Dem the book pays ${P.dem_payoff.sum():,.0f}; if every race went Rep "
      f"${P.rep_payoff.sum():,.0f}  (vs ${P.mark.sum():,.0f} marked now)")
print("  biggest single-race tilts ($ more if Dem wins):")
rt = P.groupby("race").dem_tilt.sum().sort_values()
print("   most pro-Rep:", rt.head(5).round(0).to_dict()); print("   most pro-Dem:", rt.tail(5).round(0).to_dict())

race_p = P.groupby("race").p_dem.first()                    # one probability per race (legs are consistent enough)
race_list = list(race_p.index)
thr = norm.ppf(race_p.values)
N = 40000
rng = np.random.default_rng(11)


def simulate(rho, hedge=None):
    Z = rng.standard_normal(N)
    E = rng.standard_normal((N, len(race_list)))
    X = np.sqrt(rho) * Z[:, None] + np.sqrt(1 - rho) * E
    dem = X < thr                                            # (N, races) Dem wins
    idx = {r: i for i, r in enumerate(race_list)}
    pnl = np.zeros(N)
    for r in P.itertuples():
        win_dem = dem[:, idx[r.race]]
        pnl += np.where(win_dem, r.dem_payoff, r.rep_payoff) - r.mark
    if hedge is not None:                                    # (shares of Dem-control YES, price, p_dem_control)
        h, px, pc = hedge
        Xc = np.sqrt(0.9) * Z + np.sqrt(0.1) * rng.standard_normal(N)
        demc = Xc < norm.ppf(pc)
        pnl += h * (np.where(demc, 1.0, 0.0) - px) if h > 0 else (-h) * (np.where(demc, 0.0, 1.0) - (1 - px))
    return Z, pnl


print("\nSettlement P&L from now (payoff - current mark), by how correlated races are:")
print(f"{'rho':>4s} {'mean':>8s} {'stdev':>8s} {'P(loss)':>7s} {'p5':>9s} {'p95':>8s} | {'Dem swing (Z<-1)':>17s} {'neutral':>8s} {'Rep swing (Z>1)':>16s}")
for rho in (0.0, 0.3, 0.6):
    Z, pnl = simulate(rho)
    print(f"{rho:4.1f} {pnl.mean():8,.0f} {pnl.std():8,.0f} {np.mean(pnl < 0):7.2f} {np.percentile(pnl, 5):9,.0f} "
          f"{np.percentile(pnl, 95):8,.0f} | {pnl[Z < -1].mean():17,.0f} {pnl[np.abs(Z) < 0.5].mean():8,.0f} {pnl[Z > 1].mean():16,.0f}")

# chamber-control hedge: SIG has Dem/Rep control markets for House and Senate (mapped as *_control_*).
ctrl = {ex: m for ex, m in meta.items() if m["control"]}
print("\nChamber-control markets on SIG (mid vs Polymarket):",
      {ex: (m["race"] + "_" + m["party"], round(float(mid.get(ex, np.nan)), 3), sharp.get(ex)) for ex, m in ctrl.items()})
house_dem = next((ex for ex, m in ctrl.items() if "house" in m["race"] and m["party"] == "dem"), None)
if house_dem and house_dem in mid:
    px = float(mid[house_dem]); pc = float(sharp.get(house_dem) or px)
    print(f"\nHedge with House-Dem-control YES (SIG mid {px:.3f}, Poly {pc:.3f}); rho=0.6:")
    for h in (-20000, -10000, 0, 10000, 20000, 40000):
        Z, pnl = simulate(0.6, (h, px, pc))
        print(f"  {'buy' if h >= 0 else 'sell'} {abs(h):6,d} sh (${abs(h) * (px if h >= 0 else 1 - px):7,.0f}): mean {pnl.mean():7,.0f} "
              f"stdev {pnl.std():7,.0f} P(loss) {np.mean(pnl < 0):.2f} p5 {np.percentile(pnl, 5):8,.0f} | "
              f"Dem swing {pnl[Z < -1].mean():7,.0f}  Rep swing {pnl[Z > 1].mean():7,.0f}")

# Senate control is priced at fair on SIG (Rep 0.352 vs Poly 0.355): a near-zero-EV hedge against a Republican swing.
sen_rep = next((ex for ex, m in ctrl.items() if "senate" in m["race"] and m["party"] == "rep"), None)
held = {r.ex: r.qty for r in P.itertuples()}
print(f"\ncurrently held control positions: { {ex: held.get(ex) for ex in ctrl} }")
if sen_rep and sen_rep in mid:
    px = float(mid[sen_rep]); pr = float(sharp.get(sen_rep) or px)
    print(f"Hedge with Senate-REP-control YES (SIG mid {px:.3f}, Poly {pr:.3f}); rho=0.6:")
    for h in (0, 5000, 10000, 20000, 30000):
        # buying Rep-control YES = paying out in a Rep swing; model it as SELLING Dem-control at (1-px), p_dem = 1-pr
        Z, pnl = simulate(0.6, (-h, 1 - px, 1 - pr))
        print(f"  buy {h:6,d} sh (${h * px:7,.0f}): mean {pnl.mean():7,.0f} stdev {pnl.std():7,.0f} "
              f"P(loss) {np.mean(pnl < 0):.2f} p5 {np.percentile(pnl, 5):8,.0f} | Dem swing {pnl[Z < -1].mean():7,.0f}  "
              f"neutral {pnl[np.abs(Z) < 0.5].mean():7,.0f}  Rep swing {pnl[Z > 1].mean():7,.0f}")
