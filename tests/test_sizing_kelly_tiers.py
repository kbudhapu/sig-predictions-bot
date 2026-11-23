"""Per-tier Kelly aggressiveness (catch-up posture): the directional sizer scales k(σ_p) by the edge's
PROVENNESS, so a proven edge sizes UP (toward full/double Kelly) while an unproven one stays small — and
the risk caps still bind on top (Kelly-up within caps, never removing caps).

Pins:
  • a PROVEN edge (cross_market_divergence ×4) sizes ~4× an UNPROVEN one (order_flow ×1) at the same σ_p;
  • the fraction ceiling binds (a ×12 multiplier still clamps to kelly_frac_ceiling, not 3× budget);
  • an unproven edge is capped at a FULL budget (never doubled);
  • the gate's per-market cap still clamps a huge sized quantity (the backstop is intact).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
import yaml

from sig.engine.composition import SizingAdapter, RiskGateAdapter
from sig.engine.protocols import Intent
from strategies.sizing.sizing import Sizer
from strategies.risk_gate.gate import RiskGate

CONFIG_PATH = "config/sig.yaml"
EQUITY = 100_000.0


def _cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _intent(edge: str, *, price=0.5, sigma_p=0.06, strategy_type="directional") -> Intent:
    # kelly_multipliers is keyed by edge IDENTITY (strategy_id). Production directional edges carry
    # strategy_type='directional' with a DISTINCT strategy_id (e.g. 'cross_market_divergence'), so the
    # intent here mirrors that: strategy_id=<edge>, strategy_type='directional'. (A prior version set
    # strategy_type==strategy_id, which hid the type-vs-id lookup bug that under-sized cross-market 4×.)
    return Intent(strategy_id=edge, exchange_id="X", side="buy", action="open",
                  strategy_type=strategy_type, price=price, metadata={"sigma_p": sigma_p})


# third_place_threshold=None ⇒ Browne multiplier m is None ⇒ treated as 1.0 (deterministic: isolates the
# per-tier Kelly multiplier from the behind-boost, which is exercised separately via the ceiling test).
def _adapter(cfg) -> SizingAdapter:
    return SizingAdapter(Sizer(cfg), balance=EQUITY, third_place_threshold=None)


def test_config_has_kelly_multipliers_and_ceiling():
    s = _cfg()["sizing"]
    assert s["kelly_multipliers"]["cross_market_divergence"] == 4.0   # PROVEN → full Kelly
    assert s["kelly_multipliers"]["order_flow"] == 1.0                # UNPROVEN → quarter
    assert float(s["kelly_frac_ceiling"]) == 2.0


def test_proven_edge_sizes_up_vs_unproven():
    """Same σ_p, same budget: cross_market (×4, proven) targets ~4× the order_flow (×1, unproven) size."""
    a = _adapter(_cfg())
    budget, avail = 10_000.0, 1_000_000.0
    proven = a.size(_intent("cross_market_divergence"), avail, budget)
    unproven = a.size(_intent("order_flow"), avail, budget)
    assert proven.qty > 0 and unproven.qty > 0
    # base k≈0.25 at σ_p=0.06; proven ×4 ⇒ frac 1.0 (full budget), unproven ×1 ⇒ frac 0.25 (quarter)
    assert proven.qty == 4 * unproven.qty
    assert abs(proven.capital_commitment - budget) < 1.0          # proven deploys the full edge budget
    assert abs(unproven.capital_commitment - 0.25 * budget) < 1.0  # unproven stays quarter


def test_unproven_default_stays_quarter():
    """An edge not listed falls to `default` (×1) → stays quarter-Kelly, not sized up."""
    a = _adapter(_cfg())
    d = a.size(_intent("some_new_unvalidated_edge"), 1_000_000.0, 10_000.0)
    assert abs(d.capital_commitment - 0.25 * 10_000.0) < 1.0


def test_fraction_ceiling_binds_even_at_huge_multiplier():
    """A ×12 multiplier WOULD target 3× budget (k=0.25×12=3.0), but kelly_frac_ceiling=2.0 clamps it to
    2× — the ceiling binds, so 'double Kelly' is the most a proven edge targets, never runaway."""
    cfg = copy.deepcopy(_cfg())
    cfg["sizing"]["kelly_multipliers"] = {"huge": 12.0, "default": 1.0}
    cfg["sizing"]["kelly_frac_ceiling"] = 2.0
    a = _adapter(cfg)
    budget = 10_000.0
    d = a.size(_intent("huge"), 1_000_000.0, budget)
    assert abs(d.capital_commitment - 2.0 * budget) < 1.0         # clamped to 2× (ceiling), not 3×


def test_unproven_never_doubles_even_when_behind():
    """Deeply behind (big Browne m), an UNPROVEN edge (×1) is still capped at a FULL budget (frac 1.0),
    never doubled — only proven edges may exceed one budget."""
    a = SizingAdapter(Sizer(_cfg()), balance=20_000.0, third_place_threshold=500_000.0,
                      time_fraction_remaining=0.1)   # far behind, little time ⇒ large m
    m = a._sizer.browne_multiplier(
        __import__("strategies.sizing.sizing", fromlist=["Standing"]).Standing(
            balance=20_000.0, third_place_threshold=500_000.0, time_fraction_remaining=0.1))
    assert m is not None and m > 4.0, f"expected a large behind-multiplier, got {m}"
    budget = 10_000.0
    unproven = a.size(_intent("order_flow"), 1_000_000.0, budget)   # ×1 → ceiling 1.0
    proven = a.size(_intent("cross_market_divergence"), 1_000_000.0, budget)  # ×4 → ceiling 2.0
    assert abs(unproven.capital_commitment - 1.0 * budget) < budget * 0.01   # capped at full budget
    assert abs(proven.capital_commitment - 2.0 * budget) < budget * 0.01     # proven reaches double
    assert proven.qty == 2 * unproven.qty


def test_gate_per_market_cap_still_binds_on_a_huge_size():
    """Kelly-up within caps: a huge directional quantity is still clamped by the gate's per-market cap."""
    cfg = _cfg()
    gate = RiskGateAdapter(RiskGate(config=cfg), balance=EQUITY)
    gate.set_positions({})                              # flat book
    # qty whose notional (80k × 0.5 = $40k) exceeds the per-market cap ($15k) but stays under the
    # erroneous-order ceiling ($100k) and available capital — so the gate CLAMPS it rather than rejects.
    huge = 80_000
    verdict = gate.validate(_intent("cross_market_divergence", price=0.5), huge, EQUITY)
    assert verdict.approved
    per_market_cap_notional = float(cfg["capital"]["max_per_market_frac"]) * EQUITY
    max_qty = per_market_cap_notional / 0.5
    assert verdict.clamped_qty is not None
    assert verdict.clamped_qty <= max_qty + 1          # clamped to the per-market cap, not the huge ask
    assert verdict.clamped_qty < huge
