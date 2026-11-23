# Strategy docs — index (ground truth from code + live droplet, 2026-10-05 @ 7087618)

Read in this order:
1. `00_pipeline.md`: processes, cycle, placement queue, sizing, risk gate, fill policies, exit pass, **known defects**.
2. `capital_and_allocation.md`: what really decides capital (pool + per-market cap; the priors are mostly decorative).
3. Strategies:

| doc | strategy | live status | capital now |
|---|---|---|---|
| `cross_market.md` | cross_market_divergence (+ sniper folded in) | **LIVE, core edge** | ~95% of NAV |
| `market_making.md` | market_making (+ MR maker-skew) | LIVE, cash-starved | ~4% |
| `mean_reversion.md` | mr_taker | signals, sizes to 0 | ~0 |
| `order_flow.md` | order_flow | inert | 0 |
| `dutch_book.md` | dutch_book | enabled, never fires | 0 |
| `arb.md` | arb (venue relationships) | inert (venue publishes none) | 0 |
| `event_trigger.md` | event_trigger | inert; **provider file untracked** | 0 |
| `shadow_research.md` | sniper/book_pressure/dutch recorder/mm_shadow/FL scan | record-only | — |

## Top issues found in the code read (not fixed; ranked by P&L impact)
1. **Short cross positions are judged as longs by the exit** (venue `option` is always "YES"). Buy-NO → `thesis_reversed`
   → sell-NO → re-buy churn (967, 1061). Fix: `is_long = qty > 0`. → `cross_market.md`
2. **MM fights cross_market**: MM treats the whole venue position as its own inventory, so it unloads cross's
   over-cap positions at −2¢ skew (4,142 fills / ~$45k on cross markets since 10-04). Fix: exclude cross-owned
   markets from MM. → `market_making.md`
3. `strategy.on_fill()` is never called (MR position tracker dead; any on_fill-based perf is dead).
4. The pool reserve starves every non-pool strategy when cash < $15k.
5. `sig/engine/event_trigger_provider.py` is untracked, droplet-only.
6. Data: local orders mislabel exit sells as buys, the fill ledger is unsigned, MM multi-leg fills are unattributable,
   and MM bundles never leave OMS `_pending`.
