"""Measure the sniper's NET edge: convergence captured (does SIG move toward the sharp after a take?)
minus the spread paid to cross. Bucketed by the take's edge so we can set min_edge / max_cross from data.

Per snipe (data/sniper_shadow/snipes.jsonl: ts, exch, side, take_price, edge=|sharp−touch|, sig_bid/ask,
sharp), in YES-space:
  entry   = (sig_bid+sig_ask)/2                       the SIG YES mid at the take
  gap     = sharp − entry                              signed; the thesis is SIG converges to sharp
  conv(H) = (mid_history[exch @ ts+H] − entry) * sign(gap)   +ve = moved toward sharp (thesis working)
  spread  = take_price − entry           (buy YES)     how far past mid we crossed (the cost)
          = entry − (1 − take_price)      (buy NO)
  net(H)  = conv(H) − spread

Short-horizon conv UNDERSTATES the settlement edge (full convergence = |gap| at Nov 4), so read net as a
floor. Run:  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/sniper_validate.py
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
SNIPES = os.environ.get("SNIPES", "data/sniper_shadow/snipes.jsonl")
HORIZONS_S = [900, 1800, 3600, 7200]        # 15, 30, 60, 120 min (mid_history span caps the long ones)
TOL_S = 300.0
EDGE_BUCKETS = [(0.03, 0.05), (0.05, 0.08), (0.08, 1.0)]


def _epoch(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _load_mh(conn):
    mh = defaultdict(list)
    for ex, ts, mid in conn.execute("SELECT exchange_id, ts, mid FROM mid_history"):
        t = _epoch(ts)
        if t is not None:
            mh[str(ex)].append((t, float(mid)))
    for ex in mh:
        mh[ex].sort()
    return mh


def _mid_at(mh, ex, t):
    arr = mh.get(str(ex))
    if not arr:
        return None
    i = bisect.bisect_left(arr, (t,))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(arr) and (best is None or abs(arr[j][0] - t) < abs(best[0] - t)):
            best = arr[j]
    return best[1] if best and abs(best[0] - t) <= TOL_S else None


def _ebucket(e):
    for lo, hi in EDGE_BUCKETS:
        if lo <= e < hi:
            return f"edge[{lo:.2f},{hi:.2f})"
    return "edge[other]"


def main() -> int:
    conn = sqlite3.connect(DB)
    mh = _load_mh(conn)
    snipes = []
    with open(SNIPES) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except ValueError:
                continue
            t = _epoch(s.get("ts"))
            if t is not None:
                snipes.append((t, s))
    print(f"# sniper net-edge — {len(snipes)} snipes, mid_history {sum(len(v) for v in mh.values())} rows")
    print("# conv = move toward sharp; spread = cross paid; net = conv − spread (cents of YES-mid)\n")

    for H in HORIZONS_S:
        # agg[bucket] = [n, conv_sum, spread_sum, conv_hits]
        agg = defaultdict(lambda: [0, 0.0, 0.0, 0])
        tot = [0, 0.0, 0.0, 0]
        for t, s in snipes:
            try:
                sb, sa = float(s["sig_bid"]), float(s["sig_ask"])
                sharp = float(s["sharp"])
                tp = float(s["take_price"])
                edge = float(s.get("edge", 0.0))
                side = str(s.get("side"))
            except (KeyError, TypeError, ValueError):
                continue
            entry = (sb + sa) / 2.0
            if not (0.0 < entry < 1.0 and 0.0 < sharp < 1.0):
                continue
            fut = _mid_at(mh, s.get("exch"), t + H)
            if fut is None:
                continue
            gap = sharp - entry
            if gap == 0:
                continue
            conv = (fut - entry) * (1.0 if gap > 0 else -1.0)
            spread = (tp - entry) if side == "yes" else (entry - (1.0 - tp))
            b = agg[_ebucket(edge)]
            b[0] += 1; b[1] += conv; b[2] += spread; b[3] += 1 if conv > 0 else 0
            tot[0] += 1; tot[1] += conv; tot[2] += spread; tot[3] += 1 if conv > 0 else 0
        if tot[0] == 0:
            print(f"+{H//60}min: no paired samples")
            continue
        print(f"+{H//60}min  (n={tot[0]})")
        print("    %-18s %5s %8s %8s %8s %6s" % ("bucket", "n", "conv_c", "spread_c", "net_c", "conv%"))
        for lo, hi in EDGE_BUCKETS:
            key = f"edge[{lo:.2f},{hi:.2f})"
            v = agg.get(key)
            if not v or not v[0]:
                continue
            n = v[0]
            print("    %-18s %5d %+8.2f %8.2f %+8.2f %5.0f%%" %
                  (key, n, 100 * v[1] / n, 100 * v[2] / n, 100 * (v[1] - v[2]) / n, 100 * v[3] / n))
        n = tot[0]
        print("    %-18s %5d %+8.2f %8.2f %+8.2f %5.0f%%\n" %
              ("ALL", n, 100 * tot[1] / n, 100 * tot[2] / n, 100 * (tot[1] - tot[2]) / n, 100 * tot[3] / n))

    # PER-MARKET convergence at 60min (which races actually move toward the sharp)
    H = 3600
    perm = defaultdict(lambda: [0, 0.0, 0, 0.0])   # [n, conv_sum, conv_hits, gap_sum]
    for t, s in snipes:
        try:
            sb, sa, sharp = float(s["sig_bid"]), float(s["sig_ask"]), float(s["sharp"])
        except (KeyError, TypeError, ValueError):
            continue
        entry = (sb + sa) / 2.0
        if not (0.0 < entry < 1.0 and 0.0 < sharp < 1.0):
            continue
        fut = _mid_at(mh, s.get("exch"), t + H)
        if fut is None:
            continue
        gap = sharp - entry
        if gap == 0:
            continue
        conv = (fut - entry) * (1.0 if gap > 0 else -1.0)
        m = perm[str(s.get("exch"))]
        m[0] += 1; m[1] += conv; m[2] += 1 if conv > 0 else 0; m[3] += abs(gap)
    print(f"\n# per-market convergence @60min (top by snipe count) — does SIG move toward the sharp?")
    print("    %-7s %5s %8s %6s %8s" % ("exch", "n", "conv_c", "conv%", "avg_gap_c"))
    for ex, m in sorted(perm.items(), key=lambda kv: kv[1][0], reverse=True)[:12]:
        if m[0] < 5:
            continue
        n = m[0]
        print("    %-7s %5d %+8.2f %5.0f%% %8.2f" %
              (ex, n, 100 * m[1] / n, 100 * m[2] / n, 100 * m[3] / n))

    print("\nnet_c > 0 and rising with the edge bucket ⇒ thesis pays after cost. CAVEAT: mid_history is only "
          "~2h, so the 60/120min rows are thin and this says NOTHING about settlement convergence (weeks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
