#!/usr/bin/env python3
"""sig-bot live entrypoint — the single CLI the systemd units in deploy/ invoke.

Subcommands (each maps to a deploy/ unit):
  run              the ONE capital-arbiter trading process: live stack + strategies + orch.run_forever()
  feed             the market-data process: REST price poller -> StateStore (interim transport; the
                   Supabase websocket wire-adapter is an unbuilt seam — see sig/feed/rest_poller.py)
  signal           non-capital signal process (directional/tail) — DATALESS at launch: idle heartbeat only
  supervisor       non-capital health monitor: watches feed staleness + heartbeat age, pages on trouble
  reconcile        timer job: one OMS pre-flight reconciliation pass (venue-truth vs believed state)
  pre-event-check  timer job: pre-open readiness probe (LIVE secrets + account + markets reachable)
  post-settlement  timer job: post-settlement reconciliation + audit

Design (per the OMS architecture doc): exactly ONE process (`run`) is the capital arbiter; feed/signal/
supervisor are non-capital and coordinate via the shared local SQLite state (infra/state/store.py →
data/state.db). Graceful shutdown HOLDS positions — never auto-flatten (the orchestrator's halt is
halt-but-hold). The run-lock (infra/single_instance) is a dual-runtime guard: never two writers on one
account.

SIM vs LIVE is read from config/sig.yaml global.mode. `run` honours both so the exact same entrypoint is
SIM-smoke-tested before any live flip; in SIM it wires the SimulatedBroker and (with --max-cycles) runs a
bounded, deterministic cycle loop and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime, timezone

# Make `python /opt/sig-bot/main_sig.py ...` work regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import structlog  # noqa: E402

log = structlog.get_logger("main_sig")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The SIG /markets endpoint is slow (~13s/page); the client's 15s default intermittently
# ReadTimeouts and crashes discovery. Use a generous read timeout for the entrypoint's clients.
_READ_CLIENT_TIMEOUT_S = 60.0

# Max Supabase Realtime channels on ONE WS connection. Supabase caps channels-per-client (~100) and
# joining more SILENTLY stops streaming (measured: 237 -> 0 updates applied; 40 -> 186). This is the
# per-SOCKET budget, not a cap on coverage: _RealtimeSession shards the market channels across
# ceil(N / this) sockets so the whole board streams. Kept under ~100 for headroom.
_WS_MAX_CHANNELS = 80


def _make_read_client():
    from sig.sig_client import SigClient
    return SigClient(key_env="SIG_API_KEY_READ", timeout=_READ_CLIENT_TIMEOUT_S)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _extract_exchange_ids(market: dict) -> list[str]:
    """Defensively pull exchange/contract ids out of one /markets row (shape varies)."""
    ids: list[str] = []
    # A row that carries an exchanges/contracts/outcomes collection is a MARKET: its top-level `id` is the
    # market id (it 404s on /exchanges/{id}/...), so only take explicit exchange keys from it.
    has_children = any(market.get(c) for c in ("exchanges", "contracts", "outcomes"))
    for key in (("exchangeId", "exchange_id") if has_children else ("exchangeId", "exchange_id", "id")):
        v = market.get(key)
        if isinstance(v, (str, int)):
            ids.append(str(v))
    for coll in ("exchanges", "contracts", "outcomes"):
        for e in market.get(coll, []) or []:
            if isinstance(e, dict):
                for key in ("exchangeId", "exchange_id", "id"):
                    v = e.get(key)
                    if isinstance(v, (str, int)):
                        ids.append(str(v))
            elif isinstance(e, (str, int)):
                ids.append(str(e))
    return ids


def markets_all_retrying(client, *, retries: int = 3) -> list[dict]:
    """Walk /markets with retries. The ONLY way this endpoint should ever be called.

    /markets is slow (~30-80s), occasionally ReadTimeouts mid-pagination, and intermittently 503s
    (measured 2026-10-02: 4 of 5 attempts OK, one SERVICE_UNAVAILABLE). An unretried call is a
    coin-flip that silently degrades whatever depends on it — in cmd_feed a single 503 dropped the
    whole WS transport to the REST fallback, which is how the board loses live prices.
    """
    import time as _time
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return client.markets_all()
        except Exception as exc:  # noqa: BLE001 — transient API timeout/503; retry
            last_err = exc
            log.warning("markets_discovery_retry", attempt=attempt, error=type(exc).__name__,
                        msg=str(exc)[:120])
            _time.sleep(min(5.0 * attempt, 15.0))
    log.error("markets_discovery_failed", error=type(last_err).__name__ if last_err else "unknown")
    raise last_err if last_err else RuntimeError("markets discovery failed")


def discover_exchange_ids(client, *, max_markets: int | None = None, retries: int = 3) -> list[str]:
    """Walk /markets and return the deduped set of exchange ids to track/quote. Logs the count."""
    markets = markets_all_retrying(client, retries=retries)
    seen: dict[str, None] = {}
    for m in markets:
        if not isinstance(m, dict):
            continue
        for eid in _extract_exchange_ids(m):
            seen.setdefault(eid, None)
    ids = list(seen.keys())
    if max_markets is not None and max_markets > 0:
        ids = ids[:max_markets]
    log.info("exchange_ids_discovered", markets=len(markets), exchange_ids=len(ids),
             capped=(max_markets if (max_markets and len(seen) > max_markets) else None))
    return ids


def _resolve_tournament_id(client, slug: str) -> str | None:
    """GET /tournaments/{slug} → the tournament uuid. The id may be top-level (.id) or under .data.id.
    Returns None if it cannot be resolved (caller falls back to the REST poller)."""
    data = client.get(f"/tournaments/{slug}")
    if not isinstance(data, dict):
        return None
    tid = data.get("id")
    if tid is None and isinstance(data.get("data"), dict):
        tid = data["data"].get("id")
    return str(tid) if tid else None


def _build_strategy_contracts(exchange_ids: list[str]) -> dict[str, str]:
    """strategy_id -> contract_id for OMS reconciliation attribution.

    KNOWN LIMITATION: the OMS maps ONE contract per strategy, but arb/market_making each trade many
    contracts on the mechanical floor. We map each CORE strategy to a representative id so reconciliation
    has an anchor; per-contract attribution across the full book is a reconciliation gap tracked for the
    WS/attribution follow-up. (Empty when nothing was discovered.)
    """
    anchor = exchange_ids[0] if exchange_ids else ""
    return {"arb": anchor, "market_making": anchor}


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    def _request_stop() -> None:
        log.warning("shutdown_signal_received", note="holding positions; stopping loop")
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        s = getattr(signal, sig_name, None)
        if s is None:
            continue
        try:
            loop.add_signal_handler(s, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread: fall back to default KeyboardInterrupt handling.
            pass


# --------------------------------------------------------------------------- #
# run — the capital arbiter
# --------------------------------------------------------------------------- #
async def cmd_run(args: argparse.Namespace) -> int:
    from sig.config import load_config
    from infra.state.store import StateStore
    from infra.systemd_notify import notify_ready, notify_watchdog, notify_stopping
    from infra.single_instance import acquire_run_lock, SingleInstanceError

    cfg = load_config()  # fails loud if mode==LIVE and secrets are missing
    mode = cfg.mode
    log.info("run_starting", mode=mode, max_cycles=args.max_cycles)

    try:
        acquire_run_lock()
    except SingleInstanceError as exc:
        log.error("run_lock_refused", error=str(exc))
        return 1

    # Initialise the OPERATIONAL db (trades / system_events / operator_alerts / loop_heartbeats).
    # infra/state/state.db is a separate store and opening it does NOT create these tables, so the
    # trader ran with a 0-byte trading.db and no tables at all: every durable alert write failed.
    # arb_monitor.py has always done this; the trader never did. Idempotent, so it is safe on every
    # start. Best-effort: a trader that cannot create its ops log should still trade.
    try:
        from infra.persistence import db as _opsdb

        _opsdb.init_db()
        log.info("operational_db_ready", path=str(_opsdb.DB_PATH))
    except Exception as exc:                 # noqa: BLE001
        log.error("operational_db_init_failed", error=str(exc),
                  note="durable alert/ops rows will not persist this run")

    store = StateStore()
    await store.open()

    client = None
    exchange_ids: list[str] = []
    # Bankroll, defined for BOTH modes: the MM's capital-% sizing and the arb sizer both need it,
    # and there is no fallback share count any more. LIVE overwrites this with venue-truth equity.
    equity = float(args.equity)
    try:
        if mode == "LIVE":
            from sig.sig_client import SigClient
            from sig.engine.composition import build_live_stack

            client = _make_read_client()
            exchange_ids = discover_exchange_ids(client, max_markets=args.max_markets)
            strategy_contracts = _build_strategy_contracts(exchange_ids)

            equity = float(args.equity)
            # Prefer venue-truth equity when the account read succeeds (defensive: keep the fallback).
            acct = await _account_equity(client)
            if acct and acct > 0:
                equity = acct

            # Resolve the tournament id so orders route to the tournament's isolated orderbook +
            # balance (SIG OrderInput.tournamentId). Without it, orders hit the key's default/global
            # context (the wrong book).
            slug = str((cfg.global_ or {}).get("tournament_slug") or "").strip() or "midterm-elections"
            try:
                tournament_id = await asyncio.to_thread(_resolve_tournament_id, client, slug)
            except Exception as exc:
                log.error("tournament_resolve_error", slug=slug, error=type(exc).__name__,
                          msg=str(exc)[:160])
                tournament_id = None
            if not tournament_id:
                log.error("tournament_id_unresolved", slug=slug,
                          note="orders would route to the wrong book — NOT starting")
                return 2
            log.info("tournament_resolved", slug=slug, tournament_id=tournament_id)

            # Depth provider for the arb-basket exit pass: _run_exits values each open basket's legs
            # against live order-book depth (NOT in state.db) to confirm a sell is absorbable before
            # recycling. Fetched off-thread via the read client; a failure on any id degrades to
            # "no book" ⇒ that basket HOLDS (fail-closed). Without this the exit pass runs but every
            # basket holds on `book_too_thin`.
            async def _exit_books_provider(exchange_ids_to_fetch):
                async def _one(eid):
                    try:
                        return str(eid), await asyncio.to_thread(client.orderbook, eid)
                    except Exception:
                        return str(eid), None
                pairs = await asyncio.gather(*[_one(e) for e in exchange_ids_to_fetch])
                return {eid: bk for eid, bk in pairs if bk is not None}

            orch, broker, allocator, state, oms = build_live_stack(
                config=cfg.raw, strategy_contracts=strategy_contracts, store=store,
                tick_seconds=float(cfg.loops.get("fast_loop_interval_s", 5.0)), equity=equity,
                tournament_id=tournament_id, tournament_slug=slug,
                exit_books_provider=_exit_books_provider,
            )
        else:
            from sig.engine.composition import build_sim_stack

            exchange_ids = ["YES_SIM_A", "YES_SIM_B"]
            strategy_contracts = _build_strategy_contracts(exchange_ids)
            orch, broker, allocator, state, oms = build_sim_stack(
                config=cfg.raw, strategy_contracts=strategy_contracts,
                tick_seconds=float(cfg.loops.get("fast_loop_interval_s", 5.0)),
            )
            # SIM: seed a couple of fresh prices so the MM path has a touch to quote.
            await store.upsert_prices([
                {"exchangeId": "YES_SIM_A", "bestBid": 0.48, "bestAsk": 0.52, "updated_at": _now_iso()},
                {"exchangeId": "YES_SIM_B", "bestBid": 0.30, "bestAsk": 0.34, "updated_at": _now_iso()},
            ])

        # ---- per-RACE clusters for the risk gate: a race's D and R legs share one cluster, so
        #      capital.max_cluster_frac caps TOTAL exposure to one race (long-D + short-R is one bet). ----
        _race_legs: dict[str, dict[str, str]] = {}      # race -> {"dem": ex, "rep": ex} (parity shadow)
        if mode == "LIVE" and client is not None:
            try:
                import re as _re
                _pat = _re.compile(r"will the (democratic|republican) party win the (.+?)\??$", _re.I)
                _clusters: dict[str, str] = {}
                for _m in markets_all_retrying(client):
                    _mm = _pat.match((_m.get("question") or _m.get("title") or "").strip())
                    for _e in (_m.get("exchanges") or []) if _mm else []:
                        _race = "race:" + _re.sub(r"\s+", " ", _mm.group(2).strip().lower())
                        _clusters[str(_e.get("id"))] = _race
                        _party = "dem" if _mm.group(1).lower().startswith("d") else "rep"
                        _race_legs.setdefault(_race, {})[_party] = str(_e.get("id"))
                _setter = getattr(getattr(orch, "_risk_gate", None), "set_market_clusters", None)
                if callable(_setter):
                    _setter(_clusters)
                log.info("race_clusters_set", markets=len(_clusters), races=len(set(_clusters.values())))
            except Exception as exc:  # noqa: BLE001 — never block startup; gate falls back to per-market clusters
                log.error("race_clusters_failed", error=type(exc).__name__, msg=str(exc)[:160])

        # ---- register the mechanical-floor strategies (CORE tier) ----
        from sig.engine.live_strategies import CoreArbStrategy, CoreMarketMakingStrategy

        registered = []
        # equity is REQUIRED for the MM's capital-% sizing (no fallback share count exists).
        mm = CoreMarketMakingStrategy(store=store, config=cfg.raw,
                                      exchange_ids=exchange_ids, equity=equity)
        # market_making.enabled=false: the object stays (price pump / persistence / shadow wiring use it) but is
        # NOT registered, so it emits no quotes. Budget 0 alone was not enough: it kept placing reduce-only
        # quotes, including against cross-market positions (2026-10-06).
        if bool((cfg.raw.get("market_making", {}) or {}).get("enabled", True)):
            orch.register_strategy(mm)
            registered.append(mm)
        else:
            log.info("market_making_not_registered", reason="market_making.enabled is false")
        _arb_on = bool((cfg.raw.get("arb", {}) or {}).get("enabled", True))
        if mode == "LIVE" and client is not None and _arb_on:
            # bankroll is REQUIRED by arb_sizing's per-arb and total-budget bounds. Without it the
            # sizer falls through to raw capture-fillable quantities (the old ~3-concurrent-arb
            # behaviour), so pass the venue-truth equity resolved above.
            arb = CoreArbStrategy(client=client, store=store, config=cfg.raw, bankroll=equity)
            orch.register_strategy(arb)
            registered.append(arb)
        else:
            log.info("arb_not_registered",
                     reason=("arb.enabled is false" if not _arb_on
                             else "needs live read client / relationship constraints"))

        # ---- order-flow (SIGNAL tier) — live directional fade edge, behind order_flow.enabled ----
        from sig.engine.live_strategies import CoreOrderFlowStrategy

        of_strategy = None
        of_cfg = (cfg.raw.get("order_flow", {}) or {})
        if bool(of_cfg.get("enabled", False)):
            of_strategy = CoreOrderFlowStrategy(store=store, config=cfg.raw, exchange_ids=exchange_ids)
            orch.register_strategy(of_strategy)
            registered.append(of_strategy)
            log.info("order_flow_registered",
                     max_markets=min(len(exchange_ids), int(of_cfg.get("max_markets", 20))))
        else:
            log.info("order_flow_not_registered", reason="order_flow.enabled is false")

        # ---- mean-reversion TAKER (SIGNAL tier) — operator's taker MR bet, behind mr_taker.enabled.
        #      Safe-by-construction: enter-when-flat, reduce-only sell exits (gate-clamped). ----
        from sig.engine.live_strategies import CoreMeanReversionTakerStrategy

        mr_strategy = None
        mr_cfg = (cfg.raw.get("mr_taker", {}) or {})
        if bool(mr_cfg.get("enabled", False)):
            mr_strategy = CoreMeanReversionTakerStrategy(store=store, config=cfg.raw,
                                                         exchange_ids=exchange_ids)
            orch.register_strategy(mr_strategy)
            registered.append(mr_strategy)
            log.info("mr_taker_registered",
                     markets=len(mr_strategy._exchange_ids), k_enter=mr_cfg.get("k_enter", 2.0))
        else:
            log.info("mr_taker_not_registered", reason="mr_taker.enabled is false")

        # ---- dutch-book complement arb (SIGNAL tier) — near-riskless D+R>1 short-both, atomic ----
        from sig.engine.live_strategies import CoreDutchBookStrategy

        db_strategy = None
        db_cfg = (cfg.raw.get("dutch_book", {}) or {})
        if bool(db_cfg.get("enabled", False)):
            db_strategy = CoreDutchBookStrategy(store=store, config=cfg.raw, client=client, equity=equity)
            orch.register_strategy(db_strategy)
            registered.append(db_strategy)
            log.info("dutch_book_registered", pairs=len(db_strategy._pairs))
        else:
            log.info("dutch_book_not_registered", reason="dutch_book.enabled is false")

        # ---- cross-market divergence (SIGNAL tier) — trade SIG's student price toward the sharp
        #      Polymarket price, behind cross_market.enabled. The provider returns RAW Polymarket
        #      prices; the resolver (signals/cross_market) applies the single polarity flip. ----
        xm_strategy = None
        xm_provider = None
        xm_cfg = (cfg.raw.get("cross_market", {}) or {})
        if mode == "LIVE" and client is not None and bool(xm_cfg.get("enabled", False)):
            try:
                from signals.mapping import load_race_mapping
                from strategies.cross_market.strategy import (
                    CrossMarketDivergenceStrategy, DivergenceConfig)
                from sig.engine.cross_market_provider import (
                    CrossMarketProvider, LoggingStrategyWrapper)

                xm_mapping = load_race_mapping()
                # SIG prices now come from the SHARED WS STORE (sig_client=None retires the per-poll
                # /exchanges/prices self-fetch). That self-fetch existed only because the old feed kept
                # ~90 high-volume markets fresh and our election races sat in a ~20-min stale tail; the
                # sharded WS feed now covers ALL 237 markets at ~31s (verified live), so the store price
                # is representative and the self-fetch is pure 429 pressure on the shared read budget.
                # The provider reads store prices every refresh and cross-market's sig_stale_s (300s)
                # tolerates the ~31s age with room to spare. (Polymarket stays self-polled — external.)
                xm_provider = CrossMarketProvider(
                    mapping=xm_mapping, store=store, sig_client=None,
                    sig_fetch_interval_s=float(xm_cfg.get("sig_fetch_interval_s", 30.0)),
                    # THESIS-BASED EXIT params (distinct from MM's price stop): take profit when the gap
                    # closes to within convergence_tol of the sharp price; hold a losing position while the
                    # benchmark still backs it; cut only on a WIDE hard backstop (≥ MM's 0.20).
                    external_stale_s=float(xm_cfg.get("external_stale_s", 30.0)),
                    convergence_tol=float(xm_cfg.get("exit_convergence_tol", 0.01)),
                    exit_hard_stop_pct=float(xm_cfg.get("exit_hard_stop_pct", 0.25)),
                    exit_support_conviction=float(xm_cfg.get("exit_support_conviction", 0.30)),
                    thesis_reversal_tol=float(xm_cfg.get("exit_thesis_reversal_tol", 0.05)),
                    # Wikipedia SECONDARY benchmark (coarse, ~71 Senate/Governor seats Polymarket doesn't
                    # cover). Flag-gated OFF by default → fully inert until flipped on; Polymarket stays
                    # PRIMARY on overlap. The provider self-fetches the SIG seat titles (one static call).
                    wiki_enabled=bool(xm_cfg.get("wiki_enabled", False)),
                    # Concurrent Polymarket sweep bound — lets ~119 races refresh in ~1s (was sequential,
                    # ~10s) so poll_interval_s can drop toward 1. Probed safe at 20 (0×429 live).
                    poly_concurrency=int(xm_cfg.get("poll_concurrency", 20)))
                # min_divergence / target_gap are tunable from config (the edge is validated vs the sharp
                # Polymarket benchmark, so a smaller min gap still captures real edge); fall back to the
                # strategy defaults when a key is absent.
                _dc_defaults = DivergenceConfig()
                dcfg = DivergenceConfig(
                    cadence_s=float(xm_cfg.get("cadence_s", 30.0)),
                    min_divergence=float(xm_cfg.get("min_divergence", _dc_defaults.min_divergence)),
                    target_gap=float(xm_cfg.get("target_gap", _dc_defaults.target_gap)),
                    # conviction weighting: confident benchmark → smaller threshold + conviction-scaled size,
                    # always floored at round-trip cost. sig_stale_s: how old a SIG price this slow edge
                    # tolerates (the lag is the edge; the sharp side stays fresh; order is a protective limit).
                    conviction_weighting=bool(xm_cfg.get("conviction_weighting", _dc_defaults.conviction_weighting)),
                    conviction_cost_floor=float(xm_cfg.get("conviction_cost_floor", _dc_defaults.conviction_cost_floor)),
                    sig_stale_s=float(xm_cfg.get("sig_stale_s", _dc_defaults.sig_stale_s)),
                    entry_mode=str(xm_cfg.get("entry_mode", _dc_defaults.entry_mode)),
                    maker_entry_timeout_s=float(xm_cfg.get("maker_entry_timeout_s", _dc_defaults.maker_entry_timeout_s)),
                    # tick / price band from the shared risk config so the snapped limit price matches the
                    # gate's grid exactly (the gate rejects an off-tick price).
                    tick=float((cfg.raw.get("risk", {}) or {}).get("tick", _dc_defaults.tick)),
                    price_min=float((cfg.raw.get("risk", {}) or {}).get("price_min", _dc_defaults.price_min)),
                    price_max=float((cfg.raw.get("risk", {}) or {}).get("price_max", _dc_defaults.price_max)))
                xm_strategy = CrossMarketDivergenceStrategy(
                    mapping=xm_mapping,
                    sig_price_provider=xm_provider.sig_price_provider,
                    external_provider=xm_provider.external_provider,   # RAW; resolver owns the flip
                    config=dcfg, sig_book_provider=xm_provider.sig_book)
                # Wrap so every emitted signal is logged (race, SIG price, aligned external price AFTER
                # the resolver's single flip, divergence, direction, size, skepticism) — observability + a
                # live polarity check (inverted race aligned ≈ 0.925, not 0.075). Transparent delegate.
                # leg_shadow_mode=shadow also records each signal's leg type (NO-only filter research).
                _leg_shadow = str(xm_cfg.get("leg_shadow_mode", "off")).lower() == "shadow"
                xm_strategy = LoggingStrategyWrapper(
                    xm_strategy,
                    leg_record_path=(str(xm_cfg.get("leg_shadow_path", "data/leg_shadow/signals.jsonl"))
                                     if _leg_shadow else None))
                orch.register_strategy(xm_strategy)
                registered.append(xm_strategy)
                # Route cross-market positions through the THESIS-based exit (benchmark-aware hold +
                # convergence take-profit + WIDE backstop) instead of the default price stop; MM and other
                # directional positions are not owned by this override, so they keep their own price stop.
                orch.register_directional_exit_override(xm_provider.exit_decision)
                # MM ↔ cross overlap: MM keeps quoting cross races, but stands aside (and has its resting
                # orders cancelled) on any race where the venue position exceeds MM's own inventory cap —
                # there MM would read cross's bet as its own over-cap inventory and sell it down.
                # Mode: market_making.cross_overlap = dynamic (default) | exclude | allow.
                mm.set_cross_owned(xm_provider.active_sig_exchange_ids())
                orch.register_mm_conflict_source(mm.conflicted_exchanges)
                mm.set_sharp_provider(xm_provider.aligned_benchmark)   # sharp-anchored quoting (mode-gated)
                log.info("mm_cross_overlap_wired", mode=mm._cross_overlap,
                         cross_races=len(xm_provider.active_sig_exchange_ids()))
                # EXCLUSIVE RACES: order_flow and dutch_book never trade cross races. There the cross exit override
                # sells every held position on convergence (order_flow / dutch fills included) and order_flow's
                # fade-sells net cross's own positions -> buy/sell churn (2026-10-08 review). Flag-gated, default on.
                if bool(xm_cfg.get("exclusive_races", True)):
                    _xr = set(xm_provider.active_sig_exchange_ids())
                    if of_strategy is not None:
                        _before = list(getattr(of_strategy, "_exchange_ids", []))
                        of_strategy._exchange_ids = [e for e in _before if str(e) not in _xr]
                        log.info("order_flow_cross_races_excluded", before=len(_before),
                                 after=len(of_strategy._exchange_ids))
                    if db_strategy is not None and hasattr(db_strategy, "set_excluded"):
                        db_strategy.set_excluded(_xr)
                        log.info("dutch_book_cross_races_excluded", races=len(_xr))
                log.info("cross_market_registered", active_races=len(xm_provider.active_sig_exchange_ids()),
                         sig_exchange_ids=xm_provider.active_sig_exchange_ids(),
                         poll_interval_s=float(xm_cfg.get("poll_interval_s", 15.0)))
            except Exception as exc:  # noqa: BLE001 — never let wiring block the mechanical floor
                log.error("cross_market_registration_failed", error=type(exc).__name__, msg=str(exc)[:200])
                xm_strategy = None
                xm_provider = None
        else:
            log.info("cross_market_not_registered", reason="cross_market.enabled false or not live")

        # ---- stale-quote sniper (SIGNAL) — take other bots' stale SIG quotes mispriced vs the sharp,
        #      marketable, before they re-quote. Reuses xm_provider's aligned_benchmark. ----
        from sig.engine.live_strategies import CoreSniperStrategy

        sniper_strategy = None
        sn_cfg = (cfg.raw.get("sniper", {}) or {})
        if bool(sn_cfg.get("enabled", False)) and xm_provider is not None:
            sniper_strategy = CoreSniperStrategy(store=store, config=cfg.raw, xm_provider=xm_provider,
                                                 equity=equity)
            orch.register_strategy(sniper_strategy)
            registered.append(sniper_strategy)
            log.info("sniper_registered", markets=len(xm_provider.active_sig_exchange_ids()),
                     min_edge=sn_cfg.get("min_edge", 0.03))
        else:
            log.info("sniper_not_registered", reason="sniper.enabled false or no cross-market provider")

        # ---- book-pressure (SIGNAL, SHADOW by default) — queue-imbalance on the bounded L2 book set;
        #      needs no sharp, covers pure-SIG races. place=false ⇒ records would-be signals only. ----
        from sig.engine.live_strategies import CoreBookPressureStrategy

        bp_strategy = None
        bp_cfg = (cfg.raw.get("book_pressure", {}) or {})
        if bool(bp_cfg.get("enabled", False)):
            bp_strategy = CoreBookPressureStrategy(store=store, config=cfg.raw)
            orch.register_strategy(bp_strategy)
            registered.append(bp_strategy)
            log.info("book_pressure_registered", place=bool(bp_cfg.get("place", False)),
                     min_imbalance=bp_cfg.get("min_imbalance", 0.33), record=bool(bp_cfg.get("record", True)))
        else:
            log.info("book_pressure_not_registered", reason="book_pressure.enabled false")

        # ---- event-trigger (TAIL) — all-237 SIG abnormal-move (from the WS store window) × GDELT news,
        #      behind event_trigger.enabled. Reads price MOVES from the shared store (Option A), NOT
        #      /price-history × 237; hybrid-enriches only firing candidates; own poller task below. ----
        et_provider = None
        et_strategy = None
        et_cfg = (cfg.raw.get("event_trigger", {}) or {})
        if mode == "LIVE" and client is not None and bool(et_cfg.get("enabled", False)):
            try:
                from sig.engine.event_trigger_provider import EventTriggerProvider
                from strategies.event_trigger.strategy import EventTriggerStrategy
                from signals.event_trigger import EventTriggerConfig

                et_markets = markets_all_retrying(client)        # titles → race keys (all parseable races)
                et_provider = EventTriggerProvider(
                    store=store, markets=et_markets, sig_client=client,
                    # reuse cross-market's SIG-aligned sharp price where a race overlaps Polymarket (no new poll)
                    sharp_benchmark=(xm_provider.aligned_benchmark if xm_provider is not None else None),
                    window=int(et_cfg.get("window", 14)), min_candles=int(et_cfg.get("min_candles", 6)),
                    move_mult=float(et_cfg.get("move_mult", 3.0)), min_move=float(et_cfg.get("min_move", 0.03)),
                    enrich_tradecount=bool(et_cfg.get("enrich_tradecount", True)),
                    enrich_resolution=str(et_cfg.get("enrich_resolution", "5m")),
                    news_rate_limit_s=float(et_cfg.get("news_rate_limit_s", 5.0)),
                    news_mult=float(et_cfg.get("news_mult", 3.0)))
                _etc_defaults = EventTriggerConfig()
                etc = EventTriggerConfig(
                    enabled=True, tier=str(et_cfg.get("tier", "TAIL")),
                    min_confidence=float(et_cfg.get("min_confidence", _etc_defaults.min_confidence)),
                    min_sigma=float(et_cfg.get("min_sigma", _etc_defaults.min_sigma)),
                    max_sigma=float(et_cfg.get("max_sigma", _etc_defaults.max_sigma)))
                et_strategy = EventTriggerStrategy(
                    abnormal_move_provider=et_provider.abnormal_move_provider,
                    news_provider=et_provider.news_provider,
                    sig_price_provider=et_provider.sig_price_provider,
                    exchange_to_race_key=et_provider.exchange_to_race_key,
                    sharp_provider=et_provider.sharp_provider,
                    config=etc, cadence_s=float(et_cfg.get("cadence_s", 300.0)))
                orch.register_strategy(et_strategy)
                registered.append(et_strategy)
                log.info("event_trigger_registered", races=len(et_provider._queries),
                         markets=len(et_provider._ex_race),
                         sharp=bool(xm_provider is not None), tier=etc.tier)
            except Exception as exc:  # noqa: BLE001 — never let wiring block the mechanical floor
                log.error("event_trigger_registration_failed", error=type(exc).__name__, msg=str(exc)[:200])
                et_provider = None
                et_strategy = None
        else:
            log.info("event_trigger_not_registered", reason="event_trigger.enabled false or not live")

        # ---- startup HARD GATE ----
        skip = (mode != "LIVE")
        ok = await orch.startup(skip_credential_check=skip, skip_eligibility_check=skip)
        if not ok:
            log.error("startup_gate_failed", note="hard gate blocked start — NOT trading")
            return 2
        log.info("startup_gate_passed", mode=mode)

        await allocator.refresh()
        mm.set_prices(await store.read_prices())
        if mode == "LIVE":
            await _cancel_orphan_transient_orders(broker=broker, store=store)

        notify_ready()

        # ---- bounded SIM smoke path ----
        if args.max_cycles and args.max_cycles > 0:
            # Deterministic smoke: drop cadence so every strategy is due each hammered cycle
            # (real runs keep config cadence via run_forever's timed ticks).
            for s in registered:
                try:
                    s.cadence_s = 0.0
                except Exception:
                    pass
            for i in range(args.max_cycles):
                mm.set_prices(await store.read_prices())
                await allocator.refresh()
                await orch._run_cycle()
                notify_watchdog()
                placed = len(getattr(broker, "placed", []) or [])
                log.info("sim_cycle", cycle=i + 1, broker_placed_total=placed)
            log.info("sim_run_complete", cycles=args.max_cycles,
                     broker_placed_total=len(getattr(broker, "placed", []) or []))
            return 0

        # ---- live forever path ----
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        _install_signal_handlers(loop, stop_event)

        pump = asyncio.create_task(_price_and_watchdog_pump(
            store=store, mm=mm, client=(client if (mode == "LIVE" and args.self_feed) else None),
            exchange_ids=exchange_ids, interval_s=float(cfg.feed.price_stale_after_s) / 3.0 or 5.0,
            max_ids_per_call=int(cfg.feed.snapshot_max_ids_per_call),
            order_flow=of_strategy, mr_taker=mr_strategy, dutch_book=db_strategy,
            sniper=sniper_strategy,
            equity_fn=(getattr(broker, "last_equity", None) if mode == "LIVE" else None),
        ), name="price_watchdog_pump")

        # Order-book DEPTH on its own task: slow by nature (~20 serial venue reads), so it must not
        # sit in front of the MM's price refresh or the watchdog pet. See _orderbook_pump.
        # The sniper also consumes this L2 depth (optional — it ranks/sizes the stale-ladder sweep where a
        # book is available; markets without one still snipe on NBBO + sharp). So poll if EITHER consumer
        # is live, over the same bounded liquidity-picked set.
        _ob_consumer = of_strategy is not None or sniper_strategy is not None or bp_strategy is not None
        ob_pump = asyncio.create_task(_orderbook_pump(
            order_flow=of_strategy, sniper=sniper_strategy, book_pressure=bp_strategy,
            of_client=(client if (mode == "LIVE" and _ob_consumer) else None),
            # poll ONLY the liquidity-picked markets (order_flow.markets), not the full discovered board
            exchange_ids=[str(x) for x in (of_cfg.get("markets") or exchange_ids)],
            interval_s=float(of_cfg.get("book_poll_interval_s", 30.0)),
            of_max_markets=int(of_cfg.get("max_markets", 20)),
        ), name="orderbook_pump")

        # cross-market external poller (Polymarket) — keeps the provider caches fresh for the strategy.
        xm_poller = None
        if xm_provider is not None:
            try:
                await xm_provider.refresh()   # prime once so the first cycles already have data
            except Exception:  # noqa: BLE001
                pass
            xm_poller = asyncio.create_task(xm_provider.run_poller(
                stop_event=stop_event, interval_s=float(xm_cfg.get("poll_interval_s", 15.0)),
                min_interval_s=float(xm_cfg.get("poll_min_interval_s", 1.0))),
                name="cross_market_poller")

        # event-trigger poller — its OWN task (never the price pump): samples the store price window and
        # advances the paced GDELT sweep. A slow GDELT fetch here can never starve MM's price freshness.
        et_poller = None
        if et_provider is not None:
            et_poller = asyncio.create_task(et_provider.run_poller(
                stop_event=stop_event, price_interval_s=float(et_cfg.get("price_interval_s", 20.0))),
                name="event_trigger_poller")

        # position resync — pin the local position store to venue truth so the risk gate's deploy caps
        # never drift into falsely tripping no_room (the believed-state inflation that idled ~$50k). LIVE
        # only; piggybacks the broker's 20s TTL-cached positions read, so it adds ~no 429 load.
        resync_task = None
        if mode == "LIVE":
            resync_task = asyncio.create_task(_position_resync_loop(
                store=store, broker=broker, stop_event=stop_event,
                interval_s=float(cfg.loops.get("position_resync_interval_s", 60.0))),
                name="position_resync")

        # rolling-buffer persistence — restore the mid histories a restart would otherwise wipe (mr-skew's
        # 60-min window especially), then snapshot them periodically. Keyed by strategy name in state_meta.
        persistables = {n: s for n, s in (("market_making", mm), ("order_flow", of_strategy))
                        if s is not None and hasattr(s, "snapshot")}
        if persistables:
            await _restore_persistables(store, persistables)
        snapshot_task = asyncio.create_task(_state_snapshot_loop(
            store=store, persistables=persistables, stop_event=stop_event,
            interval_s=float(cfg.loops.get("state_snapshot_interval_s", 30.0))),
            name="state_snapshot") if persistables else None

        # config hot-reload — apply overlay edits (gate caps, sniper tunables) WITHOUT a restart, so
        # tuning no longer wipes live state (mr_skew buffer, caches) or churns the trader.
        reload_task = asyncio.create_task(_config_reload_loop(
            orch=orch, strategies={"sniper": sniper_strategy, "book_pressure": bp_strategy},
            stop_event=stop_event, interval_s=float(cfg.loops.get("config_reload_interval_s", 10.0))),
            name="config_reload")

        # forward mid-history recorder — self-collected time-series (venue keeps none beyond ~24h); the
        # substrate for validating shadow signals + measuring realized edge. LIVE; local-only, ~no 429 load.
        midhist_task = None
        if mode == "LIVE":
            midhist_task = asyncio.create_task(_mid_history_loop(
                store=store, stop_event=stop_event,
                interval_s=float(cfg.loops.get("mid_history_interval_s", 60.0)),
                retention_days=float(cfg.loops.get("mid_history_retention_days", 14.0))),
                name="mid_history")

        # sharp-history recorder — durable per-minute Polymarket benchmark per cross race (backtest substrate).
        sharphist_task = None
        if mode == "LIVE" and xm_provider is not None:
            sharphist_task = asyncio.create_task(_sharp_history_loop(
                store=store, xm_provider=xm_provider, stop_event=stop_event,
                interval_s=float(cfg.loops.get("sharp_history_interval_s", 60.0)),
                retention_days=float(cfg.loops.get("sharp_history_retention_days", 45.0))),
                name="sharp_history")

        # capital rotation (shadow-first) — records would-be swaps of converged holdings into bigger gaps.
        rotation_task = None
        if mode == "LIVE" and xm_provider is not None:
            rotation_task = asyncio.create_task(_rotation_loop(
                xm_provider=xm_provider, broker=broker, cfg_raw=cfg.raw, stop_event=stop_event),
                name="rotation")

        # capital rotation v2 (shadow, beside v1): virtual book + 60-min gap persistence + execution prices.
        rotation_v2_task = None
        if mode == "LIVE" and xm_provider is not None:
            rotation_v2_task = asyncio.create_task(_rotation_v2_loop(
                xm_provider=xm_provider, broker=broker, cfg_raw=cfg.raw, stop_event=stop_event),
                name="rotation_v2")

        # election-night race-call play — dry-run shadow (records would-be buys; no orders).
        election_task = election_live_task = None
        if mode == "LIVE" and xm_provider is not None:
            _en_pairs = [(race, legs["dem"], legs["rep"]) for race, legs in _race_legs.items()
                         if "dem" in legs and "rep" in legs]
            election_task = asyncio.create_task(_election_night_loop(
                store=store, xm_provider=xm_provider, pairs=_en_pairs, cfg_raw=cfg.raw, stop_event=stop_event),
                name="election_night")
            election_live_task = asyncio.create_task(_election_night_live_loop(
                store=store, broker=broker, orch=orch, xm_provider=xm_provider, pairs=_en_pairs, cfg_raw=cfg.raw,
                stop_event=stop_event), name="election_night_live")

        # fair-band maker LIVE pilot — exclusive markets, hard carve-out, own write cap (fair_band_live_mode: live).
        fair_band_live_task = None
        if mode == "LIVE" and xm_provider is not None:
            fair_band_live_task = asyncio.create_task(_fair_band_live_loop(
                store=store, broker=broker, orch=orch, xm_provider=xm_provider, cfg_raw=cfg.raw,
                stop_event=stop_event, exclude=set((strategy_contracts or {}).values())), name="fair_band_live")

        # fair-band maker shadow — join-only quotes at fair +- w on balanced/liquid markets (record-only).
        fair_band_task = None
        if mode == "LIVE" and xm_provider is not None:
            fair_band_task = asyncio.create_task(_fair_band_loop(
                store=store, xm_provider=xm_provider, cfg_raw=cfg.raw, stop_event=stop_event), name="fair_band")

        # Kalshi second-benchmark shadow — party-verified Kalshi quotes per mapped SIG leg (record-only).
        kalshi_task = None
        if mode == "LIVE":
            kalshi_task = asyncio.create_task(_kalshi_shadow_loop(store=store, cfg_raw=cfg.raw, stop_event=stop_event),
                                              name="kalshi_shadow")

        # maker-entry shadow — resting touch quotes on the Polymarket-favoured side (scored against the tape later).
        maker_task = None
        if mode == "LIVE" and xm_provider is not None:
            maker_task = asyncio.create_task(_maker_shadow_loop(
                xm_provider=xm_provider, cfg_raw=cfg.raw, stop_event=stop_event), name="maker_shadow")
        # fresh-dislocation reversion shadow (sig/engine/fresh_gap.py) — record-only.
        fresh_gap_task = fresh_gap_t2_task = None
        if mode == "LIVE" and xm_provider is not None:
            fresh_gap_task = asyncio.create_task(_fresh_gap_loop(
                store=store, xm_provider=xm_provider, cfg_raw=cfg.raw, stop_event=stop_event), name="fresh_gap")
            fresh_gap_t2_task = asyncio.create_task(_fresh_gap_loop(
                store=store, xm_provider=xm_provider, cfg_raw=cfg.raw, stop_event=stop_event, prefix="fresh_gap_t2"),
                name="fresh_gap_t2")
        # poly-led lag shadow (sig/engine/fresh_gap.poly_lead_signal) - record-only, 1h/3h/6h lookbacks side by side.
        poly_lead_task = None
        if mode == "LIVE" and xm_provider is not None:
            poly_lead_task = asyncio.create_task(_poly_lead_loop(
                store=store, xm_provider=xm_provider, cfg_raw=cfg.raw, stop_event=stop_event), name="poly_lead")
        # capital ROTATION live trader - OFF unless rotation_live_mode: live (one swap at a time, isolated).
        rotation_live_task = None
        if mode == "LIVE" and xm_provider is not None:
            rotation_live_task = asyncio.create_task(_rotation_live_loop(
                store=store, broker=broker, orch=orch, xm_provider=xm_provider, cfg_raw=cfg.raw,
                stop_event=stop_event), name="rotation_live")
        # fresh-dislocation reversion LIVE pilot - OFF unless fresh_gap_live_mode: live (exclusive markets, carve-out).
        fresh_gap_live_task = None
        if mode == "LIVE" and xm_provider is not None:
            fresh_gap_live_task = asyncio.create_task(_fresh_gap_live_loop(
                store=store, broker=broker, orch=orch, xm_provider=xm_provider, cfg_raw=cfg.raw,
                stop_event=stop_event, exclude=set((strategy_contracts or {}).values())), name="fresh_gap_live")

        # D+R parity reversion (shadow) — records would-be NO-pair entries/exits from fresh cached quotes.
        parity_task = parity_live_task = None
        if mode == "LIVE" and xm_provider is not None:
            _pairs = [(race, legs["dem"], legs["rep"]) for race, legs in _race_legs.items()
                      if "dem" in legs and "rep" in legs]
            parity_task = asyncio.create_task(_parity_loop(
                xm_provider=xm_provider, pairs=_pairs, cfg_raw=cfg.raw, stop_event=stop_event),
                name="parity_shadow")
            parity_live_task = asyncio.create_task(_parity_live_loop(
                store=store, broker=broker, orch=orch, xm_provider=xm_provider, pairs=_pairs, cfg_raw=cfg.raw,
                stop_event=stop_event), name="parity_live")

        # SIG public trade tape (record-only, slow) — loops.trade_tape_mode: on|off (default off).
        tape_task = None
        if mode == "LIVE" and client is not None and str(cfg.loops.get("trade_tape_mode", "off")).lower() == "on":
            tape_task = asyncio.create_task(_trade_tape_loop(
                store=store, client=client, exchange_ids=exchange_ids, stop_event=stop_event,
                reads_per_min=float(cfg.loops.get("trade_tape_reads_per_min", 4.0)),
                retention_days=float(cfg.loops.get("trade_tape_retention_days", 30.0))),
                name="trade_tape")

        # NAV history — the honest P&L-per-day series (15-min samples by default).
        navhist_task = None
        if mode == "LIVE" and client is not None:
            navhist_task = asyncio.create_task(_nav_history_loop(
                store=store, broker=broker, client=client, stop_event=stop_event,
                interval_s=float(cfg.loops.get("nav_history_interval_s", 900.0)), slug=slug,
                xm_provider=xm_provider),
                name="nav_history")

        # fill ingestion — cumulative strategy_fills ledger (venue only returns the rolling last ~200).
        fillingest_task = None
        if mode == "LIVE":
            fillingest_task = asyncio.create_task(_fill_ingest_loop(
                store=store, client=client, stop_event=stop_event, broker=broker,
                interval_s=float(cfg.loops.get("fill_ingest_interval_s", 60.0))),
                name="fill_ingest")

        # attribution flush — drain the broker's (venue_order_id -> strategy) buffer to the durable
        # order_attribution table so the fill ledger can be tied to a strategy (the ~77%-UNATTRIBUTED fix).
        attrflush_task = None
        if mode == "LIVE":
            attrflush_task = asyncio.create_task(_attribution_flush_loop(
                store=store, broker=broker, stop_event=stop_event,
                interval_s=float(cfg.loops.get("attribution_flush_interval_s", 30.0))),
                name="attribution_flush")

        # MM shadow research — vol-aware spread + tape-toxicity, RECORD-ONLY (never touches a live quote).
        # Validate in shadow before any live MM change. LIVE only; vol part is free, tape part gated off.
        mmshadow_task = None
        if mode == "LIVE":
            mmshadow_task = asyncio.create_task(_mm_shadow_loop(
                store=store, client=client, stop_event=stop_event,
                exchange_ids=[str(x) for x in (of_cfg.get("markets") or exchange_ids)],
                cfg=(cfg.raw.get("mm_shadow", {}) or {})),
                name="mm_shadow")

        run_task = asyncio.create_task(orch.run_forever(), name="orch_run_forever")

        try:
            stop_wait = asyncio.create_task(stop_event.wait(), name="stop_wait")
            done, _pending = await asyncio.wait(
                {run_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if run_task in done:
                # run_forever returned on its own (shouldn't, but surface it)
                log.warning("run_forever_returned", note="loop exited unexpectedly")
        finally:
            notify_stopping()
            orch.halt("shutdown")  # halt-but-hold: stop new orders, keep positions
            if persistables:       # clean final flush so a graceful restart loses ~no history
                try:
                    n = await _snapshot_persistables(store, persistables)
                    log.info("state_snapshot_final", saved=n)
                except Exception:  # noqa: BLE001
                    pass
            _tasks = [t for t in (run_task, pump, ob_pump, xm_poller, et_poller, resync_task, reload_task, snapshot_task, midhist_task, fillingest_task, attrflush_task, mmshadow_task, sharphist_task, navhist_task, rotation_task, parity_task, tape_task, rotation_v2_task, parity_live_task, maker_task, kalshi_task, fair_band_task, election_task, election_live_task, fair_band_live_task, fresh_gap_task, fresh_gap_t2_task, poly_lead_task, fresh_gap_live_task, rotation_live_task) if t is not None]
            for t in _tasks:
                t.cancel()
            for t in _tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        return 0
    finally:
        try:
            await store.close()
        except Exception:
            pass


async def _account_equity(client) -> float | None:
    """Best-effort venue equity read (sync client -> thread)."""
    try:
        d = await asyncio.to_thread(client.account)
    except Exception:
        return None
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        d = d["data"]
    if not isinstance(d, dict):
        return None
    for k in ("equity", "portfolioValue", "totalValue", "netLiquidation", "balance"):
        v = d.get(k)
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


async def _price_and_watchdog_pump(*, store, mm, client, exchange_ids, interval_s: float,
                                   max_ids_per_call: int, order_flow=None, mr_taker=None,
                                   dutch_book=None, sniper=None, equity_fn=None) -> None:
    """Keep MM prices warm from StateStore every tick, and pet the systemd watchdog. If `client` is
    set (--self-feed), also REST-poll prices into StateStore (single-process mode); otherwise the
    separate `feed` process is the price writer and this only reads.

    If an `order_flow` strategy is wired, also feed it the latest prices.

    This loop must stay FAST. It is the only thing that refreshes the MM's price snapshot, and the MM
    quotes only markets whose row is fresher than market_making.max_quote_age_s — so every second this
    loop is blocked is added to the apparent age of EVERY price the MM sees. It also pets the systemd
    watchdog. Order-BOOK depth is therefore polled by a SEPARATE task (`_orderbook_pump`): depth is
    ~20 serial venue reads at ~4-8s each, which blew this loop's period out to minutes, froze the MM's
    snapshot (observed: quotable=0, every row apparently >10min old) and tripped the dead-man watchdog
    (observed: age_seconds=49.9 against a 10s threshold)."""
    from infra.systemd_notify import notify_watchdog
    interval_s = max(1.0, float(interval_s))
    import time as _time
    last_tick = _time.monotonic()
    worst_gap = 0.0
    while True:
        try:
            # STARVATION DETECTOR. This loop is the only thing refreshing the MM's price snapshot,
            # so any gap here is added to the apparent age of EVERY price the MM sees. If the gap far
            # exceeds interval_s the event loop is being held by something else (CPU-bound work in a
            # cycle, or a sync call), and the fix belongs THERE, not in the freshness cutoff.
            now_m = _time.monotonic()
            gap = now_m - last_tick
            last_tick = now_m
            if gap > max(2.0 * interval_s, interval_s + 2.0):
                worst_gap = max(worst_gap, gap)
                log.warning("price_pump_starved", gap_s=round(gap, 1), target_s=interval_s,
                            worst_s=round(worst_gap, 1),
                            note="event loop held elsewhere; MM prices age by exactly this much")
            if client is not None:
                from sig.feed.rest_poller import poll_prices_once
                await poll_prices_once(store=store, client=client, exchange_ids=exchange_ids,
                                       max_ids_per_call=max_ids_per_call)
            prices = await store.read_prices()
            mm.set_prices(prices)
            # LIVE NAV → capital-%-sized strategies. They were built with the startup /account `balance`,
            # which is FREE CASH (not NAV) and was never refreshed: after a restart at $2.3k cash the MM sized
            # every quote as if the account were $2.3k (~$3 quotes, 38× too small).
            if equity_fn is not None:
                try:
                    nav = equity_fn()
                    if nav and float(nav) > 0:
                        mm.set_equity(float(nav))
                        if dutch_book is not None:
                            dutch_book.set_equity(float(nav))
                except Exception:  # noqa: BLE001 — never let a NAV read break the pump
                    pass
            if order_flow is not None:
                order_flow.set_prices(prices)
            if mr_taker is not None:
                mr_taker.set_prices(prices)
            if dutch_book is not None:
                dutch_book.set_prices(prices)
            if sniper is not None:
                sniper.set_prices(prices)
            notify_watchdog()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("price_pump_iteration_error", error=type(exc).__name__, msg=str(exc)[:160])
        await asyncio.sleep(interval_s)


async def _orderbook_pump(*, order_flow, of_client, exchange_ids, interval_s: float,
                          of_max_markets: int = 0, sniper=None, book_pressure=None) -> None:
    """Feed order-flow its order-BOOK depth on its OWN cadence (depth is not in the prices table).

    Deliberately separate from `_price_and_watchdog_pump`: this does ~`of_max_markets` serial venue
    reads at ~4-8s each, so it is slow by nature and must never sit in front of the MM's price
    refresh or the watchdog pet. Capped at `of_max_markets` ids and fails soft (a bad id → no book →
    that market is simply skipped), so it can never take the loop down or blow the 100-reads/min
    budget."""
    of_ids = [str(e) for e in (exchange_ids or [])][: max(0, int(of_max_markets))]
    if of_client is None or not of_ids or (order_flow is None and sniper is None and book_pressure is None):
        return
    interval_s = max(1.0, float(interval_s))
    while True:
        try:
            books = await _poll_orderbooks(of_client, of_ids)
            if books:
                if order_flow is not None:
                    order_flow.set_books(books)
                if sniper is not None:
                    sniper.set_books(books)
                if book_pressure is not None:
                    book_pressure.set_books(books)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("orderbook_pump_iteration_error", error=type(exc).__name__, msg=str(exc)[:160])
        await asyncio.sleep(interval_s)


async def _position_resync_loop(*, store, broker, stop_event, interval_s: float) -> None:
    """Periodically overwrite the LOCAL position store with venue truth, so the risk gate (which sizes
    its deploy/per-market room off this store) can never drift into falsely tripping no_room.

    WHY THIS EXISTS: the per-fill venue-truth rewrite + startup reconcile both fail under 429 pressure,
    and the sniper's both-sides takes get NETTED at the venue (buy-NO cancels held-YES) while the local
    store keeps ADDING — so the believed-state inflates (observed 2.3x: $99k local vs $49k venue), the
    gate thinks it is ~fully deployed, and EVERY new entry is rejected `no_room_to_add_exposure` with
    ~$50k of real cash idle. This loop keeps the store pinned to truth.

    SAFETY: `get_positions_valued` uses the broker's 20s TTL-cached positions read (no extra 429 load),
    and we act ONLY on a NON-EMPTY read — an empty list is almost certainly a failed/rate-limited fetch
    (we always hold ~145 positions mid-tournament), and zeroing the whole book on a transient miss would
    be catastrophic. venue `quantity` is signed (- = short YES), matching the store's convention."""
    if broker is None or store is None or not hasattr(broker, "get_positions_valued"):
        return
    interval_s = max(20.0, float(interval_s))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            valued = await broker.get_positions_valued()
            if valued:                      # non-empty = trusted truth; empty = skip (never zero the book)
                venue_ids: set[str] = set()
                for p in valued:
                    ex = str(p.get("exchange_id"))
                    if not ex or ex == "None":
                        continue
                    venue_ids.add(ex)
                    await store.upsert_position(ex, quantity=float(p.get("quantity", 0.0) or 0.0),
                                                avg_price=float(p.get("avg_cost", 0.0) or 0.0))
                local = await store.read_positions()
                zeroed = 0
                for ex, row in (local or {}).items():
                    if str(ex) not in venue_ids and abs(float(row.get("quantity", 0) or 0)) > 0:
                        await store.upsert_position(str(ex), quantity=0.0, avg_price=0.0)
                        zeroed += 1
                log.info("position_resync", venue=len(valued), phantoms_zeroed=zeroed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad resync must never kill the loop
            log.warning("position_resync_failed", error=type(exc).__name__, msg=str(exc)[:160])
        await asyncio.sleep(interval_s)


async def _mid_history_loop(*, store, stop_event, interval_s: float, retention_days: float) -> None:
    """Record a periodic TIME-SERIES of per-market mids to the durable store. The venue keeps no usable
    price history beyond ~24h, so we self-collect forward — this is the substrate for validating shadow
    signals against actual forward moves and for any time-series research. Reads the latest-only `prices`
    table each tick, appends mids, and prunes to retention. Cheap: one local read + one batch write, no
    venue API calls."""
    from datetime import datetime, timezone, timedelta
    interval_s = max(15.0, float(interval_s))
    prune_every = max(1, int(3600 / interval_s))     # prune roughly hourly
    i = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        try:
            prices = await store.read_prices() or {}
            ts = datetime.now(timezone.utc).isoformat()
            rows = []
            for ex, row in prices.items():
                if not isinstance(row, dict):
                    continue
                b, a = row.get("best_bid"), row.get("best_ask")
                try:
                    if b is None or a is None:
                        continue
                    rows.append((str(ex), (float(b) + float(a)) / 2.0, ts, float(b), float(a),
                                 row.get("bid_size"), row.get("ask_size")))
                except (TypeError, ValueError):
                    continue
            if rows:
                await store.append_mid_history(rows)
            i += 1
            if retention_days > 0 and i % prune_every == 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=float(retention_days))).isoformat()
                removed = await store.prune_mid_history(cutoff)
                if removed:
                    log.info("mid_history_pruned", removed=removed)
            if i % 10 == 1:
                log.info("mid_history_recorded", markets=len(rows))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("mid_history_failed", error=type(exc).__name__, msg=str(exc)[:160])


def _book_edge(positions: list, snapshot: list) -> float | None:
    """Σ held YES-qty × (sharp − SIG mid) over positions whose race has a live sharp. Position quantity is signed
    YES (− = held NO), so a held NO with SIG above the sharp counts positive. None when nothing is mapped."""
    marks = {str(r["exchange_id"]): (float(r["sharp"]), float(r["sig_mid"]))
             for r in snapshot or [] if r.get("sharp") is not None and r.get("sig_mid") is not None}
    edge, seen = 0.0, False
    for p in positions or []:
        ex = str(p.get("exchangeId", p.get("exchange_id", "")))
        if ex in marks:
            s, m = marks[ex]
            edge += float(p.get("quantity", 0) or 0) * (s - m)
            seen = True
    return round(edge, 2) if seen else None


async def _nav_history_loop(*, store, broker, client, stop_event, interval_s: float, slug: str,
                            xm_provider=None) -> None:
    """Record NAV (cash + marked positions), unrealized P&L, position count and the venue leaderboard P&L/rank
    every interval into nav_history — the honest P&L-per-day series. Account + positions come from the
    broker's TTL caches; the only extra venue call is one leaderboard read per interval (paged to find us).
    Also records book_edge (see _book_edge) from the cross-market provider's cached sharp snapshot."""
    from datetime import datetime, timezone
    interval_s = max(60.0, float(interval_s))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            acct = await broker.get_account()
            resp = await asyncio.to_thread(broker._read_positions_raw)
            summ = (resp or {}).get("summary", {}) if isinstance(resp, dict) else {}
            npos = len((resp or {}).get("positions", []) or []) if isinstance(resp, dict) else None
            lb_pnl = lb_rank = None
            try:
                me = None
                first = await asyncio.to_thread(client.get, f"/tournaments/{slug}/leaderboard",
                                                {"limit": 50})
                rank = (first or {}).get("myRank")
                if rank:
                    off = max(int(rank) - 25, 0)
                    page = await asyncio.to_thread(client.get, f"/tournaments/{slug}/leaderboard",
                                                   {"limit": 50, "offset": off})
                    for r in (page or {}).get("leaderboard", []):
                        if r.get("rank") == rank:
                            me = r
                    lb_rank = int(rank)
                    lb_pnl = float(me["pnl"]) if me else None
            except Exception:  # noqa: BLE001 — leaderboard is optional
                pass
            book_edge = None
            if xm_provider is not None and isinstance(resp, dict):
                try:
                    book_edge = _book_edge(resp.get("positions", []) or [], xm_provider.sharp_snapshot())
                except Exception:  # noqa: BLE001 — book_edge is optional
                    book_edge = None
            await store.append_nav({
                "ts": datetime.now(timezone.utc).isoformat(), "cash": float(acct.buying_power),
                "market_value": float(summ.get("totalMarketValue", 0.0) or 0.0), "nav": float(acct.equity),
                "unrealized": float(summ.get("totalUnrealizedPnl", 0.0) or 0.0), "positions": npos,
                "lb_pnl": lb_pnl, "lb_rank": lb_rank, "book_edge": book_edge})
            log.info("nav_recorded", nav=round(float(acct.equity), 2), lb_pnl=lb_pnl, rank=lb_rank,
                     book_edge=book_edge)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("nav_history_failed", error=type(exc).__name__, msg=str(exc)[:160])
        await asyncio.sleep(interval_s)


async def _trade_tape_loop(*, store, client, exchange_ids: list, stop_event, reads_per_min: float,
                           retention_days: float) -> None:
    """Record SIG's public trade tape (GET /exchanges/{id}/trades) into trade_tape for microstructure research.
    Deliberately SLOW: one market per tick at `reads_per_min` (default 4 of the shared 100/min read budget),
    cycling through every market; each read asks only for trades since the last one recorded (resumes after a
    restart from the DB). A 429 backs the loop off for 120s. Record-only — nothing trades on it."""
    from datetime import datetime, timezone, timedelta
    from sig.sig_client import SigAPIError
    ids = [str(e) for e in exchange_ids or []]
    if not ids or client is None:
        log.info("trade_tape_not_started", markets=len(ids))
        return
    gap = 60.0 / max(0.5, float(reads_per_min))
    try:
        last = await store.latest_trade_ts()
    except Exception:  # noqa: BLE001
        last = {}
    log.info("trade_tape_started", markets=len(ids), reads_per_min=reads_per_min, lap_min=round(len(ids) * gap / 60, 1))
    i = got = truncated = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(gap)
        ex = ids[i % len(ids)]
        i += 1
        try:
            resp = await asyncio.to_thread(client.exchange_trades, ex, limit=200, since_iso=last.get(ex))
            data = (resp or {}).get("data", []) if isinstance(resp, dict) else []
            if data:
                await store.append_trades(ex, data, datetime.now(timezone.utc).isoformat())
                newest = max(str(t.get("createdAt") or "") for t in data)
                if newest:
                    last[ex] = newest
                got += len(data)
                if len(data) >= 200:
                    truncated += 1                       # more than one page since last lap: some trades missed
            if i % len(ids) == 0:
                log.info("trade_tape_lap", trades=got, truncated_markets=truncated)
                got = truncated = 0
                if retention_days > 0:
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=float(retention_days))).isoformat()
                    await store.prune_trade_tape(cutoff)
        except asyncio.CancelledError:
            raise
        except SigAPIError as exc:
            if exc.status == 429:
                log.warning("trade_tape_rate_limited_backoff", seconds=120)
                await asyncio.sleep(120.0)
            elif exc.status == 404 and len(ids) > 1:
                ids.remove(ex)                           # not an exchange id (e.g. a market id): stop polling it
                log.info("trade_tape_dropped_unknown_id", exchange=ex, remaining=len(ids))
            else:
                log.warning("trade_tape_read_failed", exchange=ex, status=exc.status)
        except Exception as exc:  # noqa: BLE001 — recorder must never affect trading
            log.warning("trade_tape_failed", exchange=ex, error=type(exc).__name__, msg=str(exc)[:160])


def _parity_quotes(pairs: list, prices: dict, now_epoch: float, max_age_s: float) -> list:
    """PairQuotes for every (race, d_ex, r_ex) whose BOTH legs have a SIG bid/ask in the shared store
    (`prices` rows: best_bid, best_ask, updated_at ISO) updated within max_age_s."""
    from datetime import datetime
    from sig.engine.parity import PairQuote

    def leg(ex):
        row = prices.get(str(ex))
        if not isinstance(row, dict) or row.get("best_bid") is None or row.get("best_ask") is None:
            return None
        try:
            ts = datetime.fromisoformat(str(row.get("updated_at")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None
        if now_epoch - ts > max_age_s:
            return None
        return float(row["best_bid"]), float(row["best_ask"])

    out = []
    for race, d_ex, r_ex in pairs:
        d, r = leg(d_ex), leg(r_ex)
        if d and r:
            out.append(PairQuote(race, str(d_ex), str(r_ex), d[0], d[1], r[0], r[1]))
    return out


async def _parity_live_loop(*, store, broker, orch, xm_provider, pairs: list, cfg_raw: dict, stop_event) -> None:
    """D+R PARITY LIVE pilot (sig/engine/parity_live.py). OFF unless cross_market.parity_live_mode == live (or it still
    owns legs from an earlier run: then it starts winding down and keeps managing them until flat). Every
    parity_live_interval_s: kill checks -> venue reads (open orders, positions, account) -> fresh cached SIG quotes ->
    trader.tick. Heart-beats the ownership lease while it holds anything."""
    import time as _time
    from sig.engine.orchestrator import _signed_qty
    from sig.engine.ownership import registry
    from sig.engine.parity_live import SID, ParityLive, ParityLiveConfig
    xm = (cfg_raw.get("cross_market", {}) or {})
    mode_live = str(xm.get("parity_live_mode", "off")).lower() == "live"
    if (not mode_live and not registry().owned_by(SID) and not registry().orders_of(SID)) or not pairs:
        log.info("parity_live_not_started", pairs=len(pairs))
        return
    cfg_ok = True
    try:
        d = ParityLiveConfig()
        lc = ParityLiveConfig(pairs=int(xm.get("parity_live_pairs", d.pairs)),
                              budget_usd=float(xm.get("parity_live_budget_usd", d.budget_usd)),
                              max_open=int(xm.get("parity_live_max_open", d.max_open)),
                              entry_edge=float(xm.get("parity_live_entry_edge", d.entry_edge)),
                              exit_pnl=float(xm.get("parity_live_exit_pnl", d.exit_pnl)),
                              max_loss_usd=float(xm.get("parity_live_max_loss_usd", d.max_loss_usd)))
    except (TypeError, ValueError) as exc:
        log.error("parity_live_bad_config_winding_down", error=str(exc)[:160])
        lc, cfg_ok = ParityLiveConfig(), False
    trader = ParityLive(broker=broker, ownership=registry(), cfg=lc, others_reserved=_others_reserved(orch, SID))
    trader.load()
    if not mode_live:
        trader.halt("mode_off_wind_down")
    if not cfg_ok:
        trader.halt("bad_config")
    ledger = getattr(orch, "ledger", None) if orch is not None else None
    if ledger is None or not hasattr(ledger, "external_reserves"):
        log.error("parity_live_no_ledger_refusing")
        trader.halt("no_ledger")
    else:
        ledger.external_reserves[SID] = trader.unused_budget          # registered BEFORE the first placement
    interval = max(15.0, float(xm.get("parity_live_interval_s", 30.0)))
    log.info("parity_live_started", pairs=lc.pairs, budget=lc.budget_usd, max_open=lc.max_open,
             entry_edge=lc.entry_edge, active=len(trader.active), halt=trader.halt_reason)
    it = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval if it else 5.0)
        it += 1
        try:
            registry().beat(SID)          # LEASE: without heartbeats exit:* strategies may trade our legs
        except Exception as exc:  # noqa: BLE001
            log.warning("parity_live_heartbeat_failed", error=type(exc).__name__)
        try:
            trader.check_kill(orchestrator_halted=bool(getattr(orch, "is_halted", False)))
            if trader.winding_down and not trader.active and not registry().owned_by(SID):
                if it % 120 == 1:
                    log.info("parity_live_idle", halt=trader.halt_reason)
                continue
            opens = await broker.get_open_orders_all()
            snaps = await broker.get_positions(fresh=True) or []
            pos = {str(getattr(p, "contract_id", "")): _signed_qty(p) for p in snaps}
            acct = await broker.get_account()
            free = float(acct.buying_power) if acct is not None else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("parity_live_read_failed", error=type(exc).__name__, msg=str(exc)[:160])
            continue
        try:
            qs = _parity_quotes(pairs, getattr(xm_provider, "_sig_prices", {}) or {}, _time.time(), lc.max_quote_age_s)
            quotes = {q.race: {"d": q.d_ex, "r": q.r_ex, "d_bid": q.d_bid, "d_ask": q.d_ask,
                               "r_bid": q.r_bid, "r_ask": q.r_ask} for q in qs}
            res = await trader.tick(quotes, pos, opens, free)
            if res.get("placed") or res.get("claimed") or res.get("closed") or res.get("place_errors") or it % 20 == 1:
                log.info("parity_live_tick", carve_out=round(trader.unused_budget(), 2), reserved=trader.reserved(),
                         halt=trader.halt_reason, quoted=len(quotes), **res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never crash the bot; next tick re-reads venue truth
            log.warning("parity_live_failed", error=type(exc).__name__, msg=str(exc)[:200])


async def _parity_loop(*, xm_provider, pairs: list, cfg_raw: dict, stop_event) -> None:
    """D+R PARITY reversion (sig/engine/parity.py). mode=shadow: every interval, decide would-be NO-pair
    entries/exits from the shared WS store SIG quotes the cross-market provider caches (no venue calls)
    and append them to jsonl. No orders. The open shadow book is rebuilt from the log on restart."""
    import json as _json
    import os as _os
    import time as _time
    from datetime import datetime, timezone
    from sig.engine.parity import open_from_events, step
    pc = (cfg_raw.get("cross_market", {}) or {})
    mode = str(pc.get("parity_mode", "off")).lower()
    if mode != "shadow" or not pairs:
        log.info("parity_not_started", mode=mode, pairs=len(pairs))
        return
    interval = max(10.0, float(pc.get("parity_interval_s", 30.0)))
    entry_edge = float(pc.get("parity_entry_edge", 0.02))
    exit_pnl = float(pc.get("parity_exit_pnl", 0.01))
    max_age = float(pc.get("parity_max_quote_age_s", 120.0))
    path = str(pc.get("parity_record_path", "data/parity_shadow/events.jsonl"))
    book = {}
    try:
        if _os.path.exists(path):
            book = open_from_events([_json.loads(l) for l in open(path, encoding="utf-8") if l.strip()])
    except Exception as exc:  # noqa: BLE001
        log.warning("parity_book_reload_failed", error=type(exc).__name__)
    log.info("parity_started", mode=mode, pairs=len(pairs), open=len(book), entry_edge=entry_edge, exit_pnl=exit_pnl)
    n = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval)
        try:
            quotes = _parity_quotes(pairs, getattr(xm_provider, "_sig_prices", {}) or {}, _time.time(), max_age)
            events = step(quotes, book, now_iso=datetime.now(timezone.utc).isoformat(),
                          entry_edge=entry_edge, exit_pnl=exit_pnl)
            if events:
                _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n".join(_json.dumps(e) for e in events) + "\n")
                for e in events:
                    log.info("parity_shadow_" + e["event"], race=e["race"], edge=e.get("entry_edge"),
                             pnl=e.get("exit_pnl"))
            n += 1
            if n % 20 == 1:
                log.info("parity_shadow_tick", quoted_pairs=len(quotes), open=len(book))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("parity_shadow_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _rotation_loop(*, xm_provider, broker, cfg_raw: dict, stop_event) -> None:
    """Cross-market CAPITAL ROTATION. mode=shadow: every interval decide whether the fully-deployed book should
    swap its most-converged holding for a much bigger new divergence (sig/engine/rotation.py) and RECORD the
    would-be swap to jsonl — no orders. (live mode is deliberately NOT implemented until shadow validates.)
    Inputs are cache reads (provider + broker TTL caches) plus one account read per interval."""
    import json as _json
    import os as _os
    import time as _time
    from datetime import datetime, timezone
    from sig.engine.rotation import Candidate, Held, decide_rotation
    xm = (cfg_raw.get("cross_market", {}) or {})
    mode = str(xm.get("rotation_mode", "off")).lower()
    if mode not in ("shadow",):
        log.info("rotation_not_started", mode=mode)
        return
    interval = max(30.0, float(xm.get("rotation_interval_s", 60.0)))
    margin = float(xm.get("rotation_margin", 0.04))
    per_hour = int(xm.get("rotation_max_per_hour", 4))
    min_val = float(xm.get("rotation_min_position_usd", 500.0))
    cap_frac = float((cfg_raw.get("capital", {}) or {}).get("max_per_market_frac", 0.05))
    cost_floor = float(xm.get("conviction_cost_floor", 0.05))
    path = str(xm.get("rotation_record_path", "data/rotation_shadow/swaps.jsonl"))
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    recent: list[float] = []
    seen: dict[tuple, float] = {}        # shadow: (sell, buy) -> last recorded; nothing executes, so the same
                                         # swap would be re-found every interval and inflate the validation
    log.info("rotation_started", mode=mode, margin=margin, per_hour=per_hour, interval_s=interval)
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval)
        try:
            acct = await broker.get_account()
            nav, cash = float(acct.equity), float(acct.buying_power)
            size = cap_frac * nav
            owned = set(xm_provider.active_sig_exchange_ids())
            prices = getattr(xm_provider, "_sig_prices", {}) or {}

            def _hs(ex):
                r = prices.get(ex) or {}
                b, a = r.get("best_bid"), r.get("best_ask")
                return max((float(a) - float(b)) / 2.0, 0.0025) if b is not None and a is not None else 0.005

            def _mid(ex):
                q = xm_provider.sig_price_provider(ex)
                return float(q[0]) if q else None

            held, held_ids = [], set()
            for p in (await broker.get_positions_valued()) or []:
                ex = str(p.get("exchange_id"))
                q = float(p.get("quantity", 0) or 0)
                if ex not in owned or abs(q) < 1:
                    continue
                held_ids.add(ex)
                sh, m = xm_provider.aligned_benchmark(ex), _mid(ex)
                if sh is None or m is None:
                    continue
                cp = float(p.get("current_price", 0) or 0)
                held.append(Held(ex, q, m, float(sh), _hs(ex), abs(q) * (cp if q > 0 else 1 - cp)))
            cands = []
            for ex in owned - held_ids:
                sh, m = xm_provider.aligned_benchmark(ex), _mid(ex)
                if sh is not None and m is not None:
                    cands.append(Candidate(ex, m, float(sh), _hs(ex)))
            now = _time.time()
            recent = [t for t in recent if now - t < 3600]
            room = max(per_hour - len(recent), 0)
            swaps = decide_rotation(held, cands, cash=cash, position_size=size, margin=margin,
                                    max_swaps=min(room, 1), cost_floor=cost_floor, min_held_value=min_val)
            for sw in swaps:
                pair = (sw.sell.exchange_id, sw.buy.exchange_id)
                if now - seen.get(pair, 0.0) < 1800:
                    continue
                seen[pair] = now
                recent.append(now)
                rec = {"ts": datetime.now(timezone.utc).isoformat(), "mode": mode, "nav": round(nav, 2),
                       "cash": round(cash, 2), "size": round(size, 2), "advantage": sw.advantage,
                       "sell": {"exchange_id": sw.sell.exchange_id, "qty": sw.sell.qty, "sig_mid": sw.sell.sig_mid,
                                "sharp": sw.sell.sharp, "remaining_gap": round(sw.sell.remaining_gap, 4),
                                "half_spread": sw.sell.half_spread, "market_value": round(sw.sell.market_value, 2)},
                       "buy": {"exchange_id": sw.buy.exchange_id, "side": sw.buy.side, "sig_mid": sw.buy.sig_mid,
                               "sharp": sw.buy.sharp, "gap": round(sw.buy.gap, 4), "half_spread": sw.buy.half_spread}}
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(_json.dumps(rec) + "\n")
                log.info("rotation_shadow_swap", sell=sw.sell.exchange_id, remaining=round(sw.sell.remaining_gap, 4),
                         buy=sw.buy.exchange_id, gap=round(sw.buy.gap, 4), advantage=sw.advantage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("rotation_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _rotation_v2_loop(*, xm_provider, broker, cfg_raw: dict, stop_event) -> None:
    """Capital ROTATION v2 shadow (sig/engine/rotation_v2.py) — runs BESIDE v1. A virtual book (each swap counted
    once, sold markets leave it), a 60-min gap-persistence rule, and real bid/ask execution prices recorded so
    tools/rotation_v2_validate.py can value each swap at the sharp vs holding. Record-only; cache reads plus the
    same account/positions reads v1 already makes (TTL-cached)."""
    import json as _json
    import os as _os
    import time as _time
    import uuid as _uuid
    from datetime import datetime, timezone
    from sig.engine.rotation import Candidate, Held
    from sig.engine.rotation_v2 import PersistenceTracker, VirtualSwap, book_from_events, decide_v2, held_side_prices
    xm = (cfg_raw.get("cross_market", {}) or {})
    if str(xm.get("rotation_v2_mode", "off")).lower() != "shadow":
        log.info("rotation_v2_not_started")
        return
    interval = max(30.0, float(xm.get("rotation_interval_s", 60.0)))
    margin = float(xm.get("rotation_margin", 0.04))
    per_hour = int(xm.get("rotation_max_per_hour", 4))
    min_val = float(xm.get("rotation_min_position_usd", 500.0))
    persist_s = float(xm.get("rotation_v2_persist_s", 3600.0))
    # retune 2026-10-09 (tools/research/rotation_replay.py): top-ups of held markets, partial sells, own margin,
    # 24h freeze instead of forever. All default OFF -> unchanged v2 behaviour.
    topup = bool(xm.get("rotation_v2_topup", False))
    partial = bool(xm.get("rotation_v2_partial", False))
    margin = float(xm.get("rotation_v2_margin", margin))
    _fz = xm.get("rotation_v2_freeze_s")
    freeze_s = float(_fz) if _fz is not None else None
    cap_frac = float((cfg_raw.get("capital", {}) or {}).get("max_per_market_frac", 0.05))
    cost_floor = float(xm.get("conviction_cost_floor", 0.05))
    path = str(xm.get("rotation_v2_record_path", "data/rotation_v2_shadow/events.jsonl"))
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    swaps: list = []
    try:
        if _os.path.exists(path):
            swaps = book_from_events([_json.loads(l) for l in open(path, encoding="utf-8") if l.strip()])
    except Exception as exc:  # noqa: BLE001
        log.warning("rotation_v2_reload_failed", error=type(exc).__name__)
    tracker = PersistenceTracker()
    recent: list[float] = []
    log.info("rotation_v2_started", persist_s=persist_s, open_virtual=len(swaps), margin=margin, topup=topup,
             partial=partial, freeze_s=freeze_s, path=path)
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval)
        try:
            acct = await broker.get_account()
            nav, cash = float(acct.equity), float(acct.buying_power)
            owned = set(xm_provider.active_sig_exchange_ids())
            prices = getattr(xm_provider, "_sig_prices", {}) or {}

            def _ba(ex):
                r = prices.get(ex) or {}
                b, a = r.get("best_bid"), r.get("best_ask")
                return (float(b), float(a)) if b is not None and a is not None and float(a) >= float(b) else None

            def _sz(ex):
                r = prices.get(ex) or {}
                try:
                    return float(r.get("bid_size") or 0.0), float(r.get("ask_size") or 0.0)
                except (TypeError, ValueError):
                    return 0.0, 0.0

            def _hs(ex):
                ba = _ba(ex)
                return max((ba[1] - ba[0]) / 2.0, 0.0025) if ba else 0.005

            now = _time.time()
            held, held_ids, cands, persisted = [], set(), [], {}
            for p in (await broker.get_positions_valued()) or []:
                ex = str(p.get("exchange_id"))
                q = float(p.get("quantity", 0) or 0)
                if ex in owned and abs(q) >= 1:
                    held_ids.add(ex)
                    q_sh, q_m = xm_provider.aligned_benchmark(ex), xm_provider.sig_price_provider(ex)
                    if q_sh is not None and q_m:
                        cp = float(p.get("current_price", 0) or 0)
                        held.append(Held(ex, q, float(q_m[0]), float(q_sh), _hs(ex),
                                         abs(q) * (cp if q > 0 else 1 - cp)))
            for ex in owned:
                q_sh, q_m = xm_provider.aligned_benchmark(ex), xm_provider.sig_price_provider(ex)
                if q_sh is None or not q_m:
                    continue
                persisted[ex] = tracker.update(ex, float(q_sh), float(q_m[0]), now, cost_floor)
                if ex not in held_ids or topup:
                    cands.append(Candidate(ex, float(q_m[0]), float(q_sh), _hs(ex)))
            recent = [t for t in recent if now - t < 3600]
            if len(recent) >= per_hour:
                continue
            for sw in decide_v2(held, cands, swaps, persisted=persisted, persist_s=persist_s, cash=cash,
                                position_size=cap_frac * nav, margin=margin, cost_floor=cost_floor,
                                min_held_value=min_val, topup=topup, freeze_s=freeze_s, now=now):
                sba, bba = _ba(sw.sell.exchange_id), _ba(sw.buy.exchange_id)
                if not sba or not bba:
                    continue
                exit_px = held_side_prices(sw.sell.qty, *sba)[0]
                entry_px = held_side_prices(sw.buy.side, *bba)[1]
                if exit_px <= 0 or entry_px <= 0:
                    continue
                proceeds = abs(sw.sell.qty) * exit_px
                sold_qty = sw.sell.qty
                if partial:
                    # size by the displayed touch on both legs and the room under the bought market's cap
                    sb, sa = _sz(sw.sell.exchange_id)
                    bb, ba_ = _sz(sw.buy.exchange_id)
                    sell_depth = sb if sw.sell.qty > 0 else sa
                    buy_depth = ba_ if sw.buy.side == "yes" else bb
                    have = next((abs(h.market_value) for h in held if h.exchange_id == sw.buy.exchange_id), 0.0)
                    room = cap_frac * nav - have
                    spend = min(proceeds, room,
                                sell_depth * exit_px if sell_depth > 0 else proceeds * 0.25,
                                buy_depth * entry_px if buy_depth > 0 else room * 0.25)
                    if spend < 200.0:
                        continue
                    proceeds = spend
                    sold_qty = (spend / exit_px) * (1 if sw.sell.qty > 0 else -1)
                new_shares = proceeds / entry_px
                rec = {"event": "swap", "swap_id": _uuid.uuid4().hex[:12],
                       "ts": datetime.now(timezone.utc).isoformat(), "nav": round(nav, 2), "cash": round(cash, 2),
                       "advantage": sw.advantage, "persisted_min": round(persisted.get(sw.buy.exchange_id, 0) / 60, 1),
                       "sell": {"exchange_id": sw.sell.exchange_id, "qty": round(sold_qty, 2), "sig_mid": sw.sell.sig_mid,
                                "sharp": sw.sell.sharp, "remaining_gap": round(sw.sell.remaining_gap, 4),
                                "bid": sba[0], "ask": sba[1], "exit_px": round(exit_px, 4),
                                "proceeds": round(proceeds, 2)},
                       "buy": {"exchange_id": sw.buy.exchange_id, "side": sw.buy.side, "sig_mid": sw.buy.sig_mid,
                               "sharp": sw.buy.sharp, "gap": round(sw.buy.gap, 4), "bid": bba[0], "ask": bba[1],
                               "entry_px": round(entry_px, 4), "new_shares": round(new_shares, 2)}}
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(_json.dumps(rec) + "\n")
                swaps.extend(book_from_events([rec]))
                recent.append(now)
                log.info("rotation_v2_swap", sell=sw.sell.exchange_id, buy=sw.buy.exchange_id,
                         advantage=sw.advantage, persisted_min=rec["persisted_min"], open_virtual=len(swaps))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("rotation_v2_failed", error=type(exc).__name__, msg=str(exc)[:160])


def _kalshi_rows(legs: list[dict], markets: dict, ts: str) -> list[dict]:
    """kalshi_history rows for every mapped leg whose Kalshi market came back with a price."""
    from signals.kalshi_client import parse_quote
    out = []
    for leg in legs:
        m = markets.get(leg.get("ticker"))
        if not m:
            continue
        q = parse_quote(m)
        if q["mid"] is None:
            continue
        out.append({"exchange_id": str(leg["exchange_id"]), "ts": ts, "ticker": leg["ticker"], "bid": q["bid"],
                    "ask": q["ask"], "mid": q["mid"], "last": q["last"], "volume_24h": q["volume_24h"]})
    return out


def _fair_band_inputs(db_path: str) -> tuple[dict, dict, dict]:
    """(median |fair - SIG mid| over 24h, $/day traded over 3 days, latest Kalshi mid <=5 min old) from the local DB."""
    import sqlite3 as _sq
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    con = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        gaps: dict[str, list] = {}
        for ex, s, m in con.execute("SELECT exchange_id, sharp, sig_mid FROM sharp_history WHERE ts >= ? AND sig_mid IS NOT NULL",
                                    ((now - timedelta(hours=24)).isoformat(),)):
            gaps.setdefault(str(ex), []).append(abs(float(s) - float(m)))
        med = {ex: sorted(v)[len(v) // 2] for ex, v in gaps.items() if v}
        vol = {str(ex): float(u or 0) / 3.0 for ex, u in con.execute(
            "SELECT exchange_id, SUM(size * price) FROM trade_tape WHERE created_at >= ? GROUP BY exchange_id",
            ((now - timedelta(days=3)).isoformat(),))}
        kal = {str(ex): float(m) for ex, m in con.execute(
            "SELECT exchange_id, mid FROM kalshi_history WHERE ts >= ? AND mid IS NOT NULL ORDER BY ts",
            ((now - timedelta(minutes=5)).isoformat(),))}
    finally:
        con.close()
    return med, vol, kal


async def _fair_band_loop(*, store, xm_provider, cfg_raw: dict, stop_event) -> None:
    """FAIR-BAND MAKER shadow (sig/engine/fair_band.py): every minute record the join-only quotes at fair +- w we WOULD
    rest on balanced/liquid markets (fair = Polymarket, Kalshi fallback), with the live book. No orders. Market tags
    refresh hourly from the local DB. Scored against the trade tape by tools/fair_band_validate.py."""
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from sig.engine.fair_band import FairBandConfig, band_quotes, classify
    xm = (cfg_raw.get("cross_market", {}) or {})
    if str(xm.get("fair_band_mode", "off")).lower() != "shadow":
        log.info("fair_band_not_started")
        return
    cfg = FairBandConfig(w=float(xm.get("fair_band_w", 0.03)))
    path = str(xm.get("fair_band_path", "data/fair_band_shadow/quotes.jsonl"))
    db_path = str(getattr(store, "_db_path", None) or "data/state.db")
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    tags, kal, n = {}, {}, 0
    log.info("fair_band_started", w=cfg.w, path=path)
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(60.0)
        try:
            if n % 60 == 0 or not tags:
                med, vol, kal = await asyncio.to_thread(_fair_band_inputs, db_path)
                tags = classify(med, vol)
            elif n % 5 == 0:
                _, _, kal = await asyncio.to_thread(_fair_band_inputs, db_path)
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            ts = datetime.now(timezone.utc).isoformat()
            lines = []
            for ex, tg in tags.items():
                p = prices.get(ex) or {}
                fair = xm_provider.aligned_benchmark(ex) if ex in set(xm_provider.active_sig_exchange_ids()) else None
                src = "poly"
                if fair is None and ex in kal:
                    fair, src = kal[ex], "kalshi"
                if fair is None or not p:
                    continue
                for q in band_quotes({"exchange_id": ex, "fair": fair, "best_bid": p.get("best_bid"),
                                      "best_ask": p.get("best_ask"), "bid_size": p.get("bid_size"),
                                      "ask_size": p.get("ask_size"), "tags": tg}, cfg):
                    lines.append(_json.dumps({"ts": ts, "src": src, **q}))
            if lines:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            n += 1
            if n % 30 == 1:
                log.info("fair_band_tick", quotes=len(lines), tagged=len(tags),
                         balanced=sum(1 for t in tags.values() if "balanced" in t))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("fair_band_failed", error=type(exc).__name__, msg=str(exc)[:160])


_TRANSIENT_STRATEGIES = ("cross_market_divergence", "trim", "fund_pilot")


async def _cancel_orphan_transient_orders(*, broker, store) -> int:
    """Startup: cancel resting orders of TRANSIENT strategies (cross entries incl. 60s maker entries, exit chase
    orders, trim, funding). OMS bundles live in memory only, so after a restart nothing tracks them: they would
    rest forever at stale prices (no timeout), cross would re-enter on top (stacking), and SIG does not reserve
    cash for them. MM quotes, dutch legs and fair_band orders are left alone (their owners manage them).
    Best-effort; never blocks startup."""
    try:
        opens = await broker.get_open_orders_all()
        if opens is None:
            log.warning("orphan_cleanup_skipped", reason="open orders read failed")
            return 0
        amap = dict(await store.read_attribution_map() or {})
        n = 0
        for o in opens:
            sid = str(amap.get(str(o.get("order_id")), ""))
            if sid in _TRANSIENT_STRATEGIES or sid.startswith("exit:"):
                res = await broker.cancel_order(o["order_id"], strategy_id=sid)
                ok = str(getattr(res, "status", "")).lower() in ("canceled", "cancelled")
                n += ok
                log.info("orphan_transient_order_cancelled" if ok else "orphan_cancel_unconfirmed",
                         order_id=o["order_id"], strategy=sid, exchange=o.get("exchange_id"))
        log.info("orphan_cleanup_done", cancelled=n, open_orders=len(opens))
        return n
    except Exception as exc:  # noqa: BLE001
        log.warning("orphan_cleanup_failed", error=type(exc).__name__, msg=str(exc)[:160])
        return 0


def _fair_band_live_pnl(db_path: str, owned: set, since_iso: str, books: dict, claimed_at=None,
                        history=None) -> float | None:
    """P&L of every fill in the trader's OWNED markets since each market's CLAIM (and the pilot start), valued at
    SIG mid. By exclusivity these are all its fills, so this does not depend on order attribution. None on read
    failure."""
    import sqlite3 as _sq
    from sig.engine.fair_band_live import fills_pnl
    if not owned:
        return 0.0
    try:
        con = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            q = ",".join("?" * len(owned))
            rows = con.execute(f"SELECT exchange_id, outcome, price, quantity, signed_qty, filled_at FROM strategy_fills "
                               f"WHERE exchange_id IN ({q}) AND filled_at >= ?", (*sorted(owned), since_iso)).fetchall()
            if history:
                # each fill must fall inside one of OUR ownership windows of that market
                win: dict = {}
                for h in history:
                    win.setdefault(str(h[0]), []).append((str(h[2]), str(h[3]) if h[3] else "9999"))
                rows = [r for r in rows if any(a <= str(r[5]) < b for a, b in win.get(str(r[0]), []))]
            elif claimed_at is not None:
                rows = [r for r in rows if str(r[5]) >= str(claimed_at(r[0]) or since_iso)]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None
    return fills_pnl([{"exchange_id": r[0], "outcome": r[1], "price": r[2], "quantity": r[3], "signed_qty": r[4]}
                      for r in rows], books)


async def _fair_band_live_loop(*, store, broker, orch, xm_provider, cfg_raw: dict, stop_event,
                               exclude: set | None = None) -> None:
    """FAIR-BAND MAKER live pilot (sig/engine/fair_band_live.py). Exclusive markets, buys only, hard budget
    carved out of the orchestrator's ledger, own write cap, loss limit + STOP file.

    Robustness (2026-10-08 failure-mode review):
      * START: cancel every order registered to us (clean slate) before anything else.
      * EACH TICK, in isolated phases: (0) kill checks that need no venue read (STOP file, orchestrator halt, last
        known losses) and, when winding down, blind cancels of every registered order; (1) venue reads;
        (2) market selection (its own try); (3) loss measures -> check_kill -> tick; (4) heartbeat (ownership lease).
        A failure in one phase never skips the kill checks.
      * STOP / CANCEL: shielded cancel of every registered order on shutdown.
      * Bad config values fall back to defaults AND force wind-down (owned markets are never left unmanaged)."""
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from sig.engine.fair_band import classify
    from sig.engine.fair_band_live import FairBandLive, FairBandLiveConfig, SID, venue_pnl
    import time as _time
    from sig.engine.orchestrator import _signed_qty
    from sig.engine.ownership import registry
    xm = (cfg_raw.get("cross_market", {}) or {})
    mode_live = str(xm.get("fair_band_live_mode", "off")).lower() == "live"
    if not mode_live and not registry().owned_by(SID) and not registry().orders_of(SID):
        log.info("fair_band_live_not_started")
        return
    cfg_ok = True
    try:
        lc = FairBandLiveConfig(w=float(xm.get("fair_band_live_w", 0.03)),
                                quote_usd=float(xm.get("fair_band_live_quote_usd", 500.0)),
                                market_cap_usd=float(xm.get("fair_band_live_market_cap_usd", 1500.0)),
                                budget_usd=float(xm.get("fair_band_live_budget_usd", 10000.0)),
                                n_markets=int(xm.get("fair_band_live_markets", 10)),
                                max_writes_per_min=int(xm.get("fair_band_live_writes_per_min", 6)),
                                max_loss_usd=float(xm.get("fair_band_live_max_loss_usd", 750.0)))
    except (TypeError, ValueError) as exc:
        log.error("fair_band_live_bad_config_winding_down", error=str(exc)[:160])
        lc, cfg_ok = FairBandLiveConfig(), False
    trader = FairBandLive(broker=broker, ownership=registry(), cfg=lc)
    if not mode_live:
        trader.halt("mode_off_wind_down")
    if not cfg_ok:
        trader.halt("bad_config")
    ledger = getattr(orch, "ledger", None) if orch is not None else None
    if ledger is None or not hasattr(ledger, "external_reserves"):
        # no carve-out => no capital isolation => never QUOTE; still clean up anything we left resting
        log.error("fair_band_live_no_ledger_refusing")
        trader.halt("no_ledger")
    else:
        ledger.external_reserves[SID] = trader.unused_budget  # registered BEFORE the first placement
    exclude = {str(x) for x in (exclude or set())}
    db_path = str(getattr(store, "_db_path", None) or "data/state.db")
    sdir = "data/fair_band_live"
    _os.makedirs(sdir, exist_ok=True)
    spath = f"{sdir}/state.json"
    try:
        start_iso = _json.load(open(spath, encoding="utf-8"))["start"]
    except Exception:  # noqa: BLE001
        start_iso = datetime.now(timezone.utc).isoformat()
        _json.dump({"start": start_iso}, open(spath, "w", encoding="utf-8"))
    try:
        n0 = await trader.cancel_open_registered()                # clean slate (only ids still OPEN at the venue)
        restored = trader.restore_probation()
        if restored:
            log.info("fair_band_live_probation_restored", markets=restored)
        log.info("fair_band_live_started", budget=lc.budget_usd, markets=lc.n_markets, cap=lc.market_cap_usd,
                 quote=lc.quote_usd, max_loss=lc.max_loss_usd, start=start_iso, startup_cancelled=n0,
                 winding_down=trader.winding_down, halt=trader.halt_reason)
    except Exception as exc:  # noqa: BLE001
        log.warning("fair_band_live_startup_cancel_failed", error=type(exc).__name__)
    n, it = 0, 0
    last_books: dict = {}                               # exchange_id -> (epoch, row) for the loss measures
    last_pnl = last_vpnl = None
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(60.0 if it else 5.0)        # fixed pace even when ticks fail (no 5s spin)
            it += 1
            # (0) kill checks that need NO venue read; blind cancels while winding down
            try:
                trader.check_kill(last_pnl, last_vpnl, orchestrator_halted=bool(getattr(orch, "is_halted", False)))
            except Exception as exc:  # noqa: BLE001
                trader.halt(f"kill_check_error {type(exc).__name__}")
            # (1) venue reads
            try:
                opens = await broker.get_open_orders_all()
                snaps = await broker.get_positions(fresh=True) or []
                pos = {str(getattr(p, "contract_id", "")): _signed_qty(p) for p in snaps}
                acct = await broker.get_account()
                free = float(acct.buying_power) if acct is not None else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("fair_band_live_read_failed", error=type(exc).__name__, msg=str(exc)[:160])
                if trader.winding_down:
                    await trader.cancel_all_registered(max_n=lc.max_writes_per_min * 5)
                continue
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            fresh_sig = getattr(xm_provider, "_fresh_sig", {}) or {}
            active = set(xm_provider.active_sig_exchange_ids())
            now = datetime.now(timezone.utc)
            mono = _time.monotonic()

            def row(ex):
                p = prices.get(ex) or {}
                bid, ask = p.get("best_bid"), p.get("best_ask")
                try:
                    age = (now - datetime.fromisoformat(str(p.get("updated_at")).replace("Z", "+00:00"))).total_seconds()
                except Exception:  # noqa: BLE001 — unparseable stamp = STALE for live trading
                    age = 1e9
                fr = fresh_sig.get(ex)                       # our own batch fetch beats the shared feed's tail
                if fr is not None and mono - fr[2] < age:
                    bid, ask, age = fr[0], fr[1], mono - fr[2]
                return {"fair": xm_provider.aligned_benchmark(ex) if ex in active else None,
                        "best_bid": bid, "best_ask": ask,
                        "bid_size": p.get("bid_size"), "ask_size": p.get("ask_size"), "age_s": age}

            # (2) market selection — isolated: a DB / disk error here must not block the rest of the tick
            if opens is not None and not trader.winding_down and (n % 60 == 0 or (not registry().owned_by(SID)
                                                                                   and it % 5 == 0)):
                try:
                    med, vol, _ = await asyncio.to_thread(_fair_band_inputs, db_path)
                    tags = classify(med, vol)
                    cands = sorted((ex for ex, t in tags.items() if "liquid" in t and ex in active
                                    and ex not in exclude), key=lambda ex: -(vol.get(ex) or 0.0))
                    owned = trader.select_markets(cands, pos, opens)
                    log.info("fair_band_live_markets", owned=owned, candidates=len(cands))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("fair_band_live_selection_failed", error=type(exc).__name__, msg=str(exc)[:160])
            # (3) loss measures -> kill -> tick
            try:
                all_owned = registry().owned_by(SID)
                rows = {ex: row(ex) for ex in all_owned}
                _t = _time.time()
                for ex, r in rows.items():
                    if r.get("best_bid") is not None or r.get("best_ask") is not None:
                        last_books[ex] = (_t, r)
                books = {ex: last_books[ex][1] for ex in all_owned
                         if ex in last_books and _t - last_books[ex][0] <= 120}
                for ex in all_owned:
                    books.setdefault(ex, {"fair": rows[ex].get("fair")})
                # loss scope = every market ever owned since start (released ones keep their realized result)
                hist = [h for h in registry().history_of(SID) if str(h[2]) >= start_iso or h[3] is None]
                scope = all_owned | {str(h[0]) for h in hist}
                for ex in scope - set(books):
                    if ex in last_books:
                        books[ex] = last_books[ex][1]
                last_pnl = await asyncio.to_thread(_fair_band_live_pnl, db_path, scope, start_iso, books,
                                                   registry().claimed_at, hist)
                last_vpnl = venue_pnl(snaps, all_owned, books)
                trader.check_kill(last_pnl, last_vpnl, orchestrator_halted=bool(getattr(orch, "is_halted", False)))
                if free is not None:                 # cash other strategies reserved is not ours to spend
                    free = max(float(free) - _others_reserved(orch, SID)(), 0.0)
                res = await trader.tick(rows, pos, opens, free)
                res.update({"ts": now.isoformat(), "pnl": None if last_pnl is None else round(last_pnl, 2),
                            "venue_pnl": None if last_vpnl is None else round(last_vpnl, 2),
                            "owned": len(all_owned), "carve_out": round(trader.unused_budget(), 2),
                            "halt": trader.halt_reason})
                try:
                    with open(f"{sdir}/ticks.jsonl", "a", encoding="utf-8") as fh:
                        fh.write(_json.dumps(res) + "\n")
                except OSError:
                    pass
                if res.get("placed") or res.get("cancelled") or n % 15 == 0 or trader.winding_down:
                    log.info("fair_band_live_tick", **{k: v for k, v in res.items() if k != "ts"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never crash the bot; next tick re-reads venue truth
                log.warning("fair_band_live_failed", error=type(exc).__name__, msg=str(exc)[:200])
                if trader.winding_down:
                    await trader.cancel_all_registered(max_n=lc.max_writes_per_min * 5)
            finally:
                n += 1
            # (4) heartbeat: we are alive and managing our markets (ownership LEASE)
            try:
                registry().beat(SID)
            except Exception as exc:  # noqa: BLE001
                log.warning("fair_band_live_heartbeat_failed", error=type(exc).__name__)
    finally:
        # shutdown / cancellation: never leave quotes resting with nobody managing them
        try:
            k = await asyncio.wait_for(asyncio.shield(trader.cancel_open_registered()), timeout=30.0)
            log.info("fair_band_live_stopped_orders_cancelled", cancelled=k)
        except BaseException as exc:  # noqa: BLE001
            log.warning("fair_band_live_shutdown_cancel_incomplete", error=type(exc).__name__)


def _others_reserved(orch, sid: str):
    """Callable: cash OTHER strategies have carved out of the shared ledger (fair_band, fresh_gap, cross resting maker
    entries, ...). SIG reserves no cash for resting orders, so a pilot sizing off raw buying power could spend cash
    another strategy's resting orders need. A failing reserve counts as 'everything reserved' (no new buys)."""
    def total() -> float:
        ledger = getattr(orch, "ledger", None) if orch is not None else None
        res = getattr(ledger, "external_reserves", None) or {}
        tot = 0.0
        for k, fn in list(res.items()):
            if k == sid:
                continue
            try:
                tot += max(float(fn() if callable(fn) else fn), 0.0)
            except Exception:  # noqa: BLE001
                return float("inf")
        return tot
    return total


def _fresh_gap_live_pnl(db_path: str, scope: set, since_iso: str, books: dict, history) -> float | None:
    """P&L of the fresh-gap pilot: every fill inside one of ITS ownership windows (exclusive markets -> these are all
    its fills), buys and sells, open inventory valued at liquidation. None on read failure."""
    import sqlite3 as _sq
    from sig.engine.fresh_gap_live import fills_pnl
    if not scope:
        return 0.0
    try:
        con = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            q = ",".join("?" * len(scope))
            rows = con.execute(f"SELECT exchange_id, outcome, price, quantity, signed_qty, filled_at FROM strategy_fills "
                               f"WHERE exchange_id IN ({q}) AND filled_at >= ?", (*sorted(scope), since_iso)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None
    win: dict = {}
    for h in history or []:
        win.setdefault(str(h[0]), []).append((str(h[2]), str(h[3]) if h[3] else "9999"))
    rows = [r for r in rows if any(a <= str(r[5]) < b for a, b in win.get(str(r[0]), []))]
    return fills_pnl([{"exchange_id": r[0], "outcome": r[1], "price": r[2], "quantity": r[3], "signed_qty": r[4]}
                      for r in rows], books)


async def _fresh_gap_live_loop(*, store, broker, orch, xm_provider, cfg_raw: dict, stop_event,
                               exclude: set | None = None) -> None:
    """FRESH-DISLOCATION REVERSION live pilot (sig/engine/fresh_gap_live.py). OFF unless
    cross_market.fresh_gap_live_mode == live (or it still owns markets / orders from an earlier run: then it starts in
    wind-down to flatten and release them). Exclusive markets, hard carve-out, own write cap, loss limit + STOP file.
    Each 60s tick: kill checks -> venue reads (open orders, positions, account) -> 24h-ago values (one indexed
    sharp_history read) -> P&L -> trader.tick. Shutdown: clean slate of its own resting orders."""
    import os as _os
    import time as _time
    from datetime import datetime, timedelta, timezone
    from sig.engine.fresh_gap import FreshGapConfig
    from sig.engine.fair_band_live import venue_pnl
    from sig.engine.fresh_gap_live import SID, FreshGapLive, FreshGapLiveConfig
    from sig.engine.orchestrator import _signed_qty
    from sig.engine.ownership import registry
    xm = (cfg_raw.get("cross_market", {}) or {})
    mode_live = str(xm.get("fresh_gap_live_mode", "off")).lower() == "live"
    if not mode_live and not registry().owned_by(SID) and not registry().orders_of(SID):
        log.info("fresh_gap_live_not_started")
        return
    cfg_ok = True
    try:
        lc = FreshGapLiveConfig(budget_usd=float(xm.get("fresh_gap_live_budget_usd", 15000.0)),
                                trade_usd=float(xm.get("fresh_gap_live_trade_usd", 2500.0)),
                                max_open=int(xm.get("fresh_gap_live_max_open", 6)),
                                max_loss_usd=float(xm.get("fresh_gap_live_max_loss_usd", 750.0)),
                                entry_mode=str(xm.get("fresh_gap_live_entry_mode", "taker")).lower(),
                                exit_mode=str(xm.get("fresh_gap_live_exit_mode", "taker")).lower(),
                                signal=FreshGapConfig(
                                    th_move=float(xm.get("fresh_gap_live_th_move", xm.get("fresh_gap_th_move", 0.03))),
                                    th_gap=float(xm.get("fresh_gap_live_th_gap", xm.get("fresh_gap_th_gap", 0.02)))))
    except (TypeError, ValueError) as exc:
        log.error("fresh_gap_live_bad_config_winding_down", error=str(exc)[:160])
        lc, cfg_ok = FreshGapLiveConfig(), False
    trader = FreshGapLive(broker=broker, ownership=registry(), cfg=lc, exclude={str(x) for x in (exclude or set())},
                          others_reserved=_others_reserved(orch, SID))
    trader.load()
    try:
        registry().beat(SID)                  # LEASE: without heartbeats exit:* strategies may trade our markets
    except Exception as exc:  # noqa: BLE001
        log.warning("fresh_gap_live_heartbeat_failed", error=type(exc).__name__)
    if not mode_live:
        trader.halt("mode_off_wind_down")
    if not cfg_ok:
        trader.halt("bad_config")
    ledger = getattr(orch, "ledger", None) if orch is not None else None
    if ledger is None or not hasattr(ledger, "external_reserves"):
        log.error("fresh_gap_live_no_ledger_refusing")
        trader.halt("no_ledger")
    else:
        ledger.external_reserves[SID] = trader.unused_budget       # registered BEFORE the first placement
    db_path = str(getattr(store, "_db_path", None) or "data/state.db")
    _os.makedirs(_os.path.dirname(lc.state_path) or ".", exist_ok=True)
    start_path = _os.path.join(_os.path.dirname(lc.state_path) or ".", "start.txt")
    try:
        with open(start_path, encoding="utf-8") as fh:
            start_iso = fh.read().strip()
    except OSError:
        start_iso = datetime.now(timezone.utc).isoformat()
        with open(start_path, "w", encoding="utf-8") as fh:
            fh.write(start_iso)
    try:
        n0 = await trader.cancel_open_registered()
        log.info("fresh_gap_live_started", budget=lc.budget_usd, trade=lc.trade_usd, max_open=lc.max_open,
                 max_loss=lc.max_loss_usd, start=start_iso, startup_cancelled=n0, open_trades=len(trader.trades),
                 winding_down=trader.winding_down, halt=trader.halt_reason)
    except Exception as exc:  # noqa: BLE001
        log.warning("fresh_gap_live_startup_cancel_failed", error=type(exc).__name__)
    last_pnl, last_vpnl, last_books, it = None, None, {}, 0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(60.0 if it else 5.0)
            it += 1
            halted = bool(getattr(orch, "is_halted", False))
            try:
                trader.check_kill(last_pnl, orchestrator_halted=halted, venue_pnl_usd=last_vpnl)
            except Exception as exc:  # noqa: BLE001
                trader.halt(f"kill_check_error {type(exc).__name__}")
            try:
                opens = await broker.get_open_orders_all()
                snaps = await broker.get_positions(fresh=True) or []
                pos = {str(getattr(p, "contract_id", "")): _signed_qty(p) for p in snaps}
                acct = await broker.get_account()
                free = float(acct.buying_power) if acct is not None else None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("fresh_gap_live_read_failed", error=type(exc).__name__, msg=str(exc)[:160])
                continue
            try:
                now = datetime.now(timezone.utc)
                lb_h = float(xm.get("fresh_gap_live_lookback_h", 24.0))   # research 2026-10-11: 12h more robust
                lo = (now - timedelta(hours=lb_h, minutes=3)).isoformat()
                hi = (now - timedelta(hours=lb_h, minutes=-3)).isoformat()
                past = {}
                for r in await store._read("SELECT exchange_id, ts, sharp, sig_mid FROM sharp_history "
                                           "WHERE ts >= ? AND ts < ? ORDER BY ts", (lo, hi)):
                    if r["sharp"] is not None and r["sig_mid"] is not None:
                        past[str(r["exchange_id"])] = (float(r["sig_mid"]), float(r["sharp"]))
                prices = getattr(xm_provider, "_sig_prices", {}) or {}
                active = {str(x) for x in xm_provider.active_sig_exchange_ids()}
                owned = set(registry().owned_by(SID))
                rows = {}
                for ex in active | owned:
                    p = prices.get(ex) or {}
                    try:
                        age = (now - datetime.fromisoformat(str(p.get("updated_at")).replace("Z", "+00:00"))).total_seconds()
                    except Exception:  # noqa: BLE001 — unparseable stamp = STALE
                        age = 1e9
                    rows[ex] = {"sharp": xm_provider.aligned_benchmark(ex) if ex in active else None,
                                "best_bid": p.get("best_bid"), "best_ask": p.get("best_ask"),
                                "bid_size": p.get("bid_size"), "ask_size": p.get("ask_size"), "age_s": age}
                rows = {ex: r for ex, r in rows.items() if r["sharp"] is not None or ex in owned}
                _t = _time.time()
                for ex in owned:
                    if rows.get(ex, {}).get("best_bid") is not None:
                        last_books[ex] = (_t, rows[ex])
                hist = [h for h in registry().history_of(SID) if str(h[2]) >= start_iso or h[3] is None]
                scope = owned | {str(h[0]) for h in hist}
                books = {ex: last_books[ex][1] for ex in scope if ex in last_books}
                last_pnl = await asyncio.to_thread(_fresh_gap_live_pnl, db_path, scope, start_iso, books, hist)
                vbooks = {ex: {"best_bid": (rows.get(ex) or {}).get("best_bid"),
                               "best_ask": (rows.get(ex) or {}).get("best_ask"),
                               "fair": (rows.get(ex) or {}).get("sharp")} for ex in owned}
                last_vpnl = venue_pnl(snaps, owned, vbooks)
                trader.check_kill(last_pnl, orchestrator_halted=halted, venue_pnl_usd=last_vpnl)
                fbo = None
                oids = sorted({str(x[4]) for t_ in trader.trades.values() for x in (t_.get("recent") or [])
                               if len(x) > 4 and x[4]})
                if oids:
                    try:
                        fbo = {}
                        for r_ in await store._read(
                                f"SELECT order_id, SUM(ABS(quantity)) q FROM strategy_fills WHERE order_id IN "
                                f"({','.join('?' * len(oids))}) GROUP BY order_id", tuple(oids)):
                            fbo[str(r_["order_id"])] = float(r_["q"] or 0.0)
                    except Exception:  # noqa: BLE001 — unknown fills: keep assuming orders filled (safe side)
                        fbo = None
                res = await trader.tick(rows, past, pos, opens, free, fills_by_order=fbo)
                if res.get("placed") or res.get("claimed") or res.get("closed") or it % 15 == 1 or trader.winding_down:
                    log.info("fresh_gap_live_tick", pnl=None if last_pnl is None else round(last_pnl, 2),
                             venue_pnl=round(last_vpnl, 2),
                             carve_out=round(trader.unused_budget(), 2), halt=trader.halt_reason, **res)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never crash the bot; next tick re-reads venue truth
                log.warning("fresh_gap_live_failed", error=type(exc).__name__, msg=str(exc)[:200])
            # heartbeat (ownership LEASE) every tick, even after a failed tick: we are alive and managing
            try:
                registry().beat(SID)
            except Exception as exc:  # noqa: BLE001
                log.warning("fresh_gap_live_heartbeat_failed", error=type(exc).__name__)
    finally:
        try:
            k = await asyncio.wait_for(asyncio.shield(trader.cancel_open_registered()), timeout=30.0)
            log.info("fresh_gap_live_stopped_orders_cancelled", cancelled=k)
        except BaseException as exc:  # noqa: BLE001
            log.warning("fresh_gap_live_shutdown_cancel_incomplete", error=type(exc).__name__)


async def _rotation_live_loop(*, store, broker, orch, xm_provider, cfg_raw: dict, stop_event) -> None:
    """CAPITAL ROTATION live trader (sig/engine/rotation_live.py). OFF unless cross_market.rotation_live_mode == live
    (or an active swap / owned markets remain from an earlier run: then it finishes/releases and starts nothing new).
    Each 60s tick: kill checks -> venue reads -> decision (rotation_v2.decide_v2 with the retuned parameters, on the
    REAL book minus frozen / foreign-owned markets) -> fills by order -> trader.tick. Heartbeat every tick."""
    import time as _time
    from sig.engine.orchestrator import _signed_qty
    from sig.engine.ownership import registry
    from sig.engine.rotation import Candidate, Held
    from sig.engine.rotation_live import SID, RotationLive, RotationLiveConfig, sell_depth_ok
    from sig.engine.rotation_v2 import PersistenceTracker, decide_v2
    xm = (cfg_raw.get("cross_market", {}) or {})
    mode_live = str(xm.get("rotation_live_mode", "off")).lower() == "live"
    if not mode_live and not registry().owned_by(SID):
        log.info("rotation_live_not_started")
        return
    try:
        lc = RotationLiveConfig(max_swap_usd=float(xm.get("rotation_live_max_swap_usd", 2000.0)),
                                max_swaps_per_day=int(xm.get("rotation_live_max_swaps_per_day", 6)),
                                freeze_s=float(xm.get("rotation_live_freeze_s", 86400.0)))
    except (TypeError, ValueError) as exc:
        log.error("rotation_live_bad_config", error=str(exc)[:160])
        return
    trader = RotationLive(broker=broker, ownership=registry(), cfg=lc, others_reserved=_others_reserved(orch, SID))
    trader.load()
    if not mode_live:
        trader.stopped, trader.stop_reason = True, "mode_off"
    ledger = getattr(orch, "ledger", None) if orch is not None else None
    if ledger is None or not hasattr(ledger, "external_reserves"):
        log.error("rotation_live_no_ledger_refusing")
        trader.stopped, trader.stop_reason = True, "no_ledger"
    else:
        ledger.external_reserves[SID] = trader.unused_budget
    margin = float(xm.get("rotation_v2_margin", xm.get("rotation_margin", 0.04)))
    # live rotation's own persistence (operator 2026-10-17: 30 min); falls back to the v2 shadow's setting
    persist_s = float(xm.get("rotation_live_persist_s", xm.get("rotation_v2_persist_s", 3600.0)))
    cap_frac = float((cfg_raw.get("capital", {}) or {}).get("max_per_market_frac", 0.05))
    # bigger per-market cap ONLY for rotation buys into >= big_gap edges (operator 2026-10-16: 10% at >= 7c); 0 = off
    big_frac = float(xm.get("rotation_live_big_cap_frac", 0.0))
    big_gap = float(xm.get("rotation_live_big_cap_gap", 0.07))
    # sells rest at the bid and fill as it refills (2026-10-22), so only a really tiny known bid excludes a source
    min_sell_depth = float(xm.get("rotation_live_min_sell_depth_usd", 200.0))
    # KALSHI FALLBACK fair (2026-10-22): a market with no fresh Polymarket price uses the latest Kalshi mid
    # (<= 5 min old; Kalshi vs Poly median |diff| 0.7c on 153 legs where both exist). Off unless enabled.
    kal_on = bool(xm.get("rotation_live_kalshi_fallback", False))
    kal_db = str(getattr(store, "_db_path", None) or "data/state.db")
    kal, kal_at = {}, 0.0
    cost_floor = float(xm.get("conviction_cost_floor", 0.05))
    min_val = float(xm.get("rotation_min_position_usd", 500.0))
    tracker = PersistenceTracker()
    try:
        registry().beat(SID)
    except Exception:  # noqa: BLE001
        pass
    log.info("rotation_live_started", max_swap=lc.max_swap_usd, per_day=lc.max_swaps_per_day, margin=margin,
             active=bool(trader.swap), stopped=trader.stop_reason)
    it = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(60.0 if it else 5.0)
        it += 1
        try:
            trader.check_kill(orchestrator_halted=bool(getattr(orch, "is_halted", False)))
            opens = await broker.get_open_orders_all()
            snaps = await broker.get_positions(fresh=True) or []
            pos = {str(getattr(p, "contract_id", "")): _signed_qty(p) for p in snaps}
            acct = await broker.get_account()
            free = float(acct.buying_power) if acct is not None else None
            nav = float(acct.equity) if acct is not None else 0.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("rotation_live_read_failed", error=type(exc).__name__, msg=str(exc)[:160])
            try:
                registry().beat(SID)
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            from datetime import datetime, timezone
            now_dt = datetime.now(timezone.utc)
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            active = {str(x) for x in xm_provider.active_sig_exchange_ids()}
            if kal_on and _time.time() - kal_at >= 60.0:
                try:
                    kal = await asyncio.to_thread(_kalshi_latest, kal_db)
                except Exception:  # noqa: BLE001 — no Kalshi this tick: Poly-only
                    kal = {}
                kal_at = _time.time()
            universe = active | ({ex for ex in kal if ex in prices} if kal_on else set())
            rows = {}
            for ex in universe | set(registry().owned_by(SID)):
                p = prices.get(ex) or {}
                try:
                    age = (now_dt - datetime.fromisoformat(str(p.get("updated_at")).replace("Z", "+00:00"))).total_seconds()
                except Exception:  # noqa: BLE001
                    age = 1e9
                rows[ex] = {"best_bid": p.get("best_bid"), "best_ask": p.get("best_ask"),
                            "bid_size": p.get("bid_size"), "ask_size": p.get("ask_size"), "age_s": age}
            proposal = None
            if trader.swap is None and not trader.stopped and not trader.paused:
                now = _time.time()
                frozen = trader.frozen(now)
                reg = registry()
                held, cands, persisted = [], [], {}
                for ex in universe:
                    sh = xm_provider.aligned_benchmark(ex) if ex in active else None
                    if sh is None and kal_on:
                        sh = kal.get(ex)                       # Kalshi fallback fair
                    r = rows.get(ex) or {}
                    if sh is None or r.get("best_bid") is None or r.get("best_ask") is None:
                        continue
                    b, a = float(r["best_bid"]), float(r["best_ask"])
                    if not (0 < b < a < 1):
                        continue
                    mid = (b + a) / 2.0
                    persisted[ex] = tracker.update(ex, float(sh), mid, now, cost_floor)
                    if ex in frozen or reg.owner_of(ex) not in (None,):
                        continue                       # frozen, or owned by another strategy (pilots)
                    hs = max((a - b) / 2.0, 0.0025)
                    q = pos.get(ex, 0.0)
                    if abs(q) >= 1 and sell_depth_ok(r, q, min_sell_depth):   # tiny known bid: skip as a source
                        held.append(Held(ex, q, mid, float(sh), hs, abs(q) * (mid if q > 0 else 1 - mid)))
                    cands.append(Candidate(ex, mid, float(sh), hs))
                # cash=0: live rotation recycles BOOK capital even when cash idles (cross's maker entries rarely fill)
                sw = decide_v2(held, cands, [], persisted=persisted, persist_s=persist_s, cash=0.0,
                               position_size=cap_frac * nav, margin=margin, cost_floor=cost_floor,
                               min_held_value=min_val, topup=True, freeze_s=lc.freeze_s, now=now,
                               big_position_size=(big_frac * nav) if big_frac > 0 else None, big_gap=big_gap)
                if sw:
                    from sig.engine.rotation_v2 import cap_for
                    _bx = sw[0].buy.exchange_id
                    _q = float(pos.get(_bx, 0.0))               # account position, not the filtered sell pool
                    _r = rows.get(_bx) or {}
                    _m = (float(_r["best_bid"]) + float(_r["best_ask"])) / 2.0
                    held_usd = abs(_q) * (_m if _q > 0 else 1.0 - _m)
                    cap_usd = cap_for(sw[0].buy.gap, cap_frac * nav, (big_frac * nav) if big_frac > 0 else None, big_gap)
                    proposal = {"sell_ex": sw[0].sell.exchange_id, "sell_qty": sw[0].sell.qty,
                                "buy_ex": sw[0].buy.exchange_id, "buy_side": sw[0].buy.side,
                                "advantage": sw[0].advantage,
                                "max_buy_usd": max(cap_usd - held_usd, 0.0)}
            fbo = None
            oids = sorted({str(x[4]) for x in ((trader.swap or {}).get("recent") or []) if len(x) > 4 and x[4]})
            if oids:
                try:
                    fbo = {}
                    for r_ in await store._read(
                            f"SELECT order_id, SUM(ABS(quantity)) q FROM strategy_fills WHERE order_id IN "
                            f"({','.join('?' * len(oids))}) GROUP BY order_id", tuple(oids)):
                        fbo[str(r_["order_id"])] = float(r_["q"] or 0.0)
                except Exception:  # noqa: BLE001
                    fbo = None
            mfills = None
            if trader.swap:
                try:
                    from datetime import datetime as _dt2, timezone as _tz2
                    since = _dt2.fromtimestamp(float(trader.swap["epoch"]), _tz2.utc).isoformat()
                    mfills = {}
                    for r_ in await store._read(
                            "SELECT exchange_id, outcome, price, quantity, signed_qty FROM strategy_fills "
                            "WHERE exchange_id IN (?, ?) AND filled_at >= ?",
                            (trader.swap["sell_ex"], trader.swap["buy_ex"], since)):
                        ex_ = str(r_["exchange_id"])
                        q_ = abs(float(r_["quantity"] or 0.0))
                        sq_ = float(r_["signed_qty"] or 0.0)
                        out_ = str(r_["outcome"]).lower()
                        buy_ = (sq_ > 0) if out_ == "yes" else (sq_ < 0)
                        d_ = mfills.setdefault(ex_, {"sold_qty": 0.0, "sold_usd": 0.0, "bought_qty": 0.0,
                                                     "bought_usd": 0.0})
                        k_ = "bought" if buy_ else "sold"
                        d_[k_ + "_qty"] += q_
                        d_[k_ + "_usd"] += q_ * float(r_["price"])
                except Exception:  # noqa: BLE001 — unknown: positions-only (conservative)
                    mfills = None
            res = await trader.tick(proposal, rows, pos, opens, free, fills_by_order=fbo, market_fills=mfills)
            if res.get("placed") or res.get("claimed") or res.get("closed") or res.get("aborted") or it % 15 == 1:
                log.info("rotation_live_tick", proposal=bool(proposal), carve_out=round(trader.unused_budget(), 2),
                         swaps_today=trader.swaps_today(_time.time()), stop=trader.stop_reason, **res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never crash the bot
            log.warning("rotation_live_failed", error=type(exc).__name__, msg=str(exc)[:200])
        try:
            registry().beat(SID)               # LEASE heartbeat every tick
        except Exception as exc:  # noqa: BLE001
            log.warning("rotation_live_heartbeat_failed", error=type(exc).__name__)


async def _election_night_loop(*, store, xm_provider, pairs: list, cfg_raw: dict, stop_event) -> None:
    """ELECTION-NIGHT dry run (sig/engine/election_night.py): every 30s evaluate the race-call trigger on live
    Polymarket (+ Kalshi agreement) and SIG quotes, and RECORD what it would buy. No orders. Rarely fires before
    election night; it proves the mechanics (arming, agreement guard, one-shot per race) on live data."""
    import json as _json
    import os as _os
    import time as _time
    from datetime import datetime, timezone
    import yaml as _yaml
    from sig.engine.election_night import ElectionNightConfig, NightState, step
    xm = (cfg_raw.get("cross_market", {}) or {})
    if str(xm.get("election_night_mode", "off")).lower() != "shadow" or not pairs:
        log.info("election_night_not_started", pairs=len(pairs))
        return
    cfg = ElectionNightConfig(theta=float(xm.get("election_night_theta", 0.95)),
                              usd_per_race=float(xm.get("election_night_usd", 2000.0)))
    path = str(xm.get("election_night_path", "data/election_night_shadow/triggers.jsonl"))
    db_path = str(getattr(store, "_db_path", None) or "data/state.db")
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    try:
        kmapped = {str(l["exchange_id"]) for l in (_yaml.safe_load(open(str(xm.get("kalshi_map_path", "signals/kalshi_map.yaml")),
                                                                      encoding="utf-8")) or {}).get("legs", [])}
    except Exception:  # noqa: BLE001
        kmapped = set()
    state, n = NightState(), 0
    log.info("election_night_started", theta=cfg.theta, races=len(pairs))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(30.0)
        try:
            if n % 4 == 0:
                _, _, kal = await asyncio.to_thread(_fair_band_inputs, db_path)
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            active = set(xm_provider.active_sig_exchange_ids())
            legs = []
            for race, d_ex, r_ex in pairs:
                for ex in (d_ex, r_ex):
                    p = prices.get(str(ex)) or {}
                    legs.append({"race": race, "exchange_id": str(ex),
                                 "poly": xm_provider.aligned_benchmark(ex) if str(ex) in active else None,
                                 "kalshi": kal.get(str(ex)), "kalshi_mapped": str(ex) in kmapped,
                                 "sig_bid": p.get("best_bid"), "sig_ask": p.get("best_ask"), "ask_size": p.get("ask_size")})
            fired = step(legs, state, cfg, _time.time())
            if fired:
                ts = datetime.now(timezone.utc).isoformat()
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(_json.dumps({"ts": ts, **f}) for f in fired) + "\n")
                for f in fired:
                    log.info("election_night_trigger_shadow", race=f["race"], ex=f["exchange_id"], poly=f["poly"],
                             ask=f["sig_ask"], edge_c=f["edge_c"])
            n += 1
            if n % 120 == 1:
                log.info("election_night_tick", legs=len(legs), armed=len(state.last_low), fired_total=len(state.fired))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("election_night_failed", error=type(exc).__name__, msg=str(exc)[:160])


def _kalshi_latest(db_path: str, max_age_s: float = 300.0) -> dict:
    """Latest Kalshi mid per SIG leg, at most max_age_s old (one small indexed read)."""
    import sqlite3 as _sq
    from datetime import datetime, timezone, timedelta
    con = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        lo = (datetime.now(timezone.utc) - timedelta(seconds=max_age_s)).isoformat()
        return {str(ex): float(m) for ex, m in con.execute(
            "SELECT exchange_id, mid FROM kalshi_history WHERE ts >= ? AND mid IS NOT NULL ORDER BY ts", (lo,))}
    finally:
        con.close()


async def _election_night_live_loop(*, store, broker, orch, xm_provider, pairs: list, cfg_raw: dict,
                                    stop_event) -> None:
    """ELECTION-NIGHT LIVE trader (sig/engine/election_night_live.py). OFF unless election_night_live_mode is 'dry'
    (decisions on live data, no orders, would-be orders -> dry.jsonl) or 'live'. Outside the window it only ARMS legs
    (cache reads, no venue reads) every 30s; inside it ticks every election_night_live_tick_s and reads the venue
    (open orders + account) only when something could be placed. Heart-beats the ownership lease while it holds."""
    import os as _os
    import time as _time
    from datetime import datetime, timezone
    import yaml as _yaml
    from sig.engine.election_night import ElectionNightConfig
    from sig.engine.election_night_live import SID, ElectionNightLive, ElectionNightLiveConfig
    from sig.engine.ownership import registry
    xm = (cfg_raw.get("cross_market", {}) or {})
    mode = str(xm.get("election_night_live_mode", "off")).lower()
    if mode not in ("dry", "live") or not pairs:
        if registry().owned_by(SID):
            log.info("election_night_live_holding_only", owned=len(registry().owned_by(SID)))
            while not (stop_event is not None and stop_event.is_set()):   # keep the lease: our winners stay ours
                try:
                    registry().beat(SID)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(120.0)
        log.info("election_night_live_not_started", mode=mode, pairs=len(pairs))
        return
    try:
        d = ElectionNightLiveConfig()
        lc = ElectionNightLiveConfig(
            window_start_iso=str(xm.get("election_night_live_window_start", d.window_start_iso)),
            window_end_iso=str(xm.get("election_night_live_window_end", d.window_end_iso)),
            reserve_from_iso=str(xm.get("election_night_live_reserve_from", d.reserve_from_iso)),
            budget_usd=float(xm.get("election_night_live_budget_usd", d.budget_usd)),
            usd_per_race=float(xm.get("election_night_live_usd_per_race", d.usd_per_race)),
            max_writes_per_min=int(xm.get("election_night_live_max_writes_per_min", d.max_writes_per_min)),
            signal=ElectionNightConfig(theta=float(xm.get("election_night_live_theta", 0.95))))
        for iso_ in (lc.window_start_iso, lc.window_end_iso, lc.reserve_from_iso):
            datetime.fromisoformat(iso_)
    except (TypeError, ValueError) as exc:
        log.error("election_night_live_bad_config_not_started", error=str(exc)[:160])
        return
    trader = ElectionNightLive(broker=broker, ownership=registry(), cfg=lc, dry=(mode != "live"),
                               others_reserved=_others_reserved(orch, SID))
    trader.load()
    ledger = getattr(orch, "ledger", None) if orch is not None else None
    if mode == "live":
        if ledger is None or not hasattr(ledger, "external_reserves"):
            log.error("election_night_live_no_ledger_refusing")
            trader.stopped, trader.stop_reason = True, "no_ledger"
        else:
            ledger.external_reserves[SID] = trader.unused_budget      # registered BEFORE the first placement
    db_path = str(getattr(store, "_db_path", None) or "data/state.db")
    try:
        kmapped = {str(l["exchange_id"]) for l in (_yaml.safe_load(open(str(xm.get("kalshi_map_path", "signals/kalshi_map.yaml")),
                                                                      encoding="utf-8")) or {}).get("legs", [])}
    except Exception:  # noqa: BLE001
        kmapped = set()
    tick_s = max(2.0, float(xm.get("election_night_live_tick_s", 5.0)))
    log.info("election_night_live_started", mode=mode, races=len(pairs), budget=lc.budget_usd, per_race=lc.usd_per_race,
             window=f"{lc.window_start_iso}..{lc.window_end_iso}", reserve_from=lc.reserve_from_iso,
             jobs=len(trader.races), owned=len(registry().owned_by(SID)))
    kal, kal_at, beat_at, it = {}, 0.0, 0.0, 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        now_ep = _time.time()
        near = trader.in_window(now_ep) or trader.in_window(now_ep + 600.0)
        await asyncio.sleep(tick_s if near else 30.0)
        it += 1
        try:
            now_ep = _time.time()
            if now_ep - beat_at >= 60.0:
                registry().beat(SID)          # LEASE: without heartbeats exit:* strategies may trade our markets
                beat_at = now_ep
            trader.check_kill(orchestrator_halted=bool(getattr(orch, "is_halted", False)))
            if now_ep - kal_at >= 60.0:
                kal = await asyncio.to_thread(_kalshi_latest, db_path)
                kal_at = now_ep
            active = {str(x) for x in xm_provider.active_sig_exchange_ids()}
            legs = []
            for race, d_ex, r_ex in pairs:
                for ex in (str(d_ex), str(r_ex)):
                    bk = xm_provider.sig_book(ex, 60.0)
                    legs.append({"race": race, "exchange_id": ex,
                                 "poly": xm_provider.aligned_benchmark(ex) if ex in active else None,
                                 "kalshi": kal.get(ex), "kalshi_mapped": ex in kmapped,
                                 "sig_bid": bk[0] if bk else None, "sig_ask": bk[1] if bk else None, "ask_size": None})
            opens, free, fbo = [], 0.0, None
            th = lc.signal.theta
            maybe = trader.in_window(now_ep) and not trader.stopped and (
                any(j["attempts"] < lc.max_attempts and not j.get("skip") for j in trader.races.values()) or
                any(l["poly"] is not None and l["poly"] >= th and l["exchange_id"] in trader.night.last_low
                    for l in legs))
            if maybe and mode == "live":
                opens = await broker.get_open_orders_all()
                acct = await broker.get_account()
                free = float(acct.buying_power) if acct is not None else 0.0
                oids = sorted({str(o[1]) for j in trader.races.values() for o in j.get("orders", []) if o[1]})
                if oids:
                    try:
                        fbo = {}
                        for r_ in await store._read(
                                f"SELECT order_id, SUM(ABS(quantity)) q FROM strategy_fills WHERE order_id IN "
                                f"({','.join('?' * len(oids))}) GROUP BY order_id", tuple(oids)):
                            fbo[str(r_["order_id"])] = float(r_["q"] or 0.0)
                    except Exception:  # noqa: BLE001 — unknown fills: committed stays at full notional (safe side)
                        fbo = None
            elif maybe:
                free = float("inf")                                       # dry: record what it would do
            res = await trader.tick(legs, opens, free, fills_by_order=fbo)
            if res.get("fired") or res.get("placed") or res.get("dry") or res.get("place_errors") or it % 120 == 1:
                log.info("election_night_live_tick", mode=mode, carve_out=round(trader.unused_budget(), 2),
                         armed=len(trader.night.last_low), stopped=trader.stop_reason, paused=trader.paused, **res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never crash the bot; next tick re-reads venue truth
            log.warning("election_night_live_failed", error=type(exc).__name__, msg=str(exc)[:200])


async def _kalshi_shadow_loop(*, store, cfg_raw: dict, stop_event) -> None:
    """KALSHI second-benchmark shadow: every interval, quote every mapped leg (signals/kalshi_map.yaml, party-verified)
    in a few batched GET /markets?tickers=... calls and store them in kalshi_history. External (Kalshi) reads only —
    no SIG read budget, no orders, no effect on cross-market. Validated by tools/kalshi_shadow_validate.py."""
    import os as _os
    from datetime import datetime, timezone, timedelta
    import yaml as _yaml
    from signals.kalshi_client import KalshiClient
    xm = (cfg_raw.get("cross_market", {}) or {})
    if str(xm.get("kalshi_shadow_mode", "off")).lower() != "shadow":
        log.info("kalshi_shadow_not_started")
        return
    path = str(xm.get("kalshi_map_path", "signals/kalshi_map.yaml"))
    interval = max(30.0, float(xm.get("kalshi_shadow_interval_s", 60.0)))
    try:
        legs = (_yaml.safe_load(open(path, encoding="utf-8")) or {}).get("legs", []) if _os.path.exists(path) else []
    except Exception as exc:  # noqa: BLE001
        log.warning("kalshi_map_load_failed", error=type(exc).__name__); legs = []
    tickers = sorted({l["ticker"] for l in legs if l.get("ticker")})
    if not tickers:
        log.info("kalshi_shadow_not_started", reason="empty map")
        return
    log.info("kalshi_shadow_started", legs=len(legs), interval_s=interval)
    n = 0
    async with KalshiClient(timeout=20.0) as kc:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(interval)
            try:
                markets = await kc.markets_by_tickers(tickers)
                rows = _kalshi_rows(legs, markets, datetime.now(timezone.utc).isoformat())
                await store.append_kalshi_history(rows)
                n += 1
                if n % 30 == 1:
                    log.info("kalshi_shadow_tick", priced=len(rows), legs=len(legs))
                if n % 60 == 0:
                    await store.prune_kalshi_history((datetime.now(timezone.utc) - timedelta(days=45)).isoformat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
                log.warning("kalshi_shadow_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _maker_shadow_loop(*, xm_provider, cfg_raw: dict, stop_event) -> None:
    """MAKER-ENTRY shadow (sig/engine/maker_shadow.py): every minute record the resting touch quote we'd post on the
    Polymarket-favoured side of each live cross-market gap, with the depth in front of it. Cache reads only; no
    venue calls, no orders. Scored later against the trade tape by tools/maker_shadow_validate.py."""
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from sig.engine.maker_shadow import maker_quotes
    xm = (cfg_raw.get("cross_market", {}) or {})
    if str(xm.get("maker_shadow_mode", "off")).lower() != "shadow":
        log.info("maker_shadow_not_started")
        return
    path = str(xm.get("maker_shadow_path", "data/maker_shadow/quotes.jsonl"))
    cost_floor = float(xm.get("conviction_cost_floor", 0.05))
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    log.info("maker_shadow_started", path=path)
    n = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(60.0)
        try:
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            rows = []
            for ex in xm_provider.active_sig_exchange_ids():
                sh = xm_provider.aligned_benchmark(ex)
                p = prices.get(str(ex)) or {}
                if sh is None or not p:
                    continue
                rows.append({"exchange_id": ex, "sharp": sh, "best_bid": p.get("best_bid"), "best_ask": p.get("best_ask"),
                             "bid_size": p.get("bid_size"), "ask_size": p.get("ask_size")})
            qs = maker_quotes(rows, cost_floor=cost_floor)
            if qs:
                ts = datetime.now(timezone.utc).isoformat()
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(_json.dumps({"ts": ts, **q}) for q in qs) + "\n")
            n += 1
            if n % 30 == 1:
                log.info("maker_shadow_tick", quotes=len(qs))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("maker_shadow_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _fresh_gap_loop(*, store, xm_provider, cfg_raw: dict, stop_event, prefix: str = "fresh_gap") -> None:
    """FRESH-DISLOCATION REVERSION shadow (sig/engine/fresh_gap.py). Every 5 min: cached SIG touch + aligned Poly
    sharp, plus ONE indexed sharp_history range read for the values ~24h ago. Records would-be entries/exits with
    taker AND maker prices to a jsonl; tools/fresh_gap_validate.py scores them. Record-only: no venue calls, no orders."""
    import json as _json
    import os as _os
    import time as _time
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone
    from sig.engine.fresh_gap import FreshGapConfig, Row, entry_signal, exit_reason, held_prices
    xm = (cfg_raw.get("cross_market", {}) or {})
    # `prefix` lets a second independent shadow run other thresholds (e.g. fresh_gap_t2_* = the 2c tier) with its
    # own file; the default prefix is the original shadow (whose thresholds the live pilot also uses).
    if str(xm.get(f"{prefix}_mode", "off")).lower() != "shadow":
        log.info("fresh_gap_not_started", prefix=prefix)
        return
    cfg = FreshGapConfig(th_move=float(xm.get(f"{prefix}_th_move", 0.03)), th_gap=float(xm.get(f"{prefix}_th_gap", 0.02)))
    path = str(xm.get(f"{prefix}_path", f"data/{prefix}_shadow/events.jsonl"))
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    open_t: dict = {}                       # ex -> entry record
    last_exit: dict = {}                    # ex -> epoch of last exit (6h cooldown: no instant re-entry)
    try:
        if _os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                e = _json.loads(line)
                if e.get("event") == "entry":
                    open_t[e["exchange_id"]] = e
                elif e.get("event") == "exit":
                    open_t.pop(e["exchange_id"], None)
    except Exception as exc:  # noqa: BLE001
        log.warning("fresh_gap_reload_failed", error=type(exc).__name__)
    log.info("fresh_gap_started", prefix=prefix, path=path, open=len(open_t), th_move=cfg.th_move, th_gap=cfg.th_gap)
    n = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(300.0)
        try:
            now = datetime.now(timezone.utc)
            lb_h = float(xm.get(f"{prefix}_lookback_h", 24.0))
            lo, hi = (now - timedelta(hours=lb_h, minutes=3)).isoformat(), (now - timedelta(hours=lb_h, minutes=-3)).isoformat()
            past = {}
            for r in await store._read("SELECT exchange_id, ts, sharp, sig_mid FROM sharp_history "
                                       "WHERE ts >= ? AND ts < ? ORDER BY ts", (lo, hi)):
                if r["sharp"] is not None and r["sig_mid"] is not None:
                    past[str(r["exchange_id"])] = (float(r["sig_mid"]), float(r["sharp"]))
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            ts, ep = now.isoformat(), _time.time()
            out = []
            for ex in xm_provider.active_sig_exchange_ids():
                ex = str(ex)
                sh = xm_provider.aligned_benchmark(ex)
                p = prices.get(ex) or {}
                b, a = p.get("best_bid"), p.get("best_ask")
                if sh is None or b is None or a is None:
                    continue
                b, a, sh = float(b), float(a), float(sh)
                if ex in open_t:
                    e = open_t[ex]
                    why = exit_reason(float(e["entry_gap"]), e["side"], ep - float(e["epoch"]), sh, b, a, cfg)
                    if why:
                        out.append({"event": "exit", "ts": ts, "epoch": ep, "exchange_id": ex, "id": e["id"],
                                    "reason": why, "sharp": sh, "bid": b, "ask": a,
                                    "prices": held_prices(e["side"], b, a), "age_h": round((ep - float(e["epoch"])) / 3600, 2)})
                        open_t.pop(ex, None)
                        last_exit[ex] = ep
                    continue
                if ex not in past or ep - last_exit.get(ex, -1e18) < 6 * 3600:
                    continue
                row = Row(ex, sh, b, a, past[ex][0], past[ex][1], float(p.get("bid_size") or 0), float(p.get("ask_size") or 0))
                side = entry_signal(row, cfg)
                if side:
                    pr = held_prices(side, b, a)
                    e = {"event": "entry", "ts": ts, "epoch": ep, "id": _uuid.uuid4().hex[:10], "exchange_id": ex,
                         "side": side, "entry_gap": round(row.gap, 4), "sharp": sh, "bid": b, "ask": a,
                         "bid_size": row.bid_size, "ask_size": row.ask_size, "mid_24h": row.mid_24h,
                         "sharp_24h": row.sharp_24h, "prices": pr, "shares": round(cfg.size_usd / pr["taker_in"], 1)}
                    out.append(e)
                    open_t[ex] = e
            if out:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(_json.dumps(x) for x in out) + "\n")
            n += 1
            if out or n % 12 == 1:
                log.info("fresh_gap_tick", prefix=prefix, open=len(open_t), events=len(out), past_rows=len(past))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("fresh_gap_failed", error=type(exc).__name__, msg=str(exc)[:160])


def _parse_lookbacks(v) -> list:
    if isinstance(v, (int, float)):
        v = [v]
    if isinstance(v, str):
        v = [x for x in v.replace(" ", "").split(",") if x]
    return sorted({float(x) for x in (v or [])})


async def _poly_lead_loop(*, store, xm_provider, cfg_raw: dict, stop_event) -> None:
    """POLY-LED LAG shadow (sig/engine/fresh_gap.poly_lead_signal). Every 5 min, for EACH lookback L (default 1/3/6h):
    one indexed sharp_history range read for the values ~L ago, then would-be entries (SIG lagging a Poly move) and
    exits (half-closed / flipped / max hold) with taker AND maker prices, tagged lb_h, to one jsonl. Each lookback is
    its own independent book. Record-only: no venue calls, no orders. Scored by tools/poly_lead_validate.py."""
    import json as _json
    import os as _os
    import time as _time
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone
    from sig.engine.fresh_gap import FreshGapConfig, Row, exit_reason, held_prices, poly_lead_signal
    xm = (cfg_raw.get("cross_market", {}) or {})
    if str(xm.get("poly_lead_mode", "off")).lower() != "shadow":
        log.info("poly_lead_not_started")
        return
    th = float(xm.get("poly_lead_th_move", 0.03))
    lbs = _parse_lookbacks(xm.get("poly_lead_lookbacks_h", [1, 3, 6]))
    cfg = FreshGapConfig(max_hold_s=float(xm.get("poly_lead_max_hold_h", 24.0)) * 3600.0)
    path = str(xm.get("poly_lead_path", "data/poly_lead_shadow/events.jsonl"))
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    open_t: dict = {lb: {} for lb in lbs}        # lb -> ex -> entry record
    last_exit: dict = {lb: {} for lb in lbs}     # lb -> ex -> epoch of last exit (6h cooldown)
    try:
        if _os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                e = _json.loads(line)
                lb = float(e.get("lb_h", -1))
                if lb not in open_t:
                    continue
                if e.get("event") == "entry":
                    open_t[lb][e["exchange_id"]] = e
                elif e.get("event") == "exit":
                    open_t[lb].pop(e["exchange_id"], None)
    except Exception as exc:  # noqa: BLE001
        log.warning("poly_lead_reload_failed", error=type(exc).__name__)
    log.info("poly_lead_started", path=path, lookbacks=lbs, th_move=th, open=sum(len(v) for v in open_t.values()))
    n = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(300.0)
        try:
            now = datetime.now(timezone.utc)
            ts, ep = now.isoformat(), _time.time()
            prices = getattr(xm_provider, "_sig_prices", {}) or {}
            live = {}
            for ex in xm_provider.active_sig_exchange_ids():
                ex = str(ex)
                sh = xm_provider.aligned_benchmark(ex)
                p = prices.get(ex) or {}
                b, a = p.get("best_bid"), p.get("best_ask")
                if sh is not None and b is not None and a is not None:
                    live[ex] = (float(sh), float(b), float(a), p)
            out = []
            for lb in lbs:
                lo = (now - timedelta(hours=lb, minutes=3)).isoformat()
                hi = (now - timedelta(hours=lb, minutes=-3)).isoformat()
                past = {}
                for r in await store._read("SELECT exchange_id, ts, sharp, sig_mid FROM sharp_history "
                                           "WHERE ts >= ? AND ts < ? ORDER BY ts", (lo, hi)):
                    if r["sharp"] is not None and r["sig_mid"] is not None:
                        past[str(r["exchange_id"])] = (float(r["sig_mid"]), float(r["sharp"]))
                book, lex = open_t[lb], last_exit[lb]
                for ex, (sh, b, a, p) in live.items():
                    if ex in book:
                        e = book[ex]
                        why = exit_reason(float(e["entry_gap"]), e["side"], ep - float(e["epoch"]), sh, b, a, cfg)
                        if why:
                            out.append({"event": "exit", "ts": ts, "epoch": ep, "exchange_id": ex, "id": e["id"],
                                        "lb_h": lb, "reason": why, "sharp": sh, "bid": b, "ask": a,
                                        "prices": held_prices(e["side"], b, a),
                                        "age_h": round((ep - float(e["epoch"])) / 3600, 2)})
                            book.pop(ex, None)
                            lex[ex] = ep
                        continue
                    if ex not in past or ep - lex.get(ex, -1e18) < 6 * 3600:
                        continue
                    row = Row(ex, sh, b, a, past[ex][0], past[ex][1], float(p.get("bid_size") or 0),
                              float(p.get("ask_size") or 0))
                    side = poly_lead_signal(row, th, cfg.max_spread)
                    if side:
                        pr = held_prices(side, b, a)
                        e = {"event": "entry", "ts": ts, "epoch": ep, "id": _uuid.uuid4().hex[:10], "exchange_id": ex,
                             "lb_h": lb, "side": side, "entry_gap": round(row.gap, 4), "sharp": sh, "bid": b,
                             "ask": a, "bid_size": row.bid_size, "ask_size": row.ask_size, "mid_past": row.mid_24h,
                             "sharp_past": row.sharp_24h, "prices": pr,
                             "shares": round(cfg.size_usd / pr["taker_in"], 1)}
                        out.append(e)
                        book[ex] = e
            if out:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(_json.dumps(x) for x in out) + "\n")
            n += 1
            if out or n % 12 == 1:
                log.info("poly_lead_tick", events=len(out), open={str(lb): len(v) for lb, v in open_t.items()},
                         live=len(live))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — shadow must never affect trading
            log.warning("poly_lead_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _sharp_history_loop(*, store, xm_provider, stop_event, interval_s: float,
                              retention_days: float) -> None:
    """Record the SIG-aligned Polymarket benchmark (+ raw + SIG mid) for every cross-market race each interval
    into the durable sharp_history table. The provider only caches the LATEST quote and the venue keeps no
    history, so this is the only way to backtest anything against the sharp (exit thresholds, sniper, MM
    anchor). Cache reads + one local batch write; no venue or Polymarket calls of its own."""
    from datetime import datetime, timezone, timedelta
    interval_s = max(15.0, float(interval_s))
    prune_every = max(1, int(3600 / interval_s))
    i = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        try:
            ts = datetime.now(timezone.utc).isoformat()
            rows = [dict(r, ts=ts) for r in (xm_provider.sharp_snapshot() or [])]
            if rows:
                await store.append_sharp_history(rows)
            i += 1
            if retention_days > 0 and i % prune_every == 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=float(retention_days))).isoformat()
                await store.prune_sharp_history(cutoff)
            if i % 30 == 1:
                log.info("sharp_history_recorded", races=len(rows))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("sharp_history_failed", error=type(exc).__name__, msg=str(exc)[:160])



def _fill_yes_delta(fill: dict, action: str | None) -> float:
    """YES-space delta of a SIG fill. The venue fill has no action and its quantity sign follows the OUTCOME
    (YES +, NO -) for buys and sells alike, so the direction must come from our own order record (action captured
    at placement). Known action: buy YES +q, sell YES -q, buy NO -q, sell NO +q. Unknown: the venue's raw signed
    quantity (correct for buys, which is every pre-2026-10-11 order of the buy-only strategies)."""
    raw = float(fill.get("quantity", 0) or 0)
    q = abs(raw)
    outcome = str(fill.get("side", "")).lower()
    if action == "buy":
        return q if outcome == "yes" else -q
    if action == "sell":
        return -q if outcome == "yes" else q
    return raw

async def _fill_ingest_loop(*, store, client, stop_event, interval_s: float, broker=None) -> None:
    """Ingest venue fills into the CUMULATIVE strategy_fills ledger, tagged by strategy. The venue only
    returns the most-recent ~200 fills (history slides off), so without this we lose fill history and can
    only ever attribute a rolling window. Each fill is deduped by its venue id, tagged via the local orders
    table (broker_order_id -> strategy; 'UNATTRIBUTED' when the id wasn't captured), and snapped with the
    current mid (fills are <=interval old at ingest, so current mid ≈ mid-at-fill). One venue read/interval."""
    if client is None or not hasattr(client, "portfolio_fills"):
        return
    interval_s = max(30.0, float(interval_s))
    from datetime import datetime as _dtr, timezone as _tzr, timedelta as _tdr
    try:
        await store.resign_fills_from_actions()             # one full repair of fills signed before their action
    except Exception as exc:  # noqa: BLE001
        log.warning("fill_resign_failed", error=type(exc).__name__)
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        try:
            # DIRECTION RACE (2026-10-16): persist the broker's not-yet-flushed (order -> strategy, action) records
            # BEFORE reading the maps, so a fill can never be signed without its order's direction.
            if broker is not None and hasattr(broker, "drain_attribution"):
                try:
                    pend = broker.drain_attribution()
                    if pend:
                        await store.record_order_attribution(pend)
                except Exception as exc:  # noqa: BLE001
                    log.warning("fill_ingest_attr_flush_failed", error=type(exc).__name__)
            # PAGINATE back until we reach fills we already have (the venue returns only the newest page per call:
            # a burst of > 200 fills between polls used to be lost for good). Bounded to 10 pages per interval.
            newest_known = None
            try:
                newest_known = await store.latest_fill_time()
            except Exception:  # noqa: BLE001
                pass
            fills, cursor = [], None
            for _page in range(10):
                try:
                    resp = await asyncio.to_thread(client.portfolio_fills, None, 200, cursor)
                except TypeError:                              # test doubles without pagination
                    resp = await asyncio.to_thread(client.portfolio_fills)
                rows_p = resp.get("fills", resp.get("data", resp)) if isinstance(resp, dict) else resp
                rows_p = rows_p if isinstance(rows_p, list) else []
                fills.extend(rows_p)
                pag = (resp.get("pagination") or {}) if isinstance(resp, dict) else {}
                oldest = min((str(f.get("filledAt") or "") for f in rows_p), default="")
                if (not rows_p or not pag.get("hasMore") or not pag.get("nextCursor")
                        or (newest_known and oldest and oldest < str(newest_known))):
                    break
                cursor = pag.get("nextCursor")
            if not fills:
                continue
            # AUTHORITATIVE venue_order_id -> strategy, captured at the placement chokepoint (handles the
            # ~44% of MM orders whose broker_order_id never backfilled into `orders`). Orders table is the
            # fallback for any id predating the attribution capture; setdefault keeps attribution winning.
            omap = dict(await store.read_attribution_map())
            try:
                amap = dict(await store.read_attribution_actions())
            except Exception:  # noqa: BLE001
                amap = {}
            for row in await store._read(
                "SELECT broker_order_id, strategy FROM orders "
                "WHERE broker_order_id IS NOT NULL AND broker_order_id != ''"
            ):
                omap.setdefault(str(row["broker_order_id"]), row["strategy"])
            prices = await store.read_prices() or {}

            def _mid(ex):
                r = prices.get(str(ex))
                if isinstance(r, dict):
                    b, a = r.get("best_bid"), r.get("best_ask")
                    try:
                        if b is not None and a is not None:
                            return (float(b) + float(a)) / 2.0
                    except (TypeError, ValueError):
                        return None
                return None

            rows = []
            for f in fills:
                fid = f.get("id")
                if fid is None:
                    continue
                oid = str(f.get("orderId"))
                rows.append({
                    "fill_id": str(fid), "order_id": oid,
                    "strategy": omap.get(oid, "UNATTRIBUTED"),
                    "exchange_id": str(f.get("exchangeId")),
                    "outcome": (str(f.get("side", "")).lower() or None),
                    "price": float(f.get("price", 0) or 0),
                    "quantity": abs(float(f.get("quantity", 0) or 0)),
                    # SIGNED venue quantity (direction in the fill's own outcome) — the abs() above lost it,
                    # which made per-strategy realized P&L unreconstructable. YES-delta = ±signed_qty (yes/no).
                    "signed_qty": _fill_yes_delta(f, amap.get(oid)),
                    "filled_at": f.get("filledAt"),
                    "mid_at_fill": _mid(f.get("exchangeId")),
                })
            n = await store.append_strategy_fills(rows)
            if n:
                log.info("fills_ingested", new=n, seen=len(fills))
            try:
                await store.retag_unattributed_fills()          # late attribution flushes fix earlier tags
                await store.resign_fills_from_actions(          # ... and earlier directions
                    (_dtr.now(_tzr.utc) - _tdr(days=1)).isoformat())
            except Exception as exc:  # noqa: BLE001
                log.debug("fill_retag_failed", error=type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("fill_ingest_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _attribution_flush_loop(*, store, broker, stop_event, interval_s: float) -> None:
    """Drain the broker's in-memory (venue_order_id -> strategy) buffer to the durable order_attribution
    table. This is the authoritative fill-attribution link — SIG never echoes our idempotencyKey back, so
    the fill ledger (orderId only) can only be tied to a strategy via what we captured at placement. Cheap:
    one batched local write per interval; no venue calls. Best-effort / forward-only."""
    if broker is None or not hasattr(broker, "drain_attribution"):
        log.info("attribution_flush_not_started", reason="broker has no attribution buffer")
        return
    interval_s = max(5.0, float(interval_s))
    total = 0
    i = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        try:
            resolver = getattr(broker, "resolve_pending_legs", None)
            if callable(resolver):
                try:
                    await resolver()      # MM multi-leg leg ids → attribution (one open-orders read)
                except Exception as exc:  # noqa: BLE001
                    log.debug("leg_id_recovery_failed", error=type(exc).__name__)
            rows = broker.drain_attribution()
            if rows:
                n = await store.record_order_attribution(rows)
                total += n
            i += 1
            if i % 20 == 1:
                log.info("attribution_flushed", new_this_tick=(len(rows) if rows else 0), total_new=total)
        except asyncio.CancelledError:
            # flush whatever remains before exiting so a graceful stop loses ~nothing
            try:
                rows = broker.drain_attribution()
                if rows:
                    await store.record_order_attribution(rows)
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("attribution_flush_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _mm_shadow_loop(*, store, client, stop_event, exchange_ids, cfg: dict) -> None:
    """SHADOW MM research recorder — measures two candidate MM upgrades WITHOUT touching the live quoter:

      (1) vol-aware spread: realized vol (from the local mid_history we already collect) -> what half-width
          the quoter WOULD post if it scaled with vol (tighter when calm, wider when volatile) vs today's
          fixed k. Zero added venue load (local read only).
      (2) tape-based toxicity: recent taker imbalance on each market -> which resting side (bid/ask) informed
          flow is lifting, i.e. the side a real MM would pull/widen. Costs one tape read per market per tick,
          so it is gated OFF by default (toxicity_enabled) given this venue's 429 ceiling — turn on only when
          the read budget allows.

    We ONLY append rows to data/mm_shadow/decisions.jsonl. Nothing here changes a live order. The validator
    (tools/mm_shadow_validate.py) later joins these decisions against forward mid moves from mid_history to
    answer: would vol-scaling have cut adverse fills / raised fills, and did toxicity flags predict the move?
    """
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from strategies.market_making.vol_spread import VolSpreadConfig, realized_vol, vol_scaled_k
    from signals import trade_flow

    if not bool(cfg.get("enabled", False)):
        return
    ids = [str(x) for x in (exchange_ids or [])]
    if not ids:
        log.info("mm_shadow_not_started", reason="no markets")
        return

    interval_s = max(30.0, float(cfg.get("interval_s", 90.0)))
    window = int(cfg.get("window", 60))
    vcfg = VolSpreadConfig(
        base_k=float(cfg.get("base_k", 0.015)),
        vol_ref=float(cfg.get("vol_ref", 0.01)),
        k_min=float(cfg.get("k_min", 0.005)),
        k_max=float(cfg.get("k_max", 0.04)),
    )
    tox_on = bool(cfg.get("toxicity_enabled", False))
    tox_min_vol = int(cfg.get("tox_min_volume", 100))
    tox_thr = float(cfg.get("tox_threshold", 0.60))
    tape_limit = int(cfg.get("tape_limit", 50))
    path = str(cfg.get("record_path", "data/mm_shadow/decisions.jsonl"))
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)

    log.info("mm_shadow_started", markets=len(ids), interval_s=interval_s,
             toxicity=tox_on, base_k=vcfg.base_k, vol_ref=vcfg.vol_ref)
    i = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        try:
            prices = await store.read_prices() or {}
            ts = datetime.now(timezone.utc).isoformat()
            out = []
            for ex in ids:
                row = prices.get(ex)
                mid = None
                if isinstance(row, dict):
                    b, a = row.get("best_bid"), row.get("best_ask")
                    try:
                        if b is not None and a is not None:
                            mid = (float(b) + float(a)) / 2.0
                    except (TypeError, ValueError):
                        mid = None
                mids = await store.read_mid_history(ex, limit=window)
                vol = realized_vol(mids)
                vk = vol_scaled_k(vol, vcfg)
                rec = {
                    "ts": ts, "exchange_id": ex, "mid": mid,
                    "n_mids": len(mids), "vol": round(vol, 6),
                    "base_k": vcfg.base_k, "vol_k": round(vk, 6),
                    "imbalance": None, "tape_vol": None, "toxic_side": None,
                }
                if tox_on and client is not None and hasattr(client, "exchange_trades"):
                    try:
                        resp = await asyncio.to_thread(client.exchange_trades, ex, limit=tape_limit)
                        trades = (resp.get("data") if isinstance(resp, dict) else resp) or []
                        v = trade_flow.evaluate(trades, long_yes=True, min_volume=tox_min_vol)
                        rec["imbalance"] = v.imbalance
                        rec["tape_vol"] = v.total_vol
                        if v.imbalance is not None:
                            # heavy YES aggression lifts our ASK (we sell YES into a rising mkt) -> pull ask;
                            # heavy NO aggression hits our BID (we buy YES into a falling mkt) -> pull bid.
                            if v.imbalance >= tox_thr:
                                rec["toxic_side"] = "ask"
                            elif v.imbalance <= -tox_thr:
                                rec["toxic_side"] = "bid"
                    except Exception as exc:  # noqa: BLE001
                        log.debug("mm_shadow_tape_failed", ex=ex, error=type(exc).__name__)
                out.append(rec)
            if out:
                with open(path, "a", encoding="utf-8") as fh:
                    for r in out:
                        fh.write(_json.dumps(r) + "\n")
            i += 1
            if i % 10 == 1:
                widen = sum(1 for r in out if r["vol_k"] > r["base_k"] + 1e-9)
                tighten = sum(1 for r in out if r["vol_k"] < r["base_k"] - 1e-9)
                tox = sum(1 for r in out if r["toxic_side"])
                log.info("mm_shadow_recorded", markets=len(out), widen=widen,
                         tighten=tighten, toxic=tox)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("mm_shadow_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _snapshot_persistables(store, persistables: dict) -> int:
    """Save each persistable component's rolling-buffer snapshot to the durable KV store. Best-effort per
    component (one bad snapshot never blocks the others). Returns how many were saved."""
    saved = 0
    for name, strat in (persistables or {}).items():
        try:
            fn = getattr(strat, "snapshot", None)
            if not callable(fn):
                continue
            blob = fn()
            if blob:
                await store.set_kv("buf:" + name, blob)
                saved += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("state_snapshot_failed", strategy=name, error=type(exc).__name__)
    return saved


async def _restore_persistables(store, persistables: dict) -> None:
    """Rehydrate each persistable component's rolling buffer from the KV store on startup, so a restart no
    longer wipes the mid histories (the mr-skew 60-min window otherwise sits dormant for an hour)."""
    for name, strat in (persistables or {}).items():
        try:
            fn = getattr(strat, "restore", None)
            if not callable(fn):
                continue
            blob = await store.get_kv("buf:" + name)
            if blob:
                strat.restore(blob)
                log.info("state_restored", strategy=name)
        except Exception as exc:  # noqa: BLE001
            log.warning("state_restore_failed", strategy=name, error=type(exc).__name__)


async def _state_snapshot_loop(*, store, persistables: dict, stop_event, interval_s: float) -> None:
    """Periodically persist the rolling buffers so an UNCLEAN kill loses at most interval_s of history.
    A clean stop also flushes once more (see the shutdown path)."""
    interval_s = max(10.0, float(interval_s))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        await _snapshot_persistables(store, persistables)


async def _config_reload_loop(*, orch, strategies: dict, stop_event, interval_s: float) -> None:
    """Hot-reload the TUNABLE knobs when the config overlay changes on disk — NO restart, so nothing is
    wiped (the mr_skew buffer, position caches, in-flight state all survive). Watches the overlay mtime;
    on a change it re-loads config and pushes the hot-reloadable subset into the live objects:
      • risk-gate caps: max_deploy_frac / max_per_market_frac / max_cluster_frac
      • any strategy exposing a `reload(config)` method (e.g. the sniper: min_edge/max_cross/sweep/place)
    Knobs NOT covered here (poll intervals baked into running tasks, strategy registration, the sizer's
    kelly multipliers) still need a restart; everything applied here is logged so it is obvious what took.
    Best-effort and exception-walled: a bad/partial edit is caught and retried on the next change."""
    import os
    from sig.config import load_config
    watch = [p for p in ("config/sig.local.yaml", "config/sig.yaml") if os.path.exists(p)]

    def _mtimes() -> dict:
        out = {}
        for p in watch:
            try:
                out[p] = os.path.getmtime(p)
            except OSError:
                pass
        return out

    last = _mtimes()
    interval_s = max(3.0, float(interval_s))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        await asyncio.sleep(interval_s)
        cur = _mtimes()
        if cur == last:
            continue
        try:
            cfg = load_config()
            raw = cfg.raw
            gate = getattr(orch, "_risk_gate", None)
            inner = getattr(gate, "_gate", None)
            if inner is not None:
                cap = raw.get("capital", {}) or {}
                changed = {}
                for attr in ("max_deploy_frac", "max_per_market_frac", "max_cluster_frac", "min_order_qty"):
                    if attr in cap and hasattr(inner, attr):
                        nv = float(cap[attr])
                        if float(getattr(inner, attr)) != nv:
                            setattr(inner, attr, nv)
                            changed[attr] = nv
                if changed:
                    log.info("gate_caps_reloaded", **changed)
            for name, strat in (strategies or {}).items():
                fn = getattr(strat, "reload", None)
                if callable(fn):
                    try:
                        fn(raw)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("strategy_reload_failed", strategy=name, error=type(exc).__name__)
            last = cur  # commit only after a successful apply (a partial-write failure retries next change)
            log.info("config_reloaded", files=list(cur.keys()))
        except Exception as exc:  # noqa: BLE001 — a bad edit must never kill the loop or the trader
            log.warning("config_reload_failed", error=type(exc).__name__, msg=str(exc)[:160])


async def _poll_orderbooks(client, exchange_ids: list[str]) -> dict:
    """Fetch order-book depth for a bounded set of exchange ids off-thread. Best-effort per id: a
    failure yields no entry for that id (the strategy then skips it). Returns {exchange_id: book}."""
    async def _one(eid: str):
        try:
            return eid, await asyncio.to_thread(client.orderbook, eid)
        except Exception:
            return eid, None
    pairs = await asyncio.gather(*[_one(e) for e in exchange_ids])
    return {eid: bk for eid, bk in pairs if bk is not None}


# --------------------------------------------------------------------------- #
# feed — REST price poller (interim transport)
# --------------------------------------------------------------------------- #
async def cmd_feed(args: argparse.Namespace) -> int:
    from sig.config import load_config
    from infra.state.store import StateStore
    from sig.sig_client import SigClient
    from sig.feed.rest_poller import run_rest_price_poller
    from infra.systemd_notify import notify_ready, notify_stopping

    cfg = load_config()
    # Initialise the OPERATIONAL db (trades / system_events / operator_alerts / loop_heartbeats).
    # infra/state/state.db is a separate store and opening it does NOT create these tables, so the
    # trader ran with a 0-byte trading.db and no tables at all: every durable alert write failed.
    # arb_monitor.py has always done this; the trader never did. Idempotent, so it is safe on every
    # start. Best-effort: a trader that cannot create its ops log should still trade.
    try:
        from infra.persistence import db as _opsdb

        _opsdb.init_db()
        log.info("operational_db_ready", path=str(_opsdb.DB_PATH))
    except Exception as exc:                 # noqa: BLE001
        log.error("operational_db_init_failed", error=str(exc),
                  note="durable alert/ops rows will not persist this run")

    store = StateStore()
    await store.open()
    client = _make_read_client()
    exchange_ids = discover_exchange_ids(client, max_markets=args.max_markets)
    interval_s = float(cfg.loops.get("fast_loop_interval_s", 5.0))
    log.info("feed_starting", exchange_ids=len(exchange_ids), interval_s=interval_s)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _install_signal_handlers(loop, stop_event)
    notify_ready()

    # Transport selection: the Supabase Realtime WebSocket feed is now PRIMARY by default — it is built
    # from the POST /realtime/token response (supabaseUrl + anonKey + the user channel), no static env
    # vars. The REST poller is the SAFETY NET: feed.py already refetches the authoritative REST snapshot
    # on every (re)connect and gap, and if the WS cannot even start (token/tournament/market/connect
    # failure) we fall back to the REST poller here.
    slug = (cfg.global_.get("tournament_slug") or "").strip() or "midterm-elections"
    task = None
    rest_net = None
    try:
        from sig.feed.ws_adapter import build_realtime_feed
        tournament_id = _resolve_tournament_id(client, slug)
        if not tournament_id:
            raise RuntimeError(f"could not resolve tournament id for slug {slug!r}")
        # RETRIED: this call is now the source of EVERY market subscription, so a single transient
        # 503 here would drop the whole WS transport to the REST fallback.
        all_market_ids = [m["id"] for m in markets_all_retrying(client)
                          if isinstance(m, dict) and m.get("id")]
        # EVERY market is subscribed by default: Supabase caps channels-per-CONNECTION (~100), not
        # coverage, so _RealtimeSession shards the market channels across ceil(N/_WS_MAX_CHANNELS)
        # sockets. The old `all_market_ids[:90]` truncation left the other 147 markets subscribed to
        # nothing: they froze at their connect-snapshot price and the trader (correctly) skipped them
        # as stale, so the MM could only ever quote the ~90 live ones.
        #
        # feed.ws_max_markets is an ESCAPE HATCH, not the design: set it to fall back to one socket
        # if the sharded path ever regresses. Sharding IS proven against the live venue — 3 sockets
        # reached joins_ok=78/81/80 and all three began broadcasting ~67s after session open
        # (2026-10-02 07:39:23 -> 07:40:30). Leave it UNSET.
        ws_max_markets = (cfg.raw.get("feed", {}) or {}).get("ws_max_markets")
        market_ids = all_market_ids
        if ws_max_markets:
            market_ids = all_market_ids[:int(ws_max_markets)]
            log.warning("ws_market_subscription_capped", cap=int(ws_max_markets),
                        markets_total=len(all_market_ids),
                        dark=len(all_market_ids) - len(market_ids),
                        note="feed.ws_max_markets is set — the uncapped markets get NO price updates")
        feed = build_realtime_feed(
            store=store, client=client, config=cfg.raw, exchange_ids=exchange_ids,
            tournament_id=tournament_id, market_ids=market_ids,
            max_channels_per_connection=_WS_MAX_CHANNELS)
        import math
        log.info("feed_transport_ws", tournament_id=tournament_id, markets=len(market_ids),
                 markets_total=len(all_market_ids), ws_channels_per_socket=_WS_MAX_CHANNELS,
                 ws_sockets=math.ceil((len(market_ids) + 1) / _WS_MAX_CHANNELS),
                 note="Supabase Realtime (POST /realtime/token); SHARDED over several sockets for full "
                      "coverage; REST snapshot kept as resync/fallback")
        task = asyncio.create_task(feed.run(stop_event=stop_event))

        # ── REST SAFETY NET (not the engine) ──────────────────────────────────────────────────
        # WS is primary: push delivery is the freshest and costs nothing against the read budget.
        # But WS has blind windows — the ~67s cold-start subscribe, and any reconnect/venue flap —
        # during which a market receives nothing. A slow batch poll covers exactly those windows and
        # otherwise just acts as a sanity heartbeat that catches a market WS quietly stopped
        # updating. It writes to the SAME store, so strategies read one store and take whichever
        # source is fresher.
        #
        # Cost: 474 exchange ids / 50 per call = 10 reads per sweep, so a 60s sweep is ~10 reads/min
        # against the 100/min budget (10%). Deliberately slower than the MM's freshness cutoff, so a
        # market kept alive ONLY by REST flickers out of quotable rather than masquerading as live.
        net_s = float((cfg.raw.get("feed", {}) or {}).get("rest_safety_net_interval_s", 60.0))
        if net_s > 0:
            rest_net = asyncio.create_task(run_rest_price_poller(
                store=store, client=client, exchange_ids=exchange_ids, interval_s=net_s,
                max_ids_per_call=int(cfg.feed.snapshot_max_ids_per_call), stop_event=stop_event),
                name="rest_safety_net")
            log.info("rest_safety_net_started", interval_s=net_s, n_ids=len(exchange_ids),
                     reads_per_min=round(60.0 / net_s * (len(exchange_ids) / 50.0 + 0.999), 1),
                     note="covers the WS cold-start + reconnect windows; WS remains primary")
    except Exception as exc:
        log.warning("feed_ws_unavailable_falling_back_to_rest",
                    error=str(exc), error_type=type(exc).__name__)
        task = None
    try:
        if task is None:
            log.info("feed_transport_rest", note="REST poller fallback (WS could not start)")
            task = asyncio.create_task(run_rest_price_poller(
                store=store, client=client, exchange_ids=exchange_ids, interval_s=interval_s,
                max_ids_per_call=int(cfg.feed.snapshot_max_ids_per_call), stop_event=stop_event))
        await stop_event.wait()
        for t in (task, rest_net):
            if t is not None:
                t.cancel()
        for t in (task, rest_net):
            if t is None:
                continue
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        notify_stopping()
        await store.close()
    return 0


# --------------------------------------------------------------------------- #
# signal — dataless at launch (idle heartbeat only; places NO orders)
# --------------------------------------------------------------------------- #
async def cmd_signal(args: argparse.Namespace) -> int:
    from sig.config import load_config
    from infra.systemd_notify import notify_ready, notify_watchdog, notify_stopping

    load_config()
    log.warning("signal_idle", note="directional/tail signal sources are not wired (dataless) — "
                                     "running idle heartbeat; NO orders placed")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _install_signal_handlers(loop, stop_event)
    notify_ready()
    try:
        while not stop_event.is_set():
            notify_watchdog()
            log.info("signal_heartbeat", ts=_now_iso())
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
    finally:
        notify_stopping()
    return 0


# --------------------------------------------------------------------------- #
# supervisor — health monitor (feed staleness + heartbeat age)
# --------------------------------------------------------------------------- #
async def cmd_supervisor(args: argparse.Namespace) -> int:
    from sig.config import load_config
    from infra.state.store import StateStore
    from infra.systemd_notify import notify_ready, notify_watchdog, notify_stopping

    cfg = load_config()
    # Initialise the OPERATIONAL db (trades / system_events / operator_alerts / loop_heartbeats).
    # infra/state/state.db is a separate store and opening it does NOT create these tables, so the
    # trader ran with a 0-byte trading.db and no tables at all: every durable alert write failed.
    # arb_monitor.py has always done this; the trader never did. Idempotent, so it is safe on every
    # start. Best-effort: a trader that cannot create its ops log should still trade.
    try:
        from infra.persistence import db as _opsdb

        _opsdb.init_db()
        log.info("operational_db_ready", path=str(_opsdb.DB_PATH))
    except Exception as exc:                 # noqa: BLE001
        log.error("operational_db_init_failed", error=str(exc),
                  note="durable alert/ops rows will not persist this run")

    store = StateStore()
    await store.open()
    stale_after = float(cfg.feed.price_stale_after_s)
    poll_s = float(cfg.loops.get("slow_loop_interval_s", 300.0)) if False else 30.0

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _install_signal_handlers(loop, stop_event)
    notify_ready()
    log.info("supervisor_starting", stale_after_s=stale_after)
    try:
        while not stop_event.is_set():
            try:
                stale = await store.stale_exchanges(stale_after)
                log.info("supervisor_health", stale_exchanges=len(stale), ts=_now_iso())
                if stale:
                    log.warning("supervisor_stale_prices", n=len(stale), sample=stale[:5])
            except Exception as exc:
                log.warning("supervisor_check_error", error=type(exc).__name__, msg=str(exc)[:160])
            notify_watchdog()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_s)
            except asyncio.TimeoutError:
                pass
    finally:
        notify_stopping()
        await store.close()
    return 0


# --------------------------------------------------------------------------- #
# reconcile / post-settlement — one OMS reconciliation pass (timer jobs)
# --------------------------------------------------------------------------- #
async def _reconcile_once(tag: str) -> int:
    from sig.config import load_config
    from infra.state.store import StateStore

    cfg = load_config()
    if cfg.mode != "LIVE":
        log.info("reconcile_skipped_sim", tag=tag)
        return 0

    from sig.sig_client import SigClient
    from sig.engine.composition import build_live_stack

    # Initialise the OPERATIONAL db (trades / system_events / operator_alerts / loop_heartbeats).
    # infra/state/state.db is a separate store and opening it does NOT create these tables, so the
    # trader ran with a 0-byte trading.db and no tables at all: every durable alert write failed.
    # arb_monitor.py has always done this; the trader never did. Idempotent, so it is safe on every
    # start. Best-effort: a trader that cannot create its ops log should still trade.
    try:
        from infra.persistence import db as _opsdb

        _opsdb.init_db()
        log.info("operational_db_ready", path=str(_opsdb.DB_PATH))
    except Exception as exc:                 # noqa: BLE001
        log.error("operational_db_init_failed", error=str(exc),
                  note="durable alert/ops rows will not persist this run")

    store = StateStore()
    await store.open()
    try:
        client = _make_read_client()
        exchange_ids = discover_exchange_ids(client)
        strategy_contracts = _build_strategy_contracts(exchange_ids)
        slug = str((cfg.global_ or {}).get("tournament_slug") or "").strip() or "midterm-elections"
        try:
            tournament_id = await asyncio.to_thread(_resolve_tournament_id, client, slug)
        except Exception:
            tournament_id = None
        _orch, _broker, _alloc, _state, oms = build_live_stack(
            config=cfg.raw, strategy_contracts=strategy_contracts, store=store,
            tournament_id=tournament_id)
        result = await oms.startup()
        hard = bool(getattr(result, "hard_locked", False))
        log.info("reconcile_done", tag=tag, hard_locked=hard,
                 reason=getattr(result, "lock_reason", ""))
        try:
            await store.append_audit(f"{tag}", severity=("error" if hard else "info"),
                                     component="reconcile", payload={"hard_locked": hard})
        except Exception:
            pass
        return 1 if hard else 0
    finally:
        await store.close()


async def cmd_reconcile(args: argparse.Namespace) -> int:
    return await _reconcile_once("reconcile")


async def cmd_post_settlement(args: argparse.Namespace) -> int:
    return await _reconcile_once("post_settlement")


# --------------------------------------------------------------------------- #
# pre-event-check — pre-open readiness probe (timer job)
# --------------------------------------------------------------------------- #
async def cmd_pre_event_check(args: argparse.Namespace) -> int:
    from sig.config import load_config

    cfg = load_config()  # LIVE => required secrets must be present (fails loud)
    if cfg.mode != "LIVE":
        log.info("pre_event_check_sim", note="mode is not LIVE — probe skipped")
        return 0

    client = _make_read_client()
    failures: list[str] = []
    try:
        acct = await asyncio.to_thread(client.account)
        if not acct:
            failures.append("account_empty")
    except Exception as exc:
        failures.append(f"account:{type(exc).__name__}")
    try:
        mkts = await asyncio.to_thread(client.markets)
        rows = (mkts or {}).get("data", []) if isinstance(mkts, dict) else mkts
        if not rows:
            failures.append("markets_empty")
    except Exception as exc:
        failures.append(f"markets:{type(exc).__name__}")

    if failures:
        log.error("pre_event_check_FAIL", failures=failures)
        return 1
    log.info("pre_event_check_OK", ts=_now_iso())
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main_sig", description="sig-bot live entrypoint")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="the capital-arbiter trading process")
    pr.add_argument("--max-cycles", type=int, default=0,
                    help="run N cycles then exit (SIM smoke-test); 0 = run forever")
    pr.add_argument("--max-markets", type=int, default=0,
                    help="cap the number of discovered markets to track/quote (0 = no cap)")
    pr.add_argument("--equity", type=float, default=100_000.0,
                    help="fallback starting equity if the venue account read fails")
    pr.add_argument("--self-feed", action="store_true",
                    help="also REST-poll prices in-process (single-process mode; default off — the "
                         "feed process is the price writer)")
    pr.set_defaults(func=cmd_run)

    pf = sub.add_parser("feed", help="REST price poller -> StateStore")
    pf.add_argument("--max-markets", type=int, default=0)
    pf.set_defaults(func=cmd_feed)

    sub.add_parser("signal", help="dataless idle heartbeat (no orders)").set_defaults(func=cmd_signal)
    sub.add_parser("supervisor", help="health monitor").set_defaults(func=cmd_supervisor)
    sub.add_parser("reconcile", help="one OMS reconciliation pass").set_defaults(func=cmd_reconcile)
    sub.add_parser("pre-event-check", help="pre-open readiness probe").set_defaults(func=cmd_pre_event_check)
    sub.add_parser("post-settlement", help="post-settlement reconciliation").set_defaults(func=cmd_post_settlement)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # normalize for --max-markets default 0 -> None
    if hasattr(args, "max_markets"):
        args.max_markets = args.max_markets or None
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        log.warning("interrupted", note="holding positions")
        return 0


if __name__ == "__main__":
    sys.exit(main())
