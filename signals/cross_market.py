"""Cross-market resolver — turns a race_mapping entry + raw external venue prices into the SIG-aligned
`real_money_prices` the divergence engine compares against. Pure math, no network.

THIS is the piece the engine was missing: `divergence.assess_divergence` takes `real_money_prices` as
already-SIG-aligned YES prices, so SOMETHING has to (a) apply the mapping's polarity flip and (b) enforce
the mapping's gate (confidence / resolution_match / liquidity) BEFORE those prices reach the comparison.
Without this, the mapping's `polarity` flag is never applied — an inverted pair would be compared raw and
the divergence sign would be backwards. All of that lives here, in one place.

Data-flow contract:
  raw_external_prices  = {venue: price_of_that_venue's_yes_outcome}   # as fetched (poly_client / Kalshi)
        │  apply_polarity per venue (direct ⇒ p, inverted ⇒ 1 − p)
        ▼
  real_money_prices    = {venue: SIG-aligned YES prob}               # only liquid+usable venues of an ACTIVE race
        │
        ▼
  assess_divergence(p, p_m_sig, real_money_prices=…)                 # unchanged math core

An unmapped / low-confidence / resolution-mismatched / illiquid race yields an EMPTY dict → no comparable →
the engine produces NO cross-market signal for it (dormant and safe), which is the whole point.
"""
from __future__ import annotations

from .divergence import assess_divergence, DivergenceResult
from .mapping import RaceMapping, ExternalRef, VALID_POLARITIES


def apply_polarity(raw_yes_price: float, polarity: str) -> float:
    """Align a venue's raw YES-outcome price to the SIG market's YES. `direct` ⇒ unchanged; `inverted`
    ⇒ 1 − p (the venue measures the OPPOSITE party, so SIG-YES is the complement). Raises on a bad flag
    and on a non-probability input — a silently-wrong flip is the exact bug this module exists to prevent."""
    p = float(raw_yes_price)
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"raw_yes_price {p!r} is not a probability in [0,1]")
    if polarity == "direct":
        return p
    if polarity == "inverted":
        return 1.0 - p
    raise ValueError(f"polarity {polarity!r} not in {VALID_POLARITIES}")


def resolve_external_prices(
    mapping: RaceMapping, raw_external_prices: dict[str, float | None]
) -> dict[str, float]:
    """Project raw venue prices to SIG-aligned YES prices, applying polarity and the mapping gate.

    Only the venues of an ACTIVE-eligible race (high confidence + resolution match) that are liquid+usable
    AND present in `raw_external_prices` with a non-None price are included. Everything else is dropped, so
    a dormant/unmapped race returns {} and contributes no comparable.
    """
    out: dict[str, float] = {}
    for venue, ref in mapping.usable_externals().items():
        raw = raw_external_prices.get(venue)
        if raw is None:
            continue
        out[venue] = apply_polarity(raw, ref.polarity)
    return out


def cross_market_divergence_for_race(
    mapping: RaceMapping,
    *,
    p: float,
    p_m_sig: float,
    raw_external_prices: dict[str, float | None],
    **assess_kwargs,
) -> DivergenceResult:
    """Full path for one race: gate + polarity-align the external prices, then run the divergence math.

    `p` is our calibrated fair value, `p_m_sig` is SIG's YES price for THIS SIG market, and
    `raw_external_prices` are the venues' raw YES-outcome prices as fetched. The returned
    DivergenceResult's `cross_market` is keyed by venue and already polarity-correct; an inactive race
    yields no cross-market entries (real_money_prices=None), so no cross-market edge is reported.
    """
    real = resolve_external_prices(mapping, raw_external_prices)
    return assess_divergence(p=p, p_m_sig=p_m_sig, real_money_prices=(real or None), **assess_kwargs)
