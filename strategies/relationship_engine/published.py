"""Cross-check our self-computed arbs against SIG's published constraints, if any ever appear.

Recon §3/§7: `/relationships` is `{data: []}`, `/relationships/graph` returns 400, and
`/relationships/constraints` returns 0 constraints pre-open. SIG may start publishing at 16:00Z or may
never publish — this engine must work in both worlds, so it computes relationships itself and treats
anything SIG publishes as a CROSS-CHECK rather than a source.

Deliberately NOT authoritative in either direction. A disagreement is logged, not resolved:
  - ours-only       -> likely fine (we compute from structure SIG does not publish) but worth seeing
  - theirs-only     -> a relationship we failed to model; the important one, because it means our
                       graph is incomplete
  - both            -> agreement, the reassuring case
Silently deferring to SIG would discard arbs it does not publish; silently overriding it would hide a
gap in our own graph. So we report and let the operator decide.
"""

from __future__ import annotations

from .contracts import Opportunity


def _exchange_ids(opp: Opportunity) -> frozenset[str]:
    return frozenset(str(leg.exchange_id) for leg in opp.legs)


def _constraint_exchange_ids(constraint: dict) -> frozenset[str]:
    """Pull exchange ids out of a published constraint, tolerating shape drift.

    The constraint schema is unobserved (0 returned in recon), so this reads defensively across the
    plausible spellings instead of asserting one and crashing the fast loop on the first live payload.
    """
    ids: set[str] = set()
    for key in ("suggestedCorrectiveTrades", "correctiveTrades", "trades", "legs", "exchanges"):
        for item in constraint.get(key) or []:
            if isinstance(item, dict):
                for id_key in ("exchangeId", "exchange_id", "id"):
                    if item.get(id_key) is not None:
                        ids.add(str(item[id_key]))
                        break
            elif item is not None:
                ids.add(str(item))
    for key in ("exchangeIds", "exchange_ids"):
        for item in constraint.get(key) or []:
            if item is not None:
                ids.add(str(item))
    return frozenset(ids)


def reconcile_with_published(our_arbs: list[Opportunity], sig_constraints) -> dict:
    """Compare our arbs with SIG's published constraints.

    No-op when `sig_constraints` is empty/absent (the pre-open state): returns a report with
    `published=0` and every one of our arbs under `ours_only`, with `agreement=None` because nothing
    was published to agree with. Never mutates `our_arbs` and never drops one.
    """
    rows = sig_constraints
    if isinstance(sig_constraints, dict):
        rows = sig_constraints.get("data", sig_constraints.get("constraints", []))
    rows = [r for r in (rows or []) if isinstance(r, dict)]

    ours = {_exchange_ids(o): o for o in (our_arbs or []) if o.legs}

    if not rows:
        return {
            "published": 0,
            "ours": len(ours),
            "both": [],
            "ours_only": [sorted(k) for k in ours],
            "theirs_only": [],
            "agreement": None,
            "note": (
                "no published constraints (pre-open state per recon §3) — self-computed relationships "
                "stand on their own; nothing to cross-check against"
            ),
            "disagreements": [],
        }

    theirs: dict[frozenset[str], dict] = {}
    for row in rows:
        ids = _constraint_exchange_ids(row)
        if ids:
            theirs[ids] = row

    both = sorted(ours.keys() & theirs.keys(), key=lambda s: sorted(s))
    ours_only = sorted(ours.keys() - theirs.keys(), key=lambda s: sorted(s))
    theirs_only = sorted(theirs.keys() - ours.keys(), key=lambda s: sorted(s))

    disagreements = []
    for ids in ours_only:
        disagreements.append(
            {
                "kind": "ours_only",
                "exchange_ids": sorted(ids),
                "type": ours[ids].type,
                "edge_amount": ours[ids].edge_amount,
                "note": "we computed a relationship SIG does not publish — expected while /relationships is empty",
            }
        )
    for ids in theirs_only:
        disagreements.append(
            {
                "kind": "theirs_only",
                "exchange_ids": sorted(ids),
                "note": "SIG published a constraint we did NOT model — our relationship graph may be incomplete",
            }
        )

    total = len(ours.keys() | theirs.keys())
    return {
        "published": len(theirs),
        "ours": len(ours),
        "both": [sorted(k) for k in both],
        "ours_only": [sorted(k) for k in ours_only],
        "theirs_only": [sorted(k) for k in theirs_only],
        "agreement": (len(both) / total) if total else None,
        "note": "cross-check only — neither side is treated as authoritative",
        "disagreements": disagreements,
    }
