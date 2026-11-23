"""Validate the capital-ROTATION shadow — the gate before cross_market.rotation_mode goes `live`.

For every would-be swap in data/rotation_shadow/swaps.jsonl (sell a converged holding, buy a bigger new
divergence) it compares, at each horizon, what the swap would have done vs keeping the holding:
  swap_edge = [mark change of the NEW leg] − [mark change of the SOLD leg] − round-trip spread of both legs
using SIG mids from mid_history (held-leg space: YES for a long, NO for a short), plus the EV view at the sharp
(sharp_history) — did the gap we bought into actually pay more than the one we left?
Promote when mean swap_edge > 0 and good > bad at 4–24h over a few days. Local only.

Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/rotation_shadow_validate.py
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = "data/rotation_shadow/swaps.jsonl"
HORIZONS = (3600, 4 * 3600, 12 * 3600, 24 * 3600)


def _ep(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def main() -> None:
    if not os.path.exists(PATH):
        print("no rotation shadow records yet")
        return
    recs = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cache: dict[str, tuple[list, list]] = {}

    def series(ex):
        if ex not in cache:
            rows = db.execute("SELECT ts, mid FROM mid_history WHERE exchange_id=? ORDER BY ts", (ex,)).fetchall()
            cache[ex] = ([_ep(t) for t, _ in rows], [float(m) for _, m in rows])
        return cache[ex]

    def mid_at(ex, t):
        ts, ms = series(ex)
        i = bisect.bisect_left(ts, t)
        return ms[i] if i < len(ts) and ts[i] - t < 300 else None

    print(f"shadow swaps: {len(recs)}")
    for h in HORIZONS:
        vals = []
        for r in recs:
            t0 = _ep(r["ts"])
            s, b = r["sell"], r["buy"]
            ms1, mb1 = mid_at(s["exchange_id"], t0 + h), mid_at(b["exchange_id"], t0 + h)
            if ms1 is None or mb1 is None:
                continue
            sold_move = (ms1 - s["sig_mid"]) if s["qty"] > 0 else (s["sig_mid"] - ms1)    # per share, held-leg space
            new_move = (mb1 - b["sig_mid"]) if b["side"] == "yes" else (b["sig_mid"] - mb1)
            cost = 2 * (s["half_spread"] + b["half_spread"])
            vals.append(new_move - sold_move - cost)
        if vals:
            n = len(vals)
            print(f"  +{h // 3600:>2}h n={n:4d} mean swap_edge={100 * sum(vals) / n:+.2f}c/share  "
                  f"good={sum(v > 0 for v in vals) / n:.2f} bad={sum(v < 0 for v in vals) / n:.2f}")
        else:
            print(f"  +{h // 3600:>2}h n=0 (not enough elapsed time yet)")


if __name__ == "__main__":
    main()
