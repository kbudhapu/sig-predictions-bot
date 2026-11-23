"""2026-10-09: 21 Senate legs were mapped to the CLOSED 2024 Polymarket markets (no fair -> cross never traded 4-12c
gaps). They now point at the 2026 'Will the {party} win the {state} Senate race in 2026?' markets. Guard: none of the
dead 2024 ids may come back, and Montana (third candidate on Poly) stays dormant until reviewed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from signals.mapping import load_race_mapping  # noqa: E402

DEAD_2024 = {"500712", "500711", "500657", "500656", "500736", "500735", "500729", "500728", "255449", "255448",
             "500690", "500689", "500699", "500698", "500115", "500114", "500716", "500715", "500618", "500709",
             "500708"}


def _races():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return load_race_mapping(os.path.join(root, "signals", "race_mapping.yaml"))


def test_no_mapping_points_at_a_closed_2024_market():
    used = {r.externals["polymarket"].market_id for r in _races().values() if "polymarket" in r.externals}
    assert used & DEAD_2024 == set()


def test_remapped_senate_legs_are_active_and_direct():
    races = _races()
    for k in ("florida_senate_rep", "florida_senate_dem", "delaware_senate_dem", "texas_senate_dem",
              "massachusetts_senate_dem", "virginia_senate_dem"):
        r = races[k]
        assert r.is_active() and r.externals["polymarket"].polarity == "direct", k


def test_montana_senate_stays_dormant():
    races = _races()
    assert not races["montana_senate_rep"].is_active() and not races["montana_senate_dem"].is_active()


def test_one_polymarket_market_per_sig_leg():
    seen = {}
    for k, r in _races().items():
        p = r.externals.get("polymarket")
        if p and p.market_id and r.is_active():
            key = (p.market_id, p.polarity)          # one market may serve both legs: direct + inverted
            assert key not in seen, (k, seen.get(key))
            seen[key] = k
