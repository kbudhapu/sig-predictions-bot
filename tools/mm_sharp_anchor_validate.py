"""Validate the MM sharp-anchor SHADOW — the gate before market_making.sharp_anchor goes `live`.

Reads data/mm_shadow/sharp_anchor.jsonl (each row = an MM quote the fair-value guard WOULD have dropped:
a bid priced above sharp − margin, or an ask priced below sharp + margin) and joins it to the forward mid
from the self-collected mid_history table. Purely local — no venue calls.

The guard is worth promoting iff the quotes it drops would have been BAD fills, i.e. the market then moved
the way the sharp said:
  • dropped BID  (we'd have bought YES above fair) → good drop if the mid then FELL   (fwd move < 0)
  • dropped ASK  (we'd have sold YES below fair)  → good drop if the mid then ROSE   (fwd move > 0)
Reported per horizon: n, mean signed move in the "good-drop" direction (cents), and good/flat/bad rates.
Promote when the mean is positive and good > bad at the 15–60 min horizons on a few days of data.

Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/mm_sharp_anchor_validate.py
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("SHARP_ANCHOR_PATH", "data/mm_shadow/sharp_anchor.jsonl")
HORIZONS = (300, 900, 1800, 3600)


def _ep(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def main() -> None:
    if not os.path.exists(PATH):
        print(f"no shadow file at {PATH} (sharp_anchor not in shadow/live yet)")
        return
    recs = []
    with open(PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ser: dict[str, tuple[list[float], list[float]]] = {}
    for ex in {r["exchange_id"] for r in recs}:
        rows = db.execute("SELECT ts, mid FROM mid_history WHERE exchange_id=? ORDER BY ts", (ex,)).fetchall()
        ts, ms = [], []
        for t, m in rows:
            e = _ep(t)
            if e is not None:
                ts.append(e)
                ms.append(float(m))
        ser[ex] = (ts, ms)
    print(f"shadow rows={len(recs)}  markets={len(ser)}")
    for act in ("buy", "sell"):
        sub = [r for r in recs if r["action"] == act]
        print(f"\n=== dropped {'BIDS' if act == 'buy' else 'ASKS'} (n={len(sub)}) — good drop = mid moved "
              f"{'DOWN' if act == 'buy' else 'UP'} ===")
        for h in HORIZONS:
            vals = []
            for r in sub:
                ts, ms = ser.get(r["exchange_id"], ([], []))
                t0 = _ep(r["ts"])
                if t0 is None or not ts:
                    continue
                k = bisect.bisect_left(ts, t0 + h)
                if k >= len(ts) or ts[k] - (t0 + h) > 120:
                    continue
                move = ms[k] - float(r["mid"])
                vals.append(-move if act == "buy" else move)
            if not vals:
                print(f"  +{h // 60:>3}m  n=0")
                continue
            n = len(vals)
            print(f"  +{h // 60:>3}m  n={n:6d}  mean_good={100 * sum(vals) / n:+.3f}c  "
                  f"good={sum(v > 0 for v in vals) / n:.2f} flat={sum(v == 0 for v in vals) / n:.2f} "
                  f"bad={sum(v < 0 for v in vals) / n:.2f}")
    by = defaultdict(int)
    for r in recs:
        by[r["exchange_id"]] += 1
    print("\ntop markets by drops:", sorted(by.items(), key=lambda x: -x[1])[:10])


if __name__ == "__main__":
    main()
