# arb — venue-published relationship-constraint arbitrage. Registered, INERT (venue publishes nothing)

strategy_id `arb`, tier CORE, strategy_type `arb` (atomic multi-leg, UNWIND_ALL_LEGS, priority 8500), cadence 5s.
Always registered in LIVE. Lifetime: 0 orders.

## Thesis
SIG publishes logical relationships between its markets (`GET /relationships/constraints`). Examples: an implies-B
relationship, or mutually exclusive outcomes summing to ≤ 1. When a constraint is `violated` by ≥ `arb_min_violation`,
the venue's own `suggestedCorrectiveTrades` form a riskless basket.

## Files
- `strategies/arb_detector.py` `ArbDetector.scan()`: reads constraints, keeps `violated` ones, then for each corrective
  leg reads the L2 orderbook to confirm fillable depth (within 0.01 of the suggested price). It only surfaces arbs
  whose legs are ALL fillable, and logs `arb_scan` + `CAPTURABLE_ARB`.
- `CoreArbStrategy` (`sig/engine/live_strategies.py`): turns each capturable arb into one atomic multi-leg intent.
  Sizing: `strategies/arb_sizing` (`size_arb_basket`):
  - per-arb ≤ `arb_max_per_arb_frac` (0.08) of bankroll,
  - total arb ≤ `arb_total_max_frac` (0.85),
  - floor `arb_min_notional` $250,
  - exit-liquidity ceiling from the fetched book.
- Basket exits: `strategies/exit/decision.py` `decide_exits` via `orchestrator._run_exits`. It recycles a filled basket
  early only if the realizable gain now beats holding to settlement versus the best alternative edge.
  `max_exits_per_cycle` 1. Default is HOLD to settlement.

## Live state (2026-10-05)
- `relationship_constraints()` returns an **empty list**, so `arb_scan total_violations=0 capturable=0` every minute.
- `baskets` table: 0 rows. So `_run_exits` (basket recycling) never has anything to do.
- Cost: ~1 read per scan (no violated rows → no orderbook reads). Harmless.

## Not-wired library code (exists, NOT used by the live trader)
- `strategies/relationship_engine/` (graph, hard_arbs, soft, parsing, published): our own relationship model. Only
  `contracts.py` helpers are imported (by the exit decision).
- `strategies/chamber_model/` (joint chamber-control model, baselines in `config/chamber_baselines.yaml`).
- `strategies/resolution/` (decided-gap / endgame / rules table) and `signals/fair_value.py`.
- The `resolution` strategy_type / kelly mult exists in config, but no resolution strategy is registered.
- These could matter near election night (decided races), but none are live.

## Related live edge
The D+R complement arb the venue does NOT publish is implemented separately as `dutch_book` (see `dutch_book.md`).
