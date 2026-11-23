"""SHADOW favorite-longshot / settlement-convergence scan (LOG-ONLY — places no orders).

The edge, stated plainly: in a binary market that SETTLES to 0 or 1, prices near the extremes are
systematically biased. The favorite-longshot bias says LONGSHOTS (YES very cheap) are OVER-priced —
people overpay for cheap lottery tickets — so the true probability sits BELOW the quote and you fade
them (buy NO ≡ sell YES). FAVORITES (YES very dear) are UNDER-priced — the mirror image — so you back
them (buy YES). As settlement (2026-11-04) nears, a correctly-classified extreme converges to 0/1 and
the gap is realised.

This scanner SURFACES the candidates and the trade each WOULD be. It does NOT place orders. Going live
must wait on evidence the bias actually holds on THIS board — so the scan also runs a falsification test
NOW, before settlement: for every candidate that ALSO has a Polymarket (real-money sharp) comparable, it
checks whether SIG's extreme is mispriced in the DIRECTION the bias predicts (SIG longshot dearer than
the sharp; SIG favorite cheaper than the sharp). If the sharp does NOT corroborate, the bias is not a
free lunch on this board and the candidate is flagged CONTRA.

Run on the droplet:
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/favorite_longshot_scan.py
Optional thresholds (defaults 0.08 / 0.92):
    PYTHONPATH=/opt/sig-bot .venv/bin/python tools/favorite_longshot_scan.py 0.08 0.92
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import time

from signals.mapping import load_race_mapping
from signals.poly_client import PolymarketClient
from signals.cross_market import resolve_external_prices
from signals.trade_flow import evaluate as flow_evaluate
from sig.sig_client import SigClient

SETTLE_ISO = "2026-11-04T17:00:00.000Z"


def _open_binary_markets(client: SigClient) -> list[dict]:
    """Every OPEN single-outcome (binary YES) market in the tournament, with its one YES exchange id."""
    out = []
    for m in client.markets_all():
        if not isinstance(m, dict) or m.get("status") != "open":
            continue
        exs = m.get("exchanges") or []
        if len(exs) != 1:                      # skip composite / multi-outcome
            continue
        ex = exs[0]
        eid = ex.get("id")
        if eid is None:
            continue
        out.append({"eid": str(eid), "title": m.get("question") or m.get("title") or "?",
                    "latest": ex.get("latestPrice")})
    return out


def _sig_touch(client: SigClient, exchange_ids: list[str]) -> dict[str, tuple]:
    """exchange_id -> (bid, ask, mid). Batched at 90/call (SIG caps the price endpoint)."""
    out: dict[str, tuple] = {}
    for i in range(0, len(exchange_ids), 90):
        resp = client.exchange_prices(exchange_ids[i:i + 90])
        for r in resp.get("data", []):
            b, a = r.get("bestBid"), r.get("bestAsk")
            if b is not None and a is not None:
                b, a = float(b), float(a)
                out[str(r.get("exchangeId"))] = (b, a, (b + a) / 2.0)
    return out


async def _poly_aligned_by_eid(mapping, pc) -> dict[str, float]:
    """SIG-aligned Polymarket YES prob keyed by SIG exchange id, for the active mapped races only."""
    out: dict[str, float] = {}
    for rm in mapping.values():
        if not (rm.sig_exchange_id and rm.is_active()):
            continue
        ref = rm.usable_externals().get("polymarket")
        if ref is None or not getattr(ref, "yes_token_id", None):
            continue
        raw = await pc.get_midpoint(ref.yes_token_id)
        if raw is None:
            continue
        aligned = resolve_external_prices(rm, {"polymarket": float(raw)})
        if aligned.get("polymarket") is not None:
            out[str(rm.sig_exchange_id)] = aligned["polymarket"]
    return out


def _days_to_settle() -> float:
    try:
        d = datetime.fromisoformat(SETTLE_ISO.replace("Z", "+00:00"))
        return max((d - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.0)
    except ValueError:
        return float("nan")


async def main():
    lo = float(sys.argv[1]) if len(sys.argv) > 1 else 0.08
    hi = float(sys.argv[2]) if len(sys.argv) > 2 else 0.92

    sig = SigClient(key_env="SIG_API_KEY_READ", timeout=30.0)
    markets = _open_binary_markets(sig)
    touch = _sig_touch(sig, [m["eid"] for m in markets])
    mapping = load_race_mapping()
    async with PolymarketClient(timeout=10.0) as pc:
        poly = await _poly_aligned_by_eid(mapping, pc)

    days = _days_to_settle()
    print(f"=== SHADOW favorite-longshot scan (log-only) — lo<={lo:.2f} hi>={hi:.2f}, "
          f"{days:.1f}d to settle, {len(markets)} open binary markets ===")
    # first pass: classify candidates (no per-candidate reads yet)
    raw = []
    for m in markets:
        eid = m["eid"]
        t = touch.get(eid)
        yes_mid = t[2] if t else (float(m["latest"]) if m["latest"] is not None else None)
        if yes_mid is None:
            continue
        if yes_mid <= lo:
            side, cost, long_yes = "buy NO", (1.0 - (t[0] if t else yes_mid)), False  # short YES
        elif yes_mid >= hi:
            side, cost, long_yes = "buy YES", (t[1] if t else yes_mid), True           # back YES
        else:
            continue
        cost = min(max(cost, 1e-4), 0.9999)
        ret = (1.0 - cost) / cost * 100.0                            # return-if-correct, per $ at risk
        p = poly.get(eid)
        edge = None
        flag = "no-sharp"
        if p is not None:
            edge = (p - yes_mid) if long_yes else (yes_mid - p)      # >0 == sharp backs the bias
            flag = "CORROBORATED" if edge > 0.005 else ("CONTRA" if edge < -0.005 else "flat")
        raw.append({"eid": eid, "title": m["title"], "side": side, "ymid": yes_mid, "cost": cost,
                    "ret": ret, "p": p, "edge": edge, "flag": flag, "long_yes": long_yes})

    # second pass: trade-tape taker-flow filter. One read/candidate, PACED under the 100/min budget.
    # Flow that runs AGAINST the fade is the informed-money veto (the wiki-loss failure mode).
    sig_w = SigClient(key_env="SIG_API_KEY_READ", timeout=20.0)
    for c in raw:
        fv = None
        for attempt in range(3):
            try:
                tape = sig_w.exchange_trades(c["eid"], limit=100).get("data", [])
                fv = flow_evaluate(tape, long_yes=c["long_yes"], min_volume=50, imbalance_veto=0.60)
                break
            except Exception as exc:                                  # 429 / transient -> back off
                if "429" in str(exc) and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
        c["flow"] = fv
        time.sleep(0.7)                                              # ~85 reads/min, under the cap

    print(f"{'market':44s} {'side':7s} {'YESmid':>7s} {'cost':>6s} {'ret%':>5s} "
          f"{'POLY':>6s} {'edge':>7s} {'flowImb':>7s} {'flow':>9s}  flag")

    order = {"CORROBORATED": 0, "flat": 1, "no-sharp": 2, "CONTRA": 3}
    raw.sort(key=lambda c: (order.get(c["flag"], 9), -c["ret"]))
    n_corr = n_contra = n_nosharp = n_flowveto = 0
    for c in raw:
        n_corr += c["flag"] == "CORROBORATED"
        n_contra += c["flag"] == "CONTRA"
        n_nosharp += c["flag"] == "no-sharp"
        fv = c.get("flow")
        imb = f"{fv.imbalance:+.2f}" if (fv and fv.imbalance is not None) else "-"
        freason = fv.reason if fv else "err"
        if fv and fv.contradicts:
            n_flowveto += 1
            freason = "VETO"
        print("%-44s %-7s %7.3f %6.3f %5.0f %6s %7s %7s %9s  %s" % (
            c["title"][:44], c["side"], c["ymid"], c["cost"], c["ret"],
            f"{c['p']:.3f}" if c["p"] is not None else "-",
            f"{c['edge']:+.3f}" if c["edge"] is not None else "-",
            imb, freason, c["flag"]))

    print(f"\n--- {len(raw)} candidates: {n_corr} CORROBORATED by sharp, {n_contra} CONTRA, "
          f"{n_nosharp} no-sharp. Trade-tape flow VETO on {n_flowveto} (informed flow against the "
          f"fade). SHADOW ONLY — no orders placed. ---")
    print("Go-live gate: fund only (sharp-CORROBORATED or settlement-proven) AND flow != VETO. The "
          "flow veto is the informed-money guard — the wiki-loss failure mode.")


asyncio.run(main())
