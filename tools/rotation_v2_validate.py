"""Validate the ROTATION v2 shadow (sig/engine/rotation_v2.py).

Each virtual swap sold `q` shares of a holding at its bid and bought `new_shares` of a new market at its ask.
At each horizon it compares the SWAPPED book with simply HOLDING, per $100 of proceeds:
  @sharp : new_shares·V(sharp_new) − q·V(sharp_sold)   — the EV view (rotation's actual objective; the spreads
           paid are already inside it because the trade prices are the real bid/ask)
  @mid   : same with SIG mids                           — what the leaderboard marks look like
V(p) is the held-side value (p for YES, 1−p for NO). "entry" is the value locked at the decision.
Promote rotation only if @sharp stays positive as Polymarket moves AND @mid trends up over days.
Local only. Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/rotation_v2_validate.py
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("ROTATION_V2_PATH", "data/rotation_v2_shadow/events.jsonl")
HORIZONS = (("entry", 0), ("1h", 3600), ("4h", 4 * 3600), ("12h", 12 * 3600), ("1d", 86400),
            ("2d", 2 * 86400), ("3d", 3 * 86400))


def _ep(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def held_value(p: float, yes: bool) -> float:
    return p if yes else 1.0 - p


def swap_value(rec: dict, sharp_new: float, sharp_sold: float) -> float:
    """Swapped minus held, per $100 of proceeds, valuing each held side at the given prices."""
    s, b = rec["sell"], rec["buy"]
    q = abs(float(s["qty"]))
    v = float(b["new_shares"]) * held_value(sharp_new, b["side"] == "yes") - q * held_value(sharp_sold, float(s["qty"]) > 0)
    return 100.0 * v / max(float(s["proceeds"]), 1e-9)


def main() -> None:
    if not os.path.exists(PATH):
        print("no rotation v2 swaps yet")
        return
    recs = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    recs = [r for r in recs if r.get("event") == "swap"]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cache = {}

    def at(table, col, ex, t):
        k = (table, ex)
        if k not in cache:
            rows = db.execute(f"SELECT ts, {col} FROM {table} WHERE exchange_id=? AND {col} IS NOT NULL ORDER BY ts",
                              (ex,)).fetchall()
            cache[k] = ([_ep(a) for a, _ in rows], [float(v) for _, v in rows])
        ts, vs = cache[k]
        i = bisect.bisect_left(ts, t)
        if i < len(ts) and ts[i] - t < 300:
            return vs[i]
        return None

    latest = max((_ep(x) for x, in db.execute("SELECT MAX(ts) FROM mid_history")), default=None)
    print(f"v2 virtual swaps: {len(recs)} (each counted once)  distinct buys {len({r['buy']['exchange_id'] for r in recs})}"
          f"  distinct sells {len({r['sell']['exchange_id'] for r in recs})}")
    for lab, h in HORIZONS + (("now", None),):
        ev, mk = [], []
        for r in recs:
            t = (latest if h is None else _ep(r["ts"]) + h)
            if t is None or (h is not None and latest is not None and t > latest):
                continue
            if h == 0:
                ev.append(swap_value(r, r["buy"]["sharp"], r["sell"]["sharp"]))
                mk.append(swap_value(r, r["buy"]["sig_mid"], r["sell"]["sig_mid"]))
                continue
            sb, ss = at("sharp_history", "sharp", r["buy"]["exchange_id"], t), at("sharp_history", "sharp", r["sell"]["exchange_id"], t)
            mb, ms = at("mid_history", "mid", r["buy"]["exchange_id"], t), at("mid_history", "mid", r["sell"]["exchange_id"], t)
            if sb is not None and ss is not None:
                ev.append(swap_value(r, sb, ss))
            if mb is not None and ms is not None:
                mk.append(swap_value(r, mb, ms))
        if not ev and not mk:
            print(f"  {lab:>5s}: (not enough elapsed time yet)")
            continue
        f = lambda v: (f"{sum(v)/len(v):+6.2f} (pos {sum(x > 0 for x in v)/len(v):.2f}, n={len(v)})" if v else "   n/a")
        print(f"  {lab:>5s}: per $100 swapped  @sharp {f(ev)}   @mid {f(mk)}")


if __name__ == "__main__":
    main()
