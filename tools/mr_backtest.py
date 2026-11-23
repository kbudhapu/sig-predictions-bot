"""Minute mean-reversion BACKTEST on the candle history (offline research — places no orders).

The hypothesis: on LIQUID, RANGE-BOUND markets (not the extremes, which drift to settlement), the
1-minute price overshoots and reverts, and we — the only systematic participant in a field of humans —
can fade the overshoot. The question is never "does it mean-revert" (everything noisy does) but "does
it revert by MORE than the round-trip cost". This harness answers that NET of a conservative cost.

Method (per market):
  * pull 1m candles back over `--days`, build a regular grid (forward-fill close across sparse gaps),
  * z = (close - rolling_mean_W) / rolling_std_W,
  * ENTER when |z| >= k_entry (z>0 => fade: SHORT YES; z<0 => LONG YES),
  * EXIT when |z| <= k_exit, or z flips sign, or `hold_max` bars elapse,
  * PnL in prob space, MINUS `cost` per round-trip (buy ask / sell bid ≈ one spread). Report GROSS and
    NET so the toll is explicit.

Universe: auto-picked liquid + range-bound (mid in [lo,hi], enough candles), or pass explicit ids.
Costs dominate here by design — read the NET column, not the gross.

Run on the droplet:
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/mr_backtest.py --days 10 --W 20 --k-entry 2.0 --cost 0.01
"""
from __future__ import annotations

import argparse
import math
import time
from datetime import datetime, timedelta, timezone

from sig.sig_client import SigClient

RES_SECONDS = {"1m": 60, "5m": 300, "1h": 3600}


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _pull_candles(sig: SigClient, eid: str, *, resolution: str, days: float, max_pages: int = 30,
                  retry_429: bool = True):
    """Build (time, close, tradeCount) bars from the TRADE TAPE.

    NOTE: the venue's OHLCV endpoint (/price-history) returns EMPTY for this tournament at every
    resolution (coverage.complete=true, n=0) despite a live trade tape — so candles are unusable and we
    bucket the tape ourselves. Tape is cursor-paginated newest-first (<=200/page); we page back to the
    cutoff, then bucket the last trade in each `resolution` window as the bar close.
    """
    step = RES_SECONDS[resolution]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trades, cursor = [], None
    for _ in range(max_pages):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = sig.get(f"/exchanges/{eid}/trades", params=params)
        except Exception as exc:
            if retry_429 and "429" in str(exc):
                time.sleep(61.0)
                try:
                    resp = sig.get(f"/exchanges/{eid}/trades", params=params)
                except Exception:
                    break
            else:
                break
        data = resp.get("data", []) or []
        if not data:
            break
        trades += data
        oldest = _parse_iso(data[-1].get("createdAt"))        # newest-first => last is oldest
        cursor = (resp.get("pagination") or {}).get("nextCursor")
        if oldest is None or oldest <= cutoff or not cursor:
            break
        time.sleep(0.7)
    # bucket: last trade price in each step-window = bar close
    buckets: dict[int, list] = {}
    for tr in trades:
        t = _parse_iso(tr.get("createdAt"))
        px = tr.get("price")
        if t is None or px is None or t < cutoff:
            continue
        b = int(t.timestamp() // step)
        buckets.setdefault(b, []).append((t, float(px)))
    rows = []
    for b in sorted(buckets):
        items = sorted(buckets[b], key=lambda x: x[0])
        bar_t = datetime.fromtimestamp(b * step, tz=timezone.utc)
        rows.append((bar_t, items[-1][1], len(items)))         # close = last trade in bucket
    return rows


def _regular_grid(rows, step_s: int):
    """Forward-fill close onto a regular `step_s` grid so rolling windows are time-consistent."""
    if len(rows) < 2:
        return [], []
    t0, t1 = rows[0][0], rows[-1][0]
    n = int((t1 - t0).total_seconds() // step_s) + 1
    if n <= 0 or n > 200_000:
        return [], []
    grid = [t0 + timedelta(seconds=step_s * i) for i in range(n)]
    closes, j, last = [], 0, rows[0][1]
    for gt in grid:
        while j < len(rows) and rows[j][0] <= gt:
            last = rows[j][1]
            j += 1
        closes.append(last)
    return grid, closes


def _zscore_series(closes, W):
    z = [None] * len(closes)
    for i in range(W, len(closes)):
        win = closes[i - W:i]
        m = sum(win) / W
        var = sum((x - m) ** 2 for x in win) / W
        sd = math.sqrt(var)
        if sd > 1e-6:
            z[i] = (closes[i] - m) / sd
    return z


def _backtest_one(closes, z, *, mode, k_entry, k_exit, hold_max, cost, trail=0.02):
    """Walk the series, one position at a time. Returns (net_list, gross_list).

    mode='reversion': fade the overshoot (z>0 => SHORT YES, z<0 => LONG YES); exit on revert-to-mean.
    mode='breakout' : ride the move   (z>0 => LONG  YES, z<0 => SHORT YES); exit on a TRAILING stop
                      (retrace `trail` from the best favorable price), z-flip, or hold_max. Breakouts
                      must let winners run and cut losers, so the exit is a trail, not a mean-revert.
    """
    net, gross = [], []
    pos, entry_px, held, best = 0, 0.0, 0, 0.0
    for i in range(len(closes)):
        zi = z[i]
        px = closes[i]
        if pos == 0:
            if zi is None:
                continue
            if zi >= k_entry:
                pos = -1 if mode == "reversion" else +1
            elif zi <= -k_entry:
                pos = +1 if mode == "reversion" else -1
            if pos != 0:
                entry_px, held, best = px, 0, px
        else:
            held += 1
            best = max(best, px) if pos == +1 else min(best, px)
            if mode == "reversion":
                exit_now = (zi is not None and abs(zi) <= k_exit) or \
                           (zi is not None and ((pos == -1 and zi <= 0) or (pos == +1 and zi >= 0))) or \
                           (held >= hold_max)
            else:   # breakout: trailing stop on retrace from best, z-flip, or time
                retrace = (best - px) if pos == +1 else (px - best)
                exit_now = retrace >= trail or \
                           (zi is not None and ((pos == +1 and zi <= 0) or (pos == -1 and zi >= 0))) or \
                           (held >= hold_max)
            if exit_now:
                g = (px - entry_px) * pos
                gross.append(g)
                net.append(g - cost)
                pos = 0
    return net, gross


def _stats(xs):
    n = len(xs)
    if n == 0:
        return dict(n=0, hit=0.0, avg=0.0, total=0.0, sharpe=0.0)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / n) if n > 1 else 0.0
    hit = sum(1 for x in xs if x > 0) / n
    return dict(n=n, hit=hit, avg=m, total=sum(xs), sharpe=(m / sd * math.sqrt(n) if sd > 1e-9 else 0.0))


def _pick_universe(sig: SigClient, lo: float, hi: float, limit: int):
    """Liquid + range-bound: open binary markets with mid in [lo,hi], ranked by a cheap liquidity proxy."""
    cands = []
    for m in sig.markets_all():
        if not isinstance(m, dict) or m.get("status") != "open":
            continue
        exs = m.get("exchanges") or []
        if len(exs) != 1:
            continue
        p = exs[0].get("latestPrice")
        eid = exs[0].get("id")
        if p is None or eid is None:
            continue
        p = float(p)
        if lo <= p <= hi:
            cands.append((str(eid), m.get("question") or m.get("title") or "?", abs(p - 0.5)))
    cands.sort(key=lambda c: c[2])      # closest to 0.5 first (most range-bound / most contested)
    return cands[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=10.0)
    ap.add_argument("--resolution", default="1m", choices=list(RES_SECONDS))
    ap.add_argument("--W", type=int, default=20)
    ap.add_argument("--k-entry", type=float, default=2.0)
    ap.add_argument("--k-exit", type=float, default=0.5)
    ap.add_argument("--hold-max", type=int, default=30)
    ap.add_argument("--cost", type=float, default=0.01, help="round-trip cost in prob (≈ one spread)")
    ap.add_argument("--trail", type=float, default=0.02, help="breakout trailing stop (prob)")
    ap.add_argument("--mode", default="reversion", choices=["reversion", "breakout"])
    ap.add_argument("--lo", type=float, default=0.15)
    ap.add_argument("--hi", type=float, default=0.85)
    ap.add_argument("--markets", type=int, default=15)
    ap.add_argument("--ids", nargs="*", default=None)
    ap.add_argument("--max-pages", type=int, default=30, help="tape pages/market (200 trades each)")
    ap.add_argument("--sweep", action="store_true",
                    help="pull tape ONCE, then grid-sweep mode x k_entry x W in-memory")
    args = ap.parse_args()

    sig = SigClient(key_env="SIG_API_KEY_READ", timeout=30.0)
    if args.ids:
        universe = [(str(i), i, 0.0) for i in args.ids]
    else:
        universe = _pick_universe(sig, args.lo, args.hi, args.markets)
    step = RES_SECONDS[args.resolution]

    # pull bars ONCE per market (tape is the cost; params are free thereafter)
    series = {}
    for eid, title, _ in universe:
        rows = _pull_candles(sig, eid, resolution=args.resolution, days=args.days,
                             max_pages=args.max_pages)
        _, closes = _regular_grid(rows, step)
        series[eid] = (title, closes)

    def run_grid(mode, W, k_entry):
        zc = {}
        all_net, all_gross = [], []
        for eid, (_, closes) in series.items():
            if len(closes) < W + 5:
                continue
            z = zc.get((eid, W)) or _zscore_series(closes, W)
            zc[(eid, W)] = z
            net, gross = _backtest_one(closes, z, mode=mode, k_entry=k_entry, k_exit=args.k_exit,
                                       hold_max=args.hold_max, cost=args.cost, trail=args.trail)
            all_net += net
            all_gross += gross
        return _stats(all_net), _stats(all_gross)

    if args.sweep:
        print(f"=== sweep ({args.resolution}, {args.days}d, cost={args.cost}/rt, trail={args.trail}, "
              f"hold<={args.hold_max}) — AGGREGATE net over {len(series)} markets ===")
        print(f"{'mode':10s} {'W':>3s} {'k':>4s} {'N':>5s} {'hit%':>5s} {'gAvg':>8s} {'nAvg':>8s} "
              f"{'nTot':>7s} {'nSharpe':>7s}")
        for mode in ("reversion", "breakout"):
            for W in (20, 40, 60, 90):
                for k in (1.0, 1.5, 2.0, 2.5):
                    s, gs = run_grid(mode, W, k)
                    print("%-10s %3d %4.1f %5d %5.0f %+8.4f %+8.4f %+7.3f %7.2f" % (
                        mode, W, k, s["n"], s["hit"] * 100, gs["avg"], s["avg"],
                        s["total"], s["sharpe"]))
        print("\nBACKTEST ONLY — no orders. Read nAvg/nSharpe (NET of cost); gAvg is the raw signal.")
        return

    # single run (per-market detail)
    print(f"=== {args.mode} backtest ({args.resolution}, W={args.W}, k_entry={args.k_entry}, "
          f"k_exit={args.k_exit}, hold<={args.hold_max}, cost={args.cost}/rt, trail={args.trail}, "
          f"{args.days}d) ===")
    print(f"{'market':42s} {'bars':>5s} {'N':>4s} {'hit%':>5s} {'gAvg':>7s} {'nAvg':>7s} "
          f"{'nTot':>7s} {'nSharpe':>7s}")
    all_net, all_gross = [], []
    for eid, (title, closes) in series.items():
        if len(closes) < args.W + 5:
            print("%-42s %5d  (too few bars)" % (str(title)[:42], len(closes)))
            continue
        z = _zscore_series(closes, args.W)
        net, gross = _backtest_one(closes, z, mode=args.mode, k_entry=args.k_entry, k_exit=args.k_exit,
                                   hold_max=args.hold_max, cost=args.cost, trail=args.trail)
        all_net += net
        all_gross += gross
        s, gs = _stats(net), _stats(gross)
        print("%-42s %5d %4d %5.0f %+7.4f %+7.4f %+7.4f %7.2f" % (
            str(title)[:42], len(closes), s["n"], s["hit"] * 100,
            gs["avg"], s["avg"], s["total"], s["sharpe"]))

    agg, gagg = _stats(all_net), _stats(all_gross)
    print(f"\n--- AGGREGATE: {agg['n']} trades | gross avg {gagg['avg']:+.4f} | NET avg {agg['avg']:+.4f}"
          f" | net total {agg['total']:+.4f} | net hit {agg['hit']*100:.0f}% | net Sharpe {agg['sharpe']:.2f} ---")
    verdict = ("EDGE SURVIVES THE TOLL" if agg["avg"] > 0 and agg["sharpe"] > 1.0 else
               "GROSS-ONLY (dies at cost)" if gagg["avg"] > 0 >= agg["avg"] else "NO EDGE")
    print(f"VERDICT: {verdict}. BACKTEST ONLY — no orders.")


if __name__ == "__main__":
    main()
