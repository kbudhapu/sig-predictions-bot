"""2026-10-06: positions-read 429s failed ~20 cycles/hour and let the per-market cap be overshot."""
from __future__ import annotations

import asyncio
import time

import pytest

from sig.broker import SigBroker
from sig.sig_client import SigAPIError
from sig.engine.composition import RiskGateAdapter
from strategies.risk_gate.gate import RiskGate


class _Read:
    def __init__(self):
        self.fail = False

    def portfolio_positions(self, *a):
        if self.fail:
            raise SigAPIError(429, "/portfolio/positions", "rate_limited")
        return {"positions": [{"exchangeId": "1", "quantity": 5}]}


def _broker(read):
    b = SigBroker.__new__(SigBroker)
    b._read = lambda: read
    b._tournament_slug = None
    b._pos_raw_ttl_s = 0.0          # force a real read every call
    return b


def test_positions_429_serves_recent_cache():
    r = _Read()
    b = _broker(r)
    first = b._read_positions_raw()
    r.fail = True
    assert b._read_positions_raw() == first


def test_positions_429_without_cache_still_raises():
    r = _Read()
    r.fail = True
    with pytest.raises(SigAPIError):
        _broker(r)._read_positions_raw()


def test_positions_429_with_too_old_cache_raises():
    r = _Read()
    b = _broker(r)
    b._read_positions_raw()
    b._pos_raw_ts = time.monotonic() - 10_000
    r.fail = True
    with pytest.raises(SigAPIError):
        b._read_positions_raw()


def test_broker_authority_refresh_failure_does_not_raise():
    from sig.oms.fill_handler import _apply_broker_authority_to_state

    class _B:
        async def get_positions(self):
            raise SigAPIError(429, "/portfolio/positions", "rate_limited")

    bundle = type("Bn", (), {"bundle_id": "x", "slices": [], "strategy_id": "s"})()
    asyncio.run(_apply_broker_authority_to_state(bundle, _B(), None))   # must not raise


def _gate(nav=100_000.0):
    g = RiskGate(config={"capital": {"max_deploy_frac": 1.0, "max_per_market_frac": 0.06,
                                     "max_cluster_frac": 0.20},
                         "risk": {"hard_notional_frac": 1.0}})
    return RiskGateAdapter(g, balance=nav)


class _I:
    def __init__(self, ex, qty_px):
        self.exchange_id = ex
        self.side = "buy"
        self.price = qty_px
        self.strategy_id = "cross_market_divergence"
        self.strategy_type = "directional"


def test_pending_buy_counts_against_per_market_cap():
    a = _gate()
    a.set_positions({})
    v1 = a.validate(_I("995", 0.85), 7000, 0)           # 7000×0.85 = $5,950 → clamped to the $6k cap
    assert v1.approved
    q1 = v1.clamped_qty or 7000
    a.note_pending("995", q1 * 0.85)
    v2 = a.validate(_I("995", 0.85), 7000, 0)           # same stale positions view — must see the pending
    assert (not v2.approved) or (v2.clamped_qty or 0) * 0.85 < 100


def test_race_cluster_caps_both_legs_together():
    """Long-D + short-R on one race is ONE bet: the 2nd leg is clamped by the per-race cluster cap."""
    g = RiskGate(config={"capital": {"max_deploy_frac": 1.0, "max_per_market_frac": 0.05,
                                     "max_cluster_frac": 0.06},
                         "risk": {"hard_notional_frac": 1.0}})
    a = RiskGateAdapter(g, balance=100_000.0)
    a.set_market_clusters({"1076": "race:rhode island senate", "1077": "race:rhode island senate"})
    a.set_positions({"1076": {"quantity": 5000, "avg_price": 0.9}})       # $4,500 on the D leg
    v = a.validate(_I("1077", 0.80), 10_000, 0)                           # wants $8k NO on the R leg
    assert v.approved and (v.clamped_qty or 0) * 0.80 <= 1_500 + 1     # only $6k − $4.5k = $1.5k of room
    b = RiskGateAdapter(g, balance=100_000.0)                             # without clusters: per-market only
    b.set_positions({"1076": {"quantity": 5000, "avg_price": 0.9}})
    v2 = b.validate(_I("1077", 0.80), 10_000, 0)
    assert (v2.clamped_qty or 0) * 0.80 > 4_000
