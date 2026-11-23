"""Validate the FAIR-BAND MAKER shadow (sig/engine/fair_band.py) against the recorded trade tape.

Each recorded quote (one per side per market per minute) is matched with trade_tape trades in the following minute
that hit it (YES buyer at >= our ask; YES seller at <= our bid). Fill models: PRO-RATA at the touch, BACK-OF-QUEUE
(only volume beyond the displayed depth; always used when we sit behind the touch). Virtual inventory per market,
capped at $20k exposure. Valued at the latest SIG mid and latest fair (Polymarket, else Kalshi); plus the 60-minute
markout of every fill. Reported overall and for the balanced / liquid groups.
Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/fair_band_validate.py
"""
from __future__ import annotations

import bisect
import collections
import json
import os
import sqlite3
from datetime import datetime, timedelta

from tools.maker_shadow_validate import fill_qty, hits

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("FAIR_BAND_PATH", "data/fair_band_shadow/quotes.jsonl")
CAP = 20000.0


def _ep(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def simulate(quotes: list[dict], trades: dict, pessimistic: bool, mid_at=None) -> dict:
    """Returns per-market {inv (signed YES shares), cash, turnover, mo60_num} plus totals."""
    inv = collections.defaultdict(float); cash = collections.defaultdict(float)
    turn = collections.defaultdict(float); mo = collections.defaultdict(float)
    for q in sorted(quotes, key=lambda r: r["ts"]):
        ex, side, px = q["exchange_id"], q["side"], float(q["px"])
        i = inv[ex]; mid = (q["bid"] + q["ask"]) / 2.0
        expo = i * mid if i > 0 else -i * (1 - mid)
        adds = (side == "bid" and i >= 0) or (side == "ask" and i <= 0)
        if adds and expo >= CAP:
            continue
        want = float(q["shares"])
        t0 = _ep(q["ts"]); t1 = t0 + timedelta(seconds=60)
        got, seen = 0.0, 0.0
        for ts, tside, tpx, tqty in trades.get(ex, ()):
            if ts < t0:
                continue
            if ts >= t1:
                break
            if not hits(side, px, tside, tpx):
                continue
            behind = not q.get("at_touch", True)
            f = fill_qty(tqty, q["depth"], want, pessimistic or behind, seen)
            seen += tqty
            got = min(want, got + f)
            if got >= want:
                break
        if adds:
            unit = px if side == "bid" else 1 - px
            got = min(got, max(0.0, (CAP - expo) / max(unit, 0.01)))
        if got <= 0.5:
            continue
        sgn = 1.0 if side == "bid" else -1.0
        inv[ex] += sgn * got; cash[ex] -= sgn * got * px
        turn[ex] += got * (px if side == "bid" else 1 - px)
        if mid_at is not None:
            m60 = mid_at(ex, t0 + timedelta(minutes=60))
            if m60 is not None:
                mo[ex] += got * (m60 - px) * sgn
    return {"inv": inv, "cash": cash, "turn": turn, "mo": mo}


def main() -> None:
    if not os.path.exists(PATH):
        print("no fair-band quotes yet")
        return
    quotes = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    start = min(q["ts"] for q in quotes)
    trades = collections.defaultdict(list)
    for ex, ts, side, px, qty in db.execute("SELECT exchange_id, created_at, side, price, size FROM trade_tape "
                                            "WHERE created_at >= ? ORDER BY created_at", (start[:19],)):
        trades[ex].append((_ep(ts), str(side).upper(), float(px), float(qty)))
    last_tape = max((v[-1][0] for v in trades.values() if v), default=None)
    if last_tape is None:
        print("no tape yet"); return
    cutoff = (last_tape - timedelta(minutes=75)).isoformat()
    qs = [q for q in quotes if q["ts"] <= cutoff]
    mids = collections.defaultdict(lambda: ([], []))
    for ex, ts, m in db.execute("SELECT exchange_id, ts, mid FROM mid_history WHERE ts >= ? ORDER BY ts", (start[:19],)):
        a, b = mids[ex]; a.append(_ep(ts)); b.append(float(m))

    def mid_at(ex, t):
        a, b = mids.get(ex, ([], []))
        i = bisect.bisect_left(a, t)
        return b[i] if i < len(a) and (a[i] - t).total_seconds() < 300 else None
    last_mid = {ex: b[-1] for ex, (a, b) in mids.items() if b}
    fair_last = {ex: s for ex, s in db.execute("SELECT exchange_id, sharp FROM sharp_history WHERE ts = (SELECT MAX(ts) "
                                               "FROM sharp_history s2 WHERE s2.exchange_id = sharp_history.exchange_id)")}
    for ex, m in db.execute("SELECT exchange_id, mid FROM kalshi_history WHERE ts = (SELECT MAX(ts) FROM kalshi_history k2 "
                            "WHERE k2.exchange_id = kalshi_history.exchange_id)"):
        fair_last.setdefault(ex, m)
    hours = (_ep(cutoff) - _ep(start)).total_seconds() / 3600 if qs else 0
    print(f"quotes {len(quotes)}; scored {len(qs)} up to {cutoff[:16]} ({hours:.1f}h); markets {len({q['exchange_id'] for q in qs})}")
    if not qs or hours <= 0:
        return
    groups = {"all": qs, "balanced": [q for q in qs if "balanced" in q.get("tags", [])],
              "liquid": [q for q in qs if "liquid" in q.get("tags", [])]}
    k = 24 / hours
    for name, g in groups.items():
        for pess in (False, True):
            r = simulate(g, trades, pess, mid_at)
            turn = sum(r["turn"].values())
            pm = sum(r["cash"][e] + r["inv"][e] * last_mid[e] for e in r["inv"] if e in last_mid)
            pf = sum(r["cash"][e] + r["inv"][e] * fair_last[e] for e in r["inv"] if e in fair_last)
            expo = sum(abs(v) * (last_mid.get(e, .5) if v > 0 else 1 - last_mid.get(e, .5)) for e, v in r["inv"].items())
            print(f"  {name:8s} {'back' if pess else 'pro':4s} | markets filled {sum(1 for v in r['turn'].values() if v > 0):3d} "
                  f"turnover/day ${turn * k:9,.0f} exposure ${expo:8,.0f} | P&L@mid/day {pm * k:+8,.0f} "
                  f"EV@fair/day {pf * k:+8,.0f} | mo60 {100 * sum(r['mo'].values()) / max(turn, 1):+.2f}c")


if __name__ == "__main__":
    main()
