"""The output contract the executor consumes, plus price-access helpers.

    {legs: [{exchange_id, side, action, price, quantity}],
     type: "hard_arb" | "soft_mispricing",
     edge_amount: float,
     confidence: float}

SIG order conventions (sig/sig_broker.py): side is "yes"|"no", action is "buy"|"sell", quantity is an
integer contract count, price sits on a 0.005 tick in [0.005, 0.995]. Every market here is a single
YES exchange, so side is always "yes" and direction is carried by `action`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TICK = 0.005
MIN_PRICE = 0.005
MAX_PRICE = 0.995

HARD_ARB = "hard_arb"
SOFT_MISPRICING = "soft_mispricing"


def round_to_tick(price: float, *, action: str) -> float:
    """Snap to the 0.005 tick in the CONSERVATIVE direction for the given action.

    A buy rounds UP and a sell rounds DOWN, so tick-snapping can only ever make the computed edge
    look smaller than it is. Rounding the profitable way would let a sub-tick rounding artefact
    manufacture an arb that cannot be filled at the rounded price.
    """
    if price is None:
        raise ValueError("price is None")
    steps = float(price) / TICK
    # guard float noise so an exact tick does not creep to the next one
    steps = round(steps, 6)
    if action == "buy":
        snapped = -(-steps // 1) * TICK          # ceil
    else:
        snapped = (steps // 1) * TICK            # floor
    return round(min(max(snapped, MIN_PRICE), MAX_PRICE), 4)


@dataclass(frozen=True, slots=True)
class Leg:
    exchange_id: object
    side: str          # always "yes" in this tournament
    action: str        # "buy" | "sell"
    price: float
    quantity: int

    def to_dict(self) -> dict:
        return {
            "exchange_id": self.exchange_id,
            "side": self.side,
            "action": self.action,
            "price": self.price,
            "quantity": self.quantity,
        }


@dataclass(frozen=True, slots=True)
class Opportunity:
    legs: list[Leg]
    type: str
    edge_amount: float
    confidence: float
    # Diagnostics — deliberately OUTSIDE the four contract keys so the executor's shape is exactly
    # what was specified, while a human reading a log can still see which race and why.
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "type": self.type,
            "edge_amount": round(float(self.edge_amount), 6),
            "confidence": round(float(self.confidence), 4),
        }

    def to_dict_verbose(self) -> dict:
        return {**self.to_dict(), "meta": dict(self.meta)}


def index_prices(prices) -> dict:
    """Normalise /exchanges/prices into {exchange_id: {bestBid, bestAsk, ...}}.

    Accepts the raw envelope ({"data": [...]}), a bare list of rows, or an already-keyed dict, since
    the caller may hand us any of the three. Keys are indexed under BOTH the original type and its
    string form: market payloads and price payloads do not reliably agree on int-vs-str ids, and a
    silent type mismatch would read as "no price" and quietly suppress every arb.
    """
    rows = None
    if isinstance(prices, dict):
        if "data" in prices and isinstance(prices["data"], list):
            rows = prices["data"]
        else:
            out: dict = {}
            for key, val in prices.items():
                if isinstance(val, dict):
                    out[key] = val
                    out[str(key)] = val
            return out
    elif isinstance(prices, list):
        rows = prices
    if rows is None:
        return {}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ex = row.get("exchangeId", row.get("exchange_id", row.get("id")))
        if ex is None:
            continue
        out[ex] = row
        out[str(ex)] = row
    return out


def _num(value) -> float | None:
    """None-preserving float. A missing quote must stay None, never become 0.0 — a 0.0 ask would
    read as a free contract and fabricate an unbounded arb."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None      # NaN -> None


def best_ask(row: dict | None) -> float | None:
    if not row:
        return None
    return _num(row.get("bestAsk", row.get("best_ask", row.get("ask"))))


def best_bid(row: dict | None) -> float | None:
    if not row:
        return None
    return _num(row.get("bestBid", row.get("best_bid", row.get("bid"))))


def mid(row: dict | None) -> float | None:
    bid, ask = best_bid(row), best_ask(row)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def depth_for(depths, exchange_id, action: str) -> int | None:
    """Level-1 size available on the side we would hit, if the caller supplied orderbooks.

    Recon §4: every quote is exactly one seeded level of 1000 contracts, so level-1 size IS the
    whole tradeable depth right now — there is no curve beyond it.
    """
    if not depths:
        return None
    book = depths.get(exchange_id)
    if book is None:
        book = depths.get(str(exchange_id))
    if not isinstance(book, dict):
        return None
    levels = book.get("asks" if action == "buy" else "bids") or []
    total = 0
    for lvl in levels[:1]:
        if isinstance(lvl, dict):
            try:
                total += int(float(lvl.get("quantity", 0) or 0))
            except (TypeError, ValueError):
                continue
    return total or None
