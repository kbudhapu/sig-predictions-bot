"""
SIG Predictions Cup Orchestrator — the conductor that wires every module into a running bot.

Architecture: ONE trading process, one asyncio loop, unified cadence-gated cycle.

Ported from mbappe src/engine/orchestrator.py:
  - run_forever() shape: while-True + interrupt_event tick wait (event-driven preemption)
  - _due_strategy_legs() cadence-gating pattern → StrategyRegistry.due()
  - ReservedCapitalLedger.begin_cycle / reserve sequential sizing → CapitalLedger
  - Error isolation: catch-at-cycle-boundary, SOFT_DEGRADE on first failure, page on streak=3
  - startup gating: reconcile-hard-gate (pre-flight False → loop never starts)
  - graceful shutdown HOLDS positions (no auto-flatten)
  - off-loop dead-man: SystemHeartbeatMonitor on its OWN thread (not on the event loop)
  - SystemHeartbeatMonitor → ported shape from mbappe src/control/alerts.py

Stripped from mbappe:
  - 60s/900s cadence tied to 15-min equity bars (SIG uses write-budget-aware tick)
  - Alpaca WebSocket / SIP feed (replaced by SIG realtime feed)
  - WAL→Supabase mirror (local SQLite only)
  - BOD/EOD email (no market-open schedule)
  - ET-timezone maintenance cron (24/7 prediction market, no market-close boundary)

Spec reference: docs/specs/SIG_ORCHESTRATOR_SPEC.md
"""

from __future__ import annotations

import asyncio
try:
    import fcntl  # POSIX-only; the flock single-instance guard degrades to a no-op without it
except ImportError:  # e.g. Windows dev boxes — the Linux droplet always has fcntl
    fcntl = None  # type: ignore[assignment]
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from sig.engine.capital_ledger import CapitalLedger
from sig.engine.protocols import (
    Allocator,
    Intent,
    OrchestratorBroker,
    OrchestratorOMS,
    OrchestratorState,
    RiskGate,
    RiskGateVerdict,
    Sizing,
    SizingResult,
    Strategy,
)
from sig.engine.strategy_registry import StrategyRegistry
from sig.engine.write_gate import WriteGate

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Tick / failure constants (ported from mbappe — adapted for 24/7 prediction mkt)
# ---------------------------------------------------------------------------

DEFAULT_TICK_SECONDS = 5.0          # write-budget-aware; config overrides this
CONSECUTIVE_FAILURE_PAGE_THRESHOLD = 3
DEAD_MAN_CYCLE_MULTIPLIER = 2       # ported from mbappe alerts.py
DEFAULT_HEARTBEAT_POLL_SECONDS = 30.0
LOCK_FILE_PATH = "/tmp/sig-bot.lock"

# ---------------------------------------------------------------------------
# Single-instance lock (ported from mbappe cmd_run acquire_run_lock)
# ---------------------------------------------------------------------------


class RunLockError(RuntimeError):
    pass


class _RunLock:
    """flock-based single-instance guard. Ported from mbappe acquire_run_lock."""

    def __init__(self, path: str = LOCK_FILE_PATH) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:   # non-POSIX (e.g. Windows): flock unavailable → best-effort no-op guard
            log.warning("run_lock_unavailable_no_fcntl", path=self._path)
            self._fd = None
            return
        self._fd = os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise RunLockError(
                f"Another sig-bot instance is already running (lock: {self._path})"
            )

    def release(self) -> None:
        if self._fd is not None and fcntl is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "_RunLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Off-loop dead-man watchdog thread (ported from mbappe SystemHeartbeatMonitor)
# ---------------------------------------------------------------------------


class _DeadManWatchdog:
    """
    Runs on its OWN OS thread (not the event loop) so a wedged loop still pages.

    Ported shape from mbappe src/control/alerts.py SystemHeartbeatMonitor.
    The watchdog reads the heartbeat timestamp from state (written each cycle)
    and fires the alert callback if the age exceeds threshold.
    """

    def __init__(
        self,
        *,
        cycle_interval_seconds: float,
        get_heartbeat_age_seconds: Any,   # callable() → float
        on_stale: Any,                    # callable(age) → None
        check_interval_seconds: float = DEFAULT_HEARTBEAT_POLL_SECONDS,
        stale_threshold_seconds: float | None = None,
    ) -> None:
        self._cycle_interval = cycle_interval_seconds
        self._get_age = get_heartbeat_age_seconds
        self._on_stale = on_stale
        self._check_interval = check_interval_seconds
        # The heartbeat beats ONCE PER CYCLE (run_forever, after _run_cycle returns), so the
        # threshold has to cover the worst legitimate CYCLE — not the tick interval. Deriving it as
        # tick x 2 assumed a cycle costs about one tick; it does not. A cycle that places orders is
        # paced by the VENUE, which serialises writes at ~4.5-8.7s each, so a busy cycle legitimately
        # runs to ~160s (measured: p50 2.8s, max 161.3s). At tick=5s the threshold was 10s and the
        # watchdog fired continuously — a dead-man that always cries wolf is not a safety device.
        self._threshold = (float(stale_threshold_seconds) if stale_threshold_seconds
                           else cycle_interval_seconds * DEAD_MAN_CYCLE_MULTIPLIER)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sig-bot-deadman"
        )
        self._thread.start()
        log.info("dead_man_watchdog_started", threshold_seconds=self._threshold)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._check_interval):
            try:
                age = self._get_age()
                if age > self._threshold:
                    log.error(
                        "dead_man_heartbeat_stale",
                        age_seconds=age,
                        threshold=self._threshold,
                    )
                    self._on_stale(age)
            except Exception as exc:
                log.warning("dead_man_watchdog_error", error=str(exc))


# ---------------------------------------------------------------------------
# Order path helper
# ---------------------------------------------------------------------------


# Directional trailing-stop / stop-loss exit for HELD directional-type positions (incl. market-making
# DRIFT — a quote that filled and the market ran). Distinct from arb baskets (hold-to-settlement).
#
# A fixed take-profit is GONE: it CAPPED winners (a +453% runner sold at +100% leaves +353% on the
# table). Instead a TRAILING STOP lets a winner run as its PEAK climbs and sells only on a pullback
# FROM THE PEAK, plus a hard stop-loss for outright losers. Thresholds are per-Orchestrator, from
# sig.yaml `directional_exit:` (self._exit_trail_arm_pct / _exit_trail_pct / _exit_stop_loss_pct).
_VENUE_POS_SYNC_INTERVAL_S = 20.0   # how often to pull venue-truth positions into state (visibility)


def _signed_qty(p: object) -> float:
    """Signed net quantity from a BrokerPositionSnapshot: +long, -short.

    The snapshot splits magnitude (`qty`, always >= 0) from direction (`side`). Persisted state and
    every consumer of it key off a single signed `quantity`, so the two must be recombined here or
    a short is indistinguishable from a long.
    """
    try:
        q = abs(float(getattr(p, "qty", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return -q if str(getattr(p, "side", "")).lower() == "short" else q


def _directional_exit_decision(
    pnl_frac: float,
    peak_frac: float,
    *,
    trail_arm_pct: float,
    trail_pct: float,
    stop_loss_pct: float,
) -> str | None:
    """Trailing-stop / stop-loss verdict for ONE held directional position. None ⇒ HOLD.

    Centralised so BOTH exit paths (venue-valued and the state/local fallback) share ONE rule and can
    never drift — that drift previously left the fallback on a fixed +100% take-profit.

    `pnl_frac` is the current unrealized gain as a fraction of entry; `peak_frac` is the HIGHEST
    `pnl_frac` reached since entry (the caller tracks it across cycles in `self._pos_peak`).

      • STOP-LOSS (checked first, so a hard reversal is labelled `stop_loss`): `pnl_frac <= -stop_loss_pct`.
      • TRAILING STOP: only once ARMED, i.e. the PEAK reached `trail_arm_pct` (a barely-green position
        is not shaken out on noise; arming keys off the peak, so it stays armed after a pullback).
        Armed, sell when the gain gives back `trail_pct` OF THE PEAK:
        `pnl_frac <= peak_frac * (1 - trail_pct)` (peak +453%, trail 0.2 ⇒ sell below +362.4%). While
        the position climbs, the peak ratchets up and the trigger trails up with it — the winner RUNS
        and is sold only on a genuine pullback, never capped at a fixed level, never bled back to zero.
    """
    if pnl_frac <= -abs(stop_loss_pct):
        return "stop_loss"
    if peak_frac >= trail_arm_pct and pnl_frac <= peak_frac * (1.0 - trail_pct):
        return "trailing_take_profit"
    return None


# How far THROUGH the held leg's mark a close undercuts to be MARKETABLE (hit a resting buyer). The
# venue path has only the mark (current_price), not the touch, so selling AT the mark rested unmatched
# in the peer-to-peer book (filled_fraction=0.0). A bounded undercut (4 ticks) clears the observed
# 0.005-0.02 spreads while capping give-up; qty = held ⇒ reduce-only, so it can never flip or over-sell.
_EXIT_CROSS = 0.02


def _snap_tick(price: float, *, tick: float = 0.005, lo: float = 0.005, hi: float = 0.995) -> float:
    """Snap a price to the SIG 0.005 tick and clamp to [0.005, 0.995]. The buy-to-cancel close price is
    the complement of the mark (1 − price); rounded only to 1e-3 it is usually OFF the 0.005 tick (e.g.
    1 − 0.671244 = 0.329), and the venue rejects an off-tick limit (400 VALIDATION_ERROR) — so the stop
    DECIDES to cut but the close order bounces and the loser is never actually cleared. Snapping fixes
    the close so the cut lands."""
    snapped = round(round(float(price) / tick) * tick, 3)
    return min(max(snapped, lo), hi)


def _intent_priority(intent: Any) -> float:
    """Expected-value-per-order ranking for the scarce placement pipe (~10-13 orders/min). HIGHER =
    placed first. Tiered by role, refined by the edge/size the intent carries, so a flooding low-value
    strategy (MM's 237 quotes) can never bury a high-value edge (a dutch-book arb or a big cross-market
    divergence) behind it until it goes stale. This is the whole fix: the tiny pipe goes to the best buys.

      exits (free capital / cut risk)  >  dutch-book arb  >  relationship arb  >  cross-market divergence
      >  order-flow  >  mean-reversion  >  MM new quote  >  MM re-quote
    """
    md = intent.metadata or {}
    st = str(getattr(intent, "strategy_type", "") or "")
    sid = str(getattr(intent, "strategy_id", "") or "")
    strength = float(getattr(intent, "signal_strength", 0.0) or 0.0)
    try:
        if getattr(intent, "action", "") == "close" or sid.startswith("exit:"):
            return 10000.0 + abs(float(md.get("pnl_pct", 0.0) or 0.0))
        if md.get("dutch_book"):
            return 9000.0 + 1000.0 * float(md.get("edge", 0.0) or 0.0)
        if st == "arb":
            return 8500.0
        if sid == "sniper":   # confirmed-fillable take of a stale quote — above a resting cross-market bet
            return 8200.0 + 1000.0 * abs(float(md.get("snipe_edge", 0.0) or 0.0))
        if sid == "cross_market_divergence":
            return 8000.0 + 1000.0 * abs(float(md.get("divergence", 0.0) or 0.0)) + 10.0 * strength
        if sid == "order_flow":
            return 6000.0 + 100.0 * float(md.get("of_confidence", 0.0) or 0.0)
        if sid == "mean_reversion":
            return 5000.0 + abs(float(md.get("mr_z", 0.0) or 0.0))
        if st == "market_making":
            # baseline spread capture; a re-quote (cancel-replace) ranks below a brand-new quote
            # Rank by DOLLAR size, not share count: share count made every placed quote a $3 0.05-longshot
            # (cheap markets have the most shares per dollar) and starved the rest of the board.
            base = 50.0 if md.get("mm_requote") else 100.0
            notional = float(md.get("mm_quote_qty", 0.0) or 0.0) * float(getattr(intent, "price", 0.0) or 0.0)
            return base + 0.01 * min(notional, 4000.0)
        return 1000.0
    except Exception:                                    # noqa: BLE001 — ranking must never break the drain
        return 1000.0


def _snap_to_tick(price: float) -> float:
    """Snap to SIG's 0.005 grid, clamped to [0.005, 0.995].

    Multi-leg validates EVERY leg before executing any of them, and one off-tick price returns 400
    with nothing placed (verified live: index 4 off-tick -> 0 orders). So a group must be snapped
    before it is sent, or one bad price costs the whole group.
    """
    p = min(max(float(price), 0.005), 0.995)
    return round(round(p / 0.005) * 0.005, 3)


def _build_leg(intent: Intent, qty: float) -> dict[str, Any]:
    return {
        "contract_id": intent.exchange_id,
        "side": intent.side,                               # DIRECTION (buy/sell) → SIG action
        "qty": qty,
        "leg_id": intent.exchange_id,
        "order_type": "limit",
        "price": intent.price,
        # OUTCOME (yes/no) → SIG OrderInput.side. MM carries it in metadata["option_side"].
        "outcome": (intent.metadata or {}).get("option_side"),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """
    One trading process, one asyncio loop, unified cadence-gated cycle.

    Usage::
        orch = Orchestrator(broker=broker, state=state, oms=oms, ...)
        await orch.run_forever()
    """

    def __init__(
        self,
        *,
        broker: OrchestratorBroker,
        state: OrchestratorState,
        oms: OrchestratorOMS,
        risk_gate: RiskGate,
        sizing: Sizing,
        allocator: Allocator,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        environment: str = "sim",
        lock_path: str = LOCK_FILE_PATH,
        heartbeat_poll_seconds: float = DEFAULT_HEARTBEAT_POLL_SECONDS,
        on_dead_man: Any | None = None,  # callable(age) fired when heartbeat stale
        price_stale_after_s: float = 15.0,  # feed-staleness threshold (config safety.stale_data_halt_s)
        exit_trail_arm_pct: float = 0.5,    # directional_exit: arm the trailing stop at +50% peak
        exit_trail_pct: float = 0.2,        # directional_exit: sell on a 20%-of-peak give-back
        exit_stop_loss_pct: float = 0.5,    # directional_exit: cut a loser at -50%
        exit_check_interval_s: float = 45.0,  # directional_exit: stop/trail check cadence (own timer)
        dead_man_threshold_s: float | None = None,  # None → tick x DEAD_MAN_CYCLE_MULTIPLIER
        placement_worker: bool = False,      # OFF by default: the in-cycle path is unchanged
        placement_queue_max: int = 2000,
        placement_intent_ttl_s: float = 30.0,
        placement_batch_max_legs: int = 0,   # 0 = OFF (one venue call per intent, as before)
        placement_max_per_drain: int = 20,   # cap intents placed per drain (value-sorted); rest re-propose
        capital_edge_reserve: float = 0.0,   # >0 => POOLED capital: $ floor only pool-eligible edges may use
        capital_pool_eligible: tuple = (),   # strategy_ids allowed to touch the edge reserve
        mm_cash_reserve_frac: float = 0.0,   # >0 => slice of NAV (in free cash) only market_making may use
        mm_requote_threshold: float = 0.05, # cancel-replace an MM quote once it drifts >5% from the touch
        exit_config: Any | None = None,     # strategies.exit.ExitConfig; None → exits disabled
        exit_books_provider: Any | None = None,  # callable(list[exchange_id]) → {id: orderbook}
        best_alternative_edge_provider: Any | None = None,  # callable() → float (fraction)
        max_exits_per_cycle: int = 1,
    ) -> None:
        self._broker = broker
        self._state = state
        self._oms = oms
        self._risk_gate = risk_gate
        self._sizing = sizing
        self._allocator = allocator
        self._tick_seconds = tick_seconds
        self._environment = environment
        self._lock_path = lock_path
        self._price_stale_after_s = float(price_stale_after_s)
        self._stale_markets: set[str] = set()   # exchanges flagged stale THIS cycle (no new orders on them)

        self._registry = StrategyRegistry()
        self._ledger = CapitalLedger()
        self._write_gate = WriteGate()

        self._interrupt: asyncio.Event | None = None
        self._halt = asyncio.Event()          # set → stop accepting new intents
        self._halt_reason = ""

        self._consecutive_failures = 0
        self._last_heartbeat_at = time.monotonic()

        # --- exit / capital recycling (see _run_exits) ---
        # exit_config=None disables the exit pass entirely, so an orchestrator built without it
        # behaves exactly as before. Depth and the alternative edge both default to "unknown",
        # which the decision module reads as HOLD.
        self._last_pos_sync = 0.0           # monotonic ts of last venue-position sync
        # Account snapshot cache + 429-tolerance. get_account() runs at the TOP of every cycle; polling
        # it per-cycle is a large slice of the ~100/min SIG read budget AND a transient 429 there used to
        # fail the WHOLE cycle (→ dead-man → crash-loop). Cache it for a short TTL (equity/BP move slowly)
        # and, on a read error, REUSE the last-known snapshot instead of failing the cycle.
        self._account_cache: Any = None
        self._account_cache_ts = 0.0
        self._account_cache_ttl_s = 20.0
        # Directional take-profit-on-REVERSAL (trailing) + stop-loss for held positions (incl. MM drift).
        # arm the trail once the PEAK gain >= trail_arm_pct; then SELL when the current gain gives back
        # trail_pct OF THE PEAK (so a still-climbing winner is HELD, only a real pullback sells); cut an
        # outright loser at -stop_loss_pct. Peak is tracked per exchange_id across cycles.
        self._exit_trail_arm_pct = float(exit_trail_arm_pct)
        self._exit_trail_pct = float(exit_trail_pct)
        self._exit_stop_loss_pct = float(exit_stop_loss_pct)
        # EXIT CADENCE. The stop threshold was always correct, but the pass only ran once per
        # trading CYCLE -- and a cycle takes ~47 minutes because the MM places/cancels hundreds of
        # orders at 5-14s per API call. A 20% stop evaluated every 47 minutes behaves like a ~30%
        # stop: observed live, a position sat at -30.1% until the next pass came round. So the pass
        # also runs on its OWN fast timer: it needs one get_positions_valued() call plus pure
        # arithmetic, and nothing about it depends on the MM-heavy cycle.
        self._exit_check_interval_s = max(float(exit_check_interval_s), 1.0)
        # ONE pass at a time. The fast timer and the in-cycle call share this, so they cannot both
        # be deciding on the same position concurrently and submit two closes for it.
        self._exit_pass_lock: asyncio.Lock | None = None
        self._exit_task: asyncio.Task | None = None
        # Optional per-exchange EXIT OVERRIDE for slow convergence edges (cross-market): a callable
        # (exchange_id, pnl_frac, is_long) -> (handled, reason). When it handles a position, its verdict
        # REPLACES the default price stop for that position (thesis-based: hold while the sharp benchmark
        # backs us, take profit on convergence, cut only on a WIDE backstop). MM/other positions are not
        # handled → they keep the default price stop. Set via register_directional_exit_override().
        self._directional_exit_override: Callable[[str, float, bool], tuple[bool, str | None]] | None = None
        self._mm_conflict_source: Any | None = None   # callable() → set of exchanges MM must vacate
        self._mm_requote_threshold = float(mm_requote_threshold)
        # resting maker entries hold no venue cash (SIG does not reserve it): carve their notional out of the
        # buying power every strategy sizes against, so concurrent resting entries cannot double-commit cash.
        _mrn = getattr(self._oms, "maker_resting_notional", None)
        if callable(_mrn):
            # resolved maker entries count as spent until an account read NEWER than their resolution
            self._ledger.external_reserves["maker_resting"] = (
                lambda: _mrn(since_mono=getattr(self, "_account_cache_ts", None)))
        self._pos_peak: dict[str, float] = {}
        self._exit_cfg = exit_config
        self._exit_books_provider = exit_books_provider
        self._best_alt_edge_provider = best_alternative_edge_provider
        self._best_alt_edge = 0.0
        self._max_exits_per_cycle = max(int(max_exits_per_cycle), 0)

        # ── PLACEMENT WORKER (off by default) ───────────────────────────────────────────────────
        # The cycle used to place every intent inline: `for intent in intents: await
        # _process_intent(...)`, unbounded. With 237 quotable markets one cycle could issue 237+
        # venue round-trips, which is where the 60-160s (historically ~47min) cycles came from, and
        # exits/directional queued behind the whole MM burst.
        #
        # With the worker on, the cycle only EVALUATES and enqueues, so it is compute-only, and a
        # single sequential worker drains the queue at venue pace.
        #
        # THE CAPITAL INVARIANT, and why the WORKER owns begin_cycle. The no-double-spend guarantee
        # is "reserve sequentially against ONE snapshot", and begin_cycle() RESETS all reservations.
        # Leaving reserve in the cycle while submitting off-cycle would let the next cycle's
        # begin_cycle wipe reservations with submits still in flight. So the worker performs the
        # WHOLE path (size → gate → reserve → OMS → broker) and calls begin_cycle itself, once per
        # drain batch. The worker is then the ONLY reserver and it is strictly sequential, so the
        # guarantee holds by construction — the unit of capital accounting just becomes the drain
        # batch instead of the cycle. Verified safe: nothing in any strategy's evaluate() reads the
        # ledger (all ledger access is inside _process_intent and the exit path).
        self._placement_worker_enabled = bool(placement_worker)
        self._placement_intent_ttl_s = max(float(placement_intent_ttl_s), 0.0)
        self._placement_q: asyncio.Queue = asyncio.Queue(maxsize=max(int(placement_queue_max), 1))
        self._placement_dropped_full = 0
        self._placement_dropped_stale = 0
        # MM-quote batching via POST /orders/multi-leg. 0 disables it.
        # Measured against the live venue 2026-10-02: 10 independent legs in ONE call took 1.47s
        # (0.15s/order) vs 0.53s/order for serial singles — but the speed is NOT the point. The real
        # constraint is the 30 writes/min budget, and multi-leg spends ONE write for up to ten
        # orders, so the same budget carries ~10x the quotes.
        # SIG caps multi-leg at 10 legs and rejects duplicate legs for the same exchange.
        self._placement_batch_max_legs = max(0, min(int(placement_batch_max_legs), 10))
        self._placement_max_per_drain = max(1, int(placement_max_per_drain))
        self._capital_edge_reserve = max(0.0, float(capital_edge_reserve))
        self._capital_pool_eligible = set(capital_pool_eligible or ())
        # MM CASH RESERVE (pooled mode). Pool-eligible edges may take ALL free cash, which starved MM to ~$28
        # quote groups. This carves a NAV-fraction of free cash that ONLY market_making may spend: edges stop
        # short of it, MM may use it plus whatever sits above the edge reserve. A fraction (never absolute $).
        self._mm_cash_reserve_frac = max(0.0, float(mm_cash_reserve_frac))

        # off-loop dead-man watchdog
        self._dead_man_threshold_s = (float(dead_man_threshold_s) if dead_man_threshold_s
                                      else tick_seconds * DEAD_MAN_CYCLE_MULTIPLIER)
        self._watchdog = _DeadManWatchdog(
            cycle_interval_seconds=tick_seconds,
            get_heartbeat_age_seconds=self._heartbeat_age,
            on_stale=on_dead_man or self._default_dead_man_alert,
            check_interval_seconds=heartbeat_poll_seconds,
            stale_threshold_seconds=self._dead_man_threshold_s,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    MAX_CONCURRENT_MAKER = 5

    def _oms_has_makers(self, strategy_id: str) -> bool:
        fn = getattr(self._oms, "maker_bundles", None)
        return bool(fn(strategy_id)) if callable(fn) else False

    def register_strategy(self, strategy: Strategy) -> None:
        self._registry.register(strategy)
        log.info("strategy_registered", name=strategy.name, tier=strategy.tier,
                 cadence_s=strategy.cadence_s)

    def register_directional_exit_override(
        self, fn: "Callable[[str, float, bool], tuple[bool, str | None]]") -> None:
        """Install a per-exchange exit override for slow convergence edges (cross-market). `fn(exchange_id,
        pnl_frac, is_long)` returns (handled, reason); when handled it REPLACES the default price stop for
        that position only. See `_directional_exit_override`."""
        self._directional_exit_override = fn
        log.info("directional_exit_override_registered")

    def register_mm_conflict_source(self, fn: "Callable[[], set[str]]") -> None:
        """`fn()` → exchanges where MM must not have resting orders (cross races where cross's position
        exceeds MM's inventory cap). _run_mm_requote cancels MM's resting orders there each cycle."""
        self._mm_conflict_source = fn
        log.info("mm_conflict_source_registered")

    async def _non_mm_order_ids(self) -> set[str]:
        """Venue order ids KNOWN to belong to a non-MM strategy — never cancel these as 'MM' orders.
        Sources: in-flight OMS bundles of non-MM strategies, the durable order_attribution table, and the
        broker's not-yet-flushed attribution buffer. MM multi-leg legs carry no venue id, so they are never
        in this set (that is exactly what makes them identifiable as MM)."""
        ids: set[str] = set()
        for b in list((getattr(self._oms, "_pending", None) or {}).values()):
            if str(getattr(b, "strategy_id", "")) == "market_making":
                continue
            for sl in getattr(b, "slices", []) or []:
                if getattr(sl, "order_id", None):
                    ids.add(str(sl.order_id))
        for rec in list(getattr(self._broker, "_attr_buf", []) or []):
            oid, strat = rec[0], rec[1]                  # (oid, strategy, exchange[, action]) — 3 or 4 fields
            if strat != "market_making":
                ids.add(str(oid))
        fn = getattr(self._state, "read_attribution_map", None)
        if callable(fn):
            try:
                for oid, strat in ((await fn()) or {}).items():
                    if strat != "market_making":
                        ids.add(str(oid))
            except Exception:  # noqa: BLE001
                pass
        return ids

    def _mm_reserve_dollars(self) -> float:
        """market_making's cash carve-out in $ = mm_cash_reserve_frac × NAV (this snapshot's equity)."""
        if getattr(self, "_mm_cash_reserve_frac", 0.0) <= 0.0:
            return 0.0
        snap = getattr(self._ledger, "_snapshot", None)
        eq = float(getattr(snap, "equity", 0.0) or 0.0) if snap is not None else 0.0
        return self._mm_cash_reserve_frac * max(eq, 0.0)

    def _pooled_budget(self, strategy_id: str, available_bp: float) -> float:
        """Spendable cash for one strategy under POOLED capital (capital_edge_reserve > 0):
          • pool-eligible edges: everything except MM's carve-out;
          • market_making: its carve-out + whatever sits above (edge reserve + carve-out);
          • everyone else: whatever sits above (edge reserve + MM carve-out)."""
        mm_res = self._mm_reserve_dollars()
        if strategy_id in self._capital_pool_eligible:
            return max(available_bp - mm_res, 0.0)
        above = max(available_bp - self._capital_edge_reserve - mm_res, 0.0)
        if strategy_id == "market_making":
            return min(available_bp, mm_res + above)
        return above

    # ------------------------------------------------------------------
    # Startup sequence (spec §5)
    # ------------------------------------------------------------------

    async def startup(
        self,
        *,
        skip_credential_check: bool = False,   # True in SIM mode
        skip_eligibility_check: bool = False,  # True in SIM mode
    ) -> bool:
        """
        Gated startup — returns False if any hard gate fails.

        Gate order (spec §5):
        1. Single-instance flock (caller must hold — see run_forever)
        2. Config: already loaded by caller
        3. Credential check (skipped in SIM)
        4. Eligibility check (skipped in SIM)
        5. Open state (caller's responsibility — state is passed in ready)
        6. Arm supervisor / watchdog BEFORE trading
        7. OMS pre-flight reconciliation (hard gate)
        8–12. Feed/signal/strategies/allocator startup (caller's responsibility)
        """
        log.info("orchestrator_startup", environment=self._environment)

        # Step 3: credential check (SIM skips)
        if not skip_credential_check:
            if not await self._check_credentials():
                log.error("startup_gate_credential_check_failed")
                return False

        # Step 4: eligibility check (SIM skips)
        if not skip_eligibility_check:
            if not await self._check_eligibility():
                log.error("startup_gate_eligibility_check_failed")
                return False

        # Step 6: arm watchdog BEFORE any trading can happen
        self._watchdog.start()

        # Step 7: OMS pre-flight reconciliation — HARD GATE
        result = await self._oms.startup()
        if result.hard_locked:
            log.error(
                "startup_gate_preflight_reconciliation_failed",
                reason=result.lock_reason,
            )
            await self._state.set_halt(
                f"pre_flight_hard_gate:{result.lock_reason}",
                {"stage": "startup"},
            )
            return False

        log.info(
            "startup_gate_reconciliation_ok",
            delta_count=len(result.deltas) if hasattr(result, "deltas") else 0,
        )
        return True

    async def _check_credentials(self) -> bool:
        """Verify API credentials are present and functional."""
        try:
            await self._broker.get_account()
            return True
        except Exception as exc:
            log.error("credential_check_failed", error=str(exc))
            return False

    async def _check_eligibility(self) -> bool:
        """SIG-specific eligibility check (email/residence/scope).
        Placeholder — real implementation talks to SIG API.
        """
        return True

    # ------------------------------------------------------------------
    # Run loop (spec §1b — ported from mbappe run_forever)
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """
        Main event loop.  Blocks until halted.

        Ported from mbappe orchestrator.py run_forever():
        - while True over run_cycle()
        - catch at cycle boundary → SOFT_DEGRADE on first failure
        - page after CONSECUTIVE_FAILURE_PAGE_THRESHOLD consecutive failures
        - asyncio.wait_for(interrupt_event.wait(), timeout=tick) for event-driven preemption
        """
        self._interrupt = asyncio.Event()
        # Start the independent exit timer. Separate task, so a slow cycle cannot delay the stop.
        if self._exit_task is None or self._exit_task.done():
            self._exit_task = asyncio.create_task(self._exit_timer_loop())
        if getattr(self, "_open_orders_task", None) is None and callable(
                getattr(self._broker, "get_open_orders_all", None)):
            self._open_orders_task = asyncio.create_task(self._open_orders_loop(), name="open_orders_snapshot")
        if self._placement_worker_enabled and getattr(self, "_placement_task", None) is None:
            self._placement_task = asyncio.create_task(self._placement_worker(),
                                                       name="placement_worker")
            log.info("exit_timer_started", interval_s=self._exit_check_interval_s)

        while True:
            if self._halt.is_set():
                log.info("orchestrator_halted_waiting", reason=self._halt_reason)
                await asyncio.sleep(self._tick_seconds)
                continue

            try:
                _cycle_t0 = time.monotonic()
                await self._run_cycle()
                _cycle_s = time.monotonic() - _cycle_t0
                self._consecutive_failures = 0
                self._write_heartbeat()
                # The heartbeat beats once per CYCLE, so a cycle longer than the dead-man threshold
                # trips it by construction. Log the duration so the two can be compared directly.
                log.info("cycle_complete", duration_s=round(_cycle_s, 1))
            except Exception as exc:
                self._consecutive_failures += 1
                log.error(
                    "cycle_failed",
                    error=str(exc),
                    streak=self._consecutive_failures,
                )
                # Soft-degrade on first failure — blocks new entries
                if self._consecutive_failures == 1:
                    log.warning("soft_degrade_engaged", reason="cycle_failure_streak")
                # Page on streak  (ported from mbappe consecutive_cycle_failures >= 3)
                if self._consecutive_failures >= CONSECUTIVE_FAILURE_PAGE_THRESHOLD:
                    log.error(
                        "critical_page_cycle_failure_streak",
                        streak=self._consecutive_failures,
                        last_error=str(exc),
                    )
                    self._page(
                        "CYCLE_FAILURE_STREAK",
                        f"Cycle failed {self._consecutive_failures}x consecutively: {exc}",
                        {"component": "orchestrator", "streak": self._consecutive_failures,
                         "environment": self._environment},
                    )

            self._interrupt.clear()
            # Event-driven preemption: tick-wait wakes immediately on interrupt
            # Ported from mbappe: asyncio.wait_for(interrupt_event.wait(), timeout=tick)
            try:
                await asyncio.wait_for(
                    self._interrupt.wait(), timeout=self._tick_seconds
                )
            except asyncio.TimeoutError:
                pass

    def interrupt(self) -> None:
        """Wake the loop immediately (used by supervisor / kill-switch)."""
        if self._interrupt is not None:
            self._interrupt.set()

    def halt(self, reason: str = "operator") -> None:
        """Halt-but-HOLD: stop new orders, keep positions, keep running.

        Spec §7: positions are HELD, never auto-flattened.
        """
        already_halted = self._halt.is_set()
        self._halt_reason = reason
        self._halt.set()
        self.interrupt()
        log.warning("orchestrator_halt_hold_set", reason=reason)
        # A silent halt is the worst failure mode: the bot looks alive, holds its positions and
        # places nothing, which is indistinguishable from a quiet market. Page on the TRANSITION
        # only, so re-halting an already-halted engine cannot re-page.
        if not already_halted:
            self._page(
                "HALT_BUT_HOLD",
                f"Engine HALTED (positions held, no new orders): {reason}",
                {"component": "orchestrator", "reason": reason, "environment": self._environment},
            )

    def resume(self) -> None:
        """Clear halt (after operator resolves the condition)."""
        self._halt.clear()
        self._halt_reason = ""
        log.info("orchestrator_halt_cleared")

    # ------------------------------------------------------------------
    # Cycle body (spec §1b, §2)
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> None:
        """
        One unified cadence-gated cycle.

        Order path (spec §2):
        STRATEGY intent → SIZING (within allocator budget + ledger available)
        → RISK GATE → OMS → BROKER (via write-gate)
        → fills reconcile → OMS advance pending bundles
        """
        now = time.monotonic()

        # --- Snapshot capital once (spec §1a, ported from mbappe begin_cycle) ---
        # Cached + 429-tolerant: a fresh-enough snapshot is reused (saves reads); a read failure reuses
        # the last-known snapshot rather than failing the cycle (the /account 429 crash-loop fix). Only a
        # first-ever failure with NO cached snapshot propagates (we cannot size capital we've never read).
        account = await self._get_account_cached()
        if account is None:
            # No account snapshot and none cached (startup /account 429 storm). HOLD this cycle — placing
            # orders blind to buying power is unsafe. Returning (not raising) pets the heartbeat via
            # run_forever's success path, so a transient read-storm cannot dead-man us into a crash-loop.
            log.warning("cycle_skipped_no_account", note="account read 429 with no cached snapshot — holding")
            return
        if not self._placement_worker_enabled:
            self._ledger.begin_cycle(
                buying_power=float(account.buying_power),
                equity=float(account.equity),
            )
        # else: the placement worker calls begin_cycle once per drain batch — see __init__. Calling
        # it here too would reset reservations underneath in-flight submits.

        # --- Pull VENUE-TRUTH positions into state periodically (visibility, NOT the hard-locking
        #     strategy_contracts reconciliation). The strategy_contracts reconciler only covers one
        #     anchor contract per strategy, so market-making's many filled contracts were invisible —
        #     the gate couldn't see holdings (sells rejected as naked), the MM couldn't post its
        #     inventory-reducing ask, and no exit could value a position. This writes ALL venue
        #     positions keyed by exchange_id so read_positions sees them. ---
        if (now - self._last_pos_sync) >= _VENUE_POS_SYNC_INTERVAL_S:
            await self._sync_venue_positions()
            self._last_pos_sync = now

        # --- Advance in-flight bundles and sweep timeouts (ALWAYS — halt-but-hold manages existing) ---
        await self._oms.advance_pending_bundles()
        await self._oms.sweep_unfilled_timeouts()
        if getattr(self._oms, "maker_resolved_flag", False):
            self._oms.maker_resolved_flag = False
            self._account_cache_ts = -1e18              # a maker entry resolved (maybe FILLED): read real cash next

        # --- Feed-staleness gate (SEAM-2): do NOT trade on dead/stale prices. Halt-but-HOLD —
        #     existing bundles were already advanced above; here we only gate NEW orders. ---
        self._stale_markets = await self._read_stale_markets()
        if await self._feed_is_dead():
            log.warning("feed_dead_halt_but_hold_skip_new_orders",
                        stale_markets=len(self._stale_markets))
            return   # global feed death → place no new orders this cycle (auto-recovers when fresh)

        # --- Sync venue-truth holdings into the risk gate ONCE per cycle, before BOTH exits and
        #     entries. The gate's sell branch needs real holdings or it rejects every sell as a
        #     naked short — this is what lets a market-making ASK (a reduction of held inventory)
        #     and an EXIT sell pass. Set centrally here so the single-leg entry path is covered too,
        #     not only _run_exits (which sets it again, idempotently, for its own baskets). ---
        await self._sync_gate_positions()

        # --- TAKE-PROFIT / STOP exit for HELD directional-type positions (incl. MM drift). Runs after
        #     the feed-dead gate (don't sell on untrusted prices) and after positions are synced +
        #     pushed to the gate (so the sell is seen as a reduction, not a naked short). A winner that
        #     reversed is SOLD here rather than bled back; distinct from arb basket hold-to-settlement. ---
        await self._run_takeprofit_exits()

        # --- EXIT / capital recycling (runs BEFORE entries so freed capital is visible to them) ---
        #     Deliberately placed AFTER the feed-dead gate: an exit is priced off the same book an
        #     entry is, so selling on prices we have just declared untrustworthy is strictly worse
        #     than holding to settlement. Dead feed ⇒ hold.
        await self._run_exits()

        # --- MM cancel-replace: cancel resting quotes that have drifted off the touch BEFORE the
        #     strategies post fresh ones, so quotes stay pegged to the live market (active quote
        #     maintenance) and the freed capital is re-deployable this cycle. ---
        await self._run_mm_requote()

        # --- Run due strategies sequentially (spec §1b cadence-gating) ---
        due = self._registry.due(now)
        for strategy in due:
            if self._halt.is_set():
                break
            await self._run_strategy(strategy, now)

    # ------------------------------------------------------------------
    # Exit / capital recycling
    # ------------------------------------------------------------------

    async def _run_exits(self) -> int:
        """Decide and submit exits for open baskets. Returns the number of baskets exited.

        The DECISION lives in strategies/exit (pure); this method only supplies its inputs and
        executes the verdict. Everything here fails CLOSED: any missing input (no exit config, no
        baskets, no prices, no order-book depth, no known alternative edge) yields HOLD, because
        holding a hard arb to settlement is the safe default — it still pays its guaranteed
        remainder. Only a positively-justified exit places an order.
        """
        if self._exit_cfg is None:
            return 0
        snapshot = await self._read_state_snapshot()
        baskets = snapshot.get("baskets") or []
        if not baskets:
            return 0
        prices = snapshot.get("prices") or {}
        positions = snapshot.get("positions") or {}
        if not prices:
            # An absent/empty price map means NO VISIBILITY, never "price is zero" — decline.
            log.debug("exit_skipped_no_prices", baskets=len(baskets))
            return 0

        # The gate's sell branch needs real holdings or it rejects every sell as a naked short.
        setter = getattr(self._risk_gate, "set_positions", None)
        if callable(setter):
            setter(positions)

        books = await self._fetch_exit_books(baskets)
        days = self._exit_cfg.days_to_settlement()

        from strategies.exit import decide_exits

        decisions = decide_exits(
            baskets,
            prices=prices,
            books=books,
            best_alternative_edge=float(self._best_alternative_edge()),
            days_to_settlement=days,
            cfg=self._exit_cfg,
        )

        exited = 0
        for d in decisions:
            if not d.is_exit:
                log.debug("exit_hold", basket_id=d.basket_id, reason=d.reason,
                          realizable_gain=round(d.realizable_gain, 4))
                continue
            # CAPACITY CAP: decide_exits judges every basket against the SAME best alternative, i.e.
            # it assumes that alternative has unbounded capacity. Acting on all of them would free
            # N baskets' capital into one opportunity that can absorb a fraction of it — and the
            # surplus would have surrendered a guaranteed remainder for idle cash. Baskets arrive
            # oldest-first, so the capital locked longest gets first refusal.
            if exited >= self._max_exits_per_cycle:
                log.info("exit_deferred_capacity_cap", basket_id=d.basket_id,
                         max_per_cycle=self._max_exits_per_cycle)
                break
            if await self._submit_exit(d):
                exited += 1
        if exited:
            log.info("exit_cycle_summary", exited=exited, considered=len(decisions))
        return exited

    async def _fetch_exit_books(self, baskets: list[dict]) -> dict[str, dict]:
        """Order-book depth for every leg of every open basket, via the injected provider.

        Depth is NOT in the state DB (the `prices` table carries only best_bid/best_ask), so it has
        to be fetched. With no provider wired this returns {} and every basket holds on
        `book_too_thin` — the correct failure direction: we cannot verify the exit is absorbable, so
        we do not attempt it.
        """
        if self._exit_books_provider is None:
            return {}
        ids: list[str] = []
        for b in baskets:
            for leg in (b.get("legs") or []):
                eid = str(leg.get("exchange_id") or "")
                if eid and eid not in ids:
                    ids.append(eid)
        if not ids:
            return {}
        try:
            res = self._exit_books_provider(ids)
            if asyncio.iscoroutine(res):
                res = await res
            return dict(res or {})
        except Exception as exc:
            # A depth fetch failure must not break the cycle; it just means no exits this pass.
            log.warning("exit_book_fetch_failed", error=str(exc), n_ids=len(ids))
            return {}

    def _best_alternative_edge(self) -> float:
        """The best currently-available alternative edge, as a FRACTION of deployed capital.

        This is what makes the recycle-vs-hold rule adaptive: it is the return the freed capital can
        actually go and earn. Supplied by the entry side (the relationship engine's live opportunity
        scan) via `set_best_alternative_edge()`. Defaults to 0.0 — no known alternative means
        recycling cannot beat a guaranteed remainder, so everything holds.
        """
        if self._best_alt_edge_provider is not None:
            try:
                return max(float(self._best_alt_edge_provider()), 0.0)
            except Exception:
                return 0.0
        return max(float(self._best_alt_edge), 0.0)

    def set_best_alternative_edge(self, edge: float) -> None:
        """Entry side reports the best opportunity it can currently see (fraction of capital)."""
        try:
            self._best_alt_edge = max(float(edge), 0.0)
        except (TypeError, ValueError):
            self._best_alt_edge = 0.0

    async def _submit_exit(self, decision: Any) -> bool:
        """Submit one basket's sell legs atomically. True when the OMS accepted them.

        NO CAPITAL IS RESERVED. This is the single most important difference from the entry path.
        `_process_multi_leg_intent` reserves Σ(qty × price) and aborts when the reservation cannot be
        filled from `available_buying_power()` — but an exit RETURNS capital rather than consuming
        it, and the bot wanting to exit is, by definition, fully deployed with ~0 available. Reserving
        here would therefore refuse the exit precisely when it is most needed: the deadlock this
        whole feature exists to break.
        """
        legs = [dict(l) for l in (decision.sell_legs or [])]
        if not legs:
            return False
        if any(self._market_is_stale(str(l.get("exchange_id", ""))) for l in legs):
            log.warning("exit_skipped_stale_market", basket_id=decision.basket_id)
            return False
        if not self._write_gate.is_allowed("arb"):
            log.warning("exit_skipped_write_budget", basket_id=decision.basket_id)
            return False

        # Gate all legs together as exposure-REDUCING sells (cap-exempt, clamped to held).
        verdict = self._risk_gate.validate_multi_leg(legs, available_capital=0.0, is_exit=True)
        if not getattr(verdict, "approved", False):
            log.info("exit_rejected_by_risk_gate", basket_id=decision.basket_id,
                     reason=getattr(verdict, "reason", ""))
            return False
        final_legs = getattr(verdict, "legs", None) or legs
        if any(int(l.get("quantity", 0)) <= 0 for l in final_legs):
            # A clamp to zero on ANY leg would make this a partial exit, leaving a naked leg and
            # destroying the hedge. All legs or none.
            log.info("exit_abandoned_leg_clamped_to_zero", basket_id=decision.basket_id)
            return False

        from sig.oms.models import FillPolicy  # lazy, matching _fill_policy_for in this module

        strategy_id = f"{decision.basket_id}:exit"
        oms_legs = [
            {"contract_id": l["exchange_id"], "market_id": l.get("market_id", l["exchange_id"]),
             "side": l.get("side", "yes"), "action": "sell",
             "qty": int(l["quantity"]), "price": l.get("price"),
             "leg_id": l.get("leg_id", f"exit_{i}")}
            for i, l in enumerate(final_legs)
        ]
        try:
            await self._oms.submit(
                strategy_id=strategy_id, legs=oms_legs,
                # for_arb() == UNWIND_ALL_LEGS, which is right for an exit too: unwinding a PARTIAL
                # exit re-buys what was just sold, restoring the hedge rather than leaving a naked leg.
                fill_policy=FillPolicy.for_arb(),
                metadata={"intent_action": "close", "strategy_type": "arb", "multi_leg": True,
                          "atomic": True, "exit": True, "basket_id": decision.basket_id,
                          "exit_reason": decision.reason},
            )
            self._write_gate.record_write("arb")
        except Exception as exc:
            log.error("exit_oms_submit_failed", basket_id=decision.basket_id, error=str(exc))
            return False

        # Mark the basket closed so it is not re-exited next cycle. Best-effort: the orders are
        # already away, so a bookkeeping failure must not look like a failed exit.
        closer = getattr(self._state, "close_basket", None)
        if callable(closer):
            try:
                await closer(decision.basket_id, realized_pnl=float(decision.realizable_gain))
            except Exception as exc:
                log.warning("exit_close_basket_failed", basket_id=decision.basket_id, error=str(exc))

        # Release any cycle-local reservation held under this basket's strategy id. The ledger is
        # cycle-local (begin_cycle wipes it), so this is not what frees the capital — the broker's
        # buying power rising when the sell fills is, picked up by the NEXT cycle's begin_cycle
        # snapshot. This release just stops a stale same-cycle reservation shadowing that.
        self._ledger.release(strategy_id)
        self._ledger.release(decision.basket_id)

        log.info("exit_submitted", basket_id=decision.basket_id, reason=decision.reason,
                 legs=len(oms_legs), realizable_gain=round(decision.realizable_gain, 4))
        return True

    async def _read_stale_markets(self) -> set[str]:
        """Per-market staleness from state (infra/state.stale_exchanges). Best-effort — absent on a
        minimal state double → treat as no staleness (unit tests unaffected)."""
        fn = getattr(self._state, "stale_exchanges", None)
        if fn is None:
            return set()
        try:
            return {str(x).upper() for x in (await fn(self._price_stale_after_s) or [])}
        except Exception:
            return set()

    async def _feed_is_dead(self) -> bool:
        """Global feed-death signal (all-stale / feed-health flag) → halt new entries entirely."""
        fn = getattr(self._state, "is_feed_dead", None)
        if fn is None:
            return False
        try:
            return bool(await fn())
        except Exception:
            return False

    #: How long a directional OPEN intent that sized to zero (no cash) or hit the gate's exposure cap is
    #: skipped before it is retried. Without this, cross-market re-sent the same un-placeable intent every
    #: 30s: ~2k zero-size/cap rejections an hour (2026-10-06). Waiting a few minutes costs nothing measurable.
    CAPPED_SKIP_S = 300.0

    def _capped_key(self, intent: Intent):
        if intent.is_multi_leg or intent.strategy_type in ("market_making", "arb") or intent.action != "open":
            return None
        return (intent.strategy_id, str(intent.exchange_id), (intent.metadata or {}).get("option_side"))

    def _skip_capped(self, intent: Intent) -> bool:
        key = self._capped_key(intent)
        if key is None:
            return False
        until = self.__dict__.get("_capped_until", {}).get(key)
        if until is not None and time.monotonic() < until:
            return True
        return False

    def _note_capped(self, intent: Intent) -> None:
        key = self._capped_key(intent)
        if key is not None:
            self.__dict__.setdefault("_capped_until", {})[key] = time.monotonic() + self.CAPPED_SKIP_S

    def _market_is_stale(self, exchange_id: str) -> bool:
        return str(exchange_id).upper() in self._stale_markets

        log.debug(
            "cycle_complete",
            due_count=len(due),
            available_bp=self._ledger.available_buying_power(),
        )

    async def _run_strategy(self, strategy: Strategy, now: float) -> None:
        """
        Evaluate one strategy and route its intents through the order path.

        Error isolation (spec §6, ported from mbappe):
        - evaluate() throws → catch, log, skip, increment failure counter
        - degrade-on-first-failure, page-on-streak — loop continues with other strategies
        """
        try:
            state_snapshot = await self._read_state_snapshot()
            intents: list[Intent] = strategy.evaluate(state_snapshot)
        except Exception as exc:
            failures = self._registry.mark_failure(strategy.name)
            log.error(
                "strategy_evaluate_failed",
                strategy=strategy.name,
                error=str(exc),
                consecutive_failures=failures,
            )
            if failures >= CONSECUTIVE_FAILURE_PAGE_THRESHOLD:
                log.error(
                    "critical_page_strategy_failure_streak",
                    strategy=strategy.name,
                    streak=failures,
                )
                self._page(
                    "STRATEGY_FAILURE_STREAK",
                    f"Strategy {strategy.name} failed {failures}x consecutively: {exc}",
                    {"component": f"strategy:{strategy.name}", "streak": failures,
                     "environment": self._environment},
                )
            return  # skip this strategy, continue the loop

        self._registry.mark_ran(strategy.name, now)

        # MAKER ENTRIES: reconcile resting maker orders with what the strategy wants now (keep same-price orders,
        # cancel stale ones) and hold back new intents in markets that still have an unresolved maker order.
        maker_ix = [i for i in intents if (getattr(i, "metadata", None) or {}).get("maker_entry")]
        if maker_ix or self._oms_has_makers(strategy.name):
            try:
                desired = {(str(i.exchange_id), round(float(i.price or 0.0), 3)) for i in maker_ix}
                busy = await self._oms.maker_sync(strategy.name, desired)
                intents = [i for i in intents if not ((getattr(i, "metadata", None) or {}).get("maker_entry")
                                                      and str(i.exchange_id) in busy)]
                # HARD CAP on concurrent maker entries per strategy (exposure + per-order status reads): keep the
                # strongest new ones up to the free slots
                slots = max(self.MAX_CONCURRENT_MAKER - len([b for b in self._oms.maker_bundles(strategy.name)
                                                              if not b.terminal_action_taken]), 0)
                new_mk = sorted([i for i in intents if (getattr(i, "metadata", None) or {}).get("maker_entry")],
                                key=lambda i: -float(i.signal_strength or 0.0))
                drop = {id(i) for i in new_mk[slots:]}
                intents = [i for i in intents if id(i) not in drop]
            except Exception as exc:  # noqa: BLE001 — on failure hold ALL maker intents (never stack)
                log.warning("maker_sync_failed_holding_maker_intents", error=type(exc).__name__)
                intents = [i for i in intents if not (getattr(i, "metadata", None) or {}).get("maker_entry")]

        if self._placement_worker_enabled:
            # Compute-only cycle: hand the intents to the worker and return immediately.
            for intent in intents:
                self._enqueue_intent(intent, strategy)
        else:
            for intent in intents:
                await self._process_intent(intent, strategy)

        # NOTE (SIM-integration fix): do NOT release here. A reservation is keyed by
        # intent.strategy_id and, once it funds a SUBMITTED order, must PERSIST for the rest of
        # the cycle so the next strategy cannot re-spend it (the lock-free double-spend guarantee,
        # spec §2). The per-intent paths already release on gate-reject / submit-failure, so the
        # only thing an unconditional release(strategy.name) here frees is a *successful*
        # reservation — which re-opened the capital to the next strategy and broke the arbiter.
        # begin_cycle() resets all reservations at the top of the next cycle.

    async def _process_intent(self, intent: Intent, strategy: Strategy) -> None:
        """
        Route one intent through: SIZING → RISK GATE → OMS → BROKER.
        All sizing is sequential against the capital ledger (lock-free).
        """
        if self._oms.is_hard_locked:
            log.warning("order_path_blocked_oms_hard_locked", strategy=strategy.name)
            return

        if self._skip_capped(intent):
            return                                   # recently un-placeable (no cash / at cap): retry later

        if not _ownership_allows(intent.strategy_id, intent.exchange_id):
            log.debug("intent_skipped_market_owned", strategy_id=intent.strategy_id, exchange=intent.exchange_id)
            return                                   # owned by an isolated strategy (sig/engine/ownership.py)

        if await self._oms.has_pending_bundle(intent.strategy_id):
            log.debug("order_path_blocked_pending_bundle", strategy_id=intent.strategy_id)
            return

        # Check write budget
        if not self._write_gate.is_allowed(intent.strategy_type):
            log.warning(
                "order_path_blocked_write_budget",
                strategy=strategy.name,
                strategy_type=intent.strategy_type,
            )
            return

        # Per-market feed-staleness skip (SEAM-2): never place a NEW order on a stale market — UNLESS the
        # intent is a slow structural edge that explicitly opts out (allow_stale_market). That flag is set
        # only by cross-market divergence, which trades SIG's LAGGING student price toward the FRESH sharp
        # benchmark (the lag IS the edge), has already bounded the SIG age to its own sig_stale_s, and posts
        # a protective LIMIT at the SIG mid. The 15s gate exists for MM/arb (quotes/legs priced at the touch
        # need live prices); those never set the flag, so their behaviour is unchanged. A multi-leg (arb)
        # NEVER bypasses — an atomic basket must have every leg fresh.
        _allow_stale = bool((intent.metadata or {}).get("allow_stale_market")) and not intent.is_multi_leg
        if (self._market_is_stale(intent.exchange_id) and not _allow_stale) or (
            intent.is_multi_leg and any(self._market_is_stale(l["exchange_id"]) for l in intent.legs)
        ):
            log.warning("order_path_skipped_stale_market", strategy=strategy.name,
                        exchange_id=intent.exchange_id)
            return
        if _allow_stale and self._market_is_stale(intent.exchange_id):
            log.info("order_path_stale_gate_bypassed_slow_edge", strategy=strategy.name,
                     exchange_id=intent.exchange_id, sig_age_s=(intent.metadata or {}).get("sig_age_s"))

        # ATOMIC MULTI-LEG arb (SEAM-1): route to the OMS atomic path — reserve the TOTAL once,
        # gate all legs together (hedge-preserving), submit atomically (all-fill-or-unwind).
        if intent.is_multi_leg:
            await self._process_multi_leg_intent(intent, strategy)
            return

        available_bp = self._ledger.available_buying_power()
        if self._capital_edge_reserve > 0.0:
            # POOLED CAPITAL: every strategy draws from the shared available cash (no stranded per-edge
            # allocator caps), EXCEPT a reserve floor that only the edge strategies may touch. So MM can
            # scale up to whatever it wants out of the pool, but can never dip into the last
            # `capital_edge_reserve` — that stays as dry powder for sniper / cross-market / dutch-book to
            # pounce on a stale quote, divergence, or arb the moment it appears. (Dutch-book is multi-leg
            # and already reserves against full available_bp, so it is edge-eligible by construction.)
            allocator_budget = self._pooled_budget(intent.strategy_id, available_bp)
        else:
            allocator_budget = self._allocator.budget_for(intent.strategy_id)

        # --- SIZING (spec §2 step 2) ---
        # Arb: capture-fillable, skip tournament sizing (qty from intent metadata)
        # Directional: full sizing (Browne × σ_p within allocator budget)
        # MM: inventory-managed size from intent
        if intent.strategy_type == "arb":
            qty = float(intent.metadata.get("qty", 1.0))
            capital_commitment = qty * (intent.price or 0.5)
        elif intent.strategy_type == "market_making":
            if callable(getattr(self._broker, "get_open_orders_all", None)):
                if not await self._mm_dedupe([intent], await self._non_mm_order_ids()):
                    return
                self._mm_note_posted([intent])
            # MM is INVENTORY-MANAGED: the quoter already sized the quote (metadata["mm_quote_qty"]).
            # Do NOT Browne-size a quote — that turns a small two-sided quote into a large directional
            # position. Honour the quote size, but clamp it to the per-cycle capital snapshot
            # (allocator budget ∩ available buying power) so a quote can never over-commit the ledger.
            quote_qty = float(intent.metadata.get("mm_quote_qty", 0.0))
            price = float(intent.price if intent.price is not None else 0.5)
            budget = max(min(float(allocator_budget), float(available_bp))
                         - max(getattr(self, "_mm_resting_bid_notional", 0.0), 0.0), 0.0)
            max_qty = (budget / price) if price > 0 else 0.0
            qty = float(int(min(quote_qty, max_qty)))
            capital_commitment = qty * price
        else:
            sizing_result: SizingResult = self._sizing.size(
                intent, available_bp, allocator_budget
            )
            qty = sizing_result.qty
            capital_commitment = sizing_result.capital_commitment

        if qty <= 0:
            # Was a silent black-hole: a generated intent that sizes to zero vanished with no trace. Log it
            # with the inputs so a starved SIGNAL-tier edge (allocator budget ∩ available buying power) is
            # observable, not invisible.
            log.info("order_path_zero_size", strategy=strategy.name, exchange_id=intent.exchange_id,
                     strategy_type=intent.strategy_type, available_bp=round(float(available_bp), 2),
                     allocator_budget=round(float(allocator_budget), 2), price=intent.price,
                     sigma_p=(intent.metadata or {}).get("sigma_p"))
            self._note_capped(intent)
            return

        # Reserve capital BEFORE risk gate so subsequent strategies see reduced balance. The key is PER INTENT:
        # CapitalLedger.reserve() REPLACES a key's prior reservation, so keying by strategy let several orders of
        # ONE strategy in the same cycle each size against the full pool (live 2026-10-09: 15 concurrent maker
        # entries, $9.4k resting against ~$5.4k allowed). Per-intent keys make reservations ACCUMULATE.
        res_key = f"{intent.strategy_id}|{intent.exchange_id}|{uuid.uuid4().hex[:8]}"
        actual_reserved = self._ledger.reserve(res_key, capital_commitment)

        # --- RISK GATE (spec §2 step 3) — universal choke point, no bypass ---
        verdict: RiskGateVerdict = self._risk_gate.validate(intent, qty, actual_reserved)
        if not verdict.approved:
            log.info(
                "order_path_rejected_by_risk_gate",
                strategy=strategy.name,
                strategy_id=intent.strategy_id,
                reason=verdict.reason,
            )
            self._ledger.release(res_key)
            if verdict.reason == "no_room_to_add_exposure":
                self._note_capped(intent)
            return

        final_qty = verdict.clamped_qty if verdict.clamped_qty is not None else qty

        # --- OMS (spec §2 step 4) ---
        fill_policy = _fill_policy_for(intent)
        try:
            await self._oms.submit(
                strategy_id=intent.strategy_id,
                legs=[_build_leg(intent, final_qty)],
                fill_policy=fill_policy,
                metadata={
                    "intent_action": intent.action,
                    "strategy_type": intent.strategy_type,
                    "signal_strength": intent.signal_strength,
                },
            )
            self._write_gate.record_write(intent.strategy_type)
            if str(intent.side).lower() == "buy" and intent.strategy_type != "market_making":
                # (MM resting bids have their own inventory cap + resting-bid budget)
                noter = getattr(self._risk_gate, "note_pending", None)
                if callable(noter):
                    noter(intent.exchange_id, float(final_qty) * float(intent.price or 0.0))
        except Exception as exc:
            log.error(
                "order_path_oms_submit_failed",
                strategy=strategy.name,
                error=str(exc),
            )
            self._ledger.release(res_key)

    async def _process_multi_leg_intent(self, intent: Intent, strategy: Strategy) -> None:
        """ATOMIC MULTI-LEG arb (SEAM-1). Capture-fillable: leg quantities come from the arb strategy
        (NOT the Browne sizer). Reserve the TOTAL for all legs ONCE against the ledger, gate all legs
        together (hedge-preserving clamp or whole-batch reject), then submit through the OMS atomic
        place_multi_leg path (all-fill-or-unwind — no naked leg). On success the reservation PERSISTS
        for the cycle (no release — the fixed double-spend guarantee)."""
        legs = [dict(l) for l in (intent.legs or [])]
        if not legs:
            return
        if not all(_ownership_allows(intent.strategy_id, l.get("exchange_id", l.get("contract_id"))) for l in legs):
            log.debug("multi_leg_skipped_market_owned", strategy_id=intent.strategy_id)
            return

        def _total(ls: list[dict]) -> float:
            return sum(float(l["quantity"]) * float(l.get("price") or 0.5) for l in ls)

        # Reserve the TOTAL once (atomic capital accounting) BEFORE any submit.
        total = _total(legs)
        reserved = self._ledger.reserve(intent.strategy_id, total)
        if reserved + 1e-9 < total:
            # Can't afford the WHOLE bundle → place NO leg (never a partial/naked arb).
            log.info("order_path_multileg_insufficient_capital", strategy=strategy.name,
                     needed=total, reserved=reserved)
            self._ledger.release(intent.strategy_id)
            return

        # Risk gate: all legs together — capture-fillable qty, but each leg still passes the gate
        # (hedge-preserving proportional clamp, or reject the whole batch).
        verdict = self._risk_gate.validate_multi_leg(legs, available_capital=reserved)
        if not verdict.approved:
            log.info("order_path_multileg_rejected_by_gate", strategy=strategy.name,
                     reason=getattr(verdict, "reason", ""))
            self._ledger.release(intent.strategy_id)
            return
        final_legs = getattr(verdict, "legs", None) or legs
        if _total(final_legs) + 1e-9 < total:
            self._ledger.reserve(intent.strategy_id, _total(final_legs))   # resize to the clamped total (still ONE)

        oms_legs = [
            {"contract_id": l["exchange_id"], "market_id": l.get("market_id", l["exchange_id"]),
             "side": l["side"], "action": l.get("action", "open"),
             "qty": int(l["quantity"]), "price": l.get("price"), "leg_id": l.get("leg_id", f"leg_{i}"),
             "outcome": l.get("outcome")}                  # OUTCOME (yes/no) → SIG side
            for i, l in enumerate(final_legs)
        ]
        try:
            await self._oms.submit(
                strategy_id=intent.strategy_id, legs=oms_legs,
                fill_policy=_fill_policy_for(intent),
                metadata={"intent_action": intent.action, "strategy_type": intent.strategy_type,
                          "multi_leg": True, "atomic": True},
            )
            self._write_gate.record_write(intent.strategy_type)
            # SUCCESS → do NOT release; the committed multi-leg reservation persists for the cycle.
        except Exception as exc:
            log.error("order_path_multileg_submit_failed", strategy=strategy.name, error=str(exc))
            self._ledger.release(intent.strategy_id)

    # ------------------------------------------------------------------
    # Heartbeat (written each cycle; read by off-loop watchdog)
    # ------------------------------------------------------------------

    def _write_heartbeat(self) -> None:
        self._last_heartbeat_at = time.monotonic()
        # Also persist to state (best-effort, no await needed here — use background task)
        ts = datetime.now(timezone.utc).isoformat()
        asyncio.get_event_loop().create_task(self._state.write_heartbeat(ts))

    def _heartbeat_age(self) -> float:
        return time.monotonic() - self._last_heartbeat_at

    def _page(self, incident: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        """Best-effort critical page (webhook + Discord) that can NEVER break the caller.

        Lazily imported on purpose: this module deliberately carries no top-level dependency on
        infra.control.alerts, so the orchestrator still imports and runs in a bare test harness
        where alerting is unwired. Works from the event loop (create_task) and from the off-loop
        watchdog thread (asyncio.run), because the dead-man calls it from its own OS thread.
        """
        try:
            from infra.control.alerts import dispatch_critical_page   # best-effort: may be unwired
        except Exception:
            log.critical("critical_page_unwired", incident=incident, message=message)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None:
                loop.create_task(dispatch_critical_page(incident, message, metadata or {}))
            else:
                # Off-loop caller (the dead-man watchdog thread): own the event loop briefly.
                asyncio.run(dispatch_critical_page(incident, message, metadata or {}))
        except Exception as exc:
            log.error("critical_page_dispatch_failed", incident=incident, error=str(exc))

    def _default_dead_man_alert(self, age: float) -> None:
        threshold = self._dead_man_threshold_s
        log.error("DEAD_MAN_HEARTBEAT_STALE", age_seconds=age, threshold=threshold)
        self._page(
            "DEAD_MAN_HEARTBEAT",
            f"Engine heartbeat stale: {age:.0f}s (threshold {threshold:.0f}s)",
            {"component": "orchestrator", "heartbeat_age_seconds": round(float(age), 1),
             "threshold_seconds": threshold, "environment": self._environment},
        )

    # ------------------------------------------------------------------
    # State snapshot for strategy evaluate()
    # ------------------------------------------------------------------

    # ── MM NO-STACK ────────────────────────────────────────────────────────────────────────────────
    # MM emits its DESIRED quotes every cadence; nothing compared them with what already rests, so every
    # cycle re-posted every quote on top of the last (3,632 open orders, 140 deep on one side). Rule now:
    # at most ONE resting MM order per (exchange, side). A desired quote within MM_DEDUPE_TOL of a resting
    # order is skipped; a resting order at a different price is cancelled first (≤ MM_MAX_CANCELS_PER_DRAIN
    # per drain — writes are scarce) and only then is the new quote posted. The venue view is a full
    # paginated open-orders snapshot (own task, every MM_SNAPSHOT_S) plus a local memory of quotes posted
    # since, so a fresh post isn't duplicated before the next snapshot sees it. No snapshot yet ⇒ MM waits.
    MM_DEDUPE_TOL = 0.0101          # ≈ 2 ticks
    MM_SNAPSHOT_S = 60.0          # read budget: was 30s; positions reads were being 429'd
    MM_RECENT_TTL_S = 90.0
    MM_MAX_CANCELS_PER_DRAIN = 6
    MM_REQUOTE_MAX_CANCELS = 10
    MM_CANCEL_TIMEOUT_S = 8.0       # one slow/429-backoff cancel must never stall the trading cycle

    async def _open_orders_loop(self) -> None:
        getter = getattr(self._broker, "get_open_orders_all", None)
        if not callable(getter):
            return
        while True:
            try:
                snap = await getter()
                if snap is not None:
                    self._open_snapshot = snap
                    self._open_snapshot_ts = time.monotonic()
                    log.info("open_orders_snapshot", n=len(snap))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("open_orders_snapshot_failed", error=type(exc).__name__)
            await asyncio.sleep(self.MM_SNAPSHOT_S)

    async def _mm_dedupe(self, intents: list, non_mm: set[str]) -> list:
        """Filter MM intents against resting orders; cancel stale same-side MM orders (bounded).
        Returns the intents that may be posted. Requires a snapshot (else returns [])."""
        snap = getattr(self, "_open_snapshot", None)
        if snap is None:
            log.info("mm_waiting_for_open_orders_snapshot")
            return []
        recent = getattr(self, "_mm_recent", None)
        if recent is None:
            recent = self._mm_recent = {}
        now = time.monotonic()
        for k in [k for k, v in recent.items() if now - v[1] > self.MM_RECENT_TTL_S]:
            recent.pop(k, None)
        cancelled_ids = getattr(self, "_mm_cancelled_ids", None)
        if cancelled_ids is None:
            cancelled_ids = self._mm_cancelled_ids = set()
        resting: dict[tuple, list] = {}
        for o in snap:
            if o.get("side") != "yes" or str(o.get("order_id")) in non_mm or str(o.get("order_id")) in cancelled_ids:
                continue
            resting.setdefault((str(o["exchange_id"]), o.get("action")), []).append(o)
        # MM's capital ALREADY committed in resting bids (venue doesn't reserve cash for them): snapshot bids
        # + bids posted since the snapshot. The group path subtracts it from MM's budget, so resting + new ≤ budget.
        snap_ts = getattr(self, "_open_snapshot_ts", 0.0)
        self._mm_resting_bid_notional = (
            sum(float(o["price"]) * float(o.get("quantity", 0) or 0)
                for (ex, act), lst in resting.items() if act == "buy" for o in lst)
            + sum(v[2] for (ex, act), v in recent.items() if act == "buy" and v[1] > snap_ts and len(v) > 2))
        out, cancels_left, skipped, cancelled = [], self.MM_MAX_CANCELS_PER_DRAIN, 0, 0
        for itn in intents:
            key = (str(itn.exchange_id), str(itn.side).lower())
            px = float(itn.price or 0.0)
            r = recent.get(key)
            if r is not None and abs(r[0] - px) <= self.MM_DEDUPE_TOL:
                skipped += 1
                continue
            rest = resting.get(key, [])
            if any(abs(float(o["price"]) - px) <= self.MM_DEDUPE_TOL for o in rest) and len(rest) == 1:
                skipped += 1
                continue
            if rest:
                if cancels_left < len(rest):
                    skipped += 1          # can't clear the side this drain → don't stack; retry next drain
                    continue
                failed = False
                for o in rest:
                    try:
                        await asyncio.wait_for(self._broker.cancel_order(o["order_id"]),
                                               timeout=self.MM_CANCEL_TIMEOUT_S)
                        self._mark_cancelled(o["order_id"])
                        cancels_left -= 1
                        cancelled += 1
                    except Exception as exc:  # noqa: BLE001 — don't post over an order we couldn't clear
                        log.warning("mm_dedupe_cancel_failed", order_id=o["order_id"], error=type(exc).__name__)
                        failed = True
                        cancels_left = 0
                        break
                if failed:
                    skipped += 1
                    continue
                if key[1] == "buy":
                    self._mm_resting_bid_notional -= sum(float(o["price"]) * float(o.get("quantity", 0) or 0)
                                                         for o in rest)
                resting[key] = []
            out.append(itn)
        if skipped or cancelled:
            log.info("mm_dedupe", posted=len(out), skipped=skipped, cancelled=cancelled)
        return out

    def _mark_cancelled(self, oid) -> None:
        ids = getattr(self, "_mm_cancelled_ids", None)
        if ids is None:
            ids = self._mm_cancelled_ids = set()
        ids.add(str(oid))

    def _mm_note_posted(self, intents: list) -> None:
        recent = getattr(self, "_mm_recent", None)
        if recent is None:
            recent = self._mm_recent = {}
        now = time.monotonic()
        for itn in intents:
            px = float(itn.price or 0.0)
            qty = float((itn.metadata or {}).get("mm_quote_qty", 0.0) or 0.0)
            recent[(str(itn.exchange_id), str(itn.side).lower())] = (px, now, px * qty)

    async def _run_mm_requote(self) -> int:
        """Cancel resting MM BID quotes that have drifted below the current touch by more than
        mm_requote_threshold, so the strategy re-posts at the live market this cycle (active quote
        maintenance, not post-and-hope). Cancelling frees the venue capital hold — the next
        begin_cycle's buying_power reflects it, so the reservation is released. A quote still near the
        touch is LEFT (no thrash). Arb-basket legs are skipped (never cancel a hedge leg). Best-effort.
        """
        if self._mm_requote_threshold <= 0:
            return 0
        # Only when market making is actually REGISTERED. With MM disabled this pass still cancelled every
        # strategy's resting buys that drifted >threshold below the touch (cross maker entries, exits' chase
        # orders) and compared NO prices against the YES bid.
        _reg = getattr(self, "_registry", None)
        if _reg is not None and _reg.get("market_making") is None:
            return 0
        snap = getattr(self, "_open_snapshot", None)
        if snap is not None and time.monotonic() - getattr(self, "_open_snapshot_ts", 0.0) <= 2 * self.MM_SNAPSHOT_S:
            # full paginated view (the single-page read below saw only the newest ~100 orders)
            done = getattr(self, "_mm_cancelled_ids", set())
            orders = [o for o in snap if str(o.get("order_id")) not in done]
        else:
            getter = getattr(self._broker, "get_open_orders_valued", None)
            if not callable(getter):
                return 0
            try:
                orders = await getter()
            except Exception as exc:  # noqa: BLE001
                log.warning("mm_requote_read_failed", error=type(exc).__name__)
                return 0
        if not orders:
            return 0
        prices: dict[str, Any] = {}
        pfn = getattr(self._state, "read_prices", None)
        if callable(pfn):
            try:
                prices = await pfn() or {}
            except Exception:  # noqa: BLE001
                prices = {}
        basket_contracts: set[str] = set()
        bfn = getattr(self._state, "read_open_baskets", None)
        if callable(bfn):
            try:
                for b in (await bfn()) or []:
                    for leg in (b.get("legs") or []):
                        basket_contracts.add(str(leg.get("exchange_id", "")).upper())
            except Exception:  # noqa: BLE001
                pass
        cancelled = 0
        # MM ↔ CROSS CONFLICT: vacate MM's resting orders (both sides) on cross races where cross's position
        # exceeds MM's inventory cap — MM stopped quoting there, but its already-resting skewed asks/bids
        # would otherwise keep trading against cross's bet. Only orders NOT known to be another strategy's.
        conflicted: set[str] = set()
        _src = getattr(self, "_mm_conflict_source", None)
        if _src is not None:
            try:
                conflicted = {str(x) for x in (_src() or set())}
            except Exception:  # noqa: BLE001
                conflicted = set()
        non_mm: set[str] = await self._non_mm_order_ids() if conflicted else set()
        vacated: set[str] = set()
        # BOUNDED: each cancel is a venue write at ~2s; with a big stale backlog an unbounded pass blocked the
        # trading cycle for minutes (2 cycles / 4 min). Clear at most MM_REQUOTE_MAX_CANCELS per cycle.
        budget = self.MM_REQUOTE_MAX_CANCELS
        for o in orders:
            if budget <= 0:
                break
            exch = str(o.get("exchange_id", ""))
            oid = str(o.get("order_id", ""))
            if _owned_market(exch):
                continue                                       # isolated strategy's market: not MM's to touch
            if exch in conflicted and oid and oid not in non_mm:
                try:
                    await asyncio.wait_for(self._broker.cancel_order(oid), timeout=self.MM_CANCEL_TIMEOUT_S)
                    cancelled += 1
                    vacated.add(oid)
                    self._mark_cancelled(oid)
                    budget -= 1
                    log.info("mm_conflict_cancel", exchange=exch, order_id=oid,
                             action=o.get("action"), side=o.get("side"), price=o.get("price"))
                except Exception as exc:  # noqa: BLE001 — incl. timeout: stop this pass, never stall the cycle
                    log.warning("mm_conflict_cancel_failed", order_id=oid, error=type(exc).__name__)
                    budget = 0
                    break
        non_mm_all = None
        for o in orders:
            if budget <= 0:
                break
            try:
                if str(o.get("order_id", "")) in vacated:
                    continue
                if str(o.get("action", "")).lower() != "buy":
                    continue                                   # only re-peg bids; asks/sells = exits
                exch = str(o.get("exchange_id", ""))
                if not exch or exch.upper() in basket_contracts or self._market_is_stale(exch.upper()):
                    continue
                if _owned_market(exch):
                    continue                                   # isolated strategy's market: not MM's to touch
                if non_mm_all is None:
                    non_mm_all = await self._non_mm_order_ids()
                if str(o.get("order_id", "")) in non_mm_all:
                    continue                                   # not MM's order: never re-peg another strategy
                prow = prices.get(exch) or prices.get(exch.upper()) or {}
                # compare against the touch of the LEG this order buys (NO buy -> NO bid = 1 - YES ask)
                if str(o.get("side", "yes")).lower() == "no":
                    _ask = prow.get("best_ask")
                    bid = (1.0 - float(_ask)) if _ask is not None else None
                else:
                    bid = prow.get("best_bid")
                px = o.get("price")
                if bid is None or not px:
                    continue
                bid = float(bid); px = float(px)
                if bid <= 0:
                    continue
                drift = (bid - px) / bid                        # resting bid sits this far below touch
                if drift <= self._mm_requote_threshold:
                    continue                                    # still near the touch — leave it
                try:
                    await asyncio.wait_for(self._broker.cancel_order(o["order_id"]),
                                           timeout=self.MM_CANCEL_TIMEOUT_S)
                except Exception as exc:  # noqa: BLE001 — 429 storm / timeout: stop this pass
                    log.warning("mm_requote_cancel_failed", order_id=o.get("order_id"), error=type(exc).__name__)
                    break
                cancelled += 1
                budget -= 1
                self._mark_cancelled(o["order_id"])
                log.info("mm_requote_cancel", exchange=exch, order_id=o["order_id"],
                         resting_px=px, touch_bid=bid, drift=round(drift, 3))
            except Exception as exc:  # noqa: BLE001
                log.warning("mm_requote_cancel_failed", order_id=o.get("order_id"),
                            error=type(exc).__name__)
                continue
        return cancelled

    # ------------------------------------------------------------------
    # Placement worker (see __init__ for the capital invariant)
    # ------------------------------------------------------------------

    def _enqueue_intent(self, intent: Any, strategy: Strategy) -> None:
        """Hand one intent to the worker. NEVER blocks the cycle.

        On overflow we drop the OLDEST queued intent, not the newest: a stale quote is worthless and
        the freshest view of the book is the one worth placing."""
        item = (time.monotonic(), intent, strategy)
        try:
            self._placement_q.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._placement_q.get_nowait()          # evict oldest
            self._placement_dropped_full += 1
            self._placement_q.put_nowait(item)
        except Exception:                            # noqa: BLE001 — never let this break the cycle
            self._placement_dropped_full += 1

    def _drain_queue(self) -> int:
        n = 0
        while True:
            try:
                self._placement_q.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                return n

    async def _placement_worker(self) -> None:
        """Drain the intent queue sequentially, at venue pace, off the cycle.

        One batch = block for the first intent, refresh the capital snapshot ONCE, then process
        everything currently queued against it. Processing is strictly one-at-a-time: this task is
        the only reserver, which is what preserves the lock-free no-double-spend guarantee."""
        log.info("placement_worker_started", ttl_s=self._placement_intent_ttl_s,
                 queue_max=self._placement_q.maxsize)
        while True:
            try:
                first = await self._placement_q.get()
            except asyncio.CancelledError:
                raise
            try:
                account = await self._get_account_cached()
                if account is None:
                    log.warning("placement_batch_skipped_no_account",
                                note="cannot size capital we have never read — dropping this batch")
                    self._drain_queue()
                    continue
                # ONE snapshot for the whole batch, exactly as a cycle did.
                self._ledger.begin_cycle(buying_power=float(account.buying_power),
                                         equity=float(account.equity))
                batch = [first]
                while True:
                    try:
                        batch.append(self._placement_q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                now = time.monotonic()
                placed = stale = grouped = 0
                fresh: list[tuple[float, Any, Strategy]] = []
                for q_at, itn, strat in batch:
                    # TTL: an intent priced off a book we saw 30s+ ago is exactly the stale-quote
                    # mistake the fresh-only MM filter exists to prevent. Drop rather than place.
                    if self._placement_intent_ttl_s and (now - q_at) > self._placement_intent_ttl_s:
                        stale += 1
                        self._placement_dropped_stale += 1
                        continue
                    fresh.append((q_at, itn, strat))

                # VALUE-PRIORITY + CAP: the venue places only ~10-13 orders/min, so draining the WHOLE
                # queued batch FIFO (e.g. 240 MM quotes) blocks for ~20min AND buries high-EV edges
                # behind the flood until they go stale. Sort by expected-value-per-order (exits > arb >
                # cross-market-by-divergence > MM), keep the top N placeable this drain, and DROP the
                # low-value overflow — it re-proposes next cycle if still valid. This is the fix that lets
                # MM breadth + cross-market + dutch-book share one tiny pipe (highest-value buys go first).
                deprioritized = 0
                if len(fresh) > 1:
                    fresh.sort(key=lambda t: _intent_priority(t[1]), reverse=True)
                if self._placement_max_per_drain and len(fresh) > self._placement_max_per_drain:
                    deprioritized = len(fresh) - self._placement_max_per_drain
                    fresh = fresh[: self._placement_max_per_drain]

                groups, singles = self._group_for_multileg(fresh)
                for strat, itns in groups:
                    try:
                        n = await self._process_mm_quote_group(itns, strat)
                        placed += n
                        grouped += n
                    except Exception as exc:  # noqa: BLE001
                        log.warning("placement_group_failed", error=type(exc).__name__,
                                    msg=str(exc)[:160], n=len(itns),
                                    note="falling back to one-at-a-time for this group")
                        for itn in itns:
                            try:
                                await self._process_intent(itn, strat)
                                placed += 1
                            except Exception:  # noqa: BLE001
                                pass
                for itn, strat in singles:
                    try:
                        await self._process_intent(itn, strat)
                        placed += 1
                    except Exception as exc:  # noqa: BLE001 — one bad intent must not kill the worker
                        log.warning("placement_worker_intent_failed",
                                    error=type(exc).__name__, msg=str(exc)[:160])
                log.info("placement_batch_done", batch=len(batch), processed=placed,
                         grouped=grouped, dropped_stale=stale, deprioritized=deprioritized,
                         queue_depth=self._placement_q.qsize(),
                         dropped_full_total=self._placement_dropped_full,
                         dropped_stale_total=self._placement_dropped_stale)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the worker must never die
                log.error("placement_worker_batch_error", error=type(exc).__name__,
                          msg=str(exc)[:200])

    # ---- MM-quote batching (POST /orders/multi-leg) ----------------------------------------

    def _group_for_multileg(self, fresh: list) -> tuple[list, list]:
        """Split queued intents into multi-leg GROUPS and leftover SINGLES.

        Only plain MM quotes are grouped. Everything else goes through the unchanged single path:
          • arb / already-multi-leg intents keep their own atomic semantics;
          • directional / order-flow use chase or accept-partial fill policies, where a shared bundle
            would change how partial fills are handled.
        MM uses leave_resting, so a bundle of independent quotes is safe: the unfilled-timeout sweep
        skips leave_resting bundles entirely and sibling-unwind is arb-only.

        One leg per exchange per group — SIG rejects duplicate legs for the same exchange with 400,
        which would lose the whole group. Groups are capped at placement_batch_max_legs (<=10).
        """
        cap = self._placement_batch_max_legs
        if cap <= 1:
            return [], [(i, st) for _q, i, st in fresh]
        buckets: dict[tuple[str, str], list] = {}
        singles: list = []
        for _q, itn, strat in fresh:
            if getattr(itn, "is_multi_leg", False) or itn.strategy_type != "market_making":
                singles.append((itn, strat))
                continue
            buckets.setdefault((strat.name, itn.strategy_id), []).append((itn, strat))
        groups: list = []
        for (_sname, _sid), items in buckets.items():
            seen: set[str] = set()
            cur: list = []
            strat = items[0][1]
            for itn, st in items:
                ex = str(itn.exchange_id)
                if ex in seen or len(cur) >= cap:
                    if len(cur) > 1:
                        groups.append((strat, cur))
                    else:
                        singles.extend((i, st) for i in cur)
                    cur, seen = [], set()
                cur.append(itn)
                seen.add(ex)
            if len(cur) > 1:
                groups.append((strat, cur))
            elif cur:
                singles.extend((i, strat) for i in cur)
        return groups, singles

    async def _process_mm_quote_group(self, intents: list, strategy: Strategy) -> int:
        """Size + gate each MM quote SEQUENTIALLY, then submit them as ONE multi-leg bundle.

        Capital: the reservation is re-stated to the RUNNING TOTAL as each leg is accepted. Note this
        is strictly more correct than the per-intent path for MM, because every MM intent shares one
        strategy_id and CapitalLedger.reserve() REPLACES a strategy's prior reservation — so placing
        237 quotes one at a time left the ledger holding only the LAST quote's commitment. Here one
        reservation covers the whole bundle's real total.

        Returns the number of legs submitted (0 if the group produced nothing).
        """
        if self._oms.is_hard_locked:
            return 0
        # drop legs in markets owned by an isolated strategy BEFORE dedupe/grouping: one owned leg would make
        # the broker refuse the whole multi-leg group after dedupe had already cancelled its siblings' quotes.
        intents = [i for i in intents if _ownership_allows(i.strategy_id, i.exchange_id)]
        if not intents:
            return 0
        sid = intents[0].strategy_id
        if await self._oms.has_pending_bundle(sid):
            log.debug("order_path_blocked_pending_bundle", strategy_id=sid)
            return 0
        allocator_budget = self._allocator.budget_for(sid)
        if callable(getattr(getattr(self, "_broker", None), "get_open_orders_all", None)):
            intents = await self._mm_dedupe(intents, await self._non_mm_order_ids())
            if not intents:
                return 0
        legs: list[dict[str, Any]] = []
        posted_intents: list = []
        running = 0.0
        for intent in intents:
            if not self._write_gate.is_allowed(intent.strategy_type):
                break                                     # out of write budget — stop adding legs
            if self._market_is_stale(intent.exchange_id):
                continue
            price = float(intent.price if intent.price is not None else 0.5)
            if price <= 0:
                continue
            # Off-tick prices make SIG reject the WHOLE group (400, nothing placed), so snap here.
            price = _snap_to_tick(price)
            available_bp = max(self._ledger.available_buying_power() - running, 0.0)
            if getattr(self, "_capital_edge_reserve", 0.0) > 0.0:
                # pooled: MM spends its carve-out (+ anything above the reserves), not the allocator weight
                budget = max(self._pooled_budget(sid, self._ledger.available_buying_power()) - running
                             - max(getattr(self, "_mm_resting_bid_notional", 0.0), 0.0), 0.0)
            else:
                budget = max(min(float(allocator_budget) - running, available_bp), 0.0)
            quote_qty = float((intent.metadata or {}).get("mm_quote_qty", 0.0))
            max_qty = (budget / price) if price > 0 else 0.0
            qty = float(int(min(quote_qty, max_qty)))
            if qty <= 0:
                continue
            commitment = qty * price
            reserved = self._ledger.reserve(sid, running + commitment)
            verdict = self._risk_gate.validate(intent, qty, max(reserved - running, 0.0))
            if not verdict.approved:
                self._ledger.reserve(sid, running)        # roll back to the accepted total
                log.info("order_path_rejected_by_risk_gate", strategy=strategy.name,
                         strategy_id=sid, reason=verdict.reason, batched=True)
                continue
            final_qty = verdict.clamped_qty if verdict.clamped_qty is not None else qty
            leg = _build_leg(intent, final_qty)
            leg["price"] = price
            legs.append(leg)
            posted_intents.append(intent)
            running += final_qty * price
        if not legs:
            self._ledger.release(sid)
            return 0
        try:
            await self._oms.submit(
                strategy_id=sid,
                legs=legs,
                fill_policy=_fill_policy_for(intents[0]),
                metadata={"intent_action": intents[0].action,
                          "strategy_type": intents[0].strategy_type,
                          "batched_quotes": len(legs)},
            )
        except Exception:
            self._ledger.release(sid)
            raise
        # ONE write for up to 10 quotes — this is the whole point of the lever.
        self._write_gate.record_write(intents[0].strategy_type)
        # Multi-leg responses carry no per-leg venue ids → remember the legs so the attribution flush can
        # recover the ids from the open-orders list (makes MM fills attributable / MM P&L measurable).
        noter = getattr(getattr(self, "_broker", None), "note_pending_legs", None)
        if callable(noter):
            noter(sid, legs)
        self._mm_note_posted(posted_intents)
        log.info("mm_quote_group_submitted", strategy=strategy.name, legs=len(legs),
                 capital=round(running, 2),
                 exchanges=[l["contract_id"] for l in legs][:10])
        return len(legs)

    async def _get_account_cached(self) -> Any | None:
        """Account snapshot with a short TTL + 429/read-error tolerance. A fresh cached snapshot is
        returned without a read; otherwise we fetch. On ANY read error we reuse the last-known snapshot;
        with NOTHING cached yet (e.g. a 429 on the very first read during a startup read-storm) we return
        None and the caller HOLDS the cycle. Never raises — a /account 429 must not fail the cycle (that
        was the dead-man → crash-loop); a held cycle still pets the heartbeat, so the dead-man stays quiet."""
        now = time.monotonic()
        if self._account_cache is not None and (now - self._account_cache_ts) < self._account_cache_ttl_s:
            return self._account_cache
        try:
            acct = await self._broker.get_account()
        except Exception as exc:  # noqa: BLE001 — a read 429/transient must not kill the cycle
            if self._account_cache is not None:
                log.warning("account_read_failed_using_cache", error=type(exc).__name__,
                            age_s=round(now - self._account_cache_ts, 1))
                return self._account_cache
            log.warning("account_read_failed_no_cache_holding", error=type(exc).__name__)
            return None
        self._account_cache = acct
        self._account_cache_ts = now
        return acct

    async def _sync_venue_positions(self) -> None:
        """Pull ALL venue-truth positions (broker.get_positions) into state, keyed by exchange_id.

        VISIBILITY ONLY — deliberately NOT the strategy_contracts reconciliation (which hard-locks on
        any venue position not mapped to a strategy_contract; market-making trades many contracts under
        one strategy_id, so that reconciler would hard-lock the whole bot). This is a plain upsert so
        read_positions / the gate / the MM ask-side / the take-profit exit can all SEE the holdings.
        Best-effort: a broker/read error leaves the last-known positions in place, never crashes the
        cycle."""
        try:
            positions = await self._broker.get_positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("venue_position_sync_failed", error=type(exc).__name__, msg=str(exc)[:160])
            return
        upsert = getattr(self._state, "upsert_position", None)
        if not callable(upsert):
            return
        venue_ids: set[str] = set()
        for p in positions or []:
            try:
                contract_id = str(getattr(p, "contract_id", "") or "")
                if not contract_id:
                    continue
                venue_ids.add(contract_id)
                await upsert({
                    "strategy_id": f"venue:{contract_id}",
                    "contract_id": contract_id,
                    # *** SIGNED. *** BrokerPositionSnapshot carries magnitude in `qty` and
                    # direction in `side`, and this used to persist the MAGNITUDE only. Every
                    # consumer reads `quantity` and ignores `side`, so a SHORT was stored as a
                    # positive number and read back as a LONG:
                    #   * the gate's sell_without_holding saw held > 0 and APPROVED further sells;
                    #   * the MM's _held_yes() saw inventory to sell and kept resting asks.
                    # Each excess sell deepened the short, which was stored positive again -- a
                    # feedback loop. It built ex1077 to -6045 (long 6045 NO, $5,524 cost basis,
                    # 27% of the book) entirely unintended. Signing it fixes BOTH consumers at
                    # once, because both already treat quantity <= 0 as "nothing to sell".
                    "qty": _signed_qty(p),
                    "side": getattr(p, "side", "flat"),
                    "avg_entry_price": float(getattr(p, "avg_entry_price", 0.0) or 0.0),
                    "source": "venue_sync",
                    "reconciled_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:  # noqa: BLE001 — one bad row must not drop the rest
                continue
        # ZERO PHANTOMS — the root cause of believed-state drift: this sync was ADDITIVE only, so a
        # position the venue NETTED AWAY (buy-NO cancels held-YES) lingered in the local store forever,
        # inflating `deployed` until the gate falsely tripped no_room_to_add_exposure and blocked all new
        # entries (~2.3x inflation observed, ~$50k idled). Now: any local position NOT in the venue set is
        # set flat. SAFETY: only when the venue read is NON-EMPTY — an empty list is a failed/suspicious
        # read (we hold ~150 positions mid-tournament), and zeroing the whole book on a transient miss
        # would be catastrophic.
        if not venue_ids:
            return
        reader = getattr(self._state, "read_positions", None)
        if not callable(reader):
            return
        try:
            local = await reader()
        except Exception:  # noqa: BLE001
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        for ex, row in (local or {}).items():
            if str(ex) in venue_ids:
                continue
            try:
                q = float((row or {}).get("quantity", 0) or 0)
            except (TypeError, ValueError):
                q = 0.0
            if abs(q) <= 0:
                continue
            try:
                await upsert({
                    "strategy_id": f"venue:{ex}", "contract_id": str(ex),
                    "qty": 0.0, "side": "flat", "avg_entry_price": 0.0,
                    "source": "venue_sync_zero", "reconciled_at": now_iso,
                })
            except Exception:  # noqa: BLE001
                continue

    # ── EXIT BACKOFF ───────────────────────────────────────────────────────────────────────────────
    # A close into an EMPTY peer-to-peer book returns open:false / quantityTraded:0, the CHASE bundle resolves
    # at once, and the very next pass (~20s) re-fires the identical close — forever (seen live: ex 951 every
    # ~20s at 0.05). Each burns one of the 30 writes/min shared with every strategy. Back off per exchange
    # while attempts make NO progress (held qty unchanged): free for the first EXIT_BACKOFF_FREE attempts,
    # then 60s, 120s, … capped at EXIT_BACKOFF_MAX_S. Any reduction in the held qty resets it.
    EXIT_BACKOFF_FREE = 2
    EXIT_BACKOFF_BASE_S = 60.0
    EXIT_BACKOFF_MAX_S = 1800.0

    def _exit_backoff_skip(self, exch: str, held_abs: float) -> bool:
        book = getattr(self, "_exit_backoff", None)
        if book is None:
            book = self._exit_backoff = {}
        st = book.get(exch)
        if st is None:
            return False
        if held_abs < st["qty"] - 0.5:            # the previous close made progress → start fresh
            book.pop(exch, None)
            return False
        return time.monotonic() < st["until"]

    def _exit_backoff_note(self, exch: str, held_abs: float) -> None:
        book = getattr(self, "_exit_backoff", None)
        if book is None:
            book = self._exit_backoff = {}
        st = book.get(exch)
        n = (st["n"] + 1) if (st is not None and held_abs >= st["qty"] - 0.5) else 1
        wait = 0.0 if n <= self.EXIT_BACKOFF_FREE else min(
            self.EXIT_BACKOFF_BASE_S * (2 ** (n - self.EXIT_BACKOFF_FREE - 1)), self.EXIT_BACKOFF_MAX_S)
        book[exch] = {"n": n, "qty": held_abs, "until": time.monotonic() + wait}
        if wait > 0:
            log.info("exit_backoff", exchange=exch, attempts=n, wait_s=round(wait),
                     note="close made no progress (empty book?) — backing off")

    async def _run_takeprofit_exits(self) -> int:
        """Serialising wrapper: ONE exit pass at a time.

        Called both from the trading cycle and from the fast exit timer, so without this the two
        could evaluate the same position concurrently and each submit a close for it. The
        per-position `has_pending_bundle(exit:<id>)` guard already blocks a second close ACROSS
        passes; this lock closes the window WITHIN overlapping passes, where neither has submitted
        yet so neither sees the other's pending bundle.
        """
        if self._exit_pass_lock is None:
            self._exit_pass_lock = asyncio.Lock()
        if self._exit_pass_lock.locked():
            # A pass is already running; a second concurrent pass would add nothing but risk.
            return 0
        async with self._exit_pass_lock:
            return await self._run_takeprofit_exits_inner()

    async def _exit_timer_loop(self) -> None:
        """Run the stop/trail check every `exit_check_interval_s`, independent of the trading cycle.

        This is what makes the 20% stop actually mean 20%. Never raises: a failure here must not
        kill the trader, and the in-cycle call remains as a backstop.
        """
        while True:
            try:
                await asyncio.sleep(self._exit_check_interval_s)
                if self._halt.is_set():
                    continue          # halt-but-HOLD: positions are held, not auto-flattened
                n = await self._run_takeprofit_exits()
                if n:
                    log.info("exit_timer_pass", exits=n,
                             interval_s=self._exit_check_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("exit_timer_pass_failed", error=type(exc).__name__,
                            msg=str(exc)[:160])

    async def _run_takeprofit_exits_inner(self) -> int:
        """Close HELD directional-type positions that hit the trailing stop or stop-loss. Returns #exits.

        Scope: positions NOT part of an open arb basket (those hold to settlement). For each, value it
        at the current BID; the trailing-stop / stop-loss verdict comes from _directional_exit_decision.

        A position that must close is closed by BUY-TO-CANCEL: buying the OPPOSITE outcome (held YES ⇒
        buy NO; held NO ⇒ buy YES) for EXACTLY the held quantity, at the complement price. Per the SIG
        API (SingleOrderInput), an opposite-signed BUY nets against the existing position at FIFO lot
        cost, so the close needs NO same-side counterparty and RELEASES buying power — robust in a thin
        book where a resting sell would never fill. qty is exactly the held amount, so the position nets
        to ~0 and never crosses past flat into a naked short. (Arb baskets, the MM ask-side and the arb
        exit keep the conventional backed SELL — this buy-to-cancel path is the directional exit only.)
        Fails CLOSED: any missing input (no price, no avg, stale market) skips that position."""
        # Contracts inside an open arb basket hold to settlement — never take-profit them here.
        basket_contracts: set[str] = set()
        bfn0 = getattr(self._state, "read_open_baskets", None)
        if callable(bfn0):
            try:
                for b in (await bfn0()) or []:
                    for leg in (b.get("legs") or []):
                        basket_contracts.add(str(leg.get("exchange_id", "")).upper())
            except Exception:  # noqa: BLE001
                pass

        # PREFERRED: value held positions from the VENUE (price + pnl% + option) so positions in
        # markets outside the WS stream are still valued + exitable. Sell winners/losers at MARKET
        # (price omitted) for a decisive fill. Falls back to the state+local-price path (SIM/tests)
        # when the broker has no venue-valued read.
        valued_fn = getattr(self._broker, "get_positions_valued", None)
        if callable(valued_fn):
            from sig.oms.models import FillPolicy
            try:
                vpositions = await valued_fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("venue_valued_positions_failed", error=type(exc).__name__)
                vpositions = []
            # Live YES book (best_bid/best_ask) so exits can CROSS real resting liquidity instead of
            # undercutting a possibly-stale venue mark — that mark-only price returned open:false/
            # quantityTraded:0 (no match, no rest) and the chase can't re-quote a non-open order, so
            # positions never closed. One local read, best-effort.
            try:
                _exit_prices = await self._state.read_prices() or {}
            except Exception:  # noqa: BLE001
                _exit_prices = {}
            sent = 0
            _bo = getattr(self, "_exit_backoff", None)
            if _bo:                                    # forget backoff for positions no longer held
                _held = {str(p.get("exchange_id", "")).upper() for p in (vpositions or [])
                         if abs(float(p.get("quantity", 0.0) or 0.0)) >= 1}
                for _k in [k for k in _bo if k not in _held]:
                    _bo.pop(_k, None)
            for pos in vpositions or []:
                try:
                    exch_u = str(pos.get("exchange_id", "")).upper()
                    if not exch_u or exch_u in basket_contracts or pos.get("settled"):
                        continue
                    if _owned_market(pos.get("exchange_id")):
                        continue                     # an isolated strategy's market: its owner manages exits
                    qty = float(pos.get("quantity", 0.0) or 0.0)
                    if abs(qty) < 1:
                        continue
                    pnl_frac = float(pos.get("pnl_pct", 0.0) or 0.0) / 100.0   # venue reports percent
                    # Trailing take-profit-on-reversal + hard stop. Update the per-contract PEAK, arm
                    # once peak >= trail_arm_pct, then sell when the gain gives back trail_pct OF THE
                    # PEAK (still-climbing winners are HELD); independently cut a loser at -stop_loss_pct.
                    peak = max(self._pos_peak.get(exch_u, pnl_frac), pnl_frac)
                    self._pos_peak[exch_u] = peak
                    # Slow-convergence edges (cross-market) use a THESIS-based exit that REPLACES the price
                    # stop for their own races: hold while the sharp benchmark backs the position, take
                    # profit on convergence, cut only on a WIDE backstop. MM/other positions are not handled
                    # by the override → they keep the default trailing-stop / price stop below.
                    reason = None
                    handled = False
                    if self._directional_exit_override is not None:
                        # Held side = SIGN of quantity (+ YES, − NO). NOT the `option` field: the venue
                        # reports option="YES" on EVERY row, so keying off it judged every held-NO short as
                        # a long → thesis_reversed on any |gap|>tol → sell NO → cross re-entered (churn).
                        _is_long = qty > 0
                        try:
                            handled, reason = self._directional_exit_override(exch_u, pnl_frac, _is_long)
                        except Exception as exc:  # noqa: BLE001 — a bad override must never block exits
                            log.warning("directional_exit_override_failed", exchange=exch_u,
                                        error=type(exc).__name__)
                            handled = False
                    if not handled:
                        reason = _directional_exit_decision(
                            pnl_frac, peak,
                            trail_arm_pct=self._exit_trail_arm_pct,
                            trail_pct=self._exit_trail_pct,
                            stop_loss_pct=self._exit_stop_loss_pct,
                        )
                    if reason is None:
                        continue
                    # SELL-TO-REDUCE the HELD leg. DIAGNOSED from the live positions feed: the row's
                    # `option` field is ALWAYS the market outcome ("YES"), NOT our side — the held side is
                    # the SIGN of `quantity` (+ = hold YES, − = hold NO). The book is full of SHORT-YES
                    # winners (qty<0, i.e. we hold NO, shorted a longshot that collapsed). Closing a short
                    # by BUYING YES is funds-checked at placement and rejected at available_bp≈0 (the
                    # deadlock); SELLING the held NO is an inventory sale — no up-front balance, and it
                    # GENERATES the proceeds that free capital + realize the gain. qty = |held| ⇒
                    # reduce-only (the venue rejects a sell beyond holdings), so never a naked flip.
                    held_outcome = "yes" if qty > 0 else "no"
                    cp = float(pos.get("current_price", 0.0) or 0.0)       # YES mark
                    held_mark = cp if held_outcome == "yes" else (1.0 - cp)  # the held leg's own mark
                    # Price to CROSS the live YES book so the sell actually matches a resting counterparty
                    # (not just undercut the mark). held YES ⇒ sell into the best YES BID; held NO ⇒ sell NO
                    # at 1 − best YES ASK (a NO-buyer rests on the YES ask). Cap by the mark−cross so we
                    # never price ABOVE the undercut; fall back to mark−cross when the book side is empty.
                    _pr = _exit_prices.get(exch_u) or _exit_prices.get(exch_u.lower()) or {}
                    def _f(x):
                        try:
                            v = float(x);  return v if 0.0 < v < 1.0 else None
                        except (TypeError, ValueError):
                            return None
                    yb, ya = _f(_pr.get("best_bid")), _f(_pr.get("best_ask"))
                    if 0.0 < cp < 1.0:
                        base = _snap_tick(held_mark - _EXIT_CROSS)
                        if held_outcome == "yes" and yb is not None:
                            close_price = _snap_tick(min(base, yb))          # cross the best YES bid
                        elif held_outcome == "no" and ya is not None:
                            close_price = _snap_tick(min(base, 1.0 - ya))    # cross the best YES ask (as NO)
                        else:
                            close_price = base                               # book side empty → undercut mark
                    else:
                        close_price = None                                   # no mark ⇒ market sell of inventory
                    exit_sid = f"exit:{exch_u}"
                    if await self._oms.has_pending_bundle(exit_sid):
                        continue
                    if self._exit_backoff_skip(exch_u, abs(qty)):
                        continue
                    log.info("takeprofit_exit", exchange=exch_u, reason=reason, close="sell_to_reduce",
                             held=held_outcome, sell=held_outcome, price=close_price,
                             pnl_pct=round(pnl_frac, 3), peak_pct=round(peak, 3),
                             qty=int(abs(qty)), venue_valued=True)
                    self._pos_peak.pop(exch_u, None)   # reset peak once we decide to exit
                    await self._oms.submit(
                        strategy_id=exit_sid,
                        legs=[{"contract_id": exch_u, "side": "sell", "outcome": held_outcome,
                               "action": "close", "qty": int(abs(qty)), "price": close_price}],
                        fill_policy=FillPolicy.for_directional_chase(),
                        metadata={"exit_reason": reason, "pnl_pct": pnl_frac, "venue_valued": True,
                                  "close": "sell_to_reduce", "held_outcome": held_outcome},
                    )
                    self._exit_backoff_note(exch_u, abs(qty))
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("takeprofit_exit_failed", exchange=str(pos.get("exchange_id")),
                                error=type(exc).__name__, msg=str(exc)[:160])
                    continue
            return sent

        # --- Fallback (SIM/tests): value from state positions + local bid, limit-sell at the bid ---
        read_pos = getattr(self._state, "read_positions", None)
        if not callable(read_pos):
            return 0
        try:
            positions = await read_pos()
        except Exception:  # noqa: BLE001
            return 0
        if not positions:
            return 0
        prices: dict[str, Any] = {}
        pfn = getattr(self._state, "read_prices", None)
        if callable(pfn):
            try:
                prices = await pfn() or {}
            except Exception:  # noqa: BLE001
                prices = {}
        # Contracts inside an open arb basket hold to settlement — never take-profit them here.
        basket_contracts: set[str] = set()
        bfn = getattr(self._state, "read_open_baskets", None)
        if callable(bfn):
            try:
                for b in (await bfn()) or []:
                    for leg in (b.get("legs") or []):
                        basket_contracts.add(str(leg.get("exchange_id", "")).upper())
            except Exception:  # noqa: BLE001
                pass
        from sig.oms.models import FillPolicy
        sent = 0
        for exch, pos in positions.items():
            try:
                exch_u = str(exch).upper()
                if exch_u in basket_contracts or self._market_is_stale(exch_u):
                    continue
                if _owned_market(exch):
                    continue
                qty = float(pos.get("quantity", pos.get("qty", 0.0)) or 0.0)
                if abs(qty) < 1:
                    continue
                avg = float(pos.get("avg_price", pos.get("avg_entry_price", 0.0)) or 0.0)
                if avg <= 0:
                    continue
                prow = prices.get(exch) or prices.get(exch_u) or {}
                bid = prow.get("best_bid")
                if bid is None:
                    continue
                bid = float(bid)
                pnl_pct = (bid - avg) / avg
                # SAME trailing-stop + stop-loss as the venue-valued path, via the ONE shared helper so
                # the two cannot drift — this fallback previously kept the OLD fixed +100% take-profit,
                # which capped winners. Track the PEAK across cycles, arm + trail off it.
                peak = max(self._pos_peak.get(exch_u, pnl_pct), pnl_pct)
                self._pos_peak[exch_u] = peak
                # Thesis-based exit override for slow-convergence edges (cross-market); see the venue path.
                reason = None
                handled = False
                if self._directional_exit_override is not None:
                    try:
                        handled, reason = self._directional_exit_override(exch_u, pnl_pct, qty > 0)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("directional_exit_override_failed", exchange=exch_u,
                                    error=type(exc).__name__)
                        handled = False
                if not handled:
                    reason = _directional_exit_decision(
                        pnl_pct, peak,
                        trail_arm_pct=self._exit_trail_arm_pct,
                        trail_pct=self._exit_trail_pct,
                        stop_loss_pct=self._exit_stop_loss_pct,
                    )
                if reason is None:
                    continue
                # BUY-TO-CANCEL: buy the OPPOSITE outcome (held YES ⇒ buy NO; held NO ⇒ buy YES) for
                # EXACTLY the held qty, at the complement price (sell held @ bid ≡ buy opposite @ 1−bid).
                # SIG nets an opposite-signed buy against the held position at FIFO lot cost — no
                # same-side counterparty, releases buying power. Held side from the SIGN of quantity
                # (SIG Position.quantity: + = YES shares, − = NO). qty = held ⇒ nets to ~0, never naked.
                held_outcome = "yes" if qty > 0 else "no"
                close_outcome = "no" if held_outcome == "yes" else "yes"
                close_price = _snap_tick(1.0 - bid if held_outcome == "yes" else bid)
                # thesis exits use the SAME limit close (the market-close path caused a doubling runaway — see
                # the venue-valued path above).
                exit_sid = f"exit:{exch_u}"
                if await self._oms.has_pending_bundle(exit_sid):
                    continue
                self._pos_peak.pop(exch_u, None)   # reset the peak once we decide to exit (venue-path parity)
                log.info("takeprofit_exit", exchange=exch_u, reason=reason, close="buy_to_cancel",
                         held=held_outcome, buy=close_outcome, price=close_price,
                         pnl_pct=round(pnl_pct, 3), peak_pct=round(peak, 3), qty=int(abs(qty)), avg=avg)
                await self._oms.submit(
                    strategy_id=exit_sid,
                    legs=[{"contract_id": exch_u, "side": "buy", "outcome": close_outcome,
                           "action": "close", "qty": int(abs(qty)), "price": close_price}],
                    fill_policy=FillPolicy.for_directional_chase(),
                    metadata={"exit_reason": reason, "pnl_pct": pnl_pct, "close": "buy_to_cancel",
                              "held_outcome": held_outcome, "close_outcome": close_outcome},
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001 — one bad position must not drop the rest
                log.warning("takeprofit_exit_failed", exchange=str(exch), error=type(exc).__name__,
                            msg=str(exc)[:160])
                continue
        return sent

    async def _sync_gate_positions(self) -> None:
        """Push reconciled venue-truth holdings into the risk gate so its sell branch can tell a
        position-REDUCING sell (a market-making ask or an exit) from a naked short. BEST-EFFORT:
        a gate without `set_positions` or a state without `read_positions` is a silent no-op, and
        any read error leaves the gate's last-known holdings in place (never clears them mid-cycle)."""
        setter = getattr(self._risk_gate, "set_positions", None)
        if not callable(setter):
            return
        fn = getattr(self._state, "read_positions", None)
        if fn is None:
            return
        try:
            positions = await fn()
        except Exception:
            return
        if positions is not None:
            setter(positions)

    async def _read_state_snapshot(self) -> dict[str, Any]:
        """The per-cycle view handed to every strategy's `evaluate()`.

        `"exposure"` keeps exactly its previous meaning and shape (whatever `read_exposure()`
        returns — an `Exposure` object from the real StateStore, a per-strategy dict from the SIM
        `StateAdapter`); nothing downstream of it changes.

        The three added keys are what an EXIT decision needs and could not previously get. Entry is
        sized from aggregate exposure, so aggregate exposure was all this carried; but you cannot
        decide to close something you cannot see individually. So:
          "positions" — per exchange_id, including the marked `unrealized_pnl`, i.e. what the book
                        is worth now versus what it cost.
          "prices"    — the bids/asks an exit would actually have to trade against, so a rule can
                        check the exit is executable instead of assuming a mark is reachable.
          "baskets"   — the open multi-leg baskets with their legs, entry cost and guaranteed
                        settlement value. Entry was atomic, so the exit is per-basket, and this is
                        the only place that grouping survives (the OMS drops it once filled).

        BEST-EFFORT BY CONSTRUCTION. `self._state` is the real StateStore in production and a
        minimal double in SIM/unit tests, so every new read goes through `getattr` + a swallowed
        exception exactly like `_read_stale_markets` / `_feed_is_dead` above. A state object that
        implements nothing new still yields the old single-key snapshot rather than an AttributeError
        mid-cycle, and an absent key reads as "no visibility", never as "nothing is open".
        """
        snapshot: dict[str, Any] = {"exposure": await self._state.read_exposure()}
        for key, method in (
            ("positions", "read_positions"),
            ("prices", "read_prices"),
            ("baskets", "read_open_baskets"),
        ):
            fn = getattr(self._state, method, None)
            if fn is None:
                continue
            try:
                value = await fn()
            except Exception:
                continue
            if value is not None:
                snapshot[key] = value
        return snapshot

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_halted(self) -> bool:
        return self._halt.is_set()

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def ledger(self) -> CapitalLedger:
        return self._ledger


# ---------------------------------------------------------------------------
# Fill-policy factory (maps intent strategy type to OMS FillPolicy)
# Lazy-imports sig.oms.models so the orchestrator can be tested without sig.oms
# on the path (the OMS is injected; FillPolicy is only needed when placing orders).
# ---------------------------------------------------------------------------

def _owned_market(exchange_id) -> bool:
    """True when an isolated strategy owns this market AND is alive to manage it (lease). An owner whose heartbeat
    lapsed leaves its positions to the orchestrator's exits. A registry error -> False: risk-reducing exits keep
    working (the broker still refuses NEW exposure in owned markets)."""
    try:
        from sig.engine.ownership import registry
        return registry().managed_owner(exchange_id) is not None
    except Exception:  # noqa: BLE001
        return False


def _ownership_allows(strategy_id, exchange_id) -> bool:
    try:
        from sig.engine.ownership import registry
        return registry().allows(strategy_id, exchange_id)
    except Exception:  # noqa: BLE001
        return False


def _fill_policy_for(intent: Intent) -> Any:
    """Map an intent's strategy_type to the appropriate OMS FillPolicy."""
    try:
        from sig.oms.models import FillPolicy  # noqa: PLC0415
        stype = intent.strategy_type
        md = getattr(intent, "metadata", None) or {}
        if md.get("maker_entry"):
            # resting maker entry: NO chase (cycle threshold never reached); the OMS timeout sweep cancels it
            return FillPolicy.for_directional_accept(
                unfilled_timeout_seconds=float(md.get("maker_timeout_s", 60.0)), cycle_cancel_threshold=10**9,
                maker_rest=True)
        if stype == "arb":
            return FillPolicy.for_arb()
        elif stype == "market_making":
            return FillPolicy.for_market_making()
        elif stype in ("directional", "resolution", "order_flow"):
            return FillPolicy.for_directional_chase()
        else:
            return FillPolicy.for_directional_accept()
    except ImportError:
        # sig.oms not on path — return a plain dict the FakeOMS in tests accepts
        return {
            "strategy_type": intent.strategy_type,
            "partial_fill_policy": "chase",
        }
