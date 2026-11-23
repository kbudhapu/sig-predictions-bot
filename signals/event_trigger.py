"""Justified-vs-overdone combiner (PART 3) — turns a SIG abnormal move + GDELT news volume into a
directional FOLLOW/FADE signal, WITHOUT ever classifying news content.

The whole point: direction comes from the move + the justified/overdone discriminator, never from
reading whether news "helps R or D" (the error-prone trap).

  • abnormal move + news spike on the SAME race  → JUSTIFIED (real info; SIG is repricing) → FOLLOW
        (trade WITH the move — up ⇒ buy, down ⇒ sell).
  • abnormal move + NO news spike (esp. a reverting spike) → OVERDONE (students overreacting) → FADE
        (trade AGAINST — up ⇒ sell, down ⇒ buy).

Confidence scales with how large the move is vs the market's baseline, a trade-count burst, the news
magnitude (follow) or the revert tell (fade), and — where a sharp price exists (Polymarket/ratings) —
whether that sharp source CONFIRMS (follow: sharp sits beyond SIG in the move's direction; fade: SIG
has overshot past the sharp price). Confidence maps to `sigma_p` for the Browne sizer; this edge is
COARSE, so the sigma band is wide (small bets) and sub-threshold signals are dropped. Pure: inputs in,
EventSignal out."""
from __future__ import annotations

from dataclasses import dataclass, field

from .abnormal_move import AbnormalMove
from .conviction import conviction
from .news_volume import NewsVolume


@dataclass(frozen=True)
class EventTriggerConfig:
    enabled: bool = False                # FLAG GATE — default OFF; nothing fires until explicitly enabled
    tier: str = "TAIL"                   # coarse edge → TAIL by default (smallest budget); may be SIGNAL
    min_confidence: float = 0.15         # below this, emit nothing
    move_ratio_cap: float = 6.0          # move_ratio at/above which the move term saturates
    news_ratio_cap: float = 6.0          # news spike ratio at/above which the news term saturates
    tradecount_bonus: float = 1.2        # multiply confidence when a trade-count burst co-fires
    revert_bonus: float = 1.3            # multiply fade confidence when the move is reverting
    sharp_confirm_bonus: float = 1.25    # sharp source confirms the follow/fade
    sharp_contradict_penalty: float = 0.5  # sharp source contradicts → dampen hard
    # sizing (wider/ higher sigma than real-money edges — this trigger is fuzzy)
    min_sigma: float = 0.10
    max_sigma: float = 0.30


@dataclass(frozen=True)
class EventSignal:
    exchange_id: str
    mode: str                            # "follow" | "fade"
    side: str                            # "buy" | "sell"
    confidence: float
    sigma_p: float
    move_direction: str                  # "up" | "down"
    move_ratio: float
    news_spike: bool
    reverting: bool
    sharp_checked: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def classify_event(
    move: AbnormalMove,
    news: NewsVolume | None,
    *,
    sharp_p: float | None = None,
    benchmark_quality: str | float = "sharp",
    cfg: EventTriggerConfig | None = None,
) -> EventSignal | None:
    """Combine one race's abnormal move + its news volume (+ optional sharp price) into a FOLLOW/FADE
    EventSignal, or None if sub-threshold / gated out. Direction is from the move + justified/overdone —
    NOT from news content."""
    cfg = cfg or EventTriggerConfig()
    news_spike = bool(news is not None and news.spike)
    up = move.direction == "up"
    reasons: list[str] = list(move.reasons)

    # ── justified vs overdone ──
    if news_spike:
        mode = "follow"                              # real info → trade WITH the move
        reasons.append("news_spike")
    else:
        mode = "fade"                                # no corroborating news → students overreacting
        reasons.append("no_news")
        if move.reverting:
            reasons.append("reverting")

    side = ("buy" if up else "sell") if mode == "follow" else ("sell" if up else "buy")

    # ── confidence ──
    conf = _clamp(move.move_ratio / cfg.move_ratio_cap, 0.0, 1.0)   # how far beyond normal wobble
    if move.tradecount_spike:
        conf *= cfg.tradecount_bonus
        reasons.append("tradecount_spike")
    if mode == "follow" and news is not None:
        news_term = 0.6 + 0.4 * _clamp(news.ratio / cfg.news_ratio_cap, 0.0, 1.0)
        conf *= news_term
    if mode == "fade" and move.reverting:
        conf *= cfg.revert_bonus

    # ── sharp cross-check (Polymarket / ratings), where one exists — SCALED BY CONVICTION ──
    # The benchmark only earns a say in proportion to its conviction (distance-from-0.5 × quality): an
    # extreme-sure sharp price confirms/contradicts at full strength, a mushy mid-range or coarse one
    # barely moves the confidence. So we fade/follow HARD against a confident benchmark, weakly against
    # a mushy one — the task's conviction-scaling, read at the benchmark's own level.
    sharp_checked = False
    if sharp_p is not None:
        sharp_checked = True
        conv = conviction(sharp_p, benchmark_quality)
        if mode == "follow":
            confirmed = (sharp_p > move.last_close) if up else (sharp_p < move.last_close)
        else:  # fade — confirmed when SIG has OVERSHOT past the sharp price
            confirmed = (move.last_close > sharp_p) if up else (move.last_close < sharp_p)
        if confirmed:
            conf *= 1.0 + (cfg.sharp_confirm_bonus - 1.0) * conv    # confident → full bonus; mushy → ~1
        else:
            conf *= 1.0 - (1.0 - cfg.sharp_contradict_penalty) * conv  # confident → full penalty; mushy → ~1
        reasons.append(f"sharp_{'confirm' if confirmed else 'contradict'}_conv{conv:.2f}")

    conf = _clamp(conf, 0.0, 1.0)
    if conf < cfg.min_confidence:
        return None

    sigma_p = _clamp(cfg.max_sigma - conf * (cfg.max_sigma - cfg.min_sigma), cfg.min_sigma, cfg.max_sigma)
    return EventSignal(
        exchange_id=move.exchange_id, mode=mode, side=side,
        confidence=round(conf, 4), sigma_p=round(sigma_p, 4),
        move_direction=move.direction, move_ratio=move.move_ratio,
        news_spike=news_spike, reverting=move.reverting, sharp_checked=sharp_checked,
        reasons=tuple(reasons),
    )
