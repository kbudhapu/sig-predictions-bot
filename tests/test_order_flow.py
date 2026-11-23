"""Tests for the order-flow inference strategy (strategies/order_flow/).

Required coverage:
  * flow imbalance computed correctly over the rolling window (+ stale trades pruned);
  * a one-sided extreme is detected;
  * flow that DISAGREES with fair-value produces a FADE;
  * flow that AGREES with informed sources produces NO fade (the key safety test);
  * the informed_flow_detected() market-making hook works.
"""
import asyncio

import pytest

from strategies.order_flow import (
    Trade, FlowConfig, FlowBook, pressure_sign,
    ReferenceView, SignalConfig, OrderFlowEngine,
    QUIET, DUMB, INFORMED, UNCONFIRMED, EDGE_TAG,
)

EX = "1075"


def _buys(n, qty, t0=0.0, dt=1.0, action="buy", side="yes"):
    return [Trade(EX, action, qty, price=0.5, timestamp=t0 + i * dt, side=side) for i in range(n)]


# ── flow imbalance + window ───────────────────────────────────────────────────
def test_pressure_sign_direction():
    assert pressure_sign("buy", "yes") == 1
    assert pressure_sign("sell", "yes") == -1
    assert pressure_sign("sell", "no") == 1      # NO sell pushes YES up
    assert pressure_sign("buy", "no") == -1


def test_imbalance_computed_over_window():
    book = FlowBook(FlowConfig())
    book.ingest_many(_buys(3, 100, t0=0.0), now=5.0)              # up 300
    book.ingest(Trade(EX, "sell", 100, timestamp=4.0), now=5.0)  # down 100
    st = book.state(EX, now=5.0)
    assert st.up_qty == 300 and st.down_qty == 100 and st.total_qty == 400
    assert st.imbalance == pytest.approx(0.5) and st.n_trades == 4


def test_stale_trades_pruned_out_of_window():
    book = FlowBook(FlowConfig(window_seconds=60))
    book.ingest(Trade(EX, "buy", 1000, timestamp=0.0), now=0.0)   # ancient fat print
    book.ingest_many(_buys(3, 50, t0=100.0), now=102.0)           # fresh, inside window
    st = book.state(EX, now=102.0)
    assert st.total_qty == 150                                    # the t=0 trade is pruned out


# ── one-sided extreme detection ───────────────────────────────────────────────
def test_one_sided_extreme_detected():
    book = FlowBook(FlowConfig(extreme_threshold=0.6, min_volume=200, min_trades=3))
    book.ingest_many(_buys(4, 100, t0=0.0), now=4.0)              # imbalance +1.0, 400 contracts
    st = book.state(EX, now=4.0)
    assert st.extreme and st.direction == 1 and st.imbalance == pytest.approx(1.0)


def test_not_extreme_when_thin():
    book = FlowBook(FlowConfig(min_volume=200, min_trades=3))
    book.ingest_many(_buys(2, 50, t0=0.0), now=1.0)              # only 100 contracts / 2 trades
    assert not book.state(EX, now=1.0).extreme


# ── fade when flow DISAGREES with the informed view ───────────────────────────
def test_fade_when_flow_disagrees_with_fair_value():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)              # crowd buying hard → price-up pressure
    view = ReferenceView(fair_divergence=-0.1)                   # fair value says DOWN → crowd is wrong
    sig = eng.signal(EX, view, market_id="m-ks-sen", now=4.0)
    assert sig is not None and sig.classification == DUMB
    assert sig.fade is True and sig.action == "sell" and sig.direction == -1   # trade AGAINST the flow
    assert sig.informed is False


def test_fade_confidence_higher_when_realmoney_agree():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)
    weak = eng.signal(EX, ReferenceView(fair_divergence=-0.1), now=4.0)
    strong = eng.signal(EX, ReferenceView(realmoney_direction=-1, realmoney_agree=True), now=4.0)
    assert strong.fade and weak.fade and strong.confidence > weak.confidence


# ── THE SAFETY TEST: do NOT fade flow that AGREES with informed sources ───────
def test_no_fade_when_flow_agrees_with_informed_realmoney():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)              # crowd buying hard (up)
    view = ReferenceView(realmoney_direction=1, realmoney_agree=True)  # real money ALSO says up → informed
    sig = eng.signal(EX, view, now=4.0)
    assert sig is not None and sig.classification == INFORMED
    assert sig.fade is False                                     # <-- never fade informed flow
    assert sig.informed is True and sig.action is None          # stand aside (emit_follow off by default)


def test_no_fade_when_flow_agrees_with_fair_value():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)             # up
    sig = eng.signal(EX, ReferenceView(fair_divergence=0.1), now=4.0)  # fair value ALSO up → informed
    assert sig.classification == INFORMED and sig.fade is False


def test_extreme_without_reference_is_unconfirmed_no_fade():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)
    sig = eng.signal(EX, None, now=4.0)                          # no informed view supplied
    assert sig.classification == UNCONFIRMED and sig.fade is False and sig.action is None


# ── market-making informed_flow_detected hook ─────────────────────────────────
def test_informed_flow_hook_true_when_informed():
    eng = OrderFlowEngine()
    eng.set_reference(EX, ReferenceView(realmoney_direction=1, realmoney_agree=True))
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)             # extreme up, agrees with real money
    assert eng.informed_flow_detected(EX, now=4.0) is True


def test_informed_flow_hook_false_when_dumb_fadeable():
    eng = OrderFlowEngine()
    eng.set_reference(EX, ReferenceView(fair_divergence=-0.1))   # informed says down; flow up → DUMB
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)
    assert eng.informed_flow_detected(EX, now=4.0) is False      # fadeable crowd, not informed → MM need not pull


def test_informed_flow_hook_false_when_quiet():
    eng = OrderFlowEngine()
    eng.set_reference(EX, ReferenceView(realmoney_direction=1, realmoney_agree=True))
    eng.ingest_many(_buys(1, 50, t0=0.0), now=1.0)              # not extreme
    assert eng.informed_flow_detected(EX, now=1.0) is False


def test_informed_flow_hook_pulls_on_unconfirmed_extreme():
    eng = OrderFlowEngine(cfg=SignalConfig(pull_on_unconfirmed=True))
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)            # extreme, no reference set
    assert eng.informed_flow_detected(EX, now=4.0) is True      # safe default: pull when unsure


# ── output contract + async entrypoint ────────────────────────────────────────
def test_signal_output_contract_shape():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(4, 100, t0=0.0), now=4.0)
    d = eng.signal(EX, ReferenceView(fair_divergence=-0.1), market_id="m1", now=4.0).to_dict()
    assert d["edge"] == EDGE_TAG and d["exchange_id"] == EX and d["market_id"] == "m1"
    assert set(d) >= {"edge", "market_id", "exchange_id", "classification", "fade", "action",
                      "direction", "strength", "confidence", "flow_imbalance", "informed",
                      "flags", "updated_at"}


def test_quiet_market_emits_no_signal():
    eng = OrderFlowEngine()
    eng.ingest_many(_buys(1, 10, t0=0.0), now=1.0)
    assert eng.signal(EX, ReferenceView(fair_divergence=-0.1), now=1.0) is None


def test_async_on_tape_matches_sync():
    eng = OrderFlowEngine()
    sig = asyncio.run(eng.on_tape(_buys(4, 100, t0=0.0), exchange_id=EX,
                                  view=ReferenceView(fair_divergence=-0.1), now=4.0))
    assert sig is not None and sig.fade is True and sig.action == "sell"
