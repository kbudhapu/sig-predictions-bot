"""ROTATION RETUNE replay (2026-10-09). Replays the rotation rule minute-by-minute over sharp_history and scores every
swap exactly like tools/rotation_v2_validate.py (gain vs simply holding = new_shares*V(new) - sold_qty*V(sold)).

Why: the v2 shadow's 4 swaps were all good (+5..7 per $100 EV@sharp; +2.35/$100 @mid after 1d) but it fires ~1x/day.
Two rules starve it: (a) it may only BUY markets we don't hold, yet every >= 8c edge on SIG right now is in the book;
(b) margin 4c + 60 min persistence. Variants here: allow TOP-UPS of held markets up to the per-market cap, margin,
persistence, partial sells.

Book: the CURRENT venue positions held fixed as the starting book (approximation — the real book changed during the
window). Prices: sharp_history (minute; sharp, SIG bid/ask) + touch sizes from mid_history (nearest row <= t).
Execution: sell at bid, buy at ask, each capped at the displayed touch size. 4 swaps/hour max; a market touched by a
swap is frozen for 24h (no ping-pong). Scored @sharp (EV) and @mid (leaderboard marks) at +4h, +24h and window end.

Run: SNAP_DB=<snapshot> python tools/research/rotation_replay.py
"""
import bisect
import collections
import os
import sqlite3
from datetime import datetime

DB = os.environ.get("SNAP_DB", "data/state.db")
NAV = 89_300.0
CAP = 0.05 * NAV                     # per-market cap (capital.max_per_market_frac 0.05)
COST_FLOOR = 0.05


def ep(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def entry_threshold(sharp):
    conv = abs(sharp - 0.5) * 2.0
    return float("inf") if conv <= 1e-9 else max(COST_FLOOR, COST_FLOOR / conv)


def V(p, yes):                       # held-side value per share
    return p if yes else 1.0 - p


def load():
    c = sqlite3.connect(DB)
    snaps = collections.defaultdict(dict)
    for ex, ts, s, b, a in c.execute("select exchange_id, ts, sharp, sig_bid, sig_ask from sharp_history "
                                     "where sharp is not null and sig_bid is not null and sig_ask is not null"):
        snaps[ep(ts)][str(ex)] = (float(s), float(b), float(a))
    sizes = collections.defaultdict(lambda: ([], []))
    t0 = min(snaps)
    for ex, ts, bs, as_ in c.execute("select exchange_id, ts, bid_size, ask_size from mid_history where ts >= ?",
                                     (datetime.utcfromtimestamp(t0 - 600).isoformat(),)):
        try:
            t = ep(ts) if isinstance(ts, str) else float(ts)
        except Exception:  # noqa: BLE001
            continue
        sizes[str(ex)][0].append(t)
        sizes[str(ex)][1].append((float(bs or 0), float(as_ or 0)))
    book = {str(ex): float(q) for ex, q in c.execute("select exchange_id, quantity from positions where abs(quantity) >= 1")}
    return snaps, sizes, book


def touch(sizes, ex, t):
    ts, rows = sizes.get(ex, ([], []))
    i = bisect.bisect_right(ts, t) - 1
    return rows[i] if i >= 0 and t - ts[i] < 900 else (0.0, 0.0)


def run(snaps, sizes, book0, *, margin, persist_s, topup, partial, per_hour=4):
    times = sorted(snaps)
    book = dict(book0)
    since, frozen, recent, swaps = {}, {}, [], []
    for t in times:
        m = snaps[t]
        for ex, (s, b, a) in m.items():                         # persistence of each market's gap
            mid = (b + a) / 2
            gap, side = abs(s - mid), ("yes" if s > mid else "no")
            if gap < entry_threshold(s):
                since.pop(ex, None)
            elif ex not in since or since[ex][0] != side:
                since[ex] = (side, t)
        recent = [x for x in recent if t - x < 3600]
        if len(recent) >= per_hour:
            continue
        held = []
        for ex, q in book.items():
            if ex not in m or frozen.get(ex, -1e18) > t:
                continue
            s, b, a = m[ex]
            mid, yes = (b + a) / 2, q > 0
            val = abs(q) * (b if yes else 1 - a)
            if val < 500:
                continue
            rem = max((s - mid) if yes else (mid - s), 0.0)
            held.append((rem, ex, q, s, b, a))
        held.sort()
        if not held:
            continue
        cands = []
        for ex, (s, b, a) in m.items():
            if frozen.get(ex, -1e18) > t or ex not in since or t - since[ex][1] < persist_s:
                continue
            mid = (b + a) / 2
            gap, side = abs(s - mid), since[ex][0]
            if not (entry_threshold(s) <= gap < 0.25):
                continue
            q_have = book.get(ex, 0.0)
            if q_have and not topup:
                continue
            if q_have and ((q_have > 0) != (side == "yes")):
                continue                                         # never trade against our own holding
            px = a if side == "yes" else 1 - b
            room = CAP - abs(q_have) * (b if q_have > 0 else 1 - a) if q_have else CAP
            if room < 200:
                continue
            cands.append((gap, ex, side, s, b, a, px, room))
        cands.sort(reverse=True)
        for gap, ex, side, s, b, a, px, room in cands:
            rem, hx, hq, hs, hb, ha = held[0]
            if hx == ex:
                continue
            hs_c, hs_h = max((a - b) / 2, 0.0025), max((ha - hb) / 2, 0.0025)
            adv = gap - rem - 2 * (hs_c + hs_h)
            if adv < margin:
                break
            hyes = hq > 0
            exit_px = hb if hyes else 1 - ha
            bsz, asz = touch(sizes, hx, t)
            sell_sh = abs(hq)
            sell_sh = min(sell_sh, bsz if hyes else asz) if (bsz or asz) else sell_sh * 0.25
            buy_cap_sh = touch(sizes, ex, t)[1 if side == "yes" else 0] or 0.0
            spend = min(sell_sh * exit_px, room, buy_cap_sh * px if buy_cap_sh else room * 0.25)
            if not partial and spend < abs(hq) * exit_px - 1:
                break
            if spend < 200:
                break
            sold = spend / exit_px
            new = spend / px
            swaps.append(dict(t=t, sell=hx, sold=sold, syes=hyes, buy=ex, new=new, byes=(side == "yes"),
                              usd=spend, adv=adv))
            book[hx] = hq - (sold if hyes else -sold)
            book[ex] = book.get(ex, 0.0) + (new if side == "yes" else -new)
            frozen[hx] = frozen[ex] = t + 86400
            recent.append(t)
            break
    return swaps, times


def score(swaps, snaps, times, horizon):
    out_s, out_m, usd = 0.0, 0.0, 0.0
    end = times[-1]
    for w in swaps:
        tt = min(w["t"] + horizon, end) if horizon else end
        if horizon and w["t"] + horizon > end:
            continue
        i = bisect.bisect_right(times, tt) - 1
        m = snaps[times[i]]
        if w["buy"] not in m or w["sell"] not in m:
            continue
        sn, bn, an = m[w["buy"]]
        so, bo, ao = m[w["sell"]]
        out_s += w["new"] * V(sn, w["byes"]) - w["sold"] * V(so, w["syes"])
        out_m += w["new"] * V((bn + an) / 2, w["byes"]) - w["sold"] * V((bo + ao) / 2, w["syes"])
        usd += w["usd"]
    return out_s, out_m, usd


def main():
    snaps, sizes, book = load()
    times = sorted(snaps)
    hrs = (times[-1] - times[0]) / 3600
    print(f"window {datetime.utcfromtimestamp(times[0]):%m-%d %H:%M} .. {datetime.utcfromtimestamp(times[-1]):%m-%d %H:%M}"
          f" ({hrs:.0f}h), {len(times)} snapshots, book {len(book)} positions")
    print("variant                          swaps  $swapped |  EV@sharp  +4h@mid  +24h@mid  end@mid | per $100: end@mid")
    for topup in (False, True):
        for margin in (0.04, 0.03, 0.02):
            for persist in (3600, 1800, 0):
                for partial in (False, True):
                    if not topup and partial:
                        continue
                    sw, _ = run(snaps, sizes, book, margin=margin, persist_s=persist, topup=topup, partial=partial)
                    e_s, e_m, usd = score(sw, snaps, times, 0)
                    _, m4, _ = score(sw, snaps, times, 4 * 3600)
                    _, m24, u24 = score(sw, snaps, times, 24 * 3600)
                    ev0 = sum(w["adv"] * w["usd"] for w in sw)
                    name = f"{'topup' if topup else 'new-only'} m={margin*100:.0f}c p={persist//60:>2}m{' part' if partial else ''}"
                    print(f"{name:32s} {len(sw):5d} {usd:9,.0f} | {e_s:+9,.0f} {m4:+8,.0f} {m24:+9,.0f} {e_m:+8,.0f} | "
                          f"{(100*e_m/usd if usd else 0):+.2f}")


if __name__ == "__main__":
    main()
