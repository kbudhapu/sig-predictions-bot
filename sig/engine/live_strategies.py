"""Live mechanical-floor strategy adapters (CORE tier) — the two classes the orchestrator
registers and runs on the fast/mechanical floor: arbitrage capture and market-making.

Both satisfy the Strategy protocol (sig/engine/protocols.py):
    name: str ; tier: str ; cadence_s: float
    evaluate(state) -> list[Intent]
    on_fill(fill) -> None
    realized_performance() -> EdgePerf

They are THIN adapters. The real logic lives in the already-built modules
(strategies/arb_detector.py, strategies/market_making/). This file only:
  * pulls tuning from the parsed config (no invented config keys), and
  * translates each module's native output into the orchestrator's `Intent` shape,
    matching EXACTLY the leg/intent dicts the pipeline consumes
    (orchestrator._process_intent / _process_multi_leg_intent, composition.py adapters,
    sig/oms, sig/broker, strategies/risk_gate/gate.py).

────────────────────────────────────────────────────────────────────────────────────
PRICE-PLUMBING DECISION (market-making)
────────────────────────────────────────────────────────────────────────────────────
The Strategy protocol's evaluate() is SYNC, but StateStore.read_prices() is ASYNC. We must
never block the orchestrator's event loop from inside evaluate(). Resolution (the simplest,
safest of the two options in the brief):

  * evaluate() reads a plain in-memory snapshot dict `self._prices` ONLY — it never touches
    the store and never awaits anything.
  * An external price-pump keeps `self._prices` current. Two equivalent entry points:
      - `set_prices(prices)`   — SYNC; the entrypoint's cycle passes it the latest prices.
      - `refresh_prices()`     — ASYNC convenience; pulls `await store.read_prices()` itself
                                 (use from an async context that already owns the store).
    The orchestrator cycle does NOT call either — wiring the pump is main_sig.py's job.

`self._prices` defaults to {} so an un-pumped strategy simply emits nothing (fail-safe: no
quotes rather than quotes on stale/absent prices).

────────────────────────────────────────────────────────────────────────────────────
GAPS / ASSUMPTIONS (see handback)
────────────────────────────────────────────────────────────────────────────────────
1. MM SIZING / sigma_p: single-leg market_making intents flow through the DIRECTIONAL sizing
   path in orchestrator._process_intent (the `else` branch → SizingAdapter.size). That sizer
   reads metadata["sigma_p"]; with no sigma_p, k_for(None) == k_missing_sigma == 0.0 (config),
   so the quote is zero-sized and DROPPED. The MM strategy's own inventory-managed quantity is
   therefore NOT the quantity that reaches the broker — the sizer RE-SIZES it to
   k(sigma)·M·budget/price. To keep quotes alive we attach metadata["sigma_p"] from the
   EXISTING config key sizing.k_reference_sigma (the reference confidence that yields
   quarter-Kelly; this is exactly the value the SIM seam test uses). The MM-computed quantity
   is preserved in metadata["mm_quote_qty"] as advisory only.

2. INTENT.side MAPPING: the MM contract's order dict carries side="yes" (the outcome) and
   action="buy"|"sell" (the direction). The risk gate adapter uses intent.side AS the buy/sell
   exposure direction, so we set intent.side = order["action"] (buy/sell) and keep the yes/no
   outcome in metadata["option_side"]. This matches the directional/MM intent convention in
   tests/test_sim_seams.py (side="buy"). The LIVE SIG broker separately wants side=yes/no +
   action=buy/sell; the single-leg orchestrator→OMS→broker path does not currently carry both
   through _build_leg — that is a pre-existing integration seam, not fixed here.

3. MM ASK (sell) quotes: the gate treats action="sell" as an EXIT and rejects it without an
   existing holding ("sell_without_holding"). So until inventory exists, only the bid side
   rests. Pre-existing gate semantics; reported, not worked around.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from sig.engine.protocols import EdgePerf, Intent

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _row_age_s(row: Any, now: Any = None) -> float | None:
    """Age in seconds of a price row's `updated_at`. None when unparseable/absent.

    None means UNKNOWN age, and the caller must treat unknown as STALE -- quoting a book whose
    freshness we cannot establish is the same mistake as quoting a known-stale one.
    """
    if not isinstance(row, dict):
        return None
    ts = row.get("updated_at")
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max((now or datetime.now(timezone.utc)) - d, timedelta(0)).total_seconds()


def _fast_cadence(config: dict, section_cadence: Any = None) -> float:
    """cadence_s = explicit section value, else loops.fast_loop_interval_s, else 5.0.
    Every branch is guarded so a missing/garbage value can never raise."""
    for candidate in (section_cadence,
                      (config.get("loops", {}) or {}).get("fast_loop_interval_s")):
        if candidate is None:
            continue
        try:
            val = float(candidate)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 5.0


# ─────────────────────────────────────────────────────────────────────────────
# CLASS 1 — CoreArbStrategy
# ─────────────────────────────────────────────────────────────────────────────
class CoreArbStrategy:
    """CORE-tier arbitrage capture. Wraps strategies/arb_detector.ArbDetector (which READS via
    the SigClient: relationship_constraints() + per-leg orderbook()) and emits ONE atomic
    multi-leg Intent per capturable arb. Multi-leg intents are routed by the orchestrator to the
    all-fill-or-unwind OMS path (no naked leg); leg quantities are capture-fillable, NOT sized by
    Browne.

    Leg dict shape matches the pipeline EXACTLY (tests/test_sim_seams.py _Arb, consumed by
    orchestrator._process_multi_leg_intent → composition.validate_multi_leg → OMS → broker):
        {"exchange_id", "side": "buy"|"sell", "action": "open", "price", "quantity"}
    where leg "side" is the buy/sell DIRECTION (CorrectiveLeg.action); the detector's yes/no
    option side is not part of the leg dict the pipeline reads.
    """

    def __init__(self, *, client: Any, store: Any = None, config: dict,
                 strategy_id: str = "arb", bankroll: float = 0.0) -> None:
        # Imported here so this module imports even in a bare harness without the strategies pkg
        # on the path; the import is cheap and only needed to build the detector.
        from strategies.arb_detector import ArbDetector

        self._client = client
        self._store = store                      # may be unused: ArbDetector reads via `client`
        self._config = config or {}
        self._fills: list[Any] = []

        # --- arb-specific sizing (strategies/arb_sizing) ---
        # Previously each basket was sized purely capture-fillable and then clamped by the
        # DIRECTIONAL 15% per-market cap => ~30% of bankroll per 2-leg arb => ~3 concurrent.
        # An absent or invalid arb_sizing block leaves this None and the old capture-fillable
        # behaviour stands, so a bad config degrades to the previous sizing, never to no trading.
        self._bankroll = float(bankroll or 0.0)
        self._arb_deployed = 0.0
        self._arb_sizing = None
        try:
            from strategies.arb_sizing import load_arb_sizing_config

            self._arb_sizing = load_arb_sizing_config(cfg=self._config)
        except Exception as exc:                 # noqa: BLE001
            log.warning("arb_sizing_config_unavailable", error=str(exc),
                        fallback="capture-fillable quantities (pre-arb_sizing behaviour)")

        arb_cfg = (self._config.get("arb", {}) or {})
        detector_kwargs: dict[str, Any] = {}
        # arb_min_violation → min_edge (absent ⇒ ArbDetector's own default). No config key maps to
        # ArbDetector.min_qty or price_tol, so those stay at the detector defaults (min_qty=1.0).
        min_violation = arb_cfg.get("arb_min_violation")
        if min_violation is not None:
            try:
                detector_kwargs["min_edge"] = float(min_violation)
            except (TypeError, ValueError):
                log.warning("arb_min_violation_unparseable", value=min_violation)
        self._detector = ArbDetector(self._client, **detector_kwargs)

        self.name: str = strategy_id
        self.tier: str = "CORE"
        self.cadence_s: float = _fast_cadence(self._config, arb_cfg.get("cadence_s"))

    def evaluate(self, state: Any) -> list[Intent]:
        """Scan for capturable arbs and emit one atomic multi-leg Intent each. NEVER raises: any
        failure logs and returns [] so one bad scan cannot kill the orchestrator cycle."""
        try:
            arbs = self._detector.scan()          # SYNC (calls sync client methods)
        except Exception as exc:                  # noqa: BLE001 — must not escape evaluate()
            log.warning("arb_scan_failed", strategy=self.name, error=str(exc))
            return []

        # Bankroll + arb already open: the total-budget bound needs both, and the snapshot is the
        # only place the open book is visible (orchestrator._read_state_snapshot).
        self._refresh_capital_view(state)

        intents: list[Intent] = []
        for arb in arbs or []:
            try:
                if not getattr(arb, "all_legs_fillable", False):
                    continue                      # no naked legs — skip partially-fillable arbs
                if float(getattr(arb, "min_fillable_qty", 0.0)) < 1:
                    continue

                floor_q = int(float(arb.min_fillable_qty))
                legs: list[dict[str, Any]] = []
                ok = True
                exit_books: dict[str, Any] = {}
                for leg in arb.legs:
                    qty = max(int(float(leg.fillable_qty)), floor_q)
                    if qty < 1 or not leg.exchange_id:
                        ok = False
                        break
                    legs.append({
                        "exchange_id": str(leg.exchange_id),
                        "side": str(leg.action),      # "buy"|"sell" DIRECTION (gate reads this; → SIG action)
                        "outcome": str(leg.side),     # "yes"|"no" OUTCOME (→ SIG OrderInput.side)
                        "action": "open",             # position action (open/close)
                        "price": leg.price,
                        "quantity": qty,
                    })
                    # The detector already fetched the whole book; its BID side is what an exit
                    # would sell into, so the liquidity ceiling costs no extra API call.
                    if getattr(leg, "book", None):
                        exit_books[str(leg.exchange_id)] = leg.book
                if not ok or not legs:
                    continue

                legs = self._size_basket(arb, legs, exit_books)
                if not legs:
                    continue

                intents.append(Intent(
                    strategy_id=self.name,
                    exchange_id=str(getattr(arb, "relationship", self.name)),
                    side="buy",
                    action="open",
                    strategy_type="arb",
                    price=legs[0]["price"],
                    metadata={},
                    legs=legs,
                ))
            except Exception as exc:              # noqa: BLE001 — one bad arb must not drop the rest
                log.warning("arb_intent_build_failed", strategy=self.name, error=str(exc))
                continue
        return intents

    # ── arb-specific sizing helpers ──
    def set_bankroll(self, bankroll: float) -> None:
        try:
            self._bankroll = max(float(bankroll), 0.0)
        except (TypeError, ValueError):
            pass

    def _refresh_capital_view(self, state: Any) -> None:
        """Pull bankroll and already-open arb notional out of the cycle snapshot.

        `arb_deployed` is what turns `arb_total_max_frac` into a real budget rather than a
        per-order limit. The snapshot's positions are the COMBINED book (arb and directional share
        one exposure view by design, spec §5), so this is a conservative over-estimate of what arb
        itself holds: counting a directional position against the arb budget can only make arbs
        smaller, never larger, which is the safe direction for a cap.
        """
        if not isinstance(state, dict):
            return
        positions = state.get("positions") or {}
        if isinstance(positions, dict):
            total = 0.0
            for row in positions.values():
                if not isinstance(row, dict):
                    continue
                try:
                    total += abs(float(row.get("quantity", 0) or 0)) * float(
                        row.get("avg_price", 0) or 0)
                except (TypeError, ValueError):
                    continue
            self._arb_deployed = total
        exposure = state.get("exposure")
        balance = getattr(exposure, "balance", None)
        if balance is None and isinstance(exposure, dict):
            balance = exposure.get("balance")
        if balance is not None:
            self.set_bankroll(balance)

    def _size_basket(self, arb: Any, legs: list[dict[str, Any]],
                     exit_books: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply the arb sizing rule. Returns the legs at their final quantity, or [] to skip.

        Falls through to the unchanged capture-fillable legs when arb sizing is unconfigured or no
        bankroll is known — the sizing rule is a REFINEMENT of the existing behaviour, so an
        unconfigured deployment keeps trading exactly as it did.
        """
        if self._arb_sizing is None or self._bankroll <= 0.0:
            return legs
        try:
            from strategies.arb_sizing import size_arb_basket

            sized = size_arb_basket(
                legs,
                edge_per_contract=float(getattr(arb, "violation_amount", 0.0) or 0.0),
                bankroll=self._bankroll,
                arb_deployed=self._arb_deployed,
                exit_books=exit_books or None,
                cfg=self._arb_sizing,
            )
        except Exception as exc:                 # noqa: BLE001 — sizing must not kill the scan
            log.warning("arb_sizing_failed", strategy=self.name, error=str(exc))
            return legs
        if not sized.sized:
            log.info("arb_skipped_by_sizing", strategy=self.name, reason=sized.reason,
                     relationship=str(getattr(arb, "relationship", "")))
            return []
        # Reserve the sized notional against this cycle's budget so a SECOND arb found in the same
        # scan sees the capital the first one just committed. Without this, N arbs in one cycle each
        # size against the same untouched budget and together breach arb_total_max_frac.
        self._arb_deployed += sized.basket_notional
        log.info("arb_sized", strategy=self.name, quantity=sized.quantity,
                 notional=round(sized.basket_notional, 2), binding=sized.binding)
        return sized.legs

    def on_fill(self, fill: Any) -> None:
        self._fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()


# ─────────────────────────────────────────────────────────────────────────────
# CLASS 2 — CoreMarketMakingStrategy
# ─────────────────────────────────────────────────────────────────────────────
class CoreMarketMakingStrategy:
    """CORE-tier market-making. Wraps strategies/market_making.MarketMaker (pure quote logic) and
    emits SINGLE-leg Intents (no legs=) for each postable resting order.

    PRICE PLUMBING: evaluate() reads ONLY the in-memory snapshot `self._prices` (never awaits,
    never blocks the loop). Keep it current with set_prices(prices) [sync, the primary path the
    entrypoint cycle uses] or refresh_prices() [async convenience that pulls from the store].
    Defaults to {} ⇒ emits nothing until pumped.

    See module docstring GAPS: MM quantity is re-sized by the orchestrator's directional sizer
    (needs metadata["sigma_p"] or it zero-sizes); we attach sigma_p from config
    sizing.k_reference_sigma so quotes survive.
    """

    def __init__(self, *, store: Any, config: dict, exchange_ids: list[str],
                 strategy_id: str = "market_making", equity: float | None = None) -> None:
        from strategies.market_making import MarketMaker, MarketMakerConfig

        # REAL BANKROLL — required for capital-% quote sizing. Without it the MM declines to quote
        # (there is no fallback share count any more), so this MUST actually be wired. This is the
        # same gap the arb sizer had: a sizer that silently never receives equity sizes nothing.
        self._equity: float | None = (float(equity) if equity else None)
        self._store = store
        self._config = config or {}
        # MARKET SET: default to the full discovered universe, but allow a curated `market_making.markets`
        # override (like order_flow.markets). When MM is a SMALL capital sleeve, quoting all 237 markets
        # is pure cycle/venue-budget overhead — each quote is a venue-serial ~5-9s write — and it STARVES
        # the higher-priority directional edges of their turn in the cycle. A curated liquid subset keeps
        # MM's spread capture where it is real while leaving the cycle+budget for cross-market.
        _mm_markets = ((config or {}).get("market_making", {}) or {}).get("markets")
        self._exchange_ids: list[str] = ([str(x) for x in _mm_markets] if _mm_markets
                                         else [str(x) for x in (exchange_ids or [])])
        # Quote size is CONFIG-DRIVEN (market_making.quote_size); other params keep their defaults.
        # The size flows: QuoterConfig.size → quote.bid_size/ask_size → to_contract order["quantity"]
        # → evaluate()'s meta["mm_quote_qty"] → orchestrator's MM branch (quote qty clamped to budget).
        self._mm = MarketMaker(MarketMakerConfig.from_config(self._config))
        # CROSS-MARKET OVERLAP. MM sizes inventory off the WHOLE venue position, so on a market where
        # cross-market holds a big convergence bet (≈12% NAV, far over MM's 5%-of-equity inventory cap) MM
        # read cross's position as its OWN over-cap inventory: pulled bids, skewed quotes −2¢ and posted
        # enlarged asks — i.e. SOLD DOWN cross's bet (mirror for shorts). Modes (market_making.cross_overlap):
        #   dynamic (default) — quote a cross race normally UNLESS the venue position there exceeds MM's own
        #                       inventory cap (the only zone where MM's inventory logic misreads cross's bet).
        #   exclude           — never quote cross races.     allow — old behaviour (no protection).
        self._cross_owned: set[str] = set()
        self._conflicted: set[str] = set()        # cross races skipped THIS cycle (orchestrator cancels MM here)
        _ov = str(((self._config.get("market_making", {}) or {}).get("cross_overlap", "dynamic"))).lower()
        self._cross_overlap = _ov if _ov in ("dynamic", "exclude", "allow") else "dynamic"
        self._inv_frac = float(self._mm._cfg.sizing.mm_max_inventory_frac)
        # SHARP-ANCHORED QUOTING (market_making.sharp_anchor = off | shadow | live). On cross races we know a
        # real-money fair value (Polymarket, polarity-aligned). MM quoting blind around SIG's mid fills BOTH
        # ways at random; anchoring makes its fills land on the side the sharp favours:
        #   • fair-value GUARD — never bid above (sharp − margin), never offer below (sharp + margin): those
        #     are fills that are wrong vs fair by construction (being picked off by the stale-quote side).
        #   • size LEAN — bid bigger / ask smaller when SIG is below sharp (and vice versa), so inventory that
        #     accumulates sits on the convergence side. Bounded by MM's inventory cap upstream; no new risk path.
        # shadow = compute + record (data/mm_shadow/sharp_anchor.jsonl), change nothing. live = apply.
        mm_cfg_raw = (self._config.get("market_making", {}) or {})
        _sa = str(mm_cfg_raw.get("sharp_anchor", "off")).lower()
        self._sharp_mode = _sa if _sa in ("off", "shadow", "live") else "off"
        self._sharp_margin = float(mm_cfg_raw.get("sharp_anchor_margin", 0.005))
        self._sharp_lean = float(mm_cfg_raw.get("sharp_lean_strength", 0.5))
        self._sharp_lean_gap = max(float(mm_cfg_raw.get("sharp_lean_gap", 0.05)), 1e-6)
        self._sharp_path = str(mm_cfg_raw.get("sharp_anchor_record_path", "data/mm_shadow/sharp_anchor.jsonl"))
        self._sharp_fn = None
        self._prices: dict[str, dict] = {}
        self._fills: list[Any] = []

        # sigma_p to attach so the directional sizer does not zero-size MM quotes. Sourced from the
        # EXISTING sizing.k_reference_sigma key (reference confidence ⇒ quarter-Kelly). None ⇒ omit
        # (quotes then get dropped by the sizer — fail-safe, and surfaced in the log).
        sizing_cfg = (self._config.get("sizing", {}) or {})
        ref_sigma = sizing_cfg.get("k_reference_sigma")
        try:
            self._mm_sigma_p: float | None = None if ref_sigma is None else float(ref_sigma)
        except (TypeError, ValueError):
            self._mm_sigma_p = None
        if self._mm_sigma_p is None:
            log.warning("mm_no_sigma_p_quotes_will_zero_size", strategy=strategy_id)

        mm_cfg = (self._config.get("market_making", {}) or {})
        # Freshness cutoff for quoting. Defaults to the feed's own price_stale_after_s so the MM and
        # the orchestrator's stale-market gate agree on what "stale" means.
        feed_cfg = (self._config.get("feed", {}) or {})
        try:
            self._max_quote_age_s = float(
                mm_cfg.get("max_quote_age_s", feed_cfg.get("price_stale_after_s", 15.0)))
        except (TypeError, ValueError):
            self._max_quote_age_s = 15.0

        self.name: str = strategy_id
        self.tier: str = "CORE"
        # MM cadence is its own knob: a full re-quote sweep is the most expensive thing the bot
        # does on a venue that serialises at ~4.5-8.7s/order, so it must not be pinned to the fast
        # arb cadence. Falls back to loops.fast_loop_interval_s when unset.
        self.cadence_s: float = _fast_cadence(self._config, mm_cfg.get("cadence_s"))

    # ── price pump (caller-driven; NOT called by the orchestrator cycle) ──
    def set_equity(self, equity: float | None) -> None:
        """Refresh the bankroll (venue equity). The MM base size and the per-market inventory cap
        are both fractions of this, so a stale equity under-sizes or over-sizes every quote."""
        try:
            self._equity = float(equity) if equity else None
        except (TypeError, ValueError):
            pass

    def set_cross_owned(self, exchange_ids) -> None:
        """Cross-market's active races (see the cross_overlap modes in __init__). Replaces the set."""
        self._cross_owned = {str(x) for x in (exchange_ids or [])}

    def set_sharp_provider(self, fn) -> None:
        """fn(exchange_id) -> SIG-aligned sharp YES price or None (cross provider's aligned_benchmark)."""
        self._sharp_fn = fn

    def _sharp_anchor(self, eid: str, orders: list[dict], mid: float | None, held: int,
                      ts: str, rec: list, stats: dict) -> list[dict]:
        """Apply (live) or evaluate-only (shadow) the fair-value guard + size lean. Returns the orders to
        place. NEVER raises — on any problem the orders pass through unchanged."""
        if self._sharp_mode == "off" or self._sharp_fn is None or eid not in self._cross_owned or mid is None:
            return orders
        try:
            sharp = self._sharp_fn(eid)
        except Exception:  # noqa: BLE001
            sharp = None
        if sharp is None or not (0.0 < float(sharp) < 1.0):
            return orders
        sharp = float(sharp)
        stats["with_sharp"] += 1
        g = max(-1.0, min(1.0, (sharp - mid) / self._sharp_lean_gap))   # >0: SIG cheap vs sharp → favour bids
        out: list[dict] = []
        for o in orders:
            act = str(o.get("action", "")).lower()
            px = float(o.get("price") or 0.0)
            qty = int(o.get("quantity") or 0)
            drop = ((act == "buy" and px > sharp - self._sharp_margin)
                    or (act == "sell" and px < sharp + self._sharp_margin))
            mult = (1.0 + self._sharp_lean * g) if act == "buy" else (1.0 - self._sharp_lean * g)
            new_qty = max(int(qty * max(mult, 0.0)), 0)
            if act == "sell":
                new_qty = min(new_qty, max(held, 0))
            if drop:
                stats["drop_" + act] += 1
                rec.append({"ts": ts, "exchange_id": eid, "action": act, "price": px, "qty": qty,
                            "mid": round(mid, 5), "sharp": round(sharp, 5), "gap": round(sharp - mid, 5),
                            "mode": self._sharp_mode})
            stats["lean_sum"] += g
            if self._sharp_mode != "live":
                out.append(o)
                continue
            if drop or new_qty < 1:
                continue
            o2 = dict(o)
            o2["quantity"] = new_qty
            out.append(o2)
        return out

    def _record_sharp(self, rec: list) -> None:
        if not rec:
            return
        try:
            import json as _json, os as _os
            _os.makedirs(_os.path.dirname(self._sharp_path) or ".", exist_ok=True)
            with open(self._sharp_path, "a", encoding="utf-8") as fh:
                for r in rec:
                    fh.write(_json.dumps(r) + "\n")
        except Exception:  # noqa: BLE001 — recording must never break quoting
            pass

    def conflicted_exchanges(self) -> set[str]:
        """Cross races MM skipped on its last evaluate because cross's position there exceeds MM's
        inventory cap. The orchestrator cancels MM's resting orders on these so stale asks/bids stop
        trading against cross's bet."""
        return set(self._conflicted)

    def _cross_conflict(self, eid: str, positions: dict, row: dict | None) -> bool:
        """True ⇒ do NOT quote this cross-owned market this cycle."""
        if self._cross_overlap == "allow":
            return False
        if self._cross_overlap == "exclude":
            return True
        pos = positions.get(eid) or positions.get(eid.upper()) or {}
        qty = _held_yes(pos)
        if qty == 0:
            return False
        if not self._equity:
            return True                               # no capital basis to judge the cap → stay out
        bb = _as_float((row or {}).get("best_bid"))
        ba = _as_float((row or {}).get("best_ask"))
        if bb is None or ba is None:
            return True
        mid = (bb + ba) / 2.0
        held_px = mid if qty > 0 else (1.0 - mid)
        return abs(qty) * held_px > self._inv_frac * float(self._equity)

    def set_prices(self, prices: dict) -> None:
        """Sync snapshot swap. `prices` is StateStore.read_prices() shape:
        {exchange_id: {best_bid, best_ask, spread, sequence, updated_at, ...}}."""
        self._prices = dict(prices or {})

    async def refresh_prices(self) -> None:
        """Async convenience: pull the latest snapshot from the store into `self._prices`."""
        if self._store is None:
            return
        try:
            self._prices = await self._store.read_prices()
        except Exception as exc:                  # noqa: BLE001 — never propagate into the loop
            log.warning("mm_refresh_prices_failed", strategy=self.name, error=str(exc))

    def evaluate(self, state: Any) -> list[Intent]:
        """Build resting-quote Intents for each configured market with a valid two-sided touch.
        NEVER raises: any failure logs and returns []."""
        try:
            # Venue-truth holdings for this cycle (reconciled positions). The MM bounds its SELL
            # (ask) side to what we actually HOLD so the ask only ever REDUCES a long position and
            # the gate accepts it as a reduction — never a naked short. This is the SAME source the
            # gate clamps sells against (gate.py `positions[exch]["quantity"]`), so the MM's emitted
            # ask and the gate's clamp agree, and it survives restarts/partials (an internal fill
            # counter would drift). Absent ⇒ treat as flat ⇒ no ask posted until bids fill.
            positions = state.get("positions") if isinstance(state, dict) else None
            positions = positions or {}

            # CAPITAL CONTEXT for capital-% sizing. The MM sleeve (mm_capital_frac x equity) is
            # split across the markets we can actually quote THIS cycle, not the configured list —
            # spreading capital across markets with no usable touch would under-size the rest.
            # *** QUOTE ONLY FRESH MARKETS. *** This is the single biggest order-volume lever, and
            # it is not a tradeoff: resting a quote on a book whose last price is minutes old is bad
            # market-making, not merely expensive. Measured live, only ~58 of 237 markets stayed
            # fresh inside the cutoff while the MM quoted ~250 of them -- so most quotes were
            # priced off dead data AND each cost ~4.5-8.7s of irreducible venue time (proven by
            # probe: batching and concurrency BOTH serialise, so the only way to spend less venue
            # time is to send fewer orders).
            #
            # Unknown age counts as stale: if we cannot establish freshness we do not quote.
            now = datetime.now(timezone.utc)
            quotable, stale_skipped, no_touch, excluded = [], 0, 0, 0
            conflicted: set[str] = set()
            for eid in self._exchange_ids:
                if eid in self._cross_owned and self._cross_conflict(eid, positions, self._prices.get(eid)):
                    conflicted.add(eid)
                    excluded += 1
                    continue
                row = self._prices.get(eid)
                if not row:
                    no_touch += 1
                    continue
                if (_as_float(row.get("best_bid")) is None
                        or _as_float(row.get("best_ask")) is None):
                    no_touch += 1
                    continue
                age = _row_age_s(row, now)
                if age is None or age > self._max_quote_age_s:
                    stale_skipped += 1
                    continue
                quotable.append(eid)
            # Report the age range the MM ACTUALLY sees. If this is far above
            # feed.rest_safety_net_interval_s then the lag is ours (pump starvation), not the venue's,
            # and the fix belongs in the pump — not in a wider cutoff.
            _ages = [a for a in (_row_age_s(self._prices.get(e), now) for e in self._exchange_ids)
                     if a is not None]
            log.info("mm_market_selection", configured=len(self._exchange_ids),
                     age_min_s=round(min(_ages), 1) if _ages else None,
                     age_max_s=round(max(_ages), 1) if _ages else None,
                     quotable=len(quotable), stale_skipped=stale_skipped, no_touch=no_touch,
                     excluded=excluded, cross_overlap=self._cross_overlap,
                     max_age_s=self._max_quote_age_s)
            self._conflicted = conflicted
            if self._equity is None:
                log.warning("mm_no_equity_declining_to_quote", strategy=self.name,
                            note="capital-% sizing needs the bankroll; there is no fallback size")
                return []
            self._mm.set_capital(equity=self._equity, active_markets=max(len(quotable), 1))

            intents: list[Intent] = []
            sa_rec: list[dict] = []
            sa_stats = {"with_sharp": 0, "drop_buy": 0, "drop_sell": 0, "lean_sum": 0.0}
            sa_ts = now.isoformat()
            for exchange_id in quotable:
                row = self._prices.get(exchange_id)
                if not row:
                    continue
                best_bid = _as_float(row.get("best_bid"))
                best_ask = _as_float(row.get("best_ask"))
                if best_bid is None or best_ask is None:
                    continue

                self._mm.set_position(exchange_id, _held_yes(positions.get(exchange_id)))
                # Depth is NOT in the prices table (best_bid/best_ask/spread/sequence only), so it
                # is unknown here and the liquidity bound is SKIPPED rather than replaced by a
                # constant. Wiring real depth would tighten sizing on thin books; a constant would
                # instead become a fixed maximum binding on every quote.
                depth = row.get("depth")
                try:
                    depth = int(depth) if depth is not None else None
                except (TypeError, ValueError):
                    depth = None
                result = self._mm.build(exchange_id, best_bid, best_ask, book_depth=depth)
                orders = self._sharp_anchor(exchange_id, list(result.get("orders", []) or []),
                                            (best_bid + best_ask) / 2.0,
                                            _held_yes(positions.get(exchange_id)), sa_ts, sa_rec, sa_stats)
                for order in orders:
                    try:
                        qty = int(order["quantity"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    if qty < 1:
                        continue
                    meta: dict[str, Any] = {
                        "option_side": order.get("side"),   # yes/no outcome (direction is in intent.side)
                        "mm_quote_qty": qty,                # advisory — sizer re-sizes (see GAPS)
                    }
                    if self._mm_sigma_p is not None:
                        meta["sigma_p"] = self._mm_sigma_p
                    intents.append(Intent(
                        strategy_id=self.name,
                        exchange_id=order["exchange_id"],
                        side=order["action"],               # buy/sell direction (gate+broker read this)
                        action="open",
                        strategy_type="market_making",
                        price=order["price"],
                        metadata=meta,
                    ))
            if self._sharp_mode != "off" and sa_stats["with_sharp"]:
                self._record_sharp(sa_rec)
                log.info("mm_sharp_anchor", mode=self._sharp_mode, markets_with_sharp=sa_stats["with_sharp"],
                         drop_bids=sa_stats["drop_buy"], drop_asks=sa_stats["drop_sell"],
                         applied=(self._sharp_mode == "live"))
            return intents
        except Exception as exc:                  # noqa: BLE001 — must not escape evaluate()
            log.warning("mm_evaluate_failed", strategy=self.name, error=str(exc))
            return []

    def on_fill(self, fill: Any) -> None:
        self._fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()

    def snapshot(self) -> dict | None:
        """Persist the MM's rolling mid buffers (mr-skew 60-min + adverse monitor) across restarts."""
        mm = getattr(self, "_mm", None)
        if mm is None:
            return None
        out: dict = {}
        if hasattr(getattr(mm, "mr", None), "snapshot"):
            out["mr"] = mm.mr.snapshot()
        if hasattr(getattr(mm, "adverse", None), "snapshot"):
            out["adverse"] = mm.adverse.snapshot()
        return out or None

    def restore(self, blob: dict) -> None:
        mm = getattr(self, "_mm", None)
        blob = blob or {}
        if mm is None:
            return
        if blob.get("mr") and hasattr(getattr(mm, "mr", None), "restore"):
            mm.mr.restore(blob["mr"])
        if blob.get("adverse") and hasattr(getattr(mm, "adverse", None), "restore"):
            mm.adverse.restore(blob["adverse"])


# ─────────────────────────────────────────────────────────────────────────────
# CLASS 3 — CoreOrderFlowStrategy
# ─────────────────────────────────────────────────────────────────────────────
class CoreOrderFlowStrategy:
    """SIGNAL-tier order-flow fade (wraps strategies/order_flow). Reads ORDER-BOOK DEPTH IMBALANCE as the
    live flow proxy — SIG exposes no market-wide trade tape (SigClient has `orderbook` + our own
    `portfolio_fills` only), so resting bid/ask depth is the available pressure signal — classifies it
    with the module's fade-dumb / don't-fade-informed safety, and emits a DIRECTIONAL fade intent only
    when the flow is a one-sided EXTREME that OPPOSES the informed view (DUMB). INFORMED flow (agrees
    with the view) and UNCONFIRMED flow (no view) are NEVER faded — the safety core, kept intact.

    INFORMED REFERENCE: the module needs an informed direction to tell DUMB (fade) from INFORMED (hold).
    Until the cross-market divergence / fair-value signals are wired live, the reference is the
    short-window realized MID MOMENTUM — a confirmed realized move is the student market's own
    information: flow OPPOSING a real move is late/panic → fade; flow WITH it is informed → stand aside.
    A real-money reference, when supplied later, supersedes momentum (the module ranks realmoney_direction
    over fair_divergence). No recent move and no reference ⇒ UNCONFIRMED ⇒ no trade.

    MARKET-STRUCTURE NOTE: a fade of up-pressure is a SELL of YES; with no YES held the risk gate rejects
    it as a naked short (`sell_without_holding`) — correct, SIG YES cannot be shorted. So fade-BUYs (fade
    a down-push) open longs and trade freely; fade-SELLs only ever REDUCE a held long. Expected, not a bug.

    PLUMBING mirrors market-making: evaluate() reads in-memory snapshots only (never awaits) — `set_books`
    (per-exchange orderbook {bids,asks,bestBid,bestAsk}) and `set_prices`, pumped by main_sig. Un-pumped ⇒
    emits nothing. Disabled via config `order_flow.enabled` ⇒ no-op.

    SIZING: single-leg directional intents route through the orchestrator's DIRECTIONAL Browne sizer
    (strategy_type="order_flow" → the `else` sizing branch), which reads metadata["sigma_p"]; we attach it
    from `order_flow.k_reference_sigma` (the conviction / aggressiveness knob — smaller ⇒ larger Kelly),
    exactly as market-making does. Capital share is the allocator's `order_flow` prior.

    EXITS: opens single-leg directional YES positions → closed by the orchestrator's _run_takeprofit_exits
    (trailing-stop / stop-loss, buy-to-cancel), which covers every held position NOT inside an arb basket —
    order_flow positions included (confirmed: that path excludes only basket legs).
    """

    def __init__(self, *, store: Any, config: dict, exchange_ids: list[str],
                 strategy_id: str = "order_flow") -> None:
        from strategies.order_flow import OrderFlowEngine, FlowConfig, SignalConfig, ReferenceView, Trade

        of = (config or {}).get("order_flow", {}) or {}
        self._store = store
        self._enabled = bool(of.get("enabled", False))
        # Prefer the LIQUIDITY-picked market list (order_flow.markets) over the full discovered universe
        # — depth-imbalance on a thin market is noise, so we scan only the chosen liquid few.
        self._exchange_ids = [str(x) for x in (of.get("markets") or exchange_ids or [])]
        self._max_markets = int(of.get("max_markets", 20))

        self._engine = OrderFlowEngine(
            FlowConfig(
                window_seconds=float(of.get("window_seconds", 60.0)),
                extreme_threshold=float(of.get("extreme_threshold", 0.6)),
                min_volume=float(of.get("min_volume", 200.0)),
                min_trades=int(of.get("min_trades", 3)),
            ),
            SignalConfig(
                pull_on_unconfirmed=bool(of.get("pull_on_unconfirmed", True)),
                emit_follow=bool(of.get("emit_follow", False)),
            ),
        )
        self._ReferenceView = ReferenceView
        self._Trade = Trade

        self._books: dict[str, dict] = {}
        self._prices: dict[str, dict] = {}
        self._mid_hist: dict[str, deque] = {}
        self._momentum_window_s = float(of.get("momentum_window_s", 45.0))
        self._momentum_min_move = float(of.get("momentum_min_move", 0.01))
        self._min_confidence = float(of.get("min_confidence", 0.0))

        # conviction → sigma_p for the directional sizer (smaller ⇒ bigger Kelly). Prefer the order_flow
        # knob; fall back to the shared sizing.k_reference_sigma (what MM uses). None ⇒ sizer zero-sizes.
        ref_sigma = of.get("k_reference_sigma")
        if ref_sigma is None:
            ref_sigma = (config.get("sizing", {}) or {}).get("k_reference_sigma")
        try:
            self._sigma_p = None if ref_sigma is None else float(ref_sigma)
        except (TypeError, ValueError):
            self._sigma_p = None
        if self._enabled and self._sigma_p is None:
            log.warning("order_flow_no_sigma_p_intents_will_zero_size", strategy=strategy_id)

        self.name = strategy_id
        self.tier = "SIGNAL"
        self.cadence_s = _fast_cadence(config, of.get("cadence_s"))
        self._fills: list[Any] = []

    # ── snapshot pumps (caller-driven; evaluate never awaits) ──
    def set_books(self, books: dict) -> None:
        self._books = dict(books or {})

    def set_prices(self, prices: dict) -> None:
        self._prices = dict(prices or {})

    def _reference(self, exchange_id: str, mid: float | None, now: float):
        """Informed reference = sign of the realized mid move over momentum_window_s. Empty view (⇒
        UNCONFIRMED ⇒ no fade) when there is too little history or the move is below the floor."""
        if mid is None:
            return self._ReferenceView()
        dq = self._mid_hist.setdefault(exchange_id, deque())
        dq.append((now, float(mid)))
        cutoff = now - self._momentum_window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        if len(dq) < 2:
            return self._ReferenceView()
        move = dq[-1][1] - dq[0][1]
        if abs(move) < self._momentum_min_move:
            return self._ReferenceView()
        # realized move as the fair-value-divergence proxy (+move ⇒ price rising ⇒ informed dir +1)
        return self._ReferenceView(fair_divergence=move)

    def evaluate(self, state: Any) -> list[Intent]:
        """Build directional fade intents from order-book imbalance. NEVER raises."""
        if not self._enabled:
            return []
        try:
            now = time.monotonic()
            intents: list[Intent] = []
            for exchange_id in self._exchange_ids[: self._max_markets]:
                book = self._books.get(exchange_id)
                if not isinstance(book, dict):
                    continue
                bid_depth, ask_depth = _book_depth(book)
                if bid_depth <= 0.0 and ask_depth <= 0.0:
                    continue
                # Ingest resting depth as flow pressure: bids = latent buying (price-up), asks = latent
                # selling (price-down). The engine windows the samples and flags the one-sided extreme.
                if bid_depth > 0.0:
                    self._engine.ingest(self._Trade(exchange_id=exchange_id, action="buy",
                                                    quantity=bid_depth, side="yes"), now)
                if ask_depth > 0.0:
                    self._engine.ingest(self._Trade(exchange_id=exchange_id, action="sell",
                                                    quantity=ask_depth, side="yes"), now)
                mid = _book_mid(book, self._prices.get(exchange_id))
                view = self._reference(exchange_id, mid, now)
                sig = self._engine.signal(exchange_id, view=view, market_id=exchange_id, now=now)
                if sig is None:
                    continue
                emit = bool(sig.fade and sig.action) and sig.confidence >= self._min_confidence
                log.info("order_flow_signal", strategy=self.name, exchange=exchange_id,
                         classification=sig.classification, imbalance=round(sig.flow_imbalance, 4),
                         direction=sig.direction, fade=sig.fade, action=sig.action,
                         strength=round(sig.strength, 4), confidence=round(sig.confidence, 4),
                         informed=sig.informed, flags=sig.flags, emit=emit)
                if not emit:
                    continue
                price = _book_touch_price(book, sig.action, mid)
                meta: dict[str, Any] = {
                    "option_side": "yes",                 # single YES exchange per market (recon §2)
                    "of_classification": sig.classification,
                    "of_imbalance": sig.flow_imbalance,
                    "of_confidence": sig.confidence,
                }
                if self._sigma_p is not None:
                    meta["sigma_p"] = self._sigma_p
                intents.append(Intent(
                    strategy_id=self.name,
                    exchange_id=exchange_id,
                    side=sig.action,                      # "buy"|"sell" direction (gate+broker read this)
                    action="open",
                    strategy_type="order_flow",
                    signal_strength=max(float(sig.confidence), 0.0) or 1.0,
                    price=price,
                    metadata=meta,
                ))
            return intents
        except Exception as exc:                          # noqa: BLE001 — must not escape evaluate()
            log.warning("order_flow_evaluate_failed", strategy=self.name, error=str(exc))
            return []

    def snapshot(self) -> dict | None:
        """Persist the momentum-reference mid buffer across restarts (its informed reference needs history)."""
        from infra.state.rolling_buffer import dump
        return {"mid_hist": dump(self._mid_hist, time.monotonic())}

    def restore(self, blob: dict) -> None:
        from infra.state.rolling_buffer import load
        mh = (blob or {}).get("mid_hist")
        if mh:
            self._mid_hist = load(mh, time.monotonic(), self._momentum_window_s)

    def on_fill(self, fill: Any) -> None:
        self._fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()


def _book_depth(book: dict) -> tuple[float, float]:
    """(total bid qty, total ask qty) from an orderbook {bids:[{price,quantity}|[p,q]], asks:[...]}.
    Tolerant of dict levels and [price, qty] pairs; non-numeric levels are skipped."""
    def _sum(levels) -> float:
        tot = 0.0
        for lvl in levels or []:
            if isinstance(lvl, dict):
                q = lvl.get("quantity", lvl.get("size"))
            elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                q = lvl[1]
            else:
                continue
            try:
                q = float(q)
            except (TypeError, ValueError):
                continue
            if q > 0:
                tot += q
        return tot
    return _sum(book.get("bids")), _sum(book.get("asks"))


def _book_mid(book: dict, price_row: Any) -> float | None:
    bb = _as_float(book.get("bestBid"))
    ba = _as_float(book.get("bestAsk"))
    if bb is None and isinstance(price_row, dict):
        bb = _as_float(price_row.get("best_bid"))
    if ba is None and isinstance(price_row, dict):
        ba = _as_float(price_row.get("best_ask"))
    if bb is not None and ba is not None and ba >= bb:
        return (bb + ba) / 2.0
    return bb if bb is not None else ba


def _book_touch_price(book: dict, action: str, mid: float | None) -> float | None:
    """Marketable limit: a fade BUY lifts the ask, a fade SELL hits the bid (decisive fill in a thin
    book). Falls back to the mid, then None."""
    bb = _as_float(book.get("bestBid"))
    ba = _as_float(book.get("bestAsk"))
    if str(action).lower() == "buy":
        return ba if ba is not None else mid
    return bb if bb is not None else mid


def _as_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _held_yes(pos_row: Any) -> int:
    """Net held YES contracts for one market from a reconciled positions row
    ({quantity, avg_price, ...} shape — StateStore.read_positions / gate convention). Anything
    unreadable or absent reads as FLAT (0): no visibility ⇒ hold no ask, never a naked short."""
    if not isinstance(pos_row, dict):
        return 0
    try:
        return int(float(pos_row.get("quantity") or 0))
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────────────────────────────
# CLASS 4 — CoreMeanReversionTakerStrategy (SIGNAL tier) — operator's taker MR bet
# ─────────────────────────────────────────────────────────────────────────────────────
class CoreMeanReversionTakerStrategy:
    """Taker mean-reversion on tight-spread liquid markets (W≈60, k≈2σ). SAFE-BY-CONSTRUCTION:
    enter only when FLAT, exits are reduce-only SELLs (gate clamps to holdings → cannot double or go
    naked). Position is tracked from OUR OWN fills, so MM inventory on the same market never confuses
    it. Marketable LIMIT prices only (cross by resting at the touch) — never market orders. Disabled
    unless mr_taker.enabled."""

    def __init__(self, *, store: Any, config: dict, exchange_ids: list[str],
                 strategy_id: str = "mean_reversion") -> None:
        from strategies.mr_taker import MRTakerEngine, MRTakerConfig

        mt = (config or {}).get("mr_taker", {}) or {}
        self._store = store
        self._enabled = bool(mt.get("enabled", False))
        # tight-spread liquid set; default to the order_flow liquidity-picked markets
        of = (config or {}).get("order_flow", {}) or {}
        self._exchange_ids = [str(x) for x in (mt.get("markets") or of.get("markets") or exchange_ids or [])]
        self._engine = MRTakerEngine(MRTakerConfig(
            enabled=self._enabled,
            window_seconds=float(mt.get("window_seconds", 3600.0)),
            min_samples=int(mt.get("min_samples", 20)),
            k_enter=float(mt.get("k_enter", 2.0)),
            k_exit=float(mt.get("k_exit", 0.5)),
            hold_max_s=float(mt.get("hold_max_s", 3600.0)),
            max_spread=float(mt.get("max_spread", 0.012)),
        ))
        ref_sigma = mt.get("k_reference_sigma")
        if ref_sigma is None:
            ref_sigma = (config.get("sizing", {}) or {}).get("k_reference_sigma")
        try:
            self._sigma_p = None if ref_sigma is None else float(ref_sigma)
        except (TypeError, ValueError):
            self._sigma_p = None

        self._prices: dict[str, dict] = {}
        self._pos: dict[str, int] = {}          # OUR OWN signed YES-equivalent, from fills
        self.name = strategy_id
        self.tier = "SIGNAL"
        self.cadence_s = _fast_cadence(config, mt.get("cadence_s"))
        if self._enabled and self._sigma_p is None:
            log.warning("mr_taker_no_sigma_p_intents_will_zero_size", strategy=strategy_id)

    def set_prices(self, prices: dict) -> None:
        self._prices = dict(prices or {})

    @staticmethod
    def _snap(p: float) -> float:
        return min(max(round(round(p / 0.005) * 0.005, 3), 0.005), 0.995)

    def evaluate(self, state: Any) -> list[Intent]:
        if not self._enabled:
            return []
        try:
            now = time.monotonic()
            out: list[Intent] = []
            for eid in self._exchange_ids:
                row = self._prices.get(eid)
                bb = _as_float((row or {}).get("best_bid"))
                ba = _as_float((row or {}).get("best_ask"))
                if bb is None or ba is None or ba <= bb:
                    continue
                mid = (bb + ba) / 2.0
                self._engine.observe(eid, mid, now)
                pos = int(self._pos.get(eid, 0))
                d = self._engine.decide(eid, position=pos, spread=(ba - bb), now=now)
                if d.kind == "none":
                    continue

                if d.kind == "enter_long":
                    side, action, opt, price = "buy", "open", "yes", self._snap(ba)       # cross up
                elif d.kind == "enter_short":
                    side, action, opt, price = "buy", "open", "no", self._snap(1.0 - bb)  # buy NO at touch
                elif d.kind == "exit":
                    # reduce-only SELL of the held leg (gate clamps to holdings)
                    if d.option_side == "yes":
                        side, action, opt, price = "sell", "close", "yes", self._snap(bb)
                    else:
                        side, action, opt, price = "sell", "close", "no", self._snap(1.0 - ba)
                else:
                    continue

                meta: dict[str, Any] = {"option_side": opt, "mr_z": d.z, "mr_reason": d.reason}
                if self._sigma_p is not None:
                    meta["sigma_p"] = self._sigma_p
                log.info("mr_taker_signal", strategy=self.name, exchange=eid, kind=d.kind,
                         z=d.z, reason=d.reason, side=side, option_side=opt, price=price, pos=pos)
                out.append(Intent(strategy_id=self.name, exchange_id=eid, side=side, action=action,
                                  strategy_type="mean_reversion", signal_strength=1.0,
                                  price=price, metadata=meta))
            return out
        except Exception as exc:                      # noqa: BLE001 — never escape evaluate()
            log.warning("mr_taker_evaluate_failed", strategy=self.name, error=str(exc))
            return []

    def on_fill(self, fill: Any) -> None:
        """Track our own signed YES-equivalent position: buy yes +q, sell yes -q, buy no -q, sell no +q."""
        try:
            eid = str(getattr(fill, "exchange_id", None) or (fill.get("exchange_id") if isinstance(fill, dict) else None))
            action = str(getattr(fill, "action", None) or (fill.get("action") if isinstance(fill, dict) else "")).lower()
            qty = int(float(getattr(fill, "quantity", None) or (fill.get("quantity") if isinstance(fill, dict) else 0) or 0))
            meta = getattr(fill, "metadata", None) or (fill.get("metadata") if isinstance(fill, dict) else {}) or {}
            opt = str(meta.get("option_side", "yes")).lower()
        except Exception:                             # noqa: BLE001
            return
        if not eid or qty <= 0:
            return
        signed = qty if opt == "yes" else -qty
        if action == "sell":
            signed = -signed
        self._pos[eid] = int(self._pos.get(eid, 0)) + signed
        if self._pos[eid] == 0:
            self._engine.clear_entry(eid)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()


# ─────────────────────────────────────────────────────────────────────────────────────
# CLASS 5 — CoreDutchBookStrategy (SIGNAL tier) — near-riskless complement arb
# ─────────────────────────────────────────────────────────────────────────────────────
class CoreDutchBookStrategy:
    """Complement (dutch-book) arb: when a race's D-YES bid + R-YES bid sum ABOVE 1, SHORT BOTH (buy NO
    on each) for a locked profit robust to a third-party win. Emits ATOMIC multi-leg intents
    (strategy_type='arb' → all-or-nothing, held to settlement), so a single naked leg is never left
    open. strategy_id='dutch_book' keeps its own allocator budget. Disabled unless dutch_book.enabled.

    Pairs are built ONCE at init from /markets titles ('Will the Democratic/Republican Party win the
    <contest>'). evaluate() is sync + non-blocking: it reads the pumped price snapshot only."""

    def __init__(self, *, store: Any, config: dict, client: Any = None,
                 equity: float | None = None, strategy_id: str = "dutch_book") -> None:
        import re
        from strategies.dutch_book import DutchBookConfig, DutchBookEngine

        db = (config or {}).get("dutch_book", {}) or {}
        self._store = store
        self._enabled = bool(db.get("enabled", False))
        self._equity = (float(equity) if equity else None)
        self._per_pair_frac = float(db.get("per_pair_frac", 0.01))   # capital per pair = frac x equity
        self._max_qty = int(db.get("max_qty_per_leg", 100000))
        # DURABLE SHADOW RECORDER: append every would-be arb to a jsonl so we can later compute, at
        # settlement, BOTH the full-arb return (both legs) and each leg ALONE (the naked-leg risk we
        # cannot otherwise estimate — the fillable leg may be adversely selected). Log-only; independent
        # of whether the strategy actually trades. Off unless dutch_book.record.
        self._record = bool(db.get("record", False))
        self._record_path = str(db.get("record_path", "data/dutch_book_shadow/arbs.jsonl"))
        self._place = bool(db.get("place", True))   # when False, RECORD ONLY (emit no live orders)
        self._cfg = DutchBookConfig(
            enabled=self._enabled,
            min_edge=float(db.get("min_edge", 0.01)),
            max_pairs=int(db.get("max_pairs", 40)),
        )
        self._engine = DutchBookEngine(self._cfg)
        self._prices: dict[str, dict] = {}
        self._pairs: dict[str, tuple[str, str]] = {}     # contest -> (D_exch, R_exch)
        self._fills: list[Any] = []
        self.name = strategy_id
        self.tier = "SIGNAL"
        self.cadence_s = _fast_cadence(config, db.get("cadence_s"))

        if self._enabled and client is not None:
            pat = re.compile(r"will the (democratic|republican) party win the (.+?)(?:\s+race)?\??$", re.I)
            try:
                from main_sig import markets_all_retrying
                markets = markets_all_retrying(client)
            except Exception as exc:                      # noqa: BLE001
                log.warning("dutch_book_markets_fetch_failed", error=str(exc)[:120])
                markets = []
            legs: dict[str, dict[str, str]] = {}
            for m in markets:
                if not isinstance(m, dict) or m.get("status") != "open":
                    continue
                exs = m.get("exchanges") or []
                mm = pat.match((m.get("question") or m.get("title") or "").strip())
                if not mm or not exs:
                    continue
                party = "D" if mm.group(1).lower().startswith("dem") else "R"
                contest = re.sub(r"\s+", " ", mm.group(2).strip().lower())
                legs.setdefault(contest, {})[party] = str(exs[0].get("id"))
            self._pairs = {c: (v["D"], v["R"]) for c, v in legs.items() if "D" in v and "R" in v}
            log.info("dutch_book_pairs_built", pairs=len(self._pairs))

    def set_equity(self, equity: float | None) -> None:
        try:
            self._equity = float(equity) if equity else self._equity
        except (TypeError, ValueError):
            pass

    def set_prices(self, prices: dict) -> None:
        self._prices = dict(prices or {})

    def _yes_bid(self, exch: str) -> float | None:
        row = self._prices.get(exch)
        return _as_float(row.get("best_bid")) if isinstance(row, dict) else None

    def _record_arb(self, contest, d_exch, r_exch, arb, qty) -> None:
        """Append one would-be arb to the durable jsonl. Enough to compute, at settlement, the full-arb
        return (both NO legs) AND each leg alone (naked-leg risk). NEVER raises into evaluate()."""
        try:
            import json as _json
            import os as _os
            from datetime import datetime as _dt, timezone as _tz
            _os.makedirs(_os.path.dirname(self._record_path) or ".", exist_ok=True)
            rec = {"ts": _dt.now(_tz.utc).isoformat(), "contest": contest, "edge": arb.edge, "qty": qty,
                   "d_exch": d_exch, "r_exch": r_exch,
                   "d_yes_bid": self._yes_bid(d_exch), "r_yes_bid": self._yes_bid(r_exch),
                   "d_no_entry": arb.legs[0].price, "r_no_entry": arb.legs[1].price}
            with open(self._record_path, "a") as fh:
                fh.write(_json.dumps(rec) + "\n")
        except Exception:                                 # noqa: BLE001 — recording must never break trading
            pass

    def set_excluded(self, exchange_ids) -> None:
        """Markets another strategy manages (cross_market races): never trade a pair touching them. In a cross race
        the cross exit override treats EVERY held position as cross's and sells the near-fair leg on convergence,
        and dutch re-buys the pair -> churn (2026-10-08 review)."""
        self._excluded = {str(x) for x in (exchange_ids or ())}

    def evaluate(self, state: Any) -> list[Intent]:
        if not self._enabled or not self._pairs:
            return []
        try:
            equity = self._equity or 0.0
            intents: list[Intent] = []
            excl = getattr(self, "_excluded", set())
            for contest, (d_exch, r_exch) in list(self._pairs.items())[: self._cfg.max_pairs]:
                if str(d_exch) in excl or str(r_exch) in excl:
                    continue
                arb = self._engine.evaluate_pair(contest, d_exch, r_exch,
                                                 self._yes_bid(d_exch), self._yes_bid(r_exch), self._cfg)
                if arb is None:
                    continue
                # 1:1 pair; size the per-leg qty from capital (equity x per_pair_frac / leg price).
                lp = max(arb.legs[0].price, arb.legs[1].price, 0.01)
                qty = int(max(min((equity * self._per_pair_frac) / lp if equity else 0.0,
                                  self._max_qty), 0))
                if qty < 1:
                    continue
                legs = [{"exchange_id": lg.exchange_id, "side": "buy", "outcome": lg.outcome,
                         "action": "open", "price": lg.price, "quantity": qty} for lg in arb.legs]
                log.info("dutch_book_arb", strategy=self.name, contest=contest, edge=arb.edge,
                         d=d_exch, r=r_exch, qty=qty, prices=[lg.price for lg in arb.legs])
                if self._record:
                    self._record_arb(contest, d_exch, r_exch, arb, qty)
                if not self._place:          # RECORD-ONLY mode: log the opportunity, place nothing
                    continue
                intents.append(Intent(strategy_id=self.name, exchange_id=f"dutchbook:{contest}",
                                      side="buy", action="open", strategy_type="arb",
                                      price=arb.legs[0].price,
                                      metadata={"dutch_book": True, "edge": arb.edge, "qty": qty},
                                      legs=legs))
            return intents
        except Exception as exc:                          # noqa: BLE001 — never escape evaluate()
            log.warning("dutch_book_evaluate_failed", strategy=self.name, error=str(exc))
            return []

    def on_fill(self, fill: Any) -> None:
        self._fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()


# ─────────────────────────────────────────────────────────────────────────────────────
# CLASS 6 — CoreSniperStrategy (SIGNAL tier) — stale-quote sniper (take, don't wait)
# ─────────────────────────────────────────────────────────────────────────────────────
class CoreSniperStrategy:
    """Snipe other bots' STALE quotes: when a SIG touch is mispriced vs the SIG-aligned sharp (Polymarket)
    by >= min_edge, TAKE it marketable before the bot re-quotes — the fill is the stale order itself, so
    this is the executable answer to the thin-book fill problem. No deception: we only react faster to a
    real sharp move. Reuses the cross-market provider's aligned_benchmark + the pumped SIG bid/ask.
    strategy_id='sniper'. Disabled unless sniper.enabled; place=false => record-only."""

    def __init__(self, *, store: Any, config: dict, xm_provider: Any = None,
                 equity: float | None = None, strategy_id: str = "sniper") -> None:
        from strategies.sniper import SniperConfig, evaluate as _snipe_eval

        sn = (config or {}).get("sniper", {}) or {}
        self._store = store
        self._enabled = bool(sn.get("enabled", False))
        self._xm = xm_provider
        self._eval = _snipe_eval
        self._cfg = SniperConfig(
            enabled=self._enabled,
            min_edge=float(sn.get("min_edge", 0.03)),
            max_sharp_age_s=float(sn.get("max_sharp_age_s", 20.0)),
            sweep_to_edge=bool(sn.get("sweep_to_edge", True)),
            max_cross=float(sn.get("max_cross", 0.02)),
        )
        self._record = bool(sn.get("record", False))
        self._record_path = str(sn.get("record_path", "data/sniper_shadow/snipes.jsonl"))
        self._place = bool(sn.get("place", True))
        ref_sigma = sn.get("k_reference_sigma")
        if ref_sigma is None:
            ref_sigma = (config.get("sizing", {}) or {}).get("k_reference_sigma")
        try:
            self._sigma_p = None if ref_sigma is None else float(ref_sigma)
        except (TypeError, ValueError):
            self._sigma_p = None
        self._prices: dict[str, dict] = {}
        self._books: dict[str, dict] = {}   # exchange_id -> raw orderbook (bids/asks L2), bounded set only
        self._fills: list[Any] = []
        self.name = strategy_id
        self.tier = "SIGNAL"
        self.cadence_s = _fast_cadence(config, sn.get("cadence_s"))
        if self._enabled and xm_provider is None:
            log.warning("sniper_no_xm_provider_inert", strategy=strategy_id)

    def set_prices(self, prices: dict) -> None:
        self._prices = dict(prices or {})

    def reload(self, config: dict) -> None:
        """Hot-reload the tunables from a freshly-loaded config WITHOUT a restart (no state wiped).
        Rebuilds the frozen SniperConfig and re-reads the place/record/enabled switches; leaves prices,
        books and fills untouched. Called by main_sig's config-watch loop on an overlay change."""
        from strategies.sniper import SniperConfig
        sn = (config or {}).get("sniper", {}) or {}
        self._enabled = bool(sn.get("enabled", False))
        self._place = bool(sn.get("place", True))
        self._record = bool(sn.get("record", False))
        self._cfg = SniperConfig(
            enabled=self._enabled,
            min_edge=float(sn.get("min_edge", 0.03)),
            max_sharp_age_s=float(sn.get("max_sharp_age_s", 20.0)),
            sweep_to_edge=bool(sn.get("sweep_to_edge", True)),
            max_cross=float(sn.get("max_cross", 0.02)),
        )
        log.info("sniper_reloaded", enabled=self._enabled, place=self._place,
                 min_edge=self._cfg.min_edge, max_cross=self._cfg.max_cross, sweep=self._cfg.sweep_to_edge)

    def set_books(self, books: dict) -> None:
        """Feed L2 depth for the bounded book-poll set (same source order_flow uses). Depth is OPTIONAL —
        it only measures the stale size sitting mispriced (for the sweep's ranking/observability); markets
        without a book still snipe on the NBBO touch + the sharp-derived sweep price."""
        self._books = dict(books or {})

    def _record_snipe(self, eid, t, sig_bid, sig_ask, sharp) -> None:
        try:
            import json as _json, os as _os
            from datetime import datetime as _dt, timezone as _tz
            _os.makedirs(_os.path.dirname(self._record_path) or ".", exist_ok=True)
            with open(self._record_path, "a") as fh:
                fh.write(_json.dumps({"ts": _dt.now(_tz.utc).isoformat(), "exch": eid,
                                      "side": t.option_side, "take_price": t.price, "edge": t.edge,
                                      "reason": t.reason, "sig_bid": sig_bid, "sig_ask": sig_ask,
                                      "sharp": sharp}) + "\n")
        except Exception:                                 # noqa: BLE001
            pass

    def evaluate(self, state: Any) -> list[Intent]:
        if not self._enabled or self._xm is None:
            return []
        try:
            ids = self._xm.active_sig_exchange_ids() if hasattr(self._xm, "active_sig_exchange_ids") else []
            out: list[Intent] = []
            for eid in ids:
                eid = str(eid)
                row = self._prices.get(eid)
                if not isinstance(row, dict):
                    continue
                sig_bid, sig_ask = _as_float(row.get("best_bid")), _as_float(row.get("best_ask"))
                try:
                    sharp = self._xm.aligned_benchmark(eid)
                except Exception:                         # noqa: BLE001
                    sharp = None
                book = self._books.get(eid)
                bids = book.get("bids") if isinstance(book, dict) else None
                asks = book.get("asks") if isinstance(book, dict) else None
                t = self._eval(eid, sig_bid, sig_ask, sharp, None, self._cfg, bids=bids, asks=asks)
                if t is None:
                    continue
                log.info("sniper_take", strategy=self.name, exchange=eid, side=t.option_side,
                         price=t.price, edge=t.edge, reason=t.reason, sharp=sharp,
                         sig_bid=sig_bid, sig_ask=sig_ask, stale_depth=t.stale_depth,
                         sweep=self._cfg.sweep_to_edge)
                if self._record:
                    self._record_snipe(eid, t, sig_bid, sig_ask, sharp)
                if not self._place:
                    continue
                meta: dict[str, Any] = {"option_side": t.option_side, "snipe_edge": t.edge,
                                        "divergence": t.edge, "snipe_reason": t.reason,
                                        "stale_depth": t.stale_depth}
                if self._sigma_p is not None:
                    meta["sigma_p"] = self._sigma_p
                out.append(Intent(strategy_id=self.name, exchange_id=eid, side="buy", action="open",
                                  strategy_type="directional", signal_strength=max(t.edge, 0.0) or 1.0,
                                  price=t.price, metadata=meta))
            return out
        except Exception as exc:                          # noqa: BLE001
            log.warning("sniper_evaluate_failed", strategy=self.name, error=str(exc))
            return []

    def on_fill(self, fill: Any) -> None:
        self._fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()


class CoreBookPressureStrategy:
    """Order-book pressure (queue-imbalance) edge — SHADOW by default. On the bounded L2 book set (same
    feed order_flow/the sniper use), when the top-N resting depth is lopsided beyond min_imbalance the
    short-horizon price should drift toward the thin side (bids-heavy ⇒ back YES; asks-heavy ⇒ buy NO).
    Needs NO external sharp, so it covers the pure-SIG races cross/sniper can't. strategy_id='book_pressure'.
    place=false (default) ⇒ RECORD-ONLY: every would-be signal is logged with the current mid so we can
    measure later whether the pressure actually predicted the move before risking a cent."""

    def __init__(self, *, store: Any, config: dict, strategy_id: str = "book_pressure") -> None:
        from strategies.book_pressure import BookPressureConfig, evaluate as _bp_eval
        bp = (config or {}).get("book_pressure", {}) or {}
        self._store = store
        self._enabled = bool(bp.get("enabled", False))
        self._eval = _bp_eval
        self._cfg = BookPressureConfig(
            enabled=self._enabled,
            levels=int(bp.get("levels", 5)),
            min_imbalance=float(bp.get("min_imbalance", 0.33)),
            min_depth=float(bp.get("min_depth", 100.0)),
            tape_confirm=bool(bp.get("tape_confirm", False)),
            tape_min=float(bp.get("tape_min", 0.20)),
        )
        self._record = bool(bp.get("record", True))
        self._record_path = str(bp.get("record_path", "data/book_pressure_shadow/signals.jsonl"))
        self._place = bool(bp.get("place", False))   # SHADOW by construction
        self._prices: dict[str, dict] = {}
        self._books: dict[str, dict] = {}
        self._fills: list[Any] = []
        self.name = strategy_id
        self.tier = "SIGNAL"
        self.cadence_s = _fast_cadence(config, bp.get("cadence_s"))

    def set_prices(self, prices: dict) -> None:
        self._prices = dict(prices or {})

    def set_books(self, books: dict) -> None:
        self._books = dict(books or {})

    def reload(self, config: dict) -> None:
        from strategies.book_pressure import BookPressureConfig
        bp = (config or {}).get("book_pressure", {}) or {}
        self._enabled = bool(bp.get("enabled", False))
        self._place = bool(bp.get("place", False))
        self._record = bool(bp.get("record", True))
        self._cfg = BookPressureConfig(
            enabled=self._enabled, levels=int(bp.get("levels", 5)),
            min_imbalance=float(bp.get("min_imbalance", 0.33)), min_depth=float(bp.get("min_depth", 100.0)),
            tape_confirm=bool(bp.get("tape_confirm", False)), tape_min=float(bp.get("tape_min", 0.20)))
        log.info("book_pressure_reloaded", enabled=self._enabled, place=self._place,
                 min_imbalance=self._cfg.min_imbalance, levels=self._cfg.levels)

    @staticmethod
    def _mid_from_book(book: dict) -> float | None:
        """Mid from the orderbook's own touch (bestBid/bestAsk) — no price-feed dependency."""
        b, a = _as_float(book.get("bestBid")), _as_float(book.get("bestAsk"))
        if b is None or a is None:
            return None
        return (b + a) / 2.0

    def _record_signal(self, eid, sig, mid) -> None:
        try:
            import json as _json, os as _os
            from datetime import datetime as _dt, timezone as _tz
            _os.makedirs(_os.path.dirname(self._record_path) or ".", exist_ok=True)
            with open(self._record_path, "a") as fh:
                fh.write(_json.dumps({"ts": _dt.now(_tz.utc).isoformat(), "exch": eid,
                                      "side": sig.option_side, "qi": sig.qi,
                                      "bid_depth": sig.bid_depth, "ask_depth": sig.ask_depth,
                                      "tape_imbalance": sig.tape_imbalance, "reason": sig.reason,
                                      "mid_at_signal": mid}) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def evaluate(self, state: Any) -> list[Intent]:
        if not self._enabled:
            return []
        out: list[Intent] = []
        try:
            for eid, book in self._books.items():
                if not isinstance(book, dict):
                    continue
                sig = self._eval(str(eid), book.get("bids"), book.get("asks"), self._cfg)
                if sig is None:
                    continue
                mid = self._mid_from_book(book)
                log.info("book_pressure_signal", strategy=self.name, exchange=eid, side=sig.option_side,
                         qi=sig.qi, bid_depth=sig.bid_depth, ask_depth=sig.ask_depth,
                         tape=sig.tape_imbalance, reason=sig.reason, mid=mid)
                if self._record:
                    self._record_signal(str(eid), sig, mid)
                # place=false ⇒ shadow only: never emit an Intent until the recorded signals validate.
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("book_pressure_evaluate_failed", strategy=self.name, error=str(exc))
            return []

    def on_fill(self, fill: Any) -> None:
        self._fills.append(fill)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf()
