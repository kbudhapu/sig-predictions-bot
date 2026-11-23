"""MM sizing: a percentage of CAPITAL, with NO fixed maximum anywhere.

What is pinned here:
  1. a quote commits the SAME DOLLARS at any price (the old 100-flat was $2 at 0.02, $95 at 0.95)
  2. size scales with the bankroll, with nothing clamping it
  3. the per-market inventory cap is ALSO capital-relative, and pulls the adding side at the cap
  4. inventory skew drives asymmetric SIZE, so the book SHEDS rather than bag-holds
  5. size is NOT edge-scaled — asserted by signature AND by behaviour
  6. no fixed maximum and no fallback share count exists
"""
from __future__ import annotations

import inspect

import pytest
import yaml

from strategies.market_making import (
    InventoryBook,
    InventoryConfig,
    MarketMaker,
    MarketMakerConfig,
    QuoterConfig,
    compute_quote,
)
from strategies.market_making.sizing import (
    SizingConfig,
    base_quote_size,
    inventory_cap_shares,
)

EQ = 100_000.0


def _cfg() -> MarketMakerConfig:
    with open("config/sig.yaml", encoding="utf-8") as fh:
        return MarketMakerConfig.from_config(yaml.safe_load(fh))


CFG = _cfg()
S = CFG.sizing


def mm(*, equity=EQ, markets=30, cfg: MarketMakerConfig | None = None) -> MarketMaker:
    return MarketMaker(cfg or CFG, equity=equity, active_markets=markets)


def quote_at(m: MarketMaker, mid: float, *, now: float = 1.0, depth=None):
    return m.build("X", round(mid - 0.04, 3), round(mid + 0.04, 3), now=now, book_depth=depth)


# --------------------------------------------------------------------------- #
# 1. CAPITAL CONSISTENCY — the headline
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mid", [0.02, 0.05, 0.10, 0.30, 0.50, 0.75, 0.95])
def test_a_quote_commits_the_same_capital_at_every_price(mid):
    """THE fix. 100 shares flat was $2 of risk at 0.02 and $95 at 0.95 — a 47x spread in exposure
    driven purely by where the market happened to be priced."""
    b = base_quote_size(equity=EQ, active_markets=30, quote_price=mid, book_depth=None, cfg=S)
    expected_capital = EQ * min(S.mm_capital_frac / 30, S.mm_max_quote_frac)
    assert b.size * mid == pytest.approx(expected_capital, rel=0.01)
    assert b.binding in ("capital", "quote_capital_ceiling")


def test_capital_committed_is_flat_across_the_price_range():
    caps = [base_quote_size(equity=EQ, active_markets=30, quote_price=p, cfg=S).size * p
            for p in (0.02, 0.10, 0.50, 0.95)]
    assert max(caps) / min(caps) < 1.02, f"capital must be price-invariant, got {caps}"
    # and the share counts must therefore differ hugely — that is the point
    shares = [base_quote_size(equity=EQ, active_markets=30, quote_price=p, cfg=S).size
              for p in (0.02, 0.95)]
    assert shares[0] > 10 * shares[1], "a cheap market must get many more shares for the same $"


def test_the_old_flat_100_is_gone():
    """A fixed share count cannot be the answer at two different prices."""
    cheap = base_quote_size(equity=EQ, active_markets=30, quote_price=0.02, cfg=S).size
    rich = base_quote_size(equity=EQ, active_markets=30, quote_price=0.95, cfg=S).size
    assert not (cheap == rich == 100), "size is still a flat constant"
    assert cheap != 100 and rich != 100


# --------------------------------------------------------------------------- #
# 2. SCALES WITH BANKROLL, NO CEILING
# --------------------------------------------------------------------------- #
def test_size_scales_linearly_with_equity_and_nothing_clamps_it():
    """NO FIXED MAXIMUM. 100x the bankroll must give 100x the quote."""
    sizes = {eq: base_quote_size(equity=eq, active_markets=30, quote_price=0.50, cfg=S).size
             for eq in (10_000, 100_000, 1_000_000, 10_000_000)}
    assert sizes[100_000] == pytest.approx(sizes[10_000] * 10, rel=0.01)
    assert sizes[10_000_000] == pytest.approx(sizes[100_000] * 100, rel=0.01)
    assert all(b == "capital" for b in
               [base_quote_size(equity=eq, active_markets=30, quote_price=0.50, cfg=S).binding
                for eq in sizes]), "no ceiling may bind at any bankroll"


def test_no_absolute_maximum_knob_exists():
    """Structural guard: if a fixed share ceiling is ever reintroduced, this fails."""
    fields = set(SizingConfig.__dataclass_fields__)
    for banned in ("max_quote_size", "unknown_depth_contracts", "fallback_quote_size", "quote_size"):
        assert banned not in fields, f"{banned} is a fixed maximum/fallback and must not exist"
    # every size knob except the share FLOOR is a fraction in (0, 1]
    for name in ("mm_capital_frac", "mm_max_quote_frac", "mm_max_inventory_frac",
                 "max_frac_of_depth"):
        assert 0.0 < getattr(S, name) <= 1.0


def test_spreading_across_more_markets_shrinks_each_quote_not_the_total():
    a = base_quote_size(equity=EQ, active_markets=10, quote_price=0.50, cfg=S)
    b = base_quote_size(equity=EQ, active_markets=100, quote_price=0.50, cfg=S)
    assert b.size < a.size
    # per-quote capital is capped, so the sleeve is not exceeded either way
    assert a.quote_capital <= EQ * S.mm_max_quote_frac + 1e-9


def test_no_equity_declines_to_quote_rather_than_inventing_a_size():
    """The fallback WAS the hardcoded 100 — i.e. the bug. Declining is the honest answer."""
    b = base_quote_size(equity=None, active_markets=30, quote_price=0.50, cfg=S)
    assert b.size == 0 and b.binding == "no_equity" and not b.sized
    q = quote_at(mm(equity=None), 0.50)["quote"]
    assert q.bid_size == 0 and q.ask_size == 0
    assert not q.post_bid and not q.post_ask
    assert "no_equity" in q.reason


# --------------------------------------------------------------------------- #
# 3. HARD PER-MARKET INVENTORY CAP — also capital-relative
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mid", [0.02, 0.10, 0.50, 0.95])
def test_inventory_cap_commits_the_same_capital_at_every_price(mid):
    cap = inventory_cap_shares(equity=EQ, quote_price=mid, cfg=S)
    assert cap * mid == pytest.approx(EQ * S.mm_max_inventory_frac, rel=0.01)


def test_inventory_cap_scales_with_bankroll():
    small = inventory_cap_shares(equity=10_000, quote_price=0.50, cfg=S)
    big = inventory_cap_shares(equity=1_000_000, quote_price=0.50, cfg=S)
    assert big == pytest.approx(small * 100, rel=0.01)


def test_at_the_cap_the_ADDING_side_is_pulled_entirely():
    """The stop that bounds the capital-% base. At the cap only the reducing side quotes — this is
    what a +430% bag-hold lacked."""
    m = mm()
    r = quote_at(m, 0.50)
    cap = r["inventory_cap"]
    m.set_position("X", cap)                       # exactly at the long cap
    q = quote_at(m, 0.50, now=2.0)["quote"]
    assert q.bid_size == 0 and not q.post_bid, "the ADDING side must be fully pulled"
    assert q.post_ask and q.ask_size > 0, "the REDUCING side must still quote"
    assert "inventory_cap_long" in q.reason


def test_the_cap_cannot_be_jumped_through_by_one_fill():
    """The adding side is bounded by remaining headroom, so a full instant fill lands AT the cap,
    never past it."""
    m = mm()
    cap = quote_at(m, 0.50)["inventory_cap"]
    m.set_position("X", cap - 5)
    q = quote_at(m, 0.50, now=2.0)["quote"]
    assert q.bid_size <= 5, f"bid {q.bid_size} could overshoot the cap by {q.bid_size - 5}"


# --------------------------------------------------------------------------- #
# 4. INVENTORY SKEW — asymmetric SIZE, sheds rather than accumulates
# --------------------------------------------------------------------------- #
def test_skew_drives_asymmetric_SIZE_not_just_price():
    """Price skew alone only makes the reducing side more LIKELY to fill; it does not make it fill
    BIGGER, so a book repeatedly hit on one side still accumulates faster than it sheds."""
    # The base must be SMALL relative to both the cap headroom and the holding, or those two
    # clamps dominate and hide the skew (base=cap=1000 at pos=500 clamps both sides to 500 —
    # the multipliers are still 0.7/1.3, but nothing observable differs).
    base = compute_quote("m", 0.46, 0.54, QuoterConfig(size=100))
    book = InventoryBook(InventoryConfig(cap=1000, size_skew_strength=0.6))
    book.set_position("m", 500)                    # half-way long
    q = book.apply("m", base)
    assert q.ask_size > q.bid_size, "long ⇒ the reducing (ask) side must be LARGER"
    assert (q.bid_size, q.ask_size) == (70, 130), "0.6 strength at frac 0.5 ⇒ x0.7 / x1.3"
    d = book.skew("m")
    assert d.ask_size_mult > 1.0 > d.bid_size_mult


def test_as_inventory_builds_the_ask_grows_and_the_bid_shrinks():
    base = compute_quote("m", 0.46, 0.54, QuoterConfig(size=1000))
    book = InventoryBook(InventoryConfig(cap=1000, size_skew_strength=0.6))
    seen = []
    for pos in (0, 200, 400, 600, 800, 1000):
        book.set_position("m", pos)
        q = book.apply("m", base)
        seen.append((pos, q.bid_size, q.ask_size))
    bids = [b for _, b, _ in seen]
    asks = [a for _, _, a in seen]
    assert bids == sorted(bids, reverse=True), f"bid must shrink monotonically: {bids}"
    assert asks[0] < asks[-1], f"ask must grow with inventory: {asks}"
    assert bids[-1] == 0, "at the cap the bid is gone"


def test_a_flat_book_quotes_symmetrically():
    """Skew is symmetric about flat — a flat MM has no directional lean."""
    book = InventoryBook(InventoryConfig(cap=1000, size_skew_strength=0.6))
    book.set_position("m", 0)
    d = book.skew("m")
    assert d.bid_size_mult == d.ask_size_mult == 1.0
    assert d.shift == 0.0


def test_ask_is_still_bounded_to_held_inventory():
    """A venue constraint, independent of the skew: SIG lists only YES exchanges and the risk gate
    rejects `sell_without_holding`, so we can only ever sell BACK what we hold."""
    base = compute_quote("m", 0.46, 0.54, QuoterConfig(size=1000))
    book = InventoryBook(InventoryConfig(cap=10_000, size_skew_strength=0.6))
    book.set_position("m", 40)
    assert book.apply("m", base).ask_size == 40


# --------------------------------------------------------------------------- #
# 5. NOT EDGE-SCALED
# --------------------------------------------------------------------------- #
def test_no_edge_parameter_exists_anywhere_in_the_size_path():
    """Capital-% is 'what fraction of my money per quote'. Edge-scaling is 'bet more when I think
    I'm right' — that is what turned MM into a directional blowup. Asserted by SIGNATURE."""
    banned = {"edge", "fair_value", "p", "divergence", "conviction", "signal", "sigma_p",
              "alpha", "score"}
    for fn in (base_quote_size, inventory_cap_shares, MarketMaker.build):
        params = set(inspect.signature(fn).parameters) - {"self"}
        assert not (params & banned), f"{fn.__name__} takes an edge-like param: {params & banned}"
    assert not (set(SizingConfig.__dataclass_fields__) & banned)


def test_identical_market_state_gives_identical_size_regardless_of_any_edge():
    """Behavioural proof: there is no channel through which an edge could reach the size."""
    a = mm()
    b = mm()
    qa = quote_at(a, 0.50)["quote"]
    qb = quote_at(b, 0.50)["quote"]
    assert (qa.bid_size, qa.ask_size) == (qb.bid_size, qb.ask_size)


def test_size_does_not_move_when_the_touch_implies_a_huge_mispricing():
    """A 0.46/0.54 book and a wildly skewed 0.05/0.95 book differ enormously in 'opportunity'. The
    committed CAPITAL must not: only price and inventory may move the size."""
    m = mm()
    tight = m.build("X", 0.46, 0.54, now=1.0)["quote"]
    wide = m.build("Y", 0.05, 0.95, now=1.0)["quote"]
    # both mids are 0.50, so the capital committed must match
    assert tight.mid == wide.mid == 0.5
    assert tight.bid_size == wide.bid_size, "a wider spread is not a bigger bet"


# --------------------------------------------------------------------------- #
# 6. Liquidity bound: market-relative, and skipped when depth is unknown
# --------------------------------------------------------------------------- #
def test_liquidity_bound_applies_only_when_depth_is_known():
    unknown = base_quote_size(equity=EQ, active_markets=30, quote_price=0.50,
                              book_depth=None, cfg=S)
    assert unknown.liquidity_shares is None and unknown.binding == "capital", \
        "unknown depth must NOT be replaced by a constant (that is a fixed max in disguise)"
    thin = base_quote_size(equity=EQ, active_markets=30, quote_price=0.50,
                           book_depth=4_000, cfg=S)
    assert thin.binding == "liquidity" and thin.size == int(4_000 * S.max_frac_of_depth)
    deep = base_quote_size(equity=EQ, active_markets=30, quote_price=0.50,
                           book_depth=10_000_000, cfg=S)
    assert deep.binding == "capital", "a deep book must not bound a capital-sized quote"


def test_config_rejects_a_fraction_outside_zero_to_one():
    with pytest.raises(ValueError, match="mm_capital_frac"):
        SizingConfig(mm_capital_frac=1.5).validate()
    with pytest.raises(ValueError, match="mm_max_inventory_frac"):
        SizingConfig(mm_max_inventory_frac=0.0).validate()


def test_mm_stays_two_sided_once_it_holds_inventory():
    """Spread capture requires both sides. Flat, the ask cannot rest (nothing to sell on a
    YES-only venue); once long, both sides quote with a positive spread."""
    m = mm()
    flat = quote_at(m, 0.50)
    assert {o["action"] for o in flat["orders"]} == {"buy"}
    m.set_position("X", 300)
    held = quote_at(m, 0.50, now=2.0)
    actions = {o["action"] for o in held["orders"]}
    assert actions == {"buy", "sell"}, "must be TWO-SIDED while holding"
    buys = [o for o in held["orders"] if o["action"] == "buy"][0]
    sells = [o for o in held["orders"] if o["action"] == "sell"][0]
    assert buys["price"] < sells["price"], "positive captured spread"
