"""FRESH-DISLOCATION REVERSION backtest (2026-10-09). Needs edge_panel.pkl from edge_scan.py (same dir as SNAP_DB).

Entry (hourly grid): SIG mid moved >= TH_MOVE c in the last 24h AWAY from Polymarket (sign(move) == -sign(gap 24h ago))
and the gap to Poly is now >= TH_GAP c. Trade TOWARD Poly. One open trade per market.
Exit: gap closed to <= EXIT_FRAC of its entry size, or MAX_H hours. P&L = signed SIG mid change - round-trip spread.
Open trades at the end are marked at the last mid (no survivorship).
Finding (3.6 days, book data from 10-06): move>=3c gap>=2c -> n=44, +1.11c/share net, 75% hit, median hold 30h;
>=4c -> +1.30..1.48c. Fresh gaps snap back; OLD gaps don't (only 1 of 26 >=8c gaps closed in ~3 days).
"""
import os
import sys

import numpy as np
import pandas as pd

DIR = os.path.dirname(os.environ.get("SNAP_DB", "data/state.db")) or "."


def load():
    P = pd.read_pickle(os.path.join(DIR, "edge_panel.pkl"))[["ex", "t", "mid", "sharp", "spread"]]
    P["mid"] *= 100
    P["sharp"] *= 100
    P = P[P.t.dt.minute == 0].sort_values(["ex", "t"]).reset_index(drop=True)
    g = P.groupby("ex")
    P["past24"] = P.mid - g.mid.shift(24)
    P["gap"] = P.sharp - P.mid
    P["gap0"] = P.groupby("ex").gap.shift(24)
    P["spr"] = P.spread.fillna(0.6)
    return P


def bt(P, th_move=3.0, th_gap=2.0, exit_frac=0.5, max_h=48):
    res = []
    for ex, d in P.groupby("ex"):
        d = d.reset_index(drop=True)
        last_exit = -1
        for i in range(len(d)):
            if i <= last_exit:
                continue
            r = d.loc[i]
            if pd.isna(r.past24) or pd.isna(r.gap) or pd.isna(r.gap0):
                continue
            if not (abs(r.past24) >= th_move and np.sign(r.past24) == -np.sign(r.gap0) and abs(r.gap) >= th_gap):
                continue
            s = np.sign(r.gap)
            j_end = min(i + max_h, len(d) - 1)
            ex_i, reason = j_end, ("open" if i + max_h > len(d) - 1 else "timeout")
            for j in range(i + 1, j_end + 1):
                gj = d.loc[j, "gap"]
                if pd.notna(gj) and abs(gj) <= abs(r.gap) * exit_frac:
                    ex_i, reason = j, "closed"
                    break
            mv = s * (d.loc[ex_i, "mid"] - r.mid)
            res.append(dict(ex=ex, t=r.t, net=mv - (r.spr + d.loc[ex_i, "spr"]) / 2, h=ex_i - i, reason=reason))
            last_exit = ex_i
    return pd.DataFrame(res)


if __name__ == "__main__":
    P = load()
    for tm in (2, 3, 4):
        x = bt(P, th_move=tm)
        print(f"move>={tm}c: n={len(x)} net {x.net.mean():+.2f}c hit {(x.net > 0).mean():.0%} "
              f"median hold {x.h.median():.0f}h {x.reason.value_counts().to_dict()}")
    sys.exit(0)
