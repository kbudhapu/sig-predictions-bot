"""Leg-type shadow (2026-10-06): tag cross-market signals by leg and record them — never change intents."""
from __future__ import annotations

import json

from sig.engine.cross_market_provider import LoggingStrategyWrapper
from sig.engine.protocols import Intent
from strategies.cross_market.strategy import leg_type
from tools.leg_shadow_validate import score


def test_leg_type():
    assert leg_type("no", 0.12) == "longshot-NO"
    assert leg_type("yes", 0.88) == "fav-YES"
    assert leg_type("no", 0.88) == "fav-NO"
    assert leg_type("yes", 0.12) == "longshot-YES"


def _intent(ex, side, sig, lt):
    return Intent(strategy_id="cross_market_divergence", exchange_id=ex, side="buy", action="open",
                  strategy_type="directional", signal_strength=0.5, price=0.5,
                  metadata={"option_side": side, "leg_type": lt, "sig_price": sig, "external_fair": 0.1,
                            "divergence": -0.06, "limit_price": 0.84, "race": "r"})


class _Inner:
    name = "cross_market_divergence"

    def __init__(self, intents):
        self.intents = intents

    def evaluate(self, state):
        return self.intents


def test_wrapper_records_deduped_and_passes_intents_through(tmp_path):
    p = tmp_path / "leg" / "signals.jsonl"
    ints = [_intent("1", "no", 0.16, "longshot-NO"), _intent("2", "yes", 0.80, "fav-YES")]
    w = LoggingStrategyWrapper(_Inner(ints), leg_record_path=str(p))
    assert w.evaluate(None) == ints                     # untouched
    assert w.evaluate(None) == ints                     # second cycle: deduped, nothing new written
    recs = [json.loads(l) for l in p.read_text().splitlines()]
    assert [(r["exchange_id"], r["leg_type"], r["no_filter_skip"]) for r in recs] == \
        [("1", "longshot-NO", False), ("2", "fav-YES", True)]
    assert w.name == "cross_market_divergence"           # still delegates


def test_wrapper_off_by_default_writes_nothing(tmp_path):
    w = LoggingStrategyWrapper(_Inner([_intent("1", "no", 0.16, "longshot-NO")]))
    w.evaluate(None)
    assert list(tmp_path.iterdir()) == []


def test_score_held_side_and_gap_closed():
    rec = {"option_side": "no", "sig_price": 0.16, "divergence": -0.06}     # sharp 0.10, SIG 0.16, bought NO
    mv, cl = score(rec, mid1=0.13, sharp1=0.10, half_spread=0.0025)
    assert abs(mv - 0.025) < 1e-9 and abs(cl - 0.5) < 1e-9                  # +3c − 0.5c; half the gap closed
