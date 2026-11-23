"""BREAK TESTS for the D+R parity live pilot (sig/engine/parity_live.py) on the fake venue of test_fresh_gap_live
(ownership enforced by the real registry; oversell / stacking / write-outside-owned checks pointed at SID)."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_fresh_gap_live as tfg  # noqa: E402
from sig.engine.ownership import MarketOwnership  # noqa: E402
from sig.engine.parity_live import SID, ParityLive, ParityLiveConfig  # noqa: E402

run = tfg.run


@pytest.fixture(autouse=True)
def _venue_checks_for_parity(monkeypatch):
    monkeypatch.setattr(tfg, "SID", SID)


class LegVenue(tfg.Venue):
    """Per-market fill probability (legging) on top of the shared fake venue."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.leg_fill = {}

    def step(self):
        base = self.fill_p
        for oid, o in list(self.orders.items()):
            p = self.leg_fill.get(o["exchange_id"], base)
            if p <= 0.0:
                if self.clock() >= o["exp"]:
                    del self.orders[oid]
                continue
        saved = {oid: o for oid, o in self.orders.items() if self.leg_fill.get(o["exchange_id"], base) <= 0.0}
        for oid in saved:
            del self.orders[oid]
        super().step()
        self.orders.update({oid: o for oid, o in saved.items() if self.clock() < o["exp"]})


def make(tmp_path, seed=1, lag=False, reject_p=0.0, **kw):
    reg = MarketOwnership(str(tmp_path / "own.json"))
    clk = tfg.Clock()
    v = LegVenue(reg, random.Random(seed), clk, lag=lag, reject_p=reject_p)
    cfg = ParityLiveConfig(stop_file=str(tmp_path / "STOP"), state_path=str(tmp_path / "parity.json"),
                           election_cutoff_iso="2099-01-01T00:00:00+00:00", **kw)
    tr = ParityLive(broker=v, ownership=reg, cfg=cfg, clock=clk)
    return reg, clk, v, tr


def quotes(v, races):
    out = {}
    for race, (d, r) in races.items():
        if d in v.book and r in v.book:
            (db, da), (rb, ra) = v.book[d], v.book[r]
            out[race] = {"d": d, "r": r, "d_bid": db, "d_ask": da, "r_bid": rb, "r_ask": ra}
    return out


RACES = {"NV": ("D1", "R1")}


def tick(tr, v, clk, races=RACES, cash=1e6, dt=60.0, opens=True):
    out = run(tr.tick(quotes(v, races), dict(v.pos), v.open_view() if opens else None, cash))
    v.step()
    clk.t += dt
    return out


def places(v, ex=None, action=None):
    return [w for w in v.writes if w[1] == SID and w[2] == "place" and (ex is None or w[3] == ex)
            and (action is None or w[4] == action)]


def no_held(v, ex):
    return max(-v.pos.get(ex, 0.0), 0.0)


def test_P1_full_round_trip_buys_both_nos_then_sells_both_and_releases(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=400)
    v.fill_p = 1.0
    v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}                 # sum of bids 1.03 -> 3c locked
    out = tick(tr, v, clk)
    assert out.get("claimed") == ["NV"] and not places(v)                 # probation: no order on the claim tick
    for _ in range(4):
        tick(tr, v, clk)
    b = places(v, action="buy")
    assert {w[3] for w in b} == {"D1", "R1"} and all(w[5] == "no" for w in b)
    assert {(w[3], w[7]) for w in b} == {("D1", 0.45), ("R1", 0.52)}      # NO ask = 1 - YES bid
    assert no_held(v, "D1") == 400 and no_held(v, "R1") == 400
    v.book = {"D1": (0.535, 0.54), "R1": (0.465, 0.47)}                 # asks sum 1.01 -> exit pnl 1.03-1.01 = 2c
    for _ in range(6):
        tick(tr, v, clk)
    s = places(v, action="sell")
    assert {(w[3], w[7]) for w in s} == {("D1", 0.46), ("R1", 0.53)}      # NO bid = 1 - YES ask
    assert no_held(v, "D1") == 0 and no_held(v, "R1") == 0
    assert "NV" not in tr.active and reg.owner_of("D1") is None and reg.owner_of("R1") is None
    assert tr.realized == pytest.approx(400 * 0.02, abs=0.01)
    assert v.violations == []


def test_P2_refuses_held_owned_busy_or_thin_edge_races(tmp_path):
    for case in ("held", "owned", "busy", "edge"):
        reg, clk, v, tr = make(tmp_path / case)
        v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}
        if case == "held":
            v.pos["R1"] = 120.0                                           # we hold YES in a leg (cross)
        elif case == "owned":
            reg.claim("rotation", ["D1"])
        elif case == "busy":
            v.orders["x"] = {"order_id": "x", "exchange_id": "D1", "side": "yes", "action": "buy", "price": 0.5,
                             "quantity": 5.0, "exp": 1e18}
        elif case == "edge":
            v.book = {"D1": (0.53, 0.535), "R1": (0.485, 0.49)}         # 1.5c < 2c
        out = tick(tr, v, clk)
        assert "claimed" not in out and not places(v), case


def test_P3_legging_short_leg_is_topped_up_without_breaking_the_lock(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=400)
    v.fill_p = 1.0
    v.leg_fill = {"R1": 0.0}                                              # R leg doesn't fill at first
    v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}
    for _ in range(4):
        tick(tr, v, clk)
    assert no_held(v, "D1") == 400 and no_held(v, "R1") == 0
    v.leg_fill = {}
    for _ in range(10):
        tick(tr, v, clk)
    rb = places(v, "R1", "buy")
    assert len(rb) >= 2 and all(w[7] <= 1.0 - 0.45 + 1e-9 for w in rb)    # pair cost never above 1
    assert no_held(v, "R1") == 400 and tr.active["NV"]["phase"] == "holding"
    assert v.violations == []


def test_P4_legging_unwinds_the_excess_when_the_lock_cannot_be_kept(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=400)
    v.fill_p = 1.0
    v.leg_fill = {"R1": 0.0}
    v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}
    for _ in range(4):
        tick(tr, v, clk)
    v.book["R1"] = (0.40, 0.405)                                          # R's NO ask 0.60 > cap 0.55
    v.leg_fill = {}
    for _ in range(10):
        tick(tr, v, clk)
    assert no_held(v, "D1") == 0 and no_held(v, "R1") == 0                # excess sold back: never a naked NO left
    assert "NV" not in tr.active and reg.owner_of("D1") is None
    assert tr.realized > -400 * 0.02 and v.violations == []                # bounded (spread) loss


def test_P5_lost_reply_waits_and_never_stacks_or_oversells(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=400)
    v.fill_p = 1.0
    v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}
    real = v.place_order
    calls = {"n": 0}

    async def flaky(**kw):
        res = await real(**kw)
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("reply lost")                              # the R entry reached the venue
        return res
    v.place_order = flaky
    for _ in range(12):
        tick(tr, v, clk)
    assert v.violations == [] and no_held(v, "R1") <= 400 and no_held(v, "D1") <= 400


def test_P6_budget_caps_reserved_capital_and_takes_biggest_edges(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=1000, budget_usd=2000.0, max_open=10)
    races = {f"R{i}": (f"D{i}x", f"R{i}x") for i in range(5)}
    for i in range(5):
        v.book[f"D{i}x"] = (0.55 + 0.002 * i, 0.555 + 0.002 * i)
        v.book[f"R{i}x"] = (0.47, 0.475)
    tick(tr, v, clk, races=races)
    assert tr.reserved() <= 2000.0 and len(tr.active) == 2
    assert set(tr.active) == {"R4", "R3"}                                  # biggest edges first
    assert tr.unused_budget() == pytest.approx(2000.0 - tr.reserved(), abs=0.01)


def test_P7_wind_down_exits_cheap_pairs_and_holds_the_rest_to_settlement(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=400)
    v.fill_p = 1.0
    races = {"A": ("DA", "RA"), "B": ("DB", "RB")}
    v.book = {"DA": (0.55, 0.555), "RA": (0.48, 0.485), "DB": (0.56, 0.565), "RB": (0.47, 0.475)}
    for _ in range(10):
        tick(tr, v, clk, races=races)
    assert all(p["phase"] == "holding" for p in tr.active.values())
    open(tr.cfg.stop_file, "w").close()
    tr.check_kill()
    v.book.update({"DA": (0.545, 0.55), "RA": (0.48, 0.485)})            # A: exit pnl 1.03-1.035 = -0.5c -> exit
    v.book.update({"DB": (0.53, 0.535), "RB": (0.47, 0.475)})            # B: 1.03-1.01 = +2c -> exit too
    for _ in range(6):
        tick(tr, v, clk, races=races)
    assert "A" not in tr.active and "B" not in tr.active
    reg2, clk2, v2, tr2 = make(tmp_path / "hold", pairs=400)
    v2.fill_p = 1.0
    v2.book = {"DA": (0.55, 0.555), "RA": (0.48, 0.485)}
    for _ in range(10):
        tick(tr2, v2, clk2, races={"A": ("DA", "RA")})
    open(tr2.cfg.stop_file, "w").close()
    tr2.check_kill()
    v2.book = {"DA": (0.55, 0.57), "RA": (0.48, 0.49)}                   # exit pnl 1.03-1.06 = -3c: hold, locked
    for _ in range(4):
        tick(tr2, v2, clk2, races={"A": ("DA", "RA")})
    assert tr2.active["A"]["phase"] == "holding" and not places(v2, action="sell")
    assert tr2.unused_budget() == 0.0                                      # no carve-out while winding down
    n = len(places(v2, action="buy"))
    v2.book = {"DC": (0.56, 0.565), "RC": (0.47, 0.475)}
    tick(tr2, v2, clk2, races={"C": ("DC", "RC")})
    assert len(places(v2, action="buy")) == n                             # no new entries


def test_P8_restart_mid_pair_resumes_without_a_second_entry(tmp_path):
    reg, clk, v, tr = make(tmp_path, pairs=400)
    v.fill_p = 1.0
    v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}
    for _ in range(3):
        tick(tr, v, clk)
    tr2 = ParityLive(broker=v, ownership=reg, cfg=tr.cfg, clock=clk)
    tr2.load()
    for _ in range(4):
        tick(tr2, v, clk)
    assert len(places(v, "D1", "buy")) == 1 and len(places(v, "R1", "buy")) == 1
    assert tr2.active["NV"]["phase"] == "holding" and v.violations == []


def test_P9_failed_open_orders_read_does_nothing(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.book = {"D1": (0.55, 0.555), "R1": (0.48, 0.485)}
    out = tick(tr, v, clk, opens=False)
    assert out.get("skipped") == "open_orders_read_failed" and not tr.active


@pytest.mark.parametrize("seed", range(10))
def test_P10_chaos_invariants(tmp_path, seed):
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=rng.random() < 0.5, reject_p=0.15, pairs=300,
                           budget_usd=1500.0, max_open=3)
    v.fill_p = rng.choice([0.3, 0.7, 1.0])
    real = v.place_order

    async def flaky(**kw):
        res = await real(**kw)
        if rng.random() < 0.1:
            raise TimeoutError("reply lost")
        return res
    v.place_order = flaky
    races = {f"X{i}": (f"D{i}", f"R{i}") for i in range(6)}
    reg.claim("fresh_gap", ["D5"])
    mid = {}
    for i in range(6):
        mid[f"D{i}"], mid[f"R{i}"] = rng.uniform(0.3, 0.7), None
        mid[f"R{i}"] = 1.0 - mid[f"D{i}"]
    peak = 0.0
    for t in range(160):
        for i in range(6):
            dev = rng.uniform(-0.01, 0.04)
            dm = min(0.95, max(0.05, mid[f"D{i}"] + rng.uniform(-0.01, 0.01)))
            mid[f"D{i}"] = dm
            rm = min(0.95, max(0.05, 1.0 - dm + dev))
            v.book[f"D{i}"] = (round(dm, 3), round(dm + 0.005, 3))
            v.book[f"R{i}"] = (round(rm, 3), round(rm + 0.005, 3))
            for leg in (f"D{i}", f"R{i}"):
                v.leg_fill[leg] = rng.choice([0.0, 0.5, 1.0]) if rng.random() < 0.2 else v.leg_fill.get(leg, v.fill_p)
        if t == 80:
            tr = ParityLive(broker=v, ownership=reg, cfg=tr.cfg, clock=clk)
            tr.load()
        if t == 120:
            open(tr.cfg.stop_file, "w").close()
        tr.check_kill()
        opens = None if rng.random() < 0.05 else v.open_view()
        run(tr.tick(quotes(v, races), dict(v.pos), opens, rng.choice([1e6, 800.0, 50.0])))
        v.step()
        clk.t += rng.choice([20.0, 60.0, 120.0])
        held_usd = sum(no_held(v, ex) * (1 - v.book[ex][0]) for ex in v.book)
        peak = max(peak, held_usd)
    assert v.violations == [], v.violations
    assert all(v.pos.get(ex, 0.0) <= 1e-6 for ex in v.book if ex != "D5")   # never long YES in a parity leg
    assert not [w for w in places(v) if w[3] == "D5"]                        # never touched a foreign market
    assert peak <= 1500.0 * 1.1 + 300                                        # budget (+ one leg of legging slack)
    for race, p in tr.active.items():                                        # any pair still open is balanced or
        hd, hr = no_held(v, p["d"]), no_held(v, p["r"])                      # being unwound, never released
        assert reg.owner_of(p["d"]) == SID and reg.owner_of(p["r"]) == SID
    for ex in v.book:
        if no_held(v, ex) > 0.5:
            assert reg.owner_of(ex) == SID                                   # holdings always stay owned


# ---------------------------------------------------------------- loop wiring (main_sig._parity_live_loop)
import asyncio as _asyncio  # noqa: E402
from datetime import datetime as _dt, timezone as _tz  # noqa: E402

_REAL_SLEEP = _asyncio.sleep


class _XM:
    def __init__(self, book):
        now = _dt.now(_tz.utc).isoformat()
        self._sig_prices = {ex: {"best_bid": b, "best_ask": a, "updated_at": now} for ex, (b, a) in book.items()}


class _Broker:
    def __init__(self, venue):
        self.v = venue

    async def place_order(self, **kw):
        return await self.v.place_order(**kw)

    async def get_open_orders_all(self):
        return self.v.open_view()

    async def get_positions(self, fresh=False):
        class P:
            def __init__(self, ex, q):
                self.contract_id, self.quantity, self.side = ex, abs(q), ("yes" if q > 0 else "no")
        return [P(ex, q) for ex, q in self.v.pos.items() if abs(q) >= 1]

    async def get_account(self):
        class A:
            buying_power = 1e6
        return A()


class _Ledger:
    def __init__(self):
        self.external_reserves = {}


class _Orch:
    def __init__(self):
        self.ledger, self.is_halted = _Ledger(), False


def _run_loop(monkeypatch, tmp_path, cfg_xm, broker, orch, ticks=4):
    import main_sig
    from sig.engine import ownership as own
    reg = MarketOwnership(str(tmp_path / "own.json"))
    monkeypatch.setattr(own, "registry", lambda: reg)
    monkeypatch.chdir(tmp_path)
    n = {"k": 0}
    stop = _asyncio.Event()

    async def fast_sleep(s):
        n["k"] += 1
        if n["k"] > ticks:
            stop.set()
        await _REAL_SLEEP(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(_asyncio.wait_for(main_sig._parity_live_loop(
            store=None, broker=broker, orch=orch, xm_provider=_XM({"D1": (0.55, 0.555), "R1": (0.48, 0.485)}),
            pairs=[("NV", "D1", "R1")], cfg_raw={"cross_market": cfg_xm}, stop_event=stop), 10))
    finally:
        loop.close()
    return reg


def test_P11_loop_off_by_default(tmp_path, monkeypatch):
    reg = _run_loop(monkeypatch, tmp_path, {}, None, None)
    assert reg.owned_by(SID) == set()


def test_P12_loop_live_claims_and_registers_its_carve_out(tmp_path, monkeypatch):
    reg0, clk, v, tr = make(tmp_path / "v")
    orch = _Orch()
    reg = _run_loop(monkeypatch, tmp_path, {"parity_live_mode": "live", "parity_live_pairs": 100}, _Broker(v), orch, ticks=3)
    assert SID in orch.ledger.external_reserves
    assert reg.owned_by(SID) == {"D1", "R1"}


def test_P13_loop_live_without_a_ledger_never_claims(tmp_path, monkeypatch):
    reg0, clk, v, tr = make(tmp_path / "v")
    reg = _run_loop(monkeypatch, tmp_path, {"parity_live_mode": "live"}, _Broker(v), None, ticks=3)
    assert reg.owned_by(SID) == set() and not v.writes
