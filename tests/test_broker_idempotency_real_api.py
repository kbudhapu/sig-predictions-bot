"""The idempotency fix, tested against SIG's ACTUAL documented behaviour.

WHY THIS FILE EXISTS SEPARATELY FROM tests/test_broker.py
    The existing broker tests pass a `FakeTransport` that serves `GET /orders?idempotencyKey=K`
    out of a dict keyed by K. The real API has NO such query parameter (its documented filters are
    status, exchangeId, marketId, limit, cursor, tournamentId) and silently ignores unknown ones,
    returning the whole order list. The test double was therefore MORE CAPABLE than the venue, and
    that single discrepancy hid a bug that stopped the bot trading for three hours while it
    reported success: the pre-flight check read rows[0], so once any order existed every later
    order looked like a duplicate and was never submitted.

    So the transport here models the venue as the spec actually describes it:
      * GET /orders IGNORES idempotencyKey and returns every order
      * orders carry `open: bool` — NO `status` field, NO `idempotencyKey` field
      * POST /orders dedupes SERVER-SIDE on the posted key, replaying the stored response
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SIG_API_KEY_WRITE", "sk_TEST_not_a_real_key")

import pytest

from sig.broker import (
    SigBroker,
    avg_price_from_order,
    filled_qty_from_order,
    status_from_order,
)
from sig.write_gate import HttpResult


class RealisticSigTransport:
    """SIG as documented. See the module docstring for the three behaviours that matter."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.orders: list[dict] = []          # the account's order list, oldest first
        self.by_key: dict[str, dict] = {}     # SERVER-side idempotency store
        self._next_id = 948183
        self.post_fail_once: HttpResult | None = None

    # -- helpers for assertions --
    def posts(self, path="/orders"):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path]

    def gets(self, path="/orders"):
        return [c for c in self.calls if c[0] == "GET" and c[1] == path]

    async def __call__(self, method, path, body, params):
        self.calls.append((method, path, body, params))

        if method == "GET" and path == "/orders":
            # *** THE CRITICAL FIDELITY POINT: idempotencyKey is NOT a supported filter. ***
            rows = list(self.orders)
            p = params or {}
            if p.get("exchangeId") is not None:          # this filter IS supported
                rows = [r for r in rows if str(r["exchangeId"]) == str(p["exchangeId"])]
            return HttpResult(200, json.dumps({"data": rows}))

        if method == "POST" and path in ("/orders", "/orders/multi-leg"):
            if self.post_fail_once is not None:
                res, self.post_fail_once = self.post_fail_once, None
                return res
            idem = (body or {}).get("idempotencyKey")
            if idem and idem in self.by_key:
                # server-side replay of the stored response — no second order created
                return HttpResult(200, json.dumps(self.by_key[idem]))
            self._next_id += 1
            oid = self._next_id
            created = {
                "orderId": oid, "exchangeId": str((body or {}).get("exchangeId", "MULTI")),
                "open": True, "remainingQuantity": float((body or {}).get("quantity") or 0),
                "quantityTraded": 0.0, "fillPrice": None, "terminalReasonCode": None,
                "action": (body or {}).get("action"), "side": (body or {}).get("side"),
                "quantity": (body or {}).get("quantity"), "totalCost": 0.0,
            }
            # the account's order LIST uses the Order schema: `id`, `open`, no status/idempotencyKey
            self.orders.append({
                "id": oid, "exchangeId": created["exchangeId"],
                "side": (body or {}).get("side", "yes"), "action": (body or {}).get("action", "buy"),
                "quantity": (body or {}).get("quantity", 0),
                "priceLimit": (body or {}).get("price"),
                "open": True, "createdAt": "2026-10-01T19:21:57.678Z", "expirationDate": None,
            })
            if idem:
                self.by_key[idem] = created
            return HttpResult(200, json.dumps(created))

        return HttpResult(200, json.dumps({"data": []}))


def broker(transport) -> SigBroker:
    return SigBroker(transport=transport, tournament_id="t-1")


def place(b, **kw):
    base = dict(strategy_id="arb", market_id="m1", exchange_id="1001", side="buy",
                action="open", quantity=100, price=0.46, outcome="yes")
    base.update(kw)
    return asyncio.run(b.place_order(**base))


# --------------------------------------------------------------------------- #
# (3) TWO DIFFERENT ORDERS BOTH PLACE — the regression this whole fix is about
# --------------------------------------------------------------------------- #
def test_two_different_orders_BOTH_place():
    """THE bug: order #2 was silently swallowed as a fake duplicate of order #1.

    Different exchanges, as the live logs showed (1076, 1074, 1073, 1072 all no-op'd against
    existing_id=948183).
    """
    t = RealisticSigTransport()
    b = broker(t)
    r1 = place(b, exchange_id="1076", price=0.935)
    r2 = place(b, exchange_id="1074", price=0.015)

    assert len(t.posts()) == 2, "both orders must be POSTed"
    assert r1.order_id is not None and r2.order_id is not None
    assert r1.order_id != r2.order_id, "two distinct venue orders"
    assert r1.was_noop is False and r2.was_noop is False
    assert len(t.orders) == 2, "two orders must exist at the venue"


def test_many_different_orders_all_place():
    """The live failure was a whole cycle's worth of orders collapsing to one. 10 distinct orders
    must produce 10 distinct venue orders."""
    t = RealisticSigTransport()
    b = broker(t)
    ids = {place(b, exchange_id=str(1000 + i), price=0.40 + i / 100).order_id for i in range(10)}
    assert len(ids) == 10 and None not in ids
    assert len(t.posts()) == 10 and len(t.orders) == 10


def test_a_preexisting_account_order_does_not_block_new_orders():
    """The exact live condition: one order already resting (948183) must not make the next a no-op."""
    t = RealisticSigTransport()
    t.orders.append({"id": 948183, "exchangeId": "1072", "side": "yes", "action": "buy",
                     "quantity": 100, "priceLimit": 0.025, "open": True,
                     "createdAt": "2026-10-01T19:21:57.678Z", "expirationDate": None})
    b = broker(t)
    r = place(b, exchange_id="1076")
    assert r.was_noop is False, "a pre-existing unrelated order must not dedupe this one"
    assert r.order_id is not None and str(r.order_id) != "948183"
    assert len(t.posts()) == 1


def test_no_preflight_GET_before_submitting():
    """Structural guard: the broken check was a GET /orders before the POST. There must be none —
    if one is ever reintroduced, this fails regardless of what it queries."""
    t = RealisticSigTransport()
    place(broker(t), exchange_id="1076")
    assert t.gets("/orders") == [], f"unexpected pre-flight GET: {t.gets('/orders')}"
    assert len(t.posts()) == 1


# --------------------------------------------------------------------------- #
# (4) GENUINE DOUBLE-SUBMIT IS STILL DEDUPED — idempotency preserved
# --------------------------------------------------------------------------- #
def test_identical_resubmit_does_not_double_place():
    """Re-placing the SAME logical order must not create a second venue order.

    Protection now comes from the server replaying the stored response for a reused key (spec:
    "An identical resolved payload replays the exact stored response"), which is where it always
    should have come from — the deterministic key makes the retry identical.
    """
    t = RealisticSigTransport()
    b = broker(t)
    r1 = place(b, exchange_id="1076", price=0.50, quantity=100)
    r2 = place(b, exchange_id="1076", price=0.50, quantity=100)   # identical intent

    assert r1.idempotency_key == r2.idempotency_key, "same intent must produce the same key"
    assert r1.order_id == r2.order_id, "the venue replays the original order"
    assert len(t.orders) == 1, "*** exactly ONE order at the venue — no double-place ***"


def test_changing_any_field_makes_a_different_order():
    """A different price/qty/exchange is a DIFFERENT order and must place separately — this is the
    other half of correct idempotency, and the half the old code destroyed."""
    t = RealisticSigTransport()
    b = broker(t)
    base = place(b, exchange_id="1076", price=0.50, quantity=100)
    for kw in ({"price": 0.51}, {"quantity": 101}, {"exchange_id": "1077"}):
        args = {"exchange_id": "1076", "price": 0.50, "quantity": 100}
        args.update(kw)
        r = place(b, **args)
        assert r.idempotency_key != base.idempotency_key, f"{kw} must change the key"
        assert r.order_id != base.order_id, f"{kw} must be its own order"
    assert len(t.orders) == 4


def test_ambiguous_502_is_resolved_by_replaying_the_key_not_by_rows0():
    """Spec: a 502 "may already have gone through ... retry the identical request with the same
    idempotency key; that retry is safe and will not double-place." So one replay POST, and still
    exactly one venue order."""
    t = RealisticSigTransport()
    t.post_fail_once = HttpResult(502, json.dumps({"error": {"code": "ORDER_STATUS_UNKNOWN"}}))
    b = broker(t)
    r = place(b, exchange_id="1076")
    assert r.order_id is not None, "the ambiguity must be resolved, not surfaced as unknown"
    assert r.reconciled is True
    assert len(t.orders) == 1, "no double-place across the replay"


# --------------------------------------------------------------------------- #
# (5) ORDER STATUS MAPPED FROM `open` — SIG sends no `status`
# --------------------------------------------------------------------------- #
def test_status_derives_from_open_not_from_a_missing_status_field():
    """Reading `status` always missed, so norm_status(None) defaulted to "open" and every order
    looked resting forever — no fill was ever recognised."""
    assert status_from_order({"open": True, "quantityTraded": 0.0}) == "open"
    assert status_from_order({"open": True, "quantityTraded": 40.0}) == "partially_filled"
    assert status_from_order({"open": False, "quantityTraded": 100.0, "quantity": 100}) == "filled"
    assert status_from_order({"open": False, "quantityTraded": 0.0,
                              "terminalReasonCode": "Expired"}) == "expired"
    assert status_from_order({"open": False, "quantityTraded": 0.0,
                              "terminalReasonCode": "SelfTradePrevented"}) == "canceled"
    assert status_from_order({"open": False, "quantityTraded": 0.0}) == "canceled"


def test_live_order_shape_does_not_read_as_filled():
    """The exact object the live API returned. It must read as a resting order, not a terminal one."""
    live = {"id": 948183, "exchangeId": "1072", "side": "yes", "action": "buy", "quantity": 100,
            "priceLimit": 0.025, "open": True, "createdAt": "2026-10-01T19:21:57.678Z",
            "expirationDate": None}
    assert status_from_order(live) == "open"
    assert filled_qty_from_order(live) == 0.0, "no fill field exists; `quantity` is NOT a fill"


def test_an_explicit_status_is_still_honoured():
    """A reporting-backed payload that does carry a status must not be second-guessed."""
    assert status_from_order({"status": "filled", "open": True}) == "filled"


def test_fill_quantity_and_price_come_from_the_real_fields():
    d = {"open": False, "quantityTraded": 73.0, "fillPrice": 0.47, "quantity": 100}
    assert filled_qty_from_order(d) == 73.0
    assert avg_price_from_order(d) == 0.47
    # `filledQuantity` / `averagePrice` (what the old code read) do not exist on SIG payloads
    assert filled_qty_from_order({"open": True}) == 0.0
    assert avg_price_from_order({"open": True}) == 0.0


def test_place_result_maps_orderId_and_open():
    t = RealisticSigTransport()
    r = place(broker(t), exchange_id="1076")
    assert r.order_id is not None
    assert r.status == "open", "a freshly placed resting order"
    assert r.filled_qty == 0.0


# --------------------------------------------------------------------------- #
# (6) PHANTOM BUNDLES — a no-op with no order id must not be tracked
# --------------------------------------------------------------------------- #
def test_oms_refuses_to_track_a_bundle_with_no_real_legs():
    """The bug produced 34 `bundle_submitted` in 10 minutes against ONE real venue order, each
    waiting for a fill that could never arrive."""
    from sig.oms.manager import OrderManagementSystem
    from sig.oms.models import FillPolicy

    class PhantomBroker:
        async def place_order(self, **kw):
            from sig.broker import OrderResult
            return OrderResult(idempotency_key="k", status="open", order_id=None, was_noop=True)
        async def place_multi_leg(self, **kw):
            from sig.broker import OrderResult
            return OrderResult(idempotency_key="k", status="open", order_id=None, was_noop=True)
        async def get_order(self, oid):
            from sig.broker import OrderResult
            return OrderResult(idempotency_key="", status="open", order_id=oid)
        async def cancel_order(self, oid):
            from sig.broker import OrderResult
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)
        async def get_positions(self): return []
        async def get_open_orders(self): return []

    class NullState:
        async def write_order(self, order): pass
        async def upsert_position(self, p): pass
        async def append_audit(self, e): pass
        async def update_order_status(self, *a, **k): pass
        async def read_exposure(self): return {}

    async def main():
        oms = OrderManagementSystem(broker=PhantomBroker(), state=NullState(),
                                    strategy_contracts={"s": "1001"})
        await oms.submit(strategy_id="s",
                         legs=[{"contract_id": "1001", "market_id": "m", "side": "buy",
                                "qty": 100, "price": 0.5, "leg_id": "l0"}],
                         fill_policy=FillPolicy.for_arb(), metadata={})
        return oms

    oms = asyncio.run(main())
    assert asyncio.run(oms.pending_bundle_count()) == 0, "a phantom bundle must NOT be tracked"


def test_a_real_order_is_still_tracked():
    """The guard must not throw away genuine bundles."""
    from sig.oms.manager import OrderManagementSystem
    from sig.oms.models import FillPolicy

    class RealBroker:
        async def place_order(self, **kw):
            from sig.broker import OrderResult
            return OrderResult(idempotency_key="k", status="open", order_id="srv-1")
        async def get_order(self, oid):
            from sig.broker import OrderResult
            return OrderResult(idempotency_key="", status="open", order_id=oid)
        async def get_positions(self): return []
        async def get_open_orders(self): return []

    class NullState:
        async def write_order(self, order): pass
        async def upsert_position(self, p): pass
        async def append_audit(self, e): pass
        async def update_order_status(self, *a, **k): pass
        async def read_exposure(self): return {}

    async def main():
        oms = OrderManagementSystem(broker=RealBroker(), state=NullState(),
                                    strategy_contracts={"s": "1001"})
        await oms.submit(strategy_id="s",
                         legs=[{"contract_id": "1001", "market_id": "m", "side": "buy",
                                "qty": 100, "price": 0.5, "leg_id": "l0"}],
                         fill_policy=FillPolicy.for_arb(), metadata={})
        return oms

    oms = asyncio.run(main())
    assert asyncio.run(oms.pending_bundle_count()) == 1


# --------------------------------------------------------------------------- #
# The local dedup ledger — it was silently empty
# --------------------------------------------------------------------------- #
def test_write_order_actually_reaches_the_local_orders_table(tmp_path):
    """StateAdapter forwarded a dict into a keyword-only signature; the TypeError was swallowed, so
    the local `orders` table (idempotency_key PRIMARY KEY) stayed permanently EMPTY."""
    from infra.state.store import StateStore
    from sig.engine.composition import StateAdapter

    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        ad = StateAdapter(store=store)
        await ad.write_order({
            "order_id": "srv-7", "bundle_id": "b1", "strategy_id": "arb",
            "contract_id": "1076", "side": "buy", "qty": 100, "status": "open",
            "idempotency_key": "sig:arb:m1:yes:abc123", "price": 0.46, "outcome": "yes",
        })
        rows = await store._read("SELECT * FROM orders")
        await store.close()
        return [dict(r) for r in rows]

    rows = asyncio.run(main())
    assert len(rows) == 1, "the local ledger must actually be written"
    r = rows[0]
    assert r["idempotency_key"] == "sig:arb:m1:yes:abc123"
    assert r["exchange_id"] == "1076"
    assert r["action"] == "buy" and r["side"] == "yes", "action=direction, side=outcome"
    assert r["quantity"] == 100
    assert r["broker_order_id"] == "srv-7"


def test_local_ledger_is_idempotent_on_the_key(tmp_path):
    from infra.state.store import StateStore
    from sig.engine.composition import StateAdapter

    async def main():
        store = await StateStore(tmp_path / "s.db").open()
        ad = StateAdapter(store=store)
        for _ in range(3):
            await ad.write_order({"order_id": "srv-7", "contract_id": "1076", "side": "buy",
                                  "qty": 100, "status": "open", "idempotency_key": "same-key"})
        n = (await store._read("SELECT COUNT(*) c FROM orders"))[0]["c"]
        await store.close()
        return n

    assert asyncio.run(main()) == 1, "a repeated key is one row, not three"
