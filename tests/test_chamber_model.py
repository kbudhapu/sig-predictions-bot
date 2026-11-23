"""ChamberModel — the factual baselines must be EXACT; the model must stay in a prove-first posture.

Two very different kinds of test here, matching the two layers:

  * baselines: hard assertions on verified 2026 facts and on the control arithmetic. A wrong
    "seats needed for control" corrupts every downstream number, so these are pinned to the
    published figures AND re-derived independently inside the test.
  * the model: assertions that it is SMALL, LOGGED and NOT TRADEABLE by default. Nothing here
    asserts the model is correct — it is a hypothesis, and tests cannot make it true.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from strategies.chamber_model import (
    DEMOCRATIC,
    REPUBLICAN,
    BaselineError,
    ChamberModelConfig,
    JointStructureEdge,
    load_baselines,
    load_chamber_model_config,
    poisson_binomial,
    rho_sensitivity,
    seat_distribution,
)
from strategies.chamber_model.baselines import _ROOT, load_baselines as _lb

BASE = load_baselines()
CFG = load_chamber_model_config()


def _raw() -> dict:
    with open("config/chamber_baselines.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _sig_cfg() -> dict:
    with open("config/sig.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# PART 1 — the factual baselines
# --------------------------------------------------------------------------- #
def test_senate_composition_is_the_verified_2026_balance():
    s = BASE.senate
    assert s.total_seats == 100
    assert s.composition == {"republican": 53, "democratic": 45, "independent": 2}
    assert sum(s.composition.values()) == 100


def test_senate_seats_up_counts_match_the_roster():
    s = BASE.senate
    assert s.seats_up_count() == 35, "33 regular Class 2 + 2 Class 3 specials (FL, OH)"
    assert s.seats_up_count(REPUBLICAN) == 22
    assert s.seats_up_count(DEMOCRATIC) == 13
    assert s.seats_up_count(REPUBLICAN) + s.seats_up_count(DEMOCRATIC) == 35
    specials = [x.state for x in s.seats_up if x.seat_class == "special"]
    assert sorted(specials) == ["florida", "ohio"]


def test_seats_not_up_completes_the_chamber():
    """not_up + up must equal each party's whole holding, or the chamber does not add to 100."""
    s = BASE.senate
    assert s.seats_not_up[REPUBLICAN] == 53 - 22 == 31
    assert s.seats_not_up[DEMOCRATIC] == (45 + 2) - 13 == 34
    assert s.seats_not_up[REPUBLICAN] + s.seats_not_up[DEMOCRATIC] + 35 == 100


def test_the_vp_tiebreak_makes_the_thresholds_ASYMMETRIC():
    """THE easiest fact to get wrong. The VP breaks ties and is a Republican, so 50-50 is
    REPUBLICAN control. A single shared "51" threshold would score the single most
    probability-dense outcome for the wrong party."""
    s = BASE.senate
    assert s.majority_seats == 51
    assert s.vp_party == REPUBLICAN
    assert s.control_threshold[REPUBLICAN] == 50
    assert s.control_threshold[DEMOCRATIC] == 51
    assert s.control_threshold[REPUBLICAN] != s.control_threshold[DEMOCRATIC]


def test_control_threshold_math_senate():
    """Re-derive the published figures from the seat structure alone.

    D: holds 34 regardless, needs 51 => 17 of the 35 up-seats; holds 13 of them => net +4.
    R: holds 31 regardless, needs 50 (VP) => 19 of the 35; holds 22 => can lose 3.
       Against a STRICT 51 (what published sources quote) R needs 20 => can lose 2.
    """
    s = BASE.senate
    assert s.seats_needed_from_up(DEMOCRATIC) == 51 - 34 == 17
    assert s.net_gain_needed(DEMOCRATIC) == 17 - 13 == +4
    assert s.can_lose(DEMOCRATIC) == 0

    assert s.seats_needed_from_up(REPUBLICAN) == 50 - 31 == 19
    assert s.net_gain_needed(REPUBLICAN) == 19 - 22 == -3
    assert s.can_lose(REPUBLICAN) == 3, "with the VP tiebreak"
    # the strict-majority figure the sources publish
    assert (51 - 31) == 20 and 22 - 20 == 2, "R can lose 2 against a strict 51"
    assert int(_raw()["senate"]["republican_can_lose"]) == 2

    assert s.can_reach_control(REPUBLICAN) and s.can_reach_control(DEMOCRATIC)


def test_published_net_gain_figures_agree_with_the_derivation():
    """The file states the headline numbers; the loader re-derives them. Disagreement == typo."""
    raw = _raw()
    assert int(raw["senate"]["democratic_net_gain_for_control"]) == BASE.senate.net_gain_needed(DEMOCRATIC)
    assert int(raw["house"]["democratic_net_gain_for_control"]) == 3


def test_house_baseline():
    h = BASE.house
    assert h.total_seats == 435
    assert h.majority_seats == 218
    assert h.composition == {"republican": 218, "democratic": 214, "independent": 1, "vacant": 2}
    assert sum(h.composition.values()) == 435
    # No VP in the House: 218 for either party, symmetric.
    assert h.control_threshold[REPUBLICAN] == h.control_threshold[DEMOCRATIC] == 218


def test_house_independent_caucus_is_flagged_unverified_not_guessed():
    """Guessing it would shift a 218-vs-217 control call, so it must stay UNVERIFIED."""
    assert _raw()["house"]["independent_caucuses_with"] == "UNVERIFIED"


def test_governors_have_no_control_threshold():
    """SIG lists governor markets, but there is no 'control of the governorships' contest — so
    governors get same-race pairs only and NO joint-control structure."""
    g = BASE.governors
    assert g.has_control_market is False
    assert _raw()["governors"]["control_threshold"] is None
    assert g.seats_up_count() == 36
    assert g.seats_up_count(REPUBLICAN) == 18 and g.seats_up_count(DEMOCRATIC) == 18
    assert _raw()["governors"]["nationwide_before"] == {"republican": 26, "democratic": 24}


def test_open_seats_are_identified():
    """Open (no incumbent on the ballot) behaves differently from a defended seat, so the status
    is carried rather than just the party."""
    s = BASE.senate
    opens = {x.state for x in s.open_seats()}
    # retirements, lost renominations, and the barred Oklahoma appointee
    assert {"kentucky", "illinois", "michigan", "minnesota", "new hampshire",
            "north carolina", "iowa", "alabama"} <= opens
    assert "texas" in opens and "louisiana" in opens, "lost renomination == open"
    assert "oklahoma" in opens, "appointee barred from the full term == open"
    assert s.seat_for("maine").is_open is False, "Collins is defending"
    assert s.seat_for("MAINE") is not None, "state lookup must be case-insensitive"


def test_oklahoma_reflects_the_post_cutoff_resignation():
    """Mullin resigned in March 2026 for DHS; appointee Armstrong is barred from the full term.
    Recalling this from training data would have recorded Mullin as the running incumbent."""
    ok = BASE.senate.seat_for("oklahoma")
    assert ok.status == "open_appointee_barred" and ok.is_open
    assert ok.party == REPUBLICAN


# --------------------------------------------------------------------------- #
# Validation — the loader must REFUSE inconsistent facts
# --------------------------------------------------------------------------- #
def _write(tmp_path, mutate) -> str:
    raw = _raw()
    mutate(raw)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(p)


def test_loader_rejects_a_composition_that_does_not_fill_the_chamber(tmp_path):
    path = _write(tmp_path, lambda r: r["senate"]["composition"].__setitem__("republican", 54))
    with pytest.raises(BaselineError) as e:
        _lb(path)
    assert any("composition sums" in m or "seats_not_up" in m for m in e.value.args[0])


def test_loader_rejects_a_party_count_that_contradicts_the_roster(tmp_path):
    path = _write(tmp_path, lambda r: r["senate"]["seats_up_by_party"].__setitem__("republican", 21))
    with pytest.raises(BaselineError) as e:
        _lb(path)
    assert any("seats_up_by_party" in m for m in e.value.args[0])


def test_loader_rejects_a_duplicated_seat(tmp_path):
    def dup(r):
        r["senate"]["seats_up"].append(dict(r["senate"]["seats_up"][0]))
        r["senate"]["seats_up_total"] = 36
    path = _write(tmp_path, dup)
    with pytest.raises(BaselineError) as e:
        _lb(path)
    assert any("duplicate" in m for m in e.value.args[0])


def test_loader_rejects_losing_the_vp_asymmetry(tmp_path):
    """If someone 'simplifies' both thresholds to 51, the load must fail loudly."""
    path = _write(tmp_path, lambda r: r["senate"]["control_threshold"].__setitem__("republican", 51))
    with pytest.raises(BaselineError) as e:
        _lb(path)
    assert any("vp_breaks_ties" in m or "majority-1" in m for m in e.value.args[0])


def test_loader_rejects_a_stale_published_net_gain(tmp_path):
    path = _write(tmp_path, lambda r: r["senate"].__setitem__("democratic_net_gain_for_control", 5))
    with pytest.raises(BaselineError) as e:
        _lb(path)
    assert any("net_gain" in m for m in e.value.args[0])


# --------------------------------------------------------------------------- #
# PART 2 — SIG coverage (measured, recorded)
# --------------------------------------------------------------------------- #
def test_recorded_sig_coverage_is_internally_consistent():
    cov = BASE.coverage
    assert cov["senate"]["baseline_up_seats"] == BASE.senate.seats_up_count() == 35
    assert cov["senate"]["sig_markets"] == 34
    assert cov["senate"]["missing_from_sig"] == ["ohio"]
    assert cov["governors"]["baseline_up_seats"] == BASE.governors.seats_up_count() == 36
    assert cov["house"]["sig_markets"] == 46 and cov["house"]["total_seats"] == 435
    assert cov["control_markets"] == 4


def test_three_way_races_are_control_indeterminate():
    """A listed Independent breaks the two-way outcome space, and a winning independent's effect
    on control depends on who they caucus with — not knowable in advance."""
    assert BASE.senate.indeterminate_states == frozenset({"montana", "nebraska"})
    assert BASE.governors.indeterminate_states == frozenset({"rhode island"})


# --------------------------------------------------------------------------- #
# PART 3 — the model: small, logged, not trusted
# --------------------------------------------------------------------------- #
def _prices(chamber, r_hold=0.70, d_hold=0.30) -> dict[str, float]:
    """P(Republican wins) for each up-seat, from the party that holds it."""
    return {s.state: (r_hold if s.party == REPUBLICAN else d_hold) for s in chamber.seats_up}


def test_poisson_binomial_is_a_proper_distribution():
    pmf = poisson_binomial([0.3, 0.5, 0.9])
    assert sum(pmf) == pytest.approx(1.0)
    assert len(pmf) == 4
    assert pmf[3] == pytest.approx(0.3 * 0.5 * 0.9)
    assert pmf[0] == pytest.approx(0.7 * 0.5 * 0.1)


def test_seat_distribution_preserves_the_marginals():
    """Whatever rho does to the JOINT law, each race's marginal must be unchanged — that is the
    defining property of a copula and the one thing here that is provable."""
    ms = [0.2, 0.5, 0.8, 0.65]
    for rho in (0.0, 0.3, 0.9):
        d = seat_distribution(ms, rho=rho)
        assert sum(d.seats_won_pmf) == pytest.approx(1.0, abs=1e-6)
        assert d.expected_seats == pytest.approx(sum(ms), abs=1e-3), f"rho={rho}"


def test_higher_rho_fattens_the_tails():
    """The mechanism that makes rho load-bearing: correlation moves probability to the extremes
    without moving the mean."""
    ms = [0.5] * 20
    lo = seat_distribution(ms, rho=0.0)
    hi = seat_distribution(ms, rho=0.9)
    assert lo.expected_seats == pytest.approx(hi.expected_seats, abs=1e-3)
    assert hi.seats_won_pmf[0] + hi.seats_won_pmf[-1] > lo.seats_won_pmf[0] + lo.seats_won_pmf[-1]


def test_rho_materially_changes_the_answer_which_is_WHY_this_is_a_hypothesis():
    """Identical prices, different rho, different control probability. If this test ever showed a
    narrow spread, the model would be safe to trust — it does not."""
    sens = rho_sensitivity(BASE.senate, REPUBLICAN, race_prices=_prices(BASE.senate))
    spread = max(sens.values()) - min(sens.values())
    assert spread > 0.05, f"rho spread {spread:.3f} — the output is substantially a claim about rho"


def test_config_is_tail_tier_and_small_by_default():
    """The brief's core requirement: not wired to large sizing on day one."""
    assert CFG.tier == "TAIL"
    assert CFG.enabled_for_sizing is False
    assert CFG.max_size_multiplier <= 0.05
    assert CFG.min_observations_before_sizing >= 200
    edges = _sig_cfg()["allocator"]["edges"]
    assert edges["joint_structure"]["tier"] == "TAIL"
    assert edges["joint_structure"]["prior"] <= 0.02
    assert edges["joint_structure"]["floor"] == 0.0, "no capital floor for an unproven edge"


def test_config_refuses_to_promote_the_edge_by_editing_one_number():
    """A hard ceiling in CODE: making this edge large requires a reviewed code change."""
    base = dict(_sig_cfg()["chamber_model"])
    with pytest.raises(ValueError, match="max_size_multiplier"):
        ChamberModelConfig.from_cfg({"chamber_model": dict(base, max_size_multiplier=0.5)})
    with pytest.raises(ValueError, match="tier"):
        ChamberModelConfig.from_cfg({"chamber_model": dict(base, tier="CORE")})
    with pytest.raises(ValueError, match="min_observations_before_sizing"):
        ChamberModelConfig.from_cfg({"chamber_model": dict(base, min_observations_before_sizing=0)})


def test_signal_is_produced_and_LOGGED_even_when_untradeable():
    """Logged-before-sized: the record must land for degraded and refused signals too — those are
    the ones you need in order to evaluate the edge at all."""
    logged: list[dict] = []

    class Spy:
        def info(self, event, **kw):
            logged.append({"event": event, **kw})

    edge = JointStructureEdge(BASE, CFG, logger=Spy())
    sig = edge.emit("senate", REPUBLICAN, race_prices=_prices(BASE.senate),
                    market_control_price=0.62)
    assert edge.observation_count == 1
    assert len(logged) == 1 and logged[0]["event"] == "joint_structure_signal"
    assert logged[0]["hypothesis"] is True
    assert "rho_sensitivity" in logged[0], "the log must carry the sensitivity, not just the point"
    assert sig.to_json()


def test_signal_is_not_tradeable_and_sizes_to_zero_by_default():
    edge = JointStructureEdge(BASE, CFG)
    sig = edge.emit("senate", REPUBLICAN, race_prices=_prices(BASE.senate),
                    market_control_price=0.62)
    assert sig.tradeable is False
    assert sig.size_multiplier == 0.0, "a consumer ignoring .tradeable still cannot size it"
    assert sig.tier == "TAIL" and sig.hypothesis is True


def test_divergence_is_still_computed_and_directional_while_untradeable():
    """The signal must be a real, scoreable claim — otherwise there is nothing to prove later."""
    edge = JointStructureEdge(BASE, CFG)
    sig = edge.emit("senate", REPUBLICAN, race_prices=_prices(BASE.senate),
                    market_control_price=0.95)
    assert sig.divergence is not None and sig.divergence < 0
    assert sig.direction == "sell"
    assert sig.would_have_been_right(party_won_control=False) is True
    assert sig.would_have_been_right(party_won_control=True) is False


def test_observation_floor_blocks_sizing_even_if_the_master_switch_is_on():
    """Two independent brakes: flipping enabled_for_sizing alone must not release sizing."""
    cfg = ChamberModelConfig.from_cfg(
        {"chamber_model": dict(_sig_cfg()["chamber_model"], enabled_for_sizing=True)})
    edge = JointStructureEdge(BASE, cfg)
    sig = edge.emit("house", REPUBLICAN, race_prices={}, market_control_price=0.55)
    assert sig.tradeable is False
    assert "observation_floor" in sig.reason or sig.reason.startswith("degraded")


def test_an_unpriced_seat_degrades_the_estimate():
    """SIG lists no Ohio Senate special, so control cannot be honestly priced: the missing seat is
    exactly as decisive as any other."""
    px = _prices(BASE.senate)
    px.pop("ohio")
    edge = JointStructureEdge(BASE, CFG)
    sig = edge.emit("senate", REPUBLICAN, race_prices=px, market_control_price=0.62)
    assert sig.tradeable is False
    assert "unpriced_seats" in sig.reason or "ohio" in str(sig.detail["missing_states"])


def test_scorecard_reports_unproven_and_never_self_promotes():
    edge = JointStructureEdge(BASE, CFG)
    for _ in range(3):
        edge.emit("senate", REPUBLICAN, race_prices=_prices(BASE.senate), market_control_price=0.95)
    card = edge.scorecard({("senate", "republican"): False})
    assert card["observations"] == 3 and card["scored"] == 3 and card["correct"] == 3
    assert card["hit_rate"] == 1.0
    assert card["proven"] is False, "code must never promote this edge; a human does that"
