"""signals/wiki_ratings.py — the Wikipedia rating-derived benchmark, tested fully OFFLINE.

A fixture captures the exact wikitext shape of the real 2026 pages ({{USRaceRating|Fam|Party}} cells
tagged with <!--Rater--> comments under a `! [[… in State|State]]` row header). We pin: the parser;
the rating→probability map (incl. the Dem mirror + toss-up); cross-rater aggregation; the mandatory
`rating_derived` quality tag; the race→SIG-market projection; the Polymarket calibration; and freshness.
No network: `scrape_office` takes an injected `fetch_wikitext`.
"""
from __future__ import annotations

import pytest

from signals.wiki_ratings import (
    CONFIDENCE,
    DEFAULT_RATING_TO_P,
    RatingEstimate,
    build_sig_exchange_benchmarks,
    calibrate,
    estimate_from_rating,
    parse_rating_args,
    parse_ratings_wikitext,
    parse_sig_seat_title,
    rating_to_p_rep,
    scrape_office,
    sig_resolver_from_race_mapping,
    to_sig_estimates,
)

# Real wikitext shape (trimmed): header block (ignored) + 3 senate races with per-rater cells.
SENATE_FIXTURE = """
{| class="wikitable sortable"
! State !! PVI !! Incumbent !! 2020 !! Cook !! Sabato !! IE !! RCP
|-
! [[2026 United States Senate election in Alabama|Alabama]]
| {{Shading PVI|R|15}}
| {{Party shading/Republican}} | {{sortname|Tommy|Tuberville}}<br />{{Small|(retiring)}}
| {{Party shading/Republican}} data-sort-value="60.10" | 60.10% R
<!--Cook-->   | {{USRaceRating|Solid|R}}
<!--Sabato--> | {{USRaceRating|Safe|R}}
<!--IE-->     | {{USRaceRating|Solid|R}}
<!--RCP-->    | {{USRaceRating|Solid|R}}
|-
! [[2026 United States Senate election in Georgia|Georgia]]
| {{Shading PVI|R|3}}
| {{Party shading/Republican}} | {{sortname|Mike|Collins}}
<!--Cook-->   | {{USRaceRating|Tossup}}
<!--Sabato--> | {{USRaceRating|Lean|R}}
<!--IE-->     | {{USRaceRating|Tossup}}
<!--RCP-->    | {{USRaceRating|Lean|R}}
|-
! [[2026 United States Senate election in Michigan|Michigan]]
| {{Shading PVI|EVEN}}
<!--Cook-->   | {{USRaceRating|Lean|D|flip}}
<!--Sabato--> | {{USRaceRating|Lean|D}}
<!--IE-->     | {{USRaceRating|Tossup}}
|}
"""

GOVERNOR_FIXTURE = """
{| class="wikitable"
! State !! Cook !! Sabato
|-
! [[2026 Georgia gubernatorial election|Georgia]]
<!--Cook-->   | {{USRaceRating|Tilt|R}}
<!--Sabato--> | {{USRaceRating|Lean|R}}
|}
"""


# ── rating → probability ─────────────────────────────────────────────────────── #
def test_parse_rating_args():
    assert parse_rating_args("Solid|R") == ("solid", "r")
    assert parse_rating_args("Tossup") == ("tossup", None)
    assert parse_rating_args("Lean|D|flip") == ("lean", "d")       # the 'flip' 3rd arg is ignored


def test_rating_to_p_rep_families_and_mirror():
    assert rating_to_p_rep("Solid", "r") == 0.95
    assert rating_to_p_rep("Solid", "d") == pytest.approx(0.05)    # mirror: 1 − 0.95
    assert rating_to_p_rep("Lean", "r") == 0.65
    assert rating_to_p_rep("Lean", "d") == pytest.approx(0.35)
    assert rating_to_p_rep("Tossup", None) == 0.50
    assert rating_to_p_rep("Nonsense", "r") is None               # unknown family → unmappable


# ── parse + aggregate ────────────────────────────────────────────────────────── #
def test_parser_extracts_each_race_and_its_raters():
    races = parse_ratings_wikitext(SENATE_FIXTURE, "senate")
    by = {r.state: r for r in races}
    assert set(by) == {"alabama", "georgia", "michigan"}           # header block ignored
    assert by["alabama"].raters["cook"] == ("solid", "r")
    assert by["georgia"].raters["sabato"] == ("lean", "r")
    assert by["michigan"].raters["cook"] == ("lean", "d")          # 'flip' stripped
    assert by["georgia"].key == "senate:georgia"


def test_consensus_is_the_mean_implied_prob_across_raters():
    races = {r.state: r for r in parse_ratings_wikitext(SENATE_FIXTURE, "senate")}
    al = estimate_from_rating(races["alabama"])
    ga = estimate_from_rating(races["georgia"])
    mi = estimate_from_rating(races["michigan"])
    assert al.p_rep == 0.95 and al.p_dem == pytest.approx(0.05)            # all Solid/Safe R
    assert ga.p_rep == pytest.approx((0.50 + 0.65 + 0.50 + 0.65) / 4)      # 0.575
    assert ga.p_dem == pytest.approx(1 - 0.575)
    assert mi.p_rep == pytest.approx((0.35 + 0.35 + 0.50) / 3)             # 0.40 → Dem-favored, correct
    assert ga.n_raters == 4 and mi.n_raters == 3


def test_every_estimate_is_tagged_rating_derived():
    for r in parse_ratings_wikitext(SENATE_FIXTURE, "senate"):
        est = estimate_from_rating(r)
        assert est.confidence == CONFIDENCE == "rating_derived"
        assert est.source == "wikipedia_ratings"


def test_scrape_office_uses_injected_fetch_offline():
    ests = scrape_office("senate", fetch_wikitext=lambda _page: SENATE_FIXTURE, now=1000.0)
    assert {e.state for e in ests} == {"alabama", "georgia", "michigan"}
    assert all(e.fetched_at == 1000.0 for e in ests)
    # freshness: fresh now, stale far in the future
    ga = next(e for e in ests if e.state == "georgia")
    assert not ga.is_stale(now=1000.0)
    assert ga.is_stale(now=1000.0 + 7 * 3600)


def test_governor_office_parses_with_its_own_page():
    ests = scrape_office("governor", fetch_wikitext=lambda _page: GOVERNOR_FIXTURE, now=5.0)
    ga = next(e for e in ests if e.state == "georgia")
    assert ga.office == "governor" and ga.key == "governor:georgia"
    assert ga.p_rep == pytest.approx((0.55 + 0.65) / 2)            # Tilt R 0.55 + Lean R 0.65 → 0.60


def test_house_office_is_not_yet_supported():
    with pytest.raises(ValueError):
        scrape_office("house", fetch_wikitext=lambda _page: "")


# ── map to SIG markets ───────────────────────────────────────────────────────── #
class _RM:                                                         # minimal stand-in for a RaceMapping
    def __init__(self, sid):
        self.sig_market_id = sid


def test_maps_race_to_the_correct_sig_per_party_markets():
    # race_mapping-style keys → (office,state) → per-party SIG ids
    race_mapping = {
        "georgia_senate_rep": _RM("259"), "georgia_senate_dem": _RM("258"),
        "texas_senate_rep": _RM("294"),
    }
    resolver = sig_resolver_from_race_mapping(race_mapping)
    assert resolver[("senate", "georgia")] == {"rep": "259", "dem": "258"}
    ests = scrape_office("senate", fetch_wikitext=lambda _p: SENATE_FIXTURE, now=1.0)
    sig = to_sig_estimates(ests, resolver)
    # Georgia consensus p_rep 0.575 → the Republican market; p_dem 0.425 → the Democratic market
    assert sig["259"]["fair"] == pytest.approx(0.575)
    assert sig["258"]["fair"] == pytest.approx(0.425)
    assert sig["259"]["confidence"] == "rating_derived"
    # Alabama/Michigan aren't in this resolver → not emitted (available as RatingEstimates for later)
    assert set(sig) == {"259", "258"}


# ── calibration vs Polymarket overlap ────────────────────────────────────────── #
def test_calibration_reports_error_and_per_band_real_money():
    ests = scrape_office("senate", fetch_wikitext=lambda _p: SENATE_FIXTURE, now=1.0)
    # pretend Polymarket prices the GA + AL races (the overlap); MI has no market
    real = {"senate:georgia": 0.60, "senate:alabama": 0.97}
    cal = calibrate(ests, real)
    assert cal["n"] == 2
    assert cal["mae"] == pytest.approx((abs(0.575 - 0.60) + abs(0.95 - 0.97)) / 2, abs=1e-4)
    # each overlap race buckets to its nearest rating band, recording the observed real-money price:
    # GA consensus 0.575 → nearest 'tilt' (0.55); AL 0.95 → 'solid'. These are the numbers to tune with.
    assert cal["by_band"]["tilt"]["real_money_mean"] == pytest.approx(0.60)
    assert cal["by_band"]["solid"]["real_money_mean"] == pytest.approx(0.97)


def test_calibration_handles_no_overlap():
    ests = scrape_office("senate", fetch_wikitext=lambda _p: SENATE_FIXTURE, now=1.0)
    assert calibrate(ests, {})["n"] == 0


def test_rating_estimate_conviction_is_coarse_discounted():
    from signals.conviction import conviction
    ests = {e.state: e for e in scrape_office("senate", fetch_wikitext=lambda _p: SENATE_FIXTURE, now=1.0)}
    al, ga = ests["alabama"], ests["georgia"]             # 0.95 (safe R) vs 0.575 (mushy)
    assert al.conviction() == conviction(0.95, "rating_derived")
    assert al.conviction() > ga.conviction()              # extreme race earns more than a mushy one
    assert al.conviction() < conviction(0.95, "sharp")    # coarse discounted vs a sharp 0.95


def test_default_mapping_is_symmetric_around_half():
    # R and D sides of the same family must sum to 1.0 (so a race's two SIG markets sum to 1)
    for fam, p in DEFAULT_RATING_TO_P.items():
        assert rating_to_p_rep(fam, "r") + rating_to_p_rep(fam, "d") == pytest.approx(1.0)


# ── SIG seat-title ↔ Wikipedia auto-match (the coverage bridge) ─────────────────── #
def test_parse_sig_seat_title_extracts_party_state_office():
    assert parse_sig_seat_title("Will the Republican Party win the Rhode Island Senate?") == \
        ("rep", "rhode_island", "senate")
    assert parse_sig_seat_title("Will the Democratic Party win the Oklahoma Governor?") == \
        ("dem", "oklahoma", "governor")
    # chamber-control markets are NOT per-state seats → None (left to sharp Polymarket)
    assert parse_sig_seat_title("Will the Republican Party win the U.S. Senate?") is None
    assert parse_sig_seat_title("Will the Democratic Party win the U.S. House?") is None
    # unrelated / House-district markets → None (Wikipedia covers only Senate+Governor)
    assert parse_sig_seat_title("Will BTC close above $100k?") is None


def _est(office, state, p_rep):
    return RatingEstimate(key=f"{office}:{state}", office=office, state=state,
                          p_rep=round(p_rep, 4), p_dem=round(1 - p_rep, 4), n_raters=4, raters={})


def test_build_sig_exchange_benchmarks_matches_by_party_state_office_direct_polarity():
    estimates = [_est("senate", "georgia", 0.575), _est("governor", "oklahoma", 0.90)]
    markets = [
        {"title": "Will the Republican Party win the Georgia Senate?", "exchanges": [{"id": "900"}]},
        {"title": "Will the Democratic Party win the Georgia Senate?", "exchanges": [{"id": "901"}]},
        {"title": "Will the Republican Party win the Oklahoma Governor?", "exchanges": [{"id": "902"}]},
        {"title": "Will the Republican Party win the U.S. Senate?", "exchanges": [{"id": "999"}]},  # excluded
        {"title": "Will the Republican Party win the Wyoming Senate?", "exchanges": [{"id": "903"}]},  # no rating
    ]
    out, unmatched = build_sig_exchange_benchmarks(markets, estimates)
    # Republican market → p_rep (direct); Democratic market → p_dem (direct). NO polarity flip.
    assert out["900"]["fair"] == pytest.approx(0.575) and out["900"]["party"] == "rep"
    assert out["901"]["fair"] == pytest.approx(0.425) and out["901"]["party"] == "dem"
    assert out["902"]["fair"] == pytest.approx(0.90)
    assert out["900"]["confidence"] == "rating_derived" and out["900"]["source"] == "wikipedia_ratings"
    assert "999" not in out                                   # U.S. chamber control excluded
    assert ("senate", "wyoming") in unmatched                 # SIG market, no Wikipedia rating → never traded
    assert set(out) == {"900", "901", "902"}


def test_build_benchmarks_skips_markets_without_an_exchange():
    estimates = [_est("senate", "maine", 0.5)]
    markets = [{"title": "Will the Republican Party win the Maine Senate?", "exchanges": []}]
    out, _ = build_sig_exchange_benchmarks(markets, estimates)
    assert out == {}
