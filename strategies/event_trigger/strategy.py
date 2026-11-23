"""EventTriggerStrategy — routes the all-237 SIG-abnormal-move × GDELT-news-volume edge into the
orchestrator as directional Intents, FLAG-GATED and observable.

Pipeline (all read-only data, injected so this stays decoupled + testable):
  1. `abnormal_move_provider()` → the SIG abnormal moves this cycle (signals/abnormal_move.scan_markets,
     all 237 markets off GET /exchanges/{id}/price-history).
  2. For each move: `news_provider(race_key)` → GDELT news volume (signals/news_volume), and
     `sharp_provider(exchange_id)` → a Polymarket/ratings sharp price if one exists.
  3. `signals/event_trigger.classify_event` → FOLLOW (news-confirmed) or FADE (overdone), a side, and a
     confidence→sigma_p. Direction is from the move + justified/overdone, NEVER from news content.
  4. Emit a directional Intent (sigma_p in metadata) → the EXISTING Browne sizer + risk gate + OMS size
     and place it; this strategy never sizes or places directly. Coarse edge ⇒ TAIL tier, wide sigma.

SAFETY. `enabled=False` by default (the flag gate) — `evaluate` returns [] until a human flips it, so
deploying this is inert. Every decision is appended to `self.events` and logged, so each abnormal move,
whether it was news-confirmed, the follow/fade call, and the resulting signal are all auditable.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import structlog

from sig.engine.protocols import EdgePerf, Intent
from signals.event_trigger import EventTriggerConfig, classify_event

log = structlog.get_logger()

STRATEGY_ID = "event_trigger"


class EventTriggerStrategy:
    """Orchestrator Strategy-protocol implementation. Providers are injected so the SIG price-history
    scan + the rate-limited GDELT sweep + the sharp-price lookup are decoupled from this sync evaluate():

      • abnormal_move_provider()                 -> list[AbnormalMove]          (SIG moves this cycle, all 237)
      • news_provider(race_key)                  -> NewsVolume | None           (GDELT volume for that race)
      • sharp_provider(exchange_id)              -> float | None                (Polymarket/ratings sharp P, if any)
      • sig_price_provider(exchange_id)          -> (yes_price, age_s) | None   (SIG price for the intent)
      • exchange_to_race_key(exchange_id)        -> race_key | None             (map a market to its race)
    """
    tier = "TAIL"

    def __init__(
        self,
        *,
        abnormal_move_provider: Callable[[], Any],
        news_provider: Callable[[str], Any],
        sig_price_provider: Callable[[str], Any],
        exchange_to_race_key: Callable[[str], Any],
        sharp_provider: Callable[[str], Any] | None = None,
        config: EventTriggerConfig | None = None,
        cadence_s: float = 300.0,            # SIGNAL/TAIL cadence — this is a slow structural trigger
        name: str = STRATEGY_ID,
    ) -> None:
        self.name = name
        self._moves = abnormal_move_provider
        self._news = news_provider
        self._sharp = sharp_provider or (lambda _ex: None)
        self._sig_price = sig_price_provider
        self._race_key = exchange_to_race_key
        self._cfg = config or EventTriggerConfig()
        self.tier = self._cfg.tier or "TAIL"
        self.cadence_s = cadence_s
        self.events: list[dict] = []          # observable audit of every abnormal move + its disposition
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
        if not self._cfg.enabled:
            return []                          # FLAG GATE — inert until explicitly enabled
        try:
            moves = self._moves() or []
        except Exception as exc:  # noqa: BLE001 — a provider failure must not crash the cycle
            log.warning("event_trigger_move_provider_failed", error=type(exc).__name__)
            return []

        out: list[Intent] = []
        for mv in moves:
            try:
                ex = str(getattr(mv, "exchange_id", "") or "")
                if not ex:
                    continue
                race_key = self._race_key(ex)
                news = self._news(race_key) if race_key else None
                sharp = self._sharp(ex)
                sig = self._sig_price(ex)
                sig_price = float(sig[0]) if sig else None

                signal = classify_event(mv, news, sharp_p=sharp, cfg=self._cfg)
                rec = {
                    "exchange_id": ex, "race": race_key, "move_dir": mv.direction,
                    "move_ratio": mv.move_ratio, "tradecount_spike": mv.tradecount_spike,
                    "reverting": mv.reverting,
                    "news_spike": bool(news is not None and getattr(news, "spike", False)),
                    "disposition": (signal.mode if signal else "none"),
                    "side": (signal.side if signal else None),
                    "confidence": (signal.confidence if signal else 0.0),
                    "ts": getattr(mv, "timestamp", "") or time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                self.events.append(rec)
                log.info("event_trigger_abnormal_move", **rec)

                if signal is None:
                    continue
                out.append(Intent(
                    strategy_id=self.name, exchange_id=ex, side=signal.side, action="open",
                    strategy_type="directional", signal_strength=round(signal.confidence, 4),
                    price=sig_price,
                    metadata={
                        "sigma_p": round(signal.sigma_p, 4), "mode": signal.mode,
                        "move_direction": signal.move_direction, "move_ratio": signal.move_ratio,
                        "news_spike": signal.news_spike, "reverting": signal.reverting,
                        "sharp_checked": signal.sharp_checked, "reasons": list(signal.reasons),
                        "edge": "event_trigger", "race": race_key, "tier": self.tier,
                    },
                ))
            except Exception as exc:  # noqa: BLE001 — isolate per-move failures
                log.warning("event_trigger_move_failed",
                            exchange=str(getattr(mv, "exchange_id", "?")), error=type(exc).__name__)
                continue
        return out
