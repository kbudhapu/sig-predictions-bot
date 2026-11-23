"""Cross-market divergence strategy — detection → sizing → execution, with the right discipline.

THE EDGE (governs every choice): we READ the sharp external price (Polymarket primary, Kalshi
cross-check) — which is real-money and already correct — and trade SIG's STUDENT price before its
students catch up. We are FASTER than SIG's slow human correctors; we are NOT smarter than the sharp
market. So we size by HOW STALE SIG IS vs the sharp price (the capturable lag), NEVER by forecasting
where the sharp price will go. There is deliberately NO term here that predicts the external price's
future move.

Routing: a SIGNAL-tier Strategy (orchestrator Strategy protocol). Each cycle it emits directional
Intents toward the external price; the EXISTING Browne/directional sizer + risk gate + OMS size and
place them. Size is carried as `sigma_p` (confidence the sharp price is right) in the intent metadata
— lower sigma ⇒ bigger Browne bet — plus `signal_strength`.

Safety (in priority order):
  1. DORMANT-SAFE: only trades RaceMapping.is_active() races (verified confidence=="high" AND
     resolution_match AND a liquid+usable external). The real mapping is all-dormant until verified,
     so the engine emits nothing until a mapping is price-verified. A WRONG mapping is worse than none.
  2. POLARITY (single flip, delegated): inverted external legs are aligned to SIG's YES (1 − p) by the
     ONE flip owner — signals/cross_market.resolve_external_prices — which this strategy CALLS. This
     layer NEVER flips itself: a second flip would cancel the resolver's and silently invert the signal
     on inverted-polarity races (the double-flip bug). signals/divergence.py likewise does pure math on
     already-SIG-aligned prices and does not flip. Exactly one layer flips, and it is the resolver.
  3. SKEPTICISM CURVE (the key safety): an implausibly large gap is far more likely OUR mapping bug /
     a stale-illiquid external than free money. Size GROWS with the gap up to a plausible band, then
     SHRINKS, then REFUSES beyond an implausibility threshold — logged loudly as a mapping-audit signal.
  4. EXTERNAL STALENESS: a frozen/stale external price makes the "divergence" illusory → skip.
  5. VENUE DISAGREEMENT: Polymarket and Kalshi disagreeing sharply = genuine uncertainty → shrink size.
  6. ELECTION-NIGHT OVERLAY (flag-gated, off by default): when the external price says a race is
     DECIDED (≥ called_hi / ≤ called_lo), fair value snaps to ~0.99/0.01 — the highest-confidence,
     near-boolean signal → max conviction (and the ONLY sanctioned huge-gap case: skepticism bypassed).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import structlog

from sig.engine.protocols import Intent, EdgePerf
from signals.divergence import assess_divergence, ConfidenceTier
from signals.cross_market import resolve_external_prices
from signals.conviction import conviction as _conviction, effective_threshold as _eff_threshold

log = structlog.get_logger()

STRATEGY_ID = "cross_market_divergence"


@dataclass(frozen=True)
class ExternalQuote:
    """One external venue's latest YES quote for a SIG market, in the VENUE's native polarity."""
    venue: str
    yes_price: float
    age_s: float


@dataclass(frozen=True)
class DivergenceConfig:
    cadence_s: float = 30.0            # SIGNAL-tier cadence (not every cycle). PLACEHOLDER — tune at open.
    min_divergence: float = 0.03       # below this gap there is no material edge
    target_gap: float = 0.20           # gap at which size saturates (gap_factor → 1); capture scales to here
    # skepticism curve: full size ≤ plausible; linear shrink to 0 at implausible; REFUSE beyond.
    skepticism_plausible: float = 0.25
    skepticism_implausible: float = 0.40
    external_stale_s: float = 30.0     # external price older than this = stale → not a comparable
    # SIG-side staleness tolerance. Cross-market is a SLOW structural edge: it trades SIG's student price
    # toward the FRESH sharp benchmark, so a SIG price older than the global fast-edge stale gate (15s) is
    # still a valid comparable — the lag IS the edge. We accept a SIG price up to this age (the sharp side
    # stays fresh via the poller; the order is a protective LIMIT at the SIG mid). Beyond this, SIG is too
    # old to trust even for a slow edge → drop the race. Intents within tolerance set allow_stale_market so
    # the orchestrator's 15s gate (built for MM/arb) does not kill them.
    sig_stale_s: float = 300.0
    tick: float = 0.005                # venue price-grid tick; the limit price is snapped to it (the risk
                                       # gate rejects an off-grid price). Keep in sync with risk.tick.
    price_min: float = 0.005
    price_max: float = 0.995
    disagree_tol: float = 0.05         # |poly − kalshi| above this = disagreement → shrink
    # confidence → sigma_p (for the Browne sizer): high confidence → low sigma → bigger bet.
    min_sigma: float = 0.03
    max_sigma: float = 0.25
    single_source_conf: float = 0.7    # one venue only → decent but less sure than two agreeing
    # benchmark-CONVICTION weighting (flag-gated OFF → unchanged live behaviour). When ON: the divergence
    # threshold becomes cost_floor / conviction(external_fair) — a CONFIDENT benchmark (price far from
    # 0.5) lets a SMALL gap through, a mushy near-0.5 one needs a BIG gap — always floored at cost so
    # conviction can never admit a sub-cost gap; and size scales by conviction (gap × conviction × Browne).
    conviction_weighting: bool = False
    conviction_cost_floor: float = 0.02
    # MAKER ENTRIES (2026-10-08): "maker" rests the entry at the best bid of the side being bought (YES: best_bid;
    # NO: 1 - best_ask) with no chase, cancelled after maker_entry_timeout_s; no book => no entry (never falls back
    # to crossing). "taker" = the original mid-limit + OMS chase. Shadow: maker +0.82 vs taker -0.04 per $100.
    entry_mode: str = "taker"
    maker_entry_timeout_s: float = 60.0      # ~ the 1-minute fill window the maker shadow validated
    # election-night race-call overlay (flag-gated OFF by default)
    election_night_mode: bool = False
    called_hi: float = 0.97            # external ≥ this ⇒ market considers it decided (YES)
    called_lo: float = 0.03            # external ≤ this ⇒ decided (NO)
    called_sigma: float = 0.03         # max-conviction sizing for a called race (Browne endgame-timed)


def leg_type(option_side: str, sig_yes_price: float) -> str:
    """Which leg of the race a buy is: the side we buy × whether this market's YES is the underdog (< 0.5).
    longshot-NO = buy NO on an underdog; fav-YES = buy YES on a favourite; fav-NO / longshot-YES the rest.
    Tag only (2026-10-06 leg-split research) — it does not change any decision."""
    underdog = sig_yes_price < 0.5
    if option_side == "no":
        return "longshot-NO" if underdog else "fav-NO"
    return "longshot-YES" if underdog else "fav-YES"


def skepticism_multiplier(divergence: float, cfg: DivergenceConfig) -> float:
    """1.0 in the plausible band; linear shrink to 0 at the implausible threshold; 0 (REFUSE) beyond.
    A 0.45 gap past `skepticism_implausible` returns 0 — 'check the mapping', not 'bet huge'."""
    d = abs(divergence)
    if d <= cfg.skepticism_plausible:
        return 1.0
    if d >= cfg.skepticism_implausible:
        return 0.0
    span = cfg.skepticism_implausible - cfg.skepticism_plausible
    return max(0.0, (cfg.skepticism_implausible - d) / span) if span > 0 else 0.0


_TIER_BASE = {
    ConfidenceTier.HIGH: 1.0, ConfidenceTier.MEDIUM: 0.6,
    ConfidenceTier.LOW: 0.4, ConfidenceTier.SUSPECT: 0.2,
}

#: Benchmark QUALITY by primary venue → the conviction quality factor. Real-money venues are SHARP
#: (~1.0); Wikipedia ratings are COARSE (`rating_derived` ≈ 0.5), so conviction weighting makes a
#: Wikipedia-benchmarked race need a BIGGER gap to clear the (cost-floored) threshold — "trust it less".
_VENUE_QUALITY = {"polymarket": "sharp", "kalshi": "sharp", "wikipedia": "rating_derived"}


class CrossMarketDivergenceStrategy:
    """Strategy-protocol implementation. Providers are injected so the async price reads (SIG state +
    the signals Polymarket/Kalshi loop) are decoupled from this sync evaluate():
      • sig_price_provider(sig_exchange_id)      -> (yes_price, age_s) | None
      • external_provider(sig_exchange_id)       -> {venue: ExternalQuote} | None
            RAW venue-native YES prices (NOT yet polarity-aligned). evaluate() aligns them via
            signals/cross_market.resolve_external_prices — the ONE flip point — and never flips here.
    """
    tier = "SIGNAL"

    def __init__(self, *, mapping: dict, sig_price_provider: Callable[[str], Any],
                 external_provider: Callable[[str], Any], config: DivergenceConfig | None = None,
                 name: str = STRATEGY_ID, sig_book_provider: Callable[[str], Any] | None = None) -> None:
        self.name = name
        self._sig_book = sig_book_provider      # (best_bid, best_ask) | None — maker entries only
        self._mapping = mapping
        self._sig_price = sig_price_provider
        self._external = external_provider
        self._cfg = config or DivergenceConfig()
        self.cadence_s = self._cfg.cadence_s
        self.audit_flags: list[dict] = []     # mapping-audit events (implausible gaps) — inspectable in tests
        self._realized_pnl = 0.0
        self._n_trades = 0

    # ── Strategy protocol ──
    def on_fill(self, fill: Any) -> None:
        self._n_trades += 1
        pnl = getattr(fill, "realized_pnl", None)
        if pnl is None and isinstance(fill, dict):
            pnl = fill.get("realized_pnl")
        if pnl is not None:
            self._realized_pnl += float(pnl)

    def realized_performance(self) -> EdgePerf:
        return EdgePerf(realized_pnl=self._realized_pnl, n_trades=self._n_trades)

    def evaluate(self, state: Any) -> list[Intent]:
        out: list[Intent] = []
        for key, rm in self._mapping.items():
            # (1) DORMANT-SAFE gate: verified high-confidence + resolution match + a liquid external.
            if not rm.is_active():
                continue
            sig_ex = rm.sig_exchange_id
            if sig_ex is None:
                continue
            sigq = self._sig_price(sig_ex)
            if sigq is None:
                continue
            sig_price, _sig_age = float(sigq[0]), float(sigq[1])
            # SIG-side staleness: these election markets update far slower than the global 15s fast-edge
            # gate, so a few-minutes-old SIG price is still a valid comparable for this slow edge. Drop only
            # a TRULY ancient price (beyond sig_stale_s); otherwise mark the intent (below) so the
            # orchestrator's fast-edge stale gate does not kill it.
            if _sig_age > self._cfg.sig_stale_s:
                continue
            quotes = self._external(sig_ex) or {}

            # (4) EXTERNAL STALENESS is enforced HERE (a frozen price is not a comparable): a stale or
            # missing venue is passed as None so the resolver drops it. (2) POLARITY is applied EXACTLY
            # ONCE — and only — by signals/cross_market.resolve_external_prices, which this layer CALLS.
            # This strategy NEVER flips: a second flip here would cancel the resolver's flip and silently
            # invert the signal on inverted-polarity races (the double-flip bug this contract forbids).
            raw_external: dict[str, float | None] = {}
            for venue, _ref in rm.usable_externals().items():
                q = quotes.get(venue)
                raw_external[venue] = (None if (q is None or q.age_s > self._cfg.external_stale_s)
                                       else float(q.yes_price))
            aligned = resolve_external_prices(rm, raw_external)   # SIG-aligned: the ONE flip + the gate
            if not aligned:
                continue                               # all external legs stale/missing/dormant → no comparable

            primary = "polymarket" if "polymarket" in aligned else next(iter(aligned))
            external_fair = aligned[primary]

            # (6) ELECTION-NIGHT race-call overlay (flag-gated). A decided external price is the ONE
            # sanctioned huge-gap case → snap to ~0.99/0.01, bypass the skepticism curve, max conviction.
            called = False
            if self._cfg.election_night_mode:
                if external_fair >= self._cfg.called_hi:
                    external_fair, called = 0.99, True
                elif external_fair <= self._cfg.called_lo:
                    external_fair, called = 0.01, True

            divergence = external_fair - sig_price
            # Threshold: the flat min_divergence, OR — when conviction_weighting is ON — a
            # conviction-dependent one: cost_floor / conviction(external_fair). A confident benchmark
            # (price far from 0.5) admits a SMALL gap; a mushy near-0.5 one needs a BIG gap. Always
            # floored at cost (conviction can never admit a sub-cost gap). conv==1.0 when the flag is off.
            quality = _VENUE_QUALITY.get(primary, "sharp")   # sharp (Polymarket) vs coarse (Wikipedia ratings)
            if self._cfg.conviction_weighting:
                conv = _conviction(external_fair, quality)
                threshold = _eff_threshold(conv, cost_floor=self._cfg.conviction_cost_floor)
            else:
                conv, threshold = 1.0, self._cfg.min_divergence
            if abs(divergence) < threshold:
                continue

            # (3) SKEPTICISM CURVE — a called race bypasses it (its huge gap is real, not a bug).
            skep = 1.0 if called else skepticism_multiplier(divergence, self._cfg)
            if skep <= 0.0:
                self._flag_mapping_audit(key, sig_ex, sig_price, external_fair, divergence, aligned)
                continue                               # REFUSE — mapping-audit signal, not a bet

            # (5) VENUE DISAGREEMENT → shrink confidence.
            if len(aligned) >= 2:
                spread = max(aligned.values()) - min(aligned.values())
                agreement_conf = (1.0 if spread <= self._cfg.disagree_tol
                                  else max(0.2, 1.0 - (spread - self._cfg.disagree_tol) / self._cfg.disagree_tol))
            else:
                agreement_conf = self._cfg.single_source_conf

            # Confidence = how far SIG is from sharp (gap_factor) × confidence sharp is right
            # (agreement) × skepticism. NO forecast of the external's future move.
            gap_factor = min(abs(divergence) / self._cfg.target_gap, 1.0)
            tier = assess_divergence(external_fair, sig_price, real_money_prices=aligned).confidence_tier
            if called:
                conf = 1.0                             # near-boolean, highest confidence
            else:
                conf = _TIER_BASE.get(tier, 0.4) * agreement_conf * gap_factor * skep * conv
            if conf <= 0.0:
                continue

            sigma_p = (self._cfg.called_sigma if called
                       else self._clamp_sigma(self._cfg.max_sigma - conf * (self._cfg.max_sigma - self._cfg.min_sigma)))
            # buy-NO ≡ sell-YES: express EVERY view as a BUY, never a naked SELL (the gate rejects a sell
            # without holding). SIG YES UNDERPRICED (div>0) ⇒ BUY YES (long). SIG YES OVERPRICED (div<0) ⇒
            # BUY NO (≡ short YES) — this unlocks the overpriced side of every race, which used to be dropped.
            long_yes = divergence > 0
            option_side = "yes" if long_yes else "no"
            # Limit on the leg we actually buy: the YES price for a YES buy, the NO price (= 1 − YES) for a NO
            # buy. Snapped to the tick grid, price-improving (a buy rounds DOWN) — the gate rejects off-tick.
            limit_price = self._snap_to_tick(sig_price if long_yes else (1.0 - sig_price), "buy")
            maker = str(self._cfg.entry_mode).lower() == "maker"
            if maker:
                book = self._sig_book(sig_ex) if self._sig_book is not None else None
                try:
                    bb, ba = float(book[0]), float(book[1])
                except (TypeError, ValueError, IndexError):
                    continue                           # no live book: skip rather than cross the spread
                if not (0.0 < bb < ba < 1.0):
                    continue
                join = bb if long_yes else (1.0 - ba)  # best bid of the leg we buy
                # the touch is already on the tick grid: ROUND (floor without an epsilon dropped valid prices a
                # tick — 0.47 -> 0.465 — which would sit behind the touch and fill less than the shadow measured)
                limit_price = round(round(join / self._cfg.tick) * self._cfg.tick, 3)
                if not (self._cfg.price_min <= limit_price <= self._cfg.price_max):
                    continue
            out.append(Intent(
                strategy_id=self.name, exchange_id=sig_ex, side="buy", action="open",
                strategy_type="directional", signal_strength=round(conf, 4), price=limit_price,
                metadata={
                    "option_side": option_side,   # yes = long YES; no = buy NO (short YES)
                    "leg_type": leg_type(option_side, sig_price),
                    "sigma_p": round(sigma_p, 4), "divergence": round(divergence, 4),
                    "conviction": round(conv, 4), "eff_threshold": round(threshold, 4),
                    "benchmark_quality": quality,
                    "external_fair": round(external_fair, 4), "sig_price": round(sig_price, 4),
                    "limit_price": limit_price,
                    # the OMS sweep runs BEFORE the strategy refreshes a still-wanted maker order each cycle, so the
                    # timeout must comfortably exceed the strategy cadence or a wanted order is cancelled anyway
                    "maker_entry": maker, "maker_timeout_s": max(float(self._cfg.maker_entry_timeout_s),
                                                                 2.5 * float(self._cfg.cadence_s)),
                    "primary_venue": primary, "venues": {v: round(p, 4) for v, p in aligned.items()},
                    "called": called, "skepticism": round(skep, 4),
                    "agreement_conf": round(agreement_conf, 4), "confidence_tier": tier.value,
                    "edge": "cross_market_divergence", "race": key,
                    # Slow-edge opt-out of the orchestrator's 15s fast-edge stale gate: the SIG price is
                    # within sig_stale_s (checked above), the sharp side is fresh, and the order is a
                    # protective limit at the SIG mid. MM/arb never set this, so their gate is unchanged.
                    "allow_stale_market": True, "sig_age_s": round(_sig_age, 1),
                },
            ))
        return out

    def _clamp_sigma(self, s: float) -> float:
        return max(self._cfg.min_sigma, min(self._cfg.max_sigma, s))

    def _snap_to_tick(self, price: float, side: str) -> float:
        """Snap a limit price onto the venue tick grid, price-improving: BUY floors, SELL ceils (never
        crosses further into the book). Clamped to the valid price band and de-dusted to 3 dp."""
        tick = self._cfg.tick or 0.005
        steps = price / tick
        snapped = (math.floor(steps) if side == "buy" else math.ceil(steps)) * tick
        snapped = max(self._cfg.price_min, min(self._cfg.price_max, snapped))
        return round(snapped, 3)

    def _flag_mapping_audit(self, key, sig_ex, sig_price, external_fair, divergence, aligned) -> None:
        rec = {"race": key, "sig_exchange_id": sig_ex, "sig_price": sig_price,
               "external_fair": external_fair, "divergence": round(divergence, 4), "venues": aligned}
        self.audit_flags.append(rec)
        log.error("cross_market_divergence_IMPLAUSIBLE_GAP_mapping_audit",
                  reason="divergence beyond skepticism threshold — likely a mapping/polarity error or a "
                         "stale/illiquid external price, NOT free money. REFUSED. Audit the mapping.",
                  **rec)
