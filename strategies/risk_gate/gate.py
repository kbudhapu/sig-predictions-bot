"""Deterministic pre-trade risk gate — Layer A of SIG_RISK_GOVERNANCE_SPEC (§2–3).

The hard, LLM-free floor under every order. Strategies PROPOSE orders; this gate DISPOSES:
  • APPROVE (unchanged),
  • APPROVE-CLAMPED (resized SMALLER to fit a cap — "fail small, not freeze", §0), or
  • REJECT (an erroneous/bug order, or no room at all to add exposure).

It NEVER increases a size. Risk concerns resolve by RESIZING (clamp); only erroneous-order checks REJECT
(they are bugs, not trades — the Knight-Capital class, §2). All limits come from config/sig.yaml; none is
hardcoded. No LLM, no network, no probabilistic call — pure arithmetic on dicts, asyncio-safe (sync & fast).

Patterns reused from mbappe (read-only reference): propose-then-cap with monotone `min()` clamps that only
reduce (portfolio_risk_governor sizing_multipliers), a hard absolute-limit pre-trade gate with exits exempt
(portfolio_brain.validate_global_exposure), and "correlated positions are ONE risk unit" (the cluster cap).
mbappe's equities-specific checks (beta / sector / factor crowding) are deliberately NOT ported — they do
not apply to prediction markets.

─ Handoff contracts (SIG_INTEGRATION_SPEC §7) ─
order = {
    "exchange_id": str,          # the SIG market/contract id
    "side": "yes" | "no",        # which outcome
    "action": "buy" | "sell",    # buy ADDS exposure (gated); sell REDUCES (exempt from exposure caps)
    "price": float,              # limit price on the 0.005 grid, in [0.005, 0.995]
    "quantity": int,             # shares (must be a positive integer)
    "order_type": "limit"|...,   # optional (defaults to limit)
    "cluster": str | None,       # optional: the correlated-cluster id this market belongs to
    "strategy": str,             # optional: proposing strategy id (for the decision log)
}
portfolio_state = {
    "balance": float,                               # total capital B (fractions are of this)
    "deployed": float,                              # optional; else summed from positions
    "positions": { exchange_id: {"notional": float, "quantity": int, "cluster": str|None} },
    "market_cluster": { exchange_id: cluster_id },  # optional market→cluster map
}
tournament_state = { "rho": float, "tau": float }   # ρ = balance / 3rd-place-threshold; τ = time fraction left
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field

_NORM = statistics.NormalDist(0.0, 1.0)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #
@dataclass
class GateResult:
    """Outcome of the gate. `order` is the (possibly clamped) order on approval, else None."""

    approved: bool
    order: dict | None
    reason: str = ""
    clamped: bool = False
    original_quantity: int = 0
    final_quantity: int = 0
    checks: list[str] = field(default_factory=list)  # which checks fired (for the decision log)

    @classmethod
    def approve(cls, order: dict, *, original_qty: int, checks: list[str], reason: str = "") -> "GateResult":
        final_qty = int(order["quantity"])
        return cls(
            approved=True,
            order=order,
            reason=reason,
            clamped=final_qty < original_qty,
            original_quantity=original_qty,
            final_quantity=final_qty,
            checks=checks,
        )

    @classmethod
    def reject(cls, reason: str, *, original_qty: int, checks: list[str]) -> "GateResult":
        return cls(
            approved=False,
            order=None,
            reason=reason,
            clamped=False,
            original_quantity=original_qty,
            final_quantity=0,
            checks=checks,
        )


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #
def _default_config_path() -> str:
    # strategies/risk_gate/gate.py → repo root is two dirs up → config/sig.yaml
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "config", "sig.yaml")


def _load_config(config: dict | None, config_path: str | None) -> dict:
    if config is not None:
        return config
    import yaml  # lazy: only needed when loading from disk

    with open(config_path or _default_config_path(), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
class RiskGate:
    """Deterministic pre-trade gate. Construct once (reads config), then call `check_order` per order."""

    def __init__(self, *, config: dict | None = None, config_path: str | None = None) -> None:
        cfg = _load_config(config, config_path)
        cap = cfg.get("capital", {})
        risk = cfg.get("risk", {})

        # exposure caps (fractions of balance)
        self.max_deploy_frac = float(cap.get("max_deploy_frac", 0.85))
        self.max_per_market_frac = float(cap.get("max_per_market_frac", 0.15))
        self.max_cluster_frac = float(cap.get("max_cluster_frac", 0.25))
        # Minimum size for an EXPOSURE-ADDING order AFTER the clamps — a sub-min order (often 1 share when
        # the gate clamps down near zero cash) wastes one of the scarce 30 writes/min for ~nothing. Skip
        # it instead. Default 1 = no-op (any positive qty passes); set >1 in config to activate. Reduce-only
        # sells (exits/dust flattening) are exempt — they return before this check.
        self.min_order_qty = int(cap.get("min_order_qty", 1))

        # ARB gets its own, SMALLER per-position cap. max_per_market_frac is a DIRECTIONAL limit --
        # it bounds what one view about the world may lose -- and a hard arb is not a view but a
        # bought spread whose payoff is contracted at entry. Applying 0.15/leg made a 2-leg basket
        # 30% of the bankroll, so the deploy cap admitted only ~3 concurrent arbs and every further
        # edge was discarded for want of capital. Absent arb_sizing this falls back to the
        # directional cap, i.e. exactly the old behaviour -- an older config cannot silently change
        # anything. Single source of truth: strategies/arb_sizing reads the same key.
        arb_sizing = cfg.get("arb_sizing", {}) or {}
        self.arb_max_per_arb_frac = float(
            arb_sizing.get("arb_max_per_arb_frac", self.max_per_market_frac))

        # erroneous-order sanity limits
        self.tick = float(risk.get("tick", 0.005))
        self.price_min = float(risk.get("price_min", 0.005))
        self.price_max = float(risk.get("price_max", 0.995))
        self.tick_tol = float(risk.get("tick_tol", 1e-9))
        # We size off CAPITAL only, so the sole fat-finger guard is a FRACTION of balance. Dropped: the
        # raw share-count ceiling (wrong unit — a cheap-price capital-sized order is millions of shares)
        # and the absolute-$ ceiling (does not scale — $100k is absurd at $2M equity). `hard_notional_frac`
        # is price- and bankroll-invariant.
        self.hard_notional_frac = float(risk.get("hard_notional_frac", 1.0))

        # authorized-concentration (ρ,τ) parameters — mirror the sizing spec so the ceiling re-derives
        self.all_in_cap_frac = float(risk.get("all_in_cap_frac", 0.95))
        self.rho_min = float(risk.get("rho_min", 0.01))
        self.rho_max = float(risk.get("rho_max", 0.99))
        self.tau_floor = float(risk.get("tau_floor", 0.01))
        self.M_0 = float(risk.get("M_0", 1.1284))
        self.M_full = float(risk.get("M_full", 3.0))

    # ── (ρ,τ) → Browne tournament multiplier M, re-derived exactly as the sizing module computes it ──
    def tournament_multiplier(self, rho: float, tau: float) -> float:
        rho_c = min(self.rho_max, max(self.rho_min, float(rho)))
        tau_c = max(self.tau_floor, float(tau))
        nu = _NORM.inv_cdf(rho_c)
        cdf = _NORM.cdf(nu)
        if cdf <= 0.0:
            cdf = 1e-12
        m_raw = (1.0 / math.sqrt(tau_c)) * (_NORM.pdf(nu) / cdf)
        return m_raw / self.M_0 if self.M_0 > 0 else m_raw

    # ── the MAXIMUM single-market/cluster concentration the sizing module authorizes at (ρ,τ) ──
    def authorized_concentration_ceiling(self, rho: float, tau: float) -> float:
        """Grind / ahead → the normal cluster cap (no override). Endgame-and-behind (M>1, ρ<1) → an
        escalating ceiling from cluster_cap up to all_in_cap as M rises to M_full (sizing §2.3 + §5).
        This is the hard ceiling the gate enforces: concentration ≤ it passes, > it clamps to it —
        catching accidental runaways AND a bug in the sizing module itself."""
        m = self.tournament_multiplier(rho, tau)
        if m <= 1.0 or float(rho) >= 1.0:
            return self.max_cluster_frac
        span = max(self.M_full - 1.0, 1e-9)
        frac = min(1.0, max(0.0, (m - 1.0) / span))
        ceiling = self.max_cluster_frac + frac * (self.all_in_cap_frac - self.max_cluster_frac)
        return min(ceiling, self.all_in_cap_frac)

    def _is_override(self, rho: float, tau: float) -> bool:
        return self.tournament_multiplier(rho, tau) > 1.0 and float(rho) < 1.0

    # ── erroneous-order checks (the Knight-Capital bug-catchers) → REJECT, never clamp ──
    def _erroneous_reason(self, order: dict, balance: float) -> str | None:
        price = order.get("price")
        qty = order.get("quantity")

        if price is None or not isinstance(price, (int, float)) or not math.isfinite(float(price)):
            return "missing_or_nonfinite_price"
        price = float(price)
        if price < self.price_min or price > self.price_max:
            return "price_out_of_bounds"
        # off-tick: price must sit on the 0.005 grid
        steps = price / self.tick
        if abs(steps - round(steps)) > max(self.tick_tol, self.tick_tol * steps):
            return "off_tick_price"

        # quantity must be a positive integer
        if isinstance(qty, bool) or qty is None:
            return "non_integer_quantity"
        if isinstance(qty, float):
            if not qty.is_integer():
                return "non_integer_quantity"
            qty = int(qty)
        if not isinstance(qty, int):
            return "non_integer_quantity"
        if qty <= 0:
            return "non_positive_quantity"

        # The ONE fat-finger guard: notional as a FRACTION OF CAPITAL. No raw share-count ceiling (wrong
        # unit) and no absolute-$ ceiling (does not scale — capping one order at $100k is absurd at $2M
        # equity). A fraction of balance is the only price- AND bankroll-invariant bound.
        notional = price * qty
        if balance > 0 and notional > self.hard_notional_frac * balance:
            return "notional_exceeds_balance_fraction_ceiling"
        return None

    # ── state helpers ──
    @staticmethod
    def _deployed(portfolio_state: dict) -> float:
        if "deployed" in portfolio_state and portfolio_state["deployed"] is not None:
            return float(portfolio_state["deployed"])
        return sum(float(p.get("notional", 0.0)) for p in portfolio_state.get("positions", {}).values())

    @staticmethod
    def _is_arb_order(order: dict) -> bool:
        """Is this order part of an arbitrage basket?

        Checked from the ORDER, not from the caller, so the arb cap cannot be obtained by calling a
        different method -- there is still exactly one choke point. Both spellings are accepted
        because the single-order path carries `strategy_type` from the Intent while the multi-leg
        path stamps `is_arb` onto each gate leg.
        """
        if bool(order.get("is_arb")):
            return True
        return str(order.get("strategy_type", "")).strip().lower() == "arb"

    @staticmethod
    def _cluster_of(order: dict, exch: str, portfolio_state: dict) -> str:
        cid = order.get("cluster")
        if cid is None:
            cid = portfolio_state.get("market_cluster", {}).get(exch)
        # a market with no declared cluster is its OWN cluster (so the cluster cap == per-market cap there)
        return str(cid) if cid is not None else f"__market__:{exch}"

    def _cluster_notional(self, cluster_id: str, exch: str, order: dict, portfolio_state: dict) -> float:
        positions = portfolio_state.get("positions", {})
        mc = portfolio_state.get("market_cluster", {})
        total = 0.0
        for pid, p in positions.items():
            pc = p.get("cluster")
            if pc is None:
                pc = mc.get(pid, f"__market__:{pid}")
            if str(pc) == cluster_id:
                total += float(p.get("notional", 0.0))
        return total

    @staticmethod
    def _clamp_qty_to_room(qty: int, price: float, room_notional: float) -> int:
        if price <= 0:
            return 0
        if room_notional <= 0:
            return 0
        return min(qty, int(math.floor(room_notional / price)))

    # ── the public entry point ──
    def check_order(self, order: dict, portfolio_state: dict, tournament_state: dict | None = None) -> GateResult:
        """Validate ONE order against portfolio + tournament state. Returns an approved (possibly clamped)
        order or a rejection. Deterministic; safe to call from async code (no I/O, no blocking)."""
        tournament_state = tournament_state or {}
        checks: list[str] = []
        original_qty = int(order.get("quantity")) if isinstance(order.get("quantity"), (int, float)) and not isinstance(order.get("quantity"), bool) and float(order.get("quantity")).is_integer() else -1
        balance = float(portfolio_state.get("balance", 0.0))

        # 1) ERRONEOUS-ORDER checks → REJECT (these are bugs, not trades)
        err = self._erroneous_reason(order, balance)
        if err is not None:
            checks.append(f"erroneous:{err}")
            oq = original_qty if original_qty >= 0 else 0
            return GateResult.reject(err, original_qty=oq, checks=checks)

        price = float(order["price"])
        qty = int(order["quantity"])
        original_qty = qty
        exch = str(order.get("exchange_id", ""))
        action = str(order.get("action", "buy")).lower()

        # 2) SELL / exposure-reducing orders are exempt from exposure caps (exits always permitted, mbappe
        #    pattern). Clamp only so you never sell MORE than you hold (that would be an error, resize-safe).
        if action == "sell":
            held = int(portfolio_state.get("positions", {}).get(exch, {}).get("quantity", 0) or 0)
            if held <= 0:
                # no position to reduce → a "sell" here would open a NAKED SHORT via the exempt path.
                # Reject: exposure-ADDING trades must come as action="buy" (side="no") through the caps.
                checks.append("sell_without_holding")
                return GateResult.reject("sell_without_holding", original_qty=original_qty, checks=checks)
            if qty > held:
                qty = held
                checks.append("sell_clamped_to_held")
            out = dict(order)
            out["quantity"] = qty
            checks.append("sell_exempt_from_exposure_caps")
            return GateResult.approve(out, original_qty=original_qty, checks=checks)

        # 3) BUY / exposure-adding → balance, aggregate, per-market, per-cluster clamps (resize, "fail small")
        rho = float(tournament_state.get("rho", 1.0))
        tau = float(tournament_state.get("tau", 1.0))
        override = self._is_override(rho, tau)
        ceiling = self.authorized_concentration_ceiling(rho, tau)
        per_market_frac = ceiling if override else self.max_per_market_frac
        cluster_frac = ceiling if override else self.max_cluster_frac
        # ARB: substitute the arb cap for the DIRECTIONAL per-market cap. Note it is deliberately
        # NOT raised by the endgame override: the override exists so a losing bot can concentrate a
        # directional swing, whereas concentrating arb would mean FEWER locked spreads -- the
        # opposite of what it needs. The cluster cap still applies unchanged.
        is_arb = self._is_arb_order(order)
        if is_arb:
            per_market_frac = self.arb_max_per_arb_frac
            checks.append(f"arb_cap({per_market_frac:.3f})")
        if override:
            checks.append(f"authorized_concentration(M={self.tournament_multiplier(rho, tau):.2f},ceil={ceiling:.3f})")

        # 3a) balance / collateral — never approve an order the balance can't cover (a buy costs price·qty)
        if balance <= 0:
            return GateResult.reject("no_balance", original_qty=original_qty, checks=checks)
        free = balance - self._deployed(portfolio_state)
        before = qty
        qty = self._clamp_qty_to_room(qty, price, free)
        if qty < before:
            checks.append("clamp:balance_collateral")

        # 3b) aggregate exposure cap (always on — the reserve floor, §2)
        agg_room = self.max_deploy_frac * balance - self._deployed(portfolio_state)
        before = qty
        qty = self._clamp_qty_to_room(qty, price, agg_room)
        if qty < before:
            checks.append("clamp:aggregate_deploy")

        # 3c) per-market cap (raised to the authorized ceiling in the endgame override)
        market_existing = float(portfolio_state.get("positions", {}).get(exch, {}).get("notional", 0.0))
        market_room = per_market_frac * balance - market_existing
        before = qty
        qty = self._clamp_qty_to_room(qty, price, market_room)
        if qty < before:
            checks.append(f"clamp:per_market({per_market_frac:.3f})")

        # 3d) per-correlated-cluster cap (the binding control for correlated books; raised under override)
        cluster_id = self._cluster_of(order, exch, portfolio_state)
        cluster_existing = self._cluster_notional(cluster_id, exch, order, portfolio_state)
        cluster_room = cluster_frac * balance - cluster_existing
        before = qty
        qty = self._clamp_qty_to_room(qty, price, cluster_room)
        if qty < before:
            checks.append(f"clamp:cluster({cluster_frac:.3f})")

        # never increase
        qty = min(qty, original_qty)

        if qty <= 0:
            # no room to ADD exposure now — reject THIS order (not a halt; sells/other markets still pass)
            return GateResult.reject("no_room_to_add_exposure", original_qty=original_qty, checks=checks)

        if qty < self.min_order_qty:
            # clamped below the minimum worth a write (e.g. a 1-share order near zero cash) → skip it rather
            # than burn one of the ~30 writes/min for ~nothing. Exposure-adding only — reduce-only sells
            # returned above, so dust can still be flattened.
            checks.append(f"below_min_order_qty({self.min_order_qty})")
            return GateResult.reject("below_min_order_qty", original_qty=original_qty, checks=checks)

        out = dict(order)
        out["quantity"] = qty
        return GateResult.approve(out, original_qty=original_qty, checks=checks)

    # ── multi-leg: check each leg; clamp ALL legs by the SAME factor (preserve the hedge ratio) ──
    def check_multi_leg(
        self, legs: list[dict], portfolio_state: dict, tournament_state: dict | None = None
    ) -> GateResult:
        """Atomic multi-leg (e.g. an arb). Erroneous leg → REJECT the whole batch. Otherwise clamp EVERY
        leg by the single worst clamp factor so leg ratios are preserved (a lopsided clamp would break the
        hedge). Returns a GateResult whose `order` carries the clamped `legs`."""
        checks: list[str] = []
        if not legs:
            return GateResult.reject("empty_multi_leg", original_qty=0, checks=checks)
        original_total = sum(int(l.get("quantity", 0)) for l in legs if isinstance(l.get("quantity"), (int, float)))

        # erroneous check per leg (atomic: any bug leg kills the batch)
        balance = float(portfolio_state.get("balance", 0.0))
        for i, leg in enumerate(legs):
            err = self._erroneous_reason(leg, balance)
            if err is not None:
                checks.append(f"leg{i}:erroneous:{err}")
                return GateResult.reject(f"leg{i}_{err}", original_qty=original_total, checks=checks)

        # per-leg clamp factor from a single-leg check; take the MIN factor and apply to all legs
        factor = 1.0
        for i, leg in enumerate(legs):
            res = self.check_order(leg, portfolio_state, tournament_state)
            if not res.approved:
                # a leg with no room at all → the atomic batch cannot be placed in balance
                return GateResult.reject(f"leg{i}_{res.reason}", original_qty=original_total, checks=checks + res.checks)
            oq = int(leg.get("quantity"))
            fq = int(res.order["quantity"])
            if oq > 0:
                factor = min(factor, fq / oq)
            checks.extend(f"leg{i}:{c}" for c in res.checks)

        clamped_legs = []
        for leg in legs:
            out = dict(leg)
            out["quantity"] = int(math.floor(int(leg["quantity"]) * factor))
            clamped_legs.append(out)

        if any(l["quantity"] <= 0 for l in clamped_legs):
            return GateResult.reject("multi_leg_clamped_to_zero", original_qty=original_total, checks=checks)

        final_total = sum(l["quantity"] for l in clamped_legs)
        result = GateResult(
            approved=True,
            order={"legs": clamped_legs},
            reason="" if factor >= 1.0 else f"proportional_clamp(factor={factor:.3f})",
            clamped=factor < 1.0,
            original_quantity=original_total,
            final_quantity=final_total,
            checks=checks,
        )
        return result
