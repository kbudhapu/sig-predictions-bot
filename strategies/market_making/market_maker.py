"""Market-making strategy — composition + executor output contract (pure logic, no I/O).

Ties the three pieces together per market tick:
  1. quoter.compute_quote     → symmetric limits inside the spread (tick/bounds-correct)
  2. inventory.apply          → skew against net position; pull a side at the cap
  3. adverse_selection.apply  → pull/widen the side being picked off on a fast mid move

and emits the contract the executor consumes:
  orders:  [{exchange_id, side, action, price, quantity, order_type: "limit", strategy: "mm"}]
  cancels: [{action: "cancel", exchange_id, quote_side: "bid"|"ask", strategy: "mm"}]

The strategy is a PURE function of market state + held inventory: same inputs → same desired
quote. It does NOT decide WHEN to re-post — the executor diffs the desired quote against resting
orders and only writes on change (resting orders are post-once-and-wait, which is what keeps us
under the 30 writes/min/key limit — recon §write limits). `on_market_state` is async so it drops
straight into the executor's event loop; the logic inside is non-blocking (no I/O, no sleeps).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from .quoter import QuoterConfig, Quote, compute_quote, round_to_tick
from .inventory import InventoryConfig, InventoryBook
from .adverse_selection import AdverseConfig, AdverseSelectionMonitor, AdverseSignal
from .mr_skew import MRSkewConfig, MeanReversionSkew
from .sizing import BaseSize, SizingConfig, base_quote_size, inventory_cap_shares

STRATEGY_TAG = "mm"


@dataclass(frozen=True)
class MarketMakerConfig:
    quoter: QuoterConfig = field(default_factory=QuoterConfig)
    inventory: InventoryConfig = field(default_factory=InventoryConfig)
    adverse: AdverseConfig = field(default_factory=AdverseConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    mr: MRSkewConfig = field(default_factory=MRSkewConfig)

    @classmethod
    def from_config(cls, config: dict | None) -> "MarketMakerConfig":
        """Build from the parsed app config — all of `market_making:` is now config-driven.

        The quote size is no longer a constant. `quote_size` survives ONLY as the fallback used
        when equity is unknown (it was the hardcoded 100); the live size comes from
        sizing.base_quote_size(equity, active_markets, price, depth) and is then leaned by inventory.

        What has NOT changed, and must not: size is never a function of edge / fair value /
        divergence / conviction. Edge-scaling an MM quote turns two-sided spread capture into a
        one-sided directional bet — the +453% bag-hold and the 13,144-contract blowup. Conviction
        belongs to strategies/sizing. Garbage values fall back to defaults rather than raising, so a
        bad config degrades to small-and-safe instead of refusing to quote at all.
        """
        import structlog

        mm = (config or {}).get("market_making", {}) or {}

        def _num(key, default, cast=float):
            raw = mm.get(key)
            if raw is None:
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                structlog.get_logger().warning("mm_config_unparseable", key=key, value=raw,
                                               using=default)
                return default

        fallback = max(int(_num("quote_size", QuoterConfig().size, int)), 1)
        # quote_k = half-width we TRY to rest inside the mid (clamped to the live touch); join_when_tight
        # lets MM quote AT the touch when there's no room inside (the fill-rate unlock on a tight book).
        _jwt = mm.get("join_when_tight", QuoterConfig().join_when_tight)
        quoter = replace(QuoterConfig(), size=fallback,
                         k=_num("quote_k", QuoterConfig().k),
                         join_when_tight=bool(_jwt) if isinstance(_jwt, bool) else QuoterConfig().join_when_tight)

        sizing = SizingConfig(
            mm_capital_frac=_num("mm_capital_frac", SizingConfig().mm_capital_frac),
            mm_max_quote_frac=_num("mm_max_quote_frac", SizingConfig().mm_max_quote_frac),
            mm_max_inventory_frac=_num("mm_max_inventory_frac",
                                       SizingConfig().mm_max_inventory_frac),
            max_frac_of_depth=_num("max_frac_of_depth", SizingConfig().max_frac_of_depth),
            min_quote_size=_num("min_quote_size", SizingConfig().min_quote_size, int),
        )
        inventory = InventoryConfig(
            cap=max(int(_num("max_inventory_per_market", InventoryConfig().cap, int)), 1),
            skew_ticks_at_cap=_num("skew_ticks_at_cap", InventoryConfig().skew_ticks_at_cap),
            size_skew_strength=_num("size_skew_strength", InventoryConfig().size_skew_strength),
        )
        mr = MRSkewConfig(
            enabled=bool(mm.get("mr_skew_enabled", MRSkewConfig().enabled)),
            window_seconds=_num("mr_window_seconds", MRSkewConfig().window_seconds),
            min_samples=max(int(_num("mr_min_samples", MRSkewConfig().min_samples, int)), 2),
            k_enter=_num("mr_k_enter", MRSkewConfig().k_enter),
            max_ticks=max(int(_num("mr_max_ticks", MRSkewConfig().max_ticks, int)), 0),
        )
        try:
            sizing.validate()
            inventory.validate()
        except ValueError as exc:
            # Fail SMALL, not loud: a bad MM param must not stop the trader booting, and the
            # defaults are the conservative ones, so reverting can only reduce exposure.
            structlog.get_logger().warning("mm_config_invalid_using_defaults", error=str(exc))
            sizing, inventory = SizingConfig(), InventoryConfig()
        return cls(quoter=quoter, inventory=inventory, sizing=sizing, mr=mr)


def to_contract(quote: Quote, *, side: str, strategy: str = STRATEGY_TAG) -> tuple[list[dict], list[dict]]:
    """Convert a finished Quote into (orders, cancels) in the executor's contract shape.
    A postable side becomes a resting limit order; a non-postable side becomes a cancel so
    the executor tears down any stale resting order there."""
    orders: list[dict] = []
    cancels: list[dict] = []

    if quote.post_bid and quote.bid_price is not None:
        orders.append({"exchange_id": quote.exchange_id, "side": side, "action": "buy",
                       "price": quote.bid_price, "quantity": int(quote.bid_size),
                       "order_type": "limit", "strategy": strategy})
    else:
        cancels.append({"action": "cancel", "exchange_id": quote.exchange_id,
                        "quote_side": "bid", "strategy": strategy})

    if quote.post_ask and quote.ask_price is not None:
        orders.append({"exchange_id": quote.exchange_id, "side": side, "action": "sell",
                       "price": quote.ask_price, "quantity": int(quote.ask_size),
                       "order_type": "limit", "strategy": strategy})
    else:
        cancels.append({"action": "cancel", "exchange_id": quote.exchange_id,
                        "quote_side": "ask", "strategy": strategy})

    return orders, cancels


class MarketMaker:
    """Stateful across ticks only in the ways a market-maker must be: net inventory per
    market and a short rolling mid-history for adverse-selection. Everything else is a
    pure function of the current touch."""

    def __init__(self, cfg: MarketMakerConfig | None = None, *, equity: float | None = None,
                 active_markets: int | None = None) -> None:
        self._cfg = cfg or MarketMakerConfig()
        self.inventory = InventoryBook(self._cfg.inventory)
        self.adverse = AdverseSelectionMonitor(self._cfg.adverse)
        self.mr = MeanReversionSkew(self._cfg.mr)
        # Capital context for base_quote_size. Both default to unknown, which falls back to the
        # configured quote_size — so an MM constructed without them behaves exactly as before.
        self._equity = equity
        self._active_markets = active_markets

    def set_capital(self, *, equity: float | None = None,
                    active_markets: int | None = None) -> None:
        """Refresh the capital context. The live executor calls this each cycle from the account
        snapshot and the count of markets it is actually quoting, so the per-market base tracks
        both the bankroll and how thinly it is spread."""
        if equity is not None:
            try:
                self._equity = max(float(equity), 0.0)
            except (TypeError, ValueError):
                pass
        if active_markets is not None:
            try:
                self._active_markets = max(int(active_markets), 1)
            except (TypeError, ValueError):
                pass

    # ── fills reported back by the executor (it owns the broker; we own the arithmetic) ──
    def record_fill(self, market: str, action: str, qty: float) -> int:
        return self.inventory.record_fill(market, action, qty)

    def set_position(self, market: str, qty: int) -> int:
        """Sync the held-inventory view to venue truth (absolute set). The live executor calls this
        each cycle from the reconciled positions so the ask side is bounded to what we actually
        hold — the same source the risk gate clamps sells against."""
        return self.inventory.set_position(market, qty)

    def position(self, market: str) -> int:
        return self.inventory.position(market)

    def build(self, exchange_id: str, best_bid: float | None, best_ask: float | None,
              now: float | None = None, *, book_depth: int | None = None) -> dict:
        """Synchronous core: market state → desired orders + cancels (+ structured detail).

        Note what this takes: prices, depth, time. NO edge, fair value or conviction — size is
        capital x liquidity, leaned by inventory, and nothing else.
        """
        now = time.monotonic() if now is None else now

        quote = compute_quote(exchange_id, best_bid, best_ask, self._cfg.quoter)

        # Capital/liquidity base size replaces the old constant. Priced at the MID (the quote rests
        # inside the touch, so the mid is the right capital-per-contract estimate); with no mid
        # there is no quote to size anyway and the fallback is harmless.
        base = base_quote_size(
            equity=self._equity,
            active_markets=self._active_markets or 1,
            quote_price=(quote.mid if quote.mid else 0.5),
            book_depth=book_depth,
            cfg=self._cfg.sizing,
        )
        if not base.sized:
            # No equity => no capital percentage => no quote. Declining is the correct answer; the
            # old behaviour fell back to a fixed share count, which is the bug being removed.
            quote = replace(quote, bid_size=0, ask_size=0, post_bid=False, post_ask=False,
                            reason=(quote.reason + "+no_equity" if quote.reason else "no_equity"))
        else:
            quote = replace(quote, bid_size=base.size, ask_size=base.size)

        # HARD INVENTORY CAP, also expressed in CAPITAL: mm_max_inventory_frac x equity, divided by
        # this market's price. Set per cycle so the cap tracks both the bankroll and the price —
        # a fixed share cap would bound risk 47x more loosely at 0.95 than at 0.02.
        cap_shares = inventory_cap_shares(
            equity=self._equity,
            quote_price=(quote.mid if quote.mid else 0.5),
            cfg=self._cfg.sizing,
        )
        if cap_shares is not None:
            self.inventory.set_cap(exchange_id, cap_shares)

        self.adverse.observe(exchange_id, quote.mid, now)        # feed the rolling mid history
        quote = self.inventory.apply(exchange_id, quote)          # size+price skew, cap pulls
        signal = self.adverse.evaluate(exchange_id, now)          # fast-move detection
        quote = self.adverse.apply(signal, quote)                 # pull/widen the picked-off side

        # MEAN-REVERSION ALPHA SKEW (maker-only, bounded). Tilt the whole quote center by a few ticks
        # toward the expected reversion, but keep BOTH sides strictly inside the live touch so we never
        # cross (still a maker). The ask stays inventory-bounded upstream, so this can't open a short.
        self.mr.observe(exchange_id, quote.mid, now)
        mr_tilt, mr_z = self.mr.tilt_ticks(exchange_id, now)
        if (mr_tilt and best_bid is not None and best_ask is not None
                and quote.bid_price is not None and quote.ask_price is not None):
            tick = self._cfg.quoter.tick
            lo, hi = self._cfg.quoter.price_min, self._cfg.quoter.price_max
            band_lo = round_to_tick(best_bid + tick, tick=tick, lo=lo, hi=hi)
            band_hi = round_to_tick(best_ask - tick, tick=tick, lo=lo, hi=hi)
            if band_hi - band_lo >= tick - 1e-9:
                nb = round_to_tick(quote.bid_price + mr_tilt * tick, tick=tick, lo=lo, hi=hi)
                na = round_to_tick(quote.ask_price + mr_tilt * tick, tick=tick, lo=lo, hi=hi)
                nb = min(max(nb, band_lo), round(band_hi - tick, 3))
                na = max(min(na, band_hi), round(nb + tick, 3))
                quote = replace(quote, bid_price=nb, ask_price=na,
                                reason=(f"{quote.reason}+mr:z{mr_z}t{mr_tilt}" if quote.reason
                                        else f"mr:z{mr_z}t{mr_tilt}"))

        orders, cancels = to_contract(quote, side=self._cfg.quoter.side)
        return {"exchange_id": exchange_id, "orders": orders, "cancels": cancels,
                "quote": quote, "adverse": signal, "position": self.position(exchange_id),
                "base_size": base.to_dict(), "skew": self.inventory.skew(exchange_id),
                "inventory_cap": self.inventory.cap(exchange_id),
                "mr_tilt": mr_tilt, "mr_z": mr_z}

    async def on_market_state(self, exchange_id: str, best_bid: float | None,
                              best_ask: float | None, now: float | None = None,
                              *, book_depth: int | None = None) -> dict:
        """Async entrypoint for the executor's event loop (non-blocking — no I/O inside)."""
        return self.build(exchange_id, best_bid, best_ask, now, book_depth=book_depth)
