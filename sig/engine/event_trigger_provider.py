"""Live wiring for the event-trigger edge — the providers + background poller the EventTriggerStrategy
consumes. Kept OUT of signals/ (concurrent sessions own that package); this only composes the already-built
pure pieces (signals/abnormal_move, signals/news_volume, signals/event_trigger).

DATA SOURCE — Option A (read the live WS stream, NOT 237 REST calls):
  • The poller reads the shared store ONCE per tick (store.read_prices(), all 237, WS-fed ~31s) and keeps
    its OWN in-memory rolling window of mids per market. The abnormal-move scan runs on that window — no
    /price-history × 237 (which would re-create the exact 429 pressure the cross-market retire just removed).
  • HYBRID tradeCount enrichment: a mid-only series has no trade counts, so for the FEW markets that fire a
    price-move candidate we make ONE /exchanges/{id}/price-history call each to confirm a trade-count burst.
    A handful of reads (only firing candidates), never 237.
  • News: a paced GDELT timelinevol sweep (≤1 req / rate_limit_s, external — off the SIG read budget) keeps a
    {race_key: NewsVolume} cache the sync provider reads.
  • Sharp cross-check: delegated to an injected benchmark callable (the cross-market provider's SIG-aligned
    sharp price) where a race overlaps Polymarket; None elsewhere (classify_event handles None).

Runs in its OWN asyncio task (run_poller) — NEVER inside the MM price-pump loop (the order_flow lesson: a
blocking poll in the price pump starved MM's freshness). evaluate() reads caches only; it never blocks.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

import structlog

from signals.abnormal_move import AbnormalMove, Candle, detect_abnormal_move, AbnormalMoveConfig
from signals.news_volume import RaceQuery, build_query, detect_news_spike, parse_timelinevol, _default_fetch_timelinevol

log = structlog.get_logger("event_trigger_provider")

# "Will the {Party} win the {State} {Office}?" → (office, state). Office incl. the U.S. chamber-control rows.
_TITLE_RE = re.compile(
    r"win the (?:U\.S\.\s+)?(?P<rest>.+?)\s*\??$", re.IGNORECASE)
_OFFICE_WORDS = ("senate", "house", "governor")


def _parse_title(title: str) -> tuple[str, str] | None:
    """(race_key, GDELT query) from a SIG market title, or None if it is not a parseable race.
    race_key = "{office}:{state}" (e.g. 'senate:georgia'); the state phrase drops a trailing office word."""
    if not title:
        return None
    m = _TITLE_RE.search(str(title))
    if not m:
        return None
    rest = m.group("rest").strip().lower()                      # e.g. "georgia senate" | "ga-02 house race" | "senate"
    office = next((o for o in _OFFICE_WORDS if o in rest), None)
    if office is None:
        return None
    # state/district = the phrase with the office word (and a trailing "race") removed
    state = rest.replace(office, " ").replace("race", " ")
    state = re.sub(r"\s+", " ", state).strip() or "us"
    race_key = f"{office}:{state}"
    return race_key, build_query(state, office)


class EventTriggerProvider:
    """Builds the 5 providers EventTriggerStrategy injects + a poller that keeps the price window and the
    news cache fresh. Option A: price moves come from an in-memory window fed by the shared WS store."""

    def __init__(
        self, *, store: Any, markets: list[dict], sig_client: Any = None,
        sharp_benchmark: Callable[[str], float | None] | None = None,
        window: int = 14, min_candles: int = 6, move_mult: float = 3.0, min_move: float = 0.03,
        enrich_tradecount: bool = True, enrich_resolution: str = "5m",
        news_rate_limit_s: float = 5.0, news_mult: float = 3.0,
    ) -> None:
        self._store = store
        self._sig_client = sig_client
        self._sharp = sharp_benchmark
        self._window = int(window)
        self._min_candles = int(min_candles)
        self._move_mult = float(move_mult)
        self._min_move = float(min_move)
        self._enrich = bool(enrich_tradecount)
        self._enrich_res = str(enrich_resolution)
        self._news_rate_limit_s = float(news_rate_limit_s)
        self._news_mult = float(news_mult)

        # exchange_id -> (race_key, RaceQuery); built once from the SIG market titles (all parseable races).
        self._ex_race: dict[str, str] = {}
        self._queries: dict[str, RaceQuery] = {}                # race_key -> RaceQuery (dedup across R/D legs)
        for mk in markets or []:
            title = mk.get("title") or mk.get("question") or ""
            parsed = _parse_title(title)
            if parsed is None:
                continue
            race_key, query = parsed
            for ex in (mk.get("exchanges") or []):
                eid = str(ex.get("id") or ex.get("exchange_id") or "")
                if eid:
                    self._ex_race[eid] = race_key
            self._queries.setdefault(race_key, RaceQuery(race_key=race_key, state=race_key.split(":", 1)[-1],
                                                         office=race_key.split(":", 1)[0], query=query))

        self._mids: dict[str, list[tuple[float, float]]] = {}   # exchange_id -> [(monotonic_ts, mid), ...]
        self._news: dict[str, Any] = {}                         # race_key -> NewsVolume (cache)
        self._news_cursor = 0                                   # round-robin index into the race list
        self._last_news_fetch = 0.0
        self._observes = 0

    # ── the 5 sync providers EventTriggerStrategy injects (read caches only; never block) ──
    def abnormal_move_provider(self) -> list[AbnormalMove]:
        """Detect price-move candidates from the in-memory mid window (all markets), then HYBRID-enrich the
        few that fire with one /price-history call each for a trade-count burst."""
        out: list[AbnormalMove] = []
        for ex, samples in self._mids.items():
            mv = self._detect_from_mids(ex, samples)
            if mv is not None:
                out.append(mv)
        if self._enrich and out:
            out = [self._enrich_tradecount(mv) for mv in out]
        return out

    def news_provider(self, race_key: str | None) -> Any | None:
        if not race_key:
            return None
        return self._news.get(race_key)

    def sharp_provider(self, exchange_id: str) -> float | None:
        if self._sharp is None:
            return None
        try:
            return self._sharp(str(exchange_id))
        except Exception:  # noqa: BLE001
            return None

    def sig_price_provider(self, exchange_id: str) -> tuple[float, float] | None:
        samples = self._mids.get(str(exchange_id))
        if not samples:
            return None
        ts, mid = samples[-1]
        return (float(mid), max(0.0, time.monotonic() - ts))

    def exchange_to_race_key(self, exchange_id: str) -> str | None:
        return self._ex_race.get(str(exchange_id))

    # ── the mid-series move detector (store-window analog of the OHLCV detector) ──
    def _detect_from_mids(self, exchange_id: str, samples: list[tuple[float, float]]) -> AbnormalMove | None:
        if len(samples) < self._min_candles:
            return None
        mids = [m for _t, m in samples]
        latest, prev = mids[-1], mids[-2]
        move = abs(latest - prev)
        # baseline = the market's own recent per-step wobble (mean |Δmid| over the window, excl. the latest).
        steps = [abs(mids[i] - mids[i - 1]) for i in range(1, len(mids) - 1)]
        baseline = (sum(steps) / len(steps)) if steps else 0.0
        move_ratio = (move / baseline) if baseline > 1e-9 else (move / self._min_move if self._min_move > 0 else 0.0)
        fired = move >= self._min_move and (
            move >= self._move_mult * baseline if baseline > 1e-9 else move >= 2.0 * self._min_move)
        if not fired:
            return None
        prev_move = prev - (mids[-3] if len(mids) >= 3 else prev)
        cur_move = latest - prev
        reverting = (abs(prev_move) >= self._min_move and (cur_move * prev_move) < 0
                     and abs(cur_move) >= 0.5 * abs(prev_move))
        return AbnormalMove(
            exchange_id=str(exchange_id), direction="up" if latest >= prev else "down",
            move_size=round(move, 6), baseline=round(baseline, 6), move_ratio=round(move_ratio, 4),
            tradecount_spike=False, tradecount_ratio=0.0, reverting=reverting,
            last_close=round(latest, 6), prev_close=round(prev, 6),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"), n_candles=len(mids), reasons=("move",))

    def _enrich_tradecount(self, mv: AbnormalMove) -> AbnormalMove:
        """One /price-history call for a FIRING market → confirm a trade-count burst (the discriminator a
        mid-only series lacks). Best-effort: any failure returns the move unchanged (price-move only)."""
        if self._sig_client is None:
            return mv
        try:
            resp = self._sig_client.get(f"/exchanges/{mv.exchange_id}/price-history",
                                        params={"resolution": self._enrich_res})
            rows = resp if isinstance(resp, list) else (resp.get("candles") or resp.get("data") or [])
            candles = [Candle.from_api(r) for r in rows if isinstance(r, dict)]
            if len(candles) < 3:
                return mv
            window = candles[-(self._window + 1):-1] or candles[:-1]
            counts = sorted(float(c.trade_count) for c in window)
            med = counts[len(counts) // 2] if counts else 0.0
            latest_tc = candles[-1].trade_count
            spike = med > 0 and latest_tc >= self._news_mult * med   # reuse the same burst multiple
            if not spike:
                return mv
            ratio = (latest_tc / med) if med > 0 else float(latest_tc)
            return AbnormalMove(
                exchange_id=mv.exchange_id, direction=mv.direction, move_size=mv.move_size,
                baseline=mv.baseline, move_ratio=mv.move_ratio, tradecount_spike=True,
                tradecount_ratio=round(ratio, 4), reverting=mv.reverting, last_close=mv.last_close,
                prev_close=mv.prev_close, timestamp=mv.timestamp, n_candles=mv.n_candles,
                reasons=tuple(mv.reasons) + ("tradecount",))
        except Exception as exc:  # noqa: BLE001 — a failed enrichment never drops the move
            log.debug("event_trigger_enrich_failed", exchange=mv.exchange_id, error=type(exc).__name__)
            return mv

    # ── background refresh (own task) ──
    async def observe_prices(self) -> None:
        """Append the latest mid per market from the shared WS store (one read, all 237)."""
        try:
            prices = await self._store.read_prices() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("event_trigger_price_read_failed", error=type(exc).__name__)
            return
        now = time.monotonic()
        for ex, row in prices.items():
            if not isinstance(row, dict):
                continue
            bid, ask = row.get("best_bid"), row.get("best_ask")
            if bid is None or ask is None:
                continue
            try:
                mid = (float(bid) + float(ask)) / 2.0
            except (TypeError, ValueError):
                continue
            dq = self._mids.setdefault(str(ex), [])
            dq.append((now, mid))
            if len(dq) > self._window + 2:
                del dq[: len(dq) - (self._window + 2)]
        self._observes += 1

    async def refresh_news_one(self) -> None:
        """Fetch ONE race's GDELT timelinevol (round-robin), paced by news_rate_limit_s. External API —
        off the SIG read budget. Continuous cycling refreshes each race every (n_races × rate_limit_s)."""
        if not self._queries or (time.monotonic() - self._last_news_fetch) < self._news_rate_limit_s:
            return
        keys = list(self._queries.keys())
        rq = self._queries[keys[self._news_cursor % len(keys)]]
        self._news_cursor += 1
        self._last_news_fetch = time.monotonic()
        try:
            resp = await asyncio.to_thread(_default_fetch_timelinevol, rq.query)
            series = parse_timelinevol(resp)
            self._news[rq.race_key] = detect_news_spike(rq.race_key, series, mult=self._news_mult)
        except Exception as exc:  # noqa: BLE001 — a bad GDELT fetch keeps the last-known cache
            log.debug("event_trigger_news_fetch_failed", race=rq.race_key, error=type(exc).__name__)

    async def run_poller(self, *, stop_event: asyncio.Event | None = None,
                         price_interval_s: float = 20.0) -> None:
        """Own task: refresh the price window every price_interval_s and advance the paced news sweep.
        NEVER call this inside the MM price pump — it is a separate task so a slow GDELT fetch can never
        starve MM's price freshness (the order_flow lesson)."""
        log.info("event_trigger_poller_start", races=len(self._queries), markets=len(self._ex_race),
                 price_interval_s=price_interval_s, news_rate_limit_s=self._news_rate_limit_s)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                await self.observe_prices()
                await self.refresh_news_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("event_trigger_poller_iteration_error", error=type(exc).__name__)
            await asyncio.sleep(max(2.0, float(price_interval_s)))
