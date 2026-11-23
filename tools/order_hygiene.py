"""Order hygiene (2026-10-06 cleanup). DRY RUN by default; nothing changes without --execute.

  (1) Lists every OPEN venue order with the strategy that placed it (order_attribution, then orders table).
  (2) --cancel-strategy NAME: cancel the venue's open orders attributed to NAME (e.g. market_making after it was
      unregistered — its resting reduce-only quotes otherwise sit on the book against cross-market positions).
  (3) --mark-stale: local `orders` rows still 'pending/open/partial/ambiguous' that the venue no longer has open
      (and are older than --min-age-min) are set to status 'stale_closed'. Their true outcome (filled vs
      cancelled) is unknown, so they are NOT relabelled as either. Local bookkeeping only — no venue writes.

Run (droplet):  PYTHONPATH=/opt/sig-bot .venv/bin/python tools/order_hygiene.py [--cancel-strategy market_making]
                [--mark-stale] [--execute]
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from sig.sig_client import SigAPIError, SigClient

DB = "data/state.db"
OPEN_STATUSES = ("pending", "open", "partial", "ambiguous")


def venue_open_orders(client) -> list[dict]:
    out, cursor = [], None
    for _ in range(25):
        params = {"limit": 200, **({"cursor": cursor} if cursor else {})}
        resp = client.get("/orders", params=params)
        rows = resp.get("data", []) if isinstance(resp, dict) else (resp or [])
        for r in rows:
            if isinstance(r, dict) and r.get("open", True) and r.get("id", r.get("orderId")) is not None:
                out.append({"order_id": str(r.get("id", r.get("orderId"))),
                            "exchange_id": str(r.get("exchangeId", r.get("exchange_id"))),
                            "side": r.get("side"), "action": r.get("action"),
                            "price": r.get("priceLimit", r.get("price")), "quantity": r.get("quantity")})
        pag = resp.get("pagination", {}) if isinstance(resp, dict) else {}
        cursor = pag.get("nextCursor")
        if not pag.get("hasMore") or not cursor:
            return out
    raise RuntimeError("too many pages of open orders to trust")


def attribution(db) -> dict[str, str]:
    m = {str(o): s for o, s in db.execute("SELECT broker_order_id, strategy FROM orders WHERE broker_order_id IS NOT NULL")}
    m.update({str(o): s for o, s in db.execute("SELECT broker_order_id, strategy FROM order_attribution")})
    return m


def stale_rows(db, open_ids: set[str], min_age_min: float, now: datetime) -> list[tuple]:
    cutoff = (now - timedelta(minutes=min_age_min)).isoformat()
    q = (f"SELECT idempotency_key, broker_order_id, strategy, created_at FROM orders "
         f"WHERE status IN ({','.join('?' * len(OPEN_STATUSES))}) AND created_at < ?")
    return [r for r in db.execute(q, (*OPEN_STATUSES, cutoff))
            if r[1] is None or str(r[1]) not in open_ids]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cancel-strategy")
    ap.add_argument("--mark-stale", action="store_true")
    ap.add_argument("--min-age-min", type=float, default=60.0)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()

    client = SigClient(timeout=30.0)
    venue = venue_open_orders(client)
    open_ids = {o["order_id"] for o in venue}
    db = sqlite3.connect(DB, timeout=30.0)
    owner = attribution(db)
    print(f"venue open orders: {len(venue)}  by strategy: "
          f"{dict(collections.Counter(owner.get(o['order_id'], 'UNATTRIBUTED') for o in venue))}")
    for o in venue:
        print(f"  {o['order_id']:>10s} ex={o['exchange_id']:>5s} {o['side']}/{o['action']} "
              f"px={o['price']} qty={o['quantity']} -> {owner.get(o['order_id'], 'UNATTRIBUTED')}")

    if a.cancel_strategy:
        # never cancel orders in a market owned by an isolated strategy (sig/engine/ownership.py) unless the
        # operator explicitly targets that owner — this tool uses a raw client that bypasses SigBroker's guard
        from sig.engine.ownership import MarketOwnership
        reg = MarketOwnership(os.environ.get("SIG_OWNERSHIP_PATH", "data/market_ownership.json"))
        targets = [o for o in venue if owner.get(o["order_id"]) == a.cancel_strategy
                   and reg.owner_of(o["exchange_id"]) in (None, a.cancel_strategy)]
        print(f"\ncancel {a.cancel_strategy}: {len(targets)} orders" + ("" if a.execute else "  (dry run)"))
        if a.execute:
            ok = 0
            for o in targets:
                for _ in range(5):
                    r = client.cancel_order_raw(o["order_id"])
                    if r.status == 429:
                        time.sleep(3)
                        continue
                    ok += 1 if r.status < 400 or r.status == 404 else 0
                    print(f"  cancel {o['order_id']}: HTTP {r.status}")
                    break
                time.sleep(2.5)                      # stay well under 30 writes/min
            print(f"  cancelled {ok}/{len(targets)}")

    if a.mark_stale:
        now = datetime.now(timezone.utc)
        rows = stale_rows(db, open_ids, a.min_age_min, now)
        print(f"\nstale local 'open' rows: {len(rows)} by strategy "
              f"{dict(collections.Counter(r[2] for r in rows).most_common(8))}" + ("" if a.execute else "  (dry run)"))
        if a.execute and rows:
            with db:
                db.executemany("UPDATE orders SET status='stale_closed', updated_at=? WHERE idempotency_key=?",
                               [(now.isoformat(), r[0]) for r in rows])
            print(f"  marked {len(rows)} rows stale_closed")


if __name__ == "__main__":
    try:
        main()
    except SigAPIError as e:
        raise SystemExit(f"SIG API error {e.status}")
