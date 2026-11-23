"""Validate the KALSHI shadow (kalshi_history, signals/kalshi_map.yaml) — the gate before Kalshi feeds cross-market.

  1. AGREEMENT: where a leg also has a Polymarket sharp, |Kalshi - Poly| per minute (median/p90) and how often they
     sit > 5c apart. Large or growing disagreement = don't trust either blindly on that race.
  2. KALSHI-ONLY legs (no Polymarket sharp): current Kalshi vs SIG gap, the gap averaged over the window, and whether
     SIG has moved TOWARD Kalshi since the first sample (the same convergence cross-market bets on).
Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/kalshi_shadow_validate.py
"""
from __future__ import annotations

import os
import sqlite3
import statistics

import yaml

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
MAP = os.environ.get("KALSHI_MAP", "signals/kalshi_map.yaml")


def minute(ts: str) -> str:
    return str(ts)[:16]


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    have = db.execute("SELECT count(*), min(ts), max(ts) FROM kalshi_history").fetchone()
    if not have or not have[0]:
        print("no kalshi_history rows yet")
        return
    names = {str(l["exchange_id"]): l["sig_key"] for l in (yaml.safe_load(open(MAP, encoding="utf-8")) or {}).get("legs", [])}
    print(f"kalshi_history rows {have[0]}  {have[1][:16]} .. {have[2][:16]}")
    K, P, M = {}, {}, {}
    for ex, ts, mid in db.execute("SELECT exchange_id, ts, mid FROM kalshi_history WHERE ts >= ?", (have[1],)):
        K.setdefault(ex, {})[minute(ts)] = mid
    for ex, ts, s in db.execute("SELECT exchange_id, ts, sharp FROM sharp_history WHERE ts >= ?", (have[1],)):
        P.setdefault(ex, {})[minute(ts)] = s
    for ex, ts, m in db.execute("SELECT exchange_id, ts, mid FROM mid_history WHERE ts >= ?", (have[1],)):
        M.setdefault(ex, {})[minute(ts)] = m
    diffs, far = [], 0
    for ex, kser in K.items():
        for t, k in kser.items():
            p = P.get(ex, {}).get(t)
            if p is not None and k is not None:
                d = abs(k - p); diffs.append(d); far += d > 0.05
    if diffs:
        diffs.sort()
        print(f"\n1. agreement (leg-minutes with both, n={len(diffs)}): |Kalshi-Poly| median {100*statistics.median(diffs):.1f}c "
              f"p90 {100*diffs[int(len(diffs)*.9)]:.1f}c max {100*diffs[-1]:.1f}c; >5c apart in {far} leg-minutes")
    only = [ex for ex in K if ex not in P or not P[ex]]
    print(f"\n2. Kalshi-only legs: {len(only)}")
    rows = []
    for ex in only:
        common = sorted(t for t in K[ex] if t in M.get(ex, {}))
        if not common:
            continue
        g = [100 * (K[ex][t] - M[ex][t]) for t in common]
        rows.append((names.get(ex, ex), ex, K[ex][common[-1]], M[ex][common[-1]], g[-1], statistics.mean(g),
                     abs(g[0]) - abs(g[-1]), len(common)))
    rows.sort(key=lambda r: -abs(r[4]))
    print(f"  {'leg':28s} {'ex':>5s} {'kalshi':>6s} {'SIG':>6s} {'gap now':>7s} {'avg gap':>7s} {'closed':>6s} {'mins':>5s}")
    for r in rows:
        print(f"  {r[0]:28s} {r[1]:>5s} {r[2]:6.3f} {r[3]:6.3f} {r[4]:+7.1f} {r[5]:+7.1f} {r[6]:+6.1f} {r[7]:5d}")
    print("  (gap = Kalshi - SIG in c, + = SIG below Kalshi; closed = how much |gap| shrank since the first sample)")


if __name__ == "__main__":
    main()
