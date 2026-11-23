from sig.engine.rotation import Candidate, Held, decide_rotation, entry_threshold


def _h(ex, qty, mid, sharp, mv=5000.0, hs=0.0025):
    return Held(ex, qty, mid, sharp, hs, mv)


def _c(ex, mid, sharp, hs=0.0025):
    return Candidate(ex, mid, sharp, hs)


def test_no_swap_when_cash_can_fund():
    assert decide_rotation([_h("1", 100, 0.90, 0.91)], [_c("2", 0.80, 0.95)], cash=5000, position_size=4400) == []


def test_swaps_weakest_for_much_bigger_gap():
    held = [_h("1", 100, 0.90, 0.91), _h("2", 100, 0.85, 0.95)]          # remaining 1c vs 10c
    out = decide_rotation(held, [_c("9", 0.80, 0.92)], cash=100, position_size=4400)   # gap 12c
    assert len(out) == 1 and out[0].sell.exchange_id == "1" and out[0].buy.exchange_id == "9"


def test_short_remaining_gap_uses_held_side():
    h = _h("1", -100, 0.12, 0.05)                                          # held NO, SIG above sharp by 7c
    assert abs(h.remaining_gap - 0.07) < 1e-9
    h2 = _h("1", -100, 0.04, 0.05)                                         # sharp now above SIG: nothing left
    assert h2.remaining_gap == 0.0


def test_margin_and_spreads_block_marginal_swap():
    held = [_h("1", 100, 0.90, 0.92)]                                      # 2c left
    out = decide_rotation(held, [_c("9", 0.85, 0.92, hs=0.01)], cash=0, position_size=4400)  # 7c gap, wide spread
    assert out == []                                                       # 7 − 2 − 2×(1+0.25) = 2.5c < 4c


def test_never_buys_a_race_already_held_or_below_entry_threshold():
    held = [_h("1", 100, 0.90, 0.905)]
    assert decide_rotation(held, [_c("1", 0.70, 0.95)], cash=0, position_size=4400) == []   # held race
    assert decide_rotation(held, [_c("9", 0.50, 0.56)], cash=0, position_size=4400) == []   # toss-up: threshold high
    assert entry_threshold(0.5) == float("inf")


def test_small_holdings_are_not_rotation_sources():
    held = [_h("1", 10, 0.90, 0.90, mv=50.0)]
    assert decide_rotation(held, [_c("9", 0.80, 0.95)], cash=0, position_size=4400) == []
