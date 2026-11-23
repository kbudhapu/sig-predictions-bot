"""The FACTUAL layer: seat baselines, control thresholds, and which races can move control.

Everything in this module is arithmetic over verified data from config/chamber_baselines.yaml. It
contains NO model and NO probability. That separation is the point: the structural layer is
provable and can be trusted, and `joint.py`'s correlation model is explicitly not.

`load_baselines()` RE-DERIVES every number it can and raises on a mismatch, rather than trusting
the file. The failure mode being defended against is a silent one: a baseline that is wrong by one
seat produces a joint-control distribution that is wrong for every price on the board, and it looks
exactly like a working model. So the arithmetic has to close before anything downstream runs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # strategies/chamber_model/ -> repo root
DEFAULT_BASELINES_PATH = os.path.join(_ROOT, "config", "chamber_baselines.yaml")

REPUBLICAN = "republican"
DEMOCRATIC = "democratic"
PARTIES = (REPUBLICAN, DEMOCRATIC)

#: Statuses that mean no incumbent is on the ballot — the seat is OPEN.
_OPEN_STATUSES = frozenset({"retiring", "lost_renomination", "open_appointee_barred", "term_limited"})


class BaselineError(ValueError):
    """The baseline file is internally inconsistent. Never recoverable: downstream maths is void."""


@dataclass(frozen=True)
class Seat:
    state: str
    party: str                      # party HOLDING the seat going into the election
    incumbent: str = ""
    status: str = "running"
    seat_class: str = "2"

    @property
    def is_open(self) -> bool:
        """No incumbent on the ballot. Open seats behave differently (no incumbency advantage),
        which is why the status is carried rather than just the party."""
        return self.status in _OPEN_STATUSES


@dataclass(frozen=True)
class ChamberBaseline:
    """One chamber's structural facts, with the control arithmetic already closed."""

    name: str
    total_seats: int
    majority_seats: int
    composition: dict[str, int]
    seats_up: tuple[Seat, ...]
    seats_not_up: dict[str, int]
    control_threshold: dict[str, int]       # per party — asymmetric when a VP tiebreak exists
    has_control_market: bool = True
    vp_party: str | None = None
    indeterminate_states: frozenset[str] = frozenset()

    # -- the control arithmetic ------------------------------------------------ #
    def seats_up_count(self, party: str | None = None) -> int:
        if party is None:
            return len(self.seats_up)
        return sum(1 for s in self.seats_up if s.party == party)

    def seats_needed_from_up(self, party: str) -> int:
        """How many of the UP seats `party` must win to reach its control threshold.

        This is THE number the joint-structure model is built on: `threshold - seats_held_anyway`.
        Clamped to [0, seats_up] because a party already over the line needs none, and a party
        that cannot reach it even by sweeping needs more than exist (reported as the sweep count,
        with `can_reach_control` telling you it is impossible).
        """
        need = self.control_threshold[party] - self.seats_not_up.get(party, 0)
        return max(0, min(need, len(self.seats_up)))

    def can_reach_control(self, party: str) -> bool:
        """False when even winning EVERY up-seat leaves the party short."""
        return self.control_threshold[party] - self.seats_not_up.get(party, 0) <= len(self.seats_up)

    def net_gain_needed(self, party: str) -> int:
        """Net seats `party` must FLIP, i.e. needed-from-up minus the up-seats it already holds.
        Negative means it can afford losses: -2 == "can lose 2 and still control"."""
        return self.seats_needed_from_up(party) - self.seats_up_count(party)

    def can_lose(self, party: str) -> int:
        """How many of its own up-seats `party` can lose and still control. 0 when it cannot
        afford any; never negative."""
        return max(0, -self.net_gain_needed(party))

    def open_seats(self) -> tuple[Seat, ...]:
        return tuple(s for s in self.seats_up if s.is_open)

    def states(self) -> tuple[str, ...]:
        return tuple(s.state for s in self.seats_up)

    def seat_for(self, state: str) -> Seat | None:
        key = str(state).strip().casefold()
        for s in self.seats_up:
            if s.state == key:
                return s
        return None


@dataclass(frozen=True)
class Baselines:
    senate: ChamberBaseline
    house: ChamberBaseline
    governors: ChamberBaseline
    meta: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)

    def chamber(self, name: str) -> ChamberBaseline:
        key = str(name).strip().casefold()
        if key in ("senate", "u.s. senate", "us senate"):
            return self.senate
        if key in ("house", "u.s. house", "us house"):
            return self.house
        if key in ("governor", "governors", "gubernatorial"):
            return self.governors
        raise KeyError(f"unknown chamber {name!r}")


def _load_yaml(path: str | None = None) -> dict:
    import yaml

    with open(path or DEFAULT_BASELINES_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _seats(block: dict) -> tuple[Seat, ...]:
    out = []
    for row in block.get("seats_up") or []:
        out.append(Seat(
            state=str(row.get("state", "")).strip().casefold(),
            party=str(row.get("party", "")).strip().casefold(),
            incumbent=str(row.get("incumbent", "") or ""),
            status=str(row.get("status", "running")).strip().casefold(),
            seat_class=str(row.get("class", "2")),
        ))
    return tuple(out)


def _check_chamber(name: str, block: dict, seats: tuple[Seat, ...], errs: list[str]) -> None:
    """Re-derive what the file asserts. Every one of these has caught a real class of error:
    a seat listed twice, a party total that does not match the roster, a not-up count that does
    not complete the chamber."""
    comp = {k: int(v) for k, v in (block.get("composition") or {}).items() if isinstance(v, int)}
    total = int(block.get("total_seats", 0))

    # composition must fill the chamber exactly
    comp_sum = sum(comp.values())
    if comp_sum != total:
        errs.append(f"{name}: composition sums to {comp_sum}, expected total_seats={total}")

    # no duplicate states on the ballot
    dupes = {s.state for s in seats if [x.state for x in seats].count(s.state) > 1}
    if dupes:
        errs.append(f"{name}: duplicate seats_up states {sorted(dupes)}")

    declared_up = block.get("seats_up_total", block.get("states_up"))
    if declared_up is not None and seats and int(declared_up) != len(seats):
        errs.append(f"{name}: seats_up_total={declared_up} but {len(seats)} rows listed")

    # per-party up counts must match the roster
    for party, declared in (block.get("seats_up_by_party") or {}).items():
        actual = sum(1 for s in seats if s.party == str(party).casefold())
        if seats and int(declared) != actual:
            errs.append(f"{name}: seats_up_by_party[{party}]={declared} but roster has {actual}")

    # not_up + up must equal the party's whole holding
    for party, not_up in (block.get("seats_not_up") or {}).items():
        p = str(party).casefold()
        up = sum(1 for s in seats if s.party == p)
        held = comp.get(p, 0)
        if p == DEMOCRATIC:                       # independents caucusing D count toward D control
            held += comp.get("independent", 0)
        if seats and int(not_up) + up != held:
            errs.append(
                f"{name}: seats_not_up[{party}]={not_up} + up={up} = {int(not_up) + up}, "
                f"but composition gives {held} held")


def load_baselines(path: str | None = None) -> Baselines:
    """Load, VALIDATE and return the baselines. Raises BaselineError on any inconsistency."""
    raw = _load_yaml(path)
    errs: list[str] = []

    sen_b = raw.get("senate") or {}
    hou_b = raw.get("house") or {}
    gov_b = raw.get("governors") or {}
    if not sen_b or not hou_b:
        raise BaselineError(["chamber_baselines.yaml is missing the senate and/or house block"])

    sen_seats, gov_seats = _seats(sen_b), _seats(gov_b)
    _check_chamber("senate", sen_b, sen_seats, errs)
    _check_chamber("house", hou_b, (), errs)
    _check_chamber("governors", gov_b, gov_seats, errs)

    # The VP asymmetry must actually be expressed, or a 50-50 Senate is scored for the wrong party.
    sen_thresh = {k: int(v) for k, v in (sen_b.get("control_threshold") or {}).items()}
    if sen_b.get("vp_breaks_ties") and sen_b.get("vice_president_party"):
        vp = str(sen_b["vice_president_party"]).casefold()
        other = DEMOCRATIC if vp == REPUBLICAN else REPUBLICAN
        if sen_thresh.get(vp) != int(sen_b.get("majority_seats", 51)) - 1:
            errs.append(
                f"senate: vp_breaks_ties is set and the VP is {vp}, so control_threshold[{vp}] "
                f"must be majority-1 ({int(sen_b.get('majority_seats', 51)) - 1}), "
                f"got {sen_thresh.get(vp)}")
        if sen_thresh.get(other) != int(sen_b.get("majority_seats", 51)):
            errs.append(f"senate: control_threshold[{other}] must be the full majority")

    coverage = raw.get("sig_coverage") or {}
    indeterminate = {
        str(r.get("state", "")).casefold(): str(r.get("chamber", "")).casefold()
        for r in (coverage.get("three_way_races") or [])
    }

    def build(nm: str, block: dict, seats: tuple[Seat, ...]) -> ChamberBaseline:
        maj = int(block.get("majority_seats") or 0)
        thresh = {k: int(v) for k, v in (block.get("control_threshold") or {}).items()
                  if v is not None}
        if not thresh:
            thresh = {p: maj for p in PARTIES}
        return ChamberBaseline(
            name=nm,
            total_seats=int(block.get("total_seats", 0)),
            majority_seats=maj,
            composition={k: int(v) for k, v in (block.get("composition") or {}).items()
                         if isinstance(v, int)},
            seats_up=seats,
            seats_not_up={k: int(v) for k, v in (block.get("seats_not_up") or {}).items()},
            control_threshold=thresh,
            has_control_market=bool(block.get("has_control_market", True)),
            vp_party=(str(block["vice_president_party"]).casefold()
                      if block.get("vice_president_party") else None),
            indeterminate_states=frozenset(
                s for s, ch in indeterminate.items() if ch == nm.casefold()),
        )

    senate = build("senate", sen_b, sen_seats)
    house = build("house", hou_b, ())
    governors = build("governor", gov_b, gov_seats)

    # Cross-check the file's own stated net-gain figures against the derived arithmetic. These are
    # the headline numbers a human would quote, so a disagreement means one of them is a typo.
    for ch, block in ((senate, sen_b), (house, hou_b)):
        declared = block.get("democratic_net_gain_for_control")
        if declared is not None and ch.seats_up:
            derived = ch.net_gain_needed(DEMOCRATIC)
            if int(declared) != derived:
                errs.append(f"{ch.name}: democratic_net_gain_for_control={declared} but derived {derived}")
    declared_lose = sen_b.get("republican_can_lose")
    if declared_lose is not None:
        # Against the FULL majority (51), which is the figure published sources quote; the VP
        # tiebreak buys one more and is reported separately so the two never get conflated.
        strict = max(0, senate.seats_up_count(REPUBLICAN)
                     - (int(sen_b.get("majority_seats", 51)) - senate.seats_not_up.get(REPUBLICAN, 0)))
        if int(declared_lose) != strict:
            errs.append(f"senate: republican_can_lose={declared_lose} but derived {strict} (vs a strict majority)")

    if errs:
        raise BaselineError(errs)
    return Baselines(senate=senate, house=house, governors=governors,
                     meta=raw.get("meta") or {}, coverage=coverage)
