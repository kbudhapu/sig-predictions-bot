"""sharp_history: the durable per-minute Polymarket benchmark series (2026-10-06)."""
from __future__ import annotations

import asyncio
import sqlite3

from infra.state.store import StateStore


def test_append_is_idempotent_and_prunes(tmp_path):
    async def _run():
        st = await StateStore(tmp_path / "s.db").open()
        try:
            rows = [{"exchange_id": "840", "ts": "2026-10-06T00:00:00+00:00", "sharp": 0.925, "raw": 0.075,
                     "sig_mid": 0.87},
                    {"exchange_id": "840", "ts": "2026-10-06T00:01:00+00:00", "sharp": 0.926}]
            await st.append_sharp_history(rows)
            await st.append_sharp_history(rows)                      # duplicate keys ignored
            assert await st.prune_sharp_history("2026-10-06T00:00:30+00:00") == 1
        finally:
            await st.close()
    asyncio.run(_run())
    n = sqlite3.connect(tmp_path / "s.db").execute("select count(*), max(sharp) from sharp_history").fetchone()
    assert n == (1, 0.926)


def test_provider_snapshot_returns_aligned_and_raw():
    from sig.engine.cross_market_provider import CrossMarketProvider
    import time

    p = CrossMarketProvider.__new__(CrossMarketProvider)
    p._by_sig_ex = {"840": object()}
    p._poly_cache = {"840": {"polymarket": (0.075, time.monotonic())}}
    p._external_stale_s = 30.0
    p.aligned_benchmark = lambda ex: 0.925
    p.sig_price_provider = lambda ex: (0.87, 1.0)
    p._sig_prices = {"840": {"best_bid": 0.865, "best_ask": 0.875}}
    assert p.sharp_snapshot() == [{"exchange_id": "840", "sharp": 0.925, "raw": 0.075, "sig_mid": 0.87,
                                   "venue": "polymarket", "sig_bid": 0.865, "sig_ask": 0.875}]


def test_backfill_parser_maps_race_to_exchange():
    from tools.backfill_sharp_history import parse
    line = ("2026-10-05 15:00:56 [debug    ] cross_market_raw               implied_aligned=0.925 "
            "polarity=inverted race=us_house_control_dem raw_yes=0.075 venue=polymarket")
    rows = parse([line])
    assert len(rows) == 1 and rows[0]["sharp"] == 0.925 and rows[0]["raw"] == 0.075


def test_nav_history_append(tmp_path):
    async def _run():
        st = await StateStore(tmp_path / "n.db").open()
        try:
            await st.append_nav({"ts": "2026-10-06T05:00:00+00:00", "cash": 3765.0, "market_value": 84221.0,
                                 "nav": 87986.19, "unrealized": 1256.0, "positions": 101,
                                 "lb_pnl": -12000.0, "lb_rank": 1045})
        finally:
            await st.close()
    asyncio.run(_run())
    assert sqlite3.connect(tmp_path / "n.db").execute("select nav, lb_rank from nav_history").fetchone() == (87986.19, 1045)


def test_book_edge_signs_and_unmapped():
    import main_sig
    pos = [{"exchangeId": "1", "quantity": 1000},      # held YES, sharp 5c above SIG  -> +50
           {"exchangeId": "2", "quantity": -2000},     # held NO, SIG 3c above sharp   -> +60
           {"exchangeId": "3", "quantity": 500}]       # no sharp -> ignored
    snap = [{"exchange_id": "1", "sharp": 0.90, "sig_mid": 0.85},
            {"exchange_id": "2", "sharp": 0.10, "sig_mid": 0.13},
            {"exchange_id": "4", "sharp": 0.5, "sig_mid": None}]
    assert main_sig._book_edge(pos, snap) == 110.0
    assert main_sig._book_edge([{"exchangeId": "3", "quantity": 5}], snap) is None


def test_nav_history_book_edge_column_and_migration(tmp_path):
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)        # an OLD nav_history without book_edge
    c.execute("CREATE TABLE nav_history (ts TEXT PRIMARY KEY, cash REAL, market_value REAL, nav REAL NOT NULL, "
              "unrealized REAL, positions INTEGER, lb_pnl REAL, lb_rank INTEGER)")
    c.commit(); c.close()

    async def _run():
        st = await StateStore(db).open()
        try:
            await st.append_nav({"ts": "2026-10-06T07:00:00+00:00", "nav": 87761.6, "book_edge": 6200.5})
        finally:
            await st.close()
    asyncio.run(_run())
    assert sqlite3.connect(db).execute("select nav, book_edge from nav_history").fetchone() == (87761.6, 6200.5)
