"""Market-making inventory manager — pure logic, no I/O.

Tracks the net position per market and rebalances by SKEWING the quote: if one side
keeps filling we accumulate a directional position, so we shift BOTH quotes against the
inventory (making the reducing side more aggressive and the adding side less), and we
suppress the adding side entirely once the per-market cap is hit.

Position convention: +N = long N YES contracts (we bought YES); −N = short (we sold YES).
Buying YES increases the position, selling YES decreases it.

The cap feeds the executor's exposure gate. cap / skew parameters are PLACEHOLDERS —
re-tune at open once organic fill rates and depth are known (depth is a ≤1000 wall pre-open).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .quoter import TICK, PRICE_MIN, PRICE_MAX, Quote, round_to_tick


@dataclass(frozen=True)
class InventoryConfig:
    cap: int = 500                 # max |net contracts| per market (config: max_inventory_per_market)
    skew_ticks_at_cap: float = 4.0 # PRICE shift (in ticks) at full cap; linear in inventory
    size_skew_strength: float = 0.6  # SIZE asymmetry at full cap: reducing x(1+s), adding x(1-s)
    tick: float = TICK
    price_min: float = PRICE_MIN
    price_max: float = PRICE_MAX

    def validate(self) -> None:
        errs = []
        if self.cap < 1:
            errs.append("cap (max_inventory_per_market) must be >= 1")
        if not (0.0 <= self.size_skew_strength <= 1.0):
            # At 1.0 the adding side reaches exactly zero at the cap, which the cap pull enforces
            # anyway. Above 1.0 the multiplier would go negative, which is meaningless.
            errs.append(f"size_skew_strength={self.size_skew_strength} must be in [0, 1]")
        if self.skew_ticks_at_cap < 0.0:
            errs.append("skew_ticks_at_cap must be >= 0")
        if errs:
            raise ValueError("invalid inventory config: " + "; ".join(errs))


@dataclass
class SkewDecision:
    position: int
    shift: float                   # signed price shift applied to BOTH sides (negative when long)
    allow_bid: bool                # False once long at cap (buying would breach the cap)
    allow_ask: bool                # False once short at cap (selling would breach the cap)
    reason: str = ""
    # --- SIZE skew: the part that actually SHEDS inventory ---
    # Price skew alone only makes the reducing side more LIKELY to fill; it does not make it fill
    # BIGGER. A book repeatedly hit on one side therefore still accumulates faster than it sheds —
    # the bag-holding failure. These multipliers put real size behind the reducing side and starve
    # the adding one, which is the Avellaneda-Stoikov lean expressed in quantity as well as price.
    bid_size_mult: float = 1.0
    ask_size_mult: float = 1.0
    bid_headroom: int = 0          # contracts that may still be BOUGHT before breaching the cap
    ask_headroom: int = 0          # contracts that may still be SOLD before breaching the cap


class InventoryBook:
    """Per-market net position + quote skew. Holds only in-memory state; the executor
    reports fills back in via record_fill (it owns the broker, we own the arithmetic)."""

    def __init__(self, cfg: InventoryConfig | None = None) -> None:
        self._cfg = cfg or InventoryConfig()
        self._pos: dict[str, int] = {}
        self._caps: dict[str, int] = {}

    def position(self, market: str) -> int:
        return self._pos.get(market, 0)

    def set_cap(self, market: str, cap_contracts: int) -> int:
        """Set this market's inventory cap IN CONTRACTS, derived from a CAPITAL fraction.

        The cap is configured as `mm_max_inventory_frac` — a fraction of equity — not as a share
        count, for the same reason the base size is: 500 contracts is $10 of risk on a 0.02 market
        and $475 on a 0.95 one. A share-count cap therefore caps RISK at wildly different levels
        across a board whose prices span 0.005-0.995, which is not a risk limit at all. MarketMaker
        converts the fraction to contracts at each market's price each cycle and calls this.

        `InventoryConfig.cap` remains the fallback for when equity is unknown.
        """
        self._caps[market] = max(int(cap_contracts), 1)
        return self._caps[market]

    def cap(self, market: str) -> int:
        """Effective cap for this market: the capital-derived one, else the configured fallback."""
        return self._caps.get(market, self._cfg.cap)

    def record_fill(self, market: str, action: str, qty: float) -> int:
        """Apply a fill (a DELTA). action ∈ {buy, sell} (of YES). Returns the new position."""
        q = int(qty)
        signed = q if str(action).lower() == "buy" else -q
        self._pos[market] = self.position(market) + signed
        return self._pos[market]

    def set_position(self, market: str, qty: int) -> int:
        """Set the ABSOLUTE net position for a market and return it. The live executor syncs this
        from the reconciled venue-truth positions each cycle — the SAME source the risk gate clamps
        sells against (gate.py `positions[exch]["quantity"]`) — so the ask is bounded to what we
        ACTUALLY hold, rather than an internal fill counter that drifts across partials/restarts."""
        self._pos[market] = int(qty)
        return self._pos[market]

    def at_cap(self, market: str) -> bool:
        return abs(self.position(market)) >= self.cap(market)

    def skew(self, market: str) -> SkewDecision:
        """How to lean the quote given current inventory. Long → shift down (sell-side
        cheaper/more likely, buy-side less); short → shift up. Block the side that would
        push the position further beyond the cap."""
        pos = self.position(market)
        cap = self.cap(market)          # capital-derived when known, configured fallback otherwise
        frac = max(-1.0, min(1.0, pos / cap)) if cap > 0 else 0.0
        # Long (frac>0) → negative shift (quotes move DOWN → we offload YES); short → up.
        shift = round(-self._cfg.skew_ticks_at_cap * self._cfg.tick * frac, 3)
        allow_bid = pos < cap          # buying adds to a long; stop at the cap
        allow_ask = pos > -cap         # selling adds to a short; stop at the cap

        # SIZE skew, linear in inventory and symmetric about flat:
        #   long  (frac>0): ask (reducing) x(1+s*frac), bid (adding) x(1-s*frac)
        #   short (frac<0): mirrored
        #   flat  (frac=0): both x1 — a flat book quotes symmetrically, which is the whole point
        s = self._cfg.size_skew_strength
        bid_size_mult = max(0.0, 1.0 - s * frac)
        ask_size_mult = max(0.0, 1.0 + s * frac)

        # HEADROOM: a fill must never be able to jump THROUGH the cap. Sizing the adding side to
        # the remaining distance to the cap makes the cap hold even on a full instant fill, rather
        # than relying on being re-evaluated before the next one lands.
        bid_headroom = max(0, cap - pos)
        ask_headroom = max(0, pos + cap)

        reason = ""
        if not allow_bid:
            reason = "inventory_cap_long"
        elif not allow_ask:
            reason = "inventory_cap_short"
        return SkewDecision(pos, shift, allow_bid, allow_ask, reason,
                            bid_size_mult=bid_size_mult, ask_size_mult=ask_size_mult,
                            bid_headroom=bid_headroom, ask_headroom=ask_headroom)

    def apply(self, market: str, quote: Quote) -> Quote:
        """Return a new Quote with inventory skew applied, cap-breaching sides pulled, and the
        SELL (ask) side bounded to held long inventory.

        The ask may only ever REDUCE a held long position — never open a naked short (the risk
        gate rejects those, `sell_without_holding`). So the ask size is clamped to the held YES
        inventory and the ask is suppressed entirely while flat or short: there is nothing to sell
        until bids fill and we are long. This is what turns one-sided accumulation into round-trips
        — buy YES at the bid, then rest an ask against the held inventory to sell it back at a
        spread (inventory-neutral-ish), instead of only ever buying."""
        d = self.skew(market)
        bid = quote.bid_price
        ask = quote.ask_price

        held_long = max(0, d.position)                 # YES contracts we actually hold and may sell

        # SIZE SKEW + CAP HEADROOM, applied to the quoter's base size.
        #   * multipliers lean size toward the reducing side (see SkewDecision)
        #   * headroom stops a single fill from jumping through the cap
        #   * the ask is additionally bounded by held_long: SIG lists only YES exchanges and the
        #     risk gate rejects `sell_without_holding`, so we can only ever SELL BACK what we hold.
        #     That is a venue constraint, not a skew — it is applied last and independently.
        bid_size = int(min(quote.bid_size * d.bid_size_mult, d.bid_headroom))
        ask_size = int(min(quote.ask_size * d.ask_size_mult, d.ask_headroom, held_long))
        bid_size = max(bid_size, 0)
        ask_size = max(ask_size, 0)

        post_bid = quote.post_bid and d.allow_bid and bid_size > 0
        post_ask = quote.post_ask and d.allow_ask and ask_size > 0

        if d.shift and (bid is not None or ask is not None):
            if bid is not None:
                bid = round_to_tick(bid + d.shift, tick=self._cfg.tick,
                                    lo=self._cfg.price_min, hi=self._cfg.price_max)
            if ask is not None:
                ask = round_to_tick(ask + d.shift, tick=self._cfg.tick,
                                    lo=self._cfg.price_min, hi=self._cfg.price_max)
            # keep bid strictly below ask after the shift
            if bid is not None and ask is not None and ask - bid < self._cfg.tick - 1e-9:
                ask = round(bid + self._cfg.tick, 3)

        reason = quote.reason
        if d.reason:
            reason = f"{reason}+{d.reason}" if reason else d.reason
        if quote.post_ask and not post_ask and not d.reason:
            # the quoter wanted an ask but we hold nothing to sell — surface WHY for the executor log
            reason = f"{reason}+no_inventory_to_sell" if reason else "no_inventory_to_sell"

        return Quote(quote.exchange_id, quote.mid, bid, ask, bid_size, ask_size,
                     post_bid=post_bid, post_ask=post_ask, reason=reason)
