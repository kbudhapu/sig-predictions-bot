"""MM quote SIZE — a percentage of CAPITAL, converted to shares at the market's price.

*** NO FIXED MAXIMUM SIZE EXISTS IN THIS MODULE. *** Every bound is relative to something that
scales: equity, or the market's own depth. A fixed share ceiling is what kept this bot quoting 100
shares — $2 of risk on a 0.02 market and $95 on a 0.95 one — and no absolute number can be right
across a board whose prices span 0.005 to 0.995, or across a bankroll that changes.

HOW IT SIZES (consistent with arb and directional, which also read capital, never share counts)

    quote_capital = min( mm_capital_frac x equity / active_markets ,   # the MM sleeve, spread
                         mm_max_quote_frac x equity )                  # per-quote ceiling
    shares        = quote_capital / price                              # same $ at any price
    then, ONLY if real depth is known:  shares = min(shares, max_frac_of_depth x depth)

Both capital terms are FRACTIONS OF EQUITY, so doubling the bankroll doubles the quote. The
per-quote ceiling is also a fraction — it stops a cycle quoting only two markets from putting the
whole sleeve into one — so it scales too, and is never an absolute number.

The liquidity bound is the one non-capital bound, and it is MARKET-relative rather than fixed: you
cannot fill or exit more than the book holds, so quoting a large multiple of visible depth makes the
"captured spread" fictional. It is applied ONLY when depth is actually known. When depth is unknown
it is SKIPPED rather than replaced by a guessed constant — a constant would be a fixed maximum
wearing a liquidity costume, and because depth is not in the prices table that constant would have
silently become the binding constraint on every quote in production.

*** WHY NOT EDGE-SCALED — a different question from capital-%; do not conflate them. ***
Capital-% is "what fraction of my money goes into each quote". Edge-scaling is "bet more when I
think I am right". The first is correct and is what every strategy here does. The second turns
two-sided spread capture into a one-sided directional bet — the +430% inventory bag-hold and the
13,144-contract blowup. MM has no conviction: it earns spread x round-trip FREQUENCY, and a bigger
quote earns no more spread per round trip, it only carries more inventory between them (the
Avellaneda-Stoikov point). So nothing here accepts an edge, fair value, divergence or confidence,
and the tests assert that by signature.

Equity is REQUIRED. With no equity there is no capital percentage to compute, and the honest answer
is to decline to quote rather than fall back to an invented share count — that fallback was the
hardcoded 100, i.e. the bug.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SizingConfig:
    """All config-driven (config/sig.yaml `market_making:`). Every bound scales; none is absolute.

    Note what is deliberately ABSENT: no max_quote_size, no unknown_depth_contracts, no fallback
    share count. Each was a fixed maximum, and a fixed maximum cannot be a correct limit across
    prices or bankrolls.
    """

    mm_capital_frac: float = 0.30        # fraction of equity for MM, spread across quoted markets
    mm_max_quote_frac: float = 0.02      # per-quote ceiling, ALSO a fraction of equity
    mm_max_inventory_frac: float = 0.05  # per-market inventory cap, ALSO a fraction of equity
    max_frac_of_depth: float = 0.25      # market-relative: at most this fraction of KNOWN depth
    min_quote_size: int = 10             # a FLOOR in shares: a 2-share order wastes one of the
                                         #   30 writes/min at any price. A floor, never a maximum.

    def validate(self) -> None:
        errs = []
        for name in ("mm_capital_frac", "mm_max_quote_frac", "mm_max_inventory_frac",
                     "max_frac_of_depth"):
            v = getattr(self, name)
            if not (0.0 < float(v) <= 1.0):
                errs.append(f"{name}={v} must be in (0, 1]")
        if self.min_quote_size < 1:
            errs.append("min_quote_size must be >= 1")
        if errs:
            raise ValueError("invalid market_making sizing config: " + "; ".join(errs))


@dataclass(frozen=True)
class BaseSize:
    """The computed per-side base, with every input that produced it (so a log line explains it)."""

    size: int
    binding: str          # capital | quote_capital_ceiling | liquidity | min_floor | no_equity
    quote_capital: float  # dollars this quote commits — the number that must be price-invariant
    capital_shares: int
    liquidity_shares: int | None   # None when depth is unknown (bound not applied)
    depth_known: bool

    @property
    def sized(self) -> bool:
        return self.size > 0

    def to_dict(self) -> dict:
        return {"size": self.size, "binding": self.binding,
                "quote_capital": round(self.quote_capital, 2),
                "capital_shares": self.capital_shares,
                "liquidity_shares": self.liquidity_shares,
                "depth_known": self.depth_known}


def base_quote_size(
    *,
    equity: float | None,
    active_markets: int,
    quote_price: float,
    book_depth: int | None = None,
    cfg: SizingConfig | None = None,
) -> BaseSize:
    """Shares to rest PER SIDE in one market, before inventory skew.

    Arguments are equity, market count, price and depth. There is deliberately no edge, fair value,
    signal or conviction parameter — see the module docstring.
    """
    cfg = cfg or SizingConfig()
    n = max(int(active_markets), 1)
    px = float(quote_price)

    depth_known = book_depth is not None and int(book_depth) > 0
    liquidity_shares = (max(int(int(book_depth) * cfg.max_frac_of_depth), 0)
                        if depth_known else None)

    if equity is None or float(equity) <= 0.0 or px <= 0.0:
        # No capital figure => no capital percentage. Decline rather than invent a share count.
        return BaseSize(0, "no_equity", 0.0, 0, liquidity_shares, depth_known)

    eq = float(equity)
    sleeve_capital = (eq * cfg.mm_capital_frac) / n
    ceiling_capital = eq * cfg.mm_max_quote_frac
    quote_capital, binding = sleeve_capital, "capital"
    if ceiling_capital < sleeve_capital:
        quote_capital, binding = ceiling_capital, "quote_capital_ceiling"

    capital_shares = int(quote_capital / px)
    size = capital_shares
    if liquidity_shares is not None and liquidity_shares < size:
        size, binding = liquidity_shares, "liquidity"
    if size < cfg.min_quote_size:
        size, binding = cfg.min_quote_size, "min_floor"

    return BaseSize(int(size), binding, quote_capital, capital_shares,
                    liquidity_shares, depth_known)


def inventory_cap_shares(*, equity: float | None, quote_price: float,
                         cfg: SizingConfig | None = None) -> int | None:
    """HARD per-market inventory cap in shares, from `mm_max_inventory_frac` x equity / price.

    This is the bound that stops the capital-% base from becoming an unbounded directional
    position — what the +430% bag-hold lacked. Being a fraction of equity it scales with the
    bankroll and commits the same dollars at any price; a share cap would bound risk 47x more
    loosely at 0.95 than at 0.02.

    Returns None when equity is unknown, in which case the caller has no basis to quote at all. No
    edge term: a cap that loosened on a good-looking signal would not be a cap.
    """
    cfg = cfg or SizingConfig()
    px = float(quote_price)
    if equity is None or float(equity) <= 0.0 or px <= 0.0:
        return None
    return max(int((float(equity) * cfg.mm_max_inventory_frac) / px), 1)
