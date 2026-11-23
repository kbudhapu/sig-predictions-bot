# dutch_book — D+R complement arb (short both when YES bids sum > 1). LIVE-enabled, effectively INERT

strategy_id `dutch_book`, tier SIGNAL, strategy_type `arb` (atomic multi-leg, UNWIND_ALL_LEGS, priority 9000+),
cadence 5s. Lifetime: 27 orders. 3 are still `open` in the local table from 10-03, 0 attributed fills.

## Thesis
The Democratic-wins and Republican-wins markets of one contest are complements. If `yes_bid_D + yes_bid_R > 1`,
buy NO on both: cost = 2 − sum, payoff ≥ 1 in every outcome (including a third-party win), locked edge = sum − 1.
The sum < 1 side (buy both YES) is NOT traded, because a third party (e.g. Nebraska/Osborn) would lose the stake.

## Files
`strategies/dutch_book.py` (pure `evaluate_pair`) and `CoreDutchBookStrategy` in `sig/engine/live_strategies.py`.
Pairs are built once at startup from `/markets` titles matching "Will the Democratic/Republican Party win the <contest>".
About 117 pairs.

## Logic
- For up to `max_pairs` (40) pairs per cycle: edge = d_bid + r_bid − 1 ≥ `min_edge` → arb.
- Legs: buy NO at 1−yes_bid on each, equal qty = `per_pair_frac·equity / max leg price`, capped at `max_qty_per_leg`.
- Placed as ONE atomic multi-leg intent: the whole total is reserved, the gate clamps both legs proportionally
  (arb cap = `arb_sizing.arb_max_per_arb_frac`), and a partial fill unwinds.
- `record: true` writes every would-be arb to `data/dutch_book_shadow/arbs.jsonl` (9,252 rows; last 2026-10-04 17:53Z).

## Why it's inert
- `min_edge: 0.06` is far above the typical complement gaps (0.005–0.02). There were 0 arbs in the last hour on 10-05.
- When it did fire (at lower min_edge on 10-03), atomic bundles filled **0%**: the thin peer-to-peer book can't fill
  both legs at once.
- Also, all bundles share strategy_id, so the pending-bundle guard allows only 1 in flight.
- The 3 stale `open` local orders from 10-03 are probably long-cancelled at the venue (the local status never updated).

## Live config
`dutch_book: enabled true, min_edge 0.06, max_pairs 40, per_pair_frac 0.01, max_qty_per_leg 100000, record true,
place true`. Allocator prior 0.10 (unused: pool-eligible). Pool-eligible: yes.

## Open questions / next step if revisited
Measure naked-leg adverse selection from the jsonl at settlement (full-arb vs each leg alone) before ever going
non-atomic. `tools/correlated_race_scan.py` is the read-only scanner for the same edge.
