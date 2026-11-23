"""Stale-quote sniper: lift a cheap ask (buy YES), hit a dear bid (buy NO), only past min_edge, only on
a fresh sharp."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.sniper import SniperConfig, evaluate


def _cfg(**kw):
    # touch-mode (sweep OFF) for the trigger-logic tests: price == the touch, exact assertions hold.
    kw.setdefault("sweep_to_edge", False)
    return SniperConfig(enabled=True, min_edge=0.03, max_sharp_age_s=20.0, **kw)


def _cfg_sweep(**kw):
    return SniperConfig(enabled=True, min_edge=0.03, max_sharp_age_s=20.0, sweep_to_edge=True, **kw)


def test_lifts_underpriced_ask():
    # sharp 0.90, SIG ask 0.80 -> ask is 10pt cheap -> BUY YES at 0.80
    t = evaluate("840", sig_bid=0.78, sig_ask=0.80, aligned_sharp=0.90, sharp_age_s=2.0, cfg=_cfg())
    assert t and t.option_side == "yes" and abs(t.price - 0.80) < 1e-9 and t.edge == 0.10


def test_hits_overpriced_bid():
    # sharp 0.10, SIG bid 0.20 -> bid is 10pt dear -> BUY NO at 1-0.20=0.80
    t = evaluate("841", sig_bid=0.20, sig_ask=0.22, aligned_sharp=0.10, sharp_age_s=2.0, cfg=_cfg())
    assert t and t.option_side == "no" and abs(t.price - 0.80) < 1e-9 and t.edge == 0.10


def test_no_snipe_below_min_edge():
    # sharp 0.90, ask 0.88 -> only 2pt < 3pt min_edge
    assert evaluate("x", 0.86, 0.88, 0.90, 2.0, _cfg()) is None


def test_stale_sharp_blocks_snipe():
    assert evaluate("x", 0.78, 0.80, 0.90, sharp_age_s=120.0, cfg=_cfg()) is None


def test_disabled_and_missing_sharp():
    assert evaluate("x", 0.78, 0.80, 0.90, 2.0, SniperConfig(enabled=False, min_edge=0.03)) is None
    assert evaluate("x", 0.78, 0.80, None, 2.0, _cfg()) is None


def test_prefers_ask_lift_when_both_would_trigger():
    # degenerate crossed/wide book: ask cheap AND bid dear vs sharp; ask-lift is checked first
    t = evaluate("x", sig_bid=0.50, sig_ask=0.80, aligned_sharp=0.90, sharp_age_s=1.0, cfg=_cfg())
    assert t and t.option_side == "yes"


def test_sweep_is_leashed_to_max_cross():
    # sharp 0.90, ask 0.80 (10pt cheap), max_cross 0.02. Edge boundary is 0.87 but the LEASH caps the pay-up
    # at touch+max_cross = 0.82, so a thin-book gap can't drag the fill to 0.87 for only min_edge of edge.
    t = evaluate("840", sig_bid=0.78, sig_ask=0.80, aligned_sharp=0.90, sharp_age_s=2.0,
                 cfg=_cfg_sweep(max_cross=0.02))
    assert t and t.option_side == "yes" and abs(t.price - 0.82) < 1e-9 and t.edge == 0.10


def test_sweep_no_side_leashed_to_max_cross():
    # sharp 0.10, bid 0.20 (10pt dear), max_cross 0.02. Sell YES down to bid-max_cross=0.18 (not to
    # sharp+min_edge=0.13) => buy NO at 1-0.18 = 0.82.
    t = evaluate("841", sig_bid=0.20, sig_ask=0.22, aligned_sharp=0.10, sharp_age_s=2.0,
                 cfg=_cfg_sweep(max_cross=0.02))
    assert t and t.option_side == "no" and abs(t.price - 0.82) < 1e-9 and t.edge == 0.10


def test_edge_boundary_binds_when_tighter_than_leash():
    # small gap (5pt) but generous leash (max_cross 0.10): the edge boundary sharp-min_edge=0.87 binds
    # first (we never pay past it, else edge < min_edge). touch 0.82 -> price 0.87, not 0.82+0.10=0.92.
    t = evaluate("x", sig_bid=0.80, sig_ask=0.82, aligned_sharp=0.90, sharp_age_s=1.0,
                 cfg=_cfg_sweep(max_cross=0.10))
    assert t and t.option_side == "yes" and abs(t.price - 0.87) < 1e-9


def test_sweep_price_never_below_touch():
    # tiny gap exactly at min_edge: sweep price must stay >= the touch (marketable), never price us out.
    t = evaluate("x", sig_bid=0.80, sig_ask=0.87, aligned_sharp=0.90, sharp_age_s=1.0, cfg=_cfg_sweep())
    assert t and t.option_side == "yes" and t.price >= 0.87 - 1e-9


def test_stale_depth_measured_to_leashed_price():
    # asks: 2000@0.80, 5000@0.845, 1000@0.88. sharp 0.90, min_edge 0.03, max_cross 0.02 => pay up to 0.82.
    # only 0.80 clears (<=0.82) => 2000 stale depth; the 0.845/0.88 levels are beyond the leash.
    asks = [{"price": 0.80, "quantity": 2000}, {"price": 0.845, "quantity": 5000},
            {"price": 0.88, "quantity": 1000}]
    t = evaluate("840", sig_bid=0.78, sig_ask=0.80, aligned_sharp=0.90, sharp_age_s=2.0,
                 cfg=_cfg_sweep(max_cross=0.02), asks=asks)
    assert t and t.stale_depth == 2000.0


def test_wider_leash_takes_more_depth():
    # same book, max_cross 0.05 => pay up to min(0.87, 0.85) = 0.85 => 0.80 and 0.845 clear => 7000.
    asks = [{"price": 0.80, "quantity": 2000}, {"price": 0.845, "quantity": 5000},
            {"price": 0.88, "quantity": 1000}]
    t = evaluate("840", sig_bid=0.78, sig_ask=0.80, aligned_sharp=0.90, sharp_age_s=2.0,
                 cfg=_cfg_sweep(max_cross=0.05), asks=asks)
    assert t and abs(t.price - 0.85) < 1e-9 and t.stale_depth == 7000.0


def test_no_book_leaves_depth_none():
    t = evaluate("840", sig_bid=0.78, sig_ask=0.80, aligned_sharp=0.90, sharp_age_s=2.0, cfg=_cfg_sweep())
    assert t and t.stale_depth is None
