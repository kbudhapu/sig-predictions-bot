"""Third adversarial review round (2026-10-08): churn, orders falling through, failure modes.
Each test fails on the code before the fix it covers."""
import asyncio
import json
import os
import sys
import time
import uuid
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.broker import OrderResult
from sig.engine import ownership as own_mod
from sig.engine.ownership import MarketOwnership


def run(c):
    return asyncio.run(c)


@pytest.fixture
def reg(tmp_path):
    r = MarketOwnership(str(tmp_path / "own.json"))
    r.beat("fair_band")
    own_mod.set_registry(r)
    yield r
    own_mod.set_registry(None)


# ───────────── OMS: cancels only count when confirmed; failed reads never wipe state ─────────────
class _St:
    def __init__(self):
        self.updates = []

    def __getattr__(self, name):                                  # any other state call: async no-op
        async def _noop(*a, **k):
            return None
        return _noop

    async def update_order_status(self, oid, status, **kw):
        self.updates.append((oid, status))

    async def append_audit(self, *a, **k):
        pass

    async def write_order(self, *a, **k):
        pass


class RefusingBroker:
    def __init__(self):
        self.placed, self.cancels = [], []

    async def cancel_order(self, oid, **kw):
        self.cancels.append(oid)
        return OrderResult(idempotency_key="", status="rejected", order_id=oid)

    async def place_order(self, **kw):
        self.placed.append(kw)
        return OrderResult(idempotency_key="", status="open", order_id=f"n{len(self.placed)}")

    async def get_order(self, oid):
        return OrderResult(idempotency_key="", status="open", order_id=oid)

    async def get_positions(self, **kw):
        return []


def _bundle(policy, status="open", filled=0.0):
    from sig.oms.models import OrderBundle, OrderSlice, StrategyClass
    sl = OrderSlice(order_id="o1", leg_id="leg_0", contract_id="X", side="buy", requested_qty=100.0,
                    filled_qty=filled, status=status, price=0.5, outcome="yes", exchange_id="X", market_id="X")
    return OrderBundle(bundle_id=str(uuid.uuid4()), strategy_id="s", strategy_class=StrategyClass.DIRECTIONAL,
                       fill_policy=policy, slices=[sl])


def test_chase_never_requotes_on_top_of_an_unconfirmed_cancel():
    from sig.oms.fill_handler import _handle_directional_chase
    from sig.oms.models import FillPolicy
    b = _bundle(FillPolicy.for_directional_chase())
    br, st = RefusingBroker(), _St()
    run(_handle_directional_chase(b, br, st, b.fill_policy))
    assert br.cancels == ["o1"] and br.placed == []           # old code re-placed -> two live orders
    assert b.slices[0].order_id == "o1"


def test_accept_partial_keeps_tracking_when_cancel_unconfirmed():
    from sig.oms.fill_handler import _handle_directional_accept_partial
    from sig.oms.models import FillPolicy
    b = _bundle(FillPolicy.for_directional_accept())
    done = run(_handle_directional_accept_partial(b, RefusingBroker(), _St()))
    assert done is False and b.slices[0].status == "open" and not b.terminal_action_taken


def test_failed_status_read_does_not_wipe_the_slice():
    from sig.oms.fill_handler import _refresh_slice
    from sig.oms.models import FillPolicy

    class Flaky:
        async def get_order(self, oid):
            return OrderResult(idempotency_key="", status="unknown", order_id=oid, filled_qty=0.0)
    b = _bundle(FillPolicy.for_directional_accept(), status="partially_filled", filled=40.0)
    run(_refresh_slice(b.slices[0], Flaky()))
    assert b.slices[0].status == "partially_filled" and b.slices[0].filled_qty == 40.0


def test_cancel_confirmed_semantics():
    from sig.oms.models import cancel_confirmed
    assert cancel_confirmed(OrderResult(idempotency_key="", status="canceled"))
    assert not cancel_confirmed(OrderResult(idempotency_key="", status="rejected"))
    assert not cancel_confirmed(OrderResult(idempotency_key="", status="failed"))
    assert cancel_confirmed(True) and not cancel_confirmed(False)


# ───────────── startup orphan cleanup ─────────────
def test_startup_cancels_only_transient_orphans():
    import main_sig

    class B:
        def __init__(self):
            self.c = []

        async def get_open_orders_all(self):
            return [{"order_id": i, "exchange_id": "1"} for i in ("a", "b", "c", "d", "e", "f")]

        async def cancel_order(self, oid, **kw):
            self.c.append(oid)
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)

    class S:
        async def read_attribution_map(self):
            return {"a": "cross_market_divergence", "b": "exit:1", "c": "market_making", "d": "dutch_book",
                    "e": "fair_band", "f": "trim"}
    b = B()
    n = run(main_sig._cancel_orphan_transient_orders(broker=b, store=S()))
    assert sorted(b.c) == ["a", "b", "f"] and n == 3


def test_startup_cleanup_skips_on_failed_read():
    import main_sig

    class B:
        async def get_open_orders_all(self):
            return None

        async def cancel_order(self, *a, **k):
            raise AssertionError("must not cancel blind")
    assert run(main_sig._cancel_orphan_transient_orders(broker=B(), store=NS())) == 0


# ───────────── MM requote ─────────────
def _req_orch(orders, prices, registered_mm=True, non_mm=()):
    from sig.engine.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._mm_requote_threshold = 0.05
    o._registry = NS(get=lambda n: object() if (registered_mm and n == "market_making") else None)
    cancels = []

    async def getter():
        return orders

    async def cancel(oid, **kw):
        cancels.append(oid)
        return OrderResult(idempotency_key="", status="canceled", order_id=oid)

    async def rp():
        return prices
    o._broker = NS(get_open_orders_valued=getter, cancel_order=cancel)
    o._state = NS(read_prices=rp)
    o._stale_markets = set()
    o._mm_conflict_source = None
    o._mm_cancelled_ids = set()

    async def nonmm():
        return set(non_mm)
    o._non_mm_order_ids = nonmm
    o._mark_cancelled = lambda oid: None
    return o, cancels


def test_requote_does_nothing_when_mm_is_not_registered():
    o, c = _req_orch([{"order_id": "x", "exchange_id": "9", "action": "buy", "side": "yes", "price": 0.3}],
                     {"9": {"best_bid": 0.5, "best_ask": 0.51}}, registered_mm=False)
    assert run(o._run_mm_requote()) == 0 and c == []


def test_requote_compares_no_orders_to_the_no_touch_and_skips_non_mm():
    orders = [{"order_id": "no_ok", "exchange_id": "9", "action": "buy", "side": "no", "price": 0.20},
              {"order_id": "cross", "exchange_id": "9", "action": "buy", "side": "yes", "price": 0.30}]
    o, c = _req_orch(orders, {"9": {"best_bid": 0.79, "best_ask": 0.80}}, non_mm={"cross"})
    run(o._run_mm_requote())
    assert c == []        # NO 0.20 sits AT the NO touch (1-0.80); the old YES-bid comparison cancelled it


# ───────────── cross-race exclusivity, trim guard ─────────────
def test_dutch_book_skips_pairs_in_excluded_races():
    from sig.engine.live_strategies import CoreDutchBookStrategy
    d = CoreDutchBookStrategy.__new__(CoreDutchBookStrategy)
    d._enabled, d._equity = True, 1000.0
    d._pairs = {"race": ("1", "2"), "other": ("3", "4")}
    seen = []
    d._cfg = NS(max_pairs=10)
    d._engine = NS(evaluate_pair=lambda c, a, b, *x: seen.append(c) or None)
    d._yes_bid = lambda ex: 0.5
    d.set_excluded({"2"})
    d.evaluate(None)
    assert seen == ["other"]


def test_trim_refuses_caps_below_the_gate(tmp_path, monkeypatch):
    import tools.trim_positions as tp
    monkeypatch.chdir(tmp_path)
    os.makedirs("config")
    open("config/sig.yaml", "w").write("capital: {max_per_market_frac: 0.15}\n")
    open("config/sig.local.yaml", "w").write("capital: {max_per_market_frac: 0.05}\n")
    assert tp._gate_per_market_cap() == 0.05
    monkeypatch.setattr(sys, "argv", ["trim", "--cap", "0.03", "--execute"])
    monkeypatch.setattr(tp, "plan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must refuse first")))
    tp.main()                                                     # returns before planning


# ───────────── maker lifecycle in the OMS ─────────────
def test_maker_bundles_do_not_block_other_markets_and_carve_out_cash():
    from sig.oms.models import FillPolicy
    from sig.oms.manager import OrderManagementSystem
    oms = OrderManagementSystem.__new__(OrderManagementSystem)
    oms._lock = asyncio.Lock()
    b = _bundle(FillPolicy.for_directional_accept(maker_rest=True, cycle_cancel_threshold=10**9))
    b.strategy_id = "cross"
    oms._pending = {b.bundle_id: b}
    assert run(oms.has_pending_bundle("cross")) is False          # per-market blocking instead
    assert abs(oms.maker_resting_notional() - 100 * 0.5) < 1e-9


def test_maker_sync_keeps_same_price_and_cancels_changed_or_old():
    from datetime import datetime, timedelta, timezone
    from sig.oms.models import FillPolicy
    from sig.oms.manager import OrderManagementSystem

    class Br:
        def __init__(self):
            self.c = []

        async def cancel_order(self, oid, **kw):
            self.c.append(oid)
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)
    oms = OrderManagementSystem.__new__(OrderManagementSystem)
    oms._broker, oms._state = Br(), _St()
    b = _bundle(FillPolicy.for_directional_accept(maker_rest=True))
    b.strategy_id = "cross"
    oms._pending = {b.bundle_id: b}
    busy = run(oms.maker_sync("cross", {("X", 0.5)}))
    assert busy == {"X"} and oms._broker.c == [] and "maker_seen_at" in b.metadata
    run(oms.maker_sync("cross", {("X", 0.45)}))                   # price changed -> cancel
    assert oms._broker.c == ["o1"]
    b2 = _bundle(FillPolicy.for_directional_accept(maker_rest=True))
    b2.strategy_id = "cross"
    b2.created_at = datetime.now(timezone.utc) - timedelta(seconds=oms.MAKER_MAX_AGE_S + 1)
    oms._pending = {b2.bundle_id: b2}
    oms._broker.c = []
    run(oms.maker_sync("cross", {("X", 0.5)}))                    # same price but too old -> cancel
    assert oms._broker.c == ["o1"]


def test_orchestrator_registers_maker_carve_out():
    from sig.engine.orchestrator import Orchestrator
    import inspect
    assert "maker_resting" in inspect.getsource(Orchestrator.__init__)


# ───────────── pilot lifecycle ─────────────
def test_trader_cancels_everything_registered_without_a_read(reg, tmp_path):
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
    reg.claim(SID, ["1"])
    for oid in ("a", "b"):
        reg.note_order(SID, oid, "1")

    class B:
        c = []

        async def cancel_order(self, oid, **kw):
            B.c.append((oid, kw.get("strategy_id")))
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)
    tr = FairBandLive(broker=B(), ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")))
    assert run(tr.cancel_all_registered()) == 2
    assert sorted(B.c) == [("a", SID), ("b", SID)] and reg.orders_of(SID) == []


def test_requote_reprices_when_a_quote_would_become_too_aggressive(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    r1 = {"1": {"fair": 0.60, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}}
    run(tr.tick(r1, {}, v.open_view(), 1e6))
    clk.t += 60
    out = run(tr.tick({"1": {**r1["1"], "fair": 0.605}}, {}, v.open_view(), 1e6))   # NO 0.37 now > target 0.365
    assert out["cancelled"] == 1 and all(o["price"] <= 0.365 + 1e-9 for o in v.orders.values() if o["side"] == "no")


def test_requote_tolerance_keeps_quotes_on_one_tick_moves(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    r1 = {"1": {"fair": 0.60, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}}
    run(tr.tick(r1, {}, v.open_view(), 1e6))
    before = set(v.orders)
    clk.t += 60
    r2 = {"1": {**r1["1"], "fair": 0.595}}      # fair -0.5c: the NO target moves 1 tick MORE conservative -> kept
    out = run(tr.tick(r2, {}, v.open_view(), 1e6))
    assert out["cancelled"] == 0 and set(v.orders) == before


def test_unconfirmed_cancel_never_gets_a_second_order_on_top(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig

    class V(Venue):
        async def cancel_order(self, order_id, *, strategy_id=None):
            return OrderResult(idempotency_key="", status="rejected")
    v = V(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    run(tr.tick({"1": {"fair": 0.60, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1,
                       "age_s": 1}}, {}, v.open_view(), 1e6))
    n = len(v.orders)
    clk.t += 60
    run(tr.tick({"1": {"fair": 0.70, "best_bid": 0.65, "best_ask": 0.66, "bid_size": 1, "ask_size": 1,
                       "age_s": 1}}, {}, v.open_view(), 1e6))   # big move: cancels refused
    keys = [(o["exchange_id"], o["side"]) for o in v.orders.values()]
    assert len(keys) == len(set(keys)) and len(v.orders) == n


def test_liquidation_value_capped_at_fair_and_one_sided_books():
    from sig.engine.fair_band_live import liquidation_value
    assert liquidation_value({"1": {"best_bid": 0.60, "best_ask": 0.61, "fair": 0.55}}, "1", "yes") == 0.55
    assert liquidation_value({"1": {"best_bid": None, "best_ask": 0.61, "fair": 0.55}}, "1", "yes") == 0.50
    assert abs(liquidation_value({"1": {"best_bid": 0.4, "best_ask": None, "fair": 0.45}}, "1", "no") - 0.50) < 1e-9
    assert liquidation_value({}, "1", "yes") == 0.0


def test_registry_prunes_ids_the_venue_stopped_listing(reg, tmp_path):
    from tests.test_isolation import Venue, Clock, claim
    import random
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    reg.note_order(SID, "gone1", "1")                             # filled/cancelled at the venue long ago
    for _ in range(3):
        clk.t += 60
        run(tr.tick({}, {}, v.open_view(), 1e6))
    assert "gone1" not in reg.orders_of(SID)


def test_wind_down_cancel_cap_is_higher_than_quoting_cap(reg, tmp_path):
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig
    tr = FairBandLive(broker=None, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")))
    tr._writes = [time.time()] * 6
    assert not tr._can_write(cancel=True)
    tr.halt("x")
    assert tr._can_write(cancel=True) and not tr._can_write()


def _pilot_with(reg, tmp_path, opens):
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID
    reg.claim(SID, ["1"])
    for oid in ("exp1", "exp2", "live"):
        reg.note_order(SID, oid, "1")

    class B:
        c = []

        async def get_open_orders_all(self):
            if isinstance(opens, Exception):
                raise opens
            return opens

        async def cancel_order(self, oid, **kw):
            B.c.append(oid)
            return OrderResult(idempotency_key="", status="canceled", order_id=oid)
    return FairBandLive(broker=B(), ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S"))), B, SID


def test_clean_slate_never_deletes_expired_orders(reg, tmp_path):
    """Live 2026-10-09: SIG answers DELETE of an EXPIRED order after ~60s with a 429; 9 expired ids stalled the
    pilot's start 6+ min and tripped the write breaker. Only ids still open at the venue are cancelled."""
    tr, B, SID = _pilot_with(reg, tmp_path, [{"order_id": "live", "exchange_id": "1"}, {"order_id": "other", "exchange_id": "9"}])
    assert run(tr.cancel_open_registered()) == 1
    assert B.c == ["live"]                                     # never touches expired ids or another strategy's order
    assert reg.orders_of(SID) == []                            # expired ids forgotten without a write


def test_clean_slate_falls_back_to_blind_cancel_when_the_read_fails(reg, tmp_path):
    tr, B, SID = _pilot_with(reg, tmp_path, RuntimeError("read 429"))
    run(tr.cancel_open_registered())
    assert sorted(B.c) == ["exp1", "exp2", "live"]             # unknown state -> cancel everything registered


def test_clean_slate_falls_back_when_the_read_returns_none(reg, tmp_path):
    tr, B, SID = _pilot_with(reg, tmp_path, None)
    run(tr.cancel_open_registered())
    assert sorted(B.c) == ["exp1", "exp2", "live"]


def test_startup_and_shutdown_use_the_read_first_clean_slate():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main_sig.py"),
               encoding="utf-8").read()
    assert "n0 = await trader.cancel_open_registered()" in src
    assert "asyncio.shield(trader.cancel_open_registered())" in src
