"""MAKER ENTRY MODE of the fresh-gap pilot (2026-10-10) — attack tests. Uses the fake venue of test_fresh_gap_live.

M1 price/expiry; M2 no stacking + resting cash counted; M3 an expired unfilled entry is retried only once the
ingested fills confirm 0 (never without fills data); M4 a filled entry hidden by stale positions is never bought
twice; M5 a dying signal cancels only orders the venue still lists as open; M6 taker fallback after the maker
window; M7 chaos stress in maker mode.
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_fresh_gap_live import SID, make, rows_for, run  # noqa: E402


def fbo(v):
    out = {}
    for f in v.fills:
        out[f["order_id"]] = out.get(f["order_id"], 0.0) + abs(f["quantity"])
    return out


def _maker(tmp_path, **kw):
    reg, clk, v, tr = make(tmp_path, entry_mode="maker", **kw)
    v.fill_p = 0.0                                          # nothing fills unless a test says so
    return reg, clk, v, tr


def _tick(tr, v, past, positions=None, fills=True, sharp=0.90):
    rows = rows_for(v, {ex: sharp for ex in v.book})
    return run(tr.tick(rows, past, dict(v.pos) if positions is None else positions, v.open_view(), 1e6,
                       fills_by_order=fbo(v) if fills else None))


def _places(v, action=None):
    return [w for w in v.writes if w[1] == SID and w[2] == "place" and (action is None or w[4] == action)]


def test_M1_maker_entry_joins_our_side_bid_with_maker_expiry(tmp_path):
    reg, clk, v, tr = _maker(tmp_path)
    v.book = {"1": (0.835, 0.845), "2": (0.155, 0.165)}
    past = {"1": (0.88, 0.90), "2": (0.12, 0.10)}
    rows = {"1": 0.90, "2": 0.10}
    run(tr.tick(rows_for(v, rows), past, {}, v.open_view(), 1e6, fills_by_order={}))
    clk.t += 60
    run(tr.tick(rows_for(v, rows), past, {}, v.open_view(), 1e6, fills_by_order={}))
    buys = {w[3]: w for w in _places(v)}
    assert buys["1"][5] == "yes" and abs(buys["1"][7] - 0.835) < 1e-9          # YES at the YES bid
    assert buys["2"][5] == "no" and abs(buys["2"][7] - 0.835) < 1e-9           # NO at 1 - YES ask (= NO bid)
    for o in v.orders.values():
        assert abs((o["exp"] - clk.t) - tr.cfg.maker_expiry_s) < 1


def test_M2_resting_entry_never_stacks_and_reserves_its_cash(tmp_path):
    reg, clk, v, tr = _maker(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    out = None
    for _ in range(4):
        out = _tick(tr, v, past)
        clk.t += 60
    assert len(_places(v)) == 1 and v.violations == []
    assert out["committed"] >= 0.9 * tr.cfg.trade_usd                            # resting buy counted


def test_M3_expired_unfilled_entry_is_retried_only_once_fills_confirm_zero(tmp_path):
    reg, clk, v, tr = _maker(tmp_path, maker_fallback_after_s=10_000, entry_window_s=10_000)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    _tick(tr, v, past)
    clk.t += 60
    _tick(tr, v, past)                                      # places the maker bid
    clk.t += tr.cfg.maker_expiry_s + 5
    v.step()                                                # expires unfilled
    for _ in range(2):                                      # gone, not resolved yet (< resolve_after_gone_s)
        _tick(tr, v, past)
        clk.t += 60
    assert len(_places(v)) == 1
    clk.t += tr.cfg.resolve_after_gone_s
    _tick(tr, v, past)                                      # fills confirm 0 -> retry
    assert len(_places(v)) == 2
    reg2, clk2, v2, tr2 = _maker(tmp_path / "b", maker_fallback_after_s=10_000, entry_window_s=10_000)
    v2.book = {"1": (0.835, 0.845)}
    _tick(tr2, v2, past, fills=False)
    clk2.t += 60
    _tick(tr2, v2, past, fills=False)
    clk2.t += tr2.cfg.maker_expiry_s + 5
    v2.step()
    for _ in range(4):                                      # no fills data: keep assuming it filled
        _tick(tr2, v2, past, fills=False)
        clk2.t += 60
    assert len(_places(v2)) == 1


def test_M4_filled_entry_with_stale_positions_is_never_bought_twice(tmp_path):
    reg, clk, v, tr = _maker(tmp_path, maker_fallback_after_s=10_000, entry_window_s=10_000)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    _tick(tr, v, past)
    clk.t += 60
    _tick(tr, v, past)
    v.fill_p = 1.0
    v.step()                                                # fully filled at the venue
    stale = {}                                              # ...but our position reads stay FLAT
    for _ in range(6):
        clk.t += 60
        _tick(tr, v, past, positions=stale)
    assert len(_places(v, "buy")) == 1 and v.violations == []


def test_M5_signal_death_cancels_only_orders_still_listed_open(tmp_path):
    reg, clk, v, tr = _maker(tmp_path)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    _tick(tr, v, past)
    clk.t += 60
    _tick(tr, v, past)
    assert len(v.orders) == 1
    v.book["1"] = (0.875, 0.885)                            # gap now 2c: no longer a fresh dislocation
    clk.t += 60
    _tick(tr, v, past)
    assert any(w[2] == "cancel" for w in v.writes if w[1] == SID) and not v.orders
    reg2, clk2, v2, tr2 = _maker(tmp_path / "b")
    v2.book = {"1": (0.835, 0.845)}
    _tick(tr2, v2, past)
    clk2.t += 60
    _tick(tr2, v2, past)
    v2.orders.clear()                                       # vanished at the venue (filled / expired)
    v2.book["1"] = (0.875, 0.885)
    clk2.t += 60
    _tick(tr2, v2, past)
    assert not any(w[2] == "cancel" for w in v2.writes if w[1] == SID)


def test_M6_falls_back_to_taker_after_the_maker_window(tmp_path):
    reg, clk, v, tr = _maker(tmp_path, maker_fallback_after_s=600.0, entry_window_s=1800.0)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    _tick(tr, v, past)
    t0 = clk.t
    clk.t += 60
    _tick(tr, v, past)                                      # maker at 0.835
    while clk.t - t0 < 1500:                                # maker attempts rest out, resolve, then cross
        clk.t += 60
        v.step()
        _tick(tr, v, past)
    prices = [w[7] for w in _places(v)]
    assert abs(prices[0] - 0.835) < 1e-9 and any(abs(p - 0.845) < 1e-9 for p in prices[1:])


@pytest.mark.parametrize("seed", list(range(40, 50)))
def test_M7_chaos_stress_maker_mode(tmp_path, seed):
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=rng.random() < 0.5, reject_p=rng.choice([0.0, 0.1, 0.3]),
                           budget_usd=7000.0, trade_usd=2000.0, max_open=4, entry_mode="maker")
    v.fill_p = 0.25
    real_place = v.place_order

    async def chaotic_place(**kw):
        res = await real_place(**kw)
        if kw.get("strategy_id") == SID and rng.random() < 0.1:
            raise TimeoutError("response lost")
        return res
    v.place_order = chaotic_place
    exs = [str(i) for i in range(10)]
    fair = {ex: rng.choice([0.85, 0.9, 0.12, 0.6]) for ex in exs}
    mid24 = {ex: fair[ex] - 0.02 if fair[ex] > 0.5 else fair[ex] + 0.02 for ex in exs}
    mids = dict(mid24)
    stale_pos = dict(v.pos)
    for k in range(260):
        clk.t += 30
        for ex in exs:
            r = rng.random()
            if r < 0.05:
                mids[ex] += (-0.04 if fair[ex] > mids[ex] else 0.04)
            elif r < 0.3:
                mids[ex] += 0.3 * (mid24[ex] - mids[ex])
            mids[ex] = min(max(round(mids[ex] * 200) / 200, 0.05), 0.95)
            v.book[ex] = (round(mids[ex] - 0.005, 3), round(mids[ex] + 0.005, 3))
        if rng.random() < 0.6:
            stale_pos = dict(v.pos)
        view = None if rng.random() < 0.05 else v.open_view()
        run(tr.tick(rows_for(v, fair, sizes=rng.choice([150.0, 800.0, 4000.0])),
                    {ex: (mid24[ex], fair[ex]) for ex in exs}, stale_pos, view, rng.choice([1e6, 2500.0]),
                    fills_by_order=None if rng.random() < 0.2 else fbo(v)))
        v.step()
        if k == 190:
            open(tr.cfg.stop_file, "w").close()
            tr.check_kill(0.0)
    v.place_order = real_place
    v.reject_p, v.fill_p = 0.0, 1.0
    for _ in range(60):
        clk.t += 60
        run(tr.tick(rows_for(v, fair), {ex: (mid24[ex], fair[ex]) for ex in exs}, dict(v.pos), v.open_view(), 1e6,
                    fills_by_order=fbo(v)))
        v.step()
    assert v.violations == [], v.violations[:5]
    assert all(abs(v.pos.get(ex, 0.0)) < 1 for ex in exs)
    assert not reg.owned_by(SID) and not tr.trades
    assert len(_places(v, "buy")) >= 1


# ================================================================ MAKER EXITS (2026-10-11, off by default)
def _held_position(tmp_path, **kw):
    reg, clk, v, tr = make(tmp_path, entry_mode="taker", exit_mode="maker", **kw)
    v.book = {"1": (0.835, 0.845)}
    past = {"1": (0.88, 0.90)}
    v.fill_p = 1.0
    for _ in range(3):                                      # claim, enter (taker), fill
        _tick(tr, v, past)
        v.step()
        clk.t += 60
    assert v.pos["1"] > 0
    v.fill_p = 0.0
    return reg, clk, v, tr, past


def test_X1_profitable_exit_rests_at_our_ask(tmp_path):
    reg, clk, v, tr, past = _held_position(tmp_path)
    v.book["1"] = (0.875, 0.885)                            # gap closed -> 'closed' exit
    _tick(tr, v, past)
    sells = _places(v, "sell")
    assert sells and abs(sells[-1][7] - 0.885) < 1e-9       # YES sold at the YES ASK, not the bid
    o = list(v.orders.values())[-1]
    assert abs((o["exp"] - clk.t) - tr.cfg.maker_expiry_s) < 1


def test_X2_flip_and_wind_down_always_sell_at_the_bid(tmp_path):
    reg, clk, v, tr, past = _held_position(tmp_path / "flip")
    v.book["1"] = (0.905, 0.915)                            # SIG now ABOVE Poly: flipped -> risk exit
    _tick(tr, v, past)
    assert abs(_places(v, "sell")[-1][7] - 0.905) < 1e-9
    reg2, clk2, v2, tr2, past2 = _held_position(tmp_path / "wd")
    open(tr2.cfg.stop_file, "w").close()
    tr2.check_kill(0.0)
    _tick(tr2, v2, past2)
    assert abs(_places(v2, "sell")[-1][7] - v2.book["1"][0]) < 1e-9


def test_X3_unfilled_maker_exit_crosses_after_the_fallback(tmp_path):
    reg, clk, v, tr, past = _held_position(tmp_path, maker_exit_fallback_s=600.0)
    v.book["1"] = (0.875, 0.885)
    t0 = clk.t
    _tick(tr, v, past)
    while clk.t - t0 < 1300:
        clk.t += 60
        v.step()
        _tick(tr, v, past)
    prices = [w[7] for w in _places(v, "sell")]
    assert abs(prices[0] - 0.885) < 1e-9 and any(abs(p - 0.875) < 1e-9 for p in prices[1:])
    assert v.violations == []


def test_X4_default_exit_mode_is_taker(tmp_path):
    from sig.engine.fresh_gap_live import FreshGapLiveConfig
    assert FreshGapLiveConfig().exit_mode == "taker"


@pytest.mark.parametrize("seed", list(range(80, 90)))
def test_X5_chaos_stress_maker_entries_and_exits(tmp_path, seed):
    rng = random.Random(seed)
    reg, clk, v, tr = make(tmp_path, seed=seed, lag=rng.random() < 0.5, reject_p=rng.choice([0.0, 0.1, 0.3]),
                           budget_usd=7000.0, trade_usd=2000.0, max_open=4, entry_mode="maker", exit_mode="maker")
    v.fill_p = 0.25
    real_place = v.place_order

    async def chaotic_place(**kw):
        res = await real_place(**kw)
        if kw.get("strategy_id") == SID and rng.random() < 0.1:
            raise TimeoutError("response lost")
        return res
    v.place_order = chaotic_place
    exs = [str(i) for i in range(10)]
    fair = {ex: rng.choice([0.85, 0.9, 0.12, 0.6]) for ex in exs}
    mid24 = {ex: fair[ex] - 0.02 if fair[ex] > 0.5 else fair[ex] + 0.02 for ex in exs}
    mids = dict(mid24)
    stale_pos = dict(v.pos)
    for k in range(260):
        clk.t += 30
        for ex in exs:
            r = rng.random()
            if r < 0.05:
                mids[ex] += (-0.04 if fair[ex] > mids[ex] else 0.04)
            elif r < 0.3:
                mids[ex] += 0.3 * (mid24[ex] - mids[ex])
            mids[ex] = min(max(round(mids[ex] * 200) / 200, 0.05), 0.95)
            v.book[ex] = (round(mids[ex] - 0.005, 3), round(mids[ex] + 0.005, 3))
        if rng.random() < 0.6:
            stale_pos = dict(v.pos)
        view = None if rng.random() < 0.05 else v.open_view()
        run(tr.tick(rows_for(v, fair, sizes=rng.choice([150.0, 800.0, 4000.0])),
                    {ex: (mid24[ex], fair[ex]) for ex in exs}, stale_pos, view, rng.choice([1e6, 2500.0]),
                    fills_by_order=None if rng.random() < 0.2 else fbo(v)))
        v.step()
        if k == 190:
            open(tr.cfg.stop_file, "w").close()
            tr.check_kill(0.0)
    v.place_order = real_place
    v.reject_p, v.fill_p = 0.0, 1.0
    for _ in range(60):
        clk.t += 60
        run(tr.tick(rows_for(v, fair), {ex: (mid24[ex], fair[ex]) for ex in exs}, dict(v.pos), v.open_view(), 1e6,
                    fills_by_order=fbo(v)))
        v.step()
    assert v.violations == [], v.violations[:5]
    assert all(abs(v.pos.get(ex, 0.0)) < 1 for ex in exs)
    assert not reg.owned_by(SID) and not tr.trades
    assert len(_places(v, "buy")) >= 1


