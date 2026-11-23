"""Loader + schema for race_mapping.yaml — SIG race ↔ external venue (Polymarket / Kalshi) contracts.

Pure data, no network. This file defines WHICH external contract corresponds to each SIG market and,
critically, HOW to align it:

  • polarity         — does the venue's YES leg mean the SAME thing as the SIG market's YES, or the
                       OPPOSITE party? "direct" ⇒ venue_yes == SIG_yes; "inverted" ⇒ venue_yes == 1 − SIG_yes.
                       (SIG "Republican wins X" vs a venue "Will a Democrat win X? / Yes" ⇒ INVERTED.
                       Getting this backwards flips the divergence sign — the #1 trap. The resolver in
                       `cross_market.py` is the ONLY place the flip is applied; see that module.)
  • resolution_match — do the two contracts resolve on the SAME event (both "party wins the seat"),
                       not one-wins-seat vs one-margin-of-victory / primary / seat-count / supermajority?
  • liquidity_ok     — is the external contract liquid enough (live price + volume/OI) to be a reliable
                       comparable? A thin/empty external price is NOISE, not a comparable.
  • confidence       — "high" (verified: exact match, same resolution, liquid)   → engine MAY trade it,
                       "low"  (plausible/structurally matched but NOT price-verified) → recorded, DORMANT,
                       "skip" (no clean match / unverifiable)                        → recorded, DORMANT.

Safety contract (see `RaceMapping.is_active`): the engine trades a race's cross-market edge ONLY when it
is confidence=="high" AND resolution_match AND at least one external is liquid+usable. Everything else is
present-but-dormant — a WRONG mapping is worse than NO mapping, so unverified matches never feed the engine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "race_mapping.yaml")

VALID_POLARITIES = ("direct", "inverted")
VALID_CONFIDENCE = ("high", "low", "skip")


@dataclass(frozen=True)
class ExternalRef:
    """One external venue's contract for a race, plus how to ALIGN it to the SIG market's YES leg.

    `yes_outcome` names the venue outcome whose price is this race's YES/target probability. `polarity`
    then says whether that price is used directly or flipped (1 − p) to match the SIG market's YES.
    Any identifier may be None until resolved against the live venue list; a ref is only USABLE once it
    is liquid and carries at least one identifier.
    """

    venue: str                           # "polymarket" | "kalshi"
    ticker: str | None = None            # Kalshi market ticker, e.g. "CONTROLS-2026-R"
    slug: str | None = None              # Polymarket gamma slug
    market_id: str | None = None         # Polymarket gamma market id
    yes_outcome: str | None = None       # the venue outcome treated as this race's YES leg
    yes_token_id: str | None = None      # Polymarket CLOB token id for yes_outcome (for /midpoint,/price)
    polarity: str = "direct"             # "direct": venue_yes == SIG_yes; "inverted": venue_yes == 1 − SIG_yes
    liquidity_ok: bool = False           # verified liquid enough to be a reliable price
    notes: str = ""

    def has_identifier(self) -> bool:
        return bool(self.ticker or self.market_id or self.yes_token_id or self.slug)

    def is_usable(self) -> bool:
        """A ref may feed the engine only if its polarity is known, it is liquid, and it names a contract."""
        return self.polarity in VALID_POLARITIES and self.liquidity_ok and self.has_identifier()


@dataclass(frozen=True)
class RaceMapping:
    key: str
    description: str
    resolution_match: bool
    confidence: str                      # "high" | "low" | "skip"
    externals: dict[str, ExternalRef] = field(default_factory=dict)
    sig_market_id: str | None = None
    sig_exchange_id: str | None = None
    notes: str = ""

    @property
    def polymarket(self) -> ExternalRef | None:
        return self.externals.get("polymarket")

    @property
    def kalshi(self) -> ExternalRef | None:
        return self.externals.get("kalshi")

    def is_active(self) -> bool:
        """True ⇒ the engine may trade this race's cross-market edge. Requires VERIFIED high confidence,
        a genuine resolution match, AND at least one liquid+usable external. Any failure ⇒ dormant."""
        return (
            self.confidence == "high"
            and self.resolution_match
            and any(e.is_usable() for e in self.externals.values())
        )

    def usable_externals(self) -> dict[str, ExternalRef]:
        """The external venues that may feed the engine for this race — empty unless the race is active
        (high confidence + resolution match), then only the liquid+usable venues. The gate, in data form."""
        if not (self.confidence == "high" and self.resolution_match):
            return {}
        return {v: e for v, e in self.externals.items() if e.is_usable()}

    def needs_resolution(self) -> bool:
        """True if any id still needs to be filled from the live market lists (SIG side or an external)."""
        if self.sig_market_id is None or self.sig_exchange_id is None:
            return True
        return any(not e.has_identifier() for e in self.externals.values())


def _external_from_cfg(venue: str, cfg: dict, race_key: str) -> ExternalRef:
    polarity = str(cfg.get("polarity", "direct")).lower()
    if polarity not in VALID_POLARITIES:
        raise ValueError(
            f"race_mapping.yaml: race {race_key!r} {venue} polarity {polarity!r} "
            f"not in {VALID_POLARITIES}"
        )
    return ExternalRef(
        venue=venue,
        ticker=cfg.get("ticker"),
        slug=cfg.get("slug"),
        market_id=cfg.get("market_id"),
        yes_outcome=cfg.get("yes_outcome"),
        yes_token_id=cfg.get("yes_token_id"),
        polarity=polarity,
        liquidity_ok=bool(cfg.get("liquidity_ok", False)),
        notes=str(cfg.get("notes", "")),
    )


def load_race_mapping(path: str | None = None) -> dict[str, RaceMapping]:
    """Parse race_mapping.yaml into {race_key: RaceMapping}. FAILS LOUD on a malformed file or an
    out-of-range polarity/confidence value (a silently-wrong flag is exactly the failure this guards)."""
    import yaml  # lazy import so the math core never needs PyYAML

    with open(path or _DEFAULT_PATH, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    races = doc.get("races") or {}
    if not isinstance(races, dict):
        raise ValueError("race_mapping.yaml: 'races' must be a mapping")

    out: dict[str, RaceMapping] = {}
    for key, cfg in races.items():
        cfg = cfg or {}
        sig = cfg.get("sig") or {}

        confidence = str(cfg.get("confidence", "skip")).lower()
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(
                f"race_mapping.yaml: race {key!r} confidence {confidence!r} not in {VALID_CONFIDENCE}"
            )

        externals: dict[str, ExternalRef] = {}
        for venue in ("polymarket", "kalshi"):
            vcfg = cfg.get(venue)
            if vcfg:
                externals[venue] = _external_from_cfg(venue, vcfg, key)

        out[key] = RaceMapping(
            key=key,
            description=str(cfg.get("description", "")),
            resolution_match=bool(cfg.get("resolution_match", False)),
            confidence=confidence,
            externals=externals,
            sig_market_id=(str(sig["market_id"]) if sig.get("market_id") is not None else None),
            sig_exchange_id=(str(sig["exchange_id"]) if sig.get("exchange_id") is not None else None),
            notes=str(cfg.get("notes", "")),
        )
    return out
