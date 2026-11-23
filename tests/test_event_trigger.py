"""The all-237 event-trigger edge — abnormal-move detection, GDELT news-volume, the justified/overdone
combine, and the flag-gated strategy. All OFFLINE (providers + fetch + pacer injected).

Pins the behaviour the task calls out: the detector fires on the real 0.675 Senate-control spike but not
on normal wobble, refuses to fire on thin history, and catches trade-count bursts; GDELT queries are
disambiguated per race and the sweep respects the rate limit; a news-confirmed move → FOLLOW while a
no-news / reverting move → FADE (direction from move+justified/overdone, never from news content); the
strategy is inert until the flag is on, is observable, and routes all 237.
"""
from __future__ import annotations

from signals.abnormal_move import (
    AbnormalMove, AbnormalMoveConfig, Candle, detect_abnormal_move, scan_markets,
)
from signals.news_volume import (
    NewsVolume, RaceQuery, build_query, detect_news_spike, parse_timelinevol,
    queries_from_race_mapping, sweep_news_volume,
)
from signals.event_trigger import EventTriggerConfig, classify_event
from strategies.event_trigger import EventTriggerStrategy


def _c(close: float, rng: float = 0.03, tc: int = 50, t: str = "t") -> Candle:
    return Candle(time=t, open=close, high=close + rng / 2, low=close - rng / 2, close=close,
                  vwap=close, volume=float(tc * 100), trade_count=tc)


# ─────────────────────────── PART 1: abnormal-move detection ─────────────────────────── #
def test_fires_on_the_real_senate_control_spike():
    # 8 candles wobbling ~0.37, then a close spike to 0.675 (the real Senate-control event)
    candles = [_c(0.37) for _ in range(8)] + [_c(0.675)]
    mv = detect_abnormal_move("843", candles)
    assert mv is not None and mv.direction == "up"
    assert mv.move_size > 0.25 and mv.move_ratio > 3.0 and "move" in mv.reasons


def test_does_not_fire_on_normal_wobble():
    candles = [_c(0.37 + 0.001 * (i % 3 - 1), rng=0.01, tc=10) for i in range(12)]
    assert detect_abnormal_move("843", candles) is None       # sub-min_move drift → no event


def test_thin_history_does_not_false_fire():
    candles = [_c(0.37), _c(0.40), _c(0.80)]                   # only 3 candles (< min_candles)
    assert detect_abnormal_move("843", candles) is None


def test_fires_on_tradecount_burst_even_without_a_big_price_move():
    candles = [_c(0.37, rng=0.01, tc=10) for _ in range(8)] + [_c(0.375, rng=0.01, tc=100)]
    mv = detect_abnormal_move("948", candles)
    assert mv is not None and mv.tradecount_spike and "tradecount" in mv.reasons


def test_detects_a_reverting_spike():
    # ... wobble, spike up to 0.67, then snap back to 0.37 → latest move is a revert
    candles = [_c(0.37) for _ in range(8)] + [_c(0.67), _c(0.37)]
    mv = detect_abnormal_move("843", candles)
    assert mv is not None and mv.direction == "down" and mv.reverting is True


def test_scan_markets_is_all_237_and_isolates_errors():
    data = {
        "843": [_c(0.37) for _ in range(8)] + [_c(0.675)],    # spikes
        "841": [_c(0.09, rng=0.01, tc=10) for _ in range(12)],  # calm
    }
    def _api(c):
        return {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                "vwap": c.vwap, "volume": c.volume, "tradeCount": c.trade_count}
    def fetch(ex):
        if ex == "BROKEN":
            raise RuntimeError("boom")
        return {"candles": [_api(c) for c in data[ex]]}
    moves = scan_markets(["843", "841", "BROKEN"], fetch)
    assert [m.exchange_id for m in moves] == ["843"]          # only the spiker; broken skipped


# ─────────────────────────── PART 2: GDELT news volume ─────────────────────────── #
def test_query_is_disambiguated_not_bare_state():
    q = build_query("georgia", "senate", ["Jon Ossoff", "Mike Collins"])
    assert '"Georgia Senate"' in q and '"Jon Ossoff"' in q and '"Mike Collins"' in q
    assert "election" in q
    assert q.strip() != '"Georgia"'                            # never a bare state query


class _Ext:
    def __init__(self, yo): self.yes_outcome = yo
class _RM:
    def __init__(self, externals): self.externals = externals


def test_queries_from_race_mapping_merges_per_party_candidates():
    rm = {
        "georgia_senate_rep": _RM({"kalshi": _Ext("Republican (Mike Collins)")}),
        "georgia_senate_dem": _RM({"kalshi": _Ext("Democratic (Jon Ossoff)")}),
        "texas_senate_rep": _RM({"kalshi": _Ext("Republican (Ken Paxton)")}),
    }
    qs = {q.race_key: q for q in queries_from_race_mapping(rm)}
    assert set(qs) == {"senate:georgia", "senate:texas"}      # per-party entries de-duped to one race
    assert "Mike Collins" in qs["senate:georgia"].query and "Jon Ossoff" in qs["senate:georgia"].query


def test_news_spike_detection_drops_trailing_zero_and_flags_a_spike():
    series = [("d1", 0.004), ("d2", 0.005), ("d3", 0.006), ("d4", 0.005), ("d5", 0.05), ("d6", 0.0)]
    nv = detect_news_spike("senate:georgia", series, mult=3.0)
    assert nv.spike is True and nv.latest == 0.05             # trailing 0 (incomplete day) ignored
    flat = [("d1", 0.01), ("d2", 0.01), ("d3", 0.01), ("d4", 0.01), ("d5", 0.011)]
    assert detect_news_spike("x", flat, mult=3.0).spike is False
    assert detect_news_spike("x", [("d1", 0.01), ("d2", 0.5)], mult=3.0).spike is False  # too few points


def test_parse_timelinevol_and_rate_limited_sweep():
    resp = {"timeline": [{"data": [{"date": "d1", "value": 0.004}, {"date": "d2", "value": 0.004},
                                    {"date": "d3", "value": 0.004}, {"date": "d4", "value": 0.004},
                                    {"date": "d5", "value": 0.05}]}]}
    assert parse_timelinevol(resp)[-1] == ("d5", 0.05)
    qs = [RaceQuery("senate:georgia", "georgia", "senate", "q1"),
          RaceQuery("senate:texas", "texas", "senate", "q2")]
    naps: list[float] = []
    out = sweep_news_volume(qs, fetch_timelinevol=lambda _q: resp,
                            sleep=lambda s: naps.append(s), rate_limit_s=5.0)
    assert set(out) == {"senate:georgia", "senate:texas"} and out["senate:georgia"].spike
    assert naps == [5.0]                                       # one 5s pause between the two calls


# ─────────────────────────── PART 3: combine (follow/fade) ─────────────────────────── #
def _mv(direction="up", ratio=4.0, tc=True, reverting=False, last=0.70, prev=0.60):
    return AbnormalMove(exchange_id="948", direction=direction, move_size=abs(last - prev),
                        baseline=0.03, move_ratio=ratio, tradecount_spike=tc, tradecount_ratio=5.0,
                        reverting=reverting, last_close=last, prev_close=prev, timestamp="t",
                        n_candles=12, reasons=("move",))


def _news(spike=True, ratio=10.0):
    return NewsVolume("senate:georgia", 0.05 if spike else 0.006, 0.005, ratio, spike, 10)


def test_move_plus_news_is_follow_with_move_direction():
    s = classify_event(_mv("up"), _news(spike=True))
    assert s.mode == "follow" and s.side == "buy"              # up + news → follow → buy (with the move)
    s2 = classify_event(_mv("down"), _news(spike=True))
    assert s2.mode == "follow" and s2.side == "sell"


def test_move_without_news_is_fade_against_the_move():
    s = classify_event(_mv("up"), _news(spike=False))
    assert s.mode == "fade" and s.side == "sell"              # up + no news → fade → sell (against)
    s2 = classify_event(_mv("down"), None)
    assert s2.mode == "fade" and s2.side == "buy"


def test_reverting_boosts_fade_confidence():
    base = classify_event(_mv("up", reverting=False), _news(spike=False))
    rev = classify_event(_mv("up", reverting=True), _news(spike=False))
    assert rev.confidence > base.confidence


def test_sharp_source_confirm_vs_contradict():
    # follow up to 0.70: sharp BEYOND (0.85) confirms; sharp below (0.55) contradicts
    conf = classify_event(_mv("up", last=0.70), _news(spike=True), sharp_p=0.85)
    contra = classify_event(_mv("up", last=0.70), _news(spike=True), sharp_p=0.55)
    assert conf.sharp_checked and conf.confidence > contra.confidence


def test_subthreshold_move_emits_nothing_and_sigma_in_band():
    assert classify_event(_mv("up", ratio=0.3, tc=False), _news(spike=False)) is None
    s = classify_event(_mv("up", ratio=5.0), _news(spike=True))
    assert EventTriggerConfig().min_sigma <= s.sigma_p <= EventTriggerConfig().max_sigma


# ─────────────────────────── the flag-gated strategy ─────────────────────────── #
def _strategy(*, enabled, moves, news_spike=True):
    return EventTriggerStrategy(
        abnormal_move_provider=lambda: moves,
        news_provider=lambda rk: _news(spike=news_spike),
        sig_price_provider=lambda ex: (0.50, 1.0),
        exchange_to_race_key=lambda ex: "senate:georgia",
        sharp_provider=lambda ex: None,
        config=EventTriggerConfig(enabled=enabled),
    )


def test_strategy_is_inert_until_the_flag_is_on():
    strat = _strategy(enabled=False, moves=[_mv("up")])
    assert strat.evaluate(None) == []                         # FLAG GATE — default off


def test_strategy_emits_a_follow_intent_and_is_observable():
    strat = _strategy(enabled=True, moves=[_mv("up")], news_spike=True)
    intents = strat.evaluate(None)
    assert len(intents) == 1
    it = intents[0]
    assert it.exchange_id == "948" and it.side == "buy" and it.strategy_type == "directional"
    assert it.metadata["mode"] == "follow" and it.metadata["edge"] == "event_trigger"
    assert 0.0 < it.metadata["sigma_p"] <= 0.30
    assert len(strat.events) == 1 and strat.events[0]["disposition"] == "follow"


def test_strategy_fade_and_all_237_routing():
    strat = _strategy(enabled=True, moves=[_mv("up")], news_spike=False)
    assert strat.evaluate(None)[0].side == "sell"             # no news → fade → sell
    # all-237: many markets route independently
    many = EventTriggerStrategy(
        abnormal_move_provider=lambda: [_mv("up"), _mv("down"), _mv("up")],
        news_provider=lambda rk: _news(spike=True),
        sig_price_provider=lambda ex: (0.5, 1.0),
        exchange_to_race_key=lambda ex: "senate:georgia",
        config=EventTriggerConfig(enabled=True))
    assert len(many.evaluate(None)) == 3


def test_sharp_crosscheck_is_conviction_scaled():
    # a CONFIDENT benchmark (0.95) confirming moves confidence MORE than a near-mushy (0.71) one
    strong = classify_event(_mv("up", last=0.70), _news(spike=True), sharp_p=0.95)
    weak = classify_event(_mv("up", last=0.70), _news(spike=True), sharp_p=0.71)
    assert strong.confidence > weak.confidence
    # and a COARSE benchmark earns less than a SHARP one at the same extreme price
    sharp_bm = classify_event(_mv("up", last=0.70), _news(spike=True), sharp_p=0.95, benchmark_quality="sharp")
    coarse_bm = classify_event(_mv("up", last=0.70), _news(spike=True), sharp_p=0.95,
                               benchmark_quality="rating_derived")
    assert sharp_bm.confidence >= coarse_bm.confidence
