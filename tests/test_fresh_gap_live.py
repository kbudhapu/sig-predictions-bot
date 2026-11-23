"""BREAK TESTS for the fresh-dislocation reversion pilot (sig/engine/fresh_gap_live.py).

A fake venue with partial fills, venue-side expiry, a lagging open-orders view, rejects, failed reads and a FOREIGN
strategy that keeps trying to trade the pilot's markets — enforced through the REAL ownership registry. Invariants
checked on every tick of a randomized run:
  - every pilot write is in a market it owns; every foreign write in an owned market is refused
  - the pilot never sells more than it holds (no short, never flips the side it trades)
  - committed (positions + open/pending buys) never exceeds the budget; one trade never exceeds trade_usd
  - never two live pilot orders in one market (no stacking); write rate <= cap
  - wind-down ends flat with every market released
"""
import asyncio
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sig.broker import OrderResult  # noqa: E402
from sig.engine.fresh_gap import FreshGapConfig  # noqa: E402
from sig.engine.fresh_gap_live import SID, FreshGapLive, FreshGapLiveConfig, fills_pnl  # noqa: E402
from sig.engine.ownership import MarketOwnership  # noqa: E402


_LOOP = asyncio.new_event_loop()        # one private loop: asyncio.run() per tick is slow/fragile next to pytest-asyncio


def run(c):
    return _LOOP.run_until_complete(c)


class Clock:
    def __init__(self):
        self.t = 2_000_000.0

    def __call__(self):
        return self.t


class Venue:
    def __init__(self, reg, rng, clock, lag=False, reject_p=0.0, fill_p=0.6):
        self.reg, self.rng, self.clock, self.lag, self.reject_p, self.fill_p = reg, rng, clock, lag, reject_p, fill_p
        self.orders, self.pos, self.n, self.lagged = {}, {}, 0, set()
        self.writes, self.refused, self.violations = [], [], []
        self.book = {}                                   # ex -> (bid, ask)
        self.fills = []

    async def place_order(self, *, strategy_id, market_id, exchange_id, side, action, quantity, price, outcome=None,
                          expiration_s=None, **_kw):
        ex = str(exchange_id)
        if not self.reg.allows(strategy_id, ex):
            self.refused.append((strategy_id, ex))
            return OrderResult(idempotency_key="", status="rejected", raw={"reason": "owned"})
        self.writes.append((self.clock(), strategy_id, "place", ex, action, outcome, int(quantity), float(price)))
        if strategy_id == SID and self.reg.owner_of(ex) != SID:
            self.violations.append(("write_outside_owned", ex))
        if strategy_id == SID and action == "sell":
            held = self.pos.get(ex, 0.0) if outcome == "yes" else -self.pos.get(ex, 0.0)
            pend_sells = sum(o["quantity"] for o in self.orders.values() if o["exchange_id"] == ex and o["action"] == "sell"
                             and o["exp"] > self.clock())                       # expired = gone (as on SIG)
            if quantity + pend_sells > held + 1e-6:
                self.violations.append(("oversell", ex, quantity, held))
        if strategy_id == SID and any(o["exchange_id"] == ex and self.reg.order_owner(o["order_id"]) == SID
                                      and o["exp"] > self.clock() for o in self.orders.values()):  # expired = gone
            self.violations.append(("stacked", ex))
        if self.rng.random() < self.reject_p:
            return OrderResult(idempotency_key="", status="rejected", raw={})
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = {"order_id": oid, "exchange_id": ex, "side": outcome, "action": action,
                            "price": float(price), "quantity": float(quantity),
                            "exp": self.clock() + float(expiration_s or 1e9)}
        self.reg.note_order(strategy_id, oid, ex)
        if self.lag:
            self.lagged.add(oid)
        return OrderResult(idempotency_key="", status="open", order_id=oid)

    async def cancel_order(self, order_id, *, strategy_id=None):
        if not self.reg.allows_cancel(strategy_id, order_id):
            return OrderResult(idempotency_key="", status="rejected")
        self.writes.append((self.clock(), strategy_id, "cancel", order_id))
        self.orders.pop(order_id, None)
        return OrderResult(idempotency_key="", status="canceled", order_id=order_id)

    async def get_open_orders_all(self):
        return self.open_view()

    def open_view(self):
        v = [dict(o) for oid, o in self.orders.items() if oid not in self.lagged]
        self.lagged.clear()
        return v

    def step(self):
        """Fill marketable orders (partially, randomly) and expire old ones."""
        now = self.clock()
        for oid, o in list(self.orders.items()):
            if now >= o["exp"]:
                del self.orders[oid]
                continue
            if self.rng.random() > self.fill_p:
                continue
            q = o["quantity"] if self.rng.random() < 0.6 else max(1.0, float(int(o["quantity"] / 2)))
            yes_delta = q if (o["side"] == "yes") == (o["action"] == "buy") else -q
            self.pos[o["exchange_id"]] = self.pos.get(o["exchange_id"], 0.0) + yes_delta
            self.fills.append({"exchange_id": o["exchange_id"], "outcome": o["side"], "price": o["price"],
                               "quantity": q, "signed_qty": yes_delta, "strategy": self.reg.order_owner(oid),
                               "order_id": oid})
            o["quantity"] -= q
            if o["quantity"] < 1:
                del self.orders[oid]


def make(tmp_path, seed=1, lag=False, reject_p=0.0, **cfg_kw):
    reg = MarketOwnership(str(tmp_path / "own.json"))
    clk = Clock()
    v = Venue(reg, random.Random(seed), clk, lag=lag, reject_p=reject_p)
    cfg = FreshGapLiveConfig(stop_file=str(tmp_path / "STOP"), state_path=str(tmp_path / "trades.json"), **cfg_kw)
    tr = FreshGapLive(broker=v, ownership=reg, cfg=cfg, clock=clk)
    return reg, clk, v, tr


def rows_for(v, sharps, sizes=5000.0):
    return {ex: {"sharp": sharps[ex], "best_bid": b, "best_ask": a, "bid_size": sizes, "ask_size": sizes, "age_s": 1.0}
            for ex, (b, a) in v.book.items()}


# ---------------------------------------------------------------- unit-level behaviour
def test_claims_on_probation_then_enters_then_exits_then_releases(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}                        # SIG moved 0.88 -> 0.84, away from Poly 0.90
    past = {"1": (0.88, 0.90)}
    rows = rows_for(v, {"1": 0.90})
    out = run(tr.tick(rows, past, dict(v.pos), v.open_view(), 1e6))
    assert out.get("claimed") == ["1"] and out["placed"] == 0          # probation: no order on the claim tick
    clk.t += 60
    out = run(tr.tick(rows, past, dict(v.pos), v.open_view(), 1e6))
    assert out["placed"] == 1 and v.writes[-1][4:6] == ("buy", "yes")
    v.fill_p = 1.0
    v.step()
    assert v.pos["1"] > 0
    v.book["1"] = (0.875, 0.885)                          # snapped back: gap 2c <= half of 6c -> exit
    for _ in range(12):                                   # release waits 3 clean reads after the last fill
        clk.t += 60
        run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
    assert abs(v.pos.get("1", 0.0)) < 1 and reg.owner_of("1") is None and tr.closed[-1]["reason"] == "closed"
    assert v.violations == []


def test_never_claims_held_owned_excluded_or_busy_markets(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {ex: (0.835, 0.845) for ex in ("held", "owned", "cross", "busy", "ok")}
    past = {ex: (0.88, 0.90) for ex in v.book}
    v.pos["held"] = 100.0
    reg.claim("fair_band", ["owned"])
    tr.exclude = {"cross"}
    v.orders["x"] = {"order_id": "x", "exchange_id": "busy", "side": "yes", "action": "buy", "price": 0.5,
                     "quantity": 1.0, "exp": 1e18}
    out = run(tr.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, dict(v.pos), v.open_view(), 1e6))
    assert out.get("claimed") == ["ok"]


def test_probation_releases_when_something_foreign_appears(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
    v.pos["1"] = 50.0                                     # a foreign fill landed between the read and the claim
    clk.t += 60
    out = run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
    assert out.get("released_foreign") == ["1"] and out["placed"] == 0 and reg.owner_of("1") is None


def test_no_entry_on_moves_toward_poly_and_respects_cooldown(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.845, 0.855)}                        # 0.80 -> 0.85 TOWARD Poly 0.90
    out = run(tr.tick(rows_for(v, {"1": 0.90}), {"1": (0.80, 0.90)}, {}, v.open_view(), 1e6))
    assert "claimed" not in out
    tr._cooldown["2"] = clk.t
    v.book = {"2": (0.835, 0.845)}
    out = run(tr.tick(rows_for(v, {"2": 0.90}), {"2": (0.88, 0.90)}, {}, v.open_view(), 1e6))
    assert "claimed" not in out


def test_failed_open_orders_read_skips_the_tick(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    out = run(tr.tick(rows_for(v, {"1": 0.90}), {"1": (0.88, 0.90)}, {}, None, 1e6))
    assert out.get("skipped") == "open_orders_read_failed" and not v.writes and reg.owner_of("1") is None


def test_budget_trade_size_and_max_open(tmp_path):
    reg, clk, v, tr = make(tmp_path, budget_usd=6000.0, trade_usd=2500.0, max_open=5)
    v.book = {str(i): (0.835, 0.845) for i in range(10)}
    past = {ex: (0.88, 0.90) for ex in v.book}
    out = run(tr.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, {}, v.open_view(), 1e6))
    assert len(out["claimed"]) == 2                       # 2 x $2.5k fits $6k; a third would not
    clk.t += 60
    run(tr.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, dict(v.pos), v.open_view(), 1e6))
    usd = [w[6] * w[7] for w in v.writes if w[2] == "place"]
    assert usd and max(usd) <= 2500.0 + 1


def test_entry_capped_by_cash_and_depth(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    rows = rows_for(v, {"1": 0.90}, sizes=300.0)
    run(tr.tick(rows, past, {}, v.open_view(), 1e6))
    clk.t += 60
    run(tr.tick(rows, past, {}, v.open_view(), 1e6))
    assert v.writes[-1][6] <= 300                         # never more than the displayed touch
    reg2, clk2, v2, tr2 = make(tmp_path / "b")
    v2.book = {"1": (0.835, 0.845)}
    run(tr2.tick(rows_for(v2, {"1": 0.90}), past, {}, v2.open_view(), 400.0))
    clk2.t += 60
    run(tr2.tick(rows_for(v2, {"1": 0.90}), past, {}, v2.open_view(), 400.0))
    assert v2.writes[-1][6] * v2.writes[-1][7] <= 400.0 + 1   # never more than free cash


def test_paused_on_orchestrator_halt_then_resumes(tmp_path):
    reg, clk, v, tr = make(tmp_path, resume_after_clear_ticks=2)
    tr.check_kill(0.0, orchestrator_halted=True)
    v.book = {"1": (0.835, 0.845)}
    out = run(tr.tick(rows_for(v, {"1": 0.90}), {"1": (0.88, 0.90)}, {}, v.open_view(), 1e6))
    assert "claimed" not in out and not v.writes
    tr.check_kill(0.0)
    tr.check_kill(0.0)
    assert not tr.paused


def test_wind_down_flattens_and_releases_everything(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {str(i): (0.835, 0.845) for i in range(4)}
    past = {ex: (0.88, 0.90) for ex in v.book}
    v.fill_p = 1.0
    for _ in range(3):
        run(tr.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert any(abs(q) >= 1 for q in v.pos.values())
    open(tr.cfg.stop_file, "w").close()
    tr.check_kill(0.0)
    for _ in range(12):
        run(tr.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert all(abs(q) < 1 for q in v.pos.values()) and not reg.owned_by(SID) and not tr.trades
    assert v.violations == []


def test_loss_limit_triggers_wind_down(tmp_path):
    reg, clk, v, tr = make(tmp_path, max_loss_usd=100.0)
    tr.check_kill(-150.0)
    assert tr.winding_down and tr.halt_reason.startswith("loss_limit")


def test_restart_reloads_trades_and_exits_orphans(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845), "2": (0.835, 0.845)}
    past = {ex: (0.88, 0.90) for ex in v.book}
    v.fill_p = 1.0
    for _ in range(3):
        run(tr.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert os.path.exists(tr.cfg.state_path)
    # restart: lose the record for market "2" (simulates a crash before the save)
    d = json.load(open(tr.cfg.state_path))
    d["trades"].pop("2", None)
    json.dump(d, open(tr.cfg.state_path, "w"))
    tr2 = FreshGapLive(broker=v, ownership=reg, cfg=tr.cfg, clock=clk)
    tr2.load()
    assert tr2.trades["2"]["phase"] == "exiting" and "1" in tr2.trades
    for _ in range(8):
        run(tr2.tick(rows_for(v, {ex: 0.90 for ex in v.book}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert abs(v.pos.get("2", 0.0)) < 1 and reg.owner_of("2") is None


def test_clean_slate_forgets_expired_and_cancels_live(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    reg.claim(SID, ["1"])
    reg.note_order(SID, "dead", "1")
    v.orders["live"] = {"order_id": "live", "exchange_id": "1", "side": "yes", "action": "buy", "price": 0.5,
                        "quantity": 10.0, "exp": 1e18}
    reg.note_order(SID, "live", "1")
    assert run(tr.cancel_open_registered()) == 1 and reg.orders_of(SID) == []


def test_fills_pnl_handles_buys_and_sells():
    fills = [{"exchange_id": "1", "outcome": "yes", "price": 0.845, "quantity": 100, "signed_qty": 100},
             {"exchange_id": "1", "outcome": "yes", "price": 0.875, "quantity": 100, "signed_qty": -100},
             {"exchange_id": "2", "outcome": "no", "price": 0.15, "quantity": 100, "signed_qty": -100}]
    books = {"2": {"best_bid": 0.84, "best_ask": 0.86}}
    # +3.00 round trip on 1; NO on 2 bought at 0.15, worth 1-0.86=0.14 -> -1.00
    assert abs(fills_pnl(fills, books) - 2.0) < 1e-9


# ---------------------------------------------------------------- randomized stress
@pytest.mark.parametrize("seed,lag,reject_p", [(1, False, 0.0), (2, True, 0.0), (3, True, 0.2), (4, False, 0.4),
                                                (5, True, 0.1), (6, False, 0.0), (7, True, 0.3), (8, False, 0.1)])
def test_randomized_stress_keeps_every_invariant(tmp_path, seed, lag, reject_p):
    rng = random.Random(100 + seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=lag, reject_p=reject_p, budget_usd=8000.0, trade_usd=2000.0,
                           max_open=4, max_writes_per_min=6)
    exs = [str(i) for i in range(12)]
    fair = {ex: rng.choice([0.85, 0.90, 0.15, 0.10, 0.6]) for ex in exs}
    mid24 = {ex: fair[ex] - 0.02 if fair[ex] > 0.5 else fair[ex] + 0.02 for ex in exs}
    mids = dict(mid24)
    foreign_tries = 0
    for k in range(300):
        clk.t += 30
        for ex in exs:                                    # SIG drifts; sometimes jumps away from fair, then reverts
            r = rng.random()
            if r < 0.04:
                mids[ex] += (-0.04 if fair[ex] > mids[ex] else 0.04)
            elif r < 0.25:
                mids[ex] += 0.3 * (mid24[ex] - mids[ex])
            mids[ex] = min(max(round(mids[ex] * 200) / 200, 0.05), 0.95)
            v.book[ex] = (round(mids[ex] - 0.005, 3), round(mids[ex] + 0.005, 3))
        past = {ex: (mid24[ex], fair[ex]) for ex in exs}
        rows = rows_for(v, fair, sizes=rng.choice([200.0, 1000.0, 5000.0]))
        # a FOREIGN strategy keeps trying to trade every market (must be refused wherever the pilot owns it)
        owned_now = sorted(reg.owned_by(SID))
        targets = owned_now[:2] + (rng.sample(exs, 1) if rng.random() < 0.1 else [])   # hammer OUR markets
        for ex in targets:
            foreign_tries += 1
            res = run(v.place_order(strategy_id="cross_market_divergence", market_id=ex, exchange_id=ex, side="buy",
                                    action="buy", quantity=5, price=0.5, outcome="yes", expiration_s=60))
            if reg.owner_of(ex) == SID:
                assert getattr(res, "status", "") == "rejected"
        view = None if rng.random() < 0.05 else v.open_view()
        out = run(tr.tick(rows, past, dict(v.pos), view, rng.choice([1e6, 3000.0, 500.0])))
        # invariants every tick
        assert out.get("committed", 0.0) <= tr.cfg.budget_usd + tr.cfg.trade_usd + 1
        recent = [w for w in v.writes if w[1] == SID and clk.t - w[0] < 60]
        assert len(recent) <= tr.cfg.max_writes_per_min + 1
        for ex in exs:
            if reg.owner_of(ex) == SID:
                t = tr.trades.get(ex)
                if t and t.get("side"):
                    held = v.pos.get(ex, 0.0) if t["side"] == "yes" else -v.pos.get(ex, 0.0)
                    assert held > -1, ("side flipped", ex, held)
        v.step()
        if k == 220:
            open(tr.cfg.stop_file, "w").close()
            tr.check_kill(0.0)
    v.reject_p = 0.0
    for _ in range(40):                                   # drain the wind-down
        clk.t += 60
        run(tr.tick(rows_for(v, fair), {ex: (mid24[ex], fair[ex]) for ex in exs}, dict(v.pos), v.open_view(), 1e6))
        v.step()
    assert v.violations == [], v.violations[:5]
    pilot_mkts = {w[3] for w in v.writes if w[1] == SID and w[2] == "place"}
    assert pilot_mkts, "the stress must actually trade"
    assert all(abs(v.pos.get(ex, 0.0)) < 1 for ex in pilot_mkts if not any(
        f["exchange_id"] == ex and f["strategy"] != SID for f in v.fills)), "wind-down must end flat"
    assert not reg.owned_by(SID) and not tr.trades
    assert foreign_tries > 0


# ---------------------------------------------------------------- loop wiring (main_sig._fresh_gap_live_loop)
def _loop_env(tmp_path, monkeypatch, cfg_extra, ticks=4, pre_owned=False):
    import main_sig
    from sig.engine import ownership as own_mod
    reg = MarketOwnership(str(tmp_path / "own.json"))
    own_mod.set_registry(reg)
    if pre_owned:
        reg.claim(SID, ["9"])
    clk = Clock()
    v = Venue(reg, random.Random(1), clk, fill_p=1.0)
    v.book = {"1": (0.835, 0.845)}

    async def gooa():
        return v.open_view()

    async def gpos(fresh=True):
        from types import SimpleNamespace
        return [SimpleNamespace(contract_id=ex, qty=abs(q), side="long" if q > 0 else "short", avg_entry_price=0.5)
                for ex, q in v.pos.items() if abs(q) >= 1]

    async def gacct():
        from types import SimpleNamespace
        return SimpleNamespace(buying_power=1e6, equity=1e6)
    v.get_open_orders_all, v.get_positions, v.get_account = gooa, gpos, gacct

    class Store:
        _db_path = str(tmp_path / "none.db")

        async def _read(self, sql, params):
            return [{"exchange_id": "1", "ts": params[0], "sharp": 0.90, "sig_mid": 0.88}]

    class XM:
        @property
        def _sig_prices(self):
            from datetime import datetime, timezone
            return {ex: {"best_bid": b, "best_ask": a, "bid_size": 5000, "ask_size": 5000,
                         "updated_at": datetime.now(timezone.utc).isoformat()} for ex, (b, a) in v.book.items()}

        def active_sig_exchange_ids(self):
            return list(v.book)

        def aligned_benchmark(self, ex):
            return 0.90

    class Ledger:
        external_reserves = {}

    class Orch:
        ledger = Ledger()
        is_halted = False

    stop = asyncio.Event()
    n = {"k": 0}
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        n["k"] += 1
        v.step()
        if n["k"] > ticks:
            stop.set()
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    monkeypatch.chdir(tmp_path)
    cfg = {"cross_market": cfg_extra}
    orch = Orch()
    asyncio.run(main_sig._fresh_gap_live_loop(store=Store(), broker=v, orch=orch, xm_provider=XM(), cfg_raw=cfg,
                                              stop_event=stop, exclude=set()))
    own_mod.set_registry(None)
    return v, reg, orch


def test_loop_off_by_default_places_nothing(tmp_path, monkeypatch):
    v, reg, orch = _loop_env(tmp_path, monkeypatch, {})
    assert not v.writes and SID not in orch.ledger.external_reserves


def test_loop_live_registers_carve_out_and_trades(tmp_path, monkeypatch):
    v, reg, orch = _loop_env(tmp_path, monkeypatch, {"fresh_gap_live_mode": "live"}, ticks=4)
    assert SID in orch.ledger.external_reserves
    assert any(w[1] == SID and w[2] == "place" and w[4] == "buy" for w in v.writes)
    assert v.violations == []


def test_loop_mode_off_with_leftover_markets_winds_down(tmp_path, monkeypatch):
    v, reg, orch = _loop_env(tmp_path, monkeypatch, {}, ticks=6, pre_owned=True)
    assert not any(w[1] == SID and w[2] == "place" and w[4] == "buy" for w in v.writes)   # never enters
    assert reg.owner_of("9") is None                                                     # flat orphan released


# ================================================================ ADVERSARIAL ROUND (2026-10-09, before go-live)
def test_A1_loop_keeps_its_ownership_lease_alive(tmp_path, monkeypatch):
    """Without a heartbeat the lease lapses after 600s and the orchestrator's exit:* strategies may trade the pilot's
    markets (sell its inventory). The loop must beat every tick."""
    v, reg, orch = _loop_env(tmp_path, monkeypatch, {"fresh_gap_live_mode": "live"}, ticks=3)
    assert reg.lease_ok(SID)


def test_A2_pilot_is_an_isolated_strategy_at_the_broker():
    from sig.engine.ownership import ISOLATED_STRATEGIES
    assert SID in ISOLATED_STRATEGIES
    reg = MarketOwnership(None)
    assert not reg.allows(SID, "unowned-market")            # can never trade outside its own markets


def test_A3_never_spends_cash_other_strategies_have_reserved(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    tr.others_reserved = lambda: 900.0                      # e.g. fair_band carve-out + cross resting maker entries
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    run(tr.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1000.0))
    clk.t += 60
    run(tr.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1000.0))
    spent = sum(w[6] * w[7] for w in v.writes if w[1] == SID and w[2] == "place")
    assert spent <= 1000.0 - 900.0 + 1                     # only the $100 nobody else reserved


def test_A4_restart_does_not_freeze_probation_markets(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    tr._gen = 500                                           # long-running process
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    run(tr.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1e6))
    assert tr.trades["1"]["phase"] == "probation"
    tr2 = FreshGapLive(broker=v, ownership=reg, cfg=tr.cfg, clock=clk)
    tr2.load()                                              # restart: gen counter back to 0
    clk.t += 60
    out = run(tr2.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1e6))
    assert out["placed"] == 1                               # judged on the first read after restart


def test_A5_broker_exception_on_place_never_double_buys(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    real = v.place_order
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        await real(**kw)                                    # the order DID reach the venue...
        raise TimeoutError("response lost")                 # ...but the response was lost
    run(tr.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1e6))
    v.place_order = flaky
    clk.t += 60
    run(tr.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1e6))
    v.lagged = set(v.orders)                                # and the venue view lags one read
    clk.t += 30
    run(tr.tick(rows_for(v, {"1": 0.90}), past, {}, v.open_view(), 1e6))
    assert calls["n"] == 1, "a second buy was sent while the first one was still unknown"
    assert v.violations == []


def test_A6_lagging_positions_never_cause_an_oversell(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    v.fill_p = 1.0
    for _ in range(3):
        run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    v.book["1"] = (0.875, 0.885)                            # exit
    stale = dict(v.pos)                                     # positions read FROZEN at the pre-exit quantity
    for _ in range(10):
        run(tr.tick(rows_for(v, {"1": 0.90}), past, stale, v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert v.violations == [], v.violations
    assert v.pos.get("1", 0.0) > -1                         # never flipped short


def test_A7_loss_stop_also_uses_the_venue_cost_basis(tmp_path):
    reg, clk, v, tr = make(tmp_path, max_loss_usd=100.0)
    tr.check_kill(0.0, venue_pnl_usd=-150.0)                # fills not ingested yet, venue already shows the loss
    assert tr.winding_down


def test_A8_no_entries_near_election_night(tmp_path):
    from datetime import datetime, timezone
    reg, clk, v, tr = make(tmp_path)
    clk.t = datetime(2026, 11, 2, 1, tzinfo=timezone.utc).timestamp()
    v.book = {"1": (0.835, 0.845)}
    out = run(tr.tick(rows_for(v, {"1": 0.90}), {"1": (0.88, 0.90)}, {}, v.open_view(), 1e6))
    assert "claimed" not in out
    assert tr.winding_down                                  # and everything is flattened before the night


def test_B1_never_dumps_into_a_blown_out_book(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    v.fill_p = 1.0
    for _ in range(3):
        run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    held_before = v.pos["1"]
    v.book["1"] = (0.70, 0.92)                              # 22c spread; mid 0.81 -> gap flipped? no: still below
    clk.t += 49 * 3600                                      # timeout -> exiting
    for _ in range(3):
        run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert not any(w[1] == SID and w[4] == "sell" and w[7] <= 0.71 for w in v.writes if w[2] == "place")
    assert v.pos["1"] == held_before                        # waits for a sane book
    v.book["1"] = (0.86, 0.87)
    for _ in range(4):
        run(tr.tick(rows_for(v, {"1": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert abs(v.pos.get("1", 0.0)) < 1                     # then exits


def test_B2_wind_down_write_rate_leaves_room_for_others():
    assert FreshGapLiveConfig().wind_down_writes_per_min <= 12


@pytest.mark.parametrize("seed", list(range(20, 32)))
def test_B3_chaos_stress_all_faults_at_once(tmp_path, seed):
    """Stale position reads, ambiguous placement exceptions, failed reads, rejects, lag, partial fills, expiry,
    a foreign strategy, a STOP mid-run: no oversell, no stacking, no write outside owned markets, budget held,
    wind-down ends flat and released."""
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=rng.random() < 0.5, reject_p=rng.choice([0.0, 0.1, 0.3]),
                           budget_usd=7000.0, trade_usd=2000.0, max_open=4)
    real_place = v.place_order

    async def chaotic_place(**kw):
        res = await real_place(**kw)
        if kw.get("strategy_id") == SID and rng.random() < 0.15:
            raise TimeoutError("response lost")            # reached the venue, response lost
        return res
    v.place_order = chaotic_place
    exs = [str(i) for i in range(10)]
    fair = {ex: rng.choice([0.85, 0.9, 0.12, 0.6]) for ex in exs}
    mid24 = {ex: fair[ex] - 0.02 if fair[ex] > 0.5 else fair[ex] + 0.02 for ex in exs}
    mids = dict(mid24)
    stale_pos = dict(v.pos)
    budget_viol = []
    for k in range(260):
        clk.t += 30
        for ex in exs:
            r = rng.random()
            if r < 0.05:
                mids[ex] += (-0.04 if fair[ex] > mids[ex] else 0.04)
            elif r < 0.3:
                mids[ex] += 0.3 * (mid24[ex] - mids[ex])
            mids[ex] = min(max(round(mids[ex] * 200) / 200, 0.05), 0.95)
            sp = 0.005 if rng.random() < 0.9 else 0.03
            v.book[ex] = (round(mids[ex] - sp, 3), round(mids[ex] + sp, 3))
        if rng.random() < 0.6:
            stale_pos = dict(v.pos)                         # 40% of reads return the PREVIOUS positions
        view = None if rng.random() < 0.05 else v.open_view()
        out = run(tr.tick(rows_for(v, fair, sizes=rng.choice([150.0, 800.0, 4000.0])),
                          {ex: (mid24[ex], fair[ex]) for ex in exs}, stale_pos, view, rng.choice([1e6, 2500.0])))
        # budget at the VENUE: cost of pilot positions + resting pilot buys
        pos_usd = sum(abs(v.pos.get(ex, 0.0)) * (mids[ex] if v.pos.get(ex, 0.0) > 0 else 1 - mids[ex])
                      for ex in exs if reg.owner_of(ex) == SID)
        rest = sum(o["price"] * o["quantity"] for o in v.orders.values()
                   if reg.order_owner(o["order_id"]) == SID and o["action"] == "buy")
        if pos_usd + rest > tr.cfg.budget_usd * 1.05 + 50:
            budget_viol.append((k, round(pos_usd + rest)))
        v.step()
        if k == 190:
            open(tr.cfg.stop_file, "w").close()
            tr.check_kill(0.0)
    v.place_order = real_place
    v.reject_p = 0.0
    for _ in range(60):
        clk.t += 60
        run(tr.tick(rows_for(v, fair), {ex: (mid24[ex], fair[ex]) for ex in exs}, dict(v.pos), v.open_view(), 1e6))
        v.step()
    assert v.violations == [], v.violations[:5]
    assert budget_viol == [], budget_viol[:5]
    assert all(abs(v.pos.get(ex, 0.0)) < 1 for ex in exs), {ex: v.pos.get(ex) for ex in exs if abs(v.pos.get(ex, 0)) >= 1}
    assert not reg.owned_by(SID) and not tr.trades


def test_C1_other_strategies_and_exits_are_locked_out_while_the_lease_is_live():
    from datetime import datetime, timedelta, timezone
    reg = MarketOwnership(None)
    reg.claim(SID, ["1"])
    reg.beat(SID)
    for other in ("cross_market_divergence", "fair_band", "market_making", "exit:1"):
        assert not reg.allows(other, "1"), other
    assert reg.managed_owner("1") == SID
    reg.beat(SID, now=datetime.now(timezone.utc) - timedelta(seconds=reg.LEASE_S + 5))   # pilot died
    assert reg.allows("exit:1", "1") and not reg.allows("cross_market_divergence", "1")  # only risk-reducing exits


def test_C2_others_reserved_sums_other_carve_outs_and_fails_safe():
    import main_sig

    class L:
        external_reserves = {"fresh_gap": lambda: 9999.0, "fair_band": lambda: 300.0, "maker_resting": lambda: 200.0}

    class O:
        ledger = L()
    assert main_sig._others_reserved(O(), "fresh_gap")() == 500.0
    L.external_reserves["broken"] = lambda: 1 / 0
    assert main_sig._others_reserved(O(), "fresh_gap")() == float("inf")
    assert main_sig._others_reserved(None, "fresh_gap")() == 0.0


def test_C3_stale_poly_on_an_owned_market_never_crashes_the_tick(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"1": (0.835, 0.845), "2": (0.835, 0.845)}
    past = {ex: (0.88, 0.90) for ex in v.book}
    v.fill_p = 1.0
    for _ in range(3):
        run(tr.tick(rows_for(v, {"1": 0.90, "2": 0.90}), past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    rows = rows_for(v, {"1": 0.90, "2": 0.90})
    rows["1"]["sharp"] = None                               # Polymarket feed stale for an owned market
    out = run(tr.tick(rows, past, dict(v.pos), v.open_view(), 1e6))
    assert "skipped" not in out
    clk.t += 49 * 3600                                      # still exits on the time stop without a fair
    for _ in range(6):
        run(tr.tick(rows, past, dict(v.pos), v.open_view(), 1e6))
        v.step()
        clk.t += 60
    assert abs(v.pos.get("1", 0.0)) < 1
