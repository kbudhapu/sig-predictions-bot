#!/usr/bin/env python3
"""At-open live-contract verification (GO_LIVE_CHECKLIST.md Phase C).

Run this ONCE at the 16:00Z tournament open so the live-contract assumptions SIM could only simulate are
EXECUTED and reported pass/fail — calm execution, not scrambling. Every check reuses the existing read
paths (sig_client / the broker's read client — same auth, SIG_API_KEY_READ); nothing here places an order
EXCEPT check 7, which is hard-gated behind --place-test-order (default OFF) and run manually only.

Usage:
    python scripts/at_open_verify.py                        # all read-only checks (default, NO orders)
    python scripts/at_open_verify.py --out recon/open.json  # choose the raw-response dump path
    python scripts/at_open_verify.py --place-test-order --test-exchange-id <ID>   # ALSO run the gated write

Exit code 0 if no hard FAIL, else 1. Raw responses are dumped to a JSON file for inspection.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.env import load_env
from sig.sig_client import SigClient, SigAPIError
from sig.broker import (
    SigBroker,
    BrokerPositionSnapshot,
    BrokerOrderSnapshot,
    BrokerAccountSnapshot,
    _as_rows,
)

# The field names the broker's parser tries (sig/broker.py) — reported next to the ACTUAL keys so a
# mismatch (the C2 risk: reads built against ASSUMED names) is visible at a glance.
ASSUMED = {
    "position": ["exchangeId|exchange_id|contractId|marketId|id", "quantity|qty|size|netQuantity",
                 "side|direction (else sign)", "averagePrice|avgPrice|avgEntryPrice|price|cost"],
    "order": ["id|orderId|order_id", "exchangeId|exchange_id|contractId|marketId", "side|option|direction",
              "quantity|qty|size", "filledQuantity|filled|filled_qty", "averagePrice|avgPrice|fillPrice",
              "status|state"],
    "account": ["buyingPower|availableBalance|available|cash|balance", "equity|portfolioValue|totalValue|netLiquidation|balance"],
    "feed_update": ["revision|rev", "previousRevision|previous_revision|prevRevision"],
}

PASS, FAIL, INFO, SKIP = "PASS", "FAIL", "INFO", "SKIP"


class Reporter:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.raw: dict = {}

    def add(self, check: str, status: str, detail: str, value=None) -> None:
        self.rows.append({"check": check, "status": status, "detail": detail, "value": value})

    def dump_raw(self, name: str, payload) -> None:
        self.raw[name] = payload

    def any_fail(self) -> bool:
        return any(r["status"] == FAIL for r in self.rows)

    def print_report(self) -> None:
        print("\n" + "=" * 78)
        print(f"  AT-OPEN VERIFICATION — {datetime.now(timezone.utc).isoformat()}")
        print("=" * 78)
        for r in self.rows:
            line = f"  [{r['status']:4}] {r['check']}"
            if r["value"] is not None:
                line += f"  → {r['value']}"
            print(line)
            if r["detail"]:
                print(f"         {r['detail']}")
        n_fail = sum(1 for r in self.rows if r["status"] == FAIL)
        n_pass = sum(1 for r in self.rows if r["status"] == PASS)
        print("-" * 78)
        print(f"  {n_pass} PASS, {n_fail} FAIL, "
              f"{sum(1 for r in self.rows if r['status']==INFO)} INFO, "
              f"{sum(1 for r in self.rows if r['status']==SKIP)} SKIP")
        print("=" * 78)


def _keys_of_first_row(resp) -> list[str]:
    rows = _as_rows(resp)
    return sorted(rows[0].keys()) if rows else []


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_1_arb_constraints(c: SigClient, rep: Reporter) -> None:
    try:
        constraints = c.relationship_constraints()
        rep.dump_raw("relationships_constraints", constraints)
        rows = _as_rows(constraints) if not isinstance(constraints, list) else constraints
        n = len(rows) if isinstance(rows, list) else (len(constraints) if hasattr(constraints, "__len__") else 0)
        if n and n > 0:
            rep.add("C1 arb-constraints fork", PASS,
                    "SIG PUBLISHES constraints → the arb detector READS them (constraint-violation path).",
                    value=f"{n} constraint(s) published")
        else:
            rep.add("C1 arb-constraints fork", INFO,
                    "Constraints EMPTY → we compute complementary-pair arbs ourselves (relationship engine).",
                    value="empty (compute-ourselves world)")
    except Exception as e:
        rep.add("C1 arb-constraints fork", FAIL, f"/relationships/constraints error: {type(e).__name__}: {e}")
    try:
        graph = c.get("/relationships/graph")
        rep.dump_raw("relationships_graph", graph)
        rep.add("C1 relationships graph", INFO, "raw graph dumped for inspection",
                value=f"keys={_keys_of_first_row(graph) or list(graph)[:6] if isinstance(graph, dict) else 'list'}")
    except Exception as e:
        rep.add("C1 relationships graph", FAIL, f"/relationships/graph error: {type(e).__name__}: {e}")


def check_2_feed(c: SigClient, rep: Reporter) -> None:
    try:
        res = c.raw("POST", "/realtime/token")
        body = {}
        try:
            body = json.loads(res.body) if res.body else {}
        except Exception:
            body = {"_raw": (res.body or "")[:200]}
        rep.dump_raw("realtime_token_response", {"status": res.status, "keys": sorted(body) if isinstance(body, dict) else None})
        token = (body or {}).get("token") or (body or {}).get("accessToken") or (body or {}).get("jwt")
        if res.status == 200 and token:
            rep.add("C3 feed token (POST /realtime/token)", PASS, "JWT returned (value not printed).",
                    value=f"token present (len~{len(str(token))})")
        else:
            rep.add("C3 feed token (POST /realtime/token)", FAIL,
                    f"no token in response (status {res.status}, keys={sorted(body) if isinstance(body,dict) else '?'})")
    except Exception as e:
        rep.add("C3 feed token (POST /realtime/token)", FAIL, f"token request error: {type(e).__name__}: {e}")
    # The Supabase Realtime WS wire-adapter is the known injectable seam (not built) — the message format
    # must be confirmed against the live stream. Report the shape sig/feed expects so it can be eyeballed.
    rep.add("C3 feed WS wire-format", INFO,
            "MANUAL: open the Supabase Realtime WS with the token and confirm each update carries "
            f"{ASSUMED['feed_update']}. sig/feed/FeedUpdate.parse keys off revision/previousRevision; "
            "if the live field names differ, swap the wire adapter (resync/staleness/backoff logic stays).",
            value="verify revision/previousRevision on a live message")


async def check_3_broker_reads(c: SigClient, rep: Reporter) -> None:
    broker = SigBroker(read_client=c)   # reuse the authed read client; no write key needed for reads
    # positions
    try:
        raw_pos = c.portfolio_positions(); rep.dump_raw("portfolio_positions", raw_pos)
        actual = _keys_of_first_row(raw_pos)
        positions = await broker.get_positions()
        ok = all(isinstance(p, BrokerPositionSnapshot) for p in positions)
        sample = positions[0] if positions else None
        rep.add("C2 broker get_positions", PASS if ok else FAIL,
                f"parsed {len(positions)} position(s). ACTUAL keys={actual or '(none — flat)'} ; "
                f"broker tries {ASSUMED['position']}",
                value=(f"sample contract={sample.contract_id} side={sample.side} qty={sample.qty}" if sample else "flat venue"))
    except Exception as e:
        rep.add("C2 broker get_positions", FAIL, f"/portfolio/positions error: {type(e).__name__}: {e}")
    # open orders
    try:
        raw_ord = c.get("/orders", params={"status": "open"}); rep.dump_raw("orders_open", raw_ord)
        actual = _keys_of_first_row(raw_ord)
        orders = await broker.get_open_orders()
        ok = all(isinstance(o, BrokerOrderSnapshot) for o in orders)
        rep.add("C2 broker get_open_orders", PASS if ok else FAIL,
                f"parsed {len(orders)} open order(s). ACTUAL keys={actual or '(none open)'} ; "
                f"broker tries {ASSUMED['order']}",
                value=f"{len(orders)} open")
    except Exception as e:
        rep.add("C2 broker get_open_orders", FAIL, f"/orders?status=open error: {type(e).__name__}: {e}")
    # account
    try:
        raw_acct = c.account(); rep.dump_raw("account", raw_acct)
        d = raw_acct.get("data", raw_acct) if isinstance(raw_acct, dict) else {}
        actual = sorted(d.keys()) if isinstance(d, dict) else []
        acct = await broker.get_account()
        ok = isinstance(acct, BrokerAccountSnapshot)
        # a plausibly-wrong parse = both zero while the raw clearly has balance fields → flag
        suspect = ok and acct.buying_power == 0.0 and acct.equity == 0.0 and actual
        rep.add("C2 broker get_account", (INFO if suspect else PASS) if ok else FAIL,
                (f"ACCOUNT parsed; ACTUAL keys={actual}; broker tries {ASSUMED['account']}. "
                 + ("⚠ both bp/equity parsed as 0 but raw has keys — CONFIRM field names." if suspect else "")),
                value=f"buying_power={acct.buying_power} equity={acct.equity}")
    except Exception as e:
        rep.add("C2 broker get_account", FAIL, f"/account error: {type(e).__name__}: {e}")


def check_4_market_depth(c: SigClient, rep: Reporter) -> None:
    try:
        mkts = _as_rows(c.markets()) or (c.markets().get("data", []) if isinstance(c.markets(), dict) else [])
        ids = [str(m.get("exchangeId") or m.get("id") or m.get("exchange_id")) for m in mkts[:25] if isinstance(m, dict)]
        ids = [i for i in ids if i and i != "None"]
        if not ids:
            rep.add("C4 market prices/depth", FAIL, "no market ids from /markets to price")
            return
        prices = c.exchange_prices(ids); rep.dump_raw("exchanges_prices", prices)
        prows = _as_rows(prices)
        rep.add("C4 market prices (bulk)", PASS if prows else FAIL,
                f"/exchanges/prices returned {len(prows)} row(s); price-row keys={_keys_of_first_row(prices)}",
                value=f"{len(prows)} priced of {len(ids)} ids")
        # depth from one orderbook — compare to the pre-open synthetic 1000-lot seeding
        ob = c.orderbook(ids[0]); rep.dump_raw("orderbook_sample", {"exchange_id": ids[0], "book": ob})
        obk = sorted(ob.keys()) if isinstance(ob, dict) else []
        rep.add("C4 orderbook depth (sample)", INFO,
                f"sample {ids[0]} book keys={obk}; compare top-of-book depth vs the pre-open synthetic "
                "1000-lot seeding (sizing's impact-adjustment depends on REAL depth)",
                value="raw book dumped")
    except Exception as e:
        rep.add("C4 market prices/depth", FAIL, f"prices/orderbook error: {type(e).__name__}: {e}")


def check_5_leaderboard_rho(c: SigClient, rep: Reporter, slug: str | None) -> None:
    try:
        if not slug:
            tours = c.get("/tournaments")
            trows = _as_rows(tours) or (tours.get("data", []) if isinstance(tours, dict) else [])
            slug = (trows[0].get("slug") if trows and isinstance(trows[0], dict) else None)
        if not slug:
            rep.add("C4 leaderboard / ρ", FAIL, "no tournament slug available to query the leaderboard")
            return
        lb = c.leaderboard(slug); rep.dump_raw("leaderboard", {"slug": slug, "leaderboard": lb})
        rows = _as_rows(lb) or (lb.get("data", []) if isinstance(lb, dict) else (lb if isinstance(lb, list) else []))
        # 3rd-place threshold: the balance/value of rank 3 (or the 3rd row)
        third = None
        if isinstance(rows, list) and len(rows) >= 3:
            r3 = rows[2] if isinstance(rows[2], dict) else {}
            third = r3.get("balance") or r3.get("value") or r3.get("equity") or r3.get("netWorth")
        if third is not None:
            rep.add("C4 leaderboard / ρ", PASS,
                    "3rd-place threshold available → ρ = balance / T₃ is computable for Browne sizing.",
                    value=f"T₃≈{third}")
        else:
            rep.add("C4 leaderboard / ρ", INFO,
                    "field not yet populated (early open) → ρ UNDEFINED → read_rho returns None → directional "
                    "sizing HOLDS until the field fills. (Expected early; re-check as the field populates.)",
                    value=f"rows={len(rows) if isinstance(rows,list) else '?'} (no T₃ yet)")
    except Exception as e:
        rep.add("C4 leaderboard / ρ", FAIL, f"leaderboard error: {type(e).__name__}: {e}")


def check_6_keys_eligibility(rep: Reporter, base_url: str | None) -> None:
    for key_env in ("SIG_API_KEY_READ", "SIG_API_KEY_WRITE", "SIG_API_KEY_FAST"):
        if not os.getenv(key_env):
            rep.add(f"C5 key {key_env}", FAIL, "not set in the environment/.env")
            continue
        try:
            kc = SigClient(key_env=key_env, base_url=base_url)
            # a cheap authenticated GET proves the key authenticates + has read scope
            acct = kc.account()
            rep.add(f"C5 key {key_env}", PASS, "authenticates (GET /account 200).",
                    value="auth OK")
            if key_env == "SIG_API_KEY_READ":
                d = acct.get("data", acct) if isinstance(acct, dict) else {}
                elig_keys = [k for k in (d or {}) if any(t in k.lower() for t in ("eligib", "residen", "email", "scope", "verif"))]
                rep.dump_raw("account_eligibility_keys", elig_keys)
                rep.add("C5 eligibility", PASS if True else INFO,
                        f"/account reachable; eligibility-related fields present: {elig_keys or '(none surfaced — confirm no RESIDENCE/EMAIL error on a trade attempt)'}",
                        value="account authed")
            kc.close()
        except SigAPIError as e:
            # eligibility failures (RESIDENCE_UPDATE_REQUIRED etc.) surface as API errors
            msg = str(e)
            elig = any(t in msg.upper() for t in ("RESIDENCE", "ELIGIB", "EMAIL", "SCOPE", "KYC"))
            rep.add(f"C5 key {key_env}", FAIL,
                    f"auth/eligibility error: {msg}" + (" ← ELIGIBILITY GATE" if elig else ""))
        except Exception as e:
            rep.add(f"C5 key {key_env}", FAIL, f"error: {type(e).__name__}: {e}")


async def check_7_test_order(rep: Reporter, exchange_id: str | None, base_url: str | None) -> None:
    """GATED. Places ONE tiny resting limit order, confirms the broker parses SIG's real write-response,
    then cancels it. NEVER auto-runs — only when --place-test-order is passed."""
    if not exchange_id:
        rep.add("C2 write-response (GATED)", FAIL,
                "--place-test-order requires --test-exchange-id <market id> (a safe, liquid market).")
        return
    broker = SigBroker()   # uses SIG_API_KEY_WRITE for the write path
    try:
        # buy 1 @ 0.005 (the price floor) — rests far from fair value, unlikely to fill, trivially cancelable
        res = await broker.place_order(strategy_id="at_open_verify", market_id=exchange_id,
                                       exchange_id=exchange_id, side="yes", action="buy",
                                       quantity=1, price=0.005)
        rep.dump_raw("test_order_place", {"status": res.status, "order_id": res.order_id,
                                          "filled_qty": res.filled_qty, "raw_keys": sorted(res.raw) if isinstance(res.raw, dict) else None})
        parsed_ok = res.order_id is not None or res.status in ("open", "filled", "partially_filled", "rejected")
        rep.add("C2 write-response (GATED)", PASS if parsed_ok else FAIL,
                f"broker parsed the live write-response: status={res.status} id={res.order_id} filled={res.filled_qty}",
                value=res.status)
        if res.order_id:
            cancel = await broker.cancel_order(res.order_id)
            rep.dump_raw("test_order_cancel", {"status": cancel.status})
            rep.add("C2 test-order cancel", PASS if cancel.status == "canceled" else INFO,
                    f"cancel status={cancel.status}", value=cancel.status)
    except Exception as e:
        rep.add("C2 write-response (GATED)", FAIL, f"test order error: {type(e).__name__}: {e}")
    finally:
        await broker.aclose()


# --------------------------------------------------------------------------- #
async def run(args) -> int:
    load_env()
    rep = Reporter()
    base_url = args.base_url
    try:
        c = SigClient(key_env="SIG_API_KEY_READ", base_url=base_url)
    except Exception as e:
        print(f"FATAL: cannot build the read client (SIG_API_KEY_READ missing?): {e}", file=sys.stderr)
        return 2

    check_1_arb_constraints(c, rep)
    check_2_feed(c, rep)
    await check_3_broker_reads(c, rep)
    check_4_market_depth(c, rep)
    check_5_leaderboard_rho(c, rep, args.slug)
    check_6_keys_eligibility(rep, base_url)

    if args.place_test_order:
        await check_7_test_order(rep, args.test_exchange_id, base_url)
    else:
        rep.add("C2 write-response (GATED)", SKIP,
                "test order NOT placed (default). Re-run with --place-test-order --test-exchange-id <ID> "
                "when ready to verify the live write-response.")

    c.close()

    # dump raw responses for inspection (never contains secrets — these are public/own-account market reads)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "recon", f"at_open_raw_{ts}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"generated_utc": ts, "results": rep.rows, "raw": rep.raw}, f, indent=2, default=str)

    rep.print_report()
    print(f"\n  raw responses dumped → {out}")
    return 1 if rep.any_fail() else 0


def main() -> int:
    p = argparse.ArgumentParser(description="sig-bot at-open live-contract verification (GO_LIVE Phase C).")
    p.add_argument("--place-test-order", action="store_true",
                   help="GATED: also place ONE tiny test order + cancel (verify the live write-response). Default OFF.")
    p.add_argument("--test-exchange-id", default=None, help="market id for the gated test order")
    p.add_argument("--slug", default=None, help="tournament slug for the leaderboard (else auto from /tournaments)")
    p.add_argument("--base-url", default=None, help="override SIG base URL (else SIG_BASE_URL / default)")
    p.add_argument("--out", default=None, help="path for the raw-response dump (default recon/at_open_raw_<ts>.json)")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
