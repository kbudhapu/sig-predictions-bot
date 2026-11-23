"""Entry-policy backtest for cross-market (ideas 2/3/4). Read-only against data/state.db.

Signal: favourable gap (sharp - SIG mid, held-side) >= entry_threshold(sharp) and < 0.25.
Policies (one position per market at a time, $SIZE each):
  P0 cross   : buy at ask (mid + hs) the minute the signal fires
  P3 wait    : like P0 but skip minutes where the gap widened >= WID over the last 5 min (SIG running away)
  P4 rest-k  : post at the bid (mid - hs); fills only when a later mid <= bid - k*hs
               (k=0 touch, k=1 the ask came down to us). Cancel if not filled in REST_H hours or signal gone.
  P4f        : P4 k=1, but cross at the ask if still unfilled and signal still live after REST_H.
Exit: sell at bid when remaining gap <= TOL. Open positions at the end valued two ways:
  liq = at bid now; ev = at sharp (what they are worth if the sharp is right).
"""
import bisect, collections, sqlite3, statistics

DB = "file:data/state.db?mode=ro"
SIZE, TOL, WID, REST_H = 4400.0, 0.02, 0.01, 2.0

db = sqlite3.connect(DB, uri=True)


def minute(ts):
    return ts[:16]


sharp = collections.defaultdict(dict)
for ex, ts, s in db.execute("SELECT exchange_id, ts, sharp FROM sharp_history WHERE sharp IS NOT NULL"):
    sharp[ex][minute(ts)] = float(s)
t0 = min(min(d) for d in sharp.values())
mids = collections.defaultdict(dict)
for ex, ts, m in db.execute("SELECT exchange_id, ts, mid FROM mid_history WHERE ts >= ?", (t0,)):
    if ex in sharp:
        mids[ex][minute(ts)] = float(m)

# half-spread: median of recorded bid/ask since 05:15Z, else current prices table, floor 0.5c
hs = {}
for ex, sp in db.execute("SELECT exchange_id, spread FROM prices WHERE spread IS NOT NULL"):
    hs[ex] = float(sp) / 2
rec = collections.defaultdict(list)
for ex, b, a in db.execute("SELECT exchange_id, sig_bid, sig_ask FROM sharp_history WHERE sig_bid IS NOT NULL AND sig_ask > sig_bid"):
    rec[ex].append((a - b) / 2)
for ex, v in rec.items():
    hs[ex] = statistics.median(v)


def thr(s):
    c = abs(s - 0.5) * 2
    return float("inf") if c < 1e-9 else max(0.05, 0.05 / c)


def series(ex):
    keys = sorted(set(sharp[ex]) & set(mids[ex]))
    # forward-fill sharp across the joined minutes only
    return [(k, sharp[ex][k], mids[ex][k]) for k in keys]


def hour(k):
    return int(k[11:13])


def run(policy, k=1, fallback=False):
    trades = []   # dicts
    for ex in sharp:
        ser = series(ex)
        if len(ser) < 30:
            continue
        h = max(hs.get(ex, 0.005), 0.005)
        pos = None          # (side, entry_px_heldside, qty, entry_key)
        pend = None         # (side, limit_px_heldside, start_idx)
        gaps = []
        for i, (key, s, m) in enumerate(ser):
            side = "yes" if s > m else "no"
            gap = abs(s - m)
            gaps.append((side, gap))
            hm = m if side == "yes" else 1 - m          # mid in the favoured side's space
            if pos:
                pside, epx, q, ek = pos
                held_mid = m if pside == "yes" else 1 - m
                held_sharp = s if pside == "yes" else 1 - s
                if held_sharp - held_mid <= TOL:
                    trades.append(dict(ex=ex, hour=hour(ek), pnl=q * (held_mid - h - epx), closed=True))
                    pos = None
                continue
            live = thr(s) <= gap < 0.25
            if pend:
                pside, lim, si = pend
                held_mid = m if pside == "yes" else 1 - m
                if held_mid <= lim - k * h:                                   # traded through our bid
                    pos = (pside, lim, SIZE / lim, ser[si][0]); pend = None
                    continue
                expired = (i - si) >= REST_H * 60
                if not live or pside != side:
                    pend = None
                elif expired:
                    pend = None
                    if fallback:
                        pos = (side, hm + h, SIZE / (hm + h), ser[si][0])
                continue
            if not live:
                continue
            if policy == "P3" and i >= 5:
                pside5, g5 = gaps[i - 5]
                if pside5 == side and gap - g5 >= WID:
                    continue                                                  # still running away: wait
            if policy in ("P0", "P3"):
                pos = (side, hm + h, SIZE / (hm + h), key)
            else:
                pend = (side, hm - h, i)
        if pos:
            pside, epx, q, ek = pos
            key, s, m = ser[-1]
            held_mid = m if pside == "yes" else 1 - m
            held_sharp = s if pside == "yes" else 1 - s
            trades.append(dict(ex=ex, hour=hour(ek), pnl=q * (held_mid - h - epx),
                               ev=q * (held_sharp - epx), closed=False))
    return trades


def summ(name, tr):
    closed = [t for t in tr if t["closed"]]
    opn = [t for t in tr if not t["closed"]]
    rc = sum(t["pnl"] for t in closed)
    liq = sum(t["pnl"] for t in opn)
    ev = sum(t["ev"] for t in opn)
    day = [t for t in tr if 13 <= t["hour"] <= 23]
    night = [t for t in tr if not 13 <= t["hour"] <= 23]

    def tot(ts):
        return sum(t["pnl"] if t["closed"] else t["ev"] for t in ts)
    print(f"{name:10s} entries={len(tr):3d} closed={len(closed):3d} realized=${rc:+8.0f}  open={len(opn):3d} "
          f"liq=${liq:+8.0f} ev=${ev:+8.0f} | total(ev)=${rc + ev:+8.0f} liq-total=${rc + liq:+8.0f} "
          f"| day n={len(day)} ${tot(day):+.0f}  night n={len(night)} ${tot(night):+.0f}")


span = (t0, max(max(d) for d in sharp.values()))
print("span", span, "markets", len(sharp), "median hs c", round(100 * statistics.median(hs.values()), 2))
summ("P0 cross", run("P0"))
summ("P3 wait", run("P3"))
summ("P4 k=0", run("P4", k=0))
summ("P4 k=1", run("P4", k=1))
summ("P4f k=1", run("P4", k=1, fallback=True))

# --- idea 2 evidence: real MM fills by hour, and how they did 60 min later
mid_ts = collections.defaultdict(lambda: ([], []))
for ex, ts, m in db.execute("SELECT exchange_id, ts, mid FROM mid_history ORDER BY exchange_id, ts"):
    a, b = mid_ts[ex]; a.append(ts[:19]); b.append(float(m))


def mid_after(ex, ts, mins):
    a, b = mid_ts[ex]
    if not a:
        return None
    from datetime import datetime, timedelta
    t = (datetime.fromisoformat(ts[:19]) + timedelta(minutes=mins)).isoformat()[:19]
    i = bisect.bisect_left(a, t)
    return b[i] if i < len(a) and a[i][:16] <= (datetime.fromisoformat(t) + timedelta(minutes=5)).isoformat()[:16] else None


byh = collections.defaultdict(list)
for strat, ex, outc, px, q, ts in db.execute(
        "SELECT strategy, exchange_id, outcome, price, quantity, filled_at FROM strategy_fills "
        "WHERE strategy IN ('market_making','UNATTRIBUTED') AND price IS NOT NULL"):
    m60 = mid_after(ex, ts, 60)
    if m60 is None:
        continue
    held = m60 if outc == "yes" else 1 - m60
    byh[int(ts[11:13])].append((held - float(px), float(q)))
print("MM/unattributed fills by UTC hour: n, share-weighted edge after 60m (c/share)")
for hh in range(24):
    v = byh.get(hh, [])
    if v:
        w = sum(q for _, q in v)
        print(f"  {hh:02d}  n={len(v):5d}  edge60={100 * sum(e * q for e, q in v) / w:+.2f}c")
