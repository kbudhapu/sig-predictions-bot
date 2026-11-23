"""Tiered per-market cap backtest for cross-market (capital-constrained, minute replay over the sharp window).

Entry: live rule (|gap| >= conviction threshold, < 25c), buy at ask (mid + half-spread), largest gap first.
Exit: sell at bid when remaining gap <= 2c (convergence), cash recycles. No rotation. Start flat with NAV cash.
A position may be TOPPED UP when its gap reaches a tier with a higher cap. Caps are on cost basis vs start NAV.
Scored: realized + open (at bid = liquidation, at sharp = EV) and a settlement Monte Carlo (each market resolves
YES with prob = sharp at window end) for outcome risk. Races share an outcome: D and R legs of one race are linked."""
import warnings
import numpy as np, pandas as pd
import yaml
from common import load_tape, minute_series, db
warnings.filterwarnings("ignore")

NAV = 88_000.0
M = minute_series("mid_history", "mid")
S = minute_series("sharp_history", "sharp")
cols = [c for c in S.columns if c in M.columns]
S = S[cols]; Ms = M.reindex(S.index)[cols]
c = db()
ba = pd.read_sql("SELECT exchange_id ex, ask - bid sp FROM mid_history WHERE bid IS NOT NULL AND ask > bid", c)
HS = (ba.groupby("ex").sp.median() / 2).clip(lower=0.0025)
hs = {ex: float(HS.get(ex, 0.0025)) for ex in cols}
t = load_tape()
days_t = (t.ts.max() - t.ts.min()).total_seconds() / 86400
vol = t[~t.ours].groupby("ex").usd.sum() / days_t
busy = set(vol[vol >= vol.quantile(2 / 3)].index)

# race links (D and R legs of one race resolve together) from the mapping
races = yaml.safe_load(open("race_mapping.yaml", encoding="utf-8"))["races"]
race_of, party_of = {}, {}
for key, r in races.items():
    ex = str((r.get("sig") or {}).get("exchange_id"))
    base = key.rsplit("_", 1)[0] if key.endswith(("_rep", "_dem")) else key
    race_of[ex] = base
    party_of[ex] = key.rsplit("_", 1)[-1]


def thr(s):
    cv = abs(s - 0.5) * 2
    return np.inf if cv < 1e-9 else max(0.05, 0.05 / cv)


POLICIES = {
    "flat 5% (current)": lambda gap, ex: 0.05,
    "8% if gap>=8c": lambda gap, ex: 0.08 if gap >= 0.08 else 0.05,
    "8% if gap>=8c & busy": lambda gap, ex: 0.08 if (gap >= 0.08 and ex in busy) else 0.05,
    "6/8/10% ladder (5/8/10c)": lambda gap, ex: 0.10 if gap >= 0.10 else (0.08 if gap >= 0.08 else 0.06),
    "flat 8% (control)": lambda gap, ex: 0.08,
}
minutes = S.index
Sv, Mv = S.values, Ms.values
col_i = {c: i for i, c in enumerate(cols)}


def run(cap_fn):
    cash = NAV
    pos = {}            # ex -> [side(+1 yes/-1 no), shares, cost]
    realized = 0.0
    n_entries = n_exits = 0
    for k in range(len(minutes)):
        srow, mrow = Sv[k], Mv[k]
        # exits
        for ex in list(pos):
            i = col_i[ex]; s, m = srow[i], mrow[i]
            if np.isnan(s) or np.isnan(m):
                continue
            side, sh, cost = pos[ex]
            hm, hshp = (m, s) if side > 0 else (1 - m, 1 - s)
            if hshp - hm <= 0.02:
                proceeds = sh * (hm - hs[ex])
                realized += proceeds - cost; cash += proceeds; n_exits += 1
                del pos[ex]
        # entries / top-ups, largest gap first
        cands = []
        for ex, i in col_i.items():
            s, m = srow[i], mrow[i]
            if np.isnan(s) or np.isnan(m):
                continue
            gap = abs(s - m)
            if not (thr(s) <= gap < 0.25):
                continue
            side = 1 if s > m else -1
            if ex in pos and pos[ex][0] != side:
                continue
            cands.append((gap, ex, side, m))
        for gap, ex, side, m in sorted(cands, reverse=True):
            cap = cap_fn(gap, ex) * NAV
            have = pos[ex][2] if ex in pos else 0.0
            room = min(cap - have, cash)
            if room < 100:
                continue
            px = (m if side > 0 else 1 - m) + hs[ex]
            sh = room / px
            if ex in pos:
                pos[ex][1] += sh; pos[ex][2] += room
            else:
                pos[ex] = [side, sh, room]
            cash -= room; n_entries += 1
    # end valuation
    i_end = len(minutes) - 1
    liq = ev = 0.0
    legs = []
    for ex, (side, sh, cost) in pos.items():
        i = col_i[ex]
        s = S[ex].ffill().iloc[i_end]; m = Ms[ex].ffill().iloc[i_end]
        hm, hshp = (m, s) if side > 0 else (1 - m, 1 - s)
        liq += sh * (hm - hs[ex]) - cost
        ev += sh * hshp - cost
        legs.append((ex, side, sh, cost, s))
    return dict(realized=realized, liq=liq, ev=ev, entries=n_entries, exits=n_exits, n_open=len(pos),
                deployed=sum(p[2] for p in pos.values()), max_pos=max([p[2] for p in pos.values()] or [0]),
                legs=legs)


def settle_mc(legs, n=20000, seed=7):
    """Settlement P&L distribution: each RACE draws one outcome; a D leg with sharp p wins w.p. p, its R partner
    loses in the same draw. Unlinked markets draw independently."""
    rng = np.random.default_rng(seed)
    by_race = {}
    for ex, side, sh, cost, p in legs:
        by_race.setdefault(race_of.get(ex, ex), []).append((ex, side, sh, cost, p))
    total = np.zeros(n)
    for race, ls in by_race.items():
        u = rng.random(n)
        p_ref = ls[0][4]
        yes_ref = u < p_ref                                  # outcome for the first leg's YES
        for j, (ex, side, sh, cost, p) in enumerate(ls):
            yes = yes_ref if j == 0 or party_of.get(ex) == party_of.get(ls[0][0]) else ~yes_ref
            win = yes if side > 0 else ~yes
            total += np.where(win, sh, 0.0) - cost
    return total


hours = (minutes[-1] - minutes[0]).total_seconds() / 3600
print(f"window {minutes[0]:%m-%d %H:%M} .. {minutes[-1]:%m-%d %H:%M} ({hours:.1f}h), {len(cols)} markets with a sharp, "
      f"start ${NAV:,.0f}; busy = top third by $/day ({len(busy)} mkts)")
print(f"{'policy':28s} {'entries':>7s} {'exits':>5s} {'open':>4s} {'deployed':>9s} {'max pos':>8s} | "
      f"{'realized':>8s} {'+open@bid':>9s} {'+open@Poly':>10s} | settle: {'mean':>7s} {'stdev':>7s} {'P(loss)':>7s} {'p5':>8s}")
for name, fn in POLICIES.items():
    r = run(fn)
    mc = settle_mc(r["legs"]) + r["realized"]
    print(f"{name:28s} {r['entries']:7d} {r['exits']:5d} {r['n_open']:4d} {r['deployed']:9,.0f} {r['max_pos']:8,.0f} | "
          f"{r['realized']:8,.0f} {r['realized'] + r['liq']:9,.0f} {r['realized'] + r['ev']:10,.0f} | "
          f"{mc.mean():7,.0f} {mc.std():7,.0f} {np.mean(mc < 0):7.2f} {np.percentile(mc, 5):8,.0f}")
