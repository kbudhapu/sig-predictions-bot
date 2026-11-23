"""Fresh-dislocation reversion (sig/engine/fresh_gap.py): entry only on moves AWAY from Poly, exits, prices."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sig.engine.fresh_gap import FreshGapConfig, Row, entry_signal, exit_reason, held_prices  # noqa: E402

C = FreshGapConfig()


def test_fires_when_sig_moves_away_from_poly():
    # Poly 0.90 both times; SIG mid 0.88 -> 0.84 (moved 4c DOWN, away from Poly) -> buy YES toward Poly
    assert entry_signal(Row("1", 0.90, 0.835, 0.845, 0.88, 0.90), C) == "yes"
    # mirror: Poly 0.10; SIG 0.12 -> 0.16 (away, up) -> buy NO
    assert entry_signal(Row("2", 0.10, 0.155, 0.165, 0.12, 0.10), C) == "no"


def test_does_not_fire_on_moves_toward_poly():
    # SIG 0.80 -> 0.85 toward Poly 0.90 (gap 10c -> 5c): convergence, never faded or chased by this rule
    assert entry_signal(Row("1", 0.90, 0.845, 0.855, 0.80, 0.90), C) is None


def test_thresholds_spread_and_flip():
    assert entry_signal(Row("1", 0.90, 0.865, 0.875, 0.88, 0.90), C) is None      # move 1c < 3c
    assert entry_signal(Row("1", 0.86, 0.845, 0.855, 0.88, 0.86), C) is None      # gap now 1c < 2c... and flipped
    assert entry_signal(Row("1", 0.90, 0.82, 0.86, 0.88, 0.90), C) is None        # spread 4c > 2c
    # gap was SIG-above-Poly 24h ago, now SIG-below-Poly: flipped through fair -> skip
    assert entry_signal(Row("1", 0.90, 0.835, 0.845, 0.93, 0.90), C) is None
    assert entry_signal(Row("1", 0.90, 0.0, 0.845, 0.88, 0.90), C) is None        # broken book


def test_exits():
    # entered YES at gap 6c; now gap 2.5c (<= half) -> closed
    assert exit_reason(0.06, "yes", 3600, 0.90, 0.87, 0.88, C) == "closed"
    assert exit_reason(0.06, "yes", 3600, 0.90, 0.85, 0.86, C) is None             # gap 4.5c: hold
    assert exit_reason(0.06, "yes", 49 * 3600, 0.90, 0.85, 0.86, C) == "timeout"
    assert exit_reason(0.06, "yes", 3600, 0.90, 0.905, 0.915, C) == "flipped"      # SIG now above Poly
    # NO trade: gap was -6c (SIG above Poly); now -2c -> closed
    assert exit_reason(-0.06, "no", 3600, 0.10, 0.115, 0.125, C) == "closed"


def test_held_prices():
    assert held_prices("yes", 0.84, 0.85) == {"taker_in": 0.85, "maker_in": 0.84, "taker_out": 0.84, "maker_out": 0.85}
    p = held_prices("no", 0.15, 0.16)
    assert abs(p["taker_in"] - 0.85) < 1e-9 and abs(p["maker_in"] - 0.84) < 1e-9
    assert abs(p["taker_out"] - 0.84) < 1e-9 and abs(p["maker_out"] - 0.85) < 1e-9


def test_shadow_loop_records_entry_then_exit(tmp_path, monkeypatch):
    import asyncio
    import json
    import main_sig
    path = str(tmp_path / "ev.jsonl")
    state = {"bid": 0.835, "ask": 0.845, "ticks": 0}

    class Store:
        async def _read(self, sql, params):
            return [{"exchange_id": "1", "ts": params[0], "sharp": 0.90, "sig_mid": 0.88}]

    class XM:
        @property
        def _sig_prices(self):
            return {"1": {"best_bid": state["bid"], "best_ask": state["ask"], "bid_size": 1000, "ask_size": 1000}}

        def active_sig_exchange_ids(self):
            return ["1"]

        def aligned_benchmark(self, ex):
            return 0.90

    stop = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        state["ticks"] += 1
        if state["ticks"] == 2:                       # after the first tick: SIG snaps back toward Poly
            state["bid"], state["ask"] = 0.875, 0.885
        if state["ticks"] >= 3:
            stop.set()
        await real_sleep(0)

    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    cfg = {"cross_market": {"fresh_gap_mode": "shadow", "fresh_gap_path": path}}
    asyncio.run(main_sig._fresh_gap_loop(store=Store(), xm_provider=XM(), cfg_raw=cfg, stop_event=stop))
    ev = [json.loads(line) for line in open(path)]
    assert [e["event"] for e in ev] == ["entry", "exit"]
    assert ev[0]["side"] == "yes" and abs(ev[0]["prices"]["taker_in"] - 0.845) < 1e-9
    assert ev[1]["reason"] == "closed" and ev[1]["id"] == ev[0]["id"]


def test_shadow_loop_off_by_default():
    import asyncio
    import main_sig
    asyncio.run(main_sig._fresh_gap_loop(store=None, xm_provider=None, cfg_raw={}, stop_event=None))   # returns at once


def test_second_shadow_tier_uses_its_own_keys_and_file(tmp_path, monkeypatch):
    """fresh_gap_t2_* runs an independent shadow (the 2c tier) with its own threshold and output file."""
    import asyncio
    import json
    import main_sig
    path = str(tmp_path / "t2.jsonl")
    state = {"ticks": 0}

    class Store:
        async def _read(self, sql, params):
            return [{"exchange_id": "1", "ts": params[0], "sharp": 0.90, "sig_mid": 0.875}]   # moved 2.5c away

    class XM:
        _sig_prices = {"1": {"best_bid": 0.845, "best_ask": 0.855, "bid_size": 1, "ask_size": 1}}

        def active_sig_exchange_ids(self):
            return ["1"]

        def aligned_benchmark(self, ex):
            return 0.90
    stop = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        state["ticks"] += 1
        if state["ticks"] >= 2:
            stop.set()
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    cfg = {"cross_market": {"fresh_gap_t2_mode": "shadow", "fresh_gap_t2_th_move": 0.02, "fresh_gap_t2_path": path,
                            "fresh_gap_mode": "off"}}
    asyncio.run(main_sig._fresh_gap_loop(store=Store(), xm_provider=XM(), cfg_raw=cfg, stop_event=stop,
                                         prefix="fresh_gap_t2"))
    ev = [json.loads(line) for line in open(path)]
    assert [e["event"] for e in ev] == ["entry"] and ev[0]["side"] == "yes"     # 2.5c move qualifies at 2c, not 3c
    import sig.config as sc
    allowed = sc._SCHEMA["cross_market"]["required"] | sc._SCHEMA["cross_market"]["optional"]
    for k in ("fresh_gap_t2_mode", "fresh_gap_t2_th_move", "fresh_gap_t2_path", "fresh_gap_t2_th_gap"):
        assert k in allowed


def test_shadow_lookback_window_is_configurable(tmp_path, monkeypatch):
    """fresh_gap_t2_lookback_h=12 queries sharp_history ~12h back (not 24h)."""
    import asyncio
    from datetime import datetime, timezone
    import main_sig
    seen = {}

    class Store:
        async def _read(self, sql, params):
            seen["lo"] = params[0]
            return []

    class XM:
        _sig_prices = {}

        def active_sig_exchange_ids(self):
            return []
    stop = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        stop.set() if seen else None
        await real_sleep(0)
    monkeypatch.setattr(main_sig.asyncio, "sleep", fast_sleep)
    cfg = {"cross_market": {"fresh_gap_t2_mode": "shadow", "fresh_gap_t2_lookback_h": 12,
                            "fresh_gap_t2_path": str(tmp_path / "x.jsonl")}}
    asyncio.run(main_sig._fresh_gap_loop(store=Store(), xm_provider=XM(), cfg_raw=cfg, stop_event=stop,
                                         prefix="fresh_gap_t2"))
    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(seen["lo"])).total_seconds() / 3600
    assert 11.9 < age_h < 12.2
