"""Tests for sig/broker.py — the SIG async write path. HTTP is MOCKED (injectable transport); NO live
order is placed. Proves: idempotency dedup, 429 -> backoff with the SAME key (+ exp backoff + max cap),
write-gate throttles at 30/min + priority jumps the queue, multi-leg atomic-or-nothing.
"""
import sys, os, json, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SIG_API_KEY_WRITE", "sk_TEST_not_a_real_key")   # key from env; never a live one

from sig.broker import (SigBroker, WriteGate, RetryConfig, OrderResult, BrokerAPI,
                        idempotency_key, intent_hash)
from sig.write_gate import HttpResult
from infra.safeguards import RateLimitBreaker

P = []; F = []
def ck(n, c, d=""): (P if c else F).append(n); print(("PASS " if c else "FAIL ") + n + (" :: " + d if d else ""))


class FakeTransport:
    """Scriptable async SIG transport. Records every call; serves query-before-submit from `by_key`;
    a successful POST /orders records the created order under its idempotencyKey (models server dedup)."""
    def __init__(self):
        self.calls = []                       # (method, path, body, params)
        self.scripts = {}                     # (method, path) -> [HttpResult, ...]
        self.by_key = {}                      # idempotencyKey -> order dict
    def script(self, method, path, results):
        self.scripts[(method, path)] = list(results)
    def posts(self, path="/orders"):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path]
    async def __call__(self, method, path, body, params):
        self.calls.append((method, path, body, params))
        if method == "GET" and path == "/orders":        # query-before-submit lookup
            row = self.by_key.get((params or {}).get("idempotencyKey"))
            return HttpResult(200, json.dumps({"data": [row] if row else []}))
        key = (method, path)
        if self.scripts.get(key):
            res = self.scripts[key].pop(0)
        else:
            res = None
        if res is None:                                   # default: successful create
            idem = (body or {}).get("idempotencyKey")
            created = {"id": "srv1", "status": "open", "idempotencyKey": idem, **(body or {})}
            if idem:
                self.by_key[idem] = created
            return HttpResult(200, json.dumps(created))
        # a scripted 2xx create should also register for dedup
        if 200 <= res.status < 300 and method == "POST" and path == "/orders":
            idem = (body or {}).get("idempotencyKey")
            if idem and idem not in self.by_key:
                try:
                    self.by_key[idem] = json.loads(res.body)
                except Exception:
                    self.by_key[idem] = {"idempotencyKey": idem, "status": "open", "id": "srv1"}
        return res


def mk(transport, **kw):
    # high-capacity gate + instant sleep so retry/gate don't actually wait (except gate-specific tests)
    waits = kw.pop("waits", None)
    async def _sleep(s):
        if waits is not None:
            waits.append(s)
    return SigBroker(transport=transport, gate=WriteGate(writes_per_min=1000, window_s=60),
                     sleep=_sleep, **kw)


def run(coro):
    return asyncio.run(coro)


# ---------------- 1. IDEMPOTENCY ----------------
def test_idempotency():
    # deterministic key: same inputs -> same key; different intent (price) -> different key
    k1 = idempotency_key("arb", "mkt1", "yes", intent_hash(exchange_id="1077", action="buy", quantity=1, price=0.05))
    k2 = idempotency_key("arb", "mkt1", "yes", intent_hash(exchange_id="1077", action="buy", quantity=1, price=0.05))
    k3 = idempotency_key("arb", "mkt1", "yes", intent_hash(exchange_id="1077", action="buy", quantity=1, price=0.06))
    ck("idempotency key is deterministic (same inputs -> same key)", k1 == k2 and k1.startswith("sig-"))
    ck("different intent -> different key", k1 != k3)

    t = FakeTransport(); b = mk(t)
    args = dict(strategy_id="arb", market_id="mkt1", exchange_id="1077", side="yes", action="buy", quantity=1, price=0.05)
    r1 = run(b.place_order(**args))
    ck("first submit places exactly one order (not a no-op)", (not r1.was_noop) and len(t.posts()) == 1 and r1.order_id == "srv1")
    r2 = run(b.place_order(**args))
    ck("query-before-submit dedups: second identical call is a NO-OP, still 1 POST", r2.was_noop and len(t.posts()) == 1)
    ck("both calls share the same deterministic idempotency key", r1.idempotency_key == r2.idempotency_key == k1)


# ---------------- 2. 429 / Retry-After ----------------
def test_429_backoff_same_key():
    t = FakeTransport()
    t.script("POST", "/orders", [HttpResult(429, "RATE", retry_after_s=60.0),
                                 HttpResult(429, "RATE", retry_after_s=60.0),
                                 HttpResult(200, json.dumps({"id": "srv1", "status": "open"}))])
    waits = []
    b = mk(t, waits=waits, breaker=RateLimitBreaker(k_consecutive=99))
    r = run(b.place_order(strategy_id="s", market_id="m", exchange_id="1077", side="yes", action="buy", quantity=1, price=0.05))
    posts = t.posts()
    keys = {c[2]["idempotencyKey"] for c in posts}
    ck("429 then success: retried to success", r.status == "open" and not r.was_noop)
    ck("retries reuse the SAME idempotency key (can't double-place)", len(keys) == 1 and len(posts) == 3)
    ck("backs off exactly Retry-After, exponential on repeat (60, then 120)", waits == [60.0, 120.0], str(waits))

    # max-retry cap: unbounded 429s -> stops at the cap, returns rejected (not infinite, not open)
    t2 = FakeTransport(); t2.script("POST", "/orders", [HttpResult(429, "RATE", retry_after_s=1.0)] * 20)
    w2 = []; b2 = mk(t2, waits=w2, retry=RetryConfig(max_retries=4), breaker=RateLimitBreaker(k_consecutive=99))
    r2 = run(b2.place_order(strategy_id="s", market_id="m", exchange_id="1077", side="yes", action="buy", quantity=1, price=0.05))
    ck("max-retry cap bounds attempts (<= max_retries+1 POSTs)", len(t2.posts()) <= 5, f"posts={len(t2.posts())}")
    ck("429-exhausted -> rejected (never mislabeled open)", r2.status == "rejected", r2.status)

    # breaker: sustained 429 trips the circuit-breaker -> STOP + surface
    t3 = FakeTransport(); t3.script("POST", "/orders", [HttpResult(429, "RATE", retry_after_s=0.0)] * 20)
    b3 = mk(t3, waits=[], breaker=RateLimitBreaker(k_consecutive=3))
    r3 = run(b3.place_order(strategy_id="s", market_id="m", exchange_id="1077", side="yes", action="buy", quantity=1, price=0.05))
    ck("sustained 429 trips breaker -> rejected + breaker.tripped", r3.status == "rejected" and b3.breaker.tripped)


# ---------------- 3. WRITE-GATE ----------------
def test_write_gate_throttle():
    async def _t():
        g = WriteGate(writes_per_min=3, window_s=0.4)
        for _ in range(3):
            await g.acquire()
        mid = time.monotonic()
        await g.acquire()                     # 4th must wait ~a full window
        return time.monotonic() - mid
    waited = run(_t())
    ck("write-gate throttles at capacity (4th write waits ~window)", waited >= 0.3, f"waited={waited:.2f}s")


def test_write_gate_priority():
    async def _t():
        g = WriteGate(writes_per_min=1, window_s=0.4)
        await g.acquire()                     # consume the only slot
        order = []
        async def normal():
            await g.acquire(priority=False); order.append("normal")
        async def urgent():
            await g.acquire(priority=True); order.append("priority")
        nt = asyncio.create_task(normal())
        await asyncio.sleep(0.02)             # ensure normal is queued first
        ut = asyncio.create_task(urgent())
        await asyncio.gather(nt, ut)
        return order
    order = run(_t())
    ck("priority acquirer jumps the queue ahead of an earlier normal one", order and order[0] == "priority", str(order))


# ---------------- 4. MULTI-LEG ATOMIC ----------------
def test_multi_leg_atomic():
    legs = [{"exchangeId": "1077", "side": "yes", "action": "buy", "quantity": 1, "price": 0.05},
            {"exchangeId": "1076", "side": "no", "action": "buy", "quantity": 1, "price": 0.93}]
    # success: server fills all legs atomically
    t = FakeTransport(); t.script("POST", "/orders/multi-leg", [HttpResult(200, json.dumps(
        {"id": "ml1", "status": "filled", "atomic": True, "legs": [{"exchangeId": "1077", "filled": 1}, {"exchangeId": "1076", "filled": 1}]}))])
    r = run(mk(t).place_multi_leg(strategy_id="arb", market_id="m", legs=legs))
    ck("multi-leg success -> filled with both legs", r.status == "filled" and len(r.legs) == 2)

    # atomic abort (server could not fill all): must report rejected, NEVER a partial/naked leg
    t2 = FakeTransport(); t2.script("POST", "/orders/multi-leg", [HttpResult(200, json.dumps({"status": "rejected", "atomic": False}))])
    r2 = run(mk(t2).place_multi_leg(strategy_id="arb", market_id="m", legs=legs))
    ck("multi-leg atomic-abort (atomic:false) -> rejected, no partial", r2.status == "rejected" and r2.raw.get("reason") == "atomic_abort")

    # hard 4xx abort: also rejected, no leg placed
    t3 = FakeTransport(); t3.script("POST", "/orders/multi-leg", [HttpResult(400, '{"error":"cannot fill atomically"}')])
    r3 = run(mk(t3).place_multi_leg(strategy_id="arb", market_id="m", legs=legs))
    ck("multi-leg 4xx -> rejected, atomic-or-nothing", r3.status == "rejected")

    # >10 legs rejected up front
    r4 = run(mk(FakeTransport()).place_multi_leg(strategy_id="arb", market_id="m", legs=legs * 6))
    ck("multi-leg >10 legs rejected before any send", r4.status == "rejected")

    # multi-leg idempotency: identical batch dedups
    t5 = FakeTransport(); b5 = mk(t5)
    run(b5.place_multi_leg(strategy_id="arb", market_id="m", legs=legs))
    r5 = run(b5.place_multi_leg(strategy_id="arb", market_id="m", legs=legs))
    ck("multi-leg identical batch dedups (no second submit)", r5.was_noop and len(t5.posts("/orders/multi-leg")) == 1)


# ---------------- protocol conformance ----------------
def test_protocol():
    ck("SigBroker satisfies the BrokerAPI Protocol", isinstance(SigBroker(transport=lambda *a: None), BrokerAPI))


def main():
    test_idempotency()
    test_429_backoff_same_key()
    test_write_gate_throttle()
    test_write_gate_priority()
    test_multi_leg_atomic()
    test_protocol()
    print(f"\n==== sig/broker.py TESTS: PASS {len(P)}/{len(P)+len(F)} ====")
    if F:
        print("FAILED:", F); sys.exit(1)


if __name__ == "__main__":
    main()


def test_cancel_never_backs_off_in_call_on_429():
    """2026-10-10: a DELETE answered with 429 (order already gone) was retried in-call with 60..600s backoff and held
    the trading cycle for 38 min. Cancels return at once; callers retry unconfirmed cancels on a later cycle."""
    t = FakeTransport()
    t.script("DELETE", "/orders/o1", [HttpResult(429, "RATE", retry_after_s=60.0)] * 5)
    waits = []
    b = mk(t, waits=waits, breaker=RateLimitBreaker(k_consecutive=99))
    r = run(b.cancel_order("o1", strategy_id="s"))
    deletes = [c for c in t.calls if c[0] == "DELETE"]
    ck("429 on cancel: exactly one DELETE, no in-call wait", len(deletes) == 1 and waits == [], f"{len(deletes)} {waits}")
    ck("429 on cancel is reported unconfirmed (not canceled)", r.status != "canceled", r.status)
    assert len(deletes) == 1 and waits == [] and r.status != "canceled"
