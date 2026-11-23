"""BREAK TESTS for the live capital-rotation trader (sig/engine/rotation_live.py), on the fake venue of
test_fresh_gap_live (partial fills, expiry, lag, rejects, ownership enforced by the real registry). The venue's
safety checks (write outside owned markets, oversell, stacking) are pointed at the rotation SID."""
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_fresh_gap_live as tfg  # noqa: E402
from sig.engine.ownership import MarketOwnership  # noqa: E402
from sig.engine.rotation_live import SID, RotationLive, RotationLiveConfig  # noqa: E402

run = tfg.run


@pytest.fixture(autouse=True)
def _venue_checks_for_rotation(monkeypatch):
    monkeypatch.setattr(tfg, "SID", SID)


def make(tmp_path, seed=1, lag=False, reject_p=0.0, **kw):
    reg = MarketOwnership(str(tmp_path / "own.json"))
    clk = tfg.Clock()
    v = tfg.Venue(reg, random.Random(seed), clk, lag=lag, reject_p=reject_p)
    cfg = RotationLiveConfig(stop_file=str(tmp_path / "STOP"), state_path=str(tmp_path / "rot.json"), **kw)
    tr = RotationLive(broker=v, ownership=reg, cfg=cfg, clock=clk)
    return reg, clk, v, tr


def rows(v, size=5000.0):
    return {ex: {"best_bid": b, "best_ask": a, "bid_size": size, "ask_size": size, "age_s": 1.0}
            for ex, (b, a) in v.book.items()}


def fbo(v):
    out = {}
    for f in v.fills:
        out[f["order_id"]] = out.get(f["order_id"], 0.0) + abs(f["quantity"])
    return out


PROP = {"sell_ex": "A", "sell_qty": 3000.0, "buy_ex": "B", "buy_side": "no", "advantage": 0.05}


def setup_book(v):
    v.book = {"A": (0.90, 0.905), "B": (0.15, 0.155)}       # A: held YES (low edge); B: buy NO (= 1 - 0.15 = 0.85)
    v.pos["A"] = 3000.0


def step_n(tr, v, clk, n, prop=None, positions=None, cash=1e6, fills=True):
    out = None
    for _ in range(n):
        out = run(tr.tick(prop, rows(v), dict(v.pos) if positions is None else positions, v.open_view(), cash,
                          fills_by_order=fbo(v) if fills else None))
        v.step()
        clk.t += 60
    return out


def places(v, action=None, ex=None):
    return [w for w in v.writes if w[1] == SID and w[2] == "place" and (action is None or w[4] == action)
            and (ex is None or w[3] == ex)]


def test_R1_full_swap_lifecycle(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    out = run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    assert out.get("claimed") == ["A", "B"] and out["placed"] == 0           # probation: no order on claim tick
    clk.t += 60
    step_n(tr, v, clk, 15)
    sells, buys = places(v, "sell", "A"), places(v, "buy", "B")
    assert sells and sells[0][5] == "yes" and abs(sells[0][7] - 0.90) < 1e-9  # sell held YES at the bid
    sold = 3000.0 - v.pos.get("A", 0.0)
    assert 0 < sold * 0.90 <= tr.cfg.max_swap_usd + 1                         # sized to max_swap_usd
    assert buys and buys[0][5] == "no" and abs(buys[0][7] - 0.85) < 1e-9      # buy NO at 1 - YES bid
    spent = sum(w[6] * w[7] for w in buys)
    assert spent <= sold * 0.90 + 1                                           # funded only by the proceeds
    assert tr.swap is None and tr.history[-1]["result"] == "done"
    assert reg.owner_of("A") is None and reg.owner_of("B") is None            # handed back
    assert {"A", "B"} <= tr.frozen(clk.t)
    assert v.violations == []


def test_R2_refuses_owned_busy_frozen_or_unbooked_markets(tmp_path):
    for case in ("owned", "busy", "frozen", "nobook", "wrongside"):
        reg, clk, v, tr = make(tmp_path / case)
        setup_book(v)
        if case == "owned":
            reg.claim("fair_band", ["B"])
        elif case == "busy":
            v.orders["x"] = {"order_id": "x", "exchange_id": "A", "side": "yes", "action": "buy", "price": 0.5,
                             "quantity": 1.0, "exp": 1e18}
        elif case == "frozen":
            tr.history.append({"sell_ex": "Z", "buy_ex": "B", "ts": clk.t, "result": "done"})
        elif case == "nobook":
            v.book["B"] = (0.10, 0.20)                                          # 10c spread
        elif case == "wrongside":
            v.pos["B"] = 500.0                                                  # holding YES, proposal buys NO
        out = run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
        assert "claimed" not in out, case
        assert reg.owner_of("A") is None or case == "owned", case


def test_R3_foreign_order_during_probation_aborts_and_releases(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    v.orders["f"] = {"order_id": "f", "exchange_id": "B", "side": "no", "action": "buy", "price": 0.5,
                     "quantity": 5.0, "exp": 1e18}                              # slipped in before the claim
    clk.t += 60
    out = run(tr.tick(None, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    assert out.get("aborted") and not places(v) and reg.owner_of("A") is None and reg.owner_of("B") is None


def test_R4_never_oversells_with_stale_positions_or_lost_replies(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    real = v.place_order
    calls = {"n": 0}

    async def flaky(**kw):
        res = await real(**kw)
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("response lost")
        return res
    v.place_order = flaky
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    stale = dict(v.pos)                                                        # position reads frozen pre-sale
    step_n(tr, v, clk, 12, positions=stale, fills=False)
    assert v.violations == [], v.violations
    assert v.pos["A"] > -1                                                     # never flipped short


def _swap_with_real_cash(tmp_path, base, reserved):
    """Buying power = `base` (cash NOT ours) + what the swap really sold - what it really bought, like the venue."""
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    tr.others_reserved = lambda: reserved
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    low = float("inf")
    for _ in range(14):
        spent = sum(w[6] * w[7] for w in places(v, "buy"))
        bp = base + (3000.0 - v.pos.get("A", 0.0)) * 0.90 - spent
        low = min(low, bp)
        run(tr.tick(None, rows(v), dict(v.pos), v.open_view(), bp, fills_by_order=fbo(v)))
        v.step()
        clk.t += 60
    proceeds = (3000.0 - v.pos.get("A", 0.0)) * 0.90
    spent = sum(w[6] * w[7] for w in places(v, "buy"))
    return tr, v, proceeds, spent, base + proceeds - spent


def test_R5_buy_never_touches_cash_other_strategies_reserved(tmp_path):
    tr, v, proceeds, spent, bp_end = _swap_with_real_cash(tmp_path, base=900.0, reserved=900.0)
    assert proceeds > 500 and spent <= proceeds + 1
    assert bp_end >= 900.0 - 1                       # the others' $900 is still there
    assert v.violations == []


def test_R5b_oversubscribed_other_reserves_do_not_zero_the_buy_leg(tmp_path):
    """2026-10-11 live: others reserved $11.1k vs ~$10k buying power -> every swap sold and bought NOTHING."""
    tr, v, proceeds, spent, bp_end = _swap_with_real_cash(tmp_path, base=8800.0, reserved=11100.0)
    assert spent >= 0.9 * proceeds - tr.cfg.min_order_usd    # the proceeds were re-invested
    assert bp_end >= 8800.0 - 1                              # never dipped into cash that isn't ours
    assert tr.history[-1]["result"] == "done"


def test_R5c_buy_capped_by_real_cash_when_it_is_below_the_proceeds(tmp_path):
    tr, v, proceeds, spent, bp_end = _swap_with_real_cash(tmp_path, base=-600.0, reserved=0.0)
    assert spent <= proceeds - 600.0 + 1                     # only what really exists
    assert bp_end >= -1


def test_R5d_a_swap_that_sold_but_could_not_buy_is_labelled(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    step_n(tr, v, clk, 14, cash=0.0)                         # venue says no cash at all
    assert not places(v, "buy") and tr.history[-1]["result"] == "sold_only"


def test_R6_carve_out_is_the_unspent_proceeds_while_buying(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    seen_buying = False
    for _ in range(12):
        if tr.swap and tr.swap["phase"] == "buying":
            seen_buying = True
            assert tr.unused_budget() == pytest.approx(tr.swap["proceeds"] - tr.swap["spent"], abs=0.01)
        step_n(tr, v, clk, 1)
    assert seen_buying and tr.unused_budget() == 0.0


def test_R7_stop_mid_swap_finishes_the_buy_and_starts_nothing_new(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    step_n(tr, v, clk, 2)
    open(tr.cfg.stop_file, "w").close()
    tr.check_kill()
    step_n(tr, v, clk, 12, prop=PROP)
    assert tr.swap is None and reg.owner_of("A") is None and reg.owner_of("B") is None
    assert len(tr.history) == 1                                               # no second swap after STOP
    assert places(v, "buy")                                                    # proceeds were reinvested, not stranded


def test_R8_daily_swap_cap(tmp_path):
    reg, clk, v, tr = make(tmp_path, max_swaps_per_day=2)
    tr.history = [{"sell_ex": f"X{i}", "buy_ex": f"Y{i}", "ts": clk.t - 100, "result": "done"} for i in range(2)]
    setup_book(v)
    out = run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    assert "claimed" not in out


def test_R9_restart_mid_swap_resumes_and_releases_orphans(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    step_n(tr, v, clk, 2)
    reg.claim(SID, ["ORPHAN"])                                               # owned but not part of the swap
    tr2 = RotationLive(broker=v, ownership=reg, cfg=tr.cfg, clock=clk)
    tr2.load()
    assert tr2.swap and tr2.swap["id"] == tr.swap["id"] and reg.owner_of("ORPHAN") is None
    step_n(tr2, v, clk, 12)
    assert tr2.swap is None and tr2.history[-1]["result"] == "done" and v.violations == []


@pytest.mark.parametrize("seed", list(range(60, 70)))
def test_R10_chaos_stress(tmp_path, seed):
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=rng.random() < 0.5, reject_p=rng.choice([0.0, 0.1, 0.3]),
                           max_swaps_per_day=50, freeze_s=600.0)
    v.fill_p = 0.5
    real = v.place_order

    async def chaotic(**kw):
        res = await real(**kw)
        if kw.get("strategy_id") == SID and rng.random() < 0.1:
            raise TimeoutError("lost")
        return res
    v.place_order = chaotic
    mk = [f"M{i}" for i in range(8)]
    for ex in mk:
        m = rng.choice([0.88, 0.9, 0.12, 0.6])
        v.book[ex] = (round(m - 0.005, 3), round(m, 3))
        v.pos[ex] = rng.choice([0.0, 2500.0, -2500.0])
    stale = dict(v.pos)
    spent_viol = []
    for k in range(240):
        clk.t += 30
        for ex in mk:
            b, a = v.book[ex]
            d = rng.choice([-0.005, 0, 0, 0.005])
            v.book[ex] = (min(max(round(b + d, 3), 0.05), 0.94), min(max(round(a + d, 3), 0.055), 0.945))
            if rng.random() < 0.05:
                v.book[ex] = (v.book[ex][0], min(v.book[ex][0] + 0.06, 0.99))     # occasional blown-out book
        held = [ex for ex in mk if abs(v.pos.get(ex, 0.0)) >= 1]
        prop = None
        if held and rng.random() < 0.5:
            a_ = rng.choice(held)
            b_ = rng.choice([x for x in mk if x != a_])
            side = "no" if v.pos.get(b_, 0.0) < 0 else ("yes" if v.pos.get(b_, 0.0) > 0 else rng.choice(["yes", "no"]))
            prop = {"sell_ex": a_, "sell_qty": v.pos[a_], "buy_ex": b_, "buy_side": side, "advantage": 0.04}
        if rng.random() < 0.6:
            stale = dict(v.pos)
        view = None if rng.random() < 0.05 else v.open_view()
        run(tr.tick(prop, rows(v, size=rng.choice([200.0, 1500.0, 6000.0])), stale, view, rng.choice([1e6, 3000.0]),
                    fills_by_order=None if rng.random() < 0.2 else fbo(v)))
        sold_usd = sum(f["price"] * f["quantity"] for f in v.fills if f["strategy"] == SID and
                       ((f["outcome"] == "yes") == (f["signed_qty"] < 0)))
        bought_usd = sum(f["price"] * f["quantity"] for f in v.fills if f["strategy"] == SID and
                         ((f["outcome"] == "yes") == (f["signed_qty"] > 0)))
        if bought_usd > sold_usd + 50:                 # every buy must be funded by our own sales
            spent_viol.append((k, round(bought_usd), round(sold_usd)))
        v.step()
        if k == 200:
            open(tr.cfg.stop_file, "w").close()
            tr.check_kill()
    v.place_order = real
    v.reject_p, v.fill_p = 0.0, 1.0
    for _ in range(40):
        clk.t += 60
        run(tr.tick(None, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order=fbo(v)))
        v.step()
    assert v.violations == [], v.violations[:5]
    assert spent_viol == [], spent_viol[:3]
    assert tr.swap is None and not reg.owned_by(SID)


def test_R11_wiring_isolation_and_config():
    from sig.engine.ownership import ISOLATED_STRATEGIES
    import sig.config as sc
    assert SID in ISOLATED_STRATEGIES and not MarketOwnership(None).allows(SID, "unowned")
    allowed = sc._SCHEMA["cross_market"]["required"] | sc._SCHEMA["cross_market"]["optional"]
    for k in ("rotation_live_mode", "rotation_live_max_swap_usd", "rotation_live_max_swaps_per_day"):
        assert k in allowed
    import asyncio
    import main_sig
    from sig.engine import ownership as own_mod
    own_mod.set_registry(MarketOwnership(None))
    asyncio.run(main_sig._rotation_live_loop(store=None, broker=None, orch=None, xm_provider=None, cfg_raw={},
                                             stop_event=None))               # off by default: returns at once
    own_mod.set_registry(None)


def _mfills(v, start_idx):
    out = {}
    for f in v.fills[start_idx:]:
        if f["strategy"] != SID:
            continue
        buy = (f["signed_qty"] > 0) if f["outcome"] == "yes" else (f["signed_qty"] < 0)
        d = out.setdefault(f["exchange_id"], {"sold_qty": 0.0, "sold_usd": 0.0, "bought_qty": 0.0, "bought_usd": 0.0})
        k = "bought" if buy else "sold"
        d[k + "_qty"] += abs(f["quantity"])
        d[k + "_usd"] += abs(f["quantity"]) * f["price"]
    return out


@pytest.mark.parametrize("seed", list(range(70, 80)))
def test_R12_fills_path_with_stale_positions_never_overspends(tmp_path, seed):
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, reject_p=rng.choice([0.0, 0.2]))
    setup_book(v)
    v.fill_p = rng.choice([0.4, 1.0])
    start = len(v.fills)
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}, market_fills={}))
    clk.t += 60
    stale = dict(v.pos)
    for _ in range(20):
        if rng.random() < 0.3:
            stale = dict(v.pos)
        run(tr.tick(None, rows(v), stale, v.open_view(), 1e6, fills_by_order=fbo(v), market_fills=_mfills(v, start)))
        v.step()
        clk.t += 60
    m = _mfills(v, start)
    sold = m.get("A", {}).get("sold_usd", 0.0)
    bought = m.get("B", {}).get("bought_usd", 0.0)
    assert bought <= sold + 1 and v.violations == []
    assert v.pos["A"] > -1


def test_R13_loop_end_to_end_decides_claims_sells_buys(tmp_path, monkeypatch):
    """The real _rotation_live_loop: decide_v2 inputs built from venue positions + provider, carve-out registered,
    heartbeat kept, a swap executed (sell low-edge A, buy high-edge B)."""
    import asyncio
    from types import SimpleNamespace
    import main_sig
    from sig.engine import ownership as own_mod
    reg = MarketOwnership(str(tmp_path / "own.json"))
    own_mod.set_registry(reg)
    clk = tfg.Clock()
    v = tfg.Venue(reg, random.Random(1), clk, fill_p=1.0)
    v.book = {"A": (0.900, 0.905), "B": (0.795, 0.800)}      # A: SIG ~ fair (little edge); B: SIG 10c below fair
    v.pos["A"] = 3000.0
    fair = {"A": 0.905, "B": 0.90}

    async def gooa():
        return v.open_view()

    async def gpos(fresh=True):
        return [SimpleNamespace(contract_id=ex, qty=abs(q), side="long" if q > 0 else "short", avg_entry_price=0.5)
                for ex, q in v.pos.items() if abs(q) >= 1]

    async def gacct():
        return SimpleNamespace(buying_power=1e6, equity=90000.0)
    v.get_open_orders_all, v.get_positions, v.get_account = gooa, gpos, gacct

    class Store:
        async def _read(self, sql, params):
            return []

    class XM:
        @property
        def _sig_prices(self):
            from datetime import datetime, timezone
            return {ex: {"best_bid": b, "best_ask": a, "bid_size": 9000, "ask_size": 9000,
                         "updated_at": datetime.now(timezone.utc).isoformat()} for ex, (b, a) in v.book.items()}

        def active_sig_exchange_ids(self):
            return list(v.book)

        def aligned_benchmark(self, ex):
            return fair[ex]

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
        clk.t += 60
        if n["k"] > 10:
            stop.set()
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    monkeypatch.chdir(tmp_path)
    cfg = {"cross_market": {"rotation_live_mode": "live", "rotation_v2_persist_s": 0, "rotation_v2_margin": 0.03}}
    orch = Orch()

    async def go():
        t = asyncio.create_task(main_sig._rotation_live_loop(store=Store(), broker=v, orch=orch, xm_provider=XM(),
                                                             cfg_raw=cfg, stop_event=stop))
        while not stop.is_set():
            await real_sleep(0)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    asyncio.run(go())
    own_mod.set_registry(None)
    assert SID in orch.ledger.external_reserves and reg.lease_ok(SID)
    assert any(w[1] == SID and w[4] == "sell" and w[3] == "A" for w in v.writes if w[2] == "place")
    assert any(w[1] == SID and w[4] == "buy" and w[3] == "B" for w in v.writes if w[2] == "place")
    assert v.violations == []


def test_R14_unknown_book_size_is_not_zero_depth(tmp_path):
    """2026-10-13 live: 1028 had no size data (None) -> read as 0 depth -> 4 zero-size buys -> sold_only."""
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    run(tr.tick(PROP, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    for _ in range(14):
        r = rows(v)
        r["B"]["bid_size"] = r["B"]["ask_size"] = None                  # buy market: sizes unknown
        run(tr.tick(None, r, dict(v.pos), v.open_view(), 1e6, fills_by_order=fbo(v)))
        v.step()
        clk.t += 60
    assert places(v, "buy", "B") and tr.history[-1]["result"] == "done"
    assert v.violations == []


def test_R15_known_zero_depth_still_blocks_and_thin_sources_are_not_proposed():
    from sig.engine.rotation_live import sell_depth_ok
    row = {"best_bid": 0.925, "best_ask": 0.93, "bid_size": 300.0, "ask_size": 2275.0}
    assert not sell_depth_ok(row, 4682.0, 500.0)          # held YES, $277 of bid: not worth a swap slot
    assert not sell_depth_ok(row, -4682.0, 500.0)         # held NO: 2275 x (1-0.93) = $159 < $500
    assert sell_depth_ok(dict(row, bid_size=None), 4682.0, 500.0)   # unknown: allowed
    assert sell_depth_ok(dict(row, bid_size=5000.0), 4682.0, 500.0)


def test_R16_big_cap_only_for_big_edges():
    """Operator 2026-10-16: 10% of NAV per market for rotation buys into >= 7c edges, 5% otherwise."""
    from sig.engine.rotation_v2 import cap_for
    assert cap_for(0.09, 4500.0, 9000.0, 0.07) == 9000.0
    assert cap_for(0.07, 4500.0, 9000.0, 0.07) == 9000.0
    assert cap_for(0.065, 4500.0, 9000.0, 0.07) == 4500.0
    assert cap_for(0.20, 4500.0, None, 0.07) == 4500.0          # off by default


def test_R17_decide_v2_tops_up_a_capped_market_only_under_the_big_cap():
    from sig.engine.rotation import Candidate, Held
    from sig.engine.rotation_v2 import decide_v2
    held = [Held("SRC", 6000.0, 0.90, 0.92, 0.0025, 5400.0),          # low edge holding (2c)
            Held("BIG", 5000.0, 0.85, 0.95, 0.0025, 4400.0)]          # 10c edge, $100 under the 5% cap
    cands = [Candidate("BIG", 0.85, 0.95, 0.0025)]
    kw = dict(persisted={"BIG": 1e9}, persist_s=0.0, cash=0.0, position_size=4500.0, margin=0.03, cost_floor=0.05,
              min_held_value=500.0, topup=True, freeze_s=86400.0, now=0.0)
    assert decide_v2(held, cands, [], **kw) == []                      # 5% cap: $100 room < $200 minimum -> blocked
    sw = decide_v2(held, cands, [], big_position_size=9000.0, big_gap=0.07, **kw)
    assert sw and sw[0].buy.exchange_id == "BIG" and sw[0].sell.exchange_id == "SRC"


def test_R18_swap_sells_only_what_the_buy_market_has_room_for(tmp_path):
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 1.0
    prop = dict(PROP, max_buy_usd=500.0)
    run(tr.tick(prop, rows(v), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    step_n(tr, v, clk, 14)
    sold_usd = (3000.0 - v.pos.get("A", 0.0)) * 0.90
    assert 0 < sold_usd <= 500.0 + 1
    assert tr.history[-1]["result"] == "done"
    reg2, clk2, v2, tr2 = make(tmp_path / "b")
    setup_book(v2)
    out = run(tr2.tick(dict(PROP, max_buy_usd=50.0), rows(v2), dict(v2.pos), v2.open_view(), 1e6, fills_by_order={}))
    assert "claimed" not in out                                         # no room: don't start a swap


def test_R19_freeze_window_is_configurable(tmp_path):
    """Operator 2026-10-17: freeze 24h -> 12h. A market swapped 13h ago is free again; 11h ago still frozen."""
    reg, clk, v, tr = make(tmp_path, freeze_s=12 * 3600.0)
    tr.history = [{"sell_ex": "A", "buy_ex": "B", "ts": clk.t - 13 * 3600, "result": "done"},
                  {"sell_ex": "C", "buy_ex": "D", "ts": clk.t - 11 * 3600, "result": "done"}]
    assert tr.frozen(clk.t) == {"C", "D"}


def test_R20_loop_wires_live_freeze_and_persistence_keys():
    import inspect
    import main_sig
    src = inspect.getsource(main_sig._rotation_live_loop)
    assert 'xm.get("rotation_live_freeze_s"' in src and "freeze_s=lc.freeze_s" in src
    assert 'xm.get("rotation_live_persist_s"' in src


def test_R21_thin_bid_sell_is_not_capped_by_the_touch_and_never_oversells(tmp_path):
    """2026-10-22: low-edge holdings sit behind 33-490 share bids. The sell leg sizes to the swap (not the first
    depth snapshot), rests at the bid, and keeps going as the bid refills; reduce-only throughout."""
    reg, clk, v, tr = make(tmp_path)
    setup_book(v)
    v.fill_p = 0.4
    def thin(r):
        r["A"]["bid_size"] = 300.0                                       # 300 shares at the bid (~$270)
        return r
    run(tr.tick(PROP, thin(rows(v)), dict(v.pos), v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    for _ in range(30):
        run(tr.tick(None, thin(rows(v)), dict(v.pos), v.open_view(), 1e6, fills_by_order=fbo(v)))
        v.step()
        clk.t += 60
    sells = places(v, "sell", "A")
    assert sells and max(w[6] for w in sells) > 300                       # not capped at the 300-share touch
    sold = 3000.0 - v.pos.get("A", 0.0)
    assert sold * 0.90 > 300 and sold * 0.90 <= tr.cfg.max_swap_usd + 1   # more than one touch, within the swap
    assert all(abs(w[7] - 0.90) < 1e-9 for w in sells)                    # never below the bid
    assert v.violations == [] and v.pos["A"] >= 0                         # reduce-only, no oversell


def test_R22_loop_reads_the_min_sell_depth_key():
    import inspect
    import main_sig
    src = inspect.getsource(main_sig._rotation_live_loop)
    assert 'xm.get("rotation_live_min_sell_depth_usd", 200.0)' in src and "sell_depth_ok(r, q, min_sell_depth)" in src


def test_R23_loop_kalshi_fallback_is_wired_and_off_by_default():
    import inspect
    import main_sig
    src = inspect.getsource(main_sig._rotation_live_loop)
    assert 'xm.get("rotation_live_kalshi_fallback", False)' in src
    assert "sh = kal.get(ex)" in src and "if ex in active else None" in src
