"""Validate the MM SHADOW decisions against actual forward mid moves — the gate before any live MM change.

Reads data/mm_shadow/decisions.jsonl (what the vol-aware spread + tape-toxicity WOULD have done, recorded
live but never acted on) and joins each decision to the mid `horizon` seconds later from the self-collected
mid_history table. Purely local — no venue calls.

Answers the two questions that decide whether to promote either lever into the live quoter:

  VOL-AWARE SPREAD — does scaling k with realized vol beat the fixed k=0.015?
    For a resting quote, the forward move over the horizon is the adverse excursion. A good half-width
    COVERS the move (move <= k) often enough to not get run over, while staying as tight as possible to win
    fills. We bucket by vol regime and compare coverage@base_k vs coverage@vol_k, and how much tighter vol_k
    is when it's calm (spread we'd reclaim -> more fills) without losing coverage.

  TAPE TOXICITY — do the flags predict the adverse move?
    ask flagged (heavy YES taker aggression) should precede mid UP (bad for a resting ask); bid flagged
    should precede mid DOWN. We report the directional hit-rate and mean adverse magnitude vs the unflagged
    base rate. A flag that doesn't beat base rate is noise and must not gate a live pull.

Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/mm_shadow_validate.py [--horizon 300]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
DECISIONS = os.environ.get("MM_SHADOW_PATH", "data/mm_shadow/decisions.jsonl")


def _epoch(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _load_mid_series() -> dict[str, tuple[list[float], list[float]]]:
    """exchange_id -> (sorted epochs, mids) from mid_history."""
    if not os.path.exists(DB):
        return {}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT exchange_id, ts, mid FROM mid_history ORDER BY exchange_id, ts").fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    series: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for r in rows:
        e = _epoch(r["ts"])
        if e is None:
            continue
        ts_list, mid_list = series[str(r["exchange_id"])]
        ts_list.append(e)
        mid_list.append(float(r["mid"]))
    return series


def _forward_mid(series, ex: str, t0: float, horizon: float, tol: float) -> float | None:
    """Mid at ~t0+horizon for a market (nearest sample within tol seconds), else None."""
    if ex not in series:
        return None
    ts_list, mid_list = series[ex]
    if not ts_list:
        return None
    target = t0 + horizon
    i = bisect.bisect_left(ts_list, target)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts_list):
            d = abs(ts_list[j] - target)
            if d <= tol and (best is None or d < best[0]):
                best = (d, mid_list[j])
    return best[1] if best else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=float, default=300.0, help="forward seconds (resting-quote adverse window)")
    ap.add_argument("--tol", type=float, default=90.0, help="max seconds from the horizon target to accept a mid")
    ap.add_argument("--move-eps", type=float, default=0.002, help="min |move| counted as a real directional move")
    args = ap.parse_args()

    if not os.path.exists(DECISIONS):
        print(f"no decisions file at {DECISIONS} — is mm_shadow.enabled true and has it ticked yet?")
        return
    series = _load_mid_series()

    rows = []
    with open(DECISIONS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # join forward move
    joined = []
    for r in rows:
        t0 = _epoch(r.get("ts"))
        m0 = r.get("mid")
        if t0 is None or m0 is None:
            continue
        m1 = _forward_mid(series, str(r.get("exchange_id")), t0, args.horizon, args.tol)
        if m1 is None:
            continue
        r = dict(r)
        r["_m0"] = float(m0)
        r["_fwd"] = float(m1) - float(m0)   # signed forward move
        joined.append(r)

    print(f"decisions={len(rows)}  joined_with_forward_mid={len(joined)}  horizon={args.horizon:.0f}s\n")
    if not joined:
        print("no decisions could be joined to a forward mid yet — let mid_history accumulate past the horizon.")
        return

    # ---- (1) vol-aware spread ----
    vol_ref = joined[0].get("vol_ref") or 0.01
    try:
        vol_ref = float(next((r["vol_ref"] for r in joined if r.get("vol_ref")), 0.01))
    except (KeyError, TypeError, ValueError):
        vol_ref = 0.01

    def _coverage(sample, kkey):
        n = len(sample)
        if not n:
            return 0.0
        return sum(1 for r in sample if abs(r["_fwd"]) <= r[kkey]) / n

    print("=== VOL-AWARE SPREAD (fixed base_k vs vol-scaled vol_k) ===")
    print(f"{'regime':<10}{'n':>6}{'mean|move|':>12}{'mean base_k':>13}{'mean vol_k':>12}"
          f"{'cov@base':>10}{'cov@vol':>9}")
    for label, pred in (("calm", lambda r: r.get("vol", 0) < vol_ref),
                        ("volatile", lambda r: r.get("vol", 0) >= vol_ref),
                        ("ALL", lambda r: True)):
        s = [r for r in joined if pred(r)]
        if not s:
            print(f"{label:<10}{0:>6}")
            continue
        mv = sum(abs(r["_fwd"]) for r in s) / len(s)
        bk = sum(r.get("base_k", 0.015) for r in s) / len(s)
        vk = sum(r.get("vol_k", 0.015) for r in s) / len(s)
        print(f"{label:<10}{len(s):>6}{mv:>12.4f}{bk:>13.4f}{vk:>12.4f}"
              f"{_coverage(s, 'base_k'):>10.2f}{_coverage(s, 'vol_k'):>9.2f}")
    calm = [r for r in joined if r.get("vol", 0) < vol_ref]
    if calm:
        tighter = sum((r.get("base_k", 0.015) - r.get("vol_k", 0.015)) for r in calm) / len(calm)
        print(f"\ncalm markets: vol_k is on avg {tighter:+.4f} tighter than base_k "
              f"({tighter/0.0001:.0f} ticks) -> fills reclaimed if coverage holds (cov@vol above).")
    print("  READ: promote vol-scaling only if cov@vol >= cov@base in VOLATILE (not run over) AND vol_k is\n"
          "  meaningfully tighter in CALM with cov@vol still high (tighter quote wins fills safely).\n")

    # ---- (2) tape toxicity ----
    flagged = [r for r in joined if r.get("toxic_side")]
    print("=== TAPE TOXICITY (does a flag precede the adverse move?) ===")
    if not flagged:
        print("no toxic_side flags in the joined set (toxicity_enabled false, or no market crossed the"
              " threshold). Enable mm_shadow.toxicity_enabled in the overlay to collect these.\n")
    else:
        # ask flag => predict mid UP (+fwd); bid flag => predict mid DOWN (-fwd)
        hits = adverse_mag = 0.0
        for r in flagged:
            pred_up = r["toxic_side"] == "ask"
            moved = r["_fwd"] if pred_up else -r["_fwd"]   # +ve => moved in predicted adverse direction
            if moved > args.move_eps:
                hits += 1
            adverse_mag += max(0.0, moved)
        n = len(flagged)
        base = [r for r in joined if not r.get("toxic_side")]
        base_mag = sum(abs(r["_fwd"]) for r in base) / len(base) if base else 0.0
        print(f"flagged={n}  directional_hit_rate={hits/n:.2f}  mean_adverse_move={adverse_mag/n:.4f}")
        print(f"unflagged base: n={len(base)}  mean|move|={base_mag:.4f}")
        print("  READ: promote a live pull only if hit_rate is clearly > 0.5 AND mean_adverse_move exceeds\n"
              "  the unflagged base move — otherwise the flag is noise and pulling just forgoes fills.\n")


if __name__ == "__main__":
    main()
