"""D+R PARITY reversion — pure decision logic (no I/O). Shadow-first (main_sig._parity_loop records only).

A race's D and R legs barely co-move on SIG (15-min change corr ≈ −0.12, efficient = −1), so the two YES
prices drift off summing to 1 and drift back (half-life ≈ 2h). See docs/research/sig_market_shape.md.

Trade (NO-PAIR side only — safe in every race):
  ENTER when d_bid + r_bid − 1 ≥ entry_edge: buy NO on BOTH legs at their NO asks (= 1 − YES bid).
        Cost per pair = 2 − d_bid − r_bid < 1. At settlement the pair pays ≥ 1 (exactly 1 if D or R wins,
        2 if a third candidate wins) — so `entry edge` is LOCKED even if we never exit.
  EXIT  when selling both NOs at their NO bids (= 1 − YES ask) realizes ≥ exit_pnl per pair:
        pnl = (d_bid0 + r_bid0) − (d_ask1 + r_ask1). Exiting recycles the capital into the next deviation.
The YES-pair side (sum < 1) is deliberately NOT traded: it only settles at 1 if no third candidate wins.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PairQuote:
    race: str
    d_ex: str
    r_ex: str
    d_bid: float
    d_ask: float
    r_bid: float
    r_ask: float

    @property
    def entry_edge(self) -> float:
        """Locked-at-settlement edge per pair for buying both NOs now (taker, at the NO asks)."""
        return self.d_bid + self.r_bid - 1.0


@dataclass
class OpenPair:
    race: str
    entry_ts: str
    d_bid0: float
    r_bid0: float
    entry_edge: float

    def exit_pnl(self, q: PairQuote) -> float:
        """Realized per-pair P&L if both NOs are sold now (taker, at the NO bids)."""
        return (self.d_bid0 + self.r_bid0) - (q.d_ask + q.r_ask)


def step(quotes: list[PairQuote], open_pairs: dict[str, OpenPair], *, now_iso: str,
         entry_edge: float = 0.02, exit_pnl: float = 0.01) -> list[dict]:
    """Advance the shadow book one tick. Mutates `open_pairs`; returns entry/exit events (dicts)."""
    events: list[dict] = []
    for q in quotes:
        if not (0.0 < q.d_bid <= q.d_ask < 1.0 and 0.0 < q.r_bid <= q.r_ask < 1.0):
            continue                                    # crossed / empty book → no decision
        op = open_pairs.get(q.race)
        if op is None:
            if q.entry_edge >= entry_edge - 1e-9:
                open_pairs[q.race] = OpenPair(q.race, now_iso, q.d_bid, q.r_bid, round(q.entry_edge, 4))
                events.append({"event": "entry", "ts": now_iso, "race": q.race, "d_ex": q.d_ex, "r_ex": q.r_ex,
                               "d_bid": q.d_bid, "d_ask": q.d_ask, "r_bid": q.r_bid, "r_ask": q.r_ask,
                               "entry_edge": round(q.entry_edge, 4),
                               "cost_per_pair": round(2.0 - q.d_bid - q.r_bid, 4)})
        else:
            pnl = op.exit_pnl(q)
            if pnl >= exit_pnl - 1e-9:
                del open_pairs[q.race]
                events.append({"event": "exit", "ts": now_iso, "race": q.race, "d_ex": q.d_ex, "r_ex": q.r_ex,
                               "d_bid": q.d_bid, "d_ask": q.d_ask, "r_bid": q.r_bid, "r_ask": q.r_ask,
                               "entry_ts": op.entry_ts, "entry_edge": op.entry_edge, "exit_pnl": round(pnl, 4)})
    return events


def open_from_events(events: list[dict]) -> dict[str, OpenPair]:
    """Rebuild the open shadow book from the recorded event log (restart-safe)."""
    book: dict[str, OpenPair] = {}
    for e in events:
        if e.get("event") == "entry":
            book[e["race"]] = OpenPair(e["race"], e["ts"], float(e["d_bid"]), float(e["r_bid"]), float(e["entry_edge"]))
        elif e.get("event") == "exit":
            book.pop(e["race"], None)
    return book
