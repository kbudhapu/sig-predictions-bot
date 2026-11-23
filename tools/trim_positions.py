"""Trim positions whose COST-BASIS notional exceeds a per-market cap (fraction of NAV) — reduce-only.

Why cost basis: the risk gate's per-market cap is on cost basis (|qty|·avgCost), so trimming each position to
cap·NAV on that same measure leaves the gate ~zero room to re-add it (cross-market won't just buy it back).

For each over-cap position the EXCESS is sold on the HELD leg (qty>0 → sell YES; qty<0 → sell NO), sized to the
resting depth within `--leash` of the touch so a thin peer-to-peer book isn't walked: held YES sells into YES
bids ≥ best_bid − leash; held NO sells into YES asks ≤ best_ask + leash (a NO sale at 1 − ask). Limit price =
the worst level used, so the order is marketable but bounded. Anything the book can't absorb is left for a later
run (or settlement). DRY RUN by default; `--execute` places orders (strategy_id 'trim', attributed).

Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/trim_positions.py --cap 0.06 [--execute]
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time

from sig.sig_client import SigClient, SigAPIError

TICK = 0.005


def _retry(f, *a, **k):
    for _ in range(8):
        try:
            return f(*a, **k)
        except SigAPIError as e:
            if e.status == 429:
                time.sleep(3)
                continue
            raise
    raise RuntimeError("429 storm")


def _levels(book, key):
    out = []
    for lv in (book or {}).get(key) or []:
        try:
            out.append((float(lv["price"]), float(lv["quantity"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _cross_owned() -> set[str]:
    from signals.mapping import load_race_mapping
    return {str(r.sig_exchange_id) for r in load_race_mapping().values()
            if r.sig_exchange_id and r.is_active()}


def _race_of() -> dict[str, str]:
    import re
    pat = re.compile(r"will the (democratic|republican) party win the (.+?)\??$", re.I)
    out = {}
    for m in _retry(SigClient(timeout=30.0).markets_all):
        mm = pat.match((m.get("question") or m.get("title") or "").strip())
        for e in (m.get("exchanges") or []) if mm else []:
            out[str(e.get("id"))] = mm.group(2).strip().lower()
    return out


def _remaining_gaps() -> dict[str, float]:
    """exchange_id -> latest SIG-aligned sharp (sharp_history), for picking the leg with less edge left."""
    import sqlite3
    db = sqlite3.connect("file:data/state.db?mode=ro", uri=True)
    return {r[0]: float(r[1]) for r in db.execute(
        "SELECT exchange_id, sharp FROM sharp_history WHERE ts=(SELECT max(ts) FROM sharp_history)")}


def cluster_excess(pos: list[dict], nav: float, cluster_cap: float) -> dict[str, int]:
    """Per RACE over cluster_cap·NAV (cost basis): shares to sell, taken from the leg with the SMALLER remaining
    favourable gap to the sharp first (the leg that has converged most gives up the least edge)."""
    race = _race_of()
    sharp = _remaining_gaps()
    by_race: dict[str, list[dict]] = {}
    for p in pos:
        if abs(float(p["quantity"])) >= 1 and str(p["exchangeId"]) in race:
            by_race.setdefault(race[str(p["exchangeId"])], []).append(p)
    out: dict[str, int] = {}
    limit = cluster_cap * nav
    for r, legs in by_race.items():
        total = sum(abs(float(p["quantity"])) * float(p["avgCost"]) for p in legs)
        excess = total - limit
        if excess <= 0 or len(legs) < 1:
            continue
        def rem(p):
            q, ym, s = float(p["quantity"]), float(p["currentPrice"]), sharp.get(str(p["exchangeId"]))
            if s is None:
                return 0.0
            return max((s - ym) if q > 0 else (ym - s), 0.0)
        for p in sorted(legs, key=rem):
            if excess <= 0:
                break
            ac = float(p["avgCost"])
            n = min(int(abs(float(p["quantity"]))), math.ceil(excess / ac))
            out[str(p["exchangeId"])] = n
            excess -= n * ac
    return out


def plan(cap: float, leash: float, min_qty: int, close_unmapped: bool = False, cluster_cap: float | None = None,
         sell_markets: set | None = None):
    c = SigClient(timeout=30.0)
    acct = _retry(c.account)
    pos = _retry(c.portfolio_positions)
    nav = float(acct["balance"]) + float(pos["summary"]["totalMarketValue"])
    limit = cap * nav
    owned = _cross_owned() if close_unmapped else set()
    cl_ex = cluster_excess(pos["positions"], nav, cluster_cap) if cluster_cap else {}
    rows = []
    for p in pos["positions"]:
        qty = float(p["quantity"])
        if abs(qty) < 1 or p.get("settled"):
            continue
        cost = abs(qty) * float(p["avgCost"])
        if sell_markets:
            # operator-selected markets (capital rebalance): sell the WHOLE position, reduce-only, within depth
            if str(p["exchangeId"]) not in sell_markets:
                continue
            excess = int(abs(qty))
        elif cluster_cap:
            if str(p["exchangeId"]) not in cl_ex:
                continue
            excess = cl_ex[str(p["exchangeId"])]
        elif close_unmapped:
            # MM leftovers: anything in a market cross-market does NOT trade → close the whole position
            if str(p["exchangeId"]) in owned:
                continue
            excess = int(abs(qty))
        else:
            if cost <= limit:
                continue
            excess = math.ceil((cost - limit) / float(p["avgCost"]))
        ex = str(p["exchangeId"])
        book = _retry(c.orderbook, ex)
        held = "yes" if qty > 0 else "no"
        if held == "yes":
            bids = sorted(_levels(book, "bids"), key=lambda x: -x[0])
            if not bids:
                rows.append((ex, p, held, excess, 0, None, "no_bids"))
                continue
            floor = bids[0][0] - leash
            use = [(px, q) for px, q in bids if px >= floor - 1e-9]
            depth = sum(q for _, q in use)
            price = min(px for px, _ in use)                     # sell YES down to this
        else:
            asks = sorted(_levels(book, "asks"), key=lambda x: x[0])
            if not asks:
                rows.append((ex, p, held, excess, 0, None, "no_asks"))
                continue
            ceil_ = asks[0][0] + leash
            use = [(px, q) for px, q in asks if px <= ceil_ + 1e-9]
            depth = sum(q for _, q in use)
            price = round(1.0 - max(px for px, _ in use), 3)     # sell NO at 1 − worst YES ask used
        q = int(min(excess, depth, abs(qty)))
        rows.append((ex, p, held, excess, q, round(round(price / TICK) * TICK, 3),
                     "ok" if q >= min_qty else "below_min"))
    return nav, limit, rows


async def execute(rows, min_qty: int):
    from main_sig import _make_read_client, _resolve_tournament_id
    from sig.broker import SigBroker
    from infra.state.store import StateStore
    tid = _resolve_tournament_id(_make_read_client(), "midterm-elections")
    b = SigBroker(tournament_id=tid, tournament_slug="midterm-elections")
    store = await StateStore().open()
    try:
        for ex, p, held, excess, q, price, why in rows:
            if why != "ok" or q < min_qty:
                continue
            from sig.engine import ownership as _own_mod
            _own_mod.set_registry(None)                 # re-read: the bot may have claimed a market since start
            # venue-side expiry: whatever the book cannot fill now must not rest unmanaged
            r = await b.place_order(strategy_id="trim", market_id=ex, exchange_id=ex, side="sell",
                                    action="close", quantity=q, price=price, outcome=held, expiration_s=120)
            await asyncio.sleep(3.0)                    # gentle: shares the account's 30 writes/min with the bot
            print(f"  PLACED ex={ex} sell {held} q={q} @ {price} -> status={r.status} id={r.order_id} "
                  f"filled={getattr(r, 'filled_qty', None)}")
        n = await store.record_order_attribution(b.drain_attribution())
        print(f"attribution rows recorded: {n}")
    finally:
        await store.close()


def _gate_per_market_cap() -> float | None:
    """capital.max_per_market_frac from config/sig.yaml with the sig.local.yaml overlay applied (what the gate uses)."""
    try:
        import yaml
        cap = None
        for path in ("config/sig.yaml", "config/sig.local.yaml"):
            try:
                d = yaml.safe_load(open(path, encoding="utf-8")) or {}
            except FileNotFoundError:
                continue
            v = ((d.get("capital") or {}).get("max_per_market_frac"))
            cap = float(v) if v is not None else cap
        return cap
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=1.0, help="per-market cap as a fraction of NAV")
    ap.add_argument("--leash", type=float, default=0.01, help="max depth below/above the touch to sell into")
    ap.add_argument("--min-qty", type=int, default=10)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--cluster-cap", type=float, default=None,
                    help="trim each RACE (D+R legs) to this fraction of NAV, from the leg with less edge left")
    ap.add_argument("--close-unmapped", action="store_true",
                    help="close ENTIRE positions in markets cross-market doesn't trade (MM leftovers)")
    ap.add_argument("--allow-rebuy", action="store_true",
                    help="allow a --cap below the risk gate's per-market cap (cross_market WILL re-buy the excess)")
    ap.add_argument("--sell-markets", type=str, default="",
                    help="comma-separated exchange ids: sell the ENTIRE position in each (capital rebalance)")
    a = ap.parse_args()
    sell = {x.strip() for x in a.sell_markets.split(",") if x.strip()}
    gate_cap = _gate_per_market_cap()
    if a.execute and not sell and not a.close_unmapped and not a.cluster_cap and gate_cap and a.cap < gate_cap \
            and not a.allow_rebuy:
        print(f"REFUSED: --cap {a.cap} is below the risk gate's per-market cap {gate_cap}: cross_market re-buys the "
              f"trimmed excess (995 was re-bought within 30s on 2026-10-06). Use --cap >= {gate_cap} or --allow-rebuy.")
        return
    nav, limit, rows = plan(a.cap, a.leash, a.min_qty, a.close_unmapped, a.cluster_cap, sell or None)
    print(f"NAV {nav:,.2f}  cap {a.cap:.3f} → ${limit:,.0f} per market (cost basis)   leash {a.leash}")
    for ex, p, held, excess, q, price, why in rows:
        print(f"  ex={ex:>5} held={held:3} qty={p['quantity']:>8} cost=${abs(p['quantity'])*p['avgCost']:>8,.0f} "
              f"excess={excess:>6} sell_now={q:>6} @ {price}  [{why}]  {p['marketTitle'][:45]}")
    if a.execute:
        asyncio.run(execute(rows, a.min_qty))
    else:
        print("DRY RUN — nothing placed (add --execute)")


if __name__ == "__main__":
    main()
