"""Rotation v2 shadow (2026-10-07): virtual book, gap persistence, execution prices, swap valuation."""
from sig.engine.rotation import Candidate, Held
from sig.engine.rotation_v2 import (PersistenceTracker, VirtualSwap, apply_virtual, book_from_events, decide_v2,
                                    held_side_prices)
from tools.rotation_v2_validate import swap_value


def _h(ex, qty, mid, sharp, mv=5000.0):
    return Held(ex, qty, mid, sharp, 0.0025, mv)


def test_persistence_tracks_continuous_live_gap_and_resets():
    t = PersistenceTracker()
    assert t.update("9", 0.92, 0.80, 0.0) == 0.0              # first seen live (12c gap)
    assert t.update("9", 0.92, 0.81, 1800.0) == 1800.0
    assert t.update("9", 0.92, 0.90, 2400.0) == 0.0           # gap 2c < threshold -> reset
    assert t.update("9", 0.92, 0.80, 3000.0) == 0.0           # live again: clock restarts
    assert t.update("9", 0.70, 0.80, 3100.0) == 0.0           # side flipped -> restarts


def test_v2_requires_persistence_and_never_reuses_markets():
    held = [_h("1", 100, 0.90, 0.91), _h("2", 100, 0.85, 0.95)]
    cands = [Candidate("9", 0.80, 0.92, 0.0025)]
    kw = dict(persist_s=3600, cash=100, position_size=4400)
    assert decide_v2(held, cands, [], persisted={"9": 600}, **kw) == []          # gap only 10 min old
    out = decide_v2(held, cands, [], persisted={"9": 4000}, **kw)
    assert len(out) == 1 and out[0].sell.exchange_id == "1" and out[0].buy.exchange_id == "9"
    done = [VirtualSwap("a", "t", "1", 100, 0.9, "9", "yes", 0.8, 112.5)]
    assert decide_v2(held, cands, done, persisted={"9": 4000}, **kw) == []      # 9 already bought, 1 already sold
    sellable, frozen = apply_virtual(held, done)
    assert [h.exchange_id for h in sellable] == ["2"] and frozen == {"1", "9"}


def test_held_side_prices():
    assert held_side_prices(100, 0.80, 0.81) == (0.80, 0.81)               # YES exits at bid, enters at ask
    ex, en = held_side_prices("no", 0.80, 0.81)
    assert abs(ex - 0.19) < 1e-9 and abs(en - 0.20) < 1e-9                  # NO exits at 1-ask, enters at 1-bid


def test_swap_value_includes_costs_and_tracks_sharp():
    # sold 100 YES at 0.90 bid (sharp 0.91) -> $90; bought YES at 0.81 ask -> 111.11 sh (sharp 0.92)
    rec = {"sell": {"qty": 100, "proceeds": 90.0}, "buy": {"side": "yes", "new_shares": 90.0 / 0.81}}
    v0 = swap_value(rec, 0.92, 0.91)
    assert abs(v0 - 100 * (111.111 * 0.92 - 91.0) / 90) < 0.05               # ~ +12.4 per $100: more EV held
    assert swap_value(rec, 0.80, 0.91) < 0 < v0                               # sharp moves against new leg -> loss


def test_book_from_events_roundtrip():
    e = {"event": "swap", "swap_id": "x", "ts": "t", "sell": {"exchange_id": "1", "qty": -50, "exit_px": 0.2},
         "buy": {"exchange_id": "9", "side": "no", "entry_px": 0.3, "new_shares": 33.3}}
    [s] = book_from_events([e, {"event": "other"}])
    assert (s.sell_ex, s.sold_qty, s.buy_ex, s.buy_side, s.new_shares) == ("1", -50.0, "9", "no", 33.3)


# ───────────── retune 2026-10-09: top-ups, partial virtual book, 24h freeze ─────────────
from datetime import datetime, timezone

T0 = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
ISO0 = datetime(2026, 10, 9, tzinfo=timezone.utc).isoformat()
KW = dict(persist_s=3600, cash=100, position_size=4400, margin=0.03, topup=True, freeze_s=86400, now=T0 + 60)


def test_topup_buys_more_of_a_held_market_on_the_same_side():
    held = [_h("1", 1000, 0.90, 0.91, mv=900), _h("2", 1000, 0.80, 0.92, mv=800)]   # 2: 12c gap, room 3.6k
    out = decide_v2(held, [Candidate("2", 0.80, 0.92, 0.0025)], [], persisted={"2": 4000}, **KW)
    assert len(out) == 1 and out[0].sell.exchange_id == "1" and out[0].buy.exchange_id == "2"
    kw = dict(KW, topup=False)
    kw.pop("freeze_s"), kw.pop("now")
    assert decide_v2(held, [Candidate("2", 0.80, 0.92, 0.0025)], [], persisted={"2": 4000}, **kw) == []  # old rule


def test_topup_never_trades_against_a_holding_or_past_the_cap():
    held = [_h("1", 1000, 0.90, 0.91, mv=900), _h("2", -1000, 0.80, 0.92, mv=200)]  # 2 held NO, cand says YES
    assert decide_v2(held, [Candidate("2", 0.80, 0.92, 0.0025)], [], persisted={"2": 4000}, **KW) == []
    full = [_h("1", 1000, 0.90, 0.91, mv=900), _h("2", 5000, 0.80, 0.92, mv=4300)]  # only $100 room left
    assert decide_v2(full, [Candidate("2", 0.80, 0.92, 0.0025)], [], persisted={"2": 4000}, **KW) == []


def test_topup_respects_margin_persistence_and_cash():
    held = [_h("1", 1000, 0.90, 0.91, mv=900), _h("2", 1000, 0.80, 0.92, mv=800)]
    c = [Candidate("2", 0.80, 0.92, 0.0025)]
    assert decide_v2(held, c, [], persisted={"2": 600}, **KW) == []                      # not persisted
    assert decide_v2(held, c, [], persisted={"2": 4000}, **dict(KW, margin=0.2)) == []    # margin
    assert decide_v2(held, c, [], persisted={"2": 4000}, **dict(KW, cash=5000)) == []     # cash can fund it


def test_virtual_book_partial_sell_and_24h_freeze():
    held = [_h("1", 1000, 0.90, 0.91, mv=900)]
    sw = [VirtualSwap("a", ISO0, "1", 400.0, 0.9, "9", "yes", 0.8, 450.0)]
    book, frozen = apply_virtual(held, sw, now=T0 + 3600, freeze_s=86400)
    assert book == [] and frozen == {"1", "9"}                                            # frozen for 24h
    book, frozen = apply_virtual(held, sw, now=T0 + 90000, freeze_s=86400)
    assert frozen == set() and len(book) == 1 and abs(book[0].qty - 600) < 1e-9           # 400 virtually sold
    assert abs(book[0].market_value - 540) < 1e-6
    gone = [VirtualSwap("b", ISO0, "1", 1000.0, 0.9, "9", "yes", 0.8, 1125.0)]
    assert apply_virtual(held, gone, now=T0 + 90000, freeze_s=86400)[0] == []            # fully sold: never resold


def test_default_v2_behaviour_unchanged():
    held = [_h("1", 100, 0.90, 0.91), _h("2", 100, 0.85, 0.95)]
    done = [VirtualSwap("a", "t", "1", 100, 0.9, "9", "yes", 0.8, 112.5)]
    sellable, frozen = apply_virtual(held, done)
    assert [h.exchange_id for h in sellable] == ["2"] and frozen == {"1", "9"}


def test_loop_reads_the_retune_keys():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main_sig.py"),
               encoding="utf-8").read()
    i = src.index("async def _rotation_v2_loop(")
    body = src[i:src.index("\ndef _kalshi_rows(", i)]
    for k in ("rotation_v2_topup", "rotation_v2_partial", "rotation_v2_margin", "rotation_v2_freeze_s"):
        assert k in body
    assert "topup=topup, freeze_s=freeze_s, now=now" in body
