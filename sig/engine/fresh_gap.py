"""FRESH-DISLOCATION REVERSION — pure decision logic (no I/O). Shadow first (main_sig._fresh_gap_loop records only).

Research (tools/research/fresh_gap_bt.py, journal 2026-10-09): when SIG's mid moves >= th_move AWAY from Polymarket
within 24h and the gap to Poly is now >= th_gap, the move partially snaps back within 1-2 days (move>=3c: +1.11c/share
net of spread, 75% hit, n=44; >=4c +1.30c; toward-Poly moves do NOT revert). OLD gaps don't close — freshness is the
whole signal, so this trades the snap-back and EXITS (recycles capital), unlike cross which holds to settlement.

ENTRY  |mid - mid_24h| >= th_move, sign(mid - mid_24h) == -sign(gap_24h)  (moved AWAY from Poly),
       |gap| >= th_gap, sign(gap) == sign(gap_24h)  (still on the same side), spread <= max_spread.
       Trade TOWARD Poly: gap > 0 -> buy YES; gap < 0 -> buy NO. One open trade per market.
EXIT   |gap| <= exit_frac * entry_gap (half closed)  or  age >= max_hold_s  or  gap flipped sign.
Prices recorded both ways: TAKER (cross the touch in and out) and MAKER (join the touch in and out) so the validator
can score both. All prices are held-side (YES price for a YES trade, NO price = 1 - YES for a NO trade).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FreshGapConfig:
    th_move: float = 0.03
    th_gap: float = 0.02
    exit_frac: float = 0.5
    max_hold_s: float = 48 * 3600.0
    max_spread: float = 0.02
    size_usd: float = 2500.0


@dataclass(frozen=True)
class Row:
    exchange_id: str
    sharp: float          # SIG-aligned Poly YES prob, now
    bid: float            # SIG YES best bid, now
    ask: float            # SIG YES best ask, now
    mid_24h: float        # SIG YES mid ~24h ago
    sharp_24h: float      # SIG-aligned Poly ~24h ago
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def gap(self) -> float:
        return self.sharp - self.mid

    @property
    def gap_24h(self) -> float:
        return self.sharp_24h - self.mid_24h


def held_prices(side: str, bid: float, ask: float) -> dict:
    """Held-side prices: taker entry/exit cross the touch, maker entry/exit join it."""
    if side == "yes":
        return {"taker_in": ask, "maker_in": bid, "taker_out": bid, "maker_out": ask}
    return {"taker_in": 1.0 - bid, "maker_in": 1.0 - ask, "taker_out": 1.0 - ask, "maker_out": 1.0 - bid}


def entry_signal(r: Row, cfg: FreshGapConfig) -> str | None:
    """'yes' / 'no' when the fresh-dislocation rule fires, else None."""
    if not (0.0 < r.bid < r.ask < 1.0) or (r.ask - r.bid) > cfg.max_spread + 1e-9:
        return None
    move = r.mid - r.mid_24h
    g0, g = r.gap_24h, r.gap
    if abs(move) < cfg.th_move - 1e-9 or abs(g) < cfg.th_gap - 1e-9 or g0 == 0 or g == 0:
        return None
    if (move > 0) == (g0 > 0):          # moved TOWARD Poly (or with it): not our signal
        return None
    if (g > 0) != (g0 > 0):             # gap flipped through Poly: not a clean dislocation
        return None
    return "yes" if g > 0 else "no"


def exit_reason(entry_gap: float, entry_side: str, age_s: float, sharp: float, bid: float, ask: float,
                cfg: FreshGapConfig) -> str | None:
    g = sharp - (bid + ask) / 2.0
    signed = g if entry_side == "yes" else -g      # remaining gap in the trade's favour
    if signed <= 0:
        return "flipped"
    if signed <= cfg.exit_frac * abs(entry_gap) + 1e-9:
        return "closed"
    if age_s >= cfg.max_hold_s:
        return "timeout"
    return None



def poly_lead_signal(r: Row, th_move: float = 0.03, max_spread: float = 0.02) -> str | None:
    """POLY-LED LAG (research 2026-10-11, tools/research/tape/new_edges.py 'E'): Polymarket moved >= th_move over the
    lookback while SIG did NOT follow (moved < th_move/3 in Poly's direction), and SIG now sits on the far side of
    Poly by >= th_move/2. Trade SIG TOWARD Poly ('yes' if Poly moved up). Row's *_24h fields hold the lookback values.
    Exits reuse exit_reason (half-closed / flipped / timeout)."""
    if not (0.0 < r.bid < r.ask < 1.0) or (r.ask - r.bid) > max_spread + 1e-9:
        return None
    dp = r.sharp - r.sharp_24h
    ds = r.mid - r.mid_24h
    if abs(dp) < th_move - 1e-9:
        return None
    up = dp > 0
    if (ds if up else -ds) >= th_move / 3.0 - 1e-9:      # SIG already followed (at least a third of the move)
        return None
    g = r.gap
    if (g > 0) != up or abs(g) < th_move / 2.0 - 1e-9:   # SIG must lag on Poly's side by >= half the move
        return None
    return "yes" if up else "no"
