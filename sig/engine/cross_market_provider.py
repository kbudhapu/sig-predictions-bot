"""Live wiring for the cross-market divergence strategy — the missing external_provider + a SIG price
provider, fed by a background poller. Kept OUT of signals/ (a concurrent session owns that package);
this only IMPORTS the already-built, polarity-correct pieces there.

────────────────────────────────────────────────────────────────────────────────────────────────────
POLARITY — read twice. This provider returns RAW, venue-native Polymarket YES prices and NEVER flips.
The single polarity flip is owned by signals/cross_market.resolve_external_prices, which the STRATEGY
calls. For the inverted race (us_house_control_dem) the mapping's yes_token_id is the Polymarket
REPUBLICAN-House token (raw midpoint ~0.075); we return that 0.075 RAW, and the resolver flips it ONCE
to the SIG-aligned ~0.925. If this provider also flipped, that race would trade backwards forever. So:
RAW here, flip in the resolver — never both.
────────────────────────────────────────────────────────────────────────────────────────────────────

Async/sync bridge: the Strategy protocol's evaluate() is SYNC, but Polymarket + the SIG store are async.
A background poller (run_poller) refreshes two in-memory caches on a sane cadence (don't hammer the
external API); the sync providers just read the caches and stamp a freshness age. A fetch failure keeps
the last value with its old timestamp, so age_s grows and the strategy's external-staleness guard drops
it — a dead Polymarket never produces a phantom "divergence".
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable

import structlog

from strategies.cross_market.strategy import ExternalQuote
from signals.cross_market import resolve_external_prices
from signals.poly_client import PolymarketError
from signals.conviction import conviction_gated_stop
from signals.mapping import RaceMapping, ExternalRef
from signals import wiki_ratings as _wiki

log = structlog.get_logger("cross_market_provider")


def _valid_prob(p: Any) -> bool:
    """A usable external probability is a real number STRICTLY inside (0, 1). Exactly 0.0/1.0 is gamma's
    placeholder for a market with no live book — not a price. Genuine longshots (0.02, 0.98) still pass."""
    try:
        return p is not None and 0.0 < float(p) < 1.0
    except (TypeError, ValueError):
        return False


def _age_from_updated_at(updated_at: Any, now: datetime | None = None) -> float:
    """Seconds since a price row's updated_at ISO stamp; 0.0 if unparseable (treat as fresh — the WS
    feed stamps every write, so a missing stamp is a shape quirk, not staleness)."""
    if not updated_at:
        return 0.0
    try:
        s = str(updated_at).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, ((now or datetime.now(timezone.utc)) - t).total_seconds())
    except (ValueError, TypeError):
        return 0.0


class CrossMarketProvider:
    """Builds the sig_price_provider + external_provider the CrossMarketDivergenceStrategy consumes, and
    a poller that keeps both fresh. Only the mapping's ACTIVE races (is_active) are polled/served."""

    def __init__(self, *, mapping: dict, store: Any,
                 poly_client_factory: Callable[[], Any] | None = None,
                 sig_client: Any = None, sig_fetch_interval_s: float = 30.0,
                 external_stale_s: float = 30.0, convergence_tol: float = 0.01,
                 exit_hard_stop_pct: float = 0.25, exit_support_conviction: float = 0.30,
                 thesis_reversal_tol: float = 0.05,
                 wiki_enabled: bool = False, sig_read_factory: Callable[[], Any] | None = None,
                 wiki_stale_s: float = _wiki.WIKI_STALE_SECONDS,
                 poly_concurrency: int = 20) -> None:
        self._mapping = mapping
        self._store = store
        # Max in-flight Polymarket fetches per refresh. The per-race fetch is network-bound and the whole
        # active set (~119 races) is swept CONCURRENTLY under this bound each poll, so a 1s cadence is
        # feasible (sequential would take ~10s). Probed live: conc=20 sweeps 119 races in ~0.9s with 0×429.
        self._poly_concurrency = max(1, int(poly_concurrency))
        # sig_exchange_id -> RaceMapping, restricted to active races we actually trade.
        self._by_sig_ex = {str(rm.sig_exchange_id): rm for rm in mapping.values()
                           if rm.sig_exchange_id and rm.is_active()}
        self._poly_factory = poly_client_factory or self._default_poly_factory
        # SIG read client (SigClient) — used to fetch FRESH SIG prices for OUR races directly, because the
        # shared feed only keeps ~90 high-volume markets fresh and these election markets fall in the stale
        # tail (observed ~20 min old). Cross-market is a slow edge but still needs a representative SIG
        # price; fetching our 5 ourselves (one batch /exchanges/prices call per poll) decouples us from the
        # slow feed. None ⇒ fall back to the shared store price (may be stale → the strategy's sig_stale_s
        # drops it). exchange_prices is SYNC, so refresh() calls it via asyncio.to_thread.
        self._sig_client = sig_client
        self._sig_fetch_interval_s = float(sig_fetch_interval_s)   # min seconds between SIG price fetches
        self._last_sig_fetch = 0.0                                 # monotonic ts of the last SIG fetch
        # ── thesis-based EXIT params (distinct from MM's price stop) ──
        self._external_stale_s = float(external_stale_s)          # drop a benchmark older than this
        self._convergence_tol = float(convergence_tol)            # |SIG − sharp| ≤ this ⇒ gap closed ⇒ TP
        self._exit_hard_stop_pct = float(exit_hard_stop_pct)      # WIDE backstop (≥ MM's 0.20; noise > gap)
        self._exit_support_conviction = float(exit_support_conviction)  # benchmark conviction to keep HOLDING
        self._thesis_reversal_tol = float(thesis_reversal_tol)     # adverse-gap band below which a flip is noise, not a reversal (HOLD)
        # caches
        self._poly_cache: dict[str, dict[str, tuple[float, float]]] = {}   # sig_ex -> {venue: (raw_yes, fetched_monotonic)}
        self._sig_prices: dict[str, dict] = {}                             # exchange_id -> store price row (fallback)
        self._fresh_sig: dict[str, tuple[float, float, float]] = {}        # exchange_id -> (bid, ask, fetched_monotonic)
        self._refreshes = 0
        # ── Wikipedia SECONDARY benchmark (flag-gated; default OFF ⇒ fully inert) ──
        # Polymarket stays PRIMARY on overlap; Wikipedia ratings cover the ~71 Senate/Governor state seats
        # Polymarket doesn't, as a COARSE (rating_derived) benchmark — the strategy's conviction weighting
        # then makes those trade only on LARGE gaps. Built once (SIG seat titles × Wikipedia ratings → a
        # per-exchange benchmark), re-scraped on the slow WIKI cadence; synthetic RaceMappings are injected
        # into the SHARED mapping dict so the strategy iterates them with no extra wiring.
        self._wiki_enabled = bool(wiki_enabled)
        self._sig_read_factory = sig_read_factory or self._default_sig_read_factory
        self._wiki_stale_s = float(wiki_stale_s)
        self._wiki_benchmarks: dict[str, dict] = {}    # sig_ex -> {fair, race, office, state, party, …}
        self._wiki_scraped_at = 0.0                    # monotonic of the last SUCCESSFUL wiki scrape (0 = never)
        self._wiki_built = False                       # SIG-market×rating match done (markets are static)

    @staticmethod
    def _default_poly_factory():
        from signals.poly_client import PolymarketClient
        return PolymarketClient(timeout=10.0)

    @staticmethod
    def _default_sig_read_factory():
        """A read-only SigClient for the ONE-TIME seat-market title fetch (markets are static). Built
        here so the provider needs no SIG client injected (main_sig passes sig_client=None)."""
        import os
        from sig.sig_client import SigClient
        return SigClient(base_url=os.environ.get("SIG_BASE_URL") or None, timeout=30.0)

    def active_sig_exchange_ids(self) -> list[str]:
        return list(self._by_sig_ex.keys())

    def wiki_sig_exchange_ids(self) -> list[str]:
        """The sig_exchange_ids benchmarked by Wikipedia (observability; empty unless wiki is enabled+built)."""
        return list(self._wiki_benchmarks.keys())

    # ── Wikipedia SECONDARY benchmark build + refresh (flag-gated) ──
    @staticmethod
    def _scrape_wiki_estimates() -> list:
        """BLOCKING (run via to_thread): scrape Wikipedia Senate + Governor rating pages → RatingEstimates."""
        out: list = []
        for office in ("senate", "governor"):
            try:
                out.extend(_wiki.scrape_office(office))
            except Exception as exc:  # noqa: BLE001
                log.warning("cross_market_wiki_scrape_failed", office=office, error=type(exc).__name__)
        return out

    def _inject_wiki_races(self, markets: list, estimates: list) -> None:
        """SYNC (runs in the coroutine, serialized with the sync evaluate()): match SIG seat markets to
        ratings and inject a 'wikipedia'-venue RaceMapping per matched exchange into the SHARED mapping —
        never over a sig_exchange_id Polymarket already owns (Polymarket stays PRIMARY on overlap)."""
        benchmarks, unmatched = _wiki.build_sig_exchange_benchmarks(markets, estimates)
        injected = 0
        for ex_id, b in benchmarks.items():
            if ex_id in self._by_sig_ex:
                continue                                   # Polymarket PRIMARY on overlap — never override
            rm = RaceMapping(
                key=f"wiki:{b['race']}", description=f"Wikipedia rating benchmark ({b['office']} {b['state']})",
                resolution_match=True, confidence="high",
                externals={"wikipedia": ExternalRef(
                    venue="wikipedia", slug=b["race"], yes_outcome=b["party"],
                    polarity="direct", liquidity_ok=True, notes="rating_derived (coarse) benchmark")},
                sig_exchange_id=ex_id, sig_market_id=None, notes=b["source"])
            self._mapping[rm.key] = rm                     # SHARED dict → the strategy iterates it
            self._by_sig_ex[ex_id] = rm
            self._wiki_benchmarks[ex_id] = b
            injected += 1
        self._wiki_built = True
        self._wiki_scraped_at = time.monotonic()
        log.info("cross_market_wiki_built", matched=len(benchmarks), injected=injected,
                 unmatched=len(unmatched), overlap_skipped=len(benchmarks) - injected)

    def _update_wiki_probs(self, estimates: list) -> None:
        """SYNC: refresh the rating-derived probabilities in place (markets fixed, only ratings move)."""
        by_os = {(e.office, e.state): e for e in estimates}
        for b in self._wiki_benchmarks.values():
            e = by_os.get((b["office"], b["state"]))
            if e is not None:
                b["fair"] = round(e.p_rep if b["party"] == "rep" else e.p_dem, 4)
        self._wiki_scraped_at = time.monotonic()
        log.info("cross_market_wiki_rescraped", races=len(self._wiki_benchmarks))

    async def _refresh_wiki(self) -> None:
        """Each poll (wiki enabled): build once (SIG titles × ratings), re-scrape on the slow WIKI cadence,
        and RE-STAMP each benchmark into the external cache with a FRESH age — so the strategy's
        external_stale_s (tuned for Polymarket's 30s) does not drop a legitimately slow 6-hourly rating.
        If the scrape has gone stale (no success within wiki_stale_s) we STOP re-stamping, so the quotes
        age out and drop (a dead scrape never produces a phantom benchmark). Network runs in a thread; the
        mapping injection + cache writes run here in the coroutine, serialized with the sync evaluate()."""
        if not self._wiki_built:
            markets = await asyncio.to_thread(self._sig_read_factory().markets_all)
            estimates = await asyncio.to_thread(self._scrape_wiki_estimates)
            if markets and estimates:
                self._inject_wiki_races(markets, estimates)
        elif (time.monotonic() - self._wiki_scraped_at) >= self._wiki_stale_s:
            estimates = await asyncio.to_thread(self._scrape_wiki_estimates)
            if estimates:
                self._update_wiki_probs(estimates)
        if not self._wiki_benchmarks:
            return
        if (time.monotonic() - self._wiki_scraped_at) > self._wiki_stale_s:
            return                                         # scrape stale → let the last quotes age out → dropped
        now = time.monotonic()
        for ex, b in self._wiki_benchmarks.items():
            self._poly_cache.setdefault(ex, {})["wikipedia"] = (float(b["fair"]), now)

    # ── SYNC providers the strategy calls (read caches only; never block / never flip) ──
    def sig_price_provider(self, sig_exchange_id: str) -> tuple[float, float] | None:
        """(mid, age_s) for a race's SIG price. Prefer OUR freshly-fetched price (age = seconds since the
        poller fetched it, typically < the poll interval); fall back to the shared store price (which, for
        these low-volume races, is usually stale — the strategy's sig_stale_s then decides)."""
        key = str(sig_exchange_id)
        fresh = self._fresh_sig.get(key) or self._fresh_sig.get(key.upper())
        if fresh is not None:
            bid, ask, fetched_at = fresh
            try:
                mid = (float(bid) + float(ask)) / 2.0
            except (TypeError, ValueError):
                mid = None
            if mid is not None:
                return (mid, max(0.0, time.monotonic() - fetched_at))
        row = self._sig_prices.get(key) or self._sig_prices.get(key.upper())
        if not isinstance(row, dict):
            return None
        bid, ask = row.get("best_bid"), row.get("best_ask")
        if bid is None or ask is None:
            return None
        try:
            mid = (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            return None
        return (mid, _age_from_updated_at(row.get("updated_at")))

    def external_provider(self, sig_exchange_id: str) -> dict | None:
        """{venue: ExternalQuote} with RAW venue-native YES prices (NO polarity flip — the resolver owns
        the flip). age_s is seconds since we fetched it, so a frozen/dead external ages into the
        strategy's staleness guard and is dropped."""
        cached = self._poly_cache.get(str(sig_exchange_id))
        if not cached:
            return None
        now = time.monotonic()
        out: dict[str, ExternalQuote] = {}
        for venue, (raw, fetched_at) in cached.items():
            if raw is None:
                continue
            out[venue] = ExternalQuote(venue=venue, yes_price=float(raw), age_s=now - fetched_at)
        return out or None

    # ── THESIS-BASED EXIT (benchmark-aware; distinct from MM's price stop) ──
    def owns_exchange(self, sig_exchange_id: str) -> bool:
        """True iff this is one of cross-market's active races (so the orchestrator routes its exit here)."""
        return str(sig_exchange_id) in self._by_sig_ex

    def sig_book(self, sig_exchange_id: str, max_age_s: float = 60.0):
        """(best_bid, best_ask) from the live SIG price row, or None if missing/stale/unparseable (maker entries)."""
        fresh = self._fresh_sig.get(str(sig_exchange_id))
        if fresh is not None and (time.monotonic() - fresh[2]) <= max_age_s and 0.0 < fresh[0] < fresh[1] < 1.0:
            return float(fresh[0]), float(fresh[1])          # our own batch fetch: these markets lag the shared feed
        row = self._sig_prices.get(str(sig_exchange_id)) or {}
        upd = row.get("updated_at")
        if not upd:
            return None
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(upd).replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            return None
        if age > max_age_s or row.get("best_bid") is None or row.get("best_ask") is None:
            return None
        return float(row["best_bid"]), float(row["best_ask"])

    def aligned_benchmark(self, sig_exchange_id: str) -> float | None:
        """The SIG-ALIGNED sharp price for this race (the ONE polarity flip, via the resolver) — e.g. the
        inverted house-D race returns ~0.925, not the RAW 0.075. None if no fresh external."""
        rm = self._by_sig_ex.get(str(sig_exchange_id))
        if rm is None:
            return None
        quotes = self.external_provider(sig_exchange_id) or {}
        raw: dict[str, float | None] = {}
        for venue in rm.usable_externals():
            q = quotes.get(venue)
            raw[venue] = (None if (q is None or q.age_s > self._external_stale_s) else float(q.yes_price))
        aligned = resolve_external_prices(rm, raw)
        if not aligned:
            return None
        return aligned.get("polymarket", next(iter(aligned.values()), None))

    def sharp_snapshot(self) -> list[dict]:
        """One row per active race with a FRESH benchmark: {exchange_id, sharp (SIG-aligned), raw, sig_mid,
        venue}. Feeds the durable sharp_history time-series (main_sig._sharp_history_loop). Cache reads only."""
        out: list[dict] = []
        now = time.monotonic()
        for ex in list(self._by_sig_ex):
            try:
                aligned = self.aligned_benchmark(ex)
                if aligned is None or not _valid_prob(aligned):
                    continue
                raw_entry = (self._poly_cache.get(ex) or {}).get("polymarket")
                raw = (float(raw_entry[0]) if raw_entry is not None
                       and (now - raw_entry[1]) <= self._external_stale_s else None)
                sig = self.sig_price_provider(ex)
                row = self._sig_prices.get(ex) or self._sig_prices.get(ex.upper()) or {}
                bid, ask = row.get("best_bid"), row.get("best_ask")
                out.append({"exchange_id": ex, "sharp": float(aligned), "raw": raw,
                            "sig_mid": (float(sig[0]) if sig else None), "venue": "polymarket",
                            "sig_bid": (float(bid) if bid is not None else None),
                            "sig_ask": (float(ask) if ask is not None else None)})
            except Exception:  # noqa: BLE001 — one bad race never drops the rest
                continue
        return out

    def exit_decision(self, sig_exchange_id: str, pnl_frac: float, is_long: bool) -> tuple[bool, str | None]:
        """Cross-market's OWN exit rule for a held position. Returns (handled, reason):
          • handled=False  → not our race; the caller uses its default (MM) price stop.
          • handled=True, reason=None                  → HOLD (thesis intact; sit through the noise).
          • handled=True, reason='convergence_take_profit' → SIG reached the sharp price: edge captured.
          • handled=True, reason='hard_stop_backstop' | 'conviction_evaporated' | 'no_benchmark' → EXIT.

        The PRIMARY exit is thesis-based (conviction_gated_stop): a price drop is NOT a reason to cut while
        the sharp benchmark still backs us — only a WIDE hard backstop or an EVAPORATED benchmark cuts. The
        NATURAL exit is convergence (the gap closing). NO tight trailing stop — the edge (3-4pt) is smaller
        than SIG's noise (8-10pt), so a tight stop would cut winners before they converge."""
        if not self.owns_exchange(sig_exchange_id):
            return (False, None)
        # (1) WIDE hard backstop — ALWAYS, whatever the benchmark says (catastrophe / bad data floor).
        if pnl_frac <= -abs(self._exit_hard_stop_pct):
            return (True, "hard_stop_backstop")
        aligned = self.aligned_benchmark(sig_exchange_id)
        sig = self.sig_price_provider(sig_exchange_id)
        sig_mid = sig[0] if sig else None
        if aligned is None or sig_mid is None:
            return (True, None)   # transient no-benchmark: HOLD (the backstop above still protects)
        # gap = aligned − SIG. +gap ⇒ SIG below the sharp price (a LONG thesis: buy YES / room to RISE);
        # −gap ⇒ SIG above it (a SHORT thesis: buy NO / room to FALL). is_long = we hold the YES leg.
        gap = aligned - sig_mid
        # (2) NATURAL exit: the gap has CLOSED (either direction) → edge captured → take profit, recycle.
        if abs(gap) <= self._convergence_tol:
            return (True, "convergence_take_profit")
        # (3) RELATIVE thesis — the benchmark still backs us iff the remaining gap points our way: a long
        #     holds while SIG is still BELOW sharp (gap>0); a short (held NO) holds while SIG is still ABOVE
        #     sharp (gap<0). This works for BOTH directions (the old benchmark-vs-0.5 test cut shorts wrongly).
        thesis_ok = (gap > 0) if is_long else (gap < 0)
        if thesis_ok:
            return (True, None)                    # HOLD — sit through the noise while the gap backs us
        # The gap flipped ADVERSE — but a BARE sign-flip is noise, not a thesis reversal. SIG+sharp wiggle
        # by several points (the edge is 3-4pt, SIG noise 8-10pt); cutting on the first flip across zero is
        # exactly the whipsaw the docstring warns against (observed: 703 thesis_reversed vs 152 real TPs,
        # most at ~breakeven). Only cut on a GENUINE reversal — the sharp now firmly on the other side by
        # more than thesis_reversal_tol. Within the band, HOLD; the WIDE hard backstop (−25%) still caps loss.
        if abs(gap) <= self._thesis_reversal_tol:
            return (True, None)                    # adverse but within noise band → HOLD, don't whipsaw
        return (True, "thesis_reversed")           # sharp firmly reversed beyond the band → cut

    # ── ASYNC refresh + poller (background) ──
    async def refresh(self) -> None:
        # (a) SIG prices — one read of the shared store (populated by the WS feed), kept as the FALLBACK.
        try:
            self._sig_prices = await self._store.read_prices() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("cross_market_sig_price_refresh_failed", error=type(exc).__name__)
        # (a2) FRESH SIG prices for OUR races — one batch /exchanges/prices call. The shared feed only keeps
        # ~90 high-volume markets fresh; these election markets fall in the stale tail, so we fetch our own.
        # Time-gated so we don't hammer the shared ~100 reads/min SIG budget (the fresh-fetch was 429-ing).
        # One batch call per sig_fetch_interval_s is plenty: the strategy's sig_stale_s (default 300s) keeps
        # the last-good price valid across skipped/failed fetches, so a slower cadence costs no freshness.
        _due = (time.monotonic() - self._last_sig_fetch) >= self._sig_fetch_interval_s
        if self._sig_client is not None and self._by_sig_ex and _due:
            self._last_sig_fetch = time.monotonic()
            try:
                ids = list(self._by_sig_ex.keys())
                resp = await asyncio.to_thread(self._sig_client.exchange_prices, ids)
                data = resp.get("data", []) if isinstance(resp, dict) else []
                got = 0
                for r in (data or []):
                    if not isinstance(r, dict):
                        continue
                    ex = r.get("exchangeId", r.get("exchange_id", r.get("id")))
                    bid = r.get("bestBid", r.get("best_bid"))
                    ask = r.get("bestAsk", r.get("best_ask"))
                    if ex is None or bid is None or ask is None:
                        continue              # incomplete quote → keep last-good (it ages out of sig_stale_s)
                    self._fresh_sig[str(ex)] = (float(bid), float(ask), time.monotonic())
                    got += 1
                log.info("cross_market_sig_fresh", requested=len(ids), got=got)
            except Exception as exc:  # noqa: BLE001 — a failed SIG price fetch falls back to the store price
                log.warning("cross_market_sig_fresh_failed", error=type(exc).__name__, msg=str(exc)[:160])
        # (b) Polymarket per active race — RAW yes-leg midpoint (fallback: gamma market probability).
        #     The ~119-race set is swept CONCURRENTLY under a semaphore so one refresh finishes in ~1s
        #     (sequential was ~10s and capped the achievable poll cadence at 15s). Each race keeps its
        #     last-good quote on a failed fetch (it then ages out via external_stale_s).
        if not self._by_sig_ex:
            return
        try:
            sem = asyncio.Semaphore(self._poly_concurrency)

            async def _one(pc: Any, sig_ex: str, rm: Any):
                ref = rm.usable_externals().get("polymarket")  # Kalshi legs dormant (no live price)
                if ref is None:
                    return None
                async with sem:
                    raw = await self._fetch_poly_raw(pc, ref)
                return (sig_ex, rm.key, ref, raw)

            got = failed = 0
            async with self._poly_factory() as pc:
                results = await asyncio.gather(
                    *[_one(pc, se, rm) for se, rm in self._by_sig_ex.items()],
                    return_exceptions=True)
                now = time.monotonic()
                for r in results:
                    if not isinstance(r, tuple):
                        failed += 1
                        continue
                    sig_ex, race_key, ref, raw = r
                    if raw is None:
                        failed += 1
                        continue
                    self._poly_cache[sig_ex] = {"polymarket": (float(raw), now)}
                    got += 1
                    # OBSERVABLE (debug — one line per race; too chatty for info at a 1s cadence): the RAW
                    # venue-native price + polarity. The resolver flips an `inverted` race once (aligned =
                    # 1 − raw); raw here + aligned at the signal confirms the single flip.
                    # Sampled: at the 1s poll this was ~37k lines/10min (≈90% of all journald volume,
                    # crushing retention to ~1-2h). One full sweep per ~60 refreshes keeps the polarity check.
                    if self._refreshes % 60 == 0:
                        log.debug("cross_market_raw", race=race_key, venue="polymarket",
                                  raw_yes=round(float(raw), 4), polarity=getattr(ref, "polarity", "?"),
                                  implied_aligned=round((1.0 - float(raw)) if getattr(ref, "polarity", "") == "inverted"
                                                        else float(raw), 4))
            self._refreshes += 1
            log.info("cross_market_poly_refreshed", races=len(self._by_sig_ex), got=got, failed=failed,
                     concurrency=self._poly_concurrency)
        except Exception as exc:  # noqa: BLE001 — a bad poll must never kill the loop
            log.warning("cross_market_poly_refresh_failed", error=type(exc).__name__, msg=str(exc)[:160])
        # (c) Wikipedia SECONDARY benchmark (flag-gated OFF by default): build once (SIG seat titles ×
        #     ratings) then re-stamp fresh each poll, so the ~71 Senate/Governor seats Polymarket doesn't
        #     cover get a COARSE rating-derived benchmark. Isolated in its own guard so a wiki hiccup never
        #     touches the Polymarket path above.
        if self._wiki_enabled:
            try:
                await self._refresh_wiki()
            except Exception as exc:  # noqa: BLE001 — wiki must never kill the poll
                log.warning("cross_market_wiki_refresh_failed", error=type(exc).__name__, msg=str(exc)[:160])

    async def _fetch_poly_raw(self, pc: Any, ref: Any) -> float | None:
        """RAW Polymarket YES-leg price for this ref — the yes_token_id midpoint (freshest), falling
        back to the gamma market's outcome price. RAW = exactly as Polymarket reports its chosen YES
        leg; NO polarity flip (the resolver applies polarity once).

        CLOB /midpoint 404s (or 400s) for a token with no live order book — true for ~21/119 thin-book
        races. That is EXPECTED, not an error: it must fall THROUGH to the gamma outcome-price fallback,
        not abort the fetch (the old single try/except swallowed the 404 and returned None, so those
        races got NO price at all and spammed a warning every poll)."""
        tok = getattr(ref, "yes_token_id", None)
        if tok:
            try:
                mid = await pc.get_midpoint(tok)
                if _valid_prob(mid):
                    return mid
            except PolymarketError as exc:
                # 404/400 = no CLOB book for this token → expected; fall through to gamma silently.
                if exc.status not in (404, 400):
                    log.warning("cross_market_midpoint_failed", status=exc.status, msg=str(exc)[:120])
            except Exception as exc:  # noqa: BLE001 — transport hiccup; still try gamma
                log.warning("cross_market_midpoint_failed", error=type(exc).__name__, msg=str(exc)[:120])
        mkt_id = getattr(ref, "market_id", None)
        if mkt_id:
            try:
                m = await pc.get_market(mkt_id)
                if m is not None:
                    # prefer_clob=False: the midpoint already 404'd/was tried above — use the gamma
                    # last-trade outcome price directly rather than re-hitting CLOB /midpoint.
                    gp = await pc.get_market_probability(m, ref.yes_outcome or "Yes", prefer_clob=False)
                    # A degenerate 0.0/1.0 is NOT a real price — it is gamma's placeholder for a market with
                    # no live book (true for the ~21 thin-book races this fallback exists for). Returning it
                    # would manufacture a fake ~100pt divergence; return None so the race is simply dropped
                    # (no external → ages out) instead of spamming IMPLAUSIBLE_GAP audits.
                    if _valid_prob(gp):
                        return gp
            except Exception as exc:  # noqa: BLE001
                log.warning("cross_market_poly_fetch_failed", venue=getattr(ref, "venue", "?"),
                            error=type(exc).__name__, msg=str(exc)[:120])
        return None

    async def run_poller(self, *, stop_event: asyncio.Event | None = None,
                         interval_s: float = 15.0, min_interval_s: float = 1.0) -> None:
        """Refresh the caches on a PERIOD of interval_s — the sleep subtracts the refresh's own wall-time,
        so interval_s is the true cycle time (refresh + sleep), not refresh + interval_s. With the
        concurrent sweep (~1s for ~119 races) a 1s period runs refreshes near back-to-back. min_interval_s
        is the hard floor (default 1.0) so a mis-set tiny interval can't busy-spin the free external API."""
        period = max(float(min_interval_s), float(interval_s))
        log.info("cross_market_poller_start", active_races=len(self._by_sig_ex),
                 interval_s=period, concurrency=self._poly_concurrency)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            t0 = time.monotonic()
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("cross_market_poller_iteration_error", error=type(exc).__name__)
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))


class LoggingStrategyWrapper:
    """Transparent Strategy-protocol wrapper that LOGS each emitted cross-market signal (race, SIG price,
    aligned external price AFTER the resolver's single flip, divergence, direction, size, skepticism) so
    the edge is observable live and polarity is confirmable (inverted race's aligned ≈ 0.925, not 0.075).
    Delegates everything else to the wrapped strategy — it does not change behaviour.

    LEG SHADOW (cross_market.leg_shadow_mode=shadow): also appends each signal, tagged with its leg type and
    whether a NO-only filter would have skipped it, to a jsonl (deduped per market+side per `leg_dedupe_s`).
    tools/leg_shadow_validate.py scores kept vs skipped legs at 1-24h. Record-only: intents pass through."""

    def __init__(self, inner: Any, *, leg_record_path: str | None = None, leg_dedupe_s: float = 1800.0) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_leg_path", leg_record_path)
        object.__setattr__(self, "_leg_dedupe_s", float(leg_dedupe_s))
        object.__setattr__(self, "_leg_last", {})

    def __getattr__(self, name: str) -> Any:          # delegate name/tier/cadence_s/on_fill/… to inner
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def evaluate(self, state: Any) -> list:
        inner = object.__getattribute__(self, "_inner")
        intents = inner.evaluate(state)
        for i in intents:
            m = getattr(i, "metadata", {}) or {}
            log.info("cross_market_signal", race=m.get("race"), side=getattr(i, "side", None),
                     leg=m.get("option_side"),   # yes = long YES; no = buy NO (short YES) — the real direction
                     sig_price=m.get("sig_price"), aligned=m.get("external_fair"),
                     divergence=m.get("divergence"), sigma_p=m.get("sigma_p"),
                     skepticism=m.get("skepticism"), primary_venue=m.get("primary_venue"),
                     venues=m.get("venues"), confidence=getattr(i, "signal_strength", None))
        self._record_legs(intents)
        return intents

    def _record_legs(self, intents: list) -> None:
        path = object.__getattribute__(self, "_leg_path")
        if not path or not intents:
            return
        import json as _json
        import os as _os
        from datetime import datetime, timezone
        last = object.__getattribute__(self, "_leg_last")
        dedupe = object.__getattribute__(self, "_leg_dedupe_s")
        now = time.monotonic()
        lines = []
        for i in intents:
            m = getattr(i, "metadata", {}) or {}
            lt = m.get("leg_type")
            if not lt:
                continue
            key = (str(getattr(i, "exchange_id", "")), m.get("option_side"))
            if now - last.get(key, -1e18) < dedupe:
                continue
            last[key] = now
            lines.append(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(), "exchange_id": key[0], "race": m.get("race"),
                "option_side": m.get("option_side"), "leg_type": lt,
                "no_filter_skip": not lt.endswith("-NO"),          # NO-only filter would drop YES legs
                "sig_price": m.get("sig_price"), "external_fair": m.get("external_fair"),
                "divergence": m.get("divergence"), "limit_price": m.get("limit_price")}))
        if not lines:
            return
        try:
            _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as exc:  # noqa: BLE001 — shadow recording must never affect trading
            log.warning("leg_shadow_record_failed", error=type(exc).__name__)
