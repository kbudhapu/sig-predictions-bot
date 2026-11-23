"""Regression for the 2026-10-09 live incident: 15 concurrent cross maker entries rested $9.4k against ~$5.4k of
cash cross was allowed to use, because CapitalLedger.reserve() REPLACES a key's reservation and the single-leg
path keyed it by strategy (each concurrent order sized against the full pool)."""
import os
import sys
import uuid

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.broker import OrderResult
from sig.engine import ownership as own_mod
from sig.engine.capital_ledger import CapitalLedger
from sig.engine.composition import SimulatedBroker, build_sim_stack
from sig.engine.ownership import MarketOwnership
from sig.engine.protocols import EdgePerf, Intent


def _cfg() -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def reg(tmp_path):
    r = MarketOwnership(str(tmp_path / "own.json"))
    own_mod.set_registry(r)
    yield r
    own_mod.set_registry(None)


class RestingBroker(SimulatedBroker):
    async def place_order(self, *, strategy_id, market_id, exchange_id, side, action, quantity, price=None,
                          priority=False, outcome=None, **_kw):
        self.placed.append({"exchange_id": exchange_id, "quantity": int(quantity), "price": price})
        oid = f"r-{uuid.uuid4().hex[:8]}"
        self._orders[oid] = {"status": "open", "filled_qty": 0.0, "avg_price": 0.0}
        return OrderResult(idempotency_key=self._key(strategy_id, market_id, side, quantity, price),
                           status="open", order_id=oid, filled_qty=0.0)


class ManyMakers:
    name = "cross_market_divergence"
    tier = "SIGNAL"
    cadence_s = 0.0

    def __init__(self, exs, sigma=0.03):
        self.exs, self.sigma = exs, sigma

    def evaluate(self, state):
        return [Intent(strategy_id=self.name, exchange_id=ex, side="buy", action="open", strategy_type="directional",
                       signal_strength=1.0 - k * 0.01, price=0.5,
                       metadata={"sigma_p": self.sigma, "option_side": "yes", "maker_entry": True, "maker_timeout_s": 600})
                for k, ex in enumerate(self.exs)]

    def on_fill(self, fill):
        pass

    def realized_performance(self):
        return EdgePerf(realized_pnl=0.0, n_trades=0)


async def _stack(exs, cash):
    s = ManyMakers(exs)
    orch, b, allocator, state, oms = build_sim_stack(config=_cfg(), strategy_contracts={s.name: exs[0]},
                                                     buying_power=cash, equity=90_000.0,
                                                     broker=RestingBroker(buying_power=cash, equity=90_000.0))
    orch.register_strategy(s)
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await allocator.refresh()
    return orch, b, oms


@pytest.mark.asyncio
async def test_concurrent_maker_entries_never_commit_more_than_the_cash():
    cash = 1000.0
    orch, b, oms = await _stack([f"M{i}" for i in range(8)], cash)
    for _ in range(4):
        await orch._run_cycle()
    committed = sum(p["quantity"] * p["price"] for p in b.placed)
    assert b.placed, "should place something"
    assert committed <= cash + 1.0, committed                 # old code: each entry sized against the full $1k


@pytest.mark.asyncio
async def test_concurrent_maker_entries_respect_an_external_carve_out():
    cash = 3000.0
    orch, b, oms = await _stack([f"M{i}" for i in range(8)], cash)
    orch.ledger.external_reserves["fair_band"] = lambda: 2000.0   # the pilot's reserve
    for _ in range(4):
        await orch._run_cycle()
    committed = sum(p["quantity"] * p["price"] for p in b.placed)
    assert committed <= cash - 2000.0 + 1.0, committed          # never dips into another strategy's carve-out


@pytest.mark.asyncio
async def test_concurrent_maker_entries_are_capped():
    orch, b, oms = await _stack([f"M{i}" for i in range(12)], 1_000_000.0)
    for _ in range(3):
        await orch._run_cycle()
    assert len({p["exchange_id"] for p in b.placed}) <= orch.MAX_CONCURRENT_MAKER


def test_ledger_reservations_accumulate_across_keys():
    led = CapitalLedger()
    led.begin_cycle(buying_power=1000.0, equity=1000.0)
    assert led.reserve("s|A|1", 700.0) == 700.0
    assert led.reserve("s|B|2", 700.0) == 300.0                  # only what is left
    assert led.available_buying_power() == 0.0



def _live_like_cfg() -> dict:
    """The live overlay's settings that matter here (verified 2026-10-09): placement worker ON, 5% per-market cap,
    4x Kelly for cross_market_divergence, pooled capital."""
    c = _cfg()
    c.setdefault("loops", {})["placement_worker"] = True
    c.setdefault("capital", {}).update({"max_per_market_frac": 0.05, "max_deploy_frac": 1.0, "pool_edge_reserve": 15000,
                                        "pool_eligible": ["sniper", "cross_market_divergence", "dutch_book"]})
    c.setdefault("sizing", {}).setdefault("kelly_multipliers", {})["cross_market_divergence"] = 4.0
    return c


@pytest.mark.asyncio
async def test_live_like_burst_through_the_placement_worker_never_over_commits():
    """Reproduces 2026-10-09 03:06: ~13 cross maker entries in one placement-worker batch, $9,446 cash with $4,001
    carved out for the pilot -> at most ~$5,445 may be committed. The old per-strategy reservation key let each
    entry size against the same cash ($7.2k placed live)."""
    import asyncio as _a
    exs = [f"M{i}" for i in range(13)]
    s = ManyMakers(exs, sigma=0.20)                            # live cross_market_signal sigma_p ~0.2
    cash = 9446.0
    orch, b, allocator, state, oms = build_sim_stack(config=_live_like_cfg(), strategy_contracts={s.name: exs[0]},
                                                     buying_power=cash, equity=89_000.0,
                                                     broker=RestingBroker(buying_power=cash, equity=89_000.0))
    orch.register_strategy(s)
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await allocator.refresh()
    orch.ledger.external_reserves["fair_band"] = lambda: 4001.0
    orch.MAX_CONCURRENT_MAKER = 100 if hasattr(orch, "MAX_CONCURRENT_MAKER") else None   # isolate the LEDGER fix
    await orch._run_cycle()                                   # enqueues the burst
    task = _a.create_task(orch._placement_worker())
    for _ in range(50):
        await _a.sleep(0.02)
        if orch._placement_q.empty():
            break
    await _a.sleep(0.05)
    task.cancel()
    committed = sum(p["quantity"] * p["price"] for p in b.placed)
    assert len(b.placed) >= 2, b.placed
    assert committed <= cash - 4001.0 + 1.0, (committed, len(b.placed))
