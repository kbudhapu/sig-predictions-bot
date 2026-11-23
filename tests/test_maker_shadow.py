"""Maker-entry shadow (2026-10-07): quote selection and tape fill matching."""
from datetime import datetime, timedelta, timezone

from sig.engine.maker_shadow import maker_quotes
from tools.maker_shadow_validate import fill_qty, hits, held_px, simulate, taker


def test_maker_quotes_pick_poly_favoured_side_and_gate():
    rows = [{"exchange_id": "1", "sharp": 0.05, "best_bid": 0.12, "best_ask": 0.125, "bid_size": 900, "ask_size": 400},
            {"exchange_id": "2", "sharp": 0.95, "best_bid": 0.86, "best_ask": 0.865, "bid_size": 700, "ask_size": 50},
            {"exchange_id": "3", "sharp": 0.50, "best_bid": 0.47, "best_ask": 0.475, "bid_size": 1, "ask_size": 1},
            {"exchange_id": "4", "sharp": 0.10, "best_bid": 0.11, "best_ask": 0.115, "bid_size": 1, "ask_size": 1}]
    q = {r["exchange_id"]: r for r in maker_quotes(rows)}
    assert q["1"]["side"] == "ask" and q["1"]["px"] == 0.125 and q["1"]["depth"] == 400   # SIG above Poly: sell YES
    assert q["2"]["side"] == "bid" and q["2"]["px"] == 0.86 and q["2"]["depth"] == 700    # SIG below Poly: buy YES
    assert "3" not in q and "4" not in q                                                 # toss-up / gap too small


def test_fill_models_and_hits():
    assert hits("ask", 0.125, "YES", 0.125) and hits("ask", 0.125, "YES", 0.13) and not hits("ask", 0.125, "NO", 0.125)
    assert hits("bid", 0.86, "NO", 0.86) and not hits("bid", 0.86, "NO", 0.865)
    assert abs(fill_qty(1000, 3000, 1000, False, 0) - 250.0) < 1e-9          # pro-rata: 1000 * 1000/4000
    assert fill_qty(1000, 3000, 1000, True, 0) == 0.0                         # behind 3000 displayed shares
    assert fill_qty(1000, 3000, 1000, True, 2500) == 500.0                    # volume finally reaches us
    assert abs(held_px("ask", 0.125) - 0.875) < 1e-9 and held_px("bid", 0.86) == 0.86


def test_simulate_vs_taker():
    t0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    q = {"ts": t0.isoformat(), "exchange_id": "1", "side": "ask", "px": 0.125, "depth": 0.0,
         "bid": 0.12, "ask": 0.125, "sharp": 0.05, "mid": 0.1225, "gap": -0.0725}
    trades = {"1": [(t0 + timedelta(seconds=10), "YES", 0.125, 10_000.0)]}
    book = simulate([q], trades, pessimistic=True)
    sh, cost, side = book["1"]
    assert side == "ask" and abs(cost - 500.0) < 1e-6 and abs(cost / sh - 0.875) < 1e-9   # bought NO at 0.875
    tk = taker([q])["1"]
    assert abs(tk[1] / tk[0] - 0.88) < 1e-9                                  # taker buys NO at 1 - bid = 0.88
