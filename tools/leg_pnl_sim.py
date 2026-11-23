"""(A) REAL cross-market P&L by leg type from strategy_fills, marked to now.
(B) Capital-capped simulation: cross at ask on signal, sell at bid on convergence; all legs vs NO-only.
Leg type: held side + whether that market's YES mid is < 0.5 (underdog) or >= 0.5 (favourite).
  longshot-NO = NO on an underdog (YES<0.5); fav-YES = YES on a favourite; fav-NO; longshot-YES.
P&L of a fill marked to now = q * (mark of the outcome bought - price). Holds through pair-cancels."""
import collections, sqlite3, statistics
from datetime import datetime

db = sqlite3.connect("file:data/state.db?mode=ro", uri=True)
now_mid, now_hs = {}, {}
for ex, b, a in db.execute("SELECT exchange_id, best_bid, best_ask FROM prices WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL"):
    now_mid[ex] = (b + a) / 2; now_hs[ex] = max((a - b) / 2, 0.0)


def leg(outcome, yes_mid):
    under = yes_mid < 0.5
    return {("no", True): "longshot-NO", ("yes", False): "fav-YES",
            ("no", False): "fav-NO", ("yes", True): "longshot-YES"}[(outcome, under)]


def ep(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


# ---------- (A) real fills
fills = db.execute("""SELECT strategy, exchange_id, outcome, price, quantity, filled_at, mid_at_fill
                      FROM strategy_fills WHERE (strategy='cross_market_divergence' OR strategy LIKE 'exit:%'
                      OR strategy='trim') AND price IS NOT NULL ORDER BY filled_at""").fetchall()
entry_class = {}           # market -> leg type of its cross ENTRIES (first entry decides)
for st, ex, out, px, q, ts, m in fills:
    if st == "cross_market_divergence" and ex not in entry_class:
        ym = m if m is not None else (px if out == "yes" else 1 - px)
        entry_class[ex] = leg(out, ym)
t_first, t_last = ep(fills[0][5]), ep(fills[-1][5])
for start in ("2026-10-03T12:00:00Z", "2026-10-05T00:00:00Z"):
    s0 = ep(start); days = (t_last - s0) / 86400
    agg = collections.defaultdict(lambda: [0.0, 0.0, 0])   # mark-mid, mark-bid(liq), n
    for st, ex, out, px, q, ts, m in fills:
        if ep(ts) < s0 or ex not in entry_class or ex not in now_mid:
            continue
        mm = now_mid[ex] if out == "yes" else 1 - now_mid[ex]
        a = agg[entry_class[ex]]
        a[0] += q * (mm - px); a[1] += q * (mm - now_hs[ex] - px); a[2] += 1
    print(f"(A) REAL cross+exit+trim fills since {start}  ({days:.2f} days)")
    T = [0, 0]
    for k, (pm, pl, n) in sorted(agg.items()):
        print(f"    {k:13s} fills={n:5d}  P&L@mid ${pm:+9.0f}  (${pm / days:+7.0f}/day)  @bid ${pl:+9.0f}")
        T[0] += pm; T[1] += pl
    print(f"    {'TOTAL':13s}              P&L@mid ${T[0]:+9.0f}  (${T[0] / days:+7.0f}/day)  @bid ${T[1]:+9.0f}")

# ---------- (B) simulation, capital-capped
SIZE, TOL, CAP = 4400.0, 0.02, 80_000.0
sharp = collections.defaultdict(dict); mids = collections.defaultdict(dict)
for ex, ts, s in db.execute("SELECT exchange_id, ts, sharp FROM sharp_history WHERE sharp IS NOT NULL"):
    sharp[ex][ts[:16]] = float(s)
t0 = min(min(d) for d in sharp.values())
for ex, ts, m in db.execute("SELECT exchange_id, ts, mid FROM mid_history WHERE ts >= ?", (t0,)):
    if ex in sharp:
        mids[ex][ts[:16]] = float(m)
hs = {ex: max(v, 0.0025) for ex, v in now_hs.items()}


def thr(s):
    c = abs(s - 0.5) * 2
    return float("inf") if c < 1e-9 else max(0.05, 0.05 / c)


minutes = sorted({k for ex in sharp for k in sharp[ex] if k in mids[ex]})
span_days = (ep(minutes[-1] + ":00Z") - ep(minutes[0] + ":00Z")) / 86400


def sim(allowed):
    pos = {}; realized = collections.Counter(); n_in = collections.Counter(); n_out = collections.Counter()
    for k in minutes:
        # exits first (frees capital)
        for ex in list(pos):
            if k in sharp[ex] and k in mids[ex]:
                side, epx, q, lt = pos[ex]; s, m = sharp[ex][k], mids[ex][k]
                hm, hsh = (m, s) if side == "yes" else (1 - m, 1 - s)
                if hsh - hm <= TOL:
                    realized[lt] += q * (hm - hs.get(ex, .0025) - epx); n_out[lt] += 1; del pos[ex]
        cands = []
        for ex in sharp:
            if ex in pos or k not in sharp[ex] or k not in mids[ex]:
                continue
            s, m = sharp[ex][k], mids[ex][k]; gap = abs(s - m)
            if thr(s) <= gap < 0.25:
                side = "yes" if s > m else "no"; lt = leg(side, m)
                if lt in allowed:
                    cands.append((gap, ex, side, m, lt))
        for gap, ex, side, m, lt in sorted(cands, reverse=True):
            if len(pos) * SIZE + SIZE > CAP:
                break
            hm = m if side == "yes" else 1 - m
            epx = hm + hs.get(ex, .0025); pos[ex] = (side, epx, SIZE / epx, lt); n_in[lt] += 1
    unreal_mid = collections.Counter(); unreal_ev = collections.Counter()
    for ex, (side, epx, q, lt) in pos.items():
        last = max(kk for kk in sharp[ex] if kk in mids[ex]); s, m = sharp[ex][last], mids[ex][last]
        hm, hsh = (m, s) if side == "yes" else (1 - m, 1 - s)
        unreal_mid[lt] += q * (hm - hs.get(ex, .0025) - epx); unreal_ev[lt] += q * (hsh - epx)
    return realized, unreal_mid, unreal_ev, n_in, n_out, len(pos)


ALL = {"longshot-NO", "fav-YES", "fav-NO", "longshot-YES"}
print(f"\n(B) SIM {minutes[0]}..{minutes[-1]} ({span_days:.2f} days), ${SIZE:.0f}/pos, capital cap ${CAP:.0f}")
for name, allowed in (("all legs", ALL), ("NO legs only", {"longshot-NO", "fav-NO"}),
                      ("longshot-NO only", {"longshot-NO"})):
    r, um, ue, ni, no, op = sim(allowed)
    R, U, E = sum(r.values()), sum(um.values()), sum(ue.values())
    print(f"  {name:17s} entries={sum(ni.values()):3d} exits={sum(no.values()):3d} open={op:2d} | realized ${R:+7.0f} "
          f"| +open@bid ${R + U:+7.0f} (${(R + U) / span_days:+6.0f}/day) | +open@sharp ${R + E:+7.0f} (${(R + E) / span_days:+6.0f}/day)")
    for lt in sorted(ni):
        print(f"      {lt:13s} in={ni[lt]:3d} out={no[lt]:3d} realized ${r[lt]:+7.0f} open@bid ${um[lt]:+7.0f} open@sharp ${ue[lt]:+7.0f}")
