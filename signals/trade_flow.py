"""Trade-tape taker-flow filter.

The venue's price WS carries no sizes ([[sig-feed-nbbo-no-sizes]]); the TRADE TAPE
(/exchanges/{id}/trades) does. Each print has `size` and `side` (YES|NO) where `side` is the
AGGRESSOR's outcome space: a YES print is someone BUYING YES (bullish YES), a NO print is someone
BUYING NO (== selling YES, bearish YES). So signed YES aggression over a recent window is

    imbalance = (yes_vol - no_vol) / (yes_vol + no_vol)   in [-1, +1]

+1 = relentless YES-buying (price pushing toward 1); -1 = relentless NO-buying (pushing toward 0).

Why this is a FILTER, not an edge: our directional edges (cross-market divergence, favorite-longshot)
fade a price we believe is wrong. The one way that goes badly is fading INFORMED flow — the price is
"wrong" by our benchmark precisely because real money is repricing it on news we don't have (this is
the shape of the wiki loss). So before we fade, we check the tape: if aggressive flow is running
AGAINST our intended direction beyond a threshold, we stand down. Quiet or confirming flow → proceed.

A thin tape carries no information, so below `min_volume` the verdict is `insufficient_flow` and it
NEVER vetoes (absence of a signal is not a veto).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class FlowVerdict:
    yes_vol: int
    no_vol: int
    n_trades: int
    imbalance: float | None     # (yes-no)/(yes+no); None when no volume
    contradicts: bool           # True => aggressive flow runs AGAINST the intended direction
    reason: str                 # confirming | neutral | contradicted | insufficient_flow

    @property
    def total_vol(self) -> int:
        return self.yes_vol + self.no_vol


def _age_s(created_at: str, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        d = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (now - d).total_seconds()


def evaluate(
    trades: list[dict],
    *,
    long_yes: bool,
    min_volume: int = 100,
    imbalance_veto: float = 0.60,
    max_age_s: float = 1800.0,
    now: datetime | None = None,
) -> FlowVerdict:
    """Summarise recent taker flow and decide whether it CONTRADICTS a trade.

    long_yes=True  -> we are backing YES (buy YES). Contradicted by heavy NO aggression (imbalance
                      <= -imbalance_veto).
    long_yes=False -> we are fading YES (buy NO == short YES). Contradicted by heavy YES aggression
                      (imbalance >= +imbalance_veto): someone is aggressively buying the thing we fade.

    Only trades fresher than max_age_s count. Below min_volume -> insufficient_flow, never a veto.
    """
    now = now or datetime.now(timezone.utc)
    yes_vol = no_vol = n = 0
    for t in trades or []:
        age = _age_s(t.get("createdAt", ""), now)
        if age is None or age > max_age_s or age < 0:
            continue
        try:
            size = int(t.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        side = str(t.get("side", "")).upper()
        if side == "YES":
            yes_vol += size
        elif side == "NO":
            no_vol += size
        else:
            continue
        n += 1

    total = yes_vol + no_vol
    if total < min_volume:
        return FlowVerdict(yes_vol, no_vol, n, None, False, "insufficient_flow")

    imb = (yes_vol - no_vol) / total
    # pressure AGAINST us: NO-dominant when long YES, YES-dominant when fading YES
    against = (-imb) if long_yes else imb
    if against >= imbalance_veto:
        return FlowVerdict(yes_vol, no_vol, n, imb, True, "contradicted")
    # confirming = flow runs WITH us past the same bar
    with_us = imb if long_yes else (-imb)
    reason = "confirming" if with_us >= imbalance_veto else "neutral"
    return FlowVerdict(yes_vol, no_vol, n, imb, False, reason)
