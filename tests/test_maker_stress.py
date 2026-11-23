"""MAKER-ENTRY STRESS (2026-10-09, before re-enabling maker entries live).

Real orchestrator + OMS + gate + sizer (SIM stack) with the LIVE settings that caused the incidents (placement
worker ON, 5% per-market cap, 4x Kelly for cross, pooled capital), a fake clock at live pace (~11 cycles/min),
signals appearing / moving / vanishing across 20 markets, a venue that randomly FILLS resting orders and debits real
cash, and a pilot carve-out on the ledger. Invariants checked every cycle:
  1. <= MAX_CONCURRENT_MAKER resting cross orders, never two in one market
  2. resting cross notional <= cash - carve-out  (SIG reserves no cash for resting orders)
  3. cash never negative
  4. status reads (GET /orders/{id}) per simulated minute stay within budget (account limit is 100 reads/min TOTAL)
"""
import asyncio
import os
import random
import sys
import uuid

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.broker import OrderResult
from sig.engine import ownership as own_mod
from sig.engine.composition import SimulatedBroker, build_sim_stack
from sig.engine.ownership import MarketOwnership
from sig.engine.protocols import EdgePerf, Intent

CARVE = 2000.0


def _cfg() -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "sig.yaml"), encoding="utf-8") as fh:
        c = yaml.safe_load(fh)
    c.setdefault("loops", {})["placement_worker"] = True
    c.setdefault("capital", {}).update({"max_per_market_frac": 0.05, "max_deploy_frac": 1.0, "pool_edge_reserve": 15000,
                                        "pool_eligible": ["sniper", "cross_market_divergence", "dutch_book"]})
    c.setdefault("sizing", {}).setdefault("kelly_multipliers", {})["cross_market_divergence"] = 4.0
    return c


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


class FillingVenue(SimulatedBroker):
    """Resting orders; each status read may FILL the order (debiting cash). Counts status reads."""

    def __init__(self, rng, cash, **kw):
        super().__init__(buying_power=cash, equity=90_000.0, **kw)
        self.rng, self.open, self.reads = rng, {}, 0

    async def place_order(self, *, strategy_id, market_id, exchange_id, side, action, quantity, price=None,
                          priority=False, outcome=None, **_kw):
        oid = f"o-{uuid.uuid4().hex[:8]}"
        self.placed.append({"exchange_id": exchange_id, "quantity": int(quantity), "price": price})
        self.open[oid] = {"ex": str(exchange_id), "q": int(quantity), "px": float(price or 0.5)}
        self._orders[oid] = {"status": "open", "filled_qty": 0.0, "avg_price": 0.0}
        return OrderResult(idempotency_key=self._key(strategy_id, market_id, side, quantity, price), status="open",
                           order_id=oid, filled_qty=0.0)

    async def get_order(self, order_id):
        self.reads += 1
        o = self.open.get(order_id)
        if o is not None and self.rng.random() < 0.15:          # a seller hits our resting bid
            self._bp -= o["q"] * o["px"]
            self._orders[order_id].update(status="filled", filled_qty=float(o["q"]), avg_price=o["px"])
            del self.open[order_id]
        return await super().get_order(order_id)

    async def cancel_order(self, order_id, **kw):
        self.open.pop(order_id, None)
        return await super().cancel_order(order_id)


class Signals:
    name = "cross_market_divergence"
    tier = "SIGNAL"
    cadence_s = 30.0

    def __init__(self, rng, markets):
        self.rng, self.markets = rng, markets
        self.live = {m: rng.choice([0.40, 0.45, 0.50, 0.55]) for m in markets if rng.random() < 0.6}

    def evaluate(self, state):
        for m in self.markets:                                  # signals appear / move / vanish
            r = self.rng.random()
            if m in self.live and r < 0.08:
                del self.live[m]
            elif m in self.live and r < 0.20:
                self.live[m] = round(self.live[m] + self.rng.choice([-0.005, 0.005]), 3)
            elif m not in self.live and r < 0.08:
                self.live[m] = self.rng.choice([0.40, 0.45, 0.50, 0.55])
        return [Intent(strategy_id=self.name, exchange_id=m, side="buy", action="open", strategy_type="directional",
                       signal_strength=self.rng.random(), price=px,
                       metadata={"sigma_p": 0.20, "option_side": "yes", "maker_entry": True, "maker_timeout_s": 75.0})
                for m, px in self.live.items()]

    def on_fill(self, fill):
        pass

    def realized_performance(self):
        return EdgePerf(realized_pnl=0.0, n_trades=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", list(range(1, 21)))
async def test_maker_entries_under_stress_keep_every_invariant(tmp_path, monkeypatch, seed):
    own_mod.set_registry(MarketOwnership(str(tmp_path / "own.json")))
    rng = random.Random(seed)
    clk = Clock()
    import sig.oms.manager as mgr
    import sig.engine.orchestrator as orc
    monkeypatch.setattr(mgr.time, "monotonic", clk)             # OMS refresh/backoff timers on the fake clock
    monkeypatch.setattr(orc.time, "monotonic", clk)             # account cache TTL (20s) on the same clock
    cash = 9000.0
    venue = FillingVenue(rng, cash)
    strat = Signals(rng, [f"X{i}" for i in range(20)])
    orch, b, allocator, state, oms = build_sim_stack(config=_cfg(), strategy_contracts={strat.name: "X0"},
                                                     buying_power=cash, equity=90_000.0, broker=venue)
    orch.register_strategy(strat)
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await allocator.refresh()
    orch.ledger.external_reserves["fair_band"] = lambda: CARVE
    worker = asyncio.create_task(orch._placement_worker())
    reads_by_min, viol = {}, []
    try:
        for cyc in range(200):
            clk.t += 5.5                                        # ~11 cycles / minute, as live
            strat.cadence_s = 0.0
            r0 = venue.reads
            await orch._run_cycle()
            for _ in range(20):
                await asyncio.sleep(0)
                if orch._placement_q.empty():
                    break
            await asyncio.sleep(0)
            minute = int(clk.t // 60)
            reads_by_min[minute] = reads_by_min.get(minute, 0) + (venue.reads - r0)
            opens = list(venue.open.values())
            per_mkt = {}
            for o in opens:
                per_mkt[o["ex"]] = per_mkt.get(o["ex"], 0) + 1
            notional = sum(o["q"] * o["px"] for o in opens)
            if len(opens) > orch.MAX_CONCURRENT_MAKER:
                viol.append(("concurrency", cyc, len(opens)))
            if any(v > 1 for v in per_mkt.values()):
                viol.append(("stacked", cyc, per_mkt))
            if notional > venue._bp - CARVE + 1.0:
                viol.append(("over_commit", cyc, round(notional), round(venue._bp)))
            if venue._bp < CARVE - 1.0:                         # fills never eat into another strategy's carve-out
                viol.append(("ate_carve_out", cyc, round(venue._bp)))
            if venue._bp < -1e-6:
                viol.append(("negative_cash", cyc, venue._bp))
    finally:
        worker.cancel()
    assert sum(p["quantity"] * p["price"] for p in venue.placed) >= 6000, "must deploy most of the $7k free cash"
    assert viol == [], viol[:5]
    peak_reads = max(reads_by_min.values())
    assert peak_reads <= 15, (peak_reads, reads_by_min)         # 5 resting entries x 2 refreshes/min (+ slack)


class HostileVenue(FillingVenue):
    """Worst-case SIG: fills happen ANY time (not only when we look), partially; a filled order reads back as
    'canceled' with NO traded quantity (GET /orders/{id} has none); 25% of account reads, 20% of status reads and
    20% of cancels fail with a 429."""

    class RateLimited(Exception):
        pass

    def tick(self):
        for oid, o in list(self.open.items()):
            r = self.rng.random()
            if r < 0.06:                                        # full fill
                q = o["q"]
            elif r < 0.12:                                      # partial fill
                q = max(1, o["q"] // 2)
            else:
                continue
            self._bp -= q * o["px"]
            o["q"] -= q
            if o["q"] <= 0:
                self._orders[oid].update(status="canceled", filled_qty=0.0, avg_price=0.0)   # no traded qty
                del self.open[oid]

    async def get_order(self, order_id):
        self.reads += 1
        if self.rng.random() < 0.20:
            raise self.RateLimited("429")
        return await SimulatedBroker.get_order(self, order_id)

    async def get_account(self):
        if self.rng.random() < 0.25:
            raise self.RateLimited("429")
        return await super().get_account()

    async def cancel_order(self, order_id, **kw):
        if self.rng.random() < 0.20:
            raise self.RateLimited("429")
        return await super().cancel_order(order_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", list(range(1, 21)))
async def test_maker_entries_on_a_hostile_venue_keep_every_invariant(tmp_path, monkeypatch, seed):
    own_mod.set_registry(MarketOwnership(str(tmp_path / "own.json")))
    rng = random.Random(1000 + seed)
    clk = Clock()
    import sig.oms.manager as mgr
    import sig.engine.orchestrator as orc
    monkeypatch.setattr(mgr.time, "monotonic", clk)
    monkeypatch.setattr(orc.time, "monotonic", clk)
    cash = 9000.0
    venue = HostileVenue(rng, cash)
    strat = Signals(rng, [f"X{i}" for i in range(20)])
    orch, b, allocator, state, oms = build_sim_stack(config=_cfg(), strategy_contracts={strat.name: "X0"},
                                                     buying_power=cash, equity=90_000.0, broker=venue)
    orch.register_strategy(strat)
    assert await orch.startup(skip_credential_check=True, skip_eligibility_check=True)
    await allocator.refresh()
    orch.ledger.external_reserves["fair_band"] = lambda: CARVE
    worker = asyncio.create_task(orch._placement_worker())
    reads_by_min, viol = {}, []
    try:
        for cyc in range(300):
            clk.t += 5.5
            strat.cadence_s = 0.0
            venue.tick()
            r0 = venue.reads
            try:
                await orch._run_cycle()
            except HostileVenue.RateLimited:
                pass                                            # a cycle may die on a 429; the next one runs
            for _ in range(20):
                await asyncio.sleep(0)
                if orch._placement_q.empty():
                    break
            await asyncio.sleep(0)
            minute = int(clk.t // 60)
            reads_by_min[minute] = reads_by_min.get(minute, 0) + (venue.reads - r0)
            opens = list(venue.open.values())
            per_mkt = {}
            for o in opens:
                per_mkt[o["ex"]] = per_mkt.get(o["ex"], 0) + 1
            notional = sum(o["q"] * o["px"] for o in opens)
            if len(opens) > orch.MAX_CONCURRENT_MAKER:
                viol.append(("concurrency", cyc, len(opens)))
            if any(v > 1 for v in per_mkt.values()):
                viol.append(("stacked", cyc, per_mkt))
            if notional > venue._bp - CARVE + 1.0:
                viol.append(("over_commit", cyc, round(notional), round(venue._bp)))
            if venue._bp < CARVE - 1.0:
                viol.append(("ate_carve_out", cyc, round(venue._bp)))
    finally:
        worker.cancel()
    assert sum(p["quantity"] * p["price"] for p in venue.placed) >= 6000, "must deploy most of the $7k free cash"
    assert viol == [], viol[:5]
    assert max(reads_by_min.values()) <= 15, reads_by_min
