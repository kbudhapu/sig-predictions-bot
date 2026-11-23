"""FAIR-BAND MAKER — pure decision logic (no I/O). Shadow-first (main_sig._fair_band_loop records quotes only).

Research (tools/research/tape/fmm_*.py, 2026-10-07/08, incl. an 8.2h out-of-sample window): resting YES quotes that
JOIN the book at fair +- w (fair = Polymarket, Kalshi fallback) fill when SIG wiggles away from fair and show
+0.3..+2.0c of favourable drift one hour later (our taker entries: -0.6c). Back-of-queue estimates: "balanced"
markets (SIG within ~1.5c of fair, median over 24h) ~2% of deployed capital per day marked to SIG, "liquid" markets
~0.9%/day. Improving inside the spread was consistently worse than joining. Fills rarely round-trip within a day:
this is a better ENTRY into the fair-value edge, not spread income.

Rules per market each minute:
  bid = min(floor(fair - w), best_bid)   ask = max(ceil(fair + w), best_ask)   (join or sit behind; never improve)
  and never cross (bid <= best_ask - tick, ask >= best_bid + tick).
  A side that would ADD exposure is skipped once that market's exposure >= cap_usd.
  Only markets tagged balanced or liquid are quoted (configurable).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TICK = 0.005


@dataclass(frozen=True)
class FairBandConfig:
    w: float = 0.03
    quote_usd: float = 2000.0
    cap_usd: float = 20000.0
    universes: tuple = ("balanced", "liquid")


def _floor(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 3)


def _ceil(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 3)


def band_quotes(row: dict, cfg: FairBandConfig, held_qty: float = 0.0) -> list[dict]:
    """Quotes for one market. row: exchange_id, fair, best_bid, best_ask, bid_size, ask_size, tags (set of str).
    held_qty: current signed YES position (+ YES, - NO) for the exposure cap."""
    tags = set(row.get("tags") or ())
    if not tags.intersection(cfg.universes):
        return []
    try:
        f, b, a = float(row["fair"]), float(row["best_bid"]), float(row["best_ask"])
    except (KeyError, TypeError, ValueError):
        return []
    if not (0.0 < b < a < 1.0 and 0.0 < f < 1.0):
        return []
    mid = (a + b) / 2.0
    expo = held_qty * mid if held_qty > 0 else -held_qty * (1.0 - mid)
    bq = min(_floor(f - cfg.w), b, round(a - TICK, 3))
    aq = max(_ceil(f + cfg.w), a, round(b + TICK, 3))
    out = []
    for side, px, depth in (("bid", bq, row.get("bid_size")), ("ask", aq, row.get("ask_size"))):
        if not (0.0 < px < 1.0):
            continue
        adds = (side == "bid" and held_qty >= 0) or (side == "ask" and held_qty <= 0)
        if adds and expo >= cfg.cap_usd:
            continue
        unit = px if side == "bid" else 1.0 - px
        out.append({"exchange_id": str(row["exchange_id"]), "side": side, "px": px,
                    "shares": round(cfg.quote_usd / max(unit, 0.01), 1),
                    "at_touch": (px == b if side == "bid" else px == a),
                    "depth": float(depth or 0.0), "fair": round(f, 4), "bid": b, "ask": a,
                    "tags": sorted(tags)})
    return out


def classify(median_gap: dict, usd_per_day: dict, *, balanced_gap: float = 0.015,
             liquid_quantile: float = 2 / 3) -> dict[str, set]:
    """{exchange_id: tags}: 'balanced' if median |fair - SIG mid| <= balanced_gap; 'liquid' if $/day traded is in the
    top (1 - liquid_quantile) of markets."""
    tags: dict[str, set] = {}
    vols = sorted(v for v in usd_per_day.values() if v is not None)
    cut = vols[int(len(vols) * liquid_quantile)] if vols else float("inf")
    for ex, g in median_gap.items():
        if g is not None and g <= balanced_gap:
            tags.setdefault(str(ex), set()).add("balanced")
    for ex, v in usd_per_day.items():
        if v is not None and v >= cut:
            tags.setdefault(str(ex), set()).add("liquid")
    return tags
