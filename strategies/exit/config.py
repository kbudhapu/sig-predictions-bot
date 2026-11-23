"""Load the exit-decision parameters from config/sig.yaml (`exit:`) — nothing is defaulted in code.

Same discipline as strategies/sizing/config.py: a frozen dataclass built from the yaml, with validation
that FAILS LOUD. The reason this matters more here than almost anywhere else in the bot is that every
one of these numbers is the difference between "recycle capital and compound" and "churn away a
guaranteed settlement remainder for a rounding error". A silently-defaulted `min_recycle_advantage`
of 0.0 would turn the whole module into "always exit when green", which is precisely the behaviour
the exit rule exists to prevent.

The `fees` convention is COPIED from strategies/relationship_engine/hard_arbs.py on purpose: there it
is documented as "total fee per 1.0 of basket" — a FRACTION charged once per basket unit (one contract
on each of the N legs), not per leg and not per dollar. Entry and exit must agree, otherwise an arb
that entry priced as profitable can be exited at a fee the entry never charged, and the two modules
would disagree about whether the trade ever made money.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass, field

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO / "config" / "sig.yaml"

#: Decision labels. Strings rather than an enum so an ExitDecision serialises straight into the
#: audit log / state DB without a custom encoder (same choice the relationship engine made for
#: `type` and `action`).
EXIT = "exit"
HOLD = "hold"


def load_raw_config(path: str | pathlib.Path | None = None) -> dict:
    """Read config/sig.yaml and assert the `exit:` section exists.

    Deliberately does NOT call sig.config.load_config(): this module is pure and must be importable
    and testable without .env, secrets or an AppConfig. sig/config.py still validates the `exit:`
    keys against its central `_SCHEMA` on the real load path, so a typo fails loud there too.
    """
    p = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "exit" not in cfg:
        raise ValueError(f"{p} is missing required 'exit' section")
    return cfg


def parse_utc(stamp: str) -> _dt.datetime:
    """Parse an ISO-8601 UTC stamp (the yaml writes a trailing 'Z', which fromisoformat rejects on
    older Pythons). Always returns a tz-aware datetime so a naive/aware subtraction can never raise
    deep inside the decision path."""
    text = str(stamp).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    out = _dt.datetime.fromisoformat(text)
    if out.tzinfo is None:
        out = out.replace(tzinfo=_dt.timezone.utc)
    return out.astimezone(_dt.timezone.utc)


@dataclass(frozen=True)
class ExitConfig:
    """Every tunable the exit decision reads. Constructed from the yaml — nothing hardcoded.

    fees
        Fractional fee per 1.0 of basket, matching the relationship engine's entry convention.
    min_recycle_advantage
        The RELATIVE margin by which the recycled return-per-day must beat the forfeited
        remainder's return-per-day. A dead heat must HOLD: exiting costs two round trips of
        spread and fees, so a marginal exit is strictly worse than doing nothing. Expressed
        relatively (a multiplier on the hold rate) rather than in dollars-per-day so the same
        number governs a $500 basket and a $30,000 one.
    liquidity_safety_margin
        How much headroom the pre-slippage gain must have over the measured slippage before the
        exit is considered absorbable. 0.25 means slippage may be at most 1/1.25 = 80% of the
        pre-slippage gain. Exists because the book we walked is a snapshot: by the time the sells
        land, a level may be gone.
    min_realizable_gain
        Absolute dollar floor. Below this the exit is not worth an order-path round trip at all,
        whatever the per-day arithmetic says.
    days_to_settlement_floor
        Divide-by-zero guard AND a cap on the 1/days divergence. As days → 0 the remainder's
        implied daily rate → ∞, which would make every late exit look insane for reasons of
        arithmetic rather than economics. Floored, same spirit as risk.tau_floor.
    alternative_horizon_days
        The expected number of days before the freed capital's NEXT opportunity realises its edge.
        See `decision.py` for the full argument; in short this is capped at days_to_settlement,
        and setting it >= the competition length recovers the strictly-conservative
        "the alternative is also held to the wall" reading.
    settlement_utc
        The single settlement wall. Every SIG market in this tournament settles at the same
        instant, so one stamp is the whole clock.
    """

    fees: float
    min_recycle_advantage: float
    liquidity_safety_margin: float
    min_realizable_gain: float
    days_to_settlement_floor: float
    alternative_horizon_days: float
    settlement_utc: str
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def settlement_at(self) -> _dt.datetime:
        return parse_utc(self.settlement_utc)

    def days_to_settlement(self, now: _dt.datetime | None = None) -> float:
        """Calendar days from `now` to the wall. NOT floored and NOT clamped at zero — the caller
        needs to see a non-positive value so `decide_basket_exit` can refuse to exit early on a
        basket that is already settling. Flooring here would hide that."""
        ref = now or _dt.datetime.now(_dt.timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=_dt.timezone.utc)
        return (self.settlement_at - ref).total_seconds() / 86400.0

    @classmethod
    def from_cfg(cls, cfg: dict) -> "ExitConfig":
        if "exit" not in cfg or not isinstance(cfg["exit"], dict):
            raise ValueError("config is missing a mapping 'exit' section")
        e = cfg["exit"]
        missing = sorted(
            {
                "fees",
                "min_recycle_advantage",
                "liquidity_safety_margin",
                "min_realizable_gain",
                "days_to_settlement_floor",
                "alternative_horizon_days",
                "settlement_utc",
            }
            - set(e)
        )
        if missing:
            raise ValueError(f"exit section is missing required keys: {missing}")

        out = cls(
            fees=float(e["fees"]),
            min_recycle_advantage=float(e["min_recycle_advantage"]),
            liquidity_safety_margin=float(e["liquidity_safety_margin"]),
            min_realizable_gain=float(e["min_realizable_gain"]),
            days_to_settlement_floor=float(e["days_to_settlement_floor"]),
            alternative_horizon_days=float(e["alternative_horizon_days"]),
            settlement_utc=str(e["settlement_utc"]),
            raw=cfg,
        )
        out.validate()
        return out

    def validate(self) -> None:
        """Fail loud on a value that would quietly invert the rule."""
        errors: list[str] = []
        if not (0.0 <= self.fees < 1.0):
            errors.append(f"exit.fees={self.fees} must be in [0, 1) (fraction per 1.0 of basket)")
        if self.min_recycle_advantage < 0.0:
            # A negative margin would mean "exit even when recycling is WORSE than holding".
            errors.append(
                f"exit.min_recycle_advantage={self.min_recycle_advantage} must be >= 0 — a negative "
                "margin would authorise exits that lose to simply holding to settlement"
            )
        if self.liquidity_safety_margin < 0.0:
            errors.append(
                f"exit.liquidity_safety_margin={self.liquidity_safety_margin} must be >= 0"
            )
        if self.min_realizable_gain < 0.0:
            errors.append(f"exit.min_realizable_gain={self.min_realizable_gain} must be >= 0")
        if self.days_to_settlement_floor <= 0.0:
            errors.append(
                f"exit.days_to_settlement_floor={self.days_to_settlement_floor} must be > 0 "
                "(it is the divide-by-zero guard on the per-day arithmetic)"
            )
        if self.alternative_horizon_days <= 0.0:
            errors.append(
                f"exit.alternative_horizon_days={self.alternative_horizon_days} must be > 0"
            )
        try:
            parse_utc(self.settlement_utc)
        except Exception as exc:  # noqa: BLE001 — surface the parse failure as a config error
            errors.append(f"exit.settlement_utc={self.settlement_utc!r} is not ISO-8601 UTC: {exc}")
        if errors:
            raise ValueError("invalid exit config:\n  - " + "\n  - ".join(errors))


def load_exit_config(path: str | pathlib.Path | None = None) -> ExitConfig:
    return ExitConfig.from_cfg(load_raw_config(path))
