"""Funding planner/executor (2026-10-08): free a fixed amount of cash at least EV cost, and never more."""
import asyncio
import random

import pytest

from sig.broker import OrderResult
from sig.engine.funding import FundingLimits, execute_plan, held_levels, plan_funding, recheck_order


def run(c):
    return asyncio.run(c)


def bk(bid, ask, bq=1000, aq=1000, deeper=()):
    return {"bids": [(bid, bq)] + [(p, q) for p, q in deeper if p < bid], "asks": [(ask, aq)]}


def test_never_sells_below_fair_minus_ev_cap_or_mid_minus_one_tick():
    positions = {"A": 1000.0, "B": 1000.0, "C": 1000.0}
    books = {"A": bk(0.90, 0.905), "B": bk(0.50, 0.51), "C": bk(0.60, 0.605)}
    fairs = {"A": 0.93, "B": 0.60, "C": 0.61}                     # B: 10c below fair -> forbidden at 5c cap
    p = plan_funding(positions, books, fairs, 100000.0)
    exs = {o["ex"] for o in p["orders"]}
    assert "B" not in exs and exs == {"A", "C"}
    for o in p["orders"]:
        lf, mid = fairs[o["ex"]], (books[o["ex"]]["bids"][0][0] + books[o["ex"]]["asks"][0][0]) / 2
        assert o["limit"] >= lf - 0.05 - 1e-9 and o["limit"] >= mid - 0.005 - 1e-9


def test_cheapest_per_dollar_first_and_hard_cap():
    positions = {"A": 5000.0, "C": 5000.0}
    books = {"A": bk(0.90, 0.905, bq=10_000), "C": bk(0.60, 0.605, bq=10_000)}
    fairs = {"A": 0.92, "C": 0.62}                                # same 2c gap: cheaper per $ at 0.90
    p = plan_funding(positions, books, fairs, 1000.0)
    assert [o["ex"] for o in p["orders"]] == ["A"]
    assert 1000.0 <= p["proceeds"] < 1000.0 + 0.90 + 1e-9         # never more than one share over the target
    assert p["orders"][0]["qty"] == 1112


def test_no_leg_uses_trim_convention_and_asks():
    # held NO (negative YES qty): NO sells at 1 - YES ask, sized by YES ask depth
    lv = held_levels(-500.0, {"bids": [(0.10, 9999)], "asks": [(0.12, 300), (0.13, 400)]})
    assert lv == [(0.88, 300.0), (0.87, 400.0)]
    p = plan_funding({"N": -500.0}, {"N": {"bids": [(0.11, 999)], "asks": [(0.12, 300), (0.13, 400)]}},
                     {"N": 0.10}, 1e9)
    o = p["orders"][0]
    assert o["outcome"] == "no" and o["qty"] == 300 and o["limit"] == 0.88   # 0.87 is > 1 tick below NO mid


def test_respects_position_size_depth_exclusions_and_missing_data():
    positions = {"A": 50.0, "X": 1000.0, "O": 1000.0, "F": 1000.0}
    books = {"A": bk(0.90, 0.905, bq=10_000), "X": bk(0.9, 0.905), "O": {"bids": [(0.9, 10)], "asks": []}}
    p = plan_funding(positions, books, {"A": 0.91, "X": 0.91, "O": 0.91}, 1e9, exclude={"X"})
    assert [o["ex"] for o in p["orders"]] == ["A"] and p["orders"][0]["qty"] == 50   # F no book, O one-sided


@pytest.mark.parametrize("seed", range(30))
def test_random_plans_never_break_bounds(seed):
    rng = random.Random(seed)
    positions, books, fairs = {}, {}, {}
    for i in range(25):
        ex = str(i)
        q = rng.choice([-1, 1]) * rng.randint(0, 6000)
        bid = round(rng.randint(2, 196) * 0.005, 3)
        ask = round(bid + 0.005 * rng.randint(1, 4), 3)
        positions[ex] = float(q)
        books[ex] = {"bids": [(bid, rng.randint(0, 5000)), (round(bid - 0.005, 3), rng.randint(0, 5000))],
                     "asks": [(ask, rng.randint(0, 5000)), (round(ask + 0.005, 3), rng.randint(0, 5000))]}
        if rng.random() < 0.9:
            fairs[ex] = min(max((bid + ask) / 2 + rng.uniform(-0.08, 0.08), 0.01), 0.99)
    need = rng.uniform(100, 20000)
    lim = FundingLimits()
    p = plan_funding(positions, books, fairs, need, lim)
    assert p["proceeds"] <= need + 1.0 + 1e-6
    for o in p["orders"]:
        q, b, f = positions[o["ex"]], books[o["ex"]], fairs[o["ex"]]
        mid = (max(x for x, _ in b["bids"]) + min(x for x, _ in b["asks"])) / 2
        lf, lm = (f, mid) if q > 0 else (1 - f, 1 - mid)
        assert (o["outcome"] == "yes") == (q > 0)
        assert o["qty"] <= abs(q)
        assert o["limit"] >= lf - lim.max_ev_cost - 1e-9 and o["limit"] >= lm - lim.max_mark_cost - 1e-9
        assert o["qty"] <= sum(n for px, n in held_levels(q, b) if px >= o["limit"] - 1e-9)


def test_recheck_skips_anything_that_moved():
    o = {"ex": "A", "outcome": "yes", "qty": 1000, "limit": 0.90}
    good = bk(0.90, 0.905)
    assert recheck_order(o, 1000.0, good, 0.92) == (1000, "ok")
    assert recheck_order(o, 1000.0, good, 0.96)[1] == "limit_now_breaks_cost_bounds"    # fair jumped
    assert recheck_order(o, 1000.0, bk(0.95, 0.955), 0.92)[1] == "limit_now_breaks_cost_bounds"  # mid ran away
    assert recheck_order(o, 300.0, good, 0.92) == (300, "ok")                           # position shrank
    assert recheck_order(o, -10.0, good, 0.92)[1] == "position_gone_or_flipped"
    assert recheck_order(o, 1000.0, bk(0.89, 0.895), 0.92)[1] == "no_depth_at_limit"    # bid fell below limit
    assert recheck_order(o, 1000.0, good, None)[1] == "no_fresh_fair"


class FakeBroker:
    def __init__(self, fill_frac=1.0, status="open", oid=True):
        self.fill_frac, self.status, self.oid = fill_frac, status, oid
        self.placed, self.cancels, self.orders = [], [], {}

    async def place_order(self, **kw):
        self.placed.append(kw)
        if self.status == "rejected":
            return OrderResult(idempotency_key="", status="rejected")
        oid = f"o{len(self.placed)}" if self.oid else None
        if oid:
            self.orders[oid] = {"q": kw["quantity"], "px": kw["price"], "st": "open", "ex": kw["exchange_id"]}
        return OrderResult(idempotency_key="", status="unknown" if not oid else "open", order_id=oid)

    async def get_order(self, oid):
        o = self.orders[oid]
        f = o["q"] * self.fill_frac
        st = o["st"] if o["st"] != "open" else ("filled" if self.fill_frac >= 1 else "open")
        return OrderResult(idempotency_key="", status=st, order_id=oid, filled_qty=f, avg_price=o["px"])

    async def cancel_order(self, oid, *, strategy_id=None):
        self.cancels.append((oid, strategy_id))
        self.orders[oid]["st"] = "canceled"
        return OrderResult(idempotency_key="", status="canceled", order_id=oid)


def _ex(plan, br, need, pos=None, book=None, fair=0.92):
    held = dict(pos or {})

    async def rp(ex):                                            # position reflects what the venue filled
        sold = sum(o["q"] * br.fill_frac for oid, o in br.orders.items() if o["ex"] == ex)
        return held.get(ex, 10_000.0) - sold

    async def rb(ex):
        return book or bk(0.90, 0.905, bq=100_000)

    async def rf(ex):
        return fair

    async def nosleep(s):
        return None
    return run(execute_plan(plan, broker=br, read_book=rb, read_position=rp, read_fair=rf, need_usd=need,
                            sleep=nosleep, wait_s=10, poll_s=5))


PLAN = {"orders": [{"ex": "A", "outcome": "yes", "qty": 2000, "limit": 0.90},
                   {"ex": "B", "outcome": "yes", "qty": 2000, "limit": 0.90}]}


def test_execute_stops_at_target_and_never_oversells():
    br = FakeBroker()
    rep = _ex(PLAN, br, need=900.0)
    assert len(br.placed) == 1 and br.placed[0]["quantity"] == 1000 and rep["proceeds"] == 900.0
    p = br.placed[0]
    assert p["side"] == "sell" and p["strategy_id"] == "fund_pilot" and p["price"] == 0.90
    assert p["market_id"].startswith("A#")                       # unique idempotency scope


def test_partial_fill_is_cancelled_and_never_replaced():
    br = FakeBroker(fill_frac=0.4)
    rep = _ex({"orders": PLAN["orders"][:1]}, br, need=5000.0)
    assert len(br.placed) == 1 and br.cancels == [("o1", "fund_pilot")]
    assert abs(rep["proceeds"] - 0.4 * 2000 * 0.90) < 1e-9


def test_ambiguous_placement_stops_everything():
    br = FakeBroker(oid=False)
    rep = _ex(PLAN, br, need=5000.0)
    assert rep["stopped"] and len(br.placed) == 1


def test_rejected_and_moved_orders_are_skipped_not_retried():
    br = FakeBroker(status="rejected")
    rep = _ex(PLAN, br, need=5000.0)
    assert len(br.placed) == 2 and rep["proceeds"] == 0.0
    br2 = FakeBroker()
    rep2 = _ex(PLAN, br2, need=5000.0, fair=0.99)                 # fair ran away -> limit now too cheap
    assert br2.placed == [] and all(r[1] == "limit_now_breaks_cost_bounds" for r in rep2["skipped"])


def test_spills_to_the_next_market_when_depth_runs_out():
    p = plan_funding({"A": 5000.0, "C": 5000.0}, {"A": bk(0.90, 0.905, bq=1000), "C": bk(0.60, 0.605)},
                     {"A": 0.92, "C": 0.62}, 1000.0)
    a, c = p["orders"][0], p["orders"][1]
    assert (a["ex"], a["qty"]) == ("A", 1000) and c["ex"] == "C" and 1000 <= p["proceeds"] < 1000.6


def test_fund_tool_refuses_unless_live_pilot_carve_out_covers_proceeds(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone
    from tools.fund_pilot import pilot_carve_out_ok
    p = tmp_path / "ticks.jsonl"
    now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)

    def tick(**kw):
        row = {"ts": (now - timedelta(seconds=30)).isoformat(), "winding_down": False, "halt": None,
               "carve_out": 10000.0, **kw}
        p.write_text(json.dumps(row) + "\n")
    assert pilot_carve_out_ok(8000, str(tmp_path / "missing.jsonl"), now=now)[0] is False
    tick()
    assert pilot_carve_out_ok(8000, str(p), now=now) == (True, "ok")
    tick(carve_out=5000.0)
    assert pilot_carve_out_ok(8000, str(p), now=now)[0] is False          # carve-out smaller than the sale
    tick(winding_down=True, halt="loss_limit_fills -800")
    assert pilot_carve_out_ok(8000, str(p), now=now)[0] is False
    tick(ts=(now - timedelta(minutes=10)).isoformat())
    assert pilot_carve_out_ok(8000, str(p), now=now)[0] is False          # loop not running



def test_unconfirmed_cancel_stops_the_run():
    class Refuse(FakeBroker):
        async def cancel_order(self, oid, *, strategy_id=None):
            self.cancels.append((oid, strategy_id))
            return OrderResult(idempotency_key="", status="rejected", order_id=oid)
    br = Refuse(fill_frac=0.4)
    rep = _ex(PLAN, br, need=5000.0)
    assert rep["stopped"].startswith("cancel_unconfirmed") and len(br.placed) == 1   # no more selling after it


def test_fill_measured_from_position_not_order_read():
    br = FakeBroker(fill_frac=1.0)
    rep = _ex({"orders": PLAN["orders"][:1]}, br, need=5000.0)
    assert abs(rep["proceeds"] - 2000 * 0.90) < 1e-9 and rep["sold"][0]["qty"] == 2000
