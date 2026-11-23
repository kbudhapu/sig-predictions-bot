"""
OrderManagementSystem — the unified OMS for the SIG Predictions Cup bot.

Consolidates what mbappe scatters across orchestrator.py, broker_fill_sieve.py,
pre_flight_reconciliation.py, and execution_adaptor.py into one coherent manager.

Reused from mbappe:
  - PendingFillBundle / ChildFillSlice mechanics → OrderBundle / OrderSlice
  - entry-guard (has_pending_bundle_for_leg) → has_pending_bundle()
  - advance_pending_cycles → advance_pending_bundles()
  - broker-authoritative reconciliation → reconciliation.py
  - pre-flight hard lock on unreconcilable state → reconciliation.py
  - cancel + resubmit (no amend) → replace_order()
  - unmapped-symbol hard lock → reconciliation.py

NOT copied from mbappe:
  - "partial_fill_residual_abandoned" + "operator must act manually"
    → replaced with autonomous per-strategy policies in fill_handler.py
"""

from __future__ import annotations

import asyncio
import structlog
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sig.oms.fill_handler import advance_bundle
from sig.oms.models import (
    FINISHED_ORDER_STATUSES,
    cancel_confirmed,
    FillPolicy,
    OrderBundle,
    OrderLifecycle,
    OrderSlice,
    PartialFillPolicy,
    StrategyClass,
    TERMINAL_ORDER_STATUSES,
)
from sig.oms.protocols import SigBroker, SigStateStore
from sig.oms.reconciliation import (
    ReconciliationResult,
    reconcile_against_broker,
    run_pre_flight_reconciliation,
)

log = structlog.get_logger()


# --------------------------------------------------------------------------- #
# Broker-result field access — the canonical broker returns an OrderResult
# (sig/broker.py); place_multi_leg returns ONE OrderResult carrying per-leg detail
# in `.legs`. These helpers read fields uniformly whether a result is an OrderResult,
# a per-leg dict, or a test snapshot.
# --------------------------------------------------------------------------- #
def _rget(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _rnum(obj: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(_rget(obj, name, default))
    except (TypeError, ValueError):
        return default


def _normalize_results(result: Any, legs: list[dict[str, Any]]) -> list[Any]:
    """Normalize a broker response to one result-like per leg. Accepts a single
    OrderResult (place_order, or place_multi_leg with a `.legs` list), or a plain
    list (test fakes)."""
    if isinstance(result, list):
        if len(result) == len(legs):
            return result
        return ([*result] + [result[-1]] * len(legs))[: len(legs)] if result else [result] * len(legs)
    legs_detail = getattr(result, "legs", None)
    if isinstance(legs_detail, (list, tuple)) and len(legs_detail) == len(legs):
        return list(legs_detail)
    return [result] * len(legs)


def _broker_avg_price(br: Any) -> float:
    """Canonical OrderResult exposes `avg_price`; some snapshots use `avg_fill_price`."""
    avg = _rnum(br, "avg_price", 0.0)
    return avg if avg != 0.0 else _rnum(br, "avg_fill_price", 0.0)


class OrderManagementSystem:
    """
    Unified order lifecycle manager.

    Usage::

        oms = OrderManagementSystem(broker=broker, state=state,
                                    strategy_contracts={"arb_az_gov": "YES_ARIZONA_GOV", ...})
        await oms.startup()          # pre-flight recon, blocks if hard-locked
        bundle = await oms.submit(signal)
        ...
        await oms.advance_pending_bundles()   # call each cycle
    """

    def __init__(
        self,
        *,
        broker: SigBroker,
        state: SigStateStore,
        strategy_contracts: dict[str, str],  # strategy_id → contract_id
        default_fill_policies: dict[str, FillPolicy] | None = None,
        reconciliation_interval_seconds: float = 60.0,
        adopt_unmapped: bool = False,
    ) -> None:
        self._broker = broker
        self._state = state
        self._strategy_contracts = strategy_contracts
        # Mechanical floor trades many contracts under few strategy_ids; adopt venue positions outside
        # strategy_contracts instead of hard-locking on them (see reconciliation.run_pre_flight).
        self._adopt_unmapped = adopt_unmapped
        self._policies: dict[str, FillPolicy] = default_fill_policies or {}
        self._reconciliation_interval = reconciliation_interval_seconds

        # Tracked bundles: bundle_id → OrderBundle
        self._pending: dict[str, OrderBundle] = {}
        self._lock = asyncio.Lock()

        # Pre-flight lock state
        self._hard_locked = False
        self._lock_reason = ""

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def startup(self) -> ReconciliationResult:
        """
        Run pre-flight reconciliation.  Latches hard lock if state is unreconcilable.

        Ported from mbappe PreFlightReconciliationEngine.execute().
        Caller must check result.hard_locked before allowing any order submission.
        """
        result = await run_pre_flight_reconciliation(
            broker=self._broker,
            state=self._state,
            strategy_contracts=self._strategy_contracts,
            adopt_unmapped=self._adopt_unmapped,
        )
        if result.hard_locked:
            self._hard_locked = True
            self._lock_reason = result.lock_reason
            log.error(
                "oms_startup_hard_locked",
                reason=result.lock_reason,
            )
        else:
            log.info(
                "oms_startup_reconciliation_ok",
                delta_count=len(result.deltas),
                applied=result.broker_positions_applied,
            )
        return result

    # ------------------------------------------------------------------
    # Entry guard (ported from mbappe has_pending_bundle_for_leg, ~line 3396)
    # ------------------------------------------------------------------

    async def has_pending_bundle(self, strategy_id: str) -> bool:
        """Block a new order for a strategy that has an IN-FLIGHT (awaiting-resolution) bundle.

        Ported from mbappe has_pending_bundle_for_leg. EXCEPTION: market-making LEAVE_RESTING bundles
        do NOT block. MM runs one strategy_id across all 237 markets and leaves quotes resting on the
        venue; a resting quote is not "in flight", and counting it would freeze the entire MM strategy
        after its first quote (the production bug). Resting bundles are still TRACKED (partial fills +
        drift detection in advance_bundle) — they just don't gate new quotes. Non-resting bundles
        (arb UNWIND_ALL_LEGS, directional CHASE) still block as before.
        """
        async with self._lock:
            for bundle in self._pending.values():
                if bundle.strategy_id != strategy_id:
                    continue
                if bundle.fill_policy.partial_fill_policy.value == "leave_resting":
                    continue
                if getattr(bundle.fill_policy, "maker_rest", False):
                    continue                        # maker entries block per MARKET (see maker_sync), not per strategy
                return True
            return False

    # ---------------- maker entries (maker_rest bundles) ----------------
    MAKER_MAX_AGE_S = 600.0
    MAKER_FORCE_GRACE_S = 180.0      # > the 120s venue expiry margin: by then the venue has expired the order
    MAKER_CANCEL_BACKOFF_S = 30.0
    MAKER_REFRESH_S = 30.0           # status-read cadence for resting maker entries (account read budget)

    def maker_bundles(self, strategy_id: str | None = None) -> list:
        return [b for b in list(self._pending.values()) if getattr(b.fill_policy, "maker_rest", False)
                and (strategy_id is None or b.strategy_id == strategy_id)]

    def maker_resting_notional(self, since_mono: float | None = None) -> float:
        """$ of unfinished maker-entry orders PLUS maker entries that RESOLVED after `since_mono` (the account
        snapshot time). SIG reserves no cash for resting orders, and its GET /orders/{id} carries no traded
        quantity — a FILLED maker entry reads as 'canceled' — so a resolved entry is conservatively treated as
        spent until an account read taken after it shows the real cash (stress test 2026-10-09: re-entries sized
        on a pre-fill snapshot over-committed). Registered as a CapitalLedger carve-out.

        An UNRESOLVED maker bundle counts its FULL notional whatever its slices say: a slice can read finished
        (filled-as-'canceled') while the bundle waits to resolve, and in that window it was neither resting nor
        spent (hostile stress 2026-10-09: $1.3k re-used). Spent entries are dropped only once an account read
        STARTED at/after them succeeded — never by age, so a 429 storm on /account cannot make spend vanish."""
        cutoff = since_mono if since_mono is not None else time.monotonic() - 120.0
        self._maker_spent = [(t, n) for t, n in getattr(self, "_maker_spent", []) if t >= cutoff]
        tot = sum(n for _t, n in self._maker_spent)                    # >= : a read STARTED at t may predate it
        for b in self.maker_bundles():
            tot += sum(float(s.requested_qty) * float(s.price or 1.0) for s in b.slices)
        return tot

    async def maker_sync(self, strategy_id: str, desired: set, now: datetime | None = None) -> set:
        """Reconcile this strategy's resting maker entries with what it wants NOW. desired = {(contract, price)}.
          * still wanted at the same price (and younger than MAKER_MAX_AGE_S): keep resting, timeout clock reset
            (no cancel/re-place churn at the same price);
          * signal gone or price changed: cancel now (no stale fill that the exit would immediately sell).
        Returns the contracts that still have an unresolved maker bundle (new intents there must wait)."""
        now = now or datetime.now(timezone.utc)
        busy = set()
        for b in self.maker_bundles(strategy_id):
            if b.terminal_action_taken:
                continue
            for s in b.slices:
                busy.add(str(s.contract_id))
                if s.cancel_confirmed or str(s.status).lower() in FINISHED_ORDER_STATUSES or not s.order_id:
                    continue
                key = (str(s.contract_id), round(float(s.price or 0.0), 3))
                young = (now - b.created_at).total_seconds() < self.MAKER_MAX_AGE_S
                if key in desired and young:
                    b.metadata["maker_seen_at"] = now
                    continue
                mono = time.monotonic()
                if mono - s.cancel_attempt_at < self.MAKER_CANCEL_BACKOFF_S:
                    continue
                s.cancel_attempt_at = mono
                # Look before cancelling: SIG answers a DELETE of an order that already filled / expired only after
                # ~60s, with a 429 (live 2026-10-09: maker_sync stalled cycles and tripped the write breaker). One
                # read tells us; a finished order is just marked finished (advance_bundle then resolves it).
                try:
                    cur = await asyncio.wait_for(self._broker.get_order(s.order_id), timeout=10.0)
                    cur_st = str(getattr(cur, "status", "") or "").lower()
                except Exception:  # noqa: BLE001 — unknown: fall back to cancelling, as before
                    cur_st = ""
                if cur_st in FINISHED_ORDER_STATUSES:
                    s.status = cur_st
                    log.info("maker_sync_skip_cancel_finished", order_id=s.order_id, status=cur_st)
                    continue
                res = await self._broker.cancel_order(s.order_id)
                if cancel_confirmed(res):
                    s.status, s.cancel_confirmed = "cancelled", True
                    await self._state.update_order_status(s.order_id, "cancelled", metadata={
                        "reason": "maker_signal_changed" if young else "maker_max_age"})
                else:
                    log.warning("maker_sync_cancel_unconfirmed", order_id=s.order_id)
        return busy

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit(
        self,
        *,
        strategy_id: str,
        legs: list[dict[str, Any]],  # [{"contract_id": ..., "side": ..., "qty": ...}]
        fill_policy: FillPolicy | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OrderBundle:
        """
        Submit one or more legs as an atomic bundle.

        - Entry-guard: raises if a pending bundle exists for this strategy.
        - Routes through sig/broker.py (place_order or place_multi_leg).
        - Writes all orders to state via state.write_order.
        - Returns the tracked OrderBundle.
        """
        if self._hard_locked:
            raise RuntimeError(
                f"OMS is hard-locked (pre-flight): {self._lock_reason}"
            )

        # Entry guard — ported from mbappe orchestrator.py ~line 3396
        if await self.has_pending_bundle(strategy_id):
            raise RuntimeError(
                f"entry_blocked_pending_bundle_exists: strategy_id={strategy_id!r} "
                "has an unresolved fill bundle — wait for it to resolve"
            )

        policy = fill_policy or self._policies.get(strategy_id)
        if policy is None:
            raise ValueError(
                f"No FillPolicy for strategy_id={strategy_id!r}. "
                "Pass fill_policy= or register a default."
            )

        bundle_id = str(uuid.uuid4())
        is_arb = policy.strategy_class == StrategyClass.ARB
        # maker_rest: a per-BUNDLE idempotency scope. The broker's key is (strategy, market_id, side, qty, price)
        # and SIG replays the stored response for an identical key, so re-entering at the same bid and size after
        # a timeout cancel would silently place nothing. market_id is only used for the key (never sent).
        if getattr(policy, "maker_rest", False):
            legs = [{**l, "market_id": f"{l.get('market_id', l['contract_id'])}#{bundle_id}"} for l in legs]

        # IDEMPOTENCY IS OWNED BY THE BROKER. sig/broker.py computes a deterministic key from
        # (strategy_id, market_id, side, intent_hash); the OMS no longer mints or passes
        # idempotency_key= (collapse of the duplicate layer). The `idempotency_key` arg is kept on
        # the signature for back-compat but is NOT forwarded — the OMS records the key the broker used.
        if len(legs) == 1:
            leg = legs[0]
            _extra = {}
            if getattr(policy, "maker_rest", False) and leg.get("price") is not None:
                # venue-side expiry as a safety net: if this process dies the venue cancels the maker entry itself
                _extra["expiration_s"] = self.MAKER_MAX_AGE_S + 120.0
            result = await self._broker.place_order(
                strategy_id=strategy_id,
                market_id=leg.get("market_id", leg["contract_id"]),
                exchange_id=leg["contract_id"],
                side=leg["side"],                       # DIRECTION (buy/sell) → SIG action
                action=leg.get("action", "open"),
                quantity=int(round(float(leg["qty"]))),
                price=leg.get("price"),
                priority=is_arb,
                outcome=leg.get("outcome"),             # OUTCOME (yes/no) → SIG side
                **_extra,
            )
        else:
            result = await self._broker.place_multi_leg(
                strategy_id=strategy_id,
                market_id=legs[0].get("market_id", legs[0]["contract_id"]),
                legs=[
                    {
                        "exchange_id": leg["contract_id"],
                        "side": leg["side"],                 # DIRECTION (buy/sell) → SIG action
                        "action": leg.get("action", "open"),
                        "quantity": int(round(float(leg["qty"]))),
                        "price": leg.get("price"),
                        "outcome": leg.get("outcome"),       # OUTCOME (yes/no) → SIG side
                    }
                    for leg in legs
                ],
                priority=is_arb,
            )
        broker_results = _normalize_results(result, legs)

        slices: list[OrderSlice] = []
        for i, (leg, br) in enumerate(zip(legs, broker_results)):
            broker_key = _rget(br, "idempotency_key")
            sl = OrderSlice(
                order_id=_rget(br, "order_id"),
                leg_id=leg.get("leg_id", f"leg_{i}"),
                contract_id=leg["contract_id"].upper(),
                side=leg["side"],
                requested_qty=float(leg["qty"]),
                filled_qty=_rnum(br, "filled_qty", 0.0),
                avg_fill_price=_broker_avg_price(br),
                status=str(_rget(br, "status", "submitted")).lower(),
                # Capture the placement inputs so a CHASE/replace can re-quote faithfully (mirrors the
                # exact args passed to place_order above). exchange_id stays as-submitted (NOT upper()).
                market_id=str(leg.get("market_id", leg["contract_id"])),
                exchange_id=str(leg["contract_id"]),
                price=leg.get("price"),
                outcome=leg.get("outcome"),
                action=str(leg.get("action", "open")),
            )
            slices.append(sl)
            await self._state.write_order(
                {
                    "order_id": sl.order_id,
                    "bundle_id": bundle_id,
                    "strategy_id": strategy_id,
                    "contract_id": sl.contract_id,
                    "side": sl.side,
                    "qty": sl.requested_qty,
                    "status": sl.status,
                    "idempotency_key": broker_key if isinstance(broker_key, str) else None,  # broker-owned
                    "price": leg.get("price"),
                    "outcome": leg.get("outcome"),
                    "action": leg.get("action"),
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        # PHANTOM GUARD. A TRACKED bundle asserts "these orders exist at the venue". A slice with no
        # order_id that the broker reported as a no-op or as ambiguous does not provably exist, and
        # tracking it creates a bundle that awaits a fill forever — the state the idempotency bug
        # produced in bulk (34 `bundle_submitted` in 10 minutes against ONE real venue order). The
        # slices are still recorded for the audit trail; what is refused is TRACKING a bundle with
        # no provably-real leg, so a future broker surprise degrades to "placed nothing" rather than
        # "believes it placed something".
        phantom = bool(slices) and all(
            s.order_id is None
            and (bool(_rget(br, "was_noop", False)) or bool(_rget(br, "ambiguous", False)))
            for s, br in zip(slices, broker_results)
        )

        bundle = OrderBundle(
            bundle_id=bundle_id,
            strategy_id=strategy_id,
            strategy_class=policy.strategy_class,
            fill_policy=policy,
            slices=slices,
            metadata=metadata or {},
        )

        # UNTRACKABLE RESTING QUOTES. SIG's multi-leg response carries no per-leg venue order id, so an MM
        # (LEAVE_RESTING) bundle whose slices all lack an order_id can never be refreshed (advance_bundle's
        # refresh skips id-less slices) and never resolves: it sat in _pending forever, re-logged every cycle
        # (~100k debug lines / 30 min — journald then kept only ~1-2h of history) and grew memory without
        # bound. Tracking it buys nothing (MM bundles don't gate entries; the venue keeps the quote resting),
        # so don't. Fills are still attributed via the broker's leg-id recovery + order_attribution.
        untrackable = (policy.partial_fill_policy == PartialFillPolicy.LEAVE_RESTING
                       and bool(slices) and all(s.order_id is None for s in slices))

        async with self._lock:
            if bundle.needs_tracking() and not phantom and not untrackable:
                self._pending[bundle_id] = bundle

        if phantom:
            log.warning(
                "bundle_not_tracked_no_real_legs",
                bundle_id=bundle_id, strategy_id=strategy_id, leg_count=len(legs),
                note="every leg was a no-op/ambiguous with no order_id — refusing to track a phantom",
            )
        log.info(
            "bundle_submitted",
            bundle_id=bundle_id,
            strategy_id=strategy_id,
            leg_count=len(legs),
            needs_tracking=bundle.needs_tracking() and not phantom,
            phantom=phantom,
        )
        return bundle

    # ------------------------------------------------------------------
    # Cancel / replace (no native amend — ported from mbappe)
    # ------------------------------------------------------------------

    async def cancel_bundle(self, bundle_id: str) -> int:
        """Cancel all open orders in a bundle.  Returns count cancelled."""
        async with self._lock:
            bundle = self._pending.get(bundle_id)
        if bundle is None:
            return 0
        cancelled = 0
        for sl in bundle.slices:
            if sl.order_id and sl.is_open:
                ok = cancel_confirmed(await self._broker.cancel_order(sl.order_id))
                if ok:
                    sl.status = "cancelled"
                    await self._state.update_order_status(
                        sl.order_id, "cancelled", metadata={"reason": "explicit_cancel"}
                    )
                    cancelled += 1
        if any(sl.order_id and sl.is_open for sl in bundle.slices):
            return cancelled                        # something still rests: keep tracking it
        self._note_maker_resolved(bundle)           # it may have FILLED just before the cancel landed
        async with self._lock:
            self._pending.pop(bundle_id, None)
        return cancelled

    def _note_maker_resolved(self, bundle) -> None:
        """A maker entry left the book (filled, cancelled or expired — GET /orders/{id} cannot tell a fill from a
        cancel). Count its full notional as SPENT until an account read newer than now shows the real cash."""
        if not getattr(bundle.fill_policy, "maker_rest", False):
            return
        spent = sum(float(s.requested_qty) * float(s.price or 1.0) for s in bundle.slices)
        if not hasattr(self, "_maker_spent"):
            self._maker_spent = []
        self._maker_spent.append((time.monotonic(), spent))
        self.maker_resolved_flag = True             # orchestrator forces a fresh account read

    async def replace_order(
        self,
        *,
        bundle_id: str,
        leg_id: str,
        new_qty: float,
        new_price: float | None = None,
    ) -> OrderSlice | None:
        """
        Cancel a specific leg and re-submit with new params.

        Replace = cancel + resubmit (no native amend — ported from mbappe alpaca.py).
        Idempotency key ensures double-placement cannot occur if the cancel is slow.
        """
        async with self._lock:
            bundle = self._pending.get(bundle_id)
        if bundle is None:
            return None

        target = next((s for s in bundle.slices if s.leg_id == leg_id), None)
        if target is None or not target.is_open:
            return None

        # Cancel old order
        if target.order_id:
            if not cancel_confirmed(await self._broker.cancel_order(target.order_id)):
                log.warning("replace_cancel_unconfirmed_no_replace", order_id=target.order_id)
                return None                          # never place a replacement on top of a live order
            await self._state.update_order_status(
                target.order_id, "cancelled",
                metadata={"reason": "replaced"}
            )

        # Re-submit via the broker — it owns the (deterministic) idempotency key; a slow cancel
        # cannot double-place because the broker dedupes on its own key.
        prior_order_id = target.order_id
        new_result = await self._broker.place_order(
            strategy_id=bundle.strategy_id,
            market_id=target.contract_id,
            exchange_id=target.contract_id,
            side=target.side,
            action="open",
            quantity=int(round(float(new_qty))),
            price=new_price,
            priority=(bundle.strategy_class == StrategyClass.ARB),
        )
        broker_key = _rget(new_result, "idempotency_key")
        target.order_id = _rget(new_result, "order_id")
        target.requested_qty = new_qty
        target.filled_qty = _rnum(new_result, "filled_qty", 0.0)
        target.status = str(_rget(new_result, "status", "submitted")).lower()
        target.last_updated = datetime.now(timezone.utc)

        await self._state.write_order(
            {
                "order_id": target.order_id,
                "bundle_id": bundle_id,
                "strategy_id": bundle.strategy_id,
                "contract_id": target.contract_id,
                "side": target.side,
                "qty": new_qty,
                "status": target.status,
                "idempotency_key": broker_key if isinstance(broker_key, str) else None,  # broker-owned
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "replace_of": prior_order_id,
            }
        )
        log.info(
            "order_replaced",
            bundle_id=bundle_id,
            leg_id=leg_id,
            new_order_id=new_result.order_id,
            new_qty=new_qty,
        )
        return target

    # ------------------------------------------------------------------
    # Cycle advance — call once per cycle
    # ------------------------------------------------------------------

    async def advance_pending_bundles(self) -> int:
        """
        Advance all tracked bundles by one cycle.

        Ported from mbappe advance_pending_cycles.
        Returns count of bundles resolved this cycle.
        """
        async with self._lock:
            bundles = list(self._pending.items())

        resolved = 0
        mono = time.monotonic()
        for bundle_id, bundle in bundles:
            # Rate limits are PER ACCOUNT (100 reads/min across ALL keys — SIG spec). advance_bundle does one
            # GET /orders/{id} per slice per cycle (~11 cycles/min): 15 resting maker entries alone were ~165
            # reads/min (429 storm 2026-10-09). Resting maker entries change slowly -> refresh them at most every
            # MAKER_REFRESH_S. Every other bundle keeps the per-cycle refresh.
            # A bundle we just acted on (cancel confirmed / forced) is read at once so it resolves promptly.
            if (getattr(bundle.fill_policy, "maker_rest", False) and not bundle.terminal_action_taken
                    and not any(getattr(s, "cancel_confirmed", False) for s in bundle.slices)):
                last = bundle.metadata.get("_refreshed_mono")
                if last is not None and mono - last < self.MAKER_REFRESH_S:
                    continue
                bundle.metadata["_refreshed_mono"] = mono
            done = await advance_bundle(bundle, self._broker, self._state)
            if done:
                self._note_maker_resolved(bundle)
            if done:
                async with self._lock:
                    self._pending.pop(bundle_id, None)
                resolved += 1

        return resolved

    # ------------------------------------------------------------------
    # Unfilled-order timeout sweep
    # ------------------------------------------------------------------

    async def sweep_unfilled_timeouts(self) -> int:
        """
        Cancel any order that has been open longer than its policy timeout.

        For LEAVE_RESTING (market-making) policies the timeout is not enforced
        — resting quotes may live indefinitely.
        Returns count of orders cancelled.
        """
        now = datetime.now(timezone.utc)
        async with self._lock:
            bundles = list(self._pending.values())

        cancelled = 0
        for bundle in bundles:
            policy = bundle.fill_policy
            # Market-making: leave resting orders alone
            if policy.partial_fill_policy.value == "leave_resting":
                continue
            elapsed = (now - bundle.created_at).total_seconds()
            if getattr(policy, "maker_rest", False):
                # timeout measured from the last time the strategy still wanted this exact order (maker_sync)
                seen = bundle.metadata.get("maker_seen_at") or bundle.created_at
                if (now - seen).total_seconds() < policy.unfilled_timeout_seconds and elapsed < self.MAKER_MAX_AGE_S:
                    continue
            elif elapsed < policy.unfilled_timeout_seconds:
                continue
            if getattr(policy, "maker_rest", False):
                # cancel anything that may still rest (open, partially_filled, unknown); trust ONLY a confirmed
                # cancel — a refused/failed cancel stays tracked and is retried next cycle.
                mono = time.monotonic()
                for sl in bundle.slices:
                    if (not sl.order_id or sl.cancel_confirmed
                            or str(sl.status).lower() in FINISHED_ORDER_STATUSES):
                        continue
                    if mono - sl.cancel_attempt_at < self.MAKER_CANCEL_BACKOFF_S:
                        continue                    # don't burn the shared write budget re-cancelling every cycle
                    sl.cancel_attempt_at = mono
                    # look before cancelling (same as maker_sync): a DELETE of an order that already filled /
                    # expired is answered late with a 429 (2026-10-10: one such cancel held a cycle for 38 min)
                    try:
                        cur = await asyncio.wait_for(self._broker.get_order(sl.order_id), timeout=10.0)
                        cur_st = str(getattr(cur, "status", "") or "").lower()
                    except Exception:  # noqa: BLE001 — unknown: cancel as before
                        cur_st = ""
                    if cur_st in FINISHED_ORDER_STATUSES:
                        sl.status = cur_st
                        log.info("maker_timeout_skip_cancel_finished", order_id=sl.order_id, status=cur_st)
                        continue
                    res = await self._broker.cancel_order(sl.order_id)
                    if cancel_confirmed(res):
                        sl.status, sl.cancel_confirmed = "cancelled", True
                        await self._state.update_order_status(sl.order_id, "cancelled",
                                                              metadata={"reason": "maker_timeout"})
                        cancelled += 1
                    else:
                        log.warning("maker_timeout_cancel_unconfirmed", order_id=sl.order_id,
                                    status=getattr(res, "status", None))
                # ESCAPE HATCH: a maker bundle can never pin the strategy forever (id-less slice, venue that keeps
                # reporting partially_filled, persistent read failures). Past max age + grace, and with every slice
                # either cancel-confirmed or past its venue expiry, resolve it and log loudly.
                if elapsed >= self.MAKER_MAX_AGE_S + self.MAKER_FORCE_GRACE_S:
                    log.error("maker_bundle_force_resolved", bundle_id=bundle.bundle_id,
                              statuses=[str(s.status) for s in bundle.slices],
                              ids=[s.order_id for s in bundle.slices])
                    bundle.terminal_action_taken = True
                continue
            for sl in bundle.slices:
                if sl.is_open and sl.order_id:
                    ok = cancel_confirmed(await self._broker.cancel_order(sl.order_id))
                    if ok:
                        sl.status = "cancelled"
                        await self._state.update_order_status(
                            sl.order_id, "cancelled",
                            metadata={"reason": "unfilled_timeout"}
                        )
                        cancelled += 1
            log.warning(
                "unfilled_order_timeout_cancelled",
                bundle_id=bundle.bundle_id,
                strategy_id=bundle.strategy_id,
                elapsed_seconds=elapsed,
                timeout=policy.unfilled_timeout_seconds,
            )
            # Let advance_bundle handle cleanup next cycle
        return cancelled

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def reconcile(self, trigger: str = "periodic") -> ReconciliationResult:
        """
        Compare believed state vs broker truth.  Broker wins.

        Also clears halt if the prior reason was a transient mismatch that is
        now resolved (operator manually corrected, fill arrived, etc.).
        """
        result = await reconcile_against_broker(
            broker=self._broker,
            state=self._state,
            strategy_contracts=self._strategy_contracts,
            trigger=trigger,
            adopt_unmapped=self._adopt_unmapped,
        )
        if result.hard_locked:
            self._hard_locked = True
            self._lock_reason = result.lock_reason
        return result

    # ------------------------------------------------------------------
    # Halt management
    # ------------------------------------------------------------------

    @property
    def is_hard_locked(self) -> bool:
        return self._hard_locked

    async def clear_halt(self, *, operator_note: str = "") -> None:
        """Clear the halt-hold after an operator has manually resolved the issue."""
        self._hard_locked = False
        self._lock_reason = ""
        await self._state.clear_halt()
        await self._state.append_audit(
            {
                "event": "halt_cleared",
                "operator_note": operator_note,
                "cleared_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.warning("oms_halt_cleared", operator_note=operator_note)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def pending_bundle_count(self) -> int:
        async with self._lock:
            return len(self._pending)

    async def get_bundle(self, bundle_id: str) -> OrderBundle | None:
        async with self._lock:
            return self._pending.get(bundle_id)
