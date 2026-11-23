"""The sizer: edge + tournament standing + allocator budget + book depth -> an order, or nothing.

    size = k(σ_p)·f*  ×  M(ρ, τ)  ÷  √n_eff   ... within the edge budget, then capped
                                   (√n_eff INVERTED in the endgame-if-behind override, §2.3)

ORDER OF OPERATIONS MATTERS and follows §5: "uncertainty shrinkage and market-impact adjustment are
applied BEFORE any cap — the cap is a backstop, not the primary control". So the pipeline shrinks for
estimate-uncertainty and own-impact first, and only then clips to budget/caps/liquidity. Doing it the
other way round would let a cap do the work that robustness is supposed to do, and would hide how much
the raw size wanted to be.

THE GOVERNING POSTURE (§1b, Thorp): overbetting is far worse than underbetting, so every ambiguous
branch here resolves toward the SMALLER size and every missing input resolves toward NO trade:
  * σ_p absent          -> k = 0                 -> no order
  * σ_p above the gate   -> noise, whatever f* is -> no order
  * ρ not computable     -> standing unknown      -> no order
  * f* <= min_edge_f     -> noise                 -> no order
  * impact-adjusted edge dies before 1 contract   -> no order
  * book thinner than the computed size           -> size down to fillable, never half-fill
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .browne import (
    BrowneState,
    concentration_fraction,
    is_endgame_behind,
    multiplier,
)
from .config import SizingConfig, load_sizing_config
from .impact import (
    impact_adjusted_f,
    max_fillable_with_edge,
    round_limit_price,
)
from .kelly import (
    YES,
    correlation_from_loadings,
    diversification_divisor,
    effective_n,
    entry_price,
    k_of_sigma,
    kelly_fraction_star,
)


@dataclass(frozen=True, slots=True)
class Edge:
    """One directional edge from the research/signal layer.

    `sigma_p` is Optional on purpose — None is a meaningful, acted-on value (maximally uncertain),
    not a missing field to default away.
    """

    market_id: str
    exchange_id: str
    p: float                      # fair-value probability
    p_m: float                    # market price (top of book, on the YES axis)
    sigma_p: float | None = None  # uncertainty of p; None => maximally uncertain
    side: str = YES               # "yes" | "no"
    cluster: str | None = None    # correlated-cluster id (e.g. the national-swing cluster)
    factor_loading: float | None = None  # loading on the shared factor, for n_eff


@dataclass(frozen=True, slots=True)
class Standing:
    """Tournament state. Re-read every cycle (§7): T₃ moves as others trade."""

    balance: float
    third_place_threshold: float | None
    time_fraction_remaining: float


@dataclass(frozen=True, slots=True)
class SizedOrder:
    exchange_id: str
    side: str
    action: str
    quantity: int
    price: float

    def to_dict(self) -> dict:
        return {
            "exchange_id": self.exchange_id,
            "side": self.side,
            "action": self.action,
            "quantity": self.quantity,
            "price": self.price,
        }


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """The order (or None) plus every intermediate the decision rested on.

    The trace is not decoration: when a size looks wrong live, the question is always *which* factor
    produced it, and recomputing by hand from p/σ_p/ρ/τ is slow and error-prone.
    """

    order: SizedOrder | None
    reason: str
    f_star: float = 0.0
    k: float = 0.0
    f_shrunk: float = 0.0
    M: float = 0.0
    n_eff: float = 1.0
    divisor: float = 1.0
    impact_f: float = 0.0
    avg_fill_price: float | None = None
    target_notional: float = 0.0
    final_notional: float = 0.0
    endgame_override: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def sized(self) -> bool:
        return self.order is not None

    def to_dict(self) -> dict:
        return self.order.to_dict() if self.order else {}


class Sizer:
    """Pure sizing logic. Public entry points are async so they compose with the bot's loops."""

    def __init__(self, cfg: SizingConfig | dict | None = None) -> None:
        if cfg is None:
            self.cfg = load_sizing_config()
        elif isinstance(cfg, SizingConfig):
            self.cfg = cfg
        else:
            self.cfg = SizingConfig.from_cfg(cfg)

    # -- components (sync, pure; exposed for testing and for the risk gate's re-derivation) --- #
    def browne_multiplier(self, standing: Standing) -> float | None:
        st = BrowneState.from_standing(
            standing.balance, standing.third_place_threshold, standing.time_fraction_remaining
        )
        if st is None:
            return None
        c = self.cfg
        return multiplier(
            st, rho_min=c.rho_min, rho_max=c.rho_max, tau_floor=c.tau_floor,
            M_0=c.M_0, M_cap=c.M_cap,
        )

    def k_for(self, sigma_p: float | None) -> float:
        c = self.cfg
        return k_of_sigma(
            sigma_p, sigma_grid=c.sigma_grid, k_grid=c.k_grid, k_missing_sigma=c.k_missing_sigma,
        )

    def n_eff_for(self, edges: list[Edge], weights: list[float] | None = None) -> float:
        if not edges:
            return 1.0
        loadings = [
            self.cfg.default_factor_loading if e.factor_loading is None else float(e.factor_loading)
            for e in edges
        ]
        w = weights if weights is not None else [1.0] * len(edges)
        return effective_n(w, correlation_from_loadings(loadings))

    # -- single-edge sizing -------------------------------------------------- #
    async def size_edge(
        self,
        edge: Edge,
        standing: Standing,
        *,
        budget: float,
        book: dict | None = None,
        n_eff: float = 1.0,
        force_endgame: bool | None = None,
    ) -> SizingDecision:
        """Size ONE edge. `budget` is the allocator's capital (in balance units) for this edge.

        `n_eff` comes from the book-level call; a lone edge is one bet (n_eff = 1).
        """
        c = self.cfg

        # --- gate 1: standing must be known ------------------------------- #
        st = BrowneState.from_standing(
            standing.balance, standing.third_place_threshold, standing.time_fraction_remaining
        )
        if st is None:
            return SizingDecision(None, "no_standing: third_place_threshold missing/zero -> rho unknown")

        # --- gate 2: the edge-REALITY gate (absolute, §2.1/§5) ------------- #
        f_star = kelly_fraction_star(edge.p, edge.p_m, edge.side)
        if f_star <= 0.0:
            return SizingDecision(None, f"no_edge: f*={f_star:.6f} <= 0", f_star=f_star)
        if f_star <= c.min_edge_f:
            return SizingDecision(
                None, f"edge_below_noise_floor: f*={f_star:.6f} <= min_edge_f={c.min_edge_f}",
                f_star=f_star,
            )
        if edge.sigma_p is None:
            return SizingDecision(
                None, "no_sigma_p: estimate reports no uncertainty -> maximally uncertain -> zero",
                f_star=f_star, k=self.k_for(None),
            )
        if float(edge.sigma_p) > c.max_sigma_to_act:
            return SizingDecision(
                None,
                f"low_confidence: sigma_p={edge.sigma_p} > max_sigma_to_act={c.max_sigma_to_act} "
                f"-> positive edge treated as estimation noise",
                f_star=f_star, k=self.k_for(edge.sigma_p),
            )

        # --- base: k(sigma) * f*, then Browne tilt ------------------------- #
        k = self.k_for(edge.sigma_p)
        f_shrunk = k * f_star
        m = multiplier(
            st, rho_min=c.rho_min, rho_max=c.rho_max, tau_floor=c.tau_floor,
            M_0=c.M_0, M_cap=c.M_cap,
        )

        endgame = is_endgame_behind(st, m, endgame_tau=c.endgame_tau) if force_endgame is None \
            else bool(force_endgame)
        # §2.3's sharpened condition: concentrate only onto a HIGH-CONFIDENCE estimate. A big
        # concentrated bet on a shaky edge with no recovery time is the overbetting trap at its worst.
        if endgame and float(edge.sigma_p) > c.endgame_min_confidence_sigma:
            endgame = False

        # --- diversification, or its deliberate inversion ------------------ #
        if endgame:
            # §2.3: the override INVERTS 1/sqrt(n_eff) — the correlated cluster is treated as ONE bet,
            # because N independent edges converge (LLN) to a medium balance that never reaches a far
            # target, while the concentrated correlated bet is the only shape that can.
            divisor = 1.0
        else:
            divisor = diversification_divisor(n_eff)

        fraction = f_shrunk * m / divisor

        # --- notional: within the allocator's budget, then caps ------------ #
        bankroll = float(standing.balance)
        target_notional = fraction * bankroll
        caps = {
            "edge_budget": float(budget),
            "per_market": c.max_per_market_frac * bankroll,
        }
        if endgame:
            conc = concentration_fraction(
                st, m, M_full=c.M_full, all_in_cap_frac=c.all_in_cap_frac
            )
            # The override is AUTHORIZED to exceed the ordinary per-market cap (that is the point of
            # §2.3) but never the all-in cap, and never the edge budget.
            caps.pop("per_market")
            caps["all_in"] = conc * bankroll
        binding = min(caps.values()) if caps else 0.0
        notional = max(min(target_notional, binding), 0.0)
        tightest_cap = next((n for n, v in caps.items() if v == binding), "none")
        # Report a BINDING cap only when it actually reduced the target. The tightest cap is almost
        # always one of these three; naming it unconditionally would make `binding_cap` read
        # "per_market" on every uncapped order, which is the opposite of a useful diagnostic (and
        # would let a test assert on a cap that never fired).
        binding_name = tightest_cap if binding < target_notional else None

        if notional <= 0.0:
            return SizingDecision(
                None, f"zero_after_caps: binding={tightest_cap}", f_star=f_star, k=k,
                f_shrunk=f_shrunk, M=m, n_eff=n_eff, divisor=divisor,
                target_notional=target_notional, endgame_override=endgame,
            )

        # --- market impact (§2.1b) ----------------------------------------- #
        px = entry_price(edge.p_m, edge.side)
        if not (0.0 < px < 1.0):
            return SizingDecision(None, f"degenerate_price: entry={px}", f_star=f_star, k=k)
        want_qty = int(notional / px)
        if want_qty < c.min_quantity:
            return SizingDecision(
                None,
                f"below_min_quantity: {want_qty} < {c.min_quantity} (notional={notional:.2f})",
                f_star=f_star, k=k, f_shrunk=f_shrunk, M=m, n_eff=n_eff, divisor=divisor,
                target_notional=target_notional, endgame_override=endgame,
            )

        if book:
            fillable = max_fillable_with_edge(
                book, edge.p, edge.side, min_f=c.min_impact_adjusted_f, hard_cap=want_qty,
            )
            if fillable < c.min_quantity:
                return SizingDecision(
                    None,
                    "impact_kills_edge: no quantity keeps the impact-adjusted edge above "
                    f"min_impact_adjusted_f={c.min_impact_adjusted_f}",
                    f_star=f_star, k=k, f_shrunk=f_shrunk, M=m, n_eff=n_eff, divisor=divisor,
                    target_notional=target_notional, endgame_override=endgame,
                )
            qty = fillable
            impact_f, fill = impact_adjusted_f(book, qty, edge.p, edge.side)
            limit = round_limit_price(fill.worst_price, "buy") if fill else round_limit_price(px, "buy")
            avg_fill = fill.avg_price if fill else None
        else:
            # No book supplied: size at the quote but DO NOT pretend impact was checked.
            qty = want_qty
            impact_f = f_star
            limit = round_limit_price(px, "buy")
            avg_fill = None

        order = SizedOrder(
            exchange_id=edge.exchange_id, side=edge.side, action="buy", quantity=int(qty), price=limit,
        )
        return SizingDecision(
            order=order,
            reason="sized_endgame_concentration" if endgame else "sized",
            f_star=f_star, k=k, f_shrunk=f_shrunk, M=m, n_eff=n_eff, divisor=divisor,
            impact_f=impact_f, avg_fill_price=avg_fill,
            target_notional=target_notional, final_notional=qty * (avg_fill or px),
            endgame_override=endgame,
            meta={
                "rho": st.rho, "tau": st.tau,
                "binding_cap": binding_name, "tightest_cap": tightest_cap,
                "impact_checked": bool(book), "cluster": edge.cluster,
            },
        )

    # -- book-level (multivariate) sizing ------------------------------------ #
    async def size_book(
        self,
        edges: list[Edge],
        standing: Standing,
        *,
        budgets: dict[str, float] | float,
        books: dict[str, dict] | None = None,
    ) -> list[SizingDecision]:
        """Size a whole correlated book: compute n_eff once, scale every position by 1/√n_eff.

        `budgets` is per-edge capital keyed by exchange_id, or one number split evenly. In the
        endgame-if-behind case the override concentrates on the single highest-edge, high-confidence
        cluster and the rest of the book is not sized — the cluster becomes ONE bet (§2.3).
        """
        if not edges:
            return []
        c = self.cfg
        books = books or {}

        def budget_for(e: Edge) -> float:
            if isinstance(budgets, dict):
                return float(budgets.get(e.exchange_id, 0.0))
            return float(budgets) / len(edges)

        st = BrowneState.from_standing(
            standing.balance, standing.third_place_threshold, standing.time_fraction_remaining
        )
        if st is None:
            return [
                SizingDecision(None, "no_standing: third_place_threshold missing/zero -> rho unknown")
                for _ in edges
            ]
        m = multiplier(
            st, rho_min=c.rho_min, rho_max=c.rho_max, tau_floor=c.tau_floor,
            M_0=c.M_0, M_cap=c.M_cap,
        )

        # Only edges that clear the reality gate participate in n_eff — a gated-out edge is not a bet,
        # so counting it would inflate n_eff and under-scale the real positions.
        actionable = [
            e for e in edges
            if kelly_fraction_star(e.p, e.p_m, e.side) > c.min_edge_f
            and e.sigma_p is not None
            and float(e.sigma_p) <= c.max_sigma_to_act
        ]

        if is_endgame_behind(st, m, endgame_tau=c.endgame_tau):
            confident = [
                e for e in actionable
                if float(e.sigma_p) <= c.endgame_min_confidence_sigma  # type: ignore[arg-type]
            ]
            if confident:
                best_cluster = self._best_cluster(confident)
                out: list[SizingDecision] = []
                for e in edges:
                    if e in best_cluster:
                        out.append(
                            await self.size_edge(
                                e, standing, budget=budget_for(e),
                                book=books.get(e.exchange_id), n_eff=1.0, force_endgame=True,
                            )
                        )
                    else:
                        out.append(
                            SizingDecision(
                                None,
                                "endgame_concentration: capital committed to the highest-edge "
                                "high-confidence cluster (§2.3) — this position is not sized",
                                M=m, endgame_override=True,
                            )
                        )
                return out

        n_eff = self.n_eff_for(actionable) if actionable else 1.0
        return [
            await self.size_edge(
                e, standing, budget=budget_for(e), book=books.get(e.exchange_id), n_eff=n_eff,
            )
            for e in edges
        ]

    def _best_cluster(self, edges: list[Edge]) -> list[Edge]:
        """The highest-edge correlated cluster, ranked by shrunk edge (i.e. confidence-gated, §2.3).

        Ranking on raw f* would pick the shakiest estimate — large f* and large σ_p correlate, since a
        wild estimate is both far from the market and uncertain. Ranking on k(σ_p)·f* is what makes
        "high-edge AND high-confidence" a single criterion rather than two.
        """
        by_cluster: dict[str, list[Edge]] = {}
        for e in edges:
            by_cluster.setdefault(e.cluster or f"__solo__:{e.exchange_id}", []).append(e)
        best_key, best_score = None, -1.0
        for key, members in by_cluster.items():
            score = sum(
                self.k_for(m.sigma_p) * kelly_fraction_star(m.p, m.p_m, m.side) for m in members
            )
            if score > best_score:
                best_key, best_score = key, score
        return by_cluster.get(best_key or "", [])
