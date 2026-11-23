"""Arb exit-decision tests (strategies/exit).

Params come from config/sig.yaml via load_exit_config() — nothing is hardcoded here either, except
the hand-computed basket arithmetic, which exists precisely to pin the numbers.

THE INVARIANT THE WHOLE FILE DEFENDS: a hard arb's unrealised profit is CONTRACTED, not at risk, so
selling early FORFEITS a guaranteed payoff. "Exit when green" is therefore wrong, and the tests
below are weighted towards proving the module REFUSES to churn — `test_hold_when_nothing_better` and
`test_adaptive_threshold_flips_once_and_monotonically` are the two that matter most.

The forfeit is the EXACT one, read off the two branches' cashflows:

    remainder = settlement_value - current_basket_value       (the contract output field)
    forfeit   = remainder + fee_cost + slippage                (what condition (A) compares)

because the exit costs both shrink the cash the exit returns and enlarge what exiting gives up.
`test_exact_forfeit_makes_exits_harder_than_the_bare_remainder` pins that directly, and every
derived threshold below is computed from `forfeit`, never from `remainder`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from strategies.exit import (
    EXIT,
    HOLD,
    R_AT_SETTLEMENT,
    R_BOOK_TOO_THIN,
    R_GAIN_BELOW_MINIMUM,
    R_NO_BETTER_OPPORTUNITY,
    R_NO_PRICE,
    R_RECYCLE_BEATS_HOLD,
    R_REMAINDER_EXHAUSTED,
    R_SLIPPAGE_EXCEEDS_GAIN,
    ExitConfig,
    decide_basket_exit,
    decide_exits,
    load_exit_config,
    recycle_return_per_day,
)

CFG = load_exit_config()

# One full-size sibling-pair basket, in the shape the state layer persists. 1000 contracts of
# "Republican wins RI Senate" + 1000 of "Democrat wins RI Senate": exactly one settles to 1.0, so the
# basket pays 1000 at the wall whatever happens. Bought for 0.45 + 0.48 = 0.93 -> $930.
QTY = 1000
ENTRY_COST = 930.0
SETTLEMENT_VALUE = 1000.0

#: Bids we could sell into today: 0.49 + 0.50 = 0.99 -> $990 realisable, $10 remainder left on the
#: table, $60 of gain available now. Those three numbers drive every expectation below.
BID_A, BID_B = 0.49, 0.50
CURRENT_VALUE = (BID_A + BID_B) * QTY          # 990.0
REMAINDER = SETTLEMENT_VALUE - CURRENT_VALUE   # 10.0
GROSS_GAIN = CURRENT_VALUE - ENTRY_COST        # 60.0
#: On a deep book (zero slippage) the exact forfeit is just the remainder plus the basket fee.
#: Written as an expression rather than 10.0 so it stays correct if `exit.fees` is ever set live.
FEE_COST = CFG.fees * QTY
FORFEIT = REMAINDER + FEE_COST


def book(bid_levels, *, ask=0.57):
    """Orderbook in the live shape. `bid_levels` is [(price, qty)], best first.

    An ask side is included but deliberately never read: an exit sells, so only the bids matter, and
    the asks are here so a test that accidentally started pricing off the mid would show up as a
    wrong number rather than a KeyError.
    """
    return {
        "bids": [{"price": p, "quantity": q} for p, q in bid_levels],
        "asks": [{"price": ask, "quantity": 5000}],
    }


def deep(price, qty=50_000):
    """A book that can absorb anything — the measured deep end was ~64,000 contracts."""
    return book([(price, qty)])


def prices_rows(top_bids):
    """/exchanges/prices rows consistent with the books. {exchange_id: top bid}.

    Must agree with the books: `_top_bid` takes the MINIMUM of the two sources, so a disagreement
    would silently (and conservatively) change the arithmetic a test is trying to pin.
    """
    return [
        {"exchangeId": ex, "bestBid": bid, "bestAsk": bid + 0.08}
        for ex, bid in top_bids.items()
    ]


def basket(legs=None, *, basket_id="b1", entry_cost=ENTRY_COST, settlement_value=SETTLEMENT_VALUE):
    return {
        "basket_id": basket_id,
        "strategy_id": "relationship_engine",
        "kind": "hard_arb",
        "legs": legs
        if legs is not None
        else [
            {"exchange_id": "101", "side": "yes", "quantity": float(QTY), "entry_price": 0.45},
            {"exchange_id": "102", "side": "yes", "quantity": float(QTY), "entry_price": 0.48},
        ],
        "entry_cost": entry_cost,
        "settlement_value": settlement_value,
    }


def world(book_a=None, book_b=None):
    """(prices, books) for the standard pair, deep on both legs unless overridden."""
    ba = book_a if book_a is not None else deep(BID_A)
    bb = book_b if book_b is not None else deep(BID_B)
    books = {"101": ba, "102": bb}
    return prices_rows({"101": BID_A, "102": BID_B}), books


def decide(*, edge, days, cfg=CFG, book_a=None, book_b=None, bask=None):
    px, books = world(book_a, book_b)
    return decide_basket_exit(
        bask if bask is not None else basket(),
        prices=px,
        books=books,
        best_alternative_edge=edge,
        days_to_settlement=days,
        cfg=cfg,
    )


def threshold_edge(*, forfeit, freed, days, cfg=CFG):
    """The alternative edge at which condition (A) exactly balances, derived from the config.

    Exit requires  recycle_per_day > forfeit_per_day × (1 + min_recycle_advantage), i.e.

        freed × e / horizon  >  (forfeit / days) × (1 + margin)
        horizon = max(min(alternative_horizon_days, days), days_floor)
        forfeit = remainder + fee_cost + slippage

    Note the numerator is the FORFEIT, not the bare remainder: the exit costs are part of what
    exiting gives up. Computing this from CFG rather than hardcoding a number keeps these tests
    meaningful if the thresholds are retuned live.
    """
    hurdle = (forfeit / max(days, cfg.days_to_settlement_floor)) * (
        1.0 + cfg.min_recycle_advantage
    )
    horizon = max(min(cfg.alternative_horizon_days, max(days, 0.0)), cfg.days_to_settlement_floor)
    return hurdle * horizon / freed


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_loads_every_threshold_from_yaml():
    """Pins that NOTHING is defaulted in code: every tunable the rule reads comes off the yaml, and
    the shipped values are in their sane ranges."""
    assert 0.0 <= CFG.fees < 1.0
    assert CFG.min_recycle_advantage > 0.0, "a zero margin would authorise churning on a dead heat"
    assert CFG.liquidity_safety_margin >= 0.0
    assert CFG.min_realizable_gain >= 0.0
    assert CFG.days_to_settlement_floor > 0.0
    assert CFG.alternative_horizon_days > 0.0
    assert CFG.settlement_at == dt.datetime(2026, 11, 4, 17, 0, tzinfo=dt.timezone.utc)


def test_config_rejects_values_that_would_invert_the_rule():
    """A negative recycle margin means "exit even when recycling LOSES to holding", and a zero days
    floor means a divide-by-zero in the decision path. Both must fail loud at load, not at 03:00."""
    with pytest.raises(ValueError, match="min_recycle_advantage"):
        dataclasses.replace(CFG, min_recycle_advantage=-0.1).validate()
    with pytest.raises(ValueError, match="days_to_settlement_floor"):
        dataclasses.replace(CFG, days_to_settlement_floor=0.0).validate()
    with pytest.raises(ValueError, match="fees"):
        dataclasses.replace(CFG, fees=1.5).validate()
    with pytest.raises(ValueError, match="settlement_utc"):
        dataclasses.replace(CFG, settlement_utc="not-a-date").validate()


def test_days_to_settlement_helper_is_signed():
    """days_to_settlement must be allowed to go NEGATIVE so the decision path can see "already
    settling" and refuse an early exit. Clamping it at zero inside the helper would hide that."""
    assert CFG.days_to_settlement(dt.datetime(2026, 10, 5, 17, 0, tzinfo=dt.timezone.utc)) == 30.0
    assert CFG.days_to_settlement(dt.datetime(2026, 11, 5, 17, 0, tzinfo=dt.timezone.utc)) == -1.0


def test_exit_config_registered_in_the_central_schema():
    """sig/config.py has a STRICT schema that rejects unknown sections and unknown keys. The `exit:`
    block must be registered there or every entrypoint's load_config() raises."""
    from sig.config import _SCHEMA, load_yaml, validate_config

    assert "exit" in _SCHEMA
    cfg = load_yaml()
    validate_config(cfg)  # the real file must pass as shipped
    assert set(cfg["exit"]) == _SCHEMA["exit"]["required"] | _SCHEMA["exit"]["optional"]


# --------------------------------------------------------------------------- #
# (A) the recycle-vs-hold comparison
# --------------------------------------------------------------------------- #
def test_exit_when_recycling_clearly_beats_holding():
    """Abundant opportunity + many days left + deep books => EXIT.

    This is the early-competition case: $10 of guaranteed payoff forgone, spread over 30 days, is
    $0.33/day, while $990 of freed capital at a 3% edge turning over in ~5 days is $5.94/day.
    Recycling wins by ~18x, so the capital must be freed rather than parked until Nov 4.
    """
    d = decide(edge=0.03, days=30.0)
    assert d.decision == EXIT
    assert d.reason == R_RECYCLE_BEATS_HOLD
    # the contract fields keep their original meaning...
    assert d.remainder == pytest.approx(REMAINDER)
    assert d.remainder_per_day == pytest.approx(REMAINDER / 30.0)
    # ...and the exact forfeit, which is what the comparison used, is reported next to them
    assert d.forfeit == pytest.approx(FORFEIT)
    assert d.forfeit_per_day == pytest.approx(FORFEIT / 30.0)
    assert d.recycle_per_day > d.forfeit_per_day * (1.0 + CFG.min_recycle_advantage)
    assert d.meta["hurdle_basis"] == "forfeit_per_day"
    # the whole basket is offered for sale, nothing less
    assert [leg["exchange_id"] for leg in d.sell_legs] == ["101", "102"]


def test_hold_when_nothing_better():
    """THE MOST IMPORTANT TEST. recycle_per_day < forfeit_per_day => HOLD.

    The gain is real ($60 on the screen) and the book is deep, so a naive "exit when green" module
    would sell. It must not: with no better home for the capital, selling gives up $10 of CONTRACTED
    profit to earn less than $10 over the same window. The guaranteed payoff is the better asset.
    """
    # No alternative at all.
    d0 = decide(edge=0.0, days=30.0)
    assert d0.decision == HOLD
    assert d0.reason == R_NO_BETTER_OPPORTUNITY
    assert d0.sell_legs == []
    # the gain was real; it just loses to waiting
    assert d0.realizable_gain == pytest.approx(GROSS_GAIN - FEE_COST)
    assert d0.recycle_per_day == 0.0
    # a HOLD still reports the full comparison, so the log explains itself
    assert d0.forfeit == pytest.approx(FORFEIT)
    assert d0.forfeit_per_day == pytest.approx(FORFEIT / 30.0)

    # An alternative that exists but is WORSE per capital-day than simply waiting.
    weak = threshold_edge(forfeit=FORFEIT, freed=ENTRY_COST + GROSS_GAIN - FEE_COST, days=30.0) * 0.5
    d1 = decide(edge=weak, days=30.0)
    assert d1.decision == HOLD
    assert d1.reason == R_NO_BETTER_OPPORTUNITY
    assert d1.recycle_per_day < d1.forfeit_per_day * (1.0 + CFG.min_recycle_advantage)


def test_dead_heat_holds_because_of_the_advantage_margin():
    """An alternative that exactly TIES the hold rate must hold, not churn. Exiting pays spread and
    fees twice, so a marginal exit is strictly worse than doing nothing — that is what
    min_recycle_advantage buys, and a tie must land on the hold side of it.

    The tie is struck against forfeit_per_day, which is the rate the decision actually uses.
    """
    freed = ENTRY_COST + GROSS_GAIN - FEE_COST
    tie = (FORFEIT / 30.0) * CFG.alternative_horizon_days / freed
    d = decide(edge=tie, days=30.0)
    assert d.recycle_per_day == pytest.approx(d.forfeit_per_day, rel=1e-9)
    assert d.decision == HOLD
    assert d.reason == R_NO_BETTER_OPPORTUNITY


def test_negative_alternative_edge_is_no_opportunity():
    """A negative "edge" is not a short — it is the absence of an opportunity. It must clamp to zero
    rather than flowing through the arithmetic as a negative recycle rate."""
    assert recycle_return_per_day(10_000.0, -0.5, 30.0, CFG)[0] == 0.0
    d = decide(edge=-0.5, days=30.0)
    assert d.decision == HOLD
    assert d.recycle_per_day == 0.0


def test_adaptive_threshold_flips_once_and_monotonically():
    """THE ADAPTIVE PROPERTY, stated as a test. Sweeping the alternative edge upward from 0 must flip
    the decision hold -> exit EXACTLY ONCE and never flip back.

    This is what proves the rule is a genuine threshold in the opportunity set rather than a
    hardcoded "sell at +X%": recycle_per_day is linear and increasing in the alternative edge, the
    hold rate is independent of it, so there is one crossing and the decision is monotone across it.
    A hardcoded rule would either never flip or flip on something other than the edge.
    """
    edges = [i * 0.0005 for i in range(0, 41)]          # 0.0000 .. 0.0200
    decisions = [decide(edge=e, days=30.0).decision for e in edges]

    assert decisions[0] == HOLD, "no opportunity must never exit"
    assert decisions[-1] == EXIT, "an abundant opportunity must exit"
    flips = [i for i in range(1, len(decisions)) if decisions[i] != decisions[i - 1]]
    assert len(flips) == 1, f"expected exactly one hold->exit crossing, got {flips}"
    cut = flips[0]
    assert all(x == HOLD for x in decisions[:cut])
    assert all(x == EXIT for x in decisions[cut:])

    # and the crossing sits where the config says it should — on the FORFEIT, not the remainder
    expected = threshold_edge(
        forfeit=FORFEIT, freed=ENTRY_COST + GROSS_GAIN - FEE_COST, days=30.0
    )
    assert edges[cut - 1] <= expected <= edges[cut]


def test_remainder_exhausted_exits_even_with_no_alternative():
    """If the bids already cover the settlement value net of exit costs, the EXACT forfeit is <= 0:
    there is nothing left to give up, holding buys literally nothing and the exit is free money. Must
    exit even with a zero alternative edge.

    The branch keys off `forfeit`, not the bare remainder, so a basket with a small positive
    remainder that is swamped by fees and slippage does NOT shortcut here — it goes through the full
    per-day comparison like everything else.
    """
    px = prices_rows({"101": 0.52, "102": 0.52})
    books = {"101": deep(0.52), "102": deep(0.52)}
    d = decide_basket_exit(
        basket(), prices=px, books=books, best_alternative_edge=0.0,
        days_to_settlement=30.0, cfg=CFG,
    )
    assert d.remainder == pytest.approx(SETTLEMENT_VALUE - 1040.0)   # -40.0
    assert d.forfeit == pytest.approx(SETTLEMENT_VALUE - 1040.0 + CFG.fees * QTY)
    assert d.forfeit <= 0.0
    assert d.decision == EXIT
    assert d.reason == R_REMAINDER_EXHAUSTED


# --------------------------------------------------------------------------- #
# the days-to-settlement clock
# --------------------------------------------------------------------------- #
def test_same_basket_same_edge_exits_at_30_days_and_holds_at_1_day():
    """THE CLOCK. Identical basket, identical books, identical alternative edge — the answer must
    DIFFER by days remaining, and the direction is: exit early, hold late.

    Why that direction is correct. The forfeit is a FIXED dollar amount; what changes is the time
    over which holding earns it. $10 over 30 days is $0.33/day and almost anything beats it. The
    same $10 over 1 day is $10/day, which is a ~365% annualised riskless rate — nothing in a
    prediction market beats that reliably, so late exits must become rare. The module gets this from
    dividing by days on BOTH sides (the alternative's horizon is capped at the wall, so inside the
    last day the two horizons coincide and only a strictly bigger edge can win). It is arithmetic,
    not a calendar of special cases.
    """
    freed = ENTRY_COST + GROSS_GAIN - FEE_COST
    e = threshold_edge(forfeit=FORFEIT, freed=freed, days=30.0) * 1.5
    # the same edge is comfortably BELOW the 1-day threshold — that is the whole point
    assert e < threshold_edge(forfeit=FORFEIT, freed=freed, days=1.0)

    early = decide(edge=e, days=30.0)
    late = decide(edge=e, days=1.0)

    assert early.decision == EXIT and early.reason == R_RECYCLE_BEATS_HOLD
    assert late.decision == HOLD and late.reason == R_NO_BETTER_OPPORTUNITY

    # and the mechanism is visible: the hold rate rose 30x while the recycle rate rose only ~5x
    assert late.forfeit_per_day == pytest.approx(30.0 * early.forfeit_per_day)
    assert late.remainder_per_day == pytest.approx(30.0 * early.remainder_per_day)
    assert late.recycle_per_day == pytest.approx(
        early.recycle_per_day * CFG.alternative_horizon_days
    )


def test_never_exits_early_at_or_past_settlement():
    """days_to_settlement <= 0 => never an early exit. There are no capital-days left to buy, the
    position liquidates itself for free, and it is also the guard that keeps the 1/days arithmetic
    from going negative and inverting the comparison."""
    for days in (0.0, -0.5, -10.0):
        d = decide(edge=5.0, days=days)       # an absurdly good alternative must not tempt it
        assert d.decision == HOLD
        assert d.reason == R_AT_SETTLEMENT
        assert d.sell_legs == []


def test_days_floor_caps_the_divergence():
    """Inside the last fraction of a day the forfeit's implied daily rate would run to infinity for
    arithmetic rather than economic reasons. days_to_settlement_floor truncates it, the same way
    risk.tau_floor truncates Browne's 1/sqrt(tau). BOTH per-day figures share the same floor."""
    tiny = CFG.days_to_settlement_floor / 10.0
    d = decide(edge=0.0, days=tiny)
    assert d.remainder_per_day == pytest.approx(REMAINDER / CFG.days_to_settlement_floor)
    assert d.forfeit_per_day == pytest.approx(FORFEIT / CFG.days_to_settlement_floor)
    assert d.decision == HOLD


# --------------------------------------------------------------------------- #
# (B) liquidity / slippage, and the arithmetic of the gain
# --------------------------------------------------------------------------- #
def test_hold_when_the_book_is_too_thin():
    """Measured bid depth runs from ~134 contracts to ~64,000. A 1000-contract leg against a
    134-contract bid cannot be exited, however good the gain and the per-day arithmetic look. The
    basket must hold and settle at the wall — and the reason must say DEPTH, not "no opportunity",
    or whoever reads the log goes hunting for the wrong problem."""
    d = decide(edge=0.50, days=30.0, book_a=book([(BID_A, 134)]))
    assert d.decision == HOLD
    assert d.reason == R_BOOK_TOO_THIN
    assert d.sell_legs == []
    assert d.meta["unfillable_legs"] == ["101"]
    # the gain genuinely looked good — that is exactly why this check has to exist
    assert d.realizable_gain > CFG.min_realizable_gain


def test_hold_when_a_leg_has_no_bid_at_all():
    """A one-sided book is common pre-open. No bid is not a zero price — it means the leg cannot be
    sold, so the basket cannot be exited."""
    d = decide(edge=0.50, days=30.0, book_a=book([]))
    assert d.decision == HOLD
    assert d.reason in (R_BOOK_TOO_THIN, R_NO_PRICE)
    assert d.sell_legs == []


def test_slippage_exceeding_the_gain_holds():
    """A book deep enough to fill but priced so badly below the top that walking it costs more than
    the gain. The fill is possible; it is just not worth doing."""
    # 500 @ 0.49 then 500 @ 0.30 -> avg 0.395 -> $95 of slippage against a $60 pre-slippage gain
    d = decide(edge=0.50, days=30.0, book_a=book([(BID_A, 500), (0.30, 500)]))
    assert d.meta["slippage"] == pytest.approx(QTY * (BID_A - 0.395))
    assert d.meta["slippage"] == pytest.approx(95.0)
    assert d.decision == HOLD
    assert d.reason == R_SLIPPAGE_EXCEEDS_GAIN


def test_fees_and_slippage_reduce_the_realizable_gain_by_exactly_their_amount():
    """The arithmetic, not just the sign.

        realizable_gain = current_basket_value - entry_cost - fees - slippage

    with current_basket_value priced at the BID. Fees follow the relationship engine's convention —
    a fraction per 1.0 of BASKET (one contract on each leg), not per leg — so entry and exit agree
    about whether the trade ever made money.
    """
    clean = decide(edge=0.03, days=30.0)
    assert clean.meta["current_basket_value"] == pytest.approx(CURRENT_VALUE)
    assert clean.meta["slippage"] == pytest.approx(0.0)
    assert clean.meta["fee_cost"] == pytest.approx(CFG.fees * QTY)
    assert clean.realizable_gain == pytest.approx(GROSS_GAIN - CFG.fees * QTY)

    # a 1%-of-basket fee costs exactly 0.01 x 1000 = $10
    feecfg = dataclasses.replace(CFG, fees=0.01)
    fee_only = decide(edge=0.03, days=30.0, cfg=feecfg)
    assert fee_only.meta["fee_cost"] == pytest.approx(10.0)
    assert fee_only.realizable_gain == pytest.approx(GROSS_GAIN - 10.0)
    assert fee_only.realizable_gain == pytest.approx(clean.realizable_gain + CFG.fees * QTY - 10.0)

    # slippage of exactly $20: 800 @ 0.49 + 200 @ 0.39 -> avg 0.470 -> 1000 x 0.02
    slipped = decide(edge=0.03, days=30.0, book_a=book([(BID_A, 800), (0.39, 200)]))
    assert slipped.meta["slippage"] == pytest.approx(20.0)
    assert slipped.realizable_gain == pytest.approx(GROSS_GAIN - CFG.fees * QTY - 20.0)
    assert slipped.realizable_gain == pytest.approx(clean.realizable_gain - 20.0)

    # both together are additive
    both = decide(edge=0.03, days=30.0, cfg=feecfg, book_a=book([(BID_A, 800), (0.39, 200)]))
    assert both.realizable_gain == pytest.approx(GROSS_GAIN - 10.0 - 20.0)

    # and the SAME two costs show up on the hold side, as part of the forfeit
    assert both.forfeit == pytest.approx(REMAINDER + 10.0 + 20.0)
    assert both.meta["forfeit_identity"] == "remainder + fee_cost + slippage"


def test_exact_forfeit_makes_exits_harder_than_the_bare_remainder():
    """THE POINT OF THE EXACT FORFEIT. A basket that WOULD have exited under the bare-remainder rule
    must now HOLD, because the forfeit is larger.

    Cashflows: holding receives `settlement_value`; exiting receives
    `current_basket_value - fee_cost - slippage`. So exiting gives up
    `remainder + fee_cost + slippage`, not just `remainder`. Charging the exit costs only to the exit
    side (which is what comparing against the bare remainder does) understates the forfeit by exactly
    `fee_cost + slippage` and authorises exits that lose to simply holding. With $20 of real
    slippage on a $10 remainder that understatement is a factor of three, so it is not a rounding
    detail — it moves the decision.
    """
    # --- slippage version: 800 @ 0.49 + 200 @ 0.39 -> avg 0.470 -> exactly $20 of slippage
    slip_book = book([(BID_A, 800), (0.39, 200)])
    probe = decide(edge=0.0, days=30.0, book_a=slip_book)
    assert probe.meta["slippage"] == pytest.approx(20.0)
    assert probe.forfeit == pytest.approx(REMAINDER + FEE_COST + 20.0)
    assert probe.forfeit > probe.remainder, "exit costs must ENLARGE the forfeit"

    freed = ENTRY_COST + probe.realizable_gain
    bare = threshold_edge(forfeit=REMAINDER, freed=freed, days=30.0)
    exact = threshold_edge(forfeit=probe.forfeit, freed=freed, days=30.0)
    assert exact > bare, "the exact forfeit must raise the bar for exiting"

    e = (bare + exact) / 2.0          # clears the old bar, fails the new one
    d = decide(edge=e, days=30.0, book_a=slip_book)
    assert d.recycle_per_day > d.remainder_per_day * (1.0 + CFG.min_recycle_advantage), (
        "the bare-remainder rule would have exited here — that is what makes this test meaningful"
    )
    assert d.recycle_per_day < d.forfeit_per_day * (1.0 + CFG.min_recycle_advantage)
    assert d.decision == HOLD
    assert d.reason == R_NO_BETTER_OPPORTUNITY

    # --- fee version: the same inversion driven by fees alone, on a perfectly deep book
    feecfg = dataclasses.replace(CFG, fees=0.01)          # $10 per basket
    fee_probe = decide(edge=0.0, days=30.0, cfg=feecfg)
    assert fee_probe.meta["slippage"] == pytest.approx(0.0)
    assert fee_probe.forfeit == pytest.approx(REMAINDER + 10.0)

    fee_freed = ENTRY_COST + fee_probe.realizable_gain
    fee_bare = threshold_edge(forfeit=REMAINDER, freed=fee_freed, days=30.0, cfg=feecfg)
    fee_exact = threshold_edge(forfeit=fee_probe.forfeit, freed=fee_freed, days=30.0, cfg=feecfg)
    assert fee_exact > fee_bare

    fd = decide(edge=(fee_bare + fee_exact) / 2.0, days=30.0, cfg=feecfg)
    assert fd.recycle_per_day > fd.remainder_per_day * (1.0 + feecfg.min_recycle_advantage)
    assert fd.decision == HOLD
    assert fd.reason == R_NO_BETTER_OPPORTUNITY


def test_current_value_is_the_bid_never_the_mid():
    """Using the mid would add half a spread of fabricated value on EVERY leg. These books carry a
    ~0.08 seeded spread, so on a 2-leg basket that is ~$80 of value that does not exist — larger than
    most arb edges, and a systematic source of bad exits. The bid is the only price we can realise."""
    d = decide(edge=0.03, days=30.0)
    mid_value = ((BID_A + 0.04) + (BID_B + 0.04)) * QTY        # what the mid would have claimed
    assert d.meta["current_basket_value"] == pytest.approx(CURRENT_VALUE)
    assert d.meta["current_basket_value"] < mid_value
    assert d.realizable_gain < mid_value - ENTRY_COST


def test_min_realizable_gain_is_an_absolute_floor():
    """Below the floor the exit is not worth an order-path round trip (or the write budget it
    consumes), whatever the per-day arithmetic says."""
    floored = dataclasses.replace(CFG, min_realizable_gain=GROSS_GAIN + 1.0)
    d = decide(edge=0.50, days=30.0, cfg=floored)
    assert d.decision == HOLD
    assert d.reason == R_GAIN_BELOW_MINIMUM


def test_a_losing_basket_is_never_exited():
    """Bids below the entry cost means exiting crystallises a LOSS while the basket is still
    contracted to pay its full settlement value. Holding is strictly dominant."""
    under = basket(entry_cost=1050.0)        # paid 1.05 for a basket worth 1.00 (a mis-entry)
    d = decide(edge=0.50, days=30.0, bask=under)
    assert d.decision == HOLD
    assert d.realizable_gain < 0.0


# --------------------------------------------------------------------------- #
# atomicity and the batch API
# --------------------------------------------------------------------------- #
def test_exit_is_all_legs_or_none():
    """A partial exit is impossible by construction. The entry was atomic because selling one leg of
    a complementary basket leaves a NAKED directional bet on a race — the one outcome the atomic
    entry exists to prevent. So either every leg is offered at its full quantity, or none is."""
    full = decide(edge=0.03, days=30.0)
    assert full.decision == EXIT
    assert len(full.sell_legs) == len(basket()["legs"])
    for leg in full.sell_legs:
        assert leg["action"] == "sell"
        assert leg["side"] == "yes"
        assert leg["quantity"] == QTY            # full size, integer contracts
        assert isinstance(leg["quantity"], int)
        assert 0.0 < leg["price"] < 1.0
        assert round(leg["price"] / 0.005, 6) == round(round(leg["price"] / 0.005), 6)

    # one thin leg kills the WHOLE basket — never a half exit
    half = decide(edge=0.03, days=30.0, book_b=book([(BID_B, 10)]))
    assert half.decision == HOLD
    assert half.sell_legs == []


def test_sell_limit_price_is_snapped_against_us():
    """The limit is the deepest bid level touched (so the whole quantity can fill), snapped DOWN to
    the 0.005 tick. Snapping the profitable way would let a sub-tick rounding artefact manufacture a
    gain that cannot actually be realised."""
    d = decide(edge=0.03, days=30.0, book_a=book([(0.4875, 2000)]))
    leg_a = next(leg for leg in d.sell_legs if leg["exchange_id"] == "101")
    assert leg_a["price"] == 0.485            # floor(0.4875 / 0.005) * 0.005
    assert leg_a["price"] <= 0.4875


def test_decide_exits_fans_across_baskets_preserving_order():
    """The batch API judges each basket independently against the same alternative edge and keeps
    input order, so the caller can zip decisions back onto its own basket list."""
    px, books = world()
    baskets = [
        basket(basket_id="b1"),
        basket(basket_id="b2", entry_cost=1050.0),       # losing -> hold
        basket(basket_id="b3"),
    ]
    out = decide_exits(
        baskets, prices=px, books=books, best_alternative_edge=0.03,
        days_to_settlement=30.0, cfg=CFG,
    )
    assert [d.basket_id for d in out] == ["b1", "b2", "b3"]
    assert [d.decision for d in out] == [EXIT, HOLD, EXIT]
    assert decide_exits([], prices=px, books=books, best_alternative_edge=0.03,
                        days_to_settlement=30.0, cfg=CFG) == []


def test_decide_exits_does_no_capacity_allocation_and_says_so():
    """PINS THE CALLER CONTRACT. Every basket is judged against the SAME alternative edge, so the
    alternative is implicitly treated as having unbounded capacity: three qualifying baskets all come
    back as "exit" even if the alternative can only absorb one.

    That is deliberate — capacity allocation needs the live deployment budget, which the allocator /
    risk gate owns, and a second copy of that accounting inside a pure decision function would drift
    from the first. But acting on all three would have two of them give up a guaranteed payoff for
    IDLE CASH, so the hazard must be impossible for an integrator to miss. This test pins both the
    behaviour and the fact that the docstring states the constraint.
    """
    px, books = world()
    out = decide_exits(
        [basket(basket_id=f"b{i}") for i in range(3)],
        prices=px, books=books, best_alternative_edge=0.03,
        days_to_settlement=30.0, cfg=CFG,
    )
    assert [d.decision for d in out] == [EXIT, EXIT, EXIT]
    # identical baskets get identical advantages, so the caller has to rank and throttle them itself
    assert len({round(d.recycle_per_day - d.meta["hurdle_per_day"], 9) for d in out}) == 1

    doc = decide_exits.__doc__ or ""
    assert "CALLER IS RESPONSIBLE" in doc
    assert "capacity" in doc.lower()


def test_decision_serialises_with_a_full_audit_trace():
    """An exit forfeits guaranteed money, so every one has to be defensible from the log alone,
    without a market snapshot that no longer exists.

    Critically, the serialised form must carry BOTH `remainder_per_day` (the contract field) and
    `forfeit_per_day` (the quantity the decision actually turned on). A log that reports one while
    the rule used the other is worse than either choice alone.
    """
    decision = decide(edge=0.03, days=30.0)
    d = decision.to_dict()
    for key in (
        "basket_id", "decision", "reason", "sell_legs", "realizable_gain", "remainder",
        "remainder_per_day", "forfeit", "forfeit_per_day", "recycle_per_day", "meta",
    ):
        assert key in d, key
    for key in (
        "current_basket_value", "slippage", "fee_cost", "freed_capital", "hurdle_per_day",
        "hurdle_basis", "forfeit", "forfeit_per_day", "forfeit_identity",
        "alternative_horizon_days", "days_to_settlement", "best_alternative_edge", "legs",
    ):
        assert key in d["meta"], key
    assert d["meta"]["legs"][0]["avg_fill"] == pytest.approx(BID_A)
    # the reported hurdle is reconstructible from the reported forfeit rate, so the log is auditable
    assert d["meta"]["hurdle_per_day"] == pytest.approx(
        d["forfeit_per_day"] * (1.0 + CFG.min_recycle_advantage), abs=1e-6
    )
    # a HOLD carries the same trace — otherwise the refusals are the ones you cannot review
    h = decide(edge=0.0, days=30.0).to_dict()
    for key in ("forfeit", "forfeit_per_day", "remainder", "remainder_per_day"):
        assert key in h and key in h["meta"], key


def test_alternative_horizon_is_capped_at_the_wall():
    """Nothing can take longer than settlement, so the alternative's holding period is capped at
    days_to_settlement. Setting alternative_horizon_days >= the competition length therefore recovers
    the strictly-conservative reading in which the days cancel on both sides and recycling can only
    win by having a bigger TOTAL edge — a config value, not a code change."""
    _, horizon_early = recycle_return_per_day(1000.0, 0.03, 30.0, CFG)
    assert horizon_early == pytest.approx(CFG.alternative_horizon_days)
    _, horizon_late = recycle_return_per_day(1000.0, 0.03, 1.0, CFG)
    assert horizon_late == pytest.approx(1.0)

    conservative = dataclasses.replace(CFG, alternative_horizon_days=400.0)
    rate, horizon = recycle_return_per_day(1000.0, 0.03, 30.0, conservative)
    assert horizon == pytest.approx(30.0)
    # days cancel: the comparison reduces to freed x edge  vs  remainder x (1+margin)
    assert rate * 30.0 == pytest.approx(1000.0 * 0.03)


def test_exit_config_from_cfg_rejects_a_missing_key():
    """A missing key must name itself rather than KeyError-ing somewhere in the decision path."""
    raw = {"exit": {k: v for k, v in CFG.raw["exit"].items() if k != "min_recycle_advantage"}}
    with pytest.raises(ValueError, match="min_recycle_advantage"):
        ExitConfig.from_cfg(raw)
    with pytest.raises(ValueError, match="exit"):
        ExitConfig.from_cfg({})
