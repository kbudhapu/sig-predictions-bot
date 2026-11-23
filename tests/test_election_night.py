"""Election-night race-call dry run (2026-10-08): arming on a crossing, Kalshi agreement, one shot per race."""
from sig.engine.election_night import ElectionNightConfig, NightState, step

CFG = ElectionNightConfig(theta=0.95, usd_per_race=2000.0)


def _leg(ex, poly, ask, kalshi=None, mapped=False, race="race:x"):
    return {"race": race, "exchange_id": ex, "poly": poly, "kalshi": kalshi, "kalshi_mapped": mapped,
            "sig_bid": None if ask is None else ask - 0.005, "sig_ask": ask, "ask_size": 3000}


def test_safe_seat_never_fires_only_a_crossing_does():
    st = NightState()
    assert step([_leg("1", 0.99, 0.95)], st, CFG, now=0.0) == []                  # 99% all along: never armed
    st2 = NightState()
    assert step([_leg("2", 0.80, 0.78)], st2, CFG, now=0.0) == []                 # arms (<= 93%)
    out = step([_leg("2", 0.96, 0.90)], st2, CFG, now=3600.0)                     # crosses 95% an hour later
    assert len(out) == 1 and out[0]["limit"] == 0.92 and out[0]["edge_c"] == 4.0 # min(0.90+0.02, 0.96-0.01)
    assert step([_leg("2", 0.97, 0.90)], st2, CFG, now=3700.0) == []              # one shot per race


def test_arming_expires():
    st = NightState()
    step([_leg("2", 0.80, 0.78)], st, CFG, now=0.0)
    assert step([_leg("2", 0.96, 0.90)], st, CFG, now=7 * 3600.0) == []           # armed >6h ago: stale


def test_kalshi_must_agree_and_sig_must_lag():
    st = NightState()
    step([_leg("2", 0.80, 0.78)], st, CFG, now=0.0)
    assert step([_leg("2", 0.96, 0.90, kalshi=0.93)], st, CFG, now=60.0) == []    # Kalshi disagrees
    assert step([_leg("2", 0.96, 0.90, mapped=True)], st, CFG, now=60.0) == []    # mapped on Kalshi, no quote
    assert step([_leg("2", 0.96, 0.955)], st, CFG, now=60.0) == []                # SIG already caught up
    out = step([_leg("2", 0.96, 0.90, kalshi=0.97)], st, CFG, now=60.0)
    assert len(out) == 1


def test_race_fires_once_even_if_both_legs_qualify():
    st = NightState()
    step([_leg("d", 0.80, 0.78), _leg("r", 0.20, 0.21)], st, CFG, now=0.0)
    out = step([_leg("d", 0.96, 0.90), _leg("r", 0.96, 0.90)], st, CFG, now=60.0)  # impossible in reality; guard
    assert len(out) == 1
