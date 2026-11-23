"""Positions must be persisted SIGNED, or a short reads back as a long.

Live failure this pins: ex1077 reached -6045 (long 6045 NO / short 6045 YES, $5,524 cost basis,
27% of the book) entirely unintended. BrokerPositionSnapshot splits magnitude (`qty`, always >= 0)
from direction (`side`), and _sync_venue_positions persisted the MAGNITUDE only. Every consumer
reads `quantity` and ignores `side`, so:

  * the risk gate's sell_without_holding saw held = +6045 > 0 and APPROVED further sells;
  * the MM's _held_yes() saw inventory to sell and kept resting asks.

Each excess sell deepened the short, which was stored positive again -- a feedback loop. The MM
orders show it: every sell exceeded its paired buy (1338>1175, 1386>1219, 1280>1107, 1251>1072),
and the venue lot timestamps match the SELL times exactly.
"""
from __future__ import annotations

import pytest

from sig.engine.orchestrator import _signed_qty


class Snap:
    def __init__(self, qty, side):
        self.qty = qty
        self.side = side


def test_a_short_is_persisted_NEGATIVE():
    """THE fix. A positive short is what authorised the runaway."""
    assert _signed_qty(Snap(6045, "short")) == -6045.0


def test_a_long_stays_positive():
    assert _signed_qty(Snap(6045, "long")) == 6045.0


def test_flat_is_zero():
    assert _signed_qty(Snap(0, "flat")) == 0.0


def test_magnitude_is_always_taken_as_absolute_before_signing():
    """qty is documented as >= 0; a negative qty plus side='short' must not double-negate."""
    assert _signed_qty(Snap(-6045, "short")) == -6045.0
    assert _signed_qty(Snap(-100, "long")) == 100.0


def test_unreadable_input_is_flat_not_an_exception():
    assert _signed_qty(Snap("nonsense", "long")) == 0.0
    assert _signed_qty(Snap(None, "short")) == 0.0
    assert _signed_qty(object()) == 0.0


# --------------------------------------------------------------------------- #
# The two consumers the sign unblocks — both already handle <= 0 correctly
# --------------------------------------------------------------------------- #
def test_the_gate_refuses_a_sell_once_the_position_reads_negative():
    """With a signed quantity the gate's existing guard finally works: held <= 0 => reject."""
    import yaml

    from strategies.risk_gate.gate import RiskGate

    with open("config/sig.yaml", encoding="utf-8") as fh:
        g = RiskGate(config=yaml.safe_load(fh))
    order = {"exchange_id": "1077", "action": "sell", "side": "yes",
             "quantity": 1000, "price": 0.09}

    short = {"balance": 100_000.0, "deployed": 0.0,
             "positions": {"1077": {"quantity": -6045}}}
    res = g.check_order(order, short, None)
    assert not res.approved and "sell_without_holding" in str(res.reason), \
        "a SHORT must not authorise further selling"

    # and the positive case must still work, or we would have broken exits
    long_ = {"balance": 100_000.0, "deployed": 0.0,
             "positions": {"1077": {"quantity": 6045}}}
    assert g.check_order(order, long_, None).approved


def test_the_mm_rests_no_ask_against_a_short():
    """_held_yes returns the signed quantity; the MM clamps ask size to max(0, position)."""
    from sig.engine.live_strategies import _held_yes

    assert _held_yes({"quantity": -6045}) == -6045
    assert max(0, _held_yes({"quantity": -6045})) == 0, "nothing to sell when short"
    assert _held_yes({"quantity": 6045}) == 6045
    assert _held_yes(None) == 0, "no visibility => flat, never a naked short"
