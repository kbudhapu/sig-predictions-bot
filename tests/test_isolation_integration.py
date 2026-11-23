"""Isolation integration tests (2026-10-08): the REAL orchestrator/OMS/gate/sizer (SIM stack) and the live loop.

  * an owned market gets NO order from any orchestrator strategy (single-leg or multi-leg), unowned still trades;
  * the ledger carve-out starves every orchestrator strategy of the reserved cash;
  * maker entries rest without chasing, block stacking, and are cancelled by the timeout sweep (and only then);
  * the live loop refuses to trade without a ledger, registers its carve-out BEFORE the first order, and does
    nothing when its mode is off.
"""
import asyncio
import os
import sqlite3
import sys
import time
import uuid

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.broker import OrderResult
from sig.engine import ownership as own_mod
from sig.engine.composition import SimulatedBroker, build_sim_stack
from sig.engine.ownership import MarketOwnership
from sig.engine.protocols import EdgePerf, Intent


def _cfg() -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def reg(tmp_path):
    r = MarketOwnership(str(tmp_path / "own.json"))
    r.beat("fair_band")
    own_mod.set_registry(r)
    yield r
    own_mod.set_registry(None)


class _Strat:
    tier = "CORE"
    cadence_s = 0.0

    def __init__(self, name, ex, md=None, legs=None):
        self.name, self.ex, self.md, self.legs = name, ex, dict(md or {}), legs
        self.calls = 0

    def evaluate(self, state):
        self.calls += 1
        if getattr(self, "calls_off", False):
            return []
        if self.legs:
            return [Intent(strategy_id=self.name, exchange_id=self.legs[0]["exchange_id"], side="buy",
                           action="open", strategy_type="arb", signal_strength=1.0, price=0.4,
                           legs=[dict(l) for l in self.legs], metadata={"sigma_p": 0.06})]
        return [Intent(strategy_id=self.name, exchange_id=self.ex, side="buy", action="open",
                       strategy_type="directional", signal_strength=1.0, price=getattr(self, "price_override", 0.5),
                       metadata={"sigma_p": 0.06, "option_side": "yes", **self.md})]

    def on_fill(self, fill):
        pass

    def realized_performance(self):
        return EdgePerf(realized_pnl=0.0, n_trades=0)


async def _stack(strat, broker=None):
    orch, b, allocator, state, oms = build_sim_stack(config=_cfg(), strategy_contracts={strat.name: strat.ex},
                                                     buying_power=100_000.0, equity=100_000.0, broker=broker)
    orch.register_strategy(strat)
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await allocator.refresh()
    return orch, b, oms


# ─────────────── orchestrator honours ownership ───────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ["market_making", "cross_market_divergence"])
async def test_owned_market_gets_no_orchestrator_order(reg, sid):
    reg.claim("fair_band", ["YES_TEST"])
    s = _Strat(sid, "YES_TEST")
    orch, b, _ = await _stack(s)
    for _ in range(3):
        await orch._run_cycle()
    assert s.calls >= 1 and b.placed == []


@pytest.mark.asyncio
async def test_unowned_market_still_trades(reg):
    reg.claim("fair_band", ["OTHER"])
    s = _Strat("market_making", "YES_TEST")
    orch, b, _ = await _stack(s)
    await orch._run_cycle()
    assert len(b.placed) == 1


@pytest.mark.asyncio
async def test_multi_leg_touching_an_owned_market_places_nothing(reg):
    reg.claim("fair_band", ["B"])
    s = _Strat("arb", "A", legs=[{"exchange_id": "A", "side": "buy", "outcome": "yes", "quantity": 10, "price": 0.4},
                                  {"exchange_id": "B", "side": "buy", "outcome": "yes", "quantity": 10, "price": 0.5}])
    orch, b, _ = await _stack(s)
    await orch._run_cycle()
    assert b.placed == []


# ─────────────── ledger carve-out starves everyone else ───────────────
@pytest.mark.asyncio
async def test_carve_out_blocks_orchestrator_spending(reg):
    s = _Strat("market_making", "YES_TEST")
    orch, b, _ = await _stack(s)
    orch.ledger.external_reserves["fair_band"] = lambda: 100_000.0         # all cash reserved
    for _ in range(3):
        await orch._run_cycle()
    assert b.placed == []
    s2 = _Strat("market_making", "YES_TEST")                               # control: partial carve-out
    orch2, b2, _ = await _stack(s2)
    orch2.ledger.external_reserves["fair_band"] = lambda: 10_000.0
    await orch2._run_cycle()
    assert len(b2.placed) == 1
    spent = b2.placed[0]["quantity"] * 0.5
    assert spent <= 100_000.0 - 10_000.0                                   # never touched the reserved $10k


# ─────────────── maker entries in the real OMS ───────────────
class RestingBroker(SimulatedBroker):
    """Orders rest (never fill) until cancelled — what a maker entry looks like on the venue."""

    async def place_order(self, *, strategy_id, market_id, exchange_id, side, action, quantity, price=None,
                          priority=False, outcome=None, **_kw):
        self.placed.append({"strategy_id": strategy_id, "exchange_id": exchange_id, "quantity": int(quantity),
                            "price": price})
        oid = f"rest-{uuid.uuid4().hex[:8]}"
        self._orders[oid] = {"status": "open", "filled_qty": 0.0, "avg_price": 0.0}
        return OrderResult(idempotency_key=self._key(strategy_id, market_id, side, quantity, price),
                           status="open", order_id=oid, filled_qty=0.0)

    def cancelled(self):
        return sum(1 for o in self._orders.values() if o["status"] == "canceled")


@pytest.mark.asyncio
async def test_maker_entry_rests_no_chase_no_stacking_then_times_out(reg):
    s = _Strat("cross_market_divergence", "YES_TEST", md={"maker_entry": True, "maker_timeout_s": 0.6})
    orch, b, oms = await _stack(s, broker=RestingBroker())
    for _ in range(8):                                                    # well past the 3-cycle chase trigger
        await orch._run_cycle()
    assert len(b.placed) == 1 and b.cancelled() == 0                      # rested; no chase, no stacking
    assert b.placed[0]["price"] == 0.5
    t_end = time.time() + 1.5                                             # time passes WITH cycles (as live)
    while time.time() < t_end:
        await orch._run_cycle()
        time.sleep(0.1)
    assert b.cancelled() == 0 and len(b.placed) == 1                      # still wanted at 0.50: KEPT (no churn)
    s.md["maker_entry"] = False                                           # signal gone -> no maker intent
    s.calls_off = True
    await orch._run_cycle()                                               # maker_sync cancels it
    assert b.cancelled() == 1
    for _ in range(3):
        await orch._run_cycle()
    assert len(b.placed) == 1                                             # no signal -> no re-entry
    s.calls_off = False
    s.md["maker_entry"] = True
    for _ in range(3):
        await orch._run_cycle()
    assert len(b.placed) == 2                                             # signal back -> exactly one fresh entry


@pytest.mark.asyncio
async def test_taker_path_still_chases(reg):
    s = _Strat("cross_market_divergence", "YES_TEST")
    orch, b, _ = await _stack(s, broker=RestingBroker())
    for _ in range(8):
        await orch._run_cycle()
    assert b.cancelled() >= 1 and len(b.placed) >= 2                     # control: chase re-quotes


# ─────────────── the live loop's own guards ───────────────
class _Store:
    def __init__(self, path):
        self._db_path = path


def _db(tmp_path):
    p = str(tmp_path / "state.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE sharp_history (ts TEXT, exchange_id TEXT, sharp REAL, sig_mid REAL);
        CREATE TABLE trade_tape (created_at TEXT, exchange_id TEXT, size REAL, price REAL);
        CREATE TABLE kalshi_history (ts TEXT, exchange_id TEXT, mid REAL);
        CREATE TABLE strategy_fills (exchange_id TEXT, outcome TEXT, price REAL, quantity REAL, signed_qty REAL,
                                     filled_at TEXT);
    """)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for i in range(6):
        c.execute("INSERT INTO trade_tape VALUES (?,?,?,?)", (now, str(i), 1000.0 * (i + 1), 0.5))
        c.execute("INSERT INTO sharp_history VALUES (?,?,?,?)", (now, str(i), 0.6, 0.55))
    c.commit()
    c.close()
    return p


class _XM:
    def __init__(self):
        from datetime import datetime, timezone
        self._sig_prices = {str(i): {"best_bid": 0.55, "best_ask": 0.56, "bid_size": 10, "ask_size": 10,
                                     "updated_at": datetime.now(timezone.utc).isoformat()} for i in range(6)}

    def active_sig_exchange_ids(self):
        return [str(i) for i in range(6)]

    def aligned_benchmark(self, ex):
        return 0.62


class _LiveBroker:
    def __init__(self, ledger):
        self.ledger, self.placed, self.carve_seen, self.resting, self.cancelled = ledger, [], [], {}, []

    async def get_positions(self, fresh=False):
        return []

    async def get_open_orders_all(self):
        return [dict(o) for o in self.resting.values()]

    async def get_account(self):
        from types import SimpleNamespace
        return SimpleNamespace(buying_power=50_000.0, equity=90_000.0)

    async def place_order(self, **kw):
        self.carve_seen.append("fair_band" in self.ledger.external_reserves)
        self.placed.append(kw)
        oid = f"o{len(self.placed)}"
        own_mod.registry().note_order(kw["strategy_id"], oid, kw["exchange_id"])
        self.resting[oid] = {"order_id": oid, "exchange_id": kw["exchange_id"], "side": kw["outcome"],
                             "action": "buy", "price": kw["price"], "quantity": float(kw["quantity"])}
        return OrderResult(idempotency_key="", status="open", order_id=oid)

    async def cancel_order(self, oid, *, strategy_id=None):
        self.cancelled.append(oid)
        self.resting.pop(oid, None)
        return OrderResult(idempotency_key="", status="canceled", order_id=oid)


ticks_arg_holder = [2]


def _run_loop(tmp_path, monkeypatch, *, mode="live", orch="ok", broker=None, ticks=2):
    import main_sig
    from sig.engine.capital_ledger import CapitalLedger
    monkeypatch.chdir(tmp_path)
    led = CapitalLedger()
    o = type("O", (), {"ledger": led})() if orch == "ok" else None
    br = broker or _LiveBroker(led)
    br.ledger = led
    stop = asyncio.Event()
    real_sleep = asyncio.sleep
    ticks = {"n": 0}
    n_ticks = ticks_arg_holder[0]

    async def fast_sleep(s):
        ticks["n"] += 1
        if ticks["n"] > n_ticks:
            stop.set()
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    cfg = {"cross_market": {"fair_band_live_mode": mode, "fair_band_live_markets": 3}}
    asyncio.run(main_sig._fair_band_live_loop(store=_Store(_db(tmp_path)), broker=br, orch=o, xm_provider=_XM(),
                                              cfg_raw=cfg, stop_event=stop))
    return br, led


def test_live_loop_registers_carve_out_before_first_order(reg, tmp_path, monkeypatch):
    br, led = _run_loop(tmp_path, monkeypatch)
    assert br.placed and all(br.carve_seen)
    assert all(p["strategy_id"] == "fair_band" for p in br.placed)
    # 6 markets -> top third by volume are "liquid" -> 2 candidates (the two most traded), cap of 3 not reached
    assert reg.owned_by("fair_band") == {"4", "5"}
    assert {p["exchange_id"] for p in br.placed} <= reg.owned_by("fair_band")
    assert "fair_band" in led.external_reserves


def test_live_loop_refuses_without_ledger(reg, tmp_path, monkeypatch):
    br, _ = _run_loop(tmp_path, monkeypatch, orch=None)
    assert br.placed == [] and reg.owned_by("fair_band") == set()


def test_live_loop_off_does_nothing(reg, tmp_path, monkeypatch):
    br, led = _run_loop(tmp_path, monkeypatch, mode="off")
    assert br.placed == [] and "fair_band" not in led.external_reserves and reg.owned_by("fair_band") == set()


def test_loss_limit_counts_only_fills_after_each_claim(reg, tmp_path):
    import main_sig
    p = _db(tmp_path)
    reg.claim("fair_band", ["1"])
    t_claim = reg.claimed_at("1")
    c = sqlite3.connect(p)
    c.execute("INSERT INTO strategy_fills VALUES ('1','yes',0.90,1000,1000,'2000-01-01T00:00:00.000Z')")  # before
    # venue format: '...Z', millisecond precision (live strategy_fills)
    c.execute("INSERT INTO strategy_fills VALUES ('1','yes',0.50,100,100,?)", (t_claim[:19] + ".999Z",))
    c.commit(); c.close()
    pnl = main_sig._fair_band_live_pnl(p, {"1"}, "1999-01-01", {"1": {"best_bid": 0.40, "best_ask": 0.41}},
                                       reg.claimed_at)
    assert abs(pnl - 100 * (0.40 - 0.50)) < 1e-6                      # the pre-claim -$500 fill is ignored



# ─────────────── review round: OMS maker_rest, MM paths, loop pacing / mode-off wind-down ───────────────
class PartialThenRestBroker(RestingBroker):
    """Maker order partly fills and KEEPS RESTING (status partially_filled); cancels are confirmed."""
    async def place_order(self, **kw):
        r = await super().place_order(**kw)
        self._orders[r.order_id].update(status="partially_filled", filled_qty=float(kw["quantity"]) / 2)
        return r


class UnknownStatusBroker(RestingBroker):
    async def get_order(self, order_id):
        return OrderResult(idempotency_key="", status="unknown", order_id=order_id)


class RefuseCancelBroker(RestingBroker):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.refuse = True

    async def cancel_order(self, order_id, **kw):
        if self.refuse:
            return OrderResult(idempotency_key="", status="rejected", order_id=order_id)
        return await super().cancel_order(order_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("brk", [PartialThenRestBroker, UnknownStatusBroker])
async def test_maker_rest_never_orphans_a_resting_residual(reg, brk):
    s = _Strat("cross_market_divergence", "YES_TEST", md={"maker_entry": True, "maker_timeout_s": 0.4})
    orch, b, oms = await _stack(s, broker=brk())
    for _ in range(4):
        await orch._run_cycle()
    assert len(b.placed) == 1 and oms._pending                   # still tracked (no orphan, no early resolve)
    time.sleep(0.5)
    await orch._run_cycle()
    assert b.cancelled() == 1                                    # the timeout sweep cancelled the residual


@pytest.mark.asyncio
async def test_maker_rest_unconfirmed_cancel_stays_tracked_then_retries(reg):
    s = _Strat("cross_market_divergence", "YES_TEST", md={"maker_entry": True, "maker_timeout_s": 0.3})
    b = RefuseCancelBroker()
    orch, b, oms = await _stack(s, broker=b)
    oms.MAKER_CANCEL_BACKOFF_S = 0.0                             # retry immediately in the test (live: 30s)
    await orch._run_cycle()
    s.md["maker_entry"] = True
    s.price_override = 0.45                                      # signal moved: the resting 0.50 must go
    time.sleep(0.4)
    for _ in range(3):
        await orch._run_cycle()
    assert oms._pending and len(b.placed) == 1                   # refused cancel: NOT marked cancelled
    first = b.placed[0]
    b.refuse = False
    await orch._run_cycle()
    await orch._run_cycle()
    assert b.cancelled() == 1                                    # confirmed cancel on retry
    assert len(b.placed) == 2 and b.placed[1] is not first       # bundle resolved -> strategy entered afresh


@pytest.mark.asyncio
async def test_maker_rest_bundles_get_distinct_keys(reg):
    class KeyBroker(RestingBroker):
        keys = []

        async def place_order(self, **kw):
            KeyBroker.keys.append(self._key(kw["strategy_id"], kw["market_id"], kw["side"], kw["quantity"],
                                            kw["price"]))
            return await super().place_order(**kw)
    s = _Strat("cross_market_divergence", "YES_TEST", md={"maker_entry": True, "maker_timeout_s": 0.2})
    orch, b, oms = await _stack(s, broker=KeyBroker())
    await orch._run_cycle()
    time.sleep(0.3)
    for _ in range(4):
        await orch._run_cycle()
    assert len(KeyBroker.keys) >= 2 and len(set(KeyBroker.keys)) == len(KeyBroker.keys)


@pytest.mark.asyncio
async def test_non_maker_accept_partial_behaviour_unchanged(reg):
    """Other ACCEPT_PARTIAL users (e.g. mean_reversion) keep the original cycle-threshold resolution."""
    from sig.oms.models import FillPolicy, PartialFillPolicy
    from sig.oms.fill_handler import advance_bundle
    s = _Strat("x", "YES_TEST")
    orch, b, oms = await _stack(s, broker=UnknownStatusBroker())
    bundle = await oms.submit(strategy_id="x", legs=[{"contract_id": "YES_TEST", "side": "buy", "qty": 5,
                                                      "price": 0.5, "outcome": "yes"}],
                              fill_policy=FillPolicy.for_directional_accept())
    done = [await advance_bundle(bundle, b, oms._state) for _ in range(3)]
    assert done == [False, False, True]                           # resolves at the threshold, not on cycle 1


@pytest.mark.asyncio
async def test_mm_group_with_an_owned_leg_still_posts_the_rest(reg):
    reg.claim("fair_band", ["A"])
    s = _Strat("market_making", "B")
    orch, b, _ = await _stack(s)
    mk = lambda ex: Intent(strategy_id="market_making", exchange_id=ex, side="buy", action="open",
                           strategy_type="market_making", signal_strength=1.0, price=0.5,
                           metadata={"sigma_p": 0.06, "option_side": "yes", "mm_quote_qty": 10})
    await orch._process_mm_quote_group([mk("A"), mk("B")], s)
    assert {p["exchange_id"] for p in b.placed} <= {"B"}


@pytest.mark.asyncio
async def test_mm_requote_never_touches_owned_markets(reg):
    reg.claim("fair_band", ["A"])
    s = _Strat("market_making", "B")                              # registered as "market_making" -> requote runs
    orch, b, _ = await _stack(s)
    cancels = []

    async def get_open_orders_valued():
        return [{"order_id": "fb1", "exchange_id": "A", "side": "yes", "action": "buy", "price": 0.30,
                 "quantity": 10}, {"order_id": "mm1", "exchange_id": "B", "side": "yes", "action": "buy",
                                   "price": 0.30, "quantity": 10}]

    async def cancel_order(oid, **kw):
        cancels.append(oid)
        return OrderResult(idempotency_key="", status="canceled", order_id=oid)

    async def read_prices():
        return {"A": {"best_bid": 0.50}, "B": {"best_bid": 0.50}}
    b.get_open_orders_valued = get_open_orders_valued
    b.cancel_order = cancel_order
    orch._state.read_prices = read_prices
    orch._open_snapshot = None
    await orch._run_mm_requote()
    assert "fb1" not in cancels and "mm1" in cancels


def test_loop_mode_off_with_owned_markets_winds_down(reg, tmp_path, monkeypatch):
    from sig.engine.capital_ledger import CapitalLedger
    reg.claim("fair_band", ["4", "5"])
    br = _LiveBroker(CapitalLedger())
    br.resting["x1"] = {"order_id": "x1", "exchange_id": "4", "side": "yes", "action": "buy", "price": 0.5,
                        "quantity": 10.0}
    reg.note_order("fair_band", "x1", "4")
    ticks_arg_holder[0] = 3
    br2, _ = _run_loop(tmp_path, monkeypatch, mode="off", broker=br)
    ticks_arg_holder[0] = 2
    assert br2.placed == [] and "x1" in br2.cancelled and reg.owned_by("fair_band") == set()


def test_loop_keeps_a_fixed_pace_when_ticks_fail(reg, tmp_path, monkeypatch):
    import main_sig
    from sig.engine.capital_ledger import CapitalLedger
    sleeps = []

    class Boom(_LiveBroker):
        async def get_open_orders_all(self):
            raise RuntimeError("venue down")
    stop = asyncio.Event()

    async def rec_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 4:
            stop.set()
    monkeypatch.setattr(main_sig.asyncio, "sleep", rec_sleep)
    monkeypatch.chdir(tmp_path)
    o = type("O", (), {"ledger": CapitalLedger()})()
    asyncio.run(main_sig._fair_band_live_loop(store=_Store(_db(tmp_path)), broker=Boom(o.ledger), orch=o,
                                              xm_provider=_XM(), cfg_raw={"cross_market": {"fair_band_live_mode": "live"}},
                                              stop_event=stop))
    assert sleeps[0] == 5.0 and all(s == 60.0 for s in sleeps[1:])


@pytest.mark.asyncio
async def test_maker_sync_never_cancels_an_order_that_already_finished(reg):
    """Live 2026-10-09: maker_sync DELETEd maker entries that had already filled / expired; SIG answers those only
    after ~60s with a 429 -> stalled cycles, tripped write breaker. Look first: a finished order is not cancelled."""
    s = _Strat("cross_market_divergence", "YES_TEST", md={"maker_entry": True, "maker_timeout_s": 600})

    class GoneBroker(RestingBroker):
        gone = False
        cancels = 0

        async def get_order(self, order_id):
            o = self._orders.get(order_id)
            if o is not None and self.gone:
                o["status"] = "expired"                 # filled/expired at the venue since our last look
            return await super().get_order(order_id)

        async def cancel_order(self, order_id, **kw):
            GoneBroker.cancels += 1
            return await super().cancel_order(order_id)
    orch, b, oms = await _stack(s, broker=GoneBroker())
    for _ in range(3):
        await orch._run_cycle()
    assert len(b.placed) == 1
    b.gone = True
    s.md["maker_entry"] = False                         # signal gone -> maker_sync would cancel it
    s.calls_off = True
    await orch._run_cycle()
    assert GoneBroker.cancels == 0                      # looked first: already finished, no DELETE sent


@pytest.mark.asyncio
async def test_maker_timeout_sweep_never_cancels_an_order_that_already_finished(reg):
    """The OMS timeout sweep is a second cancel path (2026-10-10: it DELETEd a gone maker entry -> 38-min cycle).
    It must look first, like maker_sync."""
    s = _Strat("cross_market_divergence", "YES_TEST", md={"maker_entry": True, "maker_timeout_s": 0.2})

    class GoneBroker(RestingBroker):
        gone = False
        cancels = 0

        async def get_order(self, order_id):
            o = self._orders.get(order_id)
            if o is not None and self.gone:
                o["status"] = "expired"
            return await super().get_order(order_id)

        async def cancel_order(self, order_id, **kw):
            GoneBroker.cancels += 1
            return await super().cancel_order(order_id)
    orch, b, oms = await _stack(s, broker=GoneBroker())
    await orch._run_cycle()
    assert len(b.placed) == 1
    b.gone = True
    s.calls_off = True                                  # no new intents; the bundle times out
    time.sleep(0.3)
    for _ in range(3):
        await oms.sweep_unfilled_timeouts()
    assert GoneBroker.cancels == 0
