"""Validate the LEG shadow — would a NO-only filter on cross-market entries have done better?

Each record in data/leg_shadow/signals.jsonl is a cross-market entry signal tagged with its leg type
(longshot-NO / fav-YES / fav-NO / longshot-YES) and `no_filter_skip` (a NO-only filter would drop it).
At each horizon it scores the leg we would have bought, in held-side space:
  move   = SIG mid change toward our side (c/share), minus a round trip of the market's half-spread
  closed = share of the entry gap to the sharp that has closed (sharp_history), 1.0 = fully converged
Promote the filter only if skipped legs are clearly worse than kept legs at 4-24h over several days.
Local only. Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/leg_shadow_validate.py
"""
from __future__ import annotations

import bisect
import collections
import json
import os
import sqlite3
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("LEG_SHADOW_PATH", "data/leg_shadow/signals.jsonl")
HORIZONS = (3600, 4 * 3600, 12 * 3600, 24 * 3600)


def _ep(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def score(rec: dict, mid1: float, sharp1: float | None, half_spread: float) -> tuple[float, float | None]:
    """(move net of round-trip spread, fraction of the entry gap closed) for one record, held-side space."""
    yes = rec["option_side"] == "yes"
    m0 = float(rec["sig_price"])
    move = (mid1 - m0) if yes else (m0 - mid1)
    gap0 = abs(float(rec["divergence"]))
    closed = None
    if sharp1 is not None and gap0 > 0:
        gap1 = (sharp1 - mid1) if yes else (mid1 - sharp1)
        closed = 1.0 - gap1 / gap0
    return move - 2.0 * half_spread, closed


def main() -> None:
    if not os.path.exists(PATH):
        print("no leg shadow records yet")
        return
    recs = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    hs = {ex: max(float(sp) / 2, 0.0025) for ex, sp in
          db.execute("SELECT exchange_id, spread FROM prices WHERE spread IS NOT NULL")}
    cache: dict[tuple[str, str], tuple[list, list]] = {}

    def series(table, col, ex):
        k = (table, ex)
        if k not in cache:
            rows = db.execute(f"SELECT ts, {col} FROM {table} WHERE exchange_id=? AND {col} IS NOT NULL ORDER BY ts",
                              (ex,)).fetchall()
            cache[k] = ([_ep(t) for t, _ in rows], [float(v) for _, v in rows])
        return cache[k]

    def at(table, col, ex, t):
        ts, vs = series(table, col, ex)
        i = bisect.bisect_left(ts, t)
        return vs[i] if i < len(ts) and ts[i] - t < 300 else None

    n_by = collections.Counter(r["leg_type"] for r in recs)
    print(f"leg shadow signals: {len(recs)}  by leg: {dict(n_by)}")
    for h in HORIZONS:
        agg = collections.defaultdict(list)
        for r in recs:
            t = _ep(r["ts"]) + h
            m1 = at("mid_history", "mid", r["exchange_id"], t)
            if m1 is None:
                continue
            s1 = at("sharp_history", "sharp", r["exchange_id"], t)
            mv, cl = score(r, m1, s1, hs.get(r["exchange_id"], 0.0025))
            agg[r["leg_type"]].append((mv, cl))
            agg["KEPT (NO legs)" if not r["no_filter_skip"] else "SKIPPED (YES legs)"].append((mv, cl))
        print(f"  +{h // 3600:>2}h")
        if not agg:
            print("     (not enough elapsed time yet)")
        for k in sorted(agg):
            v = agg[k]
            cls = [c for _, c in v if c is not None]
            print(f"     {k:20s} n={len(v):4d}  move={100 * sum(m for m, _ in v) / len(v):+.2f}c/share net  "
                  f"gap_closed={(sum(cls) / len(cls)) if cls else float('nan'):+.2f}  "
                  f"good={sum(m > 0 for m, _ in v) / len(v):.2f}")


if __name__ == "__main__":
    main()
