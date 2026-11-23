"""ELECTION-NIGHT race-call play — pure logic (no I/O). Dry-run shadow first (main_sig._election_night_loop).

Research (tools/research/election_night/, Polymarket 2024 state races, 10-min paths): most races are decided before
polls close; 10-14 of 45 were decided on the night (median ~3.5-4h after 6pm ET, all within ~14h). Two LOSERS traded
92.5% / 94% after polls closed (WI-Sen, MI-Sen) -> a 90% trigger loses money; no loser reached 95%. Buying the first
leg to cross 95% on a venue lagging Polymarket by 30-60 min was worth ~+$12-20k at $3k/race scaled to SIG's races.

Rule per race (D and R legs), evaluated repeatedly:
  ARMED  a leg is armed once seen at or below theta - arm_margin within the last arm_window_s (so safe seats that sit
         at 99% all along never fire; only a CROSSING from below counts).
  FIRE   an armed leg with Polymarket >= theta, Kalshi >= theta when Kalshi has a price (agreement guard),
         and SIG ask <= Polymarket - min_edge  -> buy that leg: limit = min(ask + max_sweep, poly - min_edge).
  ONCE   each race fires at most once.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ElectionNightConfig:
    theta: float = 0.95
    arm_margin: float = 0.02
    arm_window_s: float = 6 * 3600.0
    min_edge: float = 0.01
    max_sweep: float = 0.02
    usd_per_race: float = 2000.0
    require_kalshi_agreement: bool = True


@dataclass
class NightState:
    last_low: dict = field(default_factory=dict)     # exchange_id -> epoch last seen <= theta - arm_margin
    fired: set = field(default_factory=set)          # races already triggered


def step(legs: list[dict], state: NightState, cfg: ElectionNightConfig, now: float) -> list[dict]:
    """legs: {race, exchange_id, poly, kalshi (or None), sig_bid, sig_ask, ask_size}. Returns triggers (dicts)."""
    out = []
    for l in legs:
        p = l.get("poly")
        if p is None:
            continue
        if p <= cfg.theta - cfg.arm_margin:
            state.last_low[l["exchange_id"]] = now
    for l in sorted(legs, key=lambda x: -(x.get("poly") or 0.0)):
        race, ex, p, k, ask = l["race"], l["exchange_id"], l.get("poly"), l.get("kalshi"), l.get("sig_ask")
        if race in state.fired or p is None or ask is None or p < cfg.theta:
            continue
        armed_at = state.last_low.get(ex)
        if armed_at is None or now - armed_at > cfg.arm_window_s:
            continue
        if k is not None and k < cfg.theta:
            continue
        if k is None and cfg.require_kalshi_agreement and l.get("kalshi_mapped"):
            continue                                  # mapped on Kalshi but no fresh quote: don't fire blind
        if ask > p - cfg.min_edge:
            continue                                  # SIG already caught up: nothing to take
        limit = round(min(ask + cfg.max_sweep, p - cfg.min_edge), 3)
        state.fired.add(race)
        out.append({"race": race, "exchange_id": ex, "poly": round(p, 4), "kalshi": k, "sig_bid": l.get("sig_bid"),
                    "sig_ask": ask, "ask_size": l.get("ask_size"), "limit": limit,
                    "shares": round(cfg.usd_per_race / max(limit, 0.01), 1), "edge_c": round(100 * (p - limit), 2)})
    return out
