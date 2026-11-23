"""Round 4 (2026-10-08): the items left open after round 3. Each test fails on the code before its fix."""
import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SIG_API_KEY_WRITE", "sk_TEST_not_a_real_key")

from sig.broker import OrderResult, SigBroker, WriteGate
from sig.engine import ownership as own_mod
from sig.engine.ownership import MarketOwnership
from sig.write_gate import HttpResult


def run(c):
    return asyncio.run(c)


@pytest.fixture
def reg(tmp_path):
    r = MarketOwnership(str(tmp_path / "own.json"))
    r.beat("fair_band")
    own_mod.set_registry(r)
    yield r
    own_mod.set_registry(None)


class T:
    """Fake SIG transport: scripted responses per (method, path) else a 200 create."""
    def __init__(self, script=None, orders=None):
        self.calls, self.script, self.orders = [], dict(script or {}), orders or []

    async def __call__(self, method, path, body, params):
        self.calls.append((method, path, body, params))
        q = self.script.get((method, path))
        if q:
            return q.pop(0)
        if method == "GET" and path == "/orders":
            return HttpResult(200, json.dumps({"data": self.orders}))
        if method == "DELETE":
            return HttpResult(200, "{}")
        return HttpResult(200, json.dumps({"id": f"v{len(self.calls)}", "open": True, **(body or {})}))


def _b(t):
    async def _sleep(s):
        return None
    return SigBroker(transport=t, gate=WriteGate(writes_per_min=1000, window_s=60), sleep=_sleep)


# ───────────── broker ─────────────
def test_expiration_date_is_sent_for_limit_orders(reg):
    t = T()
    run(_b(t).place_order(strategy_id="x", market_id="1", exchange_id="1", side="buy", action="buy", quantity=5,
                          price=0.5, outcome="yes", expiration_s=900))
    exp = t.calls[0][2]["expirationDate"]
    when = datetime.strptime(exp, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    assert 880 <= (when - datetime.now(timezone.utc)).total_seconds() <= 905


def test_stale_send_is_refused_without_any_http(reg):
    class SlowGate(WriteGate):
        async def acquire(self, priority=False):
            time.sleep(0.05)
            return await super().acquire(priority=priority)
    t = T()

    async def _sleep(s):
        return None
    b = SigBroker(transport=t, gate=SlowGate(writes_per_min=1000, window_s=60), sleep=_sleep)
    r = run(b.place_order(strategy_id="x", market_id="1", exchange_id="1", side="buy", action="buy", quantity=5,
                          price=0.5, outcome="yes", send_deadline_s=0.01))
    assert r.status == "rejected" and "stale" in r.raw["reason"] and t.calls == []


def test_matching_recovery_respects_the_side():
    rows = [{"id": "n1", "exchangeId": "1", "side": "no", "action": "buy", "quantity": 100, "priceLimit": 0.5},
            {"id": "y1", "exchangeId": "1", "side": "yes", "action": "buy", "quantity": 100, "priceLimit": 0.5}]
    b = _b(T(orders=rows))
    f = run(b._find_matching_order(exchange_id="1", action="buy", quantity=100, price=0.5, side="yes"))
    assert f["id"] == "y1"


def test_replay_refused_by_ownership_still_looks_the_order_up(reg):
    """502 on the first send, market claimed before the replay: the replay is refused but a READ must still find
    whether the first send landed (so the caller gets the order, not an untracked 'unknown')."""
    landed = {"id": "L1", "exchangeId": "1", "side": "yes", "action": "buy", "quantity": 5, "priceLimit": 0.5,
              "open": True}

    class Claim502(T):
        async def __call__(self, method, path, body, params):
            if method == "POST" and not any(c[0] == "POST" for c in self.calls):
                self.calls.append((method, path, body, params))
                reg.claim("fair_band", ["1"])                       # claim lands while we are ambiguous
                return HttpResult(502, "{}")
            return await super().__call__(method, path, body, params)
    t = Claim502(orders=[landed])
    r = run(_b(t).place_order(strategy_id="cross_market_divergence", market_id="1", exchange_id="1", side="buy",
                              action="buy", quantity=5, price=0.5, outcome="yes"))
    assert r.order_id == "L1" and sum(1 for c in t.calls if c[0] == "POST") == 1


def test_cancel_all_refused_while_isolated_orders_exist(reg):
    reg.claim("fair_band", ["1"])
    reg.note_order("fair_band", "q1", "1")
    t = T()
    assert run(_b(t).cancel_all()).status == "rejected" and t.calls == []
    assert run(_b(t).cancel_all(force=True)).status == "canceled"


# ───────────── OMS ─────────────
def _bundle(policy, status="open"):
    from sig.oms.models import OrderBundle, OrderSlice, StrategyClass
    sl = OrderSlice(order_id="o1", leg_id="leg_0", contract_id="X", side="buy", requested_qty=100.0,
                    status=status, price=0.5, outcome="yes", exchange_id="X", market_id="X")
    return OrderBundle(bundle_id=str(uuid.uuid4()), strategy_id="cross", strategy_class=StrategyClass.DIRECTIONAL,
                       fill_policy=policy, slices=[sl])


def test_confirmed_cancel_is_final_even_if_a_read_says_partially_filled():
    from sig.oms.fill_handler import _refresh_slice
    from sig.oms.models import FillPolicy
    b = _bundle(FillPolicy.for_directional_accept(maker_rest=True))
    b.slices[0].cancel_confirmed = True

    class Br:
        async def get_order(self, oid):
            return OrderResult(idempotency_key="", status="partially_filled", order_id=oid, filled_qty=30.0)
    run(_refresh_slice(b.slices[0], Br()))
    assert b.slices[0].status == "cancelled" and b.slices[0].filled_qty == 30.0


class _St:
    def __getattr__(self, name):
        async def _noop(*a, **k):
            return None
        return _noop


def _oms(broker, bundles):
    from sig.oms.manager import OrderManagementSystem
    oms = OrderManagementSystem.__new__(OrderManagementSystem)
    oms._lock, oms._broker, oms._state = asyncio.Lock(), broker, _St()
    oms._pending = {b.bundle_id: b for b in bundles}
    return oms


def test_maker_cancel_backoff_and_forced_resolve():
    from sig.oms.models import FillPolicy

    class Refuse:
        n = 0

        async def cancel_order(self, oid, **kw):
            Refuse.n += 1
            return OrderResult(idempotency_key="", status="rejected", order_id=oid)
    b = _bundle(FillPolicy.for_directional_accept(maker_rest=True, unfilled_timeout_seconds=1.0))
    b.created_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    oms = _oms(Refuse(), [b])
    for _ in range(5):
        run(oms.sweep_unfilled_timeouts())
    assert Refuse.n == 1                                        # backoff: not a DELETE every cycle
    b.created_at = datetime.now(timezone.utc) - timedelta(seconds=oms.MAKER_MAX_AGE_S + oms.MAKER_FORCE_GRACE_S + 1)
    run(oms.sweep_unfilled_timeouts())
    assert b.terminal_action_taken                              # escape hatch: never pins the strategy forever


def test_maker_entries_carry_a_venue_expiry():
    from sig.oms.models import FillPolicy
    from sig.oms.manager import OrderManagementSystem
    seen = {}

    class Br:
        async def place_order(self, **kw):
            seen.update(kw)
            return OrderResult(idempotency_key="k", status="open", order_id="m1")
    oms = OrderManagementSystem.__new__(OrderManagementSystem)
    oms._lock, oms._broker, oms._state, oms._pending, oms._policies = asyncio.Lock(), Br(), _St(), {}, {}
    oms._hard_locked = False
    try:
        run(oms.submit(strategy_id="cross", legs=[{"contract_id": "X", "side": "buy", "qty": 10, "price": 0.5,
                                                    "outcome": "yes"}],
                       fill_policy=FillPolicy.for_directional_accept(maker_rest=True)))
    except Exception:
        pass
    assert seen.get("expiration_s", 0) >= oms.MAKER_MAX_AGE_S


def test_chase_uses_a_fresh_key_per_attempt():
    from sig.oms.fill_handler import _handle_directional_chase
    from sig.oms.models import FillPolicy
    keys = []

    class Br:
        async def cancel_order(self, oid, **kw):
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)

        async def place_order(self, **kw):
            keys.append(kw["market_id"])
            return OrderResult(idempotency_key="", status="open", order_id=f"n{len(keys)}")
    b = _bundle(FillPolicy.for_directional_chase(chase_max_attempts=5))
    for _ in range(2):
        b.slices[0].status = "open"
        run(_handle_directional_chase(b, Br(), _St(), b.fill_policy))
    assert len(keys) == 2 and len(set(keys)) == 2


# ───────────── fills ─────────────
def test_fill_retag_and_latest_time(tmp_path, monkeypatch):
    import infra.state.store as st
    monkeypatch.setattr(st, "STATE_DB_PATH", str(tmp_path / "s.db"), raising=False)

    async def main():
        s = await st.StateStore(str(tmp_path / "s.db")).open()
        await s.append_strategy_fills([{"fill_id": "f1", "order_id": "o9", "strategy": "UNATTRIBUTED",
                                        "exchange_id": "1", "outcome": "yes", "price": 0.5, "quantity": 10,
                                        "filled_at": "2026-10-08T01:00:00.000Z"}])
        await s.record_order_attribution([("o9", "cross_market_divergence", "1")])
        await s.retag_unattributed_fills()
        rows = await s._read("SELECT strategy FROM strategy_fills WHERE fill_id='f1'")
        latest = await s.latest_fill_time()
        await s.close()
        return rows[0]["strategy"], latest
    strat, latest = run(main())
    assert strat == "cross_market_divergence" and latest.startswith("2026-10-08T01:00")


# ───────────── ownership ─────────────
def test_registry_tracks_order_markets_history_and_recovers(tmp_path):
    p = tmp_path / "own.json"
    r = MarketOwnership(str(p))
    r.claim("fair_band", ["1"])
    r.note_order("fair_band", "a", "1")
    assert r.orders_in("fair_band", "1") == ["a"]
    r.forget_orders(["a"])
    r.release("fair_band", "1")
    h = r.history_of("fair_band")
    assert len(h) == 1 and h[0][0] == "1" and h[0][3] is not None
    good = p.read_text()
    p.write_text("{bad")
    (tmp_path / "own.json.bak").write_text("{bad")
    r2 = MarketOwnership(str(p))
    assert r2.corrupt and not r2.allows("cross_market_divergence", "7")
    p.write_text(good)
    r2._last_reload = 0.0
    assert r2.allows("cross_market_divergence", "7") and not r2.corrupt      # recovered without restart


# ───────────── pilot ─────────────
def test_orchestrator_halt_resumes_after_it_clears(tmp_path):
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig
    tr = FairBandLive(broker=None, ownership=MarketOwnership(None),
                      cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S"), resume_after_clear_ticks=3))
    tr.check_kill(0.0, 0.0, orchestrator_halted=True)
    assert tr.winding_down
    for _ in range(2):
        tr.check_kill(0.0, 0.0, orchestrator_halted=False)
    assert tr.winding_down
    tr.check_kill(0.0, 0.0, orchestrator_halted=False)
    assert not tr.winding_down
    tr.check_kill(-10_000.0, None)                              # a loss stop stays sticky
    for _ in range(10):
        tr.check_kill(0.0, 0.0)
    assert tr.winding_down


def test_loss_stop_hands_inventory_to_exits_but_halt_does_not(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
    for reason, expect_released in (("loss_limit_fills -900", True), ("orchestrator_halted", False)):
        own_mod.set_registry(reg)
        v = Venue(reg, random.Random(0))
        tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")),
                          clock=Clock())
        claim(tr, ["7"], v)
        run(tr.tick({}, {}, v.open_view(), 1e6))            # probation cleared while flat
        tr.halt(reason)
        run(tr.tick({}, {"7": 500.0}, v.open_view(), 1e6))
        assert (reg.owner_of("7") is None) == expect_released, reason
        if reg.owner_of("7"):
            reg.release(SID, "7")


def test_release_waits_until_no_registered_order_remains(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    reg.note_order(SID, "hidden", "1")                          # ours, but a read missed it
    tr.halt("stop_file")
    run(tr.tick({}, {}, v.open_view(), 1e6))
    assert reg.owner_of("1") == SID                             # not released while a registered order may rest
    for _ in range(3):
        clk.t += 60
        run(tr.tick({}, {}, v.open_view(), 1e6))
    assert reg.owner_of("1") is None                            # pruned after 3 clean reads -> released


def test_probation_survives_a_restart(reg, tmp_path):
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
    reg.claim(SID, ["1"])
    tr = FairBandLive(broker=None, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")))
    assert tr.restore_probation() == ["1"] and "1" in tr._probation


def test_pilot_quotes_carry_expiry_and_send_deadline(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig
    seen = []

    class V(Venue):
        async def place_order(self, **kw):
            seen.append(kw)
            return await super().place_order(**kw)
    v = V(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=Clock())
    claim(tr, ["1"], v)
    run(tr.tick({"1": {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1,
                       "age_s": 1}}, {}, v.open_view(), 1e6))
    assert seen and all(k["expiration_s"] == 900.0 and k["send_deadline_s"] == 20.0 for k in seen)


def test_loss_scope_includes_released_markets(tmp_path):
    import main_sig
    p = str(tmp_path / "s.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE strategy_fills (exchange_id TEXT, outcome TEXT, price REAL, quantity REAL, "
              "signed_qty REAL, filled_at TEXT)")
    c.execute("INSERT INTO strategy_fills VALUES ('9','yes',0.60,1000,1000,'2026-10-08T02:00:00.000Z')")  # in window
    c.execute("INSERT INTO strategy_fills VALUES ('9','yes',0.10,1000,1000,'2026-10-08T05:00:00.000Z')")  # after release
    c.commit(); c.close()
    hist = [["9", "fair_band", "2026-10-08T01:00:00+00:00", "2026-10-08T03:00:00+00:00"]]
    pnl = main_sig._fair_band_live_pnl(p, {"9"}, "2026-10-08T00:00", {"9": {"best_bid": 0.50, "best_ask": 0.51}},
                                       None, hist)
    assert abs(pnl - 1000 * (0.50 - 0.60)) < 1e-6                # released market's loss counted, later fill not


# ───────────── funding ─────────────
def test_funding_ambiguous_placement_is_looked_up_and_cancelled():
    from sig.engine.funding import execute_plan
    cancels = []

    class Br:
        async def place_order(self, **kw):
            return OrderResult(idempotency_key="", status="unknown", order_id=None)

        async def _find_matching_order(self, **kw):
            return {"id": "ghost", "open": True}

        async def cancel_order(self, oid, **kw):
            cancels.append(oid)
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)

    async def rp(ex):
        return 1000.0

    async def rb(ex):
        return {"bids": [(0.9, 10_000)], "asks": [(0.905, 10_000)]}

    async def rf(ex):
        return 0.92

    async def ns(s):
        return None
    rep = run(execute_plan({"orders": [{"ex": "A", "outcome": "yes", "qty": 100, "limit": 0.9}]}, broker=Br(),
                           read_book=rb, read_position=rp, read_fair=rf, need_usd=50, sleep=ns))
    assert cancels == ["ghost"] and rep["stopped"].endswith("cancelled")


def test_funding_paces_its_writes():
    from sig.engine.funding import execute_plan
    sleeps = []

    class Br:
        n = 0

        async def place_order(self, **kw):
            Br.n += 1
            return OrderResult(idempotency_key="", status="filled", order_id=f"o{Br.n}")

    pos = {"A": 1000.0, "B": 1000.0}

    async def rp(ex):
        return pos[ex]

    async def rb(ex):
        return {"bids": [(0.9, 10_000)], "asks": [(0.905, 10_000)]}

    async def rf(ex):
        return 0.92

    async def rec(s):
        sleeps.append(s)
    run(execute_plan({"orders": [{"ex": "A", "outcome": "yes", "qty": 100, "limit": 0.9},
                                 {"ex": "B", "outcome": "yes", "qty": 100, "limit": 0.9}]},
                     broker=Br(), read_book=rb, read_position=rp, read_fair=rf, need_usd=10_000, sleep=rec))
    assert 10.0 in sleeps                                       # >= 10s between successive placements


def test_fills_pnl_counts_netted_pairs_at_one_dollar():
    """Live 2026-10-09: 1027 YES 2294 @0.936 + NO 1205 @0.06, book 0.915/0.92, fair 0.9865 -> real P&L about
    -$19; valuing the NO leg at 1-fair (1.35c) showed about -$100 more."""
    from sig.engine.fair_band_live import fills_pnl
    fills = [{"exchange_id": "1027", "outcome": "yes", "price": 0.936, "quantity": 2294},
             {"exchange_id": "1027", "outcome": "no", "price": 0.06, "quantity": 1205}]
    books = {"1027": {"best_bid": 0.915, "best_ask": 0.92, "fair": 0.9865}}
    exact = -(2294 * 0.936 + 1205 * 0.06) + 1205 * 1.0 + (2294 - 1205) * 0.915
    assert abs(fills_pnl(fills, books) - exact) < 1e-6
    # a full round trip at cost is exactly zero, however far the book/fair are
    rt = [{"exchange_id": "9", "outcome": "yes", "price": 0.91, "quantity": 1095},
          {"exchange_id": "9", "outcome": "no", "price": 0.09, "quantity": 1095}]
    assert abs(fills_pnl(rt, {"9": {"best_bid": 0.5, "best_ask": 0.6, "fair": 0.99}})) < 1e-6
