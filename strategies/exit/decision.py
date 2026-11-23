"""Should this arb basket be sold NOW, or left to settle at the Nov-4 wall?

READ THIS BEFORE CHANGING ANYTHING HERE.

The bot enters hard-arb baskets atomically and, until this module existed, held every one of them to
settlement. With ~$85,000 deployable and ~$30,000 per full-size basket that is about THREE trades for
the whole competition, while the leaderboard runs 30+ in-and-out trades. The fix is to recycle
capital. The trap is that "recycle capital" is one short step from "sell whenever the screen is
green", and that step is a losing one.

WHY "ALWAYS EXIT WHEN GREEN" IS WRONG
    A hard arb is not a directional position whose profit might evaporate. It is a basket that spans
    every outcome of a race, so it pays exactly `settlement_value` on Nov 4 NO MATTER WHAT. The
    unrealised part of that payoff is not at risk; it is CONTRACTED. Selling early does not "lock in
    a gain" — it FORFEITS a guaranteed remainder in exchange for getting the cash sooner. That is a
    financing decision, not a risk decision, and the only thing that can justify it is a better use
    for the cash over the time you would otherwise have waited.

So the rule has two conjunctive conditions, and failing EITHER means HOLD:

  (A) RECYCLING BEATS HOLDING, PER UNIT OF TIME.
      The freed capital's expected return-per-day in the best currently-available alternative must
      exceed the FORFEIT's return-per-day if held to the wall, by a configured margin.

  (B) THE EXIT BOOK CAN ABSORB THE SELL.
      There must be enough bid depth that slippage does not eat the realisable gain. Measured bid
      depth across these books ranges from ~134 contracts to ~64,000; a $15,000 leg is genuinely
      unsellable on the thin end. An arb you cannot exit cleanly should settle at the wall — that is
      not a failure mode, it is the position doing exactly what it was bought to do.

WHAT EXACTLY IS FORFEITED (the hold side of the comparison — and it is NOT the bare `remainder`)
    Line the two branches up as cashflows and the forfeit falls out:

        HOLD  ->  receive  settlement_value                           at the wall
        EXIT  ->  receive  current_basket_value - fee_cost - slippage  now

        remainder = settlement_value - current_basket_value
        forfeit   = settlement_value - (current_basket_value - fee_cost - slippage)
                  = remainder + fee_cost + slippage

    The exit costs belong on BOTH sides. They shrink the cash the exit hands back — which is why
    `freed_capital = entry_cost + realizable_gain` is already net of them — AND they enlarge what
    exiting gives up. Charging them only to the exit side (i.e. comparing against the bare
    `remainder`) understates the forfeit by exactly `fee_cost + slippage` and so authorises exits
    that lose to simply holding. Condition (A) therefore compares against `forfeit_per_day`.

    `remainder` and `remainder_per_day` are still reported, with their original meaning, because they
    are part of the agreed output contract the integration layer reads. `forfeit` and
    `forfeit_per_day` are reported next to them so the audit trace shows the quantity that ACTUALLY
    drove the decision — a log that prints one number while the decision turned on another is worse
    than either choice alone.

WHY RETURN-PER-DAY IS THE RIGHT DENOMINATOR
    Both sides of the comparison are "money per unit of capital-time", because what is scarce is
    capital-days, not capital. A $300 forfeit sitting on $30,000 for 30 days is $10/day — trivially
    beaten by almost any live mispricing. The SAME $300 forfeit with 1 day left is $300/day, which
    almost nothing beats. The arithmetic therefore produces the behaviour we want for free:

        early in the competition   -> forfeit spread over many days  -> low hold rate -> EXIT often
        late in the competition    -> forfeit compressed into hours   -> high hold rate -> HOLD
        opportunities dry up       -> recycle rate collapses to ~0    -> HOLD, always

    None of those three behaviours is coded as a special case. They are consequences of dividing by
    days. That is the whole point of writing it this way: the rule adapts to the competition clock
    and to the opportunity set without a calendar of hardcoded thresholds.

THE HOLDING-PERIOD CONVENTION FOR THE ALTERNATIVE (a real choice — documented, not assumed)
    `recycle_per_day = (freed_capital * best_alternative_edge) / horizon`, and the question is what
    `horizon` should be. Two readings:

      1. STRICTLY CONSERVATIVE: the alternative is also a settle-at-the-wall hard arb, so
         horizon == days_to_settlement. Then the days cancel on both sides and recycling can only win
         by having a strictly BIGGER total edge than the forfeit. Defensible, but it throws away
         the real reason recycling is valuable (you can recycle AGAIN) and it makes the decision
         completely insensitive to the clock — which contradicts the observed fact that late exits
         should be rarer.

      2. WHAT IS IMPLEMENTED: horizon = min(cfg.alternative_horizon_days, days_to_settlement),
         floored by cfg.days_to_settlement_floor. The capital freed today goes into the next basket,
         which itself converges and can itself be exited — empirically on the order of days, not
         weeks. The cap at days_to_settlement is a hard fact (nothing can take longer than the wall)
         and is what makes the rule clock-aware: with 30 days left the alternative turns over in
         `alternative_horizon_days` while the forfeit is smeared across 30, so exit is easy; with
         1 day left BOTH horizons are 1 day, the days cancel, and exit requires a strictly bigger
         edge. Reading (1) is recoverable exactly by setting `alternative_horizon_days` to at least
         the competition length — so the conservative reading is a config value, not a code change.

    `best_alternative_edge` is a FRACTION OF DEPLOYED CAPITAL (0.03 == 3% on the money put up), not
    a per-contract edge in cents. The caller is expected to hand over the best live opportunity's
    edge_amount divided by the capital that opportunity would consume. Stated here because mixing
    the two units would be a silent factor-of-20 error.

WHY THE REALISABLE PRICE IS THE BID, NEVER THE MID
    Current basket value is computed from the BID — the price we could actually sell into. Using the
    mid would overstate every basket by half the spread on every leg, and these books carry a
    uniform ~0.08 seeded spread: on a 2-leg basket that is ~0.08 of fabricated value per contract,
    which is larger than most arb edges. A systematic overstatement of the gain is a systematic
    source of bad exits, so the mid does not appear anywhere in this module.

PURITY
    No network, no DB, no clock, no async. Prices, books, the alternative edge and the number of days
    left are all INJECTED — orderbook depth in particular is not in the state DB (the `prices` table
    has only best_bid/best_ask/spread/sequence), so the caller fetches it. The relationship engine is
    the mirror image of this module: prices -> opportunities, never reading positions. This one is
    positions + prices -> exit decision, never reading the market.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strategies.relationship_engine.contracts import Leg, best_bid, index_prices
from strategies.sizing.impact import fill_through, round_limit_price

from .config import EXIT, HOLD, ExitConfig

# Machine-readable reasons. Every decision carries exactly one, so the audit log can be grouped by
# cause without parsing prose.
R_RECYCLE_BEATS_HOLD = "recycle_beats_hold"
#: The basket's realisable value already covers its settlement value net of exit costs, so the EXACT
#: forfeit is <= 0: holding buys literally nothing. Keyed off `forfeit`, never the bare `remainder`.
R_REMAINDER_EXHAUSTED = "remainder_exhausted"
R_NO_BETTER_OPPORTUNITY = "no_better_opportunity"
R_BOOK_TOO_THIN = "book_too_thin"
R_SLIPPAGE_EXCEEDS_GAIN = "slippage_exceeds_gain"
R_GAIN_BELOW_MINIMUM = "gain_below_minimum"
R_AT_SETTLEMENT = "at_settlement"
R_NO_LEGS = "no_legs"
R_NO_PRICE = "no_price"


@dataclass(frozen=True)
class ExitDecision:
    """One basket's verdict plus the entire arithmetic that produced it.

    `meta` is deliberately a full trace rather than a summary. An exit forfeits guaranteed money, so
    every one of them has to be defensible after the fact from the log alone — without re-running the
    bot against a market snapshot that no longer exists.

    `remainder` / `remainder_per_day` keep their original contract meaning (settlement_value minus
    the realisable basket value, BEFORE exit costs), because the integration layer reads them.
    `forfeit` / `forfeit_per_day` are the EXACT amounts condition (A) compares against —
    `remainder + fee_cost + slippage` — and are first-class fields rather than buried in `meta`
    precisely because they, not the remainder, are what the decision turned on.
    """

    basket_id: str
    decision: str                  # "exit" | "hold"
    reason: str                    # one of the R_* constants above
    sell_legs: list[dict]          # [] when holding; else the orders to place, whole basket
    realizable_gain: float
    remainder: float               # settlement_value - current_basket_value  (contract field)
    remainder_per_day: float
    forfeit: float                 # remainder + fee_cost + slippage  <- what (A) actually compares
    forfeit_per_day: float
    recycle_per_day: float
    meta: dict = field(default_factory=dict)

    @property
    def is_exit(self) -> bool:
        return self.decision == EXIT

    def to_dict(self) -> dict:
        return {
            "basket_id": self.basket_id,
            "decision": self.decision,
            "reason": self.reason,
            "sell_legs": [dict(leg) for leg in self.sell_legs],
            "realizable_gain": round(float(self.realizable_gain), 6),
            "remainder": round(float(self.remainder), 6),
            "remainder_per_day": round(float(self.remainder_per_day), 6),
            "forfeit": round(float(self.forfeit), 6),
            "forfeit_per_day": round(float(self.forfeit_per_day), 6),
            "recycle_per_day": round(float(self.recycle_per_day), 6),
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class LegExit:
    """What one leg's sell would actually realise, after walking the bids."""

    exchange_id: object
    side: str
    quantity: int
    top_bid: float          # the realisable top-of-book price (never the mid)
    avg_price: float        # volume-weighted average over the depth consumed
    limit_price: float      # the deepest level touched, tick-snapped AGAINST us
    proceeds: float         # quantity * top_bid   (slippage is accounted separately)
    slippage: float         # quantity * (top_bid - avg_price), floored at 0
    levels_used: int
    fillable: bool


def _as_int_qty(value) -> int:
    """Contract counts are integers on SIG. The persisted basket carries `quantity` as a float (the
    state layer's column type), so round rather than truncate — truncating 999.9999 to 999 would
    leave one naked contract behind and silently un-hedge the basket."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _book_for(books, exchange_id):
    """Depth lookup tolerant of int-vs-str exchange ids, same reason index_prices indexes both: the
    market payload and the price payload do not reliably agree on the id's type, and a silent
    mismatch here would read as 'no book' and freeze every exit."""
    if not books:
        return None
    book = books.get(exchange_id)
    if book is None:
        book = books.get(str(exchange_id))
    return book if isinstance(book, dict) else None


def _top_bid(price_row: dict | None, book: dict | None) -> float | None:
    """The most conservative realisable bid available from the two independent sources.

    `prices` and `books` are separate fetches and can disagree (different sequence numbers). Taking
    the MINIMUM means a disagreement can only ever understate what we would realise. Overstating it
    is the failure that manufactures bad exits, so the asymmetry is deliberate.
    """
    quotes: list[float] = []
    from_prices = best_bid(price_row)
    if from_prices is not None and 0.0 < from_prices < 1.0:
        quotes.append(float(from_prices))
    levels = (book or {}).get("bids") or []
    tops: list[float] = []
    for lvl in levels:
        if isinstance(lvl, dict):
            px, qty = lvl.get("price"), lvl.get("quantity")
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            px, qty = lvl[0], lvl[1]
        else:
            continue
        try:
            px_f, qty_i = float(px), int(float(qty))
        except (TypeError, ValueError):
            continue
        if qty_i > 0 and 0.0 < px_f < 1.0:
            tops.append(px_f)
    if tops:
        quotes.append(max(tops))
    if not quotes:
        return None
    return min(quotes)


def evaluate_leg_exit(leg: dict, *, price_row: dict | None, book: dict | None) -> LegExit | None:
    """Walk the BIDS for one leg's full quantity. None when the leg has no realisable price at all.

    Selling consumes bids, so `fill_through(..., action="sell")` — the sizing module's book-walker is
    reused verbatim rather than reimplemented, because it already handles the two level encodings,
    the sort direction, and the partial-availability report, and it is the function the entry path
    was validated against.

    `fillable` is False when the bids cannot supply the WHOLE leg. That is not a smaller exit: a
    partial sell on one leg of a complementary basket leaves a naked directional position on a race,
    which is the one outcome the atomic entry exists to prevent.
    """
    quantity = _as_int_qty(leg.get("quantity"))
    if quantity <= 0:
        return None
    top = _top_bid(price_row, book)
    if top is None:
        return None
    side = str(leg.get("side") or "yes")
    fill = fill_through(book, quantity, action="sell", side=side)
    if fill is None or fill.quantity <= 0:
        return LegExit(
            exchange_id=leg.get("exchange_id"),
            side=side,
            quantity=quantity,
            top_bid=top,
            avg_price=top,
            limit_price=round_limit_price(top, "sell"),
            proceeds=quantity * top,
            slippage=0.0,
            levels_used=0,
            fillable=False,
        )
    # Slippage is floored at 0: `top` is the MINIMUM of two possibly-disagreeing sources, so the
    # walked average can legitimately come out above it. A negative slippage would be a credit we
    # cannot actually collect.
    slippage = max(0.0, quantity * (top - fill.avg_price))
    return LegExit(
        exchange_id=leg.get("exchange_id"),
        side=side,
        quantity=quantity,
        top_bid=top,
        avg_price=fill.avg_price,
        limit_price=round_limit_price(fill.worst_price, "sell"),
        proceeds=quantity * top,
        slippage=slippage,
        levels_used=fill.levels_used,
        fillable=fill.quantity >= quantity,
    )


def recycle_return_per_day(
    freed_capital: float,
    best_alternative_edge: float,
    days_to_settlement: float,
    cfg: ExitConfig,
) -> tuple[float, float]:
    """(recycle_per_day, horizon_days) for the freed capital.

    horizon = min(cfg.alternative_horizon_days, days_to_settlement), floored. See the module
    docstring for the full argument; the cap at the wall is what makes the rule clock-aware, and
    setting alternative_horizon_days >= the competition length collapses this to the strictly
    conservative "the alternative is held to settlement too" reading.
    """
    edge = max(0.0, float(best_alternative_edge))      # a negative "opportunity" is no opportunity
    capital = max(0.0, float(freed_capital))
    horizon = min(float(cfg.alternative_horizon_days), max(float(days_to_settlement), 0.0))
    horizon = max(horizon, cfg.days_to_settlement_floor)
    return (capital * edge) / horizon, horizon


def decide_basket_exit(
    basket: dict,
    *,
    prices,
    books: dict | None,
    best_alternative_edge: float,
    days_to_settlement: float,
    cfg: ExitConfig,
) -> ExitDecision:
    """Exit-or-hold for ONE basket. Pure: dicts in, ExitDecision out.

    Check order is load-bearing, because the reason string is what the operator reads:

      0. settlement reached/passed   -> HOLD. There is nothing to recycle INTO across zero days, and
                                        the position liquidates itself for free. Also the guard that
                                        keeps the 1/days arithmetic from going negative.
      1. no legs / no price          -> HOLD. We cannot price what we cannot see.
      2. (B) every leg fully sellable-> else HOLD "book_too_thin". ALL legs or none.
      3. (B) slippage within margin  -> else HOLD "slippage_exceeds_gain".
      4. gain clears the floor       -> else HOLD "gain_below_minimum".
      5. (A) recycle beats hold      -> EXIT "recycle_beats_hold" / HOLD "no_better_opportunity".
                                        The hurdle is built from `forfeit_per_day`, where
                                        forfeit = remainder + fee_cost + slippage — see the module
                                        docstring for the cashflow derivation.

    (B) is checked before (A) on purpose: an unsellable basket's per-day arithmetic is fiction, and
    reporting "no_better_opportunity" on a basket that was actually untradeable would send whoever
    reads the log hunting for the wrong problem.
    """
    basket_id = str(basket.get("basket_id") or "")
    legs = list(basket.get("legs") or [])
    entry_cost = float(basket.get("entry_cost") or 0.0)
    settlement_value = float(basket.get("settlement_value") or 0.0)
    price_index = index_prices(prices)
    days = float(days_to_settlement)

    base_meta: dict = {
        "strategy_id": basket.get("strategy_id"),
        "kind": basket.get("kind"),
        "n_legs": len(legs),
        "entry_cost": round(entry_cost, 6),
        "settlement_value": round(settlement_value, 6),
        "days_to_settlement": round(days, 6),
        "days_floor": cfg.days_to_settlement_floor,
        "best_alternative_edge": float(best_alternative_edge),
        "fees_frac": cfg.fees,
        "min_recycle_advantage": cfg.min_recycle_advantage,
        "liquidity_safety_margin": cfg.liquidity_safety_margin,
        "min_realizable_gain": cfg.min_realizable_gain,
    }

    def hold(reason: str, **extra) -> ExitDecision:
        """Build a HOLD, reading the numeric fields back out of the accumulated trace.

        `base_meta` is filled in as each quantity becomes known, so a hold taken at ANY check
        reports exactly the figures that check had available and zero for the ones it never reached.
        Reading them back here rather than re-passing them at every call site is what keeps the
        fields and the audit trace from drifting apart — which is the specific bug that made the
        bare-remainder version hard to review in the first place.
        """
        meta = {**base_meta, **extra}
        return ExitDecision(
            basket_id=basket_id,
            decision=HOLD,
            reason=reason,
            sell_legs=[],
            realizable_gain=float(meta.get("realizable_gain", 0.0)),
            remainder=float(meta.get("remainder", 0.0)),
            remainder_per_day=float(meta.get("remainder_per_day", 0.0)),
            forfeit=float(meta.get("forfeit", 0.0)),
            forfeit_per_day=float(meta.get("forfeit_per_day", 0.0)),
            recycle_per_day=float(meta.get("recycle_per_day", 0.0)),
            meta=meta,
        )

    # 0 — the wall. days <= 0 means the basket is settling; early exit is not merely unattractive,
    # it is incoherent (there are no capital-days left to buy).
    if days <= 0.0:
        return hold(R_AT_SETTLEMENT, note="settlement reached or passed — the basket liquidates itself")

    # 1 — nothing to price.
    if not legs:
        return hold(R_NO_LEGS)

    leg_exits: list[LegExit] = []
    for leg in legs:
        ex_id = leg.get("exchange_id")
        row = price_index.get(ex_id) or price_index.get(str(ex_id))
        evaluated = evaluate_leg_exit(leg, price_row=row, book=_book_for(books, ex_id))
        if evaluated is None:
            return hold(R_NO_PRICE, missing_leg=ex_id)
        leg_exits.append(evaluated)

    leg_trace = [
        {
            "exchange_id": le.exchange_id,
            "quantity": le.quantity,
            "top_bid": round(le.top_bid, 6),
            "avg_fill": round(le.avg_price, 6),
            "limit_price": le.limit_price,
            "slippage": round(le.slippage, 6),
            "levels_used": le.levels_used,
            "fillable": le.fillable,
        }
        for le in leg_exits
    ]
    base_meta["legs"] = leg_trace

    # Fees follow the relationship engine's convention exactly: a fraction charged once per 1.0 of
    # BASKET (one contract on each leg), not per leg. Basket units = the largest leg quantity, which
    # is the conservative reading when a basket's legs are not perfectly equal.
    basket_units = max((le.quantity for le in leg_exits), default=0)
    fee_cost = cfg.fees * basket_units

    current_basket_value = sum(le.proceeds for le in leg_exits)       # at the BID, never the mid
    slippage = sum(le.slippage for le in leg_exits)
    gross_gain = current_basket_value - entry_cost - fee_cost        # pre-slippage
    realizable_gain = gross_gain - slippage
    days_eff = max(days, cfg.days_to_settlement_floor)

    # `remainder` is the contract field: settlement_value less the realisable basket value, BEFORE
    # exit costs. It is reported unchanged because the integration layer reads it.
    remainder = settlement_value - current_basket_value
    remainder_per_day = remainder / days_eff

    # `forfeit` is what exiting ACTUALLY gives up, and therefore what condition (A) compares:
    #     HOLD -> receive settlement_value;  EXIT -> receive current_basket_value - fees - slippage
    #     forfeit = settlement_value - (current_basket_value - fee_cost - slippage)
    #             = remainder + fee_cost + slippage
    # The exit costs sit on BOTH sides — they shrink `freed_capital` AND enlarge the forfeit.
    # Charging them only to the exit side understates the forfeit by exactly fee_cost + slippage and
    # authorises exits that lose to holding.
    forfeit = remainder + fee_cost + slippage
    forfeit_per_day = forfeit / days_eff

    base_meta.update(
        {
            "basket_units": basket_units,
            "fee_cost": round(fee_cost, 6),
            "current_basket_value": round(current_basket_value, 6),
            "slippage": round(slippage, 6),
            "gross_gain_pre_slippage": round(gross_gain, 6),
            "realizable_gain": round(realizable_gain, 6),
            "remainder": round(remainder, 6),
            "remainder_per_day": round(remainder_per_day, 6),
            "forfeit": round(forfeit, 6),
            "forfeit_per_day": round(forfeit_per_day, 6),
            "forfeit_identity": "remainder + fee_cost + slippage",
        }
    )

    # 2 — (B) depth. ALL legs or none: half a hedge is a directional bet on a race.
    unfillable = [le.exchange_id for le in leg_exits if not le.fillable]
    if unfillable:
        return hold(
            R_BOOK_TOO_THIN,
            unfillable_legs=unfillable,
            note="bid depth cannot absorb the whole leg; a partial exit would leave a naked leg",
        )

    # 3 — (B) slippage. The book we walked is a snapshot, so the pre-slippage gain must clear the
    # measured slippage with headroom: slippage * (1 + margin) < gross_gain.
    if slippage * (1.0 + cfg.liquidity_safety_margin) >= gross_gain:
        return hold(
            R_SLIPPAGE_EXCEEDS_GAIN,
            slippage_with_margin=round(slippage * (1.0 + cfg.liquidity_safety_margin), 6),
        )

    # 4 — absolute floor. Below this the order-path round trip is not worth the write budget.
    if realizable_gain < cfg.min_realizable_gain:
        return hold(R_GAIN_BELOW_MINIMUM)

    # 5 — (A) the financing comparison. Freed capital is the NET cash the exit actually returns, so
    # the recycled amount is already net of fees and slippage.
    freed_capital = max(0.0, entry_cost + realizable_gain)
    recycle_per_day, horizon = recycle_return_per_day(
        freed_capital, best_alternative_edge, days, cfg
    )
    # The hurdle is built from the EXACT forfeit, never the bare remainder.
    hurdle = max(forfeit_per_day, 0.0) * (1.0 + cfg.min_recycle_advantage)
    base_meta.update(
        {
            "freed_capital": round(freed_capital, 6),
            "alternative_horizon_days": round(horizon, 6),
            "recycle_per_day": round(recycle_per_day, 6),
            "hurdle_per_day": round(hurdle, 6),
            "hurdle_basis": "forfeit_per_day",
        }
    )

    sell_legs = [
        Leg(
            exchange_id=le.exchange_id,
            side=le.side,
            action="sell",
            price=le.limit_price,
            quantity=le.quantity,
        ).to_dict()
        for le in leg_exits
    ]

    def exit_now(reason: str) -> ExitDecision:
        return ExitDecision(
            basket_id=basket_id,
            decision=EXIT,
            reason=reason,
            sell_legs=sell_legs,
            realizable_gain=realizable_gain,
            remainder=remainder,
            remainder_per_day=remainder_per_day,
            forfeit=forfeit,
            forfeit_per_day=forfeit_per_day,
            recycle_per_day=recycle_per_day,
            meta=base_meta,
        )

    # Degenerate-but-real case: the realisable value already covers the settlement value net of exit
    # costs, so the EXACT forfeit is <= 0 — holding buys literally nothing and the exit is free.
    # Keyed off `forfeit`, not the bare remainder: a basket with a small positive remainder but
    # larger fees+slippage still gives something up, and must go through the full comparison.
    if forfeit <= 0.0:
        return exit_now(R_REMAINDER_EXHAUSTED)

    if recycle_per_day > hurdle:
        return exit_now(R_RECYCLE_BEATS_HOLD)

    return hold(
        R_NO_BETTER_OPPORTUNITY,
        note="the guaranteed settlement payoff forgone (remainder + fees + slippage) earns more per "
             "capital-day than the best available alternative — selling would churn away contracted "
             "profit",
    )


def decide_exits(
    baskets,
    *,
    prices,
    books: dict | None,
    best_alternative_edge: float,
    days_to_settlement: float,
    cfg: ExitConfig,
) -> list[ExitDecision]:
    """Fan `decide_basket_exit` across every open basket, input order preserved.

    *** CALLER CONTRACT — READ THIS BEFORE ACTING ON MORE THAN ONE EXIT ***

    Every basket is judged independently against the SAME `best_alternative_edge`, so this function
    implicitly treats the best alternative as having UNBOUNDED CAPACITY. If three baskets each clear
    the hurdle, all three come back as "exit" — even when the alternative can only absorb one
    basket's worth of capital. Acting on all three would have two of them forfeit a guaranteed
    settlement payoff in exchange for IDLE CASH, which is strictly worse than holding and is the
    exact failure this module exists to prevent.

    THE CALLER IS RESPONSIBLE for not acting on more exits than the freed capital can actually be
    redeployed into. Rank the returned exits (largest advantage first, i.e. by
    `recycle_per_day - hurdle_per_day`), take only as many as the alternative's real capacity
    supports, and hold the rest. This function deliberately does no capacity allocation: that needs
    the live deployment budget, which the allocator / risk gate owns, and a second copy of that
    accounting living inside a pure decision function would be guaranteed to drift from the first.

    Nothing is sorted here — the ordering returned is the ordering handed in, so the caller can zip
    decisions straight back onto its own basket list.
    """
    return [
        decide_basket_exit(
            basket,
            prices=prices,
            books=books,
            best_alternative_edge=best_alternative_edge,
            days_to_settlement=days_to_settlement,
            cfg=cfg,
        )
        for basket in (baskets or [])
    ]
