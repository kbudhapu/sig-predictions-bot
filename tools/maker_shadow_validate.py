"""Validate the MAKER-ENTRY shadow (sig/engine/maker_shadow.py): resting touch quotes vs taking the ask.

Quotes (data/maker_shadow/quotes.jsonl, one per live gap per minute) are matched against trade_tape trades in the
following minute that hit our side at or through our price (YES buyer at >= our ask; YES seller at <= our bid).
Fill models: PRO-RATA (trade_qty * Q / (depth + Q)) and BACK-OF-QUEUE (only volume beyond the displayed depth).
Each market fills up to the $4.4k cap at Q = $500 per minute. TAKER baseline: on the first minute of each signal
episode, buy the full $4.4k at the ask (YES at ask / NO at 1 - bid).
Everything is valued at the latest SIG mid and the latest Polymarket price (held-side).
Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/maker_shadow_validate.py
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB = os.environ.get("SIG_STATE_DB", "data/state.db")
PATH = os.environ.get("MAKER_SHADOW_PATH", "data/maker_shadow/quotes.jsonl")
Q_USD, CAP_USD = 500.0, 4400.0


def _ep(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def held_px(side: str, px: float) -> float:
    """Price paid per share of the HELD outcome: a YES bid fill buys YES at px; a YES ask fill buys NO at 1-px."""
    return px if side == "bid" else 1.0 - px


def fill_qty(trade_qty: float, depth: float, q_shares: float, pessimistic: bool, seen_before: float) -> float:
    """Shares we'd get from one trade at our level. seen_before = volume at this level earlier in the same minute."""
    if pessimistic:
        after = seen_before + trade_qty
        return max(0.0, after - max(depth, seen_before))
    return trade_qty * q_shares / (depth + q_shares)


def hits(side: str, px: float, trade_side: str, trade_px: float) -> bool:
    if side == "ask":
        return trade_side == "YES" and trade_px >= px - 1e-9
    return trade_side == "NO" and trade_px <= px + 1e-9


def simulate(quotes: list[dict], trades_by_ex: dict, pessimistic: bool) -> dict:
    pos = collections.defaultdict(lambda: [0.0, 0.0])          # ex -> [held shares, cost]
    side_of = {}
    for q in sorted(quotes, key=lambda r: r["ts"]):
        ex = q["exchange_id"]
        if ex in side_of and side_of[ex] != q["side"]:
            continue                                            # never flip a held position
        hp = held_px(q["side"], q["px"])
        room = CAP_USD - pos[ex][1]
        if room <= 1 or hp <= 0:
            continue
        q_sh = min(Q_USD, room) / hp
        t0 = _ep(q["ts"]); t1 = t0 + timedelta(seconds=60)
        got, seen = 0.0, 0.0
        for tts, tside, tpx, tqty in trades_by_ex.get(ex, ()):
            if tts < t0 or tts >= t1 or not hits(q["side"], q["px"], tside, tpx):
                continue
            f = fill_qty(tqty, q["depth"], q_sh, pessimistic, seen)
            seen += tqty
            got = min(q_sh, got + f)
            if got >= q_sh:
                break
        if got > 0:
            pos[ex][0] += got; pos[ex][1] += got * hp; side_of[ex] = q["side"]
    return {ex: (p[0], p[1], side_of[ex]) for ex, p in pos.items() if p[0] > 0}


def taker(quotes: list[dict]) -> dict:
    out, last_seen = {}, {}
    for q in sorted(quotes, key=lambda r: r["ts"]):
        ex, t = q["exchange_id"], _ep(q["ts"])
        new_episode = ex not in last_seen or (t - last_seen[ex]).total_seconds() > 300
        last_seen[ex] = t
        if new_episode and ex not in out:
            px = q["ask"] if q["side"] == "bid" else 1.0 - q["bid"]     # cross the spread on the held outcome
            out[ex] = (CAP_USD / px, CAP_USD, q["side"])
    return out


def value(book: dict, mid: dict, sharp: dict) -> tuple[float, float, float]:
    cost = sum(c for _, c, _ in book.values())
    v_mid = v_sh = 0.0
    for ex, (sh, c, side) in book.items():
        if ex in mid:
            v_mid += sh * (mid[ex] if side == "bid" else 1 - mid[ex]) - c
        if ex in sharp:
            v_sh += sh * (sharp[ex] if side == "bid" else 1 - sharp[ex]) - c
    return cost, v_mid, v_sh


def main() -> None:
    if not os.path.exists(PATH):
        print("no maker shadow quotes yet")
        return
    quotes = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    start = min(q["ts"] for q in quotes)
    tr = collections.defaultdict(list)
    for ex, ts, side, px, qty in db.execute("SELECT exchange_id, created_at, side, price, size FROM trade_tape "
                                            "WHERE created_at >= ? ORDER BY created_at", (start[:19],)):
        tr[ex].append((_ep(ts), str(side).upper(), float(px), float(qty)))
    last_tape = max((t[-1][0] for t in tr.values() if t), default=None)
    # only score quotes the tape has already caught up with (the poller laps each market ~hourly)
    cutoff = (last_tape - timedelta(minutes=75)).isoformat() if last_tape else start
    qs = [q for q in quotes if q["ts"] <= cutoff]
    mid = {ex: m for ex, m in db.execute("SELECT exchange_id, mid FROM mid_history WHERE ts = (SELECT MAX(ts) FROM "
                                         "mid_history m2 WHERE m2.exchange_id = mid_history.exchange_id)")}
    sharp = {ex: s for ex, s in db.execute("SELECT exchange_id, sharp FROM sharp_history WHERE ts = (SELECT MAX(ts) "
                                           "FROM sharp_history s2 WHERE s2.exchange_id = sharp_history.exchange_id)")}
    hours = (_ep(cutoff) - _ep(start)).total_seconds() / 3600 if qs else 0
    print(f"quotes recorded {len(quotes)}; scored {len(qs)} (tape caught up to {cutoff[:16]}); {hours:.1f}h; "
          f"markets quoted {len({q['exchange_id'] for q in qs})}")
    if not qs:
        return
    print(f"{'policy':22s} {'mkts':>4s} {'deployed':>9s} {'avg held px':>11s} | {'P&L@mid':>8s} {'per $100':>8s} | "
          f"{'EV@Poly':>8s} {'per $100':>8s}")
    for name, book in (("TAKER (cross the ask)", taker(qs)),
                       ("maker, pro-rata", simulate(qs, tr, False)),
                       ("maker, back-of-queue", simulate(qs, tr, True))):
        cost, vm, vs = value(book, mid, sharp)
        shares = sum(s for s, _, _ in book.values())
        print(f"{name:22s} {len(book):4d} {cost:9,.0f} {cost / max(shares, 1):11.4f} | {vm:8,.0f} "
              f"{100 * vm / max(cost, 1):+8.2f} | {vs:8,.0f} {100 * vs / max(cost, 1):+8.2f}")
    # like-for-like: markets BOTH filled — entry price improvement of maker vs taker
    tk, mk = taker(qs), simulate(qs, tr, True)
    common = [ex for ex in mk if ex in tk]
    if common:
        imp = [100 * (tk[ex][1] / tk[ex][0] - mk[ex][1] / mk[ex][0]) for ex in common]
        print(f"\nsame markets (back-of-queue filled {len(common)}): maker entry is {sum(imp)/len(imp):+.2f}c/share "
              f"cheaper than taking the ask on average; maker filled ${sum(mk[e][1] for e in common):,.0f} "
              f"of ${CAP_USD * len(common):,.0f} wanted")


if __name__ == "__main__":
    main()
