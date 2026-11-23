"""MM quotes batched into ONE POST /orders/multi-leg call.

WHY, measured against the live venue 2026-10-02:

    10 independent legs, ONE multi-leg call : 1.47s  (0.15s/order)
    10 serial POST /orders                  : 5.30s  (0.53s/order)

The speed is not the point. Serial placement is ~0.5s/order, so latency allows ~120 orders/min while
the WRITE BUDGET allows only 30/min — we are rate-limit bound. Multi-leg spends ONE write for up to
ten orders, so the same budget carries ~10x the quotes.

The constraints that shape the grouping, all verified against the venue or the doc:
  • max 10 legs per call;
  • duplicate legs for the same exchange -> 400, NOTHING placed;
  • ALL-OR-NOTHING: every leg is validated before any executes. An off-tick price returned
    400 with 0 orders placed and named the offending leg (`index: 4`).
"""
from __future__ import annotations

import pytest
import yaml

from sig.engine.orchestrator import Orchestrator, _snap_to_tick


class _Intent:
    is_multi_leg = False

    def __init__(self, exchange_id, strategy_type="market_making", strategy_id="market_making",
                 price=0.5, qty=100):
        self.exchange_id = str(exchange_id)
        self.strategy_type = strategy_type
        self.strategy_id = strategy_id
        self.price = price
        self.side = "buy"
        self.action = "open"
        self.metadata = {"mm_quote_qty": qty, "option_side": "yes"}


class _Strat:
    def __init__(self, name="market_making"):
        self.name = name


def _grouper(cap):
    o = object.__new__(Orchestrator)
    o._placement_batch_max_legs = cap
    return Orchestrator._group_for_multileg.__get__(o)


def _fresh(intents, strat=None):
    st = strat or _Strat()
    return [(0.0, i, st) for i in intents]


# --------------------------------------------------------------------------- #
# Tick snapping — one off-tick leg kills the whole group
# --------------------------------------------------------------------------- #
def test_prices_snap_to_the_sig_tick_grid():
    assert _snap_to_tick(0.0037) == 0.005        # the exact value the venue rejected
    assert _snap_to_tick(0.5) == 0.5
    assert _snap_to_tick(0.5024) == 0.5
    assert _snap_to_tick(0.5026) == 0.505


def test_snapping_clamps_to_the_tradeable_band():
    assert _snap_to_tick(0.0) == 0.005
    assert _snap_to_tick(-1.0) == 0.005
    assert _snap_to_tick(1.0) == 0.995
    assert _snap_to_tick(42.0) == 0.995


def test_every_snapped_price_is_on_the_grid():
    for i in range(1, 1000):
        p = _snap_to_tick(i / 1000.0)
        assert abs((p / 0.005) - round(p / 0.005)) < 1e-9, f"{p} is off-tick"
        assert 0.005 <= p <= 0.995


# --------------------------------------------------------------------------- #
# Grouping rules
# --------------------------------------------------------------------------- #
def test_mm_quotes_group_up_to_the_leg_cap():
    groups, singles = _grouper(10)(_fresh([_Intent(1000 + i) for i in range(25)]))
    assert [len(g[1]) for g in groups] == [10, 10, 5]
    assert singles == []


def test_no_group_exceeds_ten_legs():
    """SIG's hard cap. A group of 11 would be rejected outright."""
    groups, _ = _grouper(10)(_fresh([_Intent(2000 + i) for i in range(100)]))
    assert groups and all(len(g[1]) <= 10 for g in groups)


def test_a_duplicate_exchange_starts_a_NEW_group():
    """Duplicate legs for one exchange return 400 and place NOTHING, so they must never share a
    group — e.g. a bid and an ask on the same market."""
    ids = ["1", "2", "1", "2"]
    groups, singles = _grouper(10)(_fresh([_Intent(e) for e in ids]))
    for _strat, itns in groups:
        exs = [i.exchange_id for i in itns]
        assert len(exs) == len(set(exs)), f"duplicate exchange inside one group: {exs}"
    all_ex = [i.exchange_id for _s, g in groups for i in g] + [i.exchange_id for i, _s in singles]
    assert sorted(all_ex) == sorted(ids), "an intent was dropped by grouping"


def test_arb_and_multileg_intents_are_never_grouped():
    """Arb has its own atomic all-fill-or-unwind semantics; it must keep them."""
    arb = _Intent("1", strategy_type="arb")
    ml = _Intent("2")
    ml.is_multi_leg = True
    mm = [_Intent(100 + i) for i in range(3)]
    groups, singles = _grouper(10)(_fresh([arb, ml] + mm))
    grouped = [i for _s, g in groups for i in g]
    assert arb not in grouped and ml not in grouped
    assert (arb, singles[0][1]) in [(i, s) for i, s in singles] or arb in [i for i, _ in singles]


def test_directional_and_order_flow_are_never_grouped():
    """They use chase / accept-partial fill policies, where sharing a bundle would change how a
    partial fill is handled. Only leave_resting MM quotes are safe to batch."""
    others = [_Intent("1", strategy_type="directional"),
              _Intent("2", strategy_type="order_flow"),
              _Intent("3", strategy_type="resolution")]
    groups, singles = _grouper(10)(_fresh(others))
    assert groups == []
    assert len(singles) == 3


def test_batching_disabled_sends_everything_single():
    for cap in (0, 1):
        groups, singles = _grouper(cap)(_fresh([_Intent(1000 + i) for i in range(5)]))
        assert groups == []
        assert len(singles) == 5


def test_a_lone_mm_quote_is_a_single_not_a_group_of_one():
    """A one-leg multi-leg call would spend the same write for no benefit."""
    groups, singles = _grouper(10)(_fresh([_Intent("1")]))
    assert groups == []
    assert len(singles) == 1


def test_no_intent_is_lost_or_duplicated_by_grouping():
    intents = [_Intent(1000 + i) for i in range(23)] + \
              [_Intent("5", strategy_type="arb"), _Intent("6", strategy_type="directional")]
    groups, singles = _grouper(10)(_fresh(intents))
    out = [id(i) for _s, g in groups for i in g] + [id(i) for i, _s in singles]
    assert sorted(out) == sorted(id(i) for i in intents)
    assert len(out) == len(set(out)), "an intent was duplicated"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_live_config_batches_within_the_venue_cap():
    loops = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["loops"]
    n = int(loops["placement_batch_max_legs"])
    assert 0 <= n <= 10, "SIG caps multi-leg at 10 legs"
    if n > 1:
        assert loops["placement_worker"] is True, (
            "batching happens in the placement worker's drain path — it does nothing with the "
            "worker off")


# --------------------------------------------------------------------------- #
# End-to-end wiring: the group really becomes ONE multi-leg submit
# --------------------------------------------------------------------------- #
class _OMS:
    is_hard_locked = False

    def __init__(self):
        self.submits = []

    async def has_pending_bundle(self, sid):
        return False

    async def submit(self, *, strategy_id, legs, fill_policy=None, metadata=None):
        self.submits.append({"strategy_id": strategy_id, "legs": legs, "metadata": metadata})
        return object()


class _Gate:
    def __init__(self):
        self.writes = []

    def is_allowed(self, stype):
        return True

    def record_write(self, stype):
        self.writes.append(stype)


class _RiskGate:
    def validate(self, intent, qty, reserved):
        class V:
            approved = True
            clamped_qty = None
            reason = ""
        return V()


class _Alloc:
    def budget_for(self, sid):
        return 1_000_000.0


def _orch_for_group(cap=10):
    from sig.engine.capital_ledger import CapitalLedger
    o = object.__new__(Orchestrator)
    o._placement_batch_max_legs = cap
    o._oms = _OMS()
    o._write_gate = _Gate()
    o._risk_gate = _RiskGate()
    o._allocator = _Alloc()
    o._ledger = CapitalLedger()
    o._ledger.begin_cycle(buying_power=1_000_000.0, equity=1_000_000.0)
    o._stale_markets = set()
    o._market_is_stale = Orchestrator._market_is_stale.__get__(o)
    o._process_mm_quote_group = Orchestrator._process_mm_quote_group.__get__(o)
    return o


@pytest.mark.asyncio
async def test_a_group_becomes_ONE_submit_with_all_legs_and_ONE_write():
    """The whole lever: 10 quotes must cost ONE write, not ten."""
    o = _orch_for_group()
    intents = [_Intent(1000 + i, price=0.5, qty=10) for i in range(10)]
    n = await o._process_mm_quote_group(intents, _Strat())
    assert n == 10
    assert len(o._oms.submits) == 1, "a group must be exactly one bundle"
    assert len(o._oms.submits[0]["legs"]) == 10
    assert len(o._write_gate.writes) == 1, (
        f"10 quotes consumed {len(o._write_gate.writes)} writes — the 30/min budget gain is the "
        "entire point of batching")


@pytest.mark.asyncio
async def test_group_legs_are_tick_snapped_before_submit():
    """One off-tick leg returns 400 and places NOTHING, so the group must be snapped first."""
    o = _orch_for_group()
    intents = [_Intent("1", price=0.0037, qty=10), _Intent("2", price=0.5026, qty=10)]
    await o._process_mm_quote_group(intents, _Strat())
    prices = [l["price"] for l in o._oms.submits[0]["legs"]]
    assert prices == [0.005, 0.505], f"legs not snapped to the 0.005 grid: {prices}"


@pytest.mark.asyncio
async def test_the_group_reserves_its_RUNNING_TOTAL_not_just_the_last_leg():
    """MM intents all share one strategy_id and CapitalLedger.reserve() REPLACES a strategy's prior
    reservation — so the per-intent path left the ledger holding only the LAST quote's commitment.
    The group must reserve the real total."""
    o = _orch_for_group()
    intents = [_Intent(1000 + i, price=0.5, qty=10) for i in range(5)]   # 5 x (10 x 0.5) = 25.0
    await o._process_mm_quote_group(intents, _Strat())
    assert o._ledger.reserved_total == pytest.approx(25.0), (
        f"reserved {o._ledger.reserved_total}, expected the 25.0 total — not one leg's 5.0")


@pytest.mark.asyncio
async def test_nothing_submitted_releases_the_reservation():
    """A group where every leg sizes to zero must not leave capital reserved."""
    o = _orch_for_group()
    intents = [_Intent(1000 + i, price=0.5, qty=0) for i in range(4)]    # qty 0 => no legs
    n = await o._process_mm_quote_group(intents, _Strat())
    assert n == 0
    assert o._oms.submits == []
    assert o._ledger.reserved_total == 0.0, "capital left reserved for a group that placed nothing"


@pytest.mark.asyncio
async def test_a_stale_market_leg_is_dropped_from_the_group():
    o = _orch_for_group()
    o._stale_markets = {"1001"}
    intents = [_Intent(1000 + i, price=0.5, qty=10) for i in range(3)]
    n = await o._process_mm_quote_group(intents, _Strat())
    assert n == 2
    assert [l["contract_id"] for l in o._oms.submits[0]["legs"]] == ["1000", "1002"]
