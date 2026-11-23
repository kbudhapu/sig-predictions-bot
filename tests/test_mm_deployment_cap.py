"""Raised MM deployment, with the per-market concentration guard still binding.

mm_capital_frac 0.30 -> 0.65 and mm_max_quote_frac 0.04 -> 0.08 raise how much capital MM deploys
in TOTAL. mm_max_inventory_frac stays 0.05 on purpose: deployment is supposed to come from quoting
MORE MARKETS, not from more risk per market. The thing that must stay true is that when only a few
markets are quotable, the per-market cap BINDS and the sleeve under-deploys rather than
concentrating (which is how the +453% / -6045-share directional bag-holds happened).

Everything here is a FRACTION of equity — there is no fixed share or dollar maximum anywhere.
"""
from __future__ import annotations

import yaml

from strategies.market_making.sizing import SizingConfig, base_quote_size, inventory_cap_shares


EQUITY = 100_000.0


def _cfg(**kw) -> SizingConfig:
    base = {"mm_capital_frac": 0.80, "mm_max_quote_frac": 0.08, "mm_max_inventory_frac": 0.05}
    base.update(kw)
    return SizingConfig(**base)


def _shares(*, equity, active_markets, price, cfg) -> int:
    """base_quote_size(...) -> BaseSize; this test only cares about the share count."""
    return base_quote_size(equity=equity, active_markets=active_markets,
                           quote_price=price, cfg=cfg).size


def _cap(*, equity, price, cfg) -> int:
    return inventory_cap_shares(equity=equity, quote_price=price, cfg=cfg)


# --------------------------------------------------------------------------- #
# The raise itself
# --------------------------------------------------------------------------- #
def test_live_config_raised_the_deployment_levers():
    mm = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["market_making"]
    assert mm["mm_capital_frac"] >= 0.75, "the MM sleeve is the deployment lever and was raised"
    assert mm["mm_capital_frac"] <= 0.85, "leave headroom for arb + directional and for margin"
    assert mm["mm_max_quote_frac"] >= 0.08, "the per-quote ceiling was raised with it"


def test_the_concentration_guard_was_NOT_raised_with_them():
    """The whole point: total deployment up, per-market risk unchanged."""
    mm = yaml.safe_load(open("config/sig.yaml", encoding="utf-8"))["market_making"]
    assert mm["mm_max_inventory_frac"] <= 0.06, (
        "the per-market inventory cap must stay bounded — raising it alongside the sleeve is how "
        "one market becomes a directional bag-hold")
    assert mm["mm_max_inventory_frac"] < mm["mm_capital_frac"]


def test_more_quotable_markets_deploy_more_capital_at_the_same_per_market_risk():
    """A and B interact: the SAME sleeve spread over more markets = more deployment WITHOUT more
    concentration. This is why fixing the feed (27 -> whole board) is the real unlock."""
    price = 0.50
    cfg = _cfg()
    per_27 = _shares(equity=EQUITY, active_markets=27, price=price, cfg=cfg)
    per_200 = _shares(equity=EQUITY, active_markets=200, price=price, cfg=cfg)
    total_27 = per_27 * 27 * price
    total_200 = per_200 * 200 * price
    assert per_200 < per_27, "spreading over more markets must shrink the per-market quote"
    assert total_200 > total_27 * 0.9, "but TOTAL deployed capital must not collapse"
    # and per-market risk never exceeds the cap in either case
    cap = _cap(equity=EQUITY, price=price, cfg=cfg)
    assert per_200 <= cap and per_27 <= cap


# --------------------------------------------------------------------------- #
# The guard binds when few markets are quotable (the case that matters NOW)
# --------------------------------------------------------------------------- #
def test_base_quote_size_is_PRE_cap_and_can_exceed_it():
    """Documenting the layering, because it is a trap. base_quote_size() is the capital-% BASE only
    — with mm_max_quote_frac (0.08) now above mm_max_inventory_frac (0.05) it CAN return more than
    the per-market cap. The cap is enforced one layer up, in MarketMaker.build via the inventory
    headroom. Never treat base_quote_size as the bound."""
    price, cfg = 0.50, _cfg()
    per_quote = _shares(equity=EQUITY, active_markets=5, price=price, cfg=cfg)
    cap = _cap(equity=EQUITY, price=price, cfg=cfg)
    assert per_quote > cap, (
        "fixture no longer exercises the layering; if the base can no longer exceed the cap this "
        "test is obsolete")


def test_with_few_quotable_markets_the_inventory_cap_BINDS_in_the_real_mm_path():
    """THE concentration check. At 5 quotable markets the per-quote ceiling (8% of equity) exceeds
    the per-market inventory cap (5%), so MarketMaker must clamp the resting size to the cap and,
    once held inventory reaches it, pull the adding side entirely. The sleeve under-deploys rather
    than concentrating — which is the behaviour the -6045-share bag-hold lacked."""
    from strategies.market_making import MarketMaker, MarketMakerConfig

    mm = MarketMaker(MarketMakerConfig(sizing=_cfg()), equity=EQUITY, active_markets=5)
    cap = _cap(equity=EQUITY, price=0.50, cfg=_cfg())

    out = mm.build("YES_X", 0.48, 0.52, now=0.0)
    assert out["inventory_cap"] == cap, f"cap not applied per cycle: {out['inventory_cap']} != {cap}"
    for side in ("bid_size", "ask_size"):
        assert getattr(out["quote"], side) <= cap, (
            f"{side} {getattr(out['quote'], side)} breaches the per-market cap {cap}")

    # at the cap the ADDING side must be pulled entirely
    mm.record_fill("YES_X", "buy", cap)
    at_cap = mm.build("YES_X", 0.48, 0.52, now=1.0)
    assert at_cap["quote"].bid_size == 0 or not at_cap["quote"].post_bid, (
        "inventory is AT the per-market cap but the bid (adding side) is still posting — that is "
        "how one market becomes a directional bag-hold")


def test_at_27_markets_the_cap_still_bounds_total_per_market_risk():
    """The live case: 27 quotable markets. A quote is min(0.65/27, 0.08) = 2.4% of equity, so the
    5% cap permits roughly two quotes' worth of inventory and then pulls the adding side."""
    price, cfg = 0.50, _cfg()
    per_quote_capital = min(cfg.mm_capital_frac / 27, cfg.mm_max_quote_frac) * EQUITY
    cap_capital = cfg.mm_max_inventory_frac * EQUITY
    assert per_quote_capital < cap_capital, "a single quote must not exhaust the per-market cap"
    assert cap_capital / per_quote_capital < 4, (
        "the cap should bind within a few fills, not be effectively unreachable")
    per_quote = _shares(equity=EQUITY, active_markets=27, price=price, cfg=cfg)
    assert per_quote <= _cap(equity=EQUITY, price=price, cfg=cfg)


# --------------------------------------------------------------------------- #
# Still no fixed maximum anywhere
# --------------------------------------------------------------------------- #
def test_every_bound_still_scales_with_equity():
    """Doubling equity must double the quote and the cap — no absolute ceiling sneaked in with the
    raise."""
    price, cfg = 0.50, _cfg()
    a = _shares(equity=EQUITY, active_markets=27, price=price, cfg=cfg)
    b = _shares(equity=2 * EQUITY, active_markets=27, price=price, cfg=cfg)
    assert b == 2 * a or abs(b - 2 * a) <= 1, f"quote did not scale with equity: {a} -> {b}"
    ca = _cap(equity=EQUITY, price=price, cfg=cfg)
    cb = _cap(equity=2 * EQUITY, price=price, cfg=cfg)
    assert cb == 2 * ca or abs(cb - 2 * ca) <= 1


def test_same_capital_committed_at_any_price():
    """The reason sizing is capital-% and not a share count: 100 shares is $2 of risk at 0.02 and
    $95 at 0.95."""
    cfg = _cfg()
    for price in (0.02, 0.25, 0.50, 0.95):
        sh = _shares(equity=EQUITY, active_markets=27, price=price, cfg=cfg)
        committed = sh * price
        assert abs(committed - 0.80 / 27 * EQUITY) < 0.02 * EQUITY, (
            f"price {price}: committed ${committed:.0f}, not ~constant across prices")
