"""Relationship-engine tests.

Fixtures mirror the REAL payload shapes, taken from MARKET_RECON.md and sig/sig_client.py:

  /markets data[]        -> {id, title, categories, exchanges:[{id}], isComposite, isMultiOutcome,
                             settlementDate, status}   -- NOTE: no state field, no party field
  /exchanges/prices      -> {data:[{exchangeId, bestBid, bestAsk, spread}]}
  /exchanges/{id}/orderbook -> {bids:[{price,quantity}], asks:[...], bestBid, bestAsk}

Where a test needs plausible numbers it uses the ACTUAL recon quotes, so the no-arb baseline is the
real market rather than an invented one.
"""

from __future__ import annotations

import pytest

from strategies.relationship_engine import (
    ChamberModel,
    HARD_ARB,
    SOFT_MISPRICING,
    build_relationship_graph,
    find_hard_arbs,
    find_soft_mispricings,
    implied_control_probability,
    parse_title,
    reconcile_with_published,
)

# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #
_NEXT_ID = [1000]


def market(title: str, exchange_id: int | None = None, category: str = "Governor") -> dict:
    """One /markets row in the real shape."""
    if exchange_id is None:
        _NEXT_ID[0] += 1
        exchange_id = _NEXT_ID[0]
    return {
        "id": 300 + exchange_id,
        "title": title,
        "thumbnailUrl": None,
        "status": "open",
        "createdAt": "2026-09-20T00:00:00.000Z",
        "settlementDate": "2026-11-04T17:00:00.000Z",
        "settledWith": None,
        "settledOn": None,
        "categories": [{"name": category}],
        "isComposite": False,
        "isMultiOutcome": False,
        "exchanges": [{"id": exchange_id}],
        "creator": None,
        "contexts": [],
    }


def prices(**by_exchange: tuple[float | None, float | None]) -> dict:
    """prices(**{"1001": (bid, ask)}) -> /exchanges/prices envelope."""
    return {
        "data": [
            {
                "exchangeId": int(ex),
                "bestBid": bid,
                "bestAsk": ask,
                "spread": (None if bid is None or ask is None else round(ask - bid, 4)),
            }
            for ex, (bid, ask) in by_exchange.items()
        ]
    }


def ex_of(mkt: dict) -> int:
    return mkt["exchanges"][0]["id"]


# --------------------------------------------------------------------------- #
# parsing / grouping
# --------------------------------------------------------------------------- #
def test_parse_title_shapes():
    assert parse_title("Will the Democratic Party win the Alabama Governor?")[0] == "Democratic"
    party, race = parse_title("Will the Independent Party win the Rhode Island Governor?")
    assert party == "Independent" and race.region == "rhode island" and race.chamber == "Governor"
    _, house = parse_title("Will the Democratic Party win the AL-02 House race?")
    assert house.chamber == "House" and house.region == "al-02"
    _, ctrl = parse_title("Will the Democratic Party win the U.S. Senate?")
    assert ctrl.is_control and ctrl.chamber == "Senate"
    assert parse_title("Totally unrelated market") is None


def test_west_virginia_is_not_grouped_with_virginia():
    """Regression guard for the OTHER phantom: 'Virginia' is a substring of 'West Virginia'.

    MARKET_RECON.md §6 records the recon's own state column mislabelling West Virginia as Virginia. A
    substring-matching parser merges these four markets into one 'race', whose YES prices sum to ~2.0
    and fake a ~1.0 sell-all arb across two unrelated contests.
    """
    va_d = market("Will the Democratic Party win the Virginia Senate?", 2001, "Senate")
    va_r = market("Will the Republican Party win the Virginia Senate?", 2002, "Senate")
    wv_d = market("Will the Democratic Party win the West Virginia Senate?", 2003, "Senate")
    wv_r = market("Will the Republican Party win the West Virginia Senate?", 2004, "Senate")

    graph = build_relationship_graph([va_d, va_r, wv_d, wv_r])
    assert len(graph.race_groups) == 2, "Virginia and West Virginia must be two distinct races"
    assert graph.group_sizes() == {2: 2}

    # real recon quotes for these four
    px = prices(**{"2001": (0.95, None), "2002": (None, 0.05),
                   "2003": (None, 0.065), "2004": (0.935, None)})
    # Neither race can form a complete basket (each leg is one-sided), so nothing is emitted — and
    # critically there is no 4-leg "arb".
    for opp in find_hard_arbs(graph, px):
        assert len(opp.legs) <= 3


# --------------------------------------------------------------------------- #
# REQUIRED: a 2-way arb is detected
# --------------------------------------------------------------------------- #
def test_two_way_hard_arb_detected():
    d = market("Will the Democratic Party win the Georgia Governor?", 1101, "Governor")
    r = market("Will the Republican Party win the Georgia Governor?", 1102, "Governor")
    graph = build_relationship_graph([d, r])
    assert graph.group_sizes() == {2: 1}

    # asks sum to 0.90 -> buy both for 0.90, settle 1.00
    px = prices(**{"1101": (0.40, 0.45), "1102": (0.40, 0.45)})
    arbs = find_hard_arbs(graph, px, max_contracts=10)
    hard = [a for a in arbs if a.type == HARD_ARB]
    assert len(hard) == 1
    opp = hard[0]
    assert {leg.action for leg in opp.legs} == {"buy"}
    assert {leg.exchange_id for leg in opp.legs} == {1101, 1102}
    assert all(leg.side == "yes" and leg.quantity == 10 for leg in opp.legs)
    assert opp.meta["per_contract_edge"] == pytest.approx(0.10, abs=1e-9)
    assert opp.edge_amount == pytest.approx(1.0, abs=1e-9)      # 0.10 * 10 contracts
    assert opp.confidence == 1.0

    out = opp.to_dict()
    assert set(out) == {"legs", "type", "edge_amount", "confidence"}
    assert set(out["legs"][0]) == {"exchange_id", "side", "action", "price", "quantity"}


def test_two_way_sell_side_arb_detected():
    d = market("Will the Democratic Party win the Nevada Governor?", 1201, "Governor")
    r = market("Will the Republican Party win the Nevada Governor?", 1202, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"1201": (0.60, 0.68), "1202": (0.55, 0.63)})   # bids sum 1.15
    hard = [a for a in find_hard_arbs(graph, px, max_contracts=5) if a.type == HARD_ARB]
    assert len(hard) == 1
    assert {leg.action for leg in hard[0].legs} == {"sell"}
    assert hard[0].meta["per_contract_edge"] == pytest.approx(0.15, abs=1e-9)


# --------------------------------------------------------------------------- #
# REQUIRED: the 3-way safety test — no phantom arb
# --------------------------------------------------------------------------- #
def test_three_way_race_produces_no_phantom_arb():
    """THE KEY SAFETY TEST.

    Rhode Island Governor lists D, R AND an Independent. Priced so that D+R alone look like a fat
    0.15 arb (0.40 + 0.45 = 0.85) while the complete 3-leg basket costs 1.03 and is NOT an arb.

    A 2-way-assuming engine buys D+R for 0.85 "riskless". If the Independent wins, both legs settle 0
    and the position is a total loss — a guaranteed loss entered as a riskless trade, which is why
    this is the one correctness point that matters most.
    """
    d = market("Will the Democratic Party win the Rhode Island Governor?", 1301, "Governor")
    r = market("Will the Republican Party win the Rhode Island Governor?", 1302, "Governor")
    i = market("Will the Independent Party win the Rhode Island Governor?", 1303, "Governor")

    graph = build_relationship_graph([d, r, i])

    # 1. the group SIZE was discovered as 3 from the data
    (group,) = graph.arbitrable_groups
    assert group.size == 3
    assert group.parties == {"Democratic", "Republican", "Independent"}
    assert graph.group_sizes() == {3: 1}

    px = prices(**{"1301": (0.36, 0.40), "1302": (0.41, 0.45), "1303": (0.14, 0.18)})

    # 2. the D+R subset really does look like an arb -> the phantom is genuinely reachable
    assert 0.40 + 0.45 == pytest.approx(0.85)

    # 3. the engine emits NOTHING: the complete basket costs 1.03 > 1.0
    assert find_hard_arbs(graph, px) == []

    # 4. and no emitted opportunity ever omits a listed outcome of the race. Re-price so the full
    #    3-leg basket IS a real arb, and confirm all three legs are present.
    px_real = prices(**{"1301": (0.33, 0.34), "1302": (0.33, 0.34), "1303": (0.29, 0.30)})
    hard = [a for a in find_hard_arbs(graph, px_real, max_contracts=7) if a.type == HARD_ARB]
    assert len(hard) == 1
    assert len(hard[0].legs) == 3, "a basket must span EVERY listed outcome"
    assert {leg.exchange_id for leg in hard[0].legs} == {1301, 1302, 1303}
    assert hard[0].meta["per_contract_edge"] == pytest.approx(1.0 - 0.98, abs=1e-9)


def test_three_way_with_unfillable_third_leg_emits_nothing():
    """The subtler half of the 3-way trap: the Independent exists but has NO ask.

    The basket cannot be completed, so buying D+R would again be a bet that the Independent is
    worthless — dressed as an arb. Guard G2.
    """
    d = market("Will the Democratic Party win the Montana Senate?", 1401, "Senate")
    r = market("Will the Republican Party win the Montana Senate?", 1402, "Senate")
    i = market("Will the Independent Party win the Montana Senate?", 1403, "Senate")
    graph = build_relationship_graph([d, r, i])
    px = prices(**{"1401": (0.36, 0.40), "1402": (0.41, 0.45), "1403": (0.02, None)})
    assert find_hard_arbs(graph, px) == []


def test_two_way_with_suspiciously_cheap_basket_is_downgraded_not_hard():
    """Guard G3: an outcome SIG never listed.

    Only D and R are listed, so the group looks exhaustive, and asks sum to 0.60 — a '0.40 arb'. But a
    mid-sum of 0.60 is far below 1.0, which is the signature of a real third candidate that simply has
    no market. Downgraded to soft rather than reported as riskless.
    """
    d = market("Will the Democratic Party win the Alaska Governor?", 1501, "Governor")
    r = market("Will the Republican Party win the Alaska Governor?", 1502, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"1501": (0.28, 0.30), "1502": (0.28, 0.30)})
    opps = find_hard_arbs(graph, px, max_contracts=4)
    assert len(opps) == 1
    assert opps[0].type == SOFT_MISPRICING
    assert opps[0].confidence < 0.5
    assert "NOT riskless" in opps[0].meta["downgraded"]


# --------------------------------------------------------------------------- #
# REQUIRED: the no-arb case (real recon prices)
# --------------------------------------------------------------------------- #
def test_no_arb_on_real_recon_prices():
    """Alabama Governor, exactly as quoted in the recon: D bid .065/ask .145, R bid .865/ask .945.

    asks sum 1.09 (> 1, no buy arb); bids sum 0.93 (< 1, no sell arb). The uniform 0.08 seeded spread
    means the honest pre-open answer is NO ARB, and the engine must say so.
    """
    d = market("Will the Democratic Party win the Alabama Governor?", 1601, "Governor")
    r = market("Will the Republican Party win the Alabama Governor?", 1602, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"1601": (0.065, 0.145), "1602": (0.865, 0.945)})
    assert find_hard_arbs(graph, px) == []


def test_sub_tick_edge_is_not_emitted():
    """An 'edge' smaller than one 0.005 tick is rounding noise."""
    d = market("Will the Democratic Party win the Iowa Governor?", 1701, "Governor")
    r = market("Will the Republican Party win the Iowa Governor?", 1702, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"1701": (0.49, 0.4985), "1702": (0.49, 0.4985)})   # asks sum 0.997
    assert find_hard_arbs(graph, px) == []


def test_fees_can_erase_an_arb():
    d = market("Will the Democratic Party win the Texas Governor?", 1801, "Governor")
    r = market("Will the Republican Party win the Texas Governor?", 1802, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"1801": (0.44, 0.47), "1802": (0.44, 0.47)})       # asks sum 0.94
    assert len(find_hard_arbs(graph, px, fees=0.0)) == 1
    assert find_hard_arbs(graph, px, fees=0.10) == []


def test_depth_caps_quantity():
    d = market("Will the Democratic Party win the Kansas Governor?", 1901, "Governor")
    r = market("Will the Republican Party win the Kansas Governor?", 1902, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"1901": (0.40, 0.45), "1902": (0.40, 0.45)})
    depths = {
        1901: {"asks": [{"price": 0.45, "quantity": 120}], "bids": []},
        1902: {"asks": [{"price": 0.45, "quantity": 1000}], "bids": []},
    }
    (opp,) = find_hard_arbs(graph, px, max_contracts=1000, depths=depths)
    assert all(leg.quantity == 120 for leg in opp.legs), "sized to the thinnest leg"


# --------------------------------------------------------------------------- #
# control markets
# --------------------------------------------------------------------------- #
def test_control_market_is_never_hard_arbed():
    """D-control + R-control are NOT a provable complementary pair (a 50-50 split means neither
    'wins'), so even priced as a fat arb they must not be emitted as hard."""
    dc = market("Will the Democratic Party win the U.S. Senate?", 2101, "Senate")
    rc = market("Will the Republican Party win the U.S. Senate?", 2102, "Senate")
    graph = build_relationship_graph([dc, rc])
    assert graph.arbitrable_groups == ()
    px = prices(**{"2101": (0.30, 0.34), "2102": (0.30, 0.34)})       # asks sum 0.68
    assert [o for o in find_hard_arbs(graph, px) if o.type == HARD_ARB] == []
    assert any("control" in reason for _, reason in graph.rejected)


# --------------------------------------------------------------------------- #
# REQUIRED: control-vs-components soft mispricing
# --------------------------------------------------------------------------- #
def _senate_universe():
    """3 contested Senate seats + the D/R control markets."""
    mkts = [
        market("Will the Democratic Party win the Georgia Senate?", 3001, "Senate"),
        market("Will the Republican Party win the Georgia Senate?", 3002, "Senate"),
        market("Will the Democratic Party win the Maine Senate?", 3003, "Senate"),
        market("Will the Republican Party win the Maine Senate?", 3004, "Senate"),
        market("Will the Democratic Party win the Texas Senate?", 3005, "Senate"),
        market("Will the Republican Party win the Texas Senate?", 3006, "Senate"),
        market("Will the Democratic Party win the U.S. Senate?", 3100, "Senate"),
        market("Will the Republican Party win the U.S. Senate?", 3101, "Senate"),
    ]
    return mkts


def test_control_vs_components_soft_mispricing_detected():
    graph = build_relationship_graph(_senate_universe())
    assert len(graph.control_relations) == 2
    d_rel = next(r for r in graph.control_relations if r.party == "Democratic")
    assert len(d_rel.components) == 3, "components = the D leg of each contested Senate race"

    # D is a heavy favourite in all three contested seats (mid 0.90), and already holds 49 seats of
    # the 97 not up here, needing 51. Components imply control is near-certain...
    px = prices(**{
        "3001": (0.88, 0.92), "3002": (0.08, 0.12),
        "3003": (0.88, 0.92), "3004": (0.08, 0.12),
        "3005": (0.88, 0.92), "3006": (0.08, 0.12),
        "3100": (0.50, 0.54),        # ...but control trades at mid 0.52 -> underpriced
        "3101": (0.46, 0.50),
    })
    models = {"Senate": ChamberModel(seats_needed=51, seats_held_not_up=49)}
    soft = find_soft_mispricings(graph, px, chamber_models=models, max_contracts=10)

    buys = [s for s in soft if s.legs and s.legs[0].exchange_id == 3100]
    assert len(buys) == 1
    opp = buys[0]
    assert opp.type == SOFT_MISPRICING
    assert opp.legs[0].action == "buy"
    assert opp.meta["implied_from_components"] > opp.meta["market_mid"]
    assert 0.0 < opp.confidence <= 0.6, "soft confidence is capped well below an identity"
    assert "STATISTICAL" in opp.meta["basis"]
    assert set(opp.to_dict()) == {"legs", "type", "edge_amount", "confidence"}


def test_control_market_without_a_seat_model_is_skipped_not_guessed():
    graph = build_relationship_graph(_senate_universe())
    px = prices(**{
        "3001": (0.88, 0.92), "3002": (0.08, 0.12),
        "3003": (0.88, 0.92), "3004": (0.08, 0.12),
        "3005": (0.88, 0.92), "3006": (0.08, 0.12),
        "3100": (0.50, 0.54), "3101": (0.46, 0.50),
    })
    soft = find_soft_mispricings(graph, px, chamber_models=None)
    assert soft and all(s.legs == [] and s.confidence == 0.0 for s in soft)
    assert all("no ChamberModel" in s.meta["skipped"] for s in soft)


def test_correlation_widens_the_control_distribution():
    """rho is not cosmetic: correlated component races put more mass in the tails, so a
    near-threshold control probability must move when rho changes."""
    ps = [0.5] * 10
    model = ChamberModel(seats_needed=8, seats_held_not_up=0)
    indep = implied_control_probability(ps, model, rho=0.0)
    corr = implied_control_probability(ps, model, rho=0.9)
    assert corr > indep, "correlation fattens the upper tail at a threshold above the mean"


# --------------------------------------------------------------------------- #
# reconcile with published constraints
# --------------------------------------------------------------------------- #
def test_reconcile_is_a_noop_when_sig_publishes_nothing():
    """The live pre-open state: /relationships/constraints returns 0."""
    d = market("Will the Democratic Party win the Georgia Governor?", 4001, "Governor")
    r = market("Will the Republican Party win the Georgia Governor?", 4002, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"4001": (0.40, 0.45), "4002": (0.40, 0.45)})
    ours = find_hard_arbs(graph, px)
    assert len(ours) == 1

    for empty in ({"data": [], "pagination": {}}, {"data": []}, [], None):
        rep = reconcile_with_published(ours, empty)
        assert rep["published"] == 0
        assert rep["agreement"] is None
        assert rep["ours_only"] == [["4001", "4002"]]
        assert rep["theirs_only"] == []
    assert len(ours) == 1, "reconcile must not mutate or drop our arbs"


def test_reconcile_flags_agreement_and_both_disagreement_directions():
    d = market("Will the Democratic Party win the Georgia Governor?", 4001, "Governor")
    r = market("Will the Republican Party win the Georgia Governor?", 4002, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"4001": (0.40, 0.45), "4002": (0.40, 0.45)})
    ours = find_hard_arbs(graph, px)

    published = {
        "data": [
            {"id": "c1", "suggestedCorrectiveTrades": [{"exchangeId": 4001}, {"exchangeId": 4002}]},
            {"id": "c2", "suggestedCorrectiveTrades": [{"exchangeId": 9001}, {"exchangeId": 9002}]},
        ]
    }
    rep = reconcile_with_published(ours, published)
    assert rep["both"] == [["4001", "4002"]]
    assert rep["theirs_only"] == [["9001", "9002"]]
    assert rep["ours_only"] == []
    assert rep["agreement"] == pytest.approx(0.5)
    kinds = {d_["kind"] for d_ in rep["disagreements"]}
    assert kinds == {"theirs_only"}
    assert any("may be incomplete" in d_["note"] for d_ in rep["disagreements"])


# --------------------------------------------------------------------------- #
# robustness
# --------------------------------------------------------------------------- #
def test_unparseable_and_incomplete_input_is_surfaced_not_crashed():
    graph = build_relationship_graph(
        [
            market("Some unrelated market title", 5001),
            {"id": 1, "title": "Will the Democratic Party win the Ohio Governor?"},   # no exchanges
            market("Will the Democratic Party win the Ohio Governor?", 5002, "Governor"),  # lone leg
            None,
            "garbage",
        ]
    )
    assert len(graph.unparsed) == 2
    assert find_hard_arbs(graph, {"data": []}) == []
    assert any("only 1 outcome" in reason for _, reason in graph.rejected)


def test_string_vs_int_exchange_ids_still_match():
    """Market payloads and price payloads need not agree on id type; a silent mismatch would read as
    'no price' and suppress every arb."""
    d = market("Will the Democratic Party win the Georgia Governor?", 6001, "Governor")
    r = market("Will the Republican Party win the Georgia Governor?", 6002, "Governor")
    graph = build_relationship_graph([d, r])
    px = {"data": [{"exchangeId": "6001", "bestBid": 0.40, "bestAsk": 0.45},
                   {"exchangeId": "6002", "bestBid": 0.40, "bestAsk": 0.45}]}
    assert len(find_hard_arbs(graph, px)) == 1


def test_duplicate_party_market_disqualifies_the_group():
    """Two markets for the same party double-counts an outcome and inflates the basket."""
    d1 = market("Will the Democratic Party win the Ohio Governor?", 7001, "Governor")
    d2 = market("Will the Democratic Party win the Ohio Governor?", 7002, "Governor")
    r = market("Will the Republican Party win the Ohio Governor?", 7003, "Governor")
    graph = build_relationship_graph([d1, d2, r])
    assert graph.arbitrable_groups == ()
    px = prices(**{"7001": (0.20, 0.25), "7002": (0.20, 0.25), "7003": (0.20, 0.25)})
    assert find_hard_arbs(graph, px) == []
    assert any("duplicate party" in reason for _, reason in graph.rejected)


def test_prices_are_on_the_half_cent_tick_and_rounded_against_us():
    d = market("Will the Democratic Party win the Georgia Governor?", 8001, "Governor")
    r = market("Will the Republican Party win the Georgia Governor?", 8002, "Governor")
    graph = build_relationship_graph([d, r])
    px = prices(**{"8001": (0.40, 0.4412), "8002": (0.40, 0.4412)})
    (opp,) = find_hard_arbs(graph, px, max_contracts=1)
    for leg in opp.legs:
        assert leg.price == pytest.approx(0.445), "a buy rounds UP to the tick, never down"
        assert round(leg.price / 0.005) == pytest.approx(leg.price / 0.005, abs=1e-6)
