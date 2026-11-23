"""Arb exit DECISIONS: sell a hedged basket now, or let it settle at the Nov-4 wall?

The entry path is atomic and, before this module, permanent — every basket was held to settlement.
With ~$85,000 deployable and ~$30,000 per full-size basket that caps the competition at about THREE
trades while the leaderboard runs 30+. This module recycles the capital, WITHOUT falling into the
trap that makes early exits lose money.

The rule, in one line:

    EXIT only if  (A) the freed capital's return-per-day in the best live alternative beats the
                      FORFEIT's return-per-day, by a configured margin
             AND  (B) the bids can absorb the WHOLE basket without slippage eating the gain.
    Fail either -> HOLD.

where, read straight off the two branches' cashflows,

    remainder = settlement_value - current_basket_value       (priced at the BID, never the mid)
    forfeit   = remainder + fee_cost + slippage               <- what (A) compares against

Exit costs sit on BOTH sides: they shrink the cash the exit returns AND enlarge what exiting gives
up. `remainder` / `remainder_per_day` are still reported with their original contract meaning for
the integration layer; `forfeit` / `forfeit_per_day` are reported alongside them because they are
what the decision actually turned on.

A hard arb's unrealised profit is CONTRACTED, not at risk. Selling early forfeits it. So this is a
financing decision measured in capital-DAYS, and dividing by days is what makes it adapt for free:
early (forfeit smeared over many days) it exits aggressively, late (forfeit compressed into hours)
and whenever the opportunity set dries up it holds. None of that is hardcoded — see decision.py for
the full argument, including the holding-period convention chosen for the alternative opportunity
and why.

`decide_exits` judges every basket against the SAME alternative edge and does NO capacity
allocation: the caller must not act on more exits than the freed capital can actually be redeployed
into. Its docstring states that contract in full.

    cfg = load_exit_config()                                  # params from config/sig.yaml `exit:`
    d   = decide_basket_exit(basket, prices=px, books=depth,
                             best_alternative_edge=0.03,
                             days_to_settlement=cfg.days_to_settlement(now), cfg=cfg)
    if d.is_exit: place_all_atomically(d.sell_legs)            # else d.reason says why not

PURE: no network, no DB, no clock, no async. Orderbook depth is NOT in the state DB, so `books` is
an injected input the caller fetches. The relationship engine is this module's mirror image (prices
-> opportunities, never reads positions); this one is positions + prices -> exit decision.
"""

from __future__ import annotations

from .config import (
    EXIT,
    HOLD,
    ExitConfig,
    load_exit_config,
    load_raw_config,
    parse_utc,
)
from .decision import (
    R_AT_SETTLEMENT,
    R_BOOK_TOO_THIN,
    R_GAIN_BELOW_MINIMUM,
    R_NO_BETTER_OPPORTUNITY,
    R_NO_LEGS,
    R_NO_PRICE,
    R_RECYCLE_BEATS_HOLD,
    R_REMAINDER_EXHAUSTED,
    R_SLIPPAGE_EXCEEDS_GAIN,
    ExitDecision,
    LegExit,
    decide_basket_exit,
    decide_exits,
    evaluate_leg_exit,
    recycle_return_per_day,
)

__all__ = [
    # entry points
    "decide_basket_exit",
    "decide_exits",
    # outputs
    "ExitDecision",
    "LegExit",
    "EXIT",
    "HOLD",
    # building blocks (exposed for tests and for callers that want the pieces)
    "evaluate_leg_exit",
    "recycle_return_per_day",
    # config
    "ExitConfig",
    "load_exit_config",
    "load_raw_config",
    "parse_utc",
    # machine-readable reasons
    "R_RECYCLE_BEATS_HOLD",
    "R_REMAINDER_EXHAUSTED",
    "R_NO_BETTER_OPPORTUNITY",
    "R_BOOK_TOO_THIN",
    "R_SLIPPAGE_EXCEEDS_GAIN",
    "R_GAIN_BELOW_MINIMUM",
    "R_AT_SETTLEMENT",
    "R_NO_LEGS",
    "R_NO_PRICE",
]
