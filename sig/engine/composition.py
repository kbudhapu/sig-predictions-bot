"""Composition root — wire the REAL sig-bot modules to the orchestrator's/OMS's Protocols.

The orchestrator and OMS were built against ASSUMED Protocol interfaces. The real modules
differ (method names, sync/async, return types). This module is the single place where the
real broker / risk-gate / sizer / allocator / state / OMS are instantiated and ADAPTED to
the interfaces the orchestrator injects — so the orchestrator runs a real cycle.

Adapters (assumed interface  ←  real method):
  RiskGateAdapter.validate(intent, qty, cap)        ← RiskGate.check_order(order, portfolio_state, tourney)
  SizingAdapter.size(intent, avail, budget)         ← Sizer.k_for(σ) × Sizer.browne_multiplier(standing)   (real sync primitives)
  AllocatorAdapter.budget_for(strategy_id)          ← Allocator.propose() → weights[edge] × capital        (refreshed per cycle)
  StateAdapter.{set_halt,write_heartbeat,read_exposure,…}  ← real StateStore where shapes match; halt/heartbeat added here

Broker: SimulatedBroker below implements the canonical BrokerAPI + the venue-truth reads for the
proof. The LIVE SigBroker ALSO implements get_positions/get_open_orders/get_account now (sourced
from the SIG read client, see sig/broker.py:501-522), so no broker read extension is outstanding.
Legacy note (now resolved): the LIVE
SigBroker previously lacked those reads. Idempotency stays BROKER-OWNED (keys below are
computed with sig/broker.py's own intent_hash/idempotency_key — the OMS never mints keys).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import structlog

log = structlog.get_logger()

from sig.broker import OrderResult, intent_hash, idempotency_key
from sig.engine.orchestrator import Orchestrator
from sig.engine.protocols import RiskGateVerdict, SizingResult
from sig.oms.manager import OrderManagementSystem
from sig.oms.models import FillPolicy
from strategies.allocator.allocator import Allocator, EdgeState
from strategies.risk_gate.gate import RiskGate
from strategies.sizing.sizing import Sizer, Standing


# --------------------------------------------------------------------------- #
# Account snapshot (orchestrator reads .buying_power / .equity)
# --------------------------------------------------------------------------- #
@dataclass
class AccountSnapshot:
    buying_power: float
    equity: float


# --------------------------------------------------------------------------- #
# SimulatedBroker — canonical BrokerAPI + venue-truth reads + get_account.
# Idempotency is broker-owned (keys via sig/broker.py's own helpers).
# --------------------------------------------------------------------------- #
class SimulatedBroker:
    """Instant-fill paper broker implementing the canonical broker interface the OMS uses,
    plus get_account/get_positions/get_open_orders. For tests and SIM mode."""

    def __init__(self, *, buying_power: float = 100_000.0, equity: float = 100_000.0,
                 track_cash: bool = False) -> None:
        self._bp = float(buying_power)
        self._equity = float(equity)
        self.placed: list[dict] = []     # audit of what was placed (for assertions)
        self._orders: dict[str, dict] = {}   # order_id → {status, filled_qty, avg_price} (truthful get_order)
        # CASH/INVENTORY TRACKING (opt-in, default OFF so every existing test keeps its old
        # semantics: a static venue where buying power never moves and positions read empty).
        #
        # With it ON, a buy debits cash and adds inventory while a sell credits cash and removes it.
        # That is required to demonstrate capital RECYCLING at all: with a frozen buying power you
        # cannot observe capital being consumed by an entry and then returned by an exit, so a
        # "capital recycles" test would pass without testing anything.
        self._track_cash = bool(track_cash)
        self._positions: dict[str, dict] = {}   # EXCHANGE_ID -> {qty, avg_entry_price}

    def _apply_fill(self, exchange_id: str, action: str, quantity: int, price: float) -> None:
        """Move cash and inventory for one filled leg. No-op unless track_cash=True.

        A "sell" here means exposure-REDUCING (an exit): it credits proceeds and reduces inventory,
        and is clamped so inventory can never go negative -- this double is not a venue that supports
        naked shorts, and silently allowing one would let a test 'prove' a recycle that could not
        happen live.
        """
        if not self._track_cash:
            return
        eid = str(exchange_id).upper()
        qty = float(quantity)
        px = float(price)
        row = self._positions.setdefault(eid, {"qty": 0.0, "avg_entry_price": 0.0})
        if str(action).lower() == "sell":
            sold = min(qty, float(row["qty"]))
            if sold <= 0.0:
                return
            self._bp += sold * px
            row["qty"] = float(row["qty"]) - sold
            if row["qty"] <= 0.0:
                self._positions.pop(eid, None)
        else:
            cost = qty * px
            self._bp -= cost
            prior_qty = float(row["qty"])
            prior_cost = prior_qty * float(row["avg_entry_price"])
            row["qty"] = prior_qty + qty
            row["avg_entry_price"] = ((prior_cost + cost) / row["qty"]) if row["qty"] else 0.0

    def _key(self, strategy_id: str, market_id: str, side: str, quantity: int, price: float | None) -> str:
        h = intent_hash(exchange_id=market_id, action=side, quantity=int(quantity), price=price)
        return idempotency_key(strategy_id, market_id, side, h)

    async def place_order(self, *, strategy_id: str, market_id: str, exchange_id: str, side: str,
                          action: str, quantity: int, price: float | None = None,
                          priority: bool = False, outcome: str | None = None,
                          expiration_s: float | None = None, send_deadline_s: float | None = None) -> OrderResult:
        self.placed.append({"strategy_id": strategy_id, "exchange_id": exchange_id,
                            "side": side, "action": action, "quantity": int(quantity), "price": price,
                            "outcome": outcome})
        oid = f"sim-{uuid.uuid4().hex[:10]}"
        avg = float(price if price is not None else 0.5)
        self._orders[oid] = {"status": "filled", "filled_qty": float(quantity), "avg_price": avg}
        self._apply_fill(exchange_id, action, int(quantity), avg)
        return OrderResult(
            idempotency_key=self._key(strategy_id, market_id, side, quantity, price),
            status="filled", order_id=oid, filled_qty=float(quantity), avg_price=avg,
        )

    async def place_multi_leg(self, *, strategy_id: str, market_id: str, legs: list[dict],
                              priority: bool = True) -> OrderResult:
        leg_results = []
        for leg in legs:
            q = int(leg["quantity"]); px = leg.get("price")
            oid = f"sim-{uuid.uuid4().hex[:10]}"
            avg = float(px if px is not None else 0.5)
            self._orders[oid] = {"status": "filled", "filled_qty": float(q), "avg_price": avg}
            self.placed.append({"strategy_id": strategy_id, "exchange_id": leg["exchange_id"],
                                "side": leg["side"], "action": leg.get("action", "open"),
                                "quantity": int(q), "price": px})
            self._apply_fill(leg["exchange_id"], leg.get("action", "open"), int(q), avg)
            leg_results.append({
                "order_id": oid, "status": "filled", "filled_qty": float(q), "avg_price": avg,
                "exchange_id": leg["exchange_id"],
                "idempotency_key": self._key(strategy_id, market_id, leg["side"], q, px),
            })
        return OrderResult(
            idempotency_key=self._key(strategy_id, market_id, "multi", 0, None),
            status="filled", order_id=f"sim-ml-{uuid.uuid4().hex[:8]}",
            filled_qty=sum(r["filled_qty"] for r in leg_results),
            avg_price=0.0, legs=leg_results,
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        if order_id in self._orders:
            self._orders[order_id]["status"] = "canceled"
        return OrderResult(idempotency_key="", status="canceled", order_id=order_id)

    async def cancel_all(self) -> OrderResult:
        for o in self._orders.values():
            o["status"] = "canceled"
        return OrderResult(idempotency_key="", status="canceled", order_id="")

    async def get_order(self, order_id: str) -> OrderResult:
        o = self._orders.get(order_id)
        if o is None:
            return OrderResult(idempotency_key="", status="filled", order_id=order_id)
        return OrderResult(idempotency_key="", status=o["status"], order_id=order_id,
                           filled_qty=float(o["filled_qty"]), avg_price=float(o["avg_price"]))

    async def get_positions(self) -> list[Any]:
        if not self._track_cash:
            return []      # flat sim venue → reconciliation sees no positions (default)
        return [
            SimpleNamespace(contract_id=eid, qty=float(r["qty"]), side="long",
                            avg_entry_price=float(r["avg_entry_price"]))
            for eid, r in self._positions.items() if float(r["qty"]) != 0.0
        ]

    async def get_open_orders(self) -> list[Any]:
        return []

    async def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(buying_power=self._bp, equity=self._equity)


# --------------------------------------------------------------------------- #
# RiskGate adapter:  validate(intent, qty, cap)  →  real check_order(order, portfolio_state)
# --------------------------------------------------------------------------- #
@dataclass
class MultiLegVerdict:
    approved: bool
    reason: str = ""
    legs: list[dict] | None = None   # gate-clamped legs (hedge-preserving) when approved


class RiskGateAdapter:
    def __init__(self, gate: RiskGate, *, balance: float,
                 nav_source: Callable[[], float | None] | None = None) -> None:
        self._gate = gate
        self.balance = float(balance)   # the gate's fraction caps are vs the bankroll; set per cycle
        # Live NAV (equity = free cash + marked positions) source — broker.last_equity. The exposure/
        # deploy caps size off TOTAL capital at risk, not free cash: seeding `balance` from the venue's
        # cash-only /account froze the 0.85 deploy cap at HALF the real equity once ~half the book was
        # deployed, falsely tripping no_room_to_add_exposure. When this returns a positive value it
        # overrides the build-time seed each cycle; None (SIM / tests) keeps the static balance.
        self._nav_source = nav_source
        # Held positions, keyed by exchange_id, as the gate's portfolio_state wants them.
        # THIS IS LOAD-BEARING FOR EXITS: gate.check_order's sell branch reads
        #   held = portfolio_state["positions"][exch]["quantity"]
        # and REJECTS with "sell_without_holding" when it finds nothing -- correctly, since a sell
        # with no position would open a naked short through the cap-exempt path. Passing a permanently
        # empty dict (as this adapter used to) therefore rejected EVERY sell, which silently made
        # exits impossible. The orchestrator refreshes this each cycle from the state snapshot.
        self.positions: dict[str, dict] = {}
        # exchange_id -> cluster id (one per RACE: its D and R legs share it). Without this the gate treats every
        # market as its own cluster, so max_cluster_frac never bound and a race could hold long-D + short-R at
        # 2 × max_per_market_frac. Set once at startup from SIG market titles (main_sig).
        self.market_clusters: dict[str, str] = {}

    def set_market_clusters(self, clusters: dict[str, str] | None) -> None:
        self.market_clusters = {str(k).upper(): str(v) for k, v in (clusters or {}).items()}

    PENDING_TTL_S = 90.0

    def note_pending(self, exchange_id: Any, notional: float) -> None:
        """Count a just-submitted BUY as exposure until venue sync can see it. The gate's positions refresh
        ~every 20s (longer when /positions is 429'd), so back-to-back orders on one market each passed the
        per-market cap against the SAME stale view (995 reached 11% vs a 6% cap). Expires after PENDING_TTL_S
        — briefly double-counting after the sync lands, which errs conservative."""
        import time as _t
        try:
            pend = getattr(self, "_pending", None)
            if pend is None:
                pend = self._pending = []
            pend.append((str(exchange_id).upper(), max(float(notional), 0.0), _t.monotonic()))
        except (TypeError, ValueError):
            pass

    def _pending_by_market(self) -> dict[str, float]:
        import time as _t
        pend = getattr(self, "_pending", None) or []
        now = _t.monotonic()
        pend = [p for p in pend if now - p[2] <= self.PENDING_TTL_S]
        self._pending = pend
        out: dict[str, float] = {}
        for ex, n, _ts in pend:
            out[ex] = out.get(ex, 0.0) + n
        return out

    def set_positions(self, positions: dict[str, dict] | None) -> None:
        """Refresh held positions for this cycle. Keys are normalised to upper-case because the OMS
        writes contract ids upper-cased (fill_handler) while intents carry them as-is."""
        self.positions = {
            str(k).upper(): dict(v) for k, v in (positions or {}).items() if isinstance(v, dict)
        }

    def _live_balance(self) -> float:
        """The capital base the fraction caps size against: venue-truth NAV when available, else the
        build-time seed. Best-effort — a failing/empty NAV read falls back to the seed (never raises)."""
        src = self._nav_source
        if src is not None:
            try:
                v = src()
                if v is not None and float(v) > 0.0:
                    return float(v)
            except Exception:  # noqa: BLE001 — a NAV read must never break the gate
                pass
        return self.balance

    def _portfolio_state(self) -> dict:
        # The gate looks up positions by the order's exchange_id VERBATIM, so expose every key in
        # both its upper- and lower-case form. Ids are numeric strings in this tournament, making
        # this a no-op today, but a miss here degrades to "sell_without_holding" -- a silent refusal
        # to exit -- so it is worth being insensitive to case rather than assuming.
        pos: dict[str, dict] = {}
        deployed = 0.0
        for k, v in self.positions.items():
            # The gate reads per-position `notional` for its per-market and per-cluster room, and
            # sums it for `deployed`. The state layer stores quantity + avg_price, so derive it --
            # without this the gate saw notional 0 everywhere, which made the aggregate deploy cap
            # and the per-market cap compute FULL room on every order no matter how much was already
            # open. That is the constraint "many smaller arbs" has to live inside, so it has to be real.
            row = dict(v)
            if "notional" not in row:
                try:
                    row["notional"] = abs(float(row.get("quantity", 0) or 0)) * float(
                        row.get("avg_price", 0) or 0)
                except (TypeError, ValueError):
                    row["notional"] = 0.0
            deployed += float(row.get("notional", 0.0) or 0.0)
            pos[k] = row
            pos.setdefault(str(k).lower(), row)
        for ex, n in self._pending_by_market().items():
            row = pos.get(ex) or {"quantity": 0, "avg_price": 0.0, "notional": 0.0}
            row = dict(row)
            row["notional"] = float(row.get("notional", 0.0) or 0.0) + n
            pos[ex] = row
            pos[str(ex).lower()] = row
            deployed += n
        mc = dict(self.market_clusters)
        mc.update({k.lower(): v for k, v in self.market_clusters.items()})
        return {"balance": self._live_balance(), "positions": pos, "deployed": deployed, "market_cluster": mc}

    def _held(self, exchange_id: Any) -> int:
        row = self.positions.get(str(exchange_id).upper()) or {}
        try:
            return int(float(row.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def validate(self, intent: Any, sized_qty: float, available_capital: float) -> RiskGateVerdict:
        # The gate's `action` is exposure-direction (buy/sell) = the intent's side; `quantity` int; price on-grid.
        order = {
            "exchange_id": intent.exchange_id,
            "market_id": getattr(intent, "exchange_id", ""),
            "action": intent.side,                       # buy = add exposure, sell = reduce
            "side": intent.side,
            "quantity": int(round(float(sized_qty))),
            "price": float(intent.price if intent.price is not None else 0.5),
            "strategy_id": intent.strategy_id,
            # Lets the gate apply the ARB cap instead of the directional per-market cap.
            "strategy_type": str(getattr(intent, "strategy_type", "") or ""),
        }
        portfolio_state = self._portfolio_state()
        gr = self._gate.check_order(order, portfolio_state, None)
        if not gr.approved:
            return RiskGateVerdict(approved=False, reason=gr.reason)
        final = int(gr.order["quantity"]) if gr.order else order["quantity"]
        clamped = final if gr.clamped else None
        return RiskGateVerdict(approved=True, clamped_qty=clamped, reason=gr.reason or "")

    def validate_multi_leg(self, legs: list[dict], *, available_capital: float = 0.0,
                           is_exit: bool = False, strategy_type: str = "arb") -> MultiLegVerdict:
        """Gate ALL arb legs together via the REAL gate.check_multi_leg (hedge-preserving: every leg
        clamped by the same factor, or the whole batch rejected). Maps clamped quantities back onto
        the intent legs.

        `is_exit=True` gates the legs as exposure-REDUCING sells (cap-exempt, clamped to held) rather
        than as the entry path does. See the comment in the body for why entry is left untouched.
        """
        # `action` is the gate's EXPOSURE DIRECTION ("buy" adds, "sell" reduces).
        #
        # For an EXIT it must come from the leg's own `action`, because in this tournament Leg.side
        # is always "yes" (relationship_engine/contracts.py) while Leg.action carries buy/sell.
        # The entry mapping below passes `side`, which hands the gate the literal string "yes" --
        # matching neither branch, so it falls through to the exposure-ADDING path.
        #
        # That is wrong in general, but it is deliberately LEFT ALONE for entries: an entry
        # `sell_all` basket (the engine's overpriced-basket direction) also carries action="sell",
        # and routing it to the sell branch would reject it as "sell_without_holding" -- the gate
        # correctly refusing to open a naked short. Fixing that properly means deciding how a
        # short basket should be expressed to the gate at all, which is an ENTRY-path question and
        # out of scope here. So exits opt in explicitly and entry gating is byte-identical.
        if is_exit:
            gate_legs = [{"exchange_id": l["exchange_id"],
                          "action": str(l.get("action") or "sell").lower(),
                          "side": l.get("side", "yes"),
                          "quantity": int(l["quantity"]), "price": float(l.get("price") or 0.5)}
                         for l in legs]
        else:
            gate_legs = [{"exchange_id": l["exchange_id"], "action": l["side"], "side": l["side"],
                          "quantity": int(l["quantity"]), "price": float(l.get("price") or 0.5)}
                         for l in legs]
        # Stamp the basket's type so the gate applies the ARB cap per leg rather than the
        # directional one. Defaults to "arb" because this method exists for atomic arb baskets;
        # pass strategy_type explicitly for anything else.
        is_arb = str(strategy_type or "").strip().lower() == "arb"
        for gl in gate_legs:
            gl["strategy_type"] = str(strategy_type or "")
            gl["is_arb"] = is_arb
        ps = self._portfolio_state()
        gr = self._gate.check_multi_leg(gate_legs, ps, None)
        if not gr.approved:
            return MultiLegVerdict(approved=False, reason=gr.reason)
        clamped = (gr.order or {}).get("legs", gate_legs)
        out = []
        for orig, cl in zip(legs, clamped):
            d = dict(orig)
            d["quantity"] = int(cl["quantity"])
            out.append(d)
        return MultiLegVerdict(approved=True, reason=gr.reason or "", legs=out)


# --------------------------------------------------------------------------- #
# Sizing adapter:  size(intent, avail, budget)  →  real Sizer sync primitives (k_for × browne)
# --------------------------------------------------------------------------- #
class SizingAdapter:
    def __init__(self, sizer: Sizer, *, balance: float, time_fraction_remaining: float = 0.5,
                 third_place_threshold: float | None = None) -> None:
        self._sizer = sizer
        self.balance = float(balance)
        self._tau = float(time_fraction_remaining)
        self._t3 = third_place_threshold
        # PER-TIER KELLY AGGRESSIVENESS (catch-up): scale the base k(σ_p) by the edge's provenness and
        # raise the fraction-of-budget ceiling so a PROVEN edge can reach full/double Kelly when behind,
        # while an unproven one stays quarter-ish. Read from sizing config (sizer.cfg.raw); absent ⇒ the
        # prior behaviour (×1, ceiling 1.0). The per-market/deploy caps still bind downstream in the gate.
        raw_sizing = (getattr(getattr(sizer, "cfg", None), "raw", {}) or {}).get("sizing", {}) or {}
        self._kelly_mult: dict = raw_sizing.get("kelly_multipliers", {}) or {}
        try:
            self._kelly_ceiling = float(raw_sizing.get("kelly_frac_ceiling", 1.0))
        except (TypeError, ValueError):
            self._kelly_ceiling = 1.0

    def _tier_multiplier(self, intent: Any) -> float:
        # Scale k(σ_p) by the edge's PROVENNESS. kelly_multipliers is keyed by edge IDENTITY (the
        # strategy_id, e.g. 'cross_market_divergence'), so look up strategy_id FIRST, then strategy_type,
        # then 'default'. NB: the live cross-market intent carries strategy_type='directional' but
        # strategy_id='cross_market_divergence' — a type-ONLY lookup silently handed it the ×1 default
        # (4× under-Kelly) despite the config's ×4 'proven' tier. strategy_id-first fixes that.
        km = self._kelly_mult or {}
        for key in (getattr(intent, "strategy_id", None), getattr(intent, "strategy_type", None)):
            if key and str(key) in km:
                try:
                    return float(km[str(key)])
                except (TypeError, ValueError):
                    return 1.0
        try:
            return float(km.get("default", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def size(self, intent: Any, available_capital: float, allocator_budget: float) -> SizingResult:
        sigma_p = intent.metadata.get("sigma_p")                       # None ⇒ k_missing_sigma (zero size)
        mult = self._tier_multiplier(intent)
        k = self._sizer.k_for(sigma_p) * mult                          # k(σ_p) scaled by edge provenness
        standing = Standing(balance=self.balance, third_place_threshold=self._t3,
                            time_fraction_remaining=self._tau)
        m = self._sizer.browne_multiplier(standing)                    # REAL tournament multiplier (↑ when behind)
        # An edge whose multiplier is ≤1 is still capped at a FULL budget (1.0); a proven edge (mult>1)
        # may target up to kelly_frac_ceiling (e.g. DOUBLE), bounded next by available capital and the gate.
        ceiling = min(self._kelly_ceiling, max(1.0, mult))
        frac = max(0.0, min(ceiling, k * (m if m is not None else 1.0)))
        budget = max(float(allocator_budget), 0.0)
        price = float(intent.price if intent.price is not None else 0.5)
        notional = min(frac * budget, float(available_capital))
        qty = int(notional / price) if price > 0 else 0
        return SizingResult(qty=float(qty), capital_commitment=qty * price)


# --------------------------------------------------------------------------- #
# Allocator adapter:  budget_for(sid) (sync)  →  real async propose() → weights[edge] × capital
# --------------------------------------------------------------------------- #
class AllocatorAdapter:
    def __init__(self, allocator: Allocator, *, capital: float) -> None:
        self._alloc = allocator
        self._capital = float(capital)
        self._weights: dict[str, float] = {}
        self._states: dict[str, EdgeState] = {}

    async def refresh(self) -> None:
        """Recompute per-edge budgets from realized performance. Call once per cycle (the real
        propose() is a whole-book async computation — not called per-intent)."""
        result = await self._alloc.propose(self._states or None)
        self._weights = result["weights"]

    def budget_for(self, strategy_id: str) -> float:
        return self._weights.get(strategy_id, 0.0) * self._capital     # fraction → dollars

    def on_fill(self, strategy_id: str, pnl: float) -> None:
        st = self._states.setdefault(strategy_id, EdgeState(strategy_id))
        st.returns.append(pnl / self._capital if self._capital else 0.0)

    def seed(self, strategies: list[Any]) -> None:
        for s in strategies:
            name = getattr(s, "name", s)
            self._states.setdefault(str(name), EdgeState(str(name)))


# --------------------------------------------------------------------------- #
# State adapter:  provides the orchestrator/OMS state Protocol. Wraps a real StateStore for the
# methods whose shapes match (best-effort); provides halt/heartbeat + a dict-shaped read_exposure
# (the real store returns an Exposure object / lacks halt+heartbeat — the bridged gap).
# --------------------------------------------------------------------------- #
class StateAdapter:
    def __init__(self, store: Any | None = None) -> None:
        self._store = store
        self._halt: dict[str, Any] | None = None
        self._heartbeat_ts: str | None = None
        self._orders: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}
        self._audit: list[dict] = []
        self._stale_markets: set[str] = set()   # SIM: exchanges whose prices are stale
        self._feed_dead: bool = False            # SIM: global feed-death flag
        self._baskets: dict[str, dict] = {}      # SIM: basket_id -> open/closed basket record

    # -- feed staleness (SEAM-2): real wiring delegates to StateStore.stale_exchanges / FeedHealth --
    def set_stale_markets(self, markets) -> None:
        self._stale_markets = {str(m).upper() for m in markets}

    def set_feed_dead(self, dead: bool) -> None:
        self._feed_dead = bool(dead)

    async def stale_exchanges(self, max_age_seconds: float) -> list[str]:
        if self._store is not None and hasattr(self._store, "stale_exchanges"):
            try:
                return list(await self._store.stale_exchanges(max_age_seconds))
            except Exception:
                pass
        return list(self._stale_markets)

    async def is_feed_dead(self) -> bool:
        return self._feed_dead

    async def _try(self, name: str, *args, **kwargs) -> Any:
        """Best-effort delegate to the real store; swallow shape mismatches (bridged in-memory)."""
        if self._store is None:
            return None
        fn = getattr(self._store, name, None)
        if fn is None:
            return None
        try:
            return await fn(*args, **kwargs)
        except Exception:
            return None

    async def write_order(self, order: dict[str, Any]) -> None:
        """Record an intended order in the local ledger.

        SHAPE BRIDGE — load-bearing. The OMS calls this with a dict {order_id, bundle_id,
        strategy_id, contract_id, side, qty, status, idempotency_key?}, but StateStore.write_order
        is KEYWORD-ONLY (idempotency_key, exchange_id, side, action, quantity, ...). Forwarding the
        dict positionally raised TypeError on EVERY call and `_try` swallowed it — so the local
        `orders` table was permanently EMPTY. Its idempotency_key PRIMARY KEY is our own dedup
        ledger and the only local record of what we have sent, so losing it silently removed the
        client-side half of idempotency.

        Falls back to the order id when no idempotency key is carried (a unique row is the point);
        a record with neither is skipped rather than written under a bogus key.
        """
        self._orders[str(order.get("order_id"))] = dict(order)
        key = order.get("idempotency_key") or order.get("idempotencyKey") or order.get("order_id")
        if not key:
            return
        exchange_id = (order.get("exchange_id") or order.get("contract_id")
                       or order.get("exchangeId") or "")
        # The OMS carries the buy/sell DIRECTION in `side` and the yes/no OUTCOME in `outcome`,
        # while the state table wants action=direction and side=outcome. Map, don't pass through.
        direction = str(order.get("action") or order.get("side") or "buy").lower()
        if direction not in ("buy", "sell"):
            direction = "buy"
        await self._try(
            "write_order",
            idempotency_key=str(key),
            exchange_id=str(exchange_id),
            side=str(order.get("outcome") or "yes").lower(),
            action=direction,
            quantity=int(float(order.get("qty") or order.get("quantity") or 0) or 0),
            price=order.get("price"),
            status=str(order.get("status") or "pending").lower(),
            strategy=str(order.get("strategy_id") or order.get("strategy") or "unknown"),
            broker_order_id=(str(order["order_id"]) if order.get("order_id") else None),
        )

    async def update_order_status(self, order_id: str, status: str, *, filled_qty: float | None = None,
                                  avg_fill_price: float | None = None, metadata: dict | None = None) -> None:
        if order_id in self._orders:
            self._orders[order_id]["status"] = status
        # `order_id` here is the VENUE order id → key on broker_order_id (NOT idempotency_key). The old
        # call passed it to update_order_status(idempotency_key=...), so it matched nothing and order
        # statuses never updated. Pass fill data through too (COALESCE keeps it when absent).
        await self._try(
            "update_order_by_broker_id", str(order_id), status=status,
            filled_quantity=(int(filled_qty) if filled_qty is not None else None),
            fill_price=avg_fill_price,
        )

    async def upsert_position(self, position: dict[str, Any]) -> None:
        """Record a broker-authoritative position, in-memory AND in the real store.

        THE TRANSLATION IS LOAD-BEARING, NOT COSMETIC. The OMS speaks dicts
        (`{strategy_id, contract_id, qty, avg_entry_price}`) while `StateStore.upsert_position` is
        `(exchange_id, *, quantity, avg_price)` — keyword-only. Forwarding the raw dict positionally
        therefore raised TypeError on EVERY call, and `_try` swallowed it, so with a real store
        wired (`build_live_stack`) the `positions` table silently stayed empty forever. That is why
        nothing could compute exposure or a mark from it. Mapping the keys here is what makes
        `StateStore.mark_to_market()` and the orchestrator's `snapshot["positions"]` have anything
        to work on in the live stack.

        A position with no contract_id is kept in memory but not forwarded: the state table is keyed
        by exchange_id, so a row with no key is not a row.
        """
        self._positions[str(position.get("strategy_id"))] = dict(position)
        exchange_id = position.get("contract_id") or position.get("exchange_id")
        if exchange_id is None:
            return
        await self._try(
            "upsert_position",
            str(exchange_id).upper(),
            quantity=float(position.get("qty", 0.0) or 0.0),
            avg_price=float(position.get("avg_entry_price", 0.0) or 0.0),
            updated_at=position.get("updated_at") or position.get("reconciled_at"),
        )

    async def read_open_orders(self) -> list[dict[str, Any]]:
        return [o for o in self._orders.values() if str(o.get("status", "")).lower() in
                {"open", "new", "accepted", "pending_new", "submitted"}]

    async def read_exposure(self) -> dict[str, Any]:
        # OMS reconciliation does exposure.get(sid); return a per-strategy believed-position dict.
        return {sid: {"qty": p.get("qty", 0.0), "side": p.get("side", "flat"),
                      "avg_entry_price": p.get("avg_entry_price", 0.0)}
                for sid, p in self._positions.items()}

    # -- position / price / basket VISIBILITY (what an exit decision needs) -------------------- #
    # read_exposure above answers "how much is deployed in total", which is all ENTRY sizing needs.
    # Closing a position needs it named individually, so these expose the per-position,
    # per-price and per-basket views. Real store first (it is truth); in-memory only as the SIM
    # fallback, normalised to the StateStore shapes so a consumer sees ONE shape in both stacks.

    async def read_positions(self) -> dict[str, dict[str, Any]]:
        """Per-exchange positions in StateStore shape, keyed by exchange_id.

        The in-memory fallback is re-keyed from strategy_id to contract_id, because the SIM record
        is written per (strategy, contract) while the state layer — and therefore anything reading
        this — keys everything by exchange_id. `unrealized_pnl` is 0.0 in the fallback and that is
        honest: the SIM adapter holds no price table, so there is nothing to mark against. Only the
        real store's `mark_to_market` produces a meaningful number.
        """
        real = await self._try("read_positions")
        if isinstance(real, dict) and real:
            return real
        out: dict[str, dict[str, Any]] = {}
        for pos in self._positions.values():
            exchange_id = str(pos.get("contract_id") or pos.get("exchange_id") or "").upper()
            if not exchange_id:
                continue
            out[exchange_id] = {
                "exchange_id": exchange_id,
                "quantity": float(pos.get("qty", 0.0) or 0.0),
                "avg_price": float(pos.get("avg_entry_price", 0.0) or 0.0),
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "updated_at": pos.get("updated_at") or pos.get("reconciled_at"),
            }
        return out

    async def read_attribution_map(self) -> dict[str, str]:
        """venue_order_id -> strategy (order_attribution table). {} when unwired."""
        real = await self._try("read_attribution_map")
        return real if isinstance(real, dict) else {}

    async def read_prices(self) -> dict[str, dict[str, Any]]:
        """Best bid/ask per exchange — delegate only. {} when unwired, which reads as 'no price
        known' and so makes a price-dependent exit rule DECLINE rather than act on a fabricated
        quote."""
        real = await self._try("read_prices")
        return real if isinstance(real, dict) else {}

    async def open_basket(self, basket_id: str, **kwargs: Any) -> None:
        """Record a filled multi-leg basket. Mirrors StateStore.open_basket's signature so the
        caller does not branch on which state object it holds."""
        self._baskets[str(basket_id)] = {
            "basket_id": str(basket_id),
            "strategy_id": str(kwargs.get("strategy_id", "")),
            "kind": str(kwargs.get("kind", "")),
            "legs": [dict(leg) for leg in (kwargs.get("legs") or [])],
            "entry_cost": float(kwargs.get("entry_cost", 0.0) or 0.0),
            "settlement_value": float(kwargs.get("settlement_value", 0.0) or 0.0),
            "status": "open",
            "opened_at": kwargs.get("opened_at"),
            "closed_at": None,
            "realized_pnl": None,
        }
        await self._try("open_basket", basket_id, **kwargs)

    async def read_open_baskets(self) -> list[dict[str, Any]]:
        real = await self._try("read_open_baskets")
        if isinstance(real, list) and real:
            return real
        return [dict(b) for b in self._baskets.values() if b.get("status") == "open"]

    async def close_basket(self, basket_id: str, *, realized_pnl: float) -> bool:
        """One-way open -> closed, so two exit decisions on the same basket cannot both succeed.
        Returns whether THIS call was the one that closed it."""
        record = self._baskets.get(str(basket_id))
        closed_here = bool(record) and record.get("status") == "open"
        if closed_here:
            record["status"] = "closed"
            record["realized_pnl"] = float(realized_pnl)
        real = await self._try("close_basket", basket_id, realized_pnl=realized_pnl)
        return bool(real) if real is not None else closed_here

    async def mark_to_market(self, **kwargs: Any) -> Any:
        """Delegate-only: marking needs the real `prices` table, which the SIM adapter does not
        keep. None means 'nothing was revalued', never 'everything is worth zero'."""
        return await self._try("mark_to_market", **kwargs)

    async def append_audit(self, entry: dict[str, Any]) -> None:
        self._audit.append(dict(entry))
        await self._try("append_audit", entry)

    async def set_halt(self, reason: str, metadata: dict[str, Any]) -> None:
        self._halt = {"reason": reason, "metadata": metadata}
        self._audit.append({"event": "halt_set", "reason": reason})

    async def clear_halt(self) -> None:
        self._halt = None

    async def get_halt(self) -> dict[str, Any] | None:
        return self._halt

    async def write_heartbeat(self, ts_iso: str) -> None:
        self._heartbeat_ts = ts_iso


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def build_sim_stack(
    *,
    config: dict,
    strategy_contracts: dict[str, str],
    buying_power: float = 100_000.0,
    equity: float = 100_000.0,
    tick_seconds: float = 5.0,
    broker: Any | None = None,
    exit_config: Any | None = None,
    exit_books_provider: Any | None = None,
    best_alternative_edge_provider: Any | None = None,
    max_exits_per_cycle: int = 1,
) -> tuple[Orchestrator, Any, AllocatorAdapter, StateAdapter, OrderManagementSystem]:
    """Instantiate the REAL gate/sizer/allocator + SimulatedBroker + real OMS, adapt them, and
    build the Orchestrator. `config` is the parsed config/sig.yaml. Pass `broker=` to inject a
    SimulatedBroker variant (reject-once, unmapped-position, partial-fill) for SIM scenarios.
    Returns the orchestrator plus the pieces a caller/test needs to drive one cycle (await
    allocator.refresh() before running).

    The `exit_*` arguments enable capital recycling. Omitted, `exit_config=None` disables the exit
    pass, so the SIM stack behaves exactly as it did before exits existed."""
    broker = broker if broker is not None else SimulatedBroker(buying_power=buying_power, equity=equity)

    gate = RiskGate(config=config)
    sizer = Sizer(config)              # SizingConfig.from_cfg indexes cfg["sizing"]/["risk"]/["capital"]
    allocator = Allocator(config)

    risk_adapter = RiskGateAdapter(gate, balance=equity)
    sizing_adapter = SizingAdapter(sizer, balance=equity)
    allocator_adapter = AllocatorAdapter(allocator, capital=equity)
    state = StateAdapter()

    oms = OrderManagementSystem(
        broker=broker, state=state, strategy_contracts=strategy_contracts,
    )

    stale_after = float(config.get("feed", {}).get("price_stale_after_s", 15.0))
    dex = config.get("directional_exit", {}) or {}
    orch = Orchestrator(
        broker=broker, state=state, oms=oms, risk_gate=risk_adapter,
        sizing=sizing_adapter, allocator=allocator_adapter,
        tick_seconds=tick_seconds, environment="sim", price_stale_after_s=stale_after,
        exit_config=exit_config, exit_books_provider=exit_books_provider,
        best_alternative_edge_provider=best_alternative_edge_provider,
        max_exits_per_cycle=max_exits_per_cycle,
        exit_trail_arm_pct=float(dex.get("trail_arm_pct", 0.5)),
        exit_trail_pct=float(dex.get("trail_pct", 0.2)),
        exit_stop_loss_pct=float(dex.get("stop_loss_pct", 0.5)),
        exit_check_interval_s=float(dex.get("exit_check_interval_s", 45.0)),
    )
    return orch, broker, allocator_adapter, state, oms


def build_live_stack(
    *,
    config: dict,
    strategy_contracts: dict[str, str],
    store,
    tick_seconds: float = 5.0,
    equity: float = 100_000.0,
    broker=None,
    tournament_id: str | None = None,
    tournament_slug: str | None = None,
    exit_books_provider: Any | None = None,
    best_alternative_edge_provider: Any | None = None,
) -> tuple[Orchestrator, Any, AllocatorAdapter, StateAdapter, OrderManagementSystem]:
    """Instantiate the REAL gate/sizer/allocator + the LIVE SigBroker + real OMS, adapt them, and
    build the Orchestrator for LIVE trading. Mirrors build_sim_stack but:
      * broker defaults to SigBroker (venue-truth reads come from its SIG read client; see
        sig/broker.py:501-522), NOT SimulatedBroker.
      * state wraps the REAL StateStore (`store`) so StateAdapter.stale_exchanges delegates to the
        store's own freshness view instead of the in-memory SIM set.
      * environment="live".
    `config` is the parsed config/sig.yaml. `equity` seeds the gate/sizer/allocator balances and
    capital. Returns the orchestrator plus the pieces a caller needs to drive one cycle (await
    allocator.refresh() before running). SigBroker is imported lazily so SIM never pays httpx's
    import-time cost."""
    if broker is None:
        from sig.broker import SigBroker
        # tournament_id routes ORDERS to the tournament book; tournament_slug scopes the
        # portfolio READS to the same book (see SigBroker.get_positions).
        broker = SigBroker(tournament_id=tournament_id, tournament_slug=tournament_slug)

    gate = RiskGate(config=config)
    sizer = Sizer(config)              # SizingConfig.from_cfg indexes cfg["sizing"]/["risk"]/["capital"]
    allocator = Allocator(config)

    # The gate's exposure/deploy caps size off NAV (equity = free cash + marked positions), refreshed
    # each cycle from venue truth via broker.last_equity — NOT the frozen cash-only build seed, which
    # halved the real deploy cap. new-order cash affordability stays off buying_power (the ledger).
    risk_adapter = RiskGateAdapter(gate, balance=equity,
                                   nav_source=getattr(broker, "last_equity", None))
    sizing_adapter = SizingAdapter(sizer, balance=equity)
    allocator_adapter = AllocatorAdapter(allocator, capital=equity)
    state = StateAdapter(store=store)   # REAL StateStore → stale_exchanges delegates to it

    oms = OrderManagementSystem(
        broker=broker, state=state, strategy_contracts=strategy_contracts,
        # Mechanical floor trades 90+ contracts under few strategy_ids — adopt venue positions outside
        # strategy_contracts instead of hard-locking startup on the first MM fill (unmapped_broker_contract).
        adopt_unmapped=True,
    )

    stale_after = float(config.get("feed", {}).get("price_stale_after_s", 15.0))
    dex = config.get("directional_exit", {}) or {}

    # MASTER SWITCH for arb-BASKET capital recycling: without an ExitConfig the orchestrator's
    # _run_exits() returns early and NO basket ever recycles (it holds every basket to the Nov-4 wall).
    # Build it from config/sig.yaml's `exit:` section so the pass actually runs live. Fail SAFE: if the
    # section is malformed we log and leave basket recycling disabled rather than refuse to start the bot
    # (the directional/MM single-leg exits above do not depend on this and keep running regardless).
    exit_config = None
    try:
        from strategies.exit.config import ExitConfig
        exit_config = ExitConfig.from_cfg(config)
    except Exception as exc:  # noqa: BLE001 — a bad exit section must not stop the bot from trading
        log.warning("exit_config_disabled", error=type(exc).__name__, msg=str(exc)[:200])
    max_exits = int((config.get("exit", {}) or {}).get("max_exits_per_cycle", 1))

    orch = Orchestrator(
        broker=broker, state=state, oms=oms, risk_gate=risk_adapter,
        sizing=sizing_adapter, allocator=allocator_adapter,
        tick_seconds=tick_seconds, environment="live", price_stale_after_s=stale_after,
        exit_trail_arm_pct=float(dex.get("trail_arm_pct", 0.5)),
        exit_trail_pct=float(dex.get("trail_pct", 0.2)),
        exit_stop_loss_pct=float(dex.get("stop_loss_pct", 0.5)),
        exit_check_interval_s=float(dex.get("exit_check_interval_s", 45.0)),
        mm_requote_threshold=float((config.get("market_making", {}) or {}).get("requote_threshold", 0.05)),
        dead_man_threshold_s=((config.get("loops", {}) or {}).get("dead_man_threshold_s") or None),
        placement_worker=bool((config.get("loops", {}) or {}).get("placement_worker", False)),
        placement_intent_ttl_s=float((config.get("loops", {}) or {}).get("placement_intent_ttl_s", 30.0)),
        placement_batch_max_legs=int((config.get("loops", {}) or {}).get("placement_batch_max_legs", 0)),
        placement_max_per_drain=int((config.get("loops", {}) or {}).get("placement_max_per_drain", 20)),
        capital_edge_reserve=float((config.get("capital", {}) or {}).get("pool_edge_reserve", 0.0)),
        mm_cash_reserve_frac=float((config.get("capital", {}) or {}).get("mm_cash_reserve_frac", 0.0)),
        capital_pool_eligible=tuple((config.get("capital", {}) or {}).get("pool_eligible", []) or []),
        exit_config=exit_config,                                   # ← flips _run_exits ON live (basket recycling)
        exit_books_provider=exit_books_provider,                   # depth per leg; None ⇒ baskets HOLD (book_too_thin)
        best_alternative_edge_provider=best_alternative_edge_provider,
        max_exits_per_cycle=max_exits,
    )
    return orch, broker, allocator_adapter, state, oms
