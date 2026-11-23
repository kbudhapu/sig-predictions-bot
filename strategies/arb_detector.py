"""SIG arb detector — READ-ONLY (Phase 2). Cannot place an order; it only READS and SURFACES.

Pipeline (per the dispatch):
  1. GET /relationships/constraints -> keep entries with evaluationStatus == "violated" and a meaningful
     violationAmount (>= min_edge).
  2. For each, CROSS-CHECK the live orderbook of each suggestedCorrectiveTrade's exchange to confirm the
     trade is actually FILLABLE (real depth on the side we'd hit, within a price tolerance) — not thin,
     uncapturable residue.
  3. Surface the real capturable arbs to the human (structured return + log + alert via the ported
     channels) so fat arbs can be grabbed BY HAND today while the write path is built safely.

Field access is intentionally TOLERANT (the SIG constraints payload had zero live violations when this was
written, so exact key spellings for a violated entry are confirmed against the first real violation). The
orderbook shape IS confirmed live: {bids:[{price,quantity}], asks:[...], bestBid, bestAsk, asOf.sequence}.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass
class CorrectiveLeg:
    exchange_id: str
    action: str              # "buy" | "sell"
    side: str                # "yes" | "no" (SIG option side)
    price: float | None
    quantity: float
    fillable_qty: float = 0.0
    best_opposing: float | None = None
    fillable: bool = False
    # The WHOLE orderbook as fetched, both sides. The fillability check reads only the side it
    # would hit, but the OPPOSITE side is what an exit has to trade against -- and it arrives in
    # the same response. Retaining it lets strategies/arb_sizing apply its exit-liquidity ceiling
    # for free; re-fetching would double the read budget against an API answering in ~7s a call.
    book: dict | None = None


@dataclass
class CapturableArb:
    relationship: str
    violation_amount: float
    legs: list[CorrectiveLeg] = field(default_factory=list)
    all_legs_fillable: bool = False
    min_fillable_qty: float = 0.0
    raw: dict = field(default_factory=dict)


class ArbDetector:
    def __init__(self, client, *, notifier=None, min_edge: float = 0.01, min_qty: float = 1.0,
                 price_tol: float = 0.01) -> None:
        self._c = client
        self._notifier = notifier
        self._min_edge = float(min_edge)      # ignore violations smaller than this (tick is 0.005)
        self._min_qty = float(min_qty)        # a leg must have at least this much fillable depth
        self._price_tol = float(price_tol)    # how far past the suggested price we still count as fillable

    # ── orderbook fillability cross-check (orderbook shape confirmed live) ──
    def _fillable_on_book(self, leg: CorrectiveLeg) -> None:
        try:
            ob = self._c.orderbook(leg.exchange_id)
        except Exception as e:
            log.warning("arb_orderbook_fetch_failed", exchange_id=leg.exchange_id, error=str(e))
            return
        leg.book = ob if isinstance(ob, dict) else None
        # buying lifts ASKS (we need asks at/below our willingness); selling hits BIDS.
        levels = ob.get("asks") if leg.action == "buy" else ob.get("bids")
        levels = levels or []
        leg.best_opposing = (_num(ob.get("bestAsk")) if leg.action == "buy" else _num(ob.get("bestBid"))) or None
        want = leg.price
        qty = 0.0
        for lvl in levels:
            px = _num(lvl.get("price")); q = _num(lvl.get("quantity"))
            if want is None:                                 # market order — all resting depth is reachable
                qty += q
            elif leg.action == "buy" and px <= want + self._price_tol:
                qty += q
            elif leg.action == "sell" and px >= want - self._price_tol:
                qty += q
        leg.fillable_qty = min(qty, leg.quantity) if leg.quantity > 0 else qty
        leg.fillable = leg.fillable_qty >= self._min_qty

    def _parse_legs(self, constraint: dict) -> list[CorrectiveLeg]:
        trades = _first(constraint, "suggestedCorrectiveTrades", "correctiveTrades", "trades", default=[]) or []
        legs = []
        for t in trades:
            legs.append(CorrectiveLeg(
                exchange_id=str(_first(t, "exchangeId", "exchange_id", "exchange", default="")),
                action=str(_first(t, "action", "side_action", default="buy")).lower(),
                side=str(_first(t, "side", "option", default="")).lower(),
                price=(None if _first(t, "price") is None else _num(_first(t, "price"))),
                quantity=_num(_first(t, "quantity", "qty", default=0.0))))
        return legs

    def scan(self) -> list[CapturableArb]:
        """Return the real, capturable arbs right now (empty when there are none)."""
        payload = self._c.relationship_constraints()
        rows = payload.get("data", []) if isinstance(payload, dict) else (payload or [])
        violated = [r for r in rows
                    if str(_first(r, "evaluationStatus", "status", default="")).lower() == "violated"
                    and _num(_first(r, "violationAmount", "violation", "amount", default=0.0)) >= self._min_edge]
        out: list[CapturableArb] = []
        for r in violated:
            arb = CapturableArb(
                relationship=str(_first(r, "relationshipName", "name", "relationshipId", "id", default="?")),
                violation_amount=_num(_first(r, "violationAmount", "violation", "amount", default=0.0)),
                legs=self._parse_legs(r), raw=r)
            for leg in arb.legs:
                if leg.exchange_id:
                    self._fillable_on_book(leg)
            arb.all_legs_fillable = bool(arb.legs) and all(l.fillable for l in arb.legs)
            arb.min_fillable_qty = min((l.fillable_qty for l in arb.legs), default=0.0)
            out.append(arb)
        capturable = [a for a in out if a.all_legs_fillable]
        self._surface(capturable, total_violations=len(violated))
        return capturable

    def _surface(self, arbs: list[CapturableArb], *, total_violations: int) -> None:
        log.info("arb_scan", total_violations=total_violations, capturable=len(arbs))
        for a in arbs:
            legs_desc = "; ".join(f"{l.action} {l.quantity}@{l.price} ex{l.exchange_id} (fillable {l.fillable_qty})"
                                  for l in a.legs)
            log.warning("CAPTURABLE_ARB", relationship=a.relationship, edge=a.violation_amount,
                        min_fillable_qty=a.min_fillable_qty, legs=legs_desc)
            if self._notifier is not None:
                try:
                    self._notifier.notify({
                        "kind": "capturable_arb", "severity": "high",
                        "message": f"Capturable arb on {a.relationship}: edge={a.violation_amount}, "
                                   f"min fillable qty={a.min_fillable_qty}",
                        "detail": {"component": "arb_detector", "relationship": a.relationship,
                                   "edge": a.violation_amount, "legs": legs_desc}})
                except Exception as e:
                    log.warning("arb_alert_failed", error=str(e))
