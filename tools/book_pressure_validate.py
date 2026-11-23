"""Validate the book-pressure (queue-imbalance) shadow signals against ACTUAL forward moves.

For each recorded signal (strategies/book_pressure: bids-heavy ⇒ predict UP / back YES; asks-heavy ⇒ predict
DOWN / buy NO), look up the mid `horizon` seconds later from the self-collected mid_history and check
whether the price actually moved the predicted way. Reports hit-rate and mean signed forward return by
horizon and by |QI| bucket.

VERDICT it informs:
  • continuation (hit >55%, mean signed return >0)  → book pressure PREDICTS the move → take #2 live.
  • reversion   (hit <45%, mean <0)                 → price REVERTS → flip #2 into a FADE (which is the
                                                       working version of order_flow's thesis).
  • inconclusive                                     → keep shadowing / need more data.

Run:  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/book_pressure_validate.py
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
SIGNALS = os.environ.get("BP_SIGNALS", "data/book_pressure_shadow/signals.jsonl")
HORIZONS_S = [60, 180, 300, 600, 900]      # 1, 3, 5, 10, 15 min
TOL_S = 120.0                               # accept a mid sample within this of the target time


def _epoch(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _load_mid_history(conn) -> dict:
    mh: dict[str, list] = defaultdict(list)
    for ex, ts, mid in conn.execute("SELECT exchange_id, ts, mid FROM mid_history"):
        t = _epoch(ts)
        if t is not None:
            mh[str(ex)].append((t, float(mid)))
    for ex in mh:
        mh[ex].sort()
    return mh


def _mid_at(mh: dict, ex: str, t: float) -> float | None:
    arr = mh.get(str(ex))
    if not arr:
        return None
    i = bisect.bisect_left(arr, (t,))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(arr) and (best is None or abs(arr[j][0] - t) < abs(best[0] - t)):
            best = arr[j]
    return best[1] if best and abs(best[0] - t) <= TOL_S else None


def _bucket(qi: float) -> str:
    a = abs(qi)
    if a >= 0.6:
        return "|QI|>=0.6"
    if a >= 0.45:
        return "0.45-0.6"
    return "0.33-0.45"


def main() -> int:
    conn = sqlite3.connect(DB)
    mh = _load_mid_history(conn)

    signals = []
    with open(SIGNALS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except ValueError:
                continue
            t = _epoch(s.get("ts"))
            if t is not None and s.get("mid_at_signal") is not None:
                signals.append((t, s))
    print(f"# book-pressure validation — {len(signals)} signals, mid_history {sum(len(v) for v in mh.values())} rows")

    for H in HORIZONS_S:
        # overall
        n = hits = 0
        edge_sum = 0.0
        by_bucket = defaultdict(lambda: [0, 0, 0.0])   # [n, hits, edge_sum]
        for t, s in signals:
            m0 = float(s["mid_at_signal"])
            m1 = _mid_at(mh, s.get("exch"), t + H)
            if m1 is None or not (0.0 < m0 < 1.0):
                continue
            fwd = m1 - m0                               # forward change in the YES mid
            signed = fwd if str(s.get("side")) == "yes" else -fwd   # +ve = moved the predicted way
            n += 1
            edge_sum += signed
            if signed > 0:
                hits += 1
            b = by_bucket[_bucket(float(s.get("qi", 0.0)))]
            b[0] += 1
            b[2] += signed
            if signed > 0:
                b[1] += 1
        if n == 0:
            print(f"\n+{H//60}min: no paired samples yet")
            continue
        print(f"\n+{H//60}min  n={n}  hit={100*hits/n:.0f}%  mean_signed_move={100*edge_sum/n:+.2f}c")
        for b in ("|QI|>=0.6", "0.45-0.6", "0.33-0.45"):
            v = by_bucket.get(b)
            if v and v[0]:
                print(f"    {b:<12} n={v[0]:<4} hit={100*v[1]/v[0]:.0f}%  mean={100*v[2]/v[0]:+.2f}c")

    print("\nread: hit>55% & mean>0 => continuation (take #2 live); hit<45% & mean<0 => reversion "
          "(flip to fade); else inconclusive. mean is in cents of YES-mid move in the predicted direction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
