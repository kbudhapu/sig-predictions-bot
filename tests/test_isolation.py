"""ADVERSARIAL isolation tests (2026-10-08) for going live with the fair-band maker pilot + maker entries.

Goal: capital can never cross between strategies, and strategies cannot touch each other's orders or positions.
Covers the ownership registry, the REAL SigBroker chokepoint (fake transport: refused orders must make ZERO HTTP
calls), the ledger carve-out, the live trader against a hostile simulated venue (random fills, lagging reads,
duplicates, rejects, failed reads, stale books, cash squeeze, kill switches, restart), and maker entries.
"""
import asyncio
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SIG_API_KEY_WRITE", "sk_TEST_not_a_real_key")

from sig.broker import SigBroker, WriteGate, OrderResult
from sig.write_gate import HttpResult
from sig.engine import ownership as own_mod
from sig.engine.ownership import MarketOwnership
from sig.engine.capital_ledger import CapitalLedger
from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID, fills_pnl


def run(c):
    return asyncio.run(c)


@pytest.fixture
def reg(tmp_path):
    r = MarketOwnership(str(tmp_path / "own.json"))
    r.beat("fair_band")                                   # owner alive (lease live) unless a test says otherwise
    own_mod.set_registry(r)
    yield r
    own_mod.set_registry(None)


# ───────────────────────── 1. registry ─────────────────────────
def test_claim_never_steals_release_only_by_owner_and_persists(tmp_path, reg):
    assert reg.claim("fair_band", ["1", "2"]) == ["1", "2"]
    assert reg.claim("other", ["2", "3"]) == ["3"]                       # 2 not stolen
    assert reg.release("other", "1") is False and reg.owner_of("1") == "fair_band"
    reg.note_order("fair_band", "o1", "1")
    reg.note_order("fair_band", "o9", "3")                               # not its market: ignored
    again = MarketOwnership(str(tmp_path / "own.json"))                 # restart
    assert again.owned_by("fair_band") == {"1", "2"} and again.order_owner("o1") == "fair_band"
    assert again.order_owner("o9") is None


def test_two_way_isolation_rules(reg):
    reg.claim("fair_band", ["1"])
    assert reg.allows("fair_band", "1") and not reg.allows("cross_market_divergence", "1")
    assert not reg.allows("exit:1", "1") and not reg.allows("market_making", "1")
    assert reg.allows("cross_market_divergence", "2")                    # unowned: unchanged
    assert not reg.allows("fair_band", "2")                              # isolated strategy can't leave home


def test_lease_lapse_lets_exits_manage_but_never_new_entries(tmp_path):
    from datetime import datetime, timedelta, timezone
    r = MarketOwnership(str(tmp_path / "own.json"))
    r.claim("fair_band", ["1"])
    r.beat("fair_band", now=datetime.now(timezone.utc) - timedelta(seconds=r.LEASE_S + 5))   # owner went quiet
    assert r.allows("exit:1", "1")                                       # exits may risk-manage abandoned inventory
    assert not r.allows("cross_market_divergence", "1") and not r.allows("market_making", "1")
    assert r.managed_owner("1") is None
    r.beat("fair_band")                                                  # owner back
    assert not r.allows("exit:1", "1") and r.managed_owner("1") == "fair_band"


def test_corrupt_registry_halts_owner(tmp_path):
    p = tmp_path / "own.json"
    p.write_text("{not json")
    r = MarketOwnership(str(p))
    assert r.corrupt
    t = FairBandLive(broker=None, ownership=r)
    assert t.select_markets(["1"], {}, []) == [] and t.winding_down and t.unused_budget() == 0.0


def test_registry_save_survives_transient_windows_replace_denial(tmp_path, monkeypatch):
    """Windows (Defender / indexer) briefly locks a just-written file, so os.replace fails with WinError 5 for a few
    ms. That made the seeded chaos test above flaky (PermissionError, never an invariant). The registry must retry a
    transient denial instead of crashing the trader's tick; a persistent denial still raises (claim() undoes)."""
    r = MarketOwnership(str(tmp_path / "own.json"))
    real, fails = os.replace, {"n": 2}

    def flaky(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(5, "Access is denied")
        return real(src, dst)
    monkeypatch.setattr(own_mod.os, "replace", flaky)
    assert r.claim("fair_band", ["1"]) == ["1"]
    assert MarketOwnership(str(tmp_path / "own.json")).owner_of("1") == "fair_band"

    def denied(src, dst):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(own_mod.os, "replace", denied)
    with pytest.raises(PermissionError):
        r.claim("fair_band", ["2"])
    assert r.owner_of("2") is None                                       # not persisted -> undone


# ───────────────────────── 2. REAL broker chokepoint ─────────────────────────
class Transport:
    def __init__(self):
        self.calls, self.n = [], 0

    async def __call__(self, method, path, body, params):
        self.calls.append((method, path, body))
        self.n += 1
        if method == "POST" and path == "/orders/multi-leg":
            return HttpResult(200, json.dumps({"orderId": f"m{self.n}", "status": "open", "legs": []}))
        if method == "DELETE":
            return HttpResult(200, "{}")
        return HttpResult(200, json.dumps({"id": f"srv{self.n}", "status": "open", **(body or {})}))


def _broker():
    async def _sleep(s):
        return None
    t = Transport()
    return SigBroker(transport=t, gate=WriteGate(writes_per_min=1000, window_s=60), sleep=_sleep), t


@pytest.mark.parametrize("intruder", ["cross_market_divergence", "exit:1", "market_making", "dutch_book",
                                      "trim", "UNATTRIBUTED", ""])
def test_intruder_orders_in_owned_market_make_zero_http_calls(reg, intruder):
    reg.claim("fair_band", ["1"])
    b, t = _broker()
    r = run(b.place_order(strategy_id=intruder, market_id="1", exchange_id="1", side="buy", action="buy",
                          quantity=10, price=0.5, outcome="no"))
    assert r.status == "rejected" and "market_owned_by:fair_band" in r.raw["reason"]
    assert t.calls == []


def test_multi_leg_with_one_owned_leg_refused_whole(reg):
    reg.claim("fair_band", ["2"])
    b, t = _broker()
    r = run(b.place_multi_leg(strategy_id="dutch_book", market_id="x", legs=[
        {"exchange_id": "1", "outcome": "yes", "side": "buy", "quantity": 5, "price": 0.4},
        {"exchange_id": "2", "outcome": "yes", "side": "buy", "quantity": 5, "price": 0.5}]))
    assert r.status == "rejected" and t.calls == []


def test_owner_orders_recorded_and_cancel_scoped(reg):
    reg.claim("fair_band", ["1"])
    b, t = _broker()
    r = run(b.place_order(strategy_id="fair_band", market_id="1", exchange_id="1", side="buy", action="buy",
                          quantity=10, price=0.5, outcome="yes"))
    assert r.status != "rejected" and reg.order_owner(r.order_id) == "fair_band"
    n0 = len(t.calls)
    for caller in (None, "market_making", "cross_market_divergence", "exit:1"):   # OMS sweep/chase/MM requote
        c = run(b.cancel_order(r.order_id, strategy_id=caller))
        assert c.status == "rejected"
    assert len(t.calls) == n0                                            # no DELETE ever sent
    c = run(b.cancel_order(r.order_id, strategy_id="fair_band"))
    assert c.status == "canceled" and t.calls[-1][0] == "DELETE"


def test_isolated_strategy_cannot_trade_outside_its_markets(reg):
    reg.claim("fair_band", ["1"])
    b, t = _broker()
    r = run(b.place_order(strategy_id="fair_band", market_id="2", exchange_id="2", side="buy", action="buy",
                          quantity=10, price=0.5, outcome="yes"))
    assert r.status == "rejected" and t.calls == []


def test_unowned_markets_unchanged_for_existing_strategies(reg):
    b, t = _broker()
    r = run(b.place_order(strategy_id="cross_market_divergence", market_id="7", exchange_id="7", side="buy",
                          action="buy", quantity=10, price=0.5, outcome="yes"))
    assert r.status != "rejected" and len(t.calls) == 1
    assert run(b.cancel_order(r.order_id)).status == "canceled"          # legacy call signature still works


# ───────────────────────── 3. ledger carve-out ─────────────────────────
def test_ledger_carve_out_is_hard_and_survives_a_failing_callable():
    led = CapitalLedger()
    val = {"v": 6000.0, "boom": False}

    def res():
        if val["boom"]:
            raise RuntimeError
        return val["v"]
    led.external_reserves["fair_band"] = res
    led.begin_cycle(buying_power=10000.0, equity=90000.0)
    assert led.available_buying_power() == 4000.0
    assert led.reserve("cross_market_divergence", 9999.0) == 4000.0       # can never dip into the carve-out
    val["boom"] = True
    led.begin_cycle(buying_power=10000.0, equity=90000.0)
    assert led.available_buying_power() == 4000.0                         # last value reused, not dropped
    val.update(boom=False, v=20000.0)
    led.begin_cycle(buying_power=10000.0, equity=90000.0)
    assert led.available_buying_power() == 0.0                            # never negative


# ───────────────────────── 4. trader vs a hostile venue ─────────────────────────
class Venue:
    """Fake SIG venue + broker: random fills, optional lagging open-orders view, rejects, and failed reads.
    Enforces the REAL ownership chokepoint via a real SigBroker for placements/cancels."""

    def __init__(self, reg, rng, lag=False, reject_p=0.0):
        self.reg, self.rng, self.lag, self.reject_p = reg, rng, lag, reject_p
        self.orders = {}             # oid -> dict
        self.pos = {}                # ex -> signed YES qty
        self.n = 0
        self.writes = []
        self.lagged = set()
        self.sells = 0
        self.pairs_netted = 0.0      # $ returned by YES/NO netting (1 per pair)
        self.mids = {}               # the mids of the current tick (set by the test)
        self.cfg = None              # when set, every placement is checked against budget / market cap
        self.violations = []

    async def place_order(self, *, strategy_id, market_id, exchange_id, side, action, quantity, price,
                          outcome=None, priority=False, **_kw):
        if not self.reg.allows(strategy_id, exchange_id):
            return OrderResult(idempotency_key="", status="rejected", raw={"reason": "owned"})
        self.writes.append(("place", strategy_id, exchange_id))
        if self.cfg is not None and self.mids:
            def val(ex, outc, px, q):
                m = self.mids[ex]
                return max(float(px), m if outc == "yes" else 1.0 - m) * float(q)
            pos = sum(FairBandLive._exposure(self.pos.get(ex, 0.0), self.mids[ex]) for ex in self.mids)
            rest = sum(val(o["exchange_id"], o["side"], o["price"], o["quantity"]) for o in self.orders.values())
            new = val(str(exchange_id), outcome, price, quantity)
            if pos + rest + new > self.cfg.budget_usd + 1:
                self.violations.append(("budget", round(pos + rest + new, 2)))
            held = self.pos.get(str(exchange_id), 0.0)
            if (outcome == "yes" and held >= 0) or (outcome == "no" and held <= 0):
                mk = FairBandLive._exposure(held, self.mids[str(exchange_id)]) + sum(
                    val(o["exchange_id"], o["side"], o["price"], o["quantity"]) for o in self.orders.values()
                    if o["exchange_id"] == str(exchange_id) and o["side"] == outcome) + new
                if mk > self.cfg.market_cap_usd + 1:
                    self.violations.append(("market_cap", str(exchange_id), round(mk, 2)))
        if action != "buy" or side != "buy":
            self.sells += 1
        if self.rng.random() < self.reject_p:
            return OrderResult(idempotency_key="", status="rejected", raw={})
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = {"order_id": oid, "exchange_id": str(exchange_id), "side": outcome, "action": "buy",
                            "price": float(price), "quantity": float(quantity)}
        self.reg.note_order(strategy_id, oid, exchange_id)
        if self.lag:
            self.lagged.add(oid)
        return OrderResult(idempotency_key="", status="open", order_id=oid)

    async def cancel_order(self, order_id, *, strategy_id=None):
        if not self.reg.allows_cancel(strategy_id, order_id):
            return OrderResult(idempotency_key="", status="rejected")
        self.writes.append(("cancel", strategy_id, order_id))
        self.orders.pop(order_id, None)
        return OrderResult(idempotency_key="", status="canceled", order_id=order_id)

    def open_view(self):
        v = [dict(o) for oid, o in self.orders.items() if oid not in self.lagged]
        self.lagged.clear()                                   # shows up one read later
        return v

    def random_fills(self):
        for oid in list(self.orders):
            if self.rng.random() < 0.3:
                o = self.orders.pop(oid)
                q = o["quantity"] * self.rng.choice([0.3, 1.0])
                if q < o["quantity"]:
                    o["quantity"] -= q
                    self.orders[oid] = o
                d = q if o["side"] == "yes" else -q
                old = self.pos.get(o["exchange_id"], 0.0)
                new = old + d
                if old * d < 0:                                           # opposite side nets pairs to $1
                    self.pairs_netted += min(abs(old), abs(d))
                self.pos[o["exchange_id"]] = new


def _rows(exs, rng, fair=0.5):
    out = {}
    for ex in exs:
        f = min(max(fair + rng.uniform(-0.1, 0.1), 0.06), 0.94)
        bid = round(round((f + rng.uniform(-0.06, 0.06)) / 0.005) * 0.005, 3)
        bid = min(max(bid, 0.01), 0.98)
        out[ex] = {"fair": f, "best_bid": bid, "best_ask": round(bid + 0.005 * rng.randint(1, 3), 3),
                   "bid_size": 500, "ask_size": 500, "age_s": 3}
    return out


class Walk:
    """Realistic per-market path: fair and the SIG book move by small steps (<= 1c/min), book within +-6c of fair."""
    def __init__(self, exs, rng):
        self.rng = rng
        self.f = {ex: rng.uniform(0.2, 0.8) for ex in exs}
        self.off = {ex: rng.uniform(-0.04, 0.04) for ex in exs}

    def rows(self):
        out = {}
        for ex in self.f:
            self.f[ex] = min(max(self.f[ex] + self.rng.uniform(-0.01, 0.01), 0.08), 0.92)
            self.off[ex] = min(max(self.off[ex] + self.rng.uniform(-0.01, 0.01), -0.06), 0.06)
            bid = round(round((self.f[ex] + self.off[ex]) / 0.005) * 0.005, 3)
            bid = min(max(bid, 0.01), 0.98)
            out[ex] = {"fair": self.f[ex], "best_bid": bid, "best_ask": round(bid + 0.005 * self.rng.randint(1, 3), 3),
                       "bid_size": 500, "ask_size": 500, "age_s": 3}
        return out


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("seed,lag,reject_p", [(1, False, 0.0), (2, True, 0.0), (3, True, 0.2), (4, False, 0.5),
                                               (5, True, 0.1), (6, False, 0.0)])
def test_trader_never_breaks_budget_caps_writes_or_isolation(reg, tmp_path, seed, lag, reject_p):
    rng = random.Random(seed)
    clk = Clock()
    cfg = FairBandLiveConfig(budget_usd=3000.0, market_cap_usd=800.0, quote_usd=300.0, n_markets=6,
                             max_writes_per_min=6, stop_file=str(tmp_path / "STOP"))
    v = Venue(reg, rng, lag=lag, reject_p=reject_p)
    reg.claim("cross_market_divergence_held", [])                         # noise
    v.pos["held_by_cross"] = 100.0                                       # an existing cross position
    tr = FairBandLive(broker=v, ownership=reg, cfg=cfg, clock=clk)
    cands = [str(i) for i in range(20)] + ["held_by_cross"]
    busy = [{"order_id": "x1", "exchange_id": "3", "side": "yes", "action": "buy", "price": 0.4, "quantity": 5}]
    owned = tr.select_markets(cands, v.pos, busy)
    assert len(owned) == 6 and "held_by_cross" not in owned and "3" not in owned
    walk = Walk(owned, rng)
    spent = 0.0
    for step in range(400):
        clk.t += 60
        rows = walk.rows()
        mids = {ex: (r["best_bid"] + r["best_ask"]) / 2 for ex, r in rows.items()}
        v.mids, v.cfg = mids, cfg
        value0 = sum(FairBandLive._exposure(v.pos.get(ex, 0.0), mids[ex]) for ex in owned)
        tr.check_kill(value0 - (spent - tr_pairs(v)))                    # loss limit, as the live loop does
        out = run(tr.tick(rows, dict(v.pos), v.open_view(), free_cash=rng.choice([None, 5000.0, 50.0])))
        assert v.violations == [], (step, v.violations[:3])            # checked at every placement
        before = {oid: dict(o) for oid, o in v.orders.items()}
        v.random_fills()
        for oid, o in before.items():                                    # cash actually spent on fills
            q = o["quantity"] - (v.orders[oid]["quantity"] if oid in v.orders else 0.0)
            spent += q * o["price"]
        # CASH BOUND: net cash out (fills minus $1 per netted YES/NO pair) never exceeds budget + loss limit
        net_out = spent - tr_pairs(v)
        assert net_out <= cfg.budget_usd + cfg.max_loss_usd + 1, (step, net_out)
        # writes per rolling minute
        assert out["placed"] + out["cancelled"] <= cfg.max_writes_per_min
        # at most one resting order per (market, side)
        keys = [(o["exchange_id"], o["side"]) for o in v.orders.values()]
        assert len(keys) == len(set(keys)) or lag                       # lag may show a dup for one tick
    assert v.sells == 0                                                  # buys only, never a sell
    assert all(w[1] == SID for w in v.writes)                            # only ever acted as itself
    assert all(o["exchange_id"] in owned for o in v.orders.values())     # never outside its markets
    assert v.pos["held_by_cross"] == 100.0                               # other strategy's position untouched


def claim(tr, exs, v):
    """select + one warm-up tick: newly claimed markets clear probation on the NEXT read."""
    owned = tr.select_markets(exs, dict(v.pos), v.open_view())
    run(tr.tick({}, dict(v.pos), v.open_view(), 1e6))
    return owned


def tr_pairs(v):
    return v.pairs_netted


def test_failed_open_orders_read_means_no_writes(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=Clock())
    tr.select_markets(["1"], {}, [])
    out = run(tr.tick(_rows(["1"], random.Random(0)), {}, None, 1e6))
    assert out.get("skipped") == "open_orders_read_failed" and v.writes == []


def test_lagging_read_never_double_places(reg, tmp_path):
    rng = random.Random(9)
    v = Venue(reg, rng, lag=True)
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    rows = {"1": {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}}
    run(tr.tick(rows, {}, v.open_view(), 1e6))
    assert len(v.orders) == 2
    first = len(v.orders)
    clk.t += 5
    run(tr.tick(rows, {}, [], 1e6))                                       # venue doesn't show them yet
    assert len(v.orders) == first


def test_duplicates_on_venue_are_cancelled(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=Clock())
    claim(tr, ["1"], v)
    rows = {"1": {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}}
    dup = [{"order_id": f"d{i}", "exchange_id": "1", "side": "yes", "action": "buy", "price": 0.55,
            "quantity": 100.0} for i in range(3)]
    for d in dup:
        v.orders[d["order_id"]] = dict(d)
        reg.note_order(SID, d["order_id"], "1")                         # ours (e.g. a retry storm)
    out = run(tr.tick(rows, {}, [dict(d) for d in dup], 1e6))
    assert out["cancelled"] == 2 and sum(1 for o in v.orders.values() if o["side"] == "yes") == 1


@pytest.mark.parametrize("trigger", ["stale", "stop", "loss"])
def test_kill_switches_cancel_everything_and_place_nothing(reg, tmp_path, trigger):
    v = Venue(reg, random.Random(0))
    clk = Clock()
    stop = tmp_path / "STOP"
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(stop), max_writes_per_min=50),
                      clock=clk)
    claim(tr, ["1", "2"], v)
    rows = _rows(["1", "2"], random.Random(1))
    run(tr.tick(rows, {}, v.open_view(), 1e6))
    assert v.orders
    clk.t += 61
    if trigger == "stale":
        rows = {ex: {**r, "age_s": 999} for ex, r in rows.items()}
    elif trigger == "stop":
        stop.write_text("x")
        tr.check_kill(0.0)
    else:
        tr.check_kill(-10_000.0)
    out = run(tr.tick(rows, {}, v.open_view(), 1e6))
    assert out["placed"] == 0 and not v.orders
    if trigger != "stale":
        assert tr.winding_down and tr.unused_budget() == 0.0
        assert reg.owned_by(SID) == set()                                 # flat + no orders -> released
        clk.t += 61
        assert run(tr.tick(_rows(["1", "2"], random.Random(2)), {}, [], 1e6))["placed"] == 0   # sticky


def test_wind_down_keeps_markets_while_holding_inventory(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=Clock())
    tr.select_markets(["1"], {}, [])
    tr.halt("test")
    run(tr.tick(_rows(["1"], random.Random(0)), {"1": 400.0}, [], 1e6))
    assert reg.owned_by(SID) == {"1"}                                     # still guarded: holds inventory
    assert not reg.allows("cross_market_divergence", "1")


def test_restart_never_adopts_foreign_orders_and_keeps_markets(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=Clock())
    tr.select_markets(["1"], {}, [])
    stray = {"order_id": "z1", "exchange_id": "1", "side": "yes", "action": "buy", "price": 0.2, "quantity": 10.0}
    v.orders["z1"] = dict(stray)
    reg2 = MarketOwnership(reg._path)                                    # "restart"
    v.reg = reg2
    tr2 = FairBandLive(broker=v, ownership=reg2, cfg=tr.cfg, clock=Clock())
    assert tr2.select_markets(["2", "3"], {}, [stray]) == ["1", "2", "3"]   # keeps 1, never re-claims busy
    run(tr2.tick({"1": {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1,
                        "age_s": 1}}, {}, [stray], 1e6))
    assert "z1" not in v.orders                                          # unregistered order: CANCELLED, not adopted
    assert reg2.order_owner("z1") is None                                # and never registered as ours


def test_fills_pnl_uses_live_sign_convention():
    # live strategy_fills: a NO BUY is stored with NEGATIVE signed_qty (YES-delta), quantity positive
    f = [{"exchange_id": "1", "outcome": "yes", "price": 0.40, "quantity": 100, "signed_qty": 100},
         {"exchange_id": "1", "outcome": "no", "price": 0.50, "quantity": 100, "signed_qty": -100}]
    assert abs(fills_pnl(f, {"1": {"best_bid": 0.45, "best_ask": 0.45}}) - (100 * 0.05 + 100 * 0.05)) < 1e-9
    # a LOSING NO position must read as a loss (the inverted-sign bug made it a gain)
    lose = [{"exchange_id": "1", "outcome": "no", "price": 0.60, "quantity": 1000, "signed_qty": -1000}]
    assert fills_pnl(lose, {"1": {"best_bid": 0.69, "best_ask": 0.70}}) < -299
    # unknown / one-sided book -> valued at 0 (a loss), never "unknown" (which disabled the limit)
    assert fills_pnl(lose, {}) == -600.0
    assert fills_pnl(lose, {"1": {"best_bid": 0.69, "best_ask": None}}) == -600.0


# ───────────────────────── 5. maker entries ─────────────────────────
def _strat(entry_mode, sig_mid, poly, book):
    from strategies.cross_market import CrossMarketDivergenceStrategy, DivergenceConfig, ExternalQuote
    from tests.test_cross_market_divergence import SIG_EX, _mapping, _sig, _ext
    return CrossMarketDivergenceStrategy(
        mapping=_mapping(), sig_price_provider=_sig(sig_mid),
        external_provider=_ext({"polymarket": ExternalQuote("polymarket", poly, 1.0)}),
        config=DivergenceConfig(entry_mode=entry_mode), sig_book_provider=(lambda ex: book)), SIG_EX


def test_maker_entry_joins_best_bid_of_the_bought_leg():
    s, _ = _strat("maker", 0.50, 0.70, (0.495, 0.505))                   # SIG cheap -> buy YES
    it = s.evaluate(None)[0]
    assert it.metadata["option_side"] == "yes" and it.price == 0.495 and it.metadata["maker_entry"] is True
    s, _ = _strat("maker", 0.50, 0.30, (0.495, 0.505))                   # SIG rich -> buy NO at 1 - ask
    it = s.evaluate(None)[0]
    assert it.metadata["option_side"] == "no" and it.price == 0.495


def test_maker_entry_without_a_book_never_crosses():
    for book in (None, (0.6, 0.5), (0.0, 0.5), ("x", 1)):
        s, _ = _strat("maker", 0.50, 0.70, book)
        assert s.evaluate(None) == []


def test_taker_mode_unchanged():
    s, _ = _strat("taker", 0.50, 0.70, None)
    it = s.evaluate(None)[0]
    assert it.metadata["maker_entry"] is False and it.price == 0.5


def test_maker_fill_policy_never_chases_and_times_out():
    from sig.engine.orchestrator import _fill_policy_for
    from sig.engine.protocols import Intent
    i = Intent(strategy_id="cross_market_divergence", exchange_id="1", side="buy", action="open",
               strategy_type="directional", signal_strength=1.0, price=0.5,
               metadata={"maker_entry": True, "maker_timeout_s": 90.0})
    p = _fill_policy_for(i)
    assert p.partial_fill_policy.value == "accept_partial" and p.unfilled_timeout_seconds == 90.0
    assert p.cycle_cancel_threshold >= 10**6
    i2 = Intent(strategy_id="cross_market_divergence", exchange_id="1", side="buy", action="open",
                strategy_type="directional", signal_strength=1.0, price=0.5, metadata={})
    assert _fill_policy_for(i2).partial_fill_policy.value == "chase"     # taker path unchanged


def test_orchestrator_helpers_respect_ownership(reg):
    from sig.engine.orchestrator import _owned_market, _ownership_allows
    reg.claim("fair_band", ["1"])
    assert _owned_market("1") and not _owned_market("2")         # live lease (fixture beat)
    assert not _ownership_allows("cross_market_divergence", "1") and _ownership_allows("cross_market_divergence", "2")


# ───────────────────────── 6. claim races (time-of-check / time-of-use) ─────────────────────────
@pytest.mark.parametrize("foreign", ["order", "position"])
def test_foreign_activity_during_claim_releases_market_untouched(reg, tmp_path, foreign):
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    read = v.open_view()                                                  # pre-claim read: market 1 is clean
    # ... another strategy acts between that read and the claim
    if foreign == "order":
        v.orders["cross1"] = {"order_id": "cross1", "exchange_id": "1", "side": "yes", "action": "buy",
                              "price": 0.5, "quantity": 100.0}
    else:
        v.pos["1"] = 250.0
    tr.select_markets(["1", "2"], {}, read)                              # claims both on the stale read
    rows = {ex: {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}
            for ex in ("1", "2")}
    out1 = run(tr.tick(rows, {}, read, 1e6))                              # same stale read: judge nothing, quote nothing
    assert out1["placed"] == 0 and v.writes == []
    clk.t += 60
    out2 = run(tr.tick(rows, dict(v.pos), v.open_view(), 1e6))            # first read after the claim
    assert out2.get("released_foreign") == ["1"]
    assert reg.owner_of("1") is None and reg.owner_of("2") == SID
    assert "cross1" in v.orders or foreign == "position"                  # foreign order never cancelled
    assert all(w[2] != "1" for w in v.writes if w[0] == "place")         # never traded market 1
    assert v.pos.get("1", 0.0) == (250.0 if foreign == "position" else 0.0)
    assert any(w[2] == "2" for w in v.writes if w[0] == "place")         # clean market 2 now quoted


def test_failed_read_does_not_clear_probation(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    tr.select_markets(["1"], {}, [])
    run(tr.tick({}, {}, None, 1e6))                                       # read failed
    run(tr.tick({}, {}, None, 1e6))
    assert "1" in tr._probation                                           # still unverified


# ───────────────────────── 7. real broker failure modes, restart, degenerate configs ─────────────────────────
class FlakyTransport:
    """Real-SigBroker transport that returns 429 / 502 / 400 / 200 from a script, cycling."""
    def __init__(self, script):
        self.script, self.i, self.calls = list(script), 0, []

    async def __call__(self, method, path, body, params):
        self.calls.append((method, path, body))
        code = self.script[self.i % len(self.script)]
        self.i += 1
        if method == "GET":
            return HttpResult(200, json.dumps({"data": []}))
        if code == 200:
            return HttpResult(200, json.dumps({"id": f"v{self.i}", "status": "open", **(body or {})}))
        return HttpResult(code, json.dumps({"error": "x"}))


@pytest.mark.parametrize("script", [[429], [502], [400], [200, 429, 502, 400]])
def test_trader_through_real_broker_failure_modes(reg, tmp_path, script):
    from sig.broker import RetryConfig

    async def _sleep(s):
        return None
    t = FlakyTransport(script)
    b = SigBroker(transport=t, gate=WriteGate(writes_per_min=1000, window_s=60), sleep=_sleep)
    tr = FairBandLive(broker=b, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S"),
                                                                      max_writes_per_min=50), clock=Clock())
    tr.select_markets(["1", "2"], {}, [])
    run(tr.tick({}, {}, [], 1e6))
    rows = {ex: {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}
            for ex in ("1", "2")}
    out = run(tr.tick(rows, {}, [], 1e6))
    posts = [c for c in t.calls if c[0] == "POST"]
    assert posts and all(c[2]["exchangeId"] in ("1", "2") for c in posts)
    assert all(c[2]["action"] == "buy" for c in posts)
    keys = {c[2]["idempotencyKey"] for c in posts}
    # retries reuse the SAME key: distinct keys == distinct logical orders (<= 2 markets x 2 sides)
    assert len(keys) <= 4
    if script == [400]:
        assert out["placed"] == 0 and tr._pending == []                   # rejected: nothing believed pending


def test_restart_mid_run_keeps_invariants(reg, tmp_path):
    rng = random.Random(11)
    clk = Clock()
    cfg = FairBandLiveConfig(budget_usd=2000.0, market_cap_usd=700.0, quote_usd=250.0, n_markets=4,
                             max_writes_per_min=8, stop_file=str(tmp_path / "S"))
    v = Venue(reg, rng)
    tr = FairBandLive(broker=v, ownership=reg, cfg=cfg, clock=clk)
    owned = claim(tr, [str(i) for i in range(8)], v)
    walk = Walk(owned, rng)
    for step in range(200):
        if step == 100:                                                   # crash + restart
            reg2 = MarketOwnership(reg._path)
            own_mod.set_registry(reg2)
            v.reg = reg2
            tr = FairBandLive(broker=v, ownership=reg2, cfg=cfg, clock=clk)
            assert reg2.owned_by(SID) == set(owned)
            assert tr.select_markets([str(i) for i in range(8)], dict(v.pos), v.open_view()) == sorted(owned)
        clk.t += 60
        rows = walk.rows()
        v.mids, v.cfg = {ex: (r["best_bid"] + r["best_ask"]) / 2 for ex, r in rows.items()}, cfg
        run(tr.tick(rows, dict(v.pos), v.open_view(), 1e6))
        assert v.violations == [], (step, v.violations[:3])
        keys = [(o["exchange_id"], o["side"]) for o in v.orders.values()]
        assert len(keys) == len(set(keys))
        v.random_fills()


@pytest.mark.parametrize("kw", [{"n_markets": 0}, {"market_cap_usd": 0.0}, {"budget_usd": 50.0, "quote_usd": 500.0}])
def test_degenerate_configs_place_nothing_or_tiny(reg, tmp_path, kw):
    v = Venue(reg, random.Random(0))
    cfg = FairBandLiveConfig(stop_file=str(tmp_path / "S"), **kw)
    tr = FairBandLive(broker=v, ownership=reg, cfg=cfg, clock=Clock())
    claim(tr, ["1", "2"], v)
    rows = {ex: {"fair": 0.6, "best_bid": 0.55, "best_ask": 0.56, "bid_size": 1, "ask_size": 1, "age_s": 1}
            for ex in reg.owned_by(SID)}
    v.mids, v.cfg = {ex: 0.555 for ex in rows}, cfg
    run(tr.tick(rows, {}, v.open_view(), 1e6))
    assert v.violations == []
    if kw.get("n_markets") == 0 or kw.get("market_cap_usd") == 0.0:
        assert v.orders == {}
    else:
        assert sum(max(o["price"], 0.555 if o["side"] == "yes" else 0.445) * o["quantity"]
                   for o in v.orders.values()) <= 50.0 + 1


# ───────────────────────── 8. hardening round (venue conventions, halts, persistence) ─────────────────────────
def test_venue_pnl_matches_live_conventions():
    from types import SimpleNamespace as NS
    from sig.engine.fair_band_live import venue_pnl
    # live: NO holding = side short, avg_entry_price is the NO price (e.g. -5151 @ 0.8626 with YES mid 0.1125)
    snaps = [NS(contract_id="1", side="short", qty=1000.0, avg_entry_price=0.86),
             NS(contract_id="2", side="long", qty=500.0, avg_entry_price=0.40),
             NS(contract_id="9", side="long", qty=999.0, avg_entry_price=0.10)]          # not owned: ignored
    books = {"1": {"best_bid": 0.19, "best_ask": 0.20}, "2": {"best_bid": 0.30, "best_ask": 0.31},
             "9": {"best_bid": 0.9, "best_ask": 0.91}}
    v = venue_pnl(snaps, {"1", "2"}, books)                      # liquidation: NO at 1-ask, YES at bid
    assert abs(v - (1000 * ((1 - 0.20) - 0.86) + 500 * (0.30 - 0.40))) < 1e-9          # -60 - 50
    assert venue_pnl(snaps, {"1"}, {}) == 1000 * (0.0 - 0.86)            # no book -> worth 0


@pytest.mark.parametrize("which", ["fills", "venue", "halted"])
def test_each_kill_source_winds_down(tmp_path, which):
    tr = FairBandLive(broker=None, ownership=MarketOwnership(None), cfg=FairBandLiveConfig(
        stop_file=str(tmp_path / "S"), max_loss_usd=500.0))
    tr.check_kill(-600.0 if which == "fills" else 0.0, -600.0 if which == "venue" else 0.0,
                  orchestrator_halted=(which == "halted"))
    assert tr.winding_down and tr.unused_budget() == 0.0
    tr2 = FairBandLive(broker=None, ownership=MarketOwnership(None), cfg=tr.cfg)
    tr2.check_kill(-499.0, -499.0, orchestrator_halted=False)
    assert not tr2.winding_down


def test_price_band_blocks_extreme_legs(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=Clock())
    claim(tr, ["1"], v)
    rows = {"1": {"fair": 0.985, "best_bid": 0.98, "best_ask": 0.985, "bid_size": 1, "ask_size": 1, "age_s": 1}}
    run(tr.tick(rows, {}, v.open_view(), 1e6))
    assert all(0.02 <= o["price"] <= 0.98 for o in v.orders.values())


def test_claim_that_cannot_be_persisted_is_undone(tmp_path):
    r = MarketOwnership(str(tmp_path / "own.json"))

    def boom():
        raise OSError("disk full")
    r._save = boom
    with pytest.raises(OSError):
        r.claim("fair_band", ["1"])
    assert r.owner_of("1") is None and r.claimed_at("1") is None



# ───────────────────────── 9. review round: capital, contamination, dead markets, registry ─────────────────────────
def _book(fair=0.6, bid=0.55, ask=0.56):
    return {"fair": fair, "best_bid": bid, "best_ask": ask, "bid_size": 1, "ask_size": 1, "age_s": 1}


def test_carve_out_keeps_cash_behind_resting_orders(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    cfg = FairBandLiveConfig(stop_file=str(tmp_path / "S"), budget_usd=10000.0)
    tr = FairBandLive(broker=v, ownership=reg, cfg=cfg, clock=Clock())
    claim(tr, ["1"], v)
    run(tr.tick({"1": _book()}, {}, v.open_view(), 1e6))
    assert v.orders and tr.unused_budget() == 10000.0          # resting quotes do NOT shrink the carve-out
    held = {"1": 1000.0}                                         # a fill: $555 of position at mid
    run(tr.tick({"1": _book()}, held, v.open_view(), 1e6))
    assert abs(tr.unused_budget() - (10000.0 - 555.0)) < 1.0
    tr.halt("test")
    resting = tr._resting_cost
    assert resting > 0 and tr.unused_budget() == resting         # winding down: keep it until cancelled


def test_cash_check_nets_out_our_own_resting_orders(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S"),
                                                                      quote_usd=500.0, max_writes_per_min=50),
                      clock=Clock())
    claim(tr, ["1", "2"], v)
    run(tr.tick({"1": _book(), "2": _book()}, {}, v.open_view(), 600.0))   # $600 free cash, venue reserves none
    spent = sum(o["price"] * o["quantity"] for o in v.orders.values())
    assert spent <= 600.0 + 1e-6, spent


def test_unregistered_order_after_probation_is_cancelled(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    claim(tr, ["1"], v)
    run(tr.tick({"1": _book()}, {}, v.open_view(), 1e6))
    ours = set(v.orders)
    assert ours
    v.orders["alien"] = {"order_id": "alien", "exchange_id": "1", "side": "no", "action": "buy", "price": 0.3,
                         "quantity": 50.0}                       # e.g. a pre-claim order the venue view lagged on
    clk.t += 60
    out = run(tr.tick({"1": _book()}, {}, v.open_view(), 1e6))
    assert out.get("stray_cancelled") == ["alien"] and "alien" not in v.orders   # nothing unmanaged may rest
    assert ours <= set(v.orders) and reg.owner_of("1") == SID            # our quotes and the market are kept


def test_dead_market_is_released_when_flat(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    tr.dead_after_ticks = 3
    claim(tr, ["1"], v)
    for _ in range(4):
        clk.t += 60
        run(tr.tick({"1": {**_book(), "fair": None}}, {}, v.open_view(), 1e6))
    assert reg.owner_of("1") is None


def test_dead_market_with_inventory_stays_owned(reg, tmp_path):
    v = Venue(reg, random.Random(0))
    clk = Clock()
    tr = FairBandLive(broker=v, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S")), clock=clk)
    tr.dead_after_ticks = 2
    claim(tr, ["1"], v)
    run(tr.tick({"1": _book()}, {}, v.open_view(), 1e6))       # probation cleared while flat
    for _ in range(4):
        clk.t += 60
        run(tr.tick({"1": {**_book(), "fair": None}}, {"1": 300.0}, v.open_view(), 1e6))
    assert reg.owner_of("1") == SID


def test_corrupt_registry_fails_closed_and_backup_recovers(tmp_path):
    p = tmp_path / "own.json"
    r = MarketOwnership(str(p))
    r.claim("fair_band", ["1"])
    p.write_text("{garbage")                                     # main copy corrupted
    r2 = MarketOwnership(str(p))
    assert not r2.corrupt and r2.owner_of("1") == "fair_band"   # recovered from .bak
    (tmp_path / "own.json.bak").write_text("{garbage")
    r3 = MarketOwnership(str(p))
    assert r3.corrupt
    assert not r3.allows("cross_market_divergence", "7")        # fail CLOSED for everyone
    assert r3.allows_cancel(None, "anything")                   # cancels still allowed


def test_send_time_recheck_closes_the_gate_race(reg):
    """An order passes the pre-queue ownership check, waits in the write gate, the market is claimed meanwhile:
    nothing may be sent."""
    class ClaimingGate(WriteGate):
        async def acquire(self, priority=False):
            reg.claim("fair_band", ["1"])                       # the claim lands while we wait in the queue
            return await super().acquire(priority=priority)

    async def _sleep(s):
        return None
    t = Transport()
    b = SigBroker(transport=t, gate=ClaimingGate(writes_per_min=1000, window_s=60), sleep=_sleep)
    r = run(b.place_order(strategy_id="cross_market_divergence", market_id="1", exchange_id="1", side="buy",
                          action="buy", quantity=10, price=0.5, outcome="yes"))
    assert r.status == "rejected" and r.raw.get("at") == "send" and t.calls == []


def test_every_fair_band_placement_has_a_unique_idempotency_key(reg, tmp_path):
    async def _sleep(s):
        return None
    t = Transport()
    b = SigBroker(transport=t, gate=WriteGate(writes_per_min=1000, window_s=60), sleep=_sleep)
    clk = Clock()
    tr = FairBandLive(broker=b, ownership=reg, cfg=FairBandLiveConfig(stop_file=str(tmp_path / "S"),
                                                                      max_writes_per_min=50), clock=clk)
    tr.select_markets(["1"], {}, [])
    run(tr.tick({}, {}, [], 1e6))
    for _ in range(3):                                           # same book every time -> identical payloads
        clk.t += 700                                             # pending forgotten (ttl) -> re-placed
        for _ in range(3):
            run(tr.tick({"1": _book()}, {}, [], 1e6))
    keys = [c[2]["idempotencyKey"] for c in t.calls if c[0] == "POST"]
    assert len(keys) >= 4 and len(set(keys)) == len(keys)


def test_positions_fresh_read_bypasses_cache():
    from types import SimpleNamespace as NS
    calls = {"n": 0}

    class Read:
        def portfolio_positions(self, *a):
            calls["n"] += 1
            return {"data": []}
    b = SigBroker.__new__(SigBroker)
    b._tournament_slug = None
    b._pos_raw_ttl_s = 20.0
    b._read = lambda: Read()
    run(b.get_positions())
    run(b.get_positions())
    assert calls["n"] == 1                                       # cached
    run(b.get_positions(fresh=True))
    assert calls["n"] == 2                                       # fresh bypasses
