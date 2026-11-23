"""BREAK TESTS for the election-night live trader (sig/engine/election_night_live.py) on the fake venue of
test_fresh_gap_live (ownership enforced by the real registry, partial fills, lag, rejects), plus a REPLAY of the real
2024 election night (tools/research/election_night/night2024.json) with SIG lagging Polymarket."""
import json
import os
import random
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_fresh_gap_live as tfg  # noqa: E402
from sig.engine.election_night import ElectionNightConfig  # noqa: E402
from sig.engine.election_night_live import SID, ElectionNightLive, ElectionNightLiveConfig  # noqa: E402
from sig.engine.ownership import MarketOwnership  # noqa: E402

run = tfg.run
H = 3600.0


@pytest.fixture(autouse=True)
def _venue_checks_for_election(monkeypatch):
    monkeypatch.setattr(tfg, "SID", SID)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def make(tmp_path, seed=1, dry=False, lag=False, reject_p=0.0, start_off=0.0, end_off=20 * H, reserve_off=-24 * H,
         **kw):
    reg = MarketOwnership(str(tmp_path / "own.json"))
    clk = tfg.Clock()
    v = tfg.Venue(reg, random.Random(seed), clk, lag=lag, reject_p=reject_p)
    cfg = ElectionNightLiveConfig(window_start_iso=iso(clk.t + start_off), window_end_iso=iso(clk.t + end_off),
                                  reserve_from_iso=iso(clk.t + reserve_off), stop_file=str(tmp_path / "STOP"),
                                  state_path=str(tmp_path / "state.json"), dry_path=str(tmp_path / "dry.jsonl"), **kw)
    tr = ElectionNightLive(broker=v, ownership=reg, cfg=cfg, dry=dry, clock=clk)
    return reg, clk, v, tr


def leg(race, ex, poly, ask, kalshi=None, bid=None):
    return {"race": race, "exchange_id": ex, "poly": poly, "kalshi": kalshi, "kalshi_mapped": kalshi is not None,
            "sig_bid": (None if ask is None else ask - 0.01) if bid is None else bid, "sig_ask": ask, "ask_size": 5000.0}


def fbo(v):
    out = {}
    for f in v.fills:
        out[f["order_id"]] = out.get(f["order_id"], 0.0) + abs(f["quantity"])
    return out


def places(v, ex=None):
    return [w for w in v.writes if w[1] == SID and w[2] == "place" and (ex is None or w[3] == ex)]


def spent_real(v):
    return sum(f["quantity"] * f["price"] for f in v.fills if f["strategy"] == SID)


def tick(tr, v, legs, cash=1e6, fills=True, dt=30.0):
    out = run(tr.tick(legs, v.open_view(), cash, fills_by_order=fbo(v) if fills else None))
    v.step()
    tr.clock.t += dt
    return out


def arm_then_call(tr, v, clk, race="R1", ex="A", ask=0.90, kalshi=0.97):
    tick(tr, v, [leg(race, ex, 0.88, 0.87)])                          # seen low -> armed
    v.book[ex] = (ask - 0.01, ask)
    return tick(tr, v, [leg(race, ex, 0.97, ask, kalshi=kalshi)])     # called


# ---------------------------------------------------------------- behaviour
def test_E1_called_race_is_claimed_bought_below_poly_topped_up_and_held(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.fill_p = 0.5
    out = arm_then_call(tr, v, clk)
    assert out["fired"] == 1 and out["placed"] == 1
    w = places(v, "A")[0]
    assert w[4] == "buy" and w[5] == "yes" and abs(w[7] - 0.92) < 1e-9          # min(ask+2c, poly-1c)
    assert reg.owner_of("A") == SID
    for _ in range(12):
        tick(tr, v, [leg("R1", "A", 0.97, 0.90, kalshi=0.97)], dt=40.0)
    usd = sum(x[6] * x[7] for x in places(v, "A"))
    assert len(places(v, "A")) <= tr.cfg.max_attempts and usd <= tr.cfg.usd_per_race + 1
    assert not [x for x in v.writes if x[1] == SID and x[2] == "place" and x[4] == "sell"]
    assert reg.owner_of("A") == SID and v.violations == []                      # never released: held to settlement


def test_E2_window_arming_and_firing(tmp_path):
    reg, clk, v, tr = make(tmp_path, start_off=2 * H, end_off=4 * H)
    tick(tr, v, [leg("R1", "A", 0.88, 0.87)])                                   # before window: arms only
    out = tick(tr, v, [leg("R1", "A", 0.97, 0.90, kalshi=0.97)])
    assert out["fired"] == 0 and not places(v)
    clk.t += 2 * H
    out = tick(tr, v, [leg("R1", "A", 0.97, 0.90, kalshi=0.97)])                # armed 2h ago, in window -> fire
    assert out["fired"] == 1 and places(v)
    clk.t += 3 * H                                                              # after the window: nothing
    n = len(places(v))
    tick(tr, v, [leg("R2", "B", 0.88, 0.87)])
    tick(tr, v, [leg("R2", "B", 0.97, 0.90, kalshi=0.97)])
    assert len(places(v)) == n


def test_E3_safe_seats_disagreement_and_caught_up_never_trade(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    for _ in range(3):
        tick(tr, v, [leg("S", "S1", 0.99, 0.97)])                               # never seen low: not armed
    tick(tr, v, [leg("K", "K1", 0.88, 0.87)])
    tick(tr, v, [leg("K", "K1", 0.97, 0.90, kalshi=0.90)])                      # Kalshi disagrees
    tick(tr, v, [leg("C", "C1", 0.88, 0.87)])
    tick(tr, v, [leg("C", "C1", 0.97, 0.965)])                                  # SIG already caught up
    tick(tr, v, [leg("N", "N1", 0.88, 0.87)])
    tick(tr, v, [leg("N", "N1", 0.94, 0.85)])                                   # below theta (the 2024 false calls)
    assert not places(v)


def test_E4_budget_caps_total_and_biggest_edges_go_first(tmp_path):
    reg, clk, v, tr = make(tmp_path, budget_usd=5000.0, max_writes_per_min=100)
    v.fill_p = 1.0
    legs0 = [leg(f"R{i}", f"X{i}", 0.88, 0.87) for i in range(10)]
    tick(tr, v, legs0)
    legs1 = [leg(f"R{i}", f"X{i}", 0.97, 0.80 + 0.01 * i, kalshi=0.97) for i in range(10)]   # X0 biggest edge
    for _ in range(10):
        tick(tr, v, legs1, dt=80.0)
    assert spent_real(v) <= 5000.0 + 1e-6
    bought = {w[3] for w in places(v)}
    assert "X0" in bought and "X9" not in bought


def test_E5_cash_cap_respects_other_strategies_reserves(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.fill_p = 1.0
    tr.others_reserved = lambda: 900.0
    tick(tr, v, [leg("R1", "A", 0.88, 0.87)])
    for _ in range(6):
        tick(tr, v, [leg("R1", "A", 0.97, 0.90, kalshi=0.97)], cash=1000.0, dt=80.0)
    assert sum(w[6] * w[7] for w in places(v)) <= 100.0 * 3 + 1                 # <= (1000 - 900) per order


def test_E6_write_cap_holds_in_a_burst_of_calls(tmp_path):
    reg, clk, v, tr = make(tmp_path, max_writes_per_min=20, budget_usd=1e6)
    tick(tr, v, [leg(f"R{i}", f"X{i}", 0.88, 0.87) for i in range(40)])
    tick(tr, v, [leg(f"R{i}", f"X{i}", 0.97, 0.90, kalshi=0.97) for i in range(40)], dt=1.0)
    tick(tr, v, [leg(f"R{i}", f"X{i}", 0.97, 0.90, kalshi=0.97) for i in range(40)], dt=1.0)
    ts = sorted(w[0] for w in places(v))
    assert max(sum(1 for t in ts if t0 <= t < t0 + 60) for t0 in ts) <= 20
    tick(tr, v, [leg(f"R{i}", f"X{i}", 0.97, 0.90, kalshi=0.97) for i in range(40)], dt=61.0)
    tick(tr, v, [leg(f"R{i}", f"X{i}", 0.97, 0.90, kalshi=0.97) for i in range(40)], dt=1.0)
    assert len({w[3] for w in places(v)}) == 40                                 # the rest went out a minute later


def test_E7_lost_reply_counts_full_notional_and_never_stacks(tmp_path):
    reg, clk, v, tr = make(tmp_path, budget_usd=3000.0)
    v.fill_p = 1.0
    real = v.place_order
    calls = {"n": 0}

    async def flaky(**kw):
        res = await real(**kw)
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("reply lost")
        return res
    v.place_order = flaky
    arm_then_call(tr, v, clk)
    assert tr.committed() >= 2000.0 - 1                                         # assumed spent
    for _ in range(10):
        tick(tr, v, [leg("R1", "A", 0.97, 0.90, kalshi=0.97)], dt=40.0)
    assert spent_real(v) <= tr.cfg.usd_per_race + 1 and v.violations == []
    tick(tr, v, [leg("R2", "B", 0.88, 0.87)])
    for _ in range(4):
        tick(tr, v, [leg("R2", "B", 0.97, 0.90, kalshi=0.97)], dt=80.0)
    assert spent_real(v) <= 3000.0 + 1


def test_E8_market_owned_by_another_strategy_is_skipped(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    reg.claim("rotation", ["A"])
    arm_then_call(tr, v, clk)
    assert not places(v) and reg.owner_of("A") == "rotation"
    assert tr.races["R1"]["skip"] == "owned_by_rotation"


def test_E9_dry_mode_records_and_never_touches_the_venue(tmp_path):
    reg, clk, v, tr = make(tmp_path, dry=True)
    arm_then_call(tr, v, clk)
    for _ in range(4):
        tick(tr, v, [leg("R1", "A", 0.97, 0.90, kalshi=0.97)], dt=80.0)
    assert v.writes == [] and reg.owner_of("A") is None
    rec = [json.loads(x) for x in open(tr.cfg.dry_path, encoding="utf-8")]
    assert len(rec) == 1 and rec[0]["exchange_id"] == "A" and abs(rec[0]["limit"] - 0.92) < 1e-9


def test_E10_stop_file_blocks_new_orders_and_keeps_holdings(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    v.fill_p = 1.0
    arm_then_call(tr, v, clk)
    n = len(places(v))
    open(tr.cfg.stop_file, "w").close()
    tr.check_kill()
    tick(tr, v, [leg("R2", "B", 0.88, 0.87)])
    tick(tr, v, [leg("R2", "B", 0.97, 0.90, kalshi=0.97)])
    assert len(places(v)) == n and reg.owner_of("A") == SID and tr.unused_budget() == 0.0


def test_E11_restart_mid_night_never_double_spends(tmp_path):
    reg, clk, v, tr = make(tmp_path, budget_usd=2500.0)
    v.fill_p = 0.0                                                              # resting, nothing filled yet
    arm_then_call(tr, v, clk)
    tr2 = ElectionNightLive(broker=v, ownership=reg, cfg=tr.cfg, dry=False, clock=clk)
    tr2.load()
    assert tr2.committed() >= 2000.0 - 1 and "R1" in tr2.night.fired
    for _ in range(3):
        run(tr2.tick([leg("R1", "A", 0.97, 0.90, kalshi=0.97)], v.open_view(), 1e6, fills_by_order=fbo(v)))
        clk.t += 30
    assert len(places(v, "A")) == 1                                             # live order still rests: no stack


def test_E12_carve_out_only_from_reserve_start_to_window_end(tmp_path):
    reg, clk, v, tr = make(tmp_path, reserve_off=1 * H, start_off=2 * H, end_off=3 * H, budget_usd=30000.0)
    assert tr.unused_budget() == 0.0                                            # weeks before: no carve-out
    clk.t += 1.5 * H
    assert tr.unused_budget() == 30000.0
    clk.t += 2 * H
    assert tr.unused_budget() == 0.0


def test_E13_failed_open_orders_read_places_nothing(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    tick(tr, v, [leg("R1", "A", 0.88, 0.87)])
    out = run(tr.tick([leg("R1", "A", 0.97, 0.90, kalshi=0.97)], None, 1e6))
    assert out.get("skipped") == "open_orders_read_failed" and not places(v)


# ---------------------------------------------------------------- chaos
@pytest.mark.parametrize("seed", range(12))
def test_E14_chaos_invariants(tmp_path, seed):
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=rng.random() < 0.5, reject_p=0.2, budget_usd=8000.0)
    v.fill_p = rng.choice([0.2, 0.6, 1.0])
    reg.claim("fresh_gap", ["X3"])
    real = v.place_order

    async def flaky(**kw):
        res = await real(**kw)
        if rng.random() < 0.15:
            raise TimeoutError("reply lost")
        return res
    v.place_order = flaky
    poly = {f"X{i}": rng.uniform(0.80, 0.93) for i in range(12)}
    for t in range(150):
        legs = []
        for i in range(12):
            ex = f"X{i}"
            if rng.random() < 0.1:
                poly[ex] = min(0.995, poly[ex] + rng.uniform(0.0, 0.06))
            elif rng.random() < 0.03:
                poly[ex] = max(0.5, poly[ex] - rng.uniform(0.0, 0.08))
            ask = round(min(0.99, poly[ex] - rng.uniform(-0.01, 0.10)), 3)
            v.book[ex] = (ask - 0.01, ask)
            legs.append(leg(f"R{i}", ex, poly[ex], None if rng.random() < 0.05 else ask,
                            kalshi=None if rng.random() < 0.5 else poly[ex] + rng.uniform(-0.02, 0.02)))
        if t == 70:
            tr = ElectionNightLive(broker=v, ownership=reg, cfg=tr.cfg, dry=False, clock=clk)   # restart
            tr.load()
        opens = None if rng.random() < 0.05 else v.open_view()
        run(tr.tick(legs, opens, rng.choice([1e6, 3000.0, 200.0]), fills_by_order=fbo(v) if rng.random() < 0.7 else None))
        v.step()
        clk.t += rng.choice([5.0, 30.0, 90.0])
    assert v.violations == [], v.violations
    assert spent_real(v) <= 8000.0 + 1
    per = {}
    for f in v.fills:
        if f["strategy"] == SID:
            per[f["exchange_id"]] = per.get(f["exchange_id"], 0.0) + f["quantity"] * f["price"]
    assert all(x <= tr.cfg.usd_per_race + 1 for x in per.values())
    assert not [w for w in places(v) if w[4] != "buy" or w[3] == "X3"]
    assert all(reg.owner_of(ex) == SID for ex in per)


# ---------------------------------------------------------------- 2024 replay
NIGHT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "research", "election_night", "night2024.json")


class LagVenue(tfg.Venue):
    """Deterministic fills: a buy fills fully when its limit >= the current SIG ask (SIG = Poly `lag` minutes ago)."""
    def step(self):
        now = self.clock()
        for oid, o in list(self.orders.items()):
            if now >= o["exp"]:
                del self.orders[oid]
                continue
            b, a = self.book.get(o["exchange_id"], (None, None))
            if a is not None and o["price"] >= a - 1e-9:
                q = o["quantity"]
                self.pos[o["exchange_id"]] = self.pos.get(o["exchange_id"], 0.0) + q
                self.fills.append({"exchange_id": o["exchange_id"], "outcome": "yes", "price": o["price"], "quantity": q,
                                   "signed_qty": q, "strategy": self.reg.order_owner(oid), "order_id": oid})
                del self.orders[oid]


def _replay(tmp_path, lag_min):
    import bisect
    legs = json.load(open(NIGHT, encoding="utf-8"))
    paths = {}
    for i, L in enumerate(legs):
        h = sorted((int(p["t"]), float(p["p"])) for p in L["hist"])
        paths[f"L{i}"] = (L["event"], [t for t, _ in h], [p for _, p in h], float(L["yes"]))
    t0, t1 = 1730847600, 1730912400                 # 2024-11-05 23:00Z .. 11-06 17:00Z
    reg = MarketOwnership(str(tmp_path / "own.json"))
    clk = tfg.Clock()
    clk.t = t0 - 6 * H
    v = LagVenue(reg, random.Random(0), clk)
    cfg = ElectionNightLiveConfig(window_start_iso=iso(t0), window_end_iso=iso(t1), reserve_from_iso=iso(t0 - 24 * H),
                                  stop_file=str(tmp_path / "STOP"), state_path=str(tmp_path / "st.json"),
                                  budget_usd=40000.0, usd_per_race=2000.0, max_writes_per_min=20)
    tr = ElectionNightLive(broker=v, ownership=reg, cfg=cfg, dry=False, clock=clk)

    def px(ts, ps, t):
        j = bisect.bisect_right(ts, t) - 1
        return ps[j] if j >= 0 else None
    while clk.t < t1:
        legs_now = []
        for ex, (ev, ts, ps, yes) in paths.items():
            p = px(ts, ps, clk.t)
            s = px(ts, ps, clk.t - lag_min * 60)
            if p is None or s is None:
                continue
            ask = round(min(0.995, s + 0.005), 3)
            v.book[ex] = (ask - 0.01, ask)
            legs_now.append({"race": ev, "exchange_id": ex, "poly": p, "kalshi": None, "kalshi_mapped": False,
                             "sig_bid": ask - 0.01, "sig_ask": ask, "ask_size": 5000.0})
        run(tr.tick(legs_now, v.open_view(), 1e6, fills_by_order=fbo(v)))
        v.step()
        clk.t += 60.0
    pnl = sum(f["quantity"] * (paths[f["exchange_id"]][3] - f["price"]) for f in v.fills)
    losers = [f for f in v.fills if paths[f["exchange_id"]][3] < 0.5]
    return pnl, spent_real(v), losers, v


@pytest.mark.parametrize("lag_min,min_pnl", [(0, -1.0), (30, 300.0), (60, 1000.0)])
def test_E15_replay_2024_night(tmp_path, lag_min, min_pnl):
    if not os.path.exists(NIGHT):
        pytest.skip("2024 night data not present")
    pnl, spent, losers, v = _replay(tmp_path, lag_min)
    assert losers == []                                                          # no false call bought at 95%
    assert spent <= 40000.0 + 1 and v.violations == []
    assert pnl >= min_pnl, (lag_min, pnl, spent)


# ---------------------------------------------------------------- loop wiring (main_sig._election_night_live_loop)
import asyncio as _asyncio  # noqa: E402

_REAL_SLEEP = _asyncio.sleep


class _XM:
    def __init__(self, legs):
        self.legs = legs                                     # ex -> (poly, bid, ask)

    def active_sig_exchange_ids(self):
        return list(self.legs)

    def aligned_benchmark(self, ex):
        return self.legs[ex][0]

    def sig_book(self, ex, max_age_s=60.0):
        return self.legs[ex][1], self.legs[ex][2]


class _Store:
    _db_path = "nonexistent.db"

    async def _read(self, *a):
        return []


def _run_loop(monkeypatch, tmp_path, cfg_xm, xm, ticks, broker=None, orch=None, script=None):
    import main_sig
    from sig.engine import ownership as own
    reg = MarketOwnership(str(tmp_path / "own.json"))
    monkeypatch.setattr(own, "registry", lambda: reg)
    monkeypatch.setattr(main_sig, "_kalshi_latest", lambda db, max_age_s=300.0: {})
    monkeypatch.chdir(tmp_path)
    count = {"n": 0}
    stop = _asyncio.Event()

    async def fast_sleep(s):
        count["n"] += 1
        if script:
            script(count["n"], xm)
        if count["n"] > ticks:
            stop.set()
        await _REAL_SLEEP(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(_asyncio.wait_for(main_sig._election_night_live_loop(
            store=_Store(), broker=broker, orch=orch, xm_provider=xm, pairs=[("NV-Sen", "D1", "R1")],
            cfg_raw={"cross_market": cfg_xm}, stop_event=stop), 10))
    finally:
        loop.close()
    return reg


def test_E16_loop_off_by_default(tmp_path, monkeypatch):
    reg = _run_loop(monkeypatch, tmp_path, {}, _XM({"D1": (0.97, 0.89, 0.90), "R1": (0.03, 0.02, 0.03)}), 3)
    assert reg.owned_by(SID) == set() and not os.path.exists(tmp_path / "data" / "election_night_live")


def test_E17_loop_dry_run_records_a_called_race_and_places_nothing(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    xm = _XM({"D1": (0.88, 0.86, 0.87), "R1": (0.12, 0.11, 0.12)})

    def script(n, x):
        if n == 3:
            x.legs["D1"] = (0.97, 0.89, 0.90)                # the call: Poly jumps, SIG lags
    cfg = {"election_night_live_mode": "dry", "election_night_live_window_start": iso(now - H),
           "election_night_live_window_end": iso(now + H)}
    reg = _run_loop(monkeypatch, tmp_path, cfg, xm, 6, broker=object(), script=script)
    rec = [json.loads(x) for x in open(tmp_path / "data" / "election_night_live" / "dry.jsonl", encoding="utf-8")]
    assert [r["exchange_id"] for r in rec] == ["D1"] and abs(rec[0]["limit"] - 0.92) < 1e-9
    assert reg.owned_by(SID) == set()                        # dry: no claims


def test_E18_loop_live_without_ledger_refuses_and_bad_config_does_not_start(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    xm = _XM({"D1": (0.88, 0.86, 0.87), "R1": (0.12, 0.11, 0.12)})

    def script(n, x):
        if n == 3:
            x.legs["D1"] = (0.97, 0.89, 0.90)

    class Boom:
        async def place_order(self, **kw):
            raise AssertionError("must not place without a ledger")

        async def get_open_orders_all(self):
            return []

        async def get_account(self):
            raise AssertionError("no reads when stopped")
    cfg = {"election_night_live_mode": "live", "election_night_live_window_start": iso(now - H),
           "election_night_live_window_end": iso(now + H)}
    _run_loop(monkeypatch, tmp_path, cfg, xm, 6, broker=Boom(), orch=None, script=script)
    (tmp_path / "b").mkdir()
    _run_loop(monkeypatch, tmp_path / "b", dict(cfg, election_night_live_window_start="not-a-date"), xm, 3, broker=Boom())


def test_E19_loop_off_but_holding_keeps_the_lease(tmp_path, monkeypatch):
    import main_sig
    from sig.engine import ownership as own
    reg = MarketOwnership(str(tmp_path / "own.json"))
    reg.claim(SID, ["D1"])
    monkeypatch.setattr(own, "registry", lambda: reg)
    stop = _asyncio.Event()
    n = {"k": 0}

    async def fast_sleep(s):
        n["k"] += 1
        if n["k"] > 2:
            stop.set()
        await _REAL_SLEEP(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(main_sig._election_night_live_loop(
            store=_Store(), broker=None, orch=None, xm_provider=_XM({}), pairs=[("NV-Sen", "D1", "R1")],
            cfg_raw={"cross_market": {}}, stop_event=stop))
    finally:
        loop.close()
    assert reg.lease_ok(SID) and reg.owner_of("D1") == SID
