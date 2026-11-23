# cross_market_divergence — THE core edge (sniper merged into it)

**Status (2026-10-05): LIVE.** Holds ~95% of NAV (~$83k MV over 16 positions, incl. legacy sniper entries).
strategy_id `cross_market_divergence`, tier SIGNAL, strategy_type `directional`, cadence 30s.
Read `00_pipeline.md` first: sizing, gating and the exit loop are shared machinery.

## Thesis
Polymarket is a real-money, liquid market. SIG is a student/bot market that does not price to it. When SIG's
price for a race diverges from the polarity-aligned Polymarket price, bet that SIG converges to Polymarket by
settlement (Nov 4). We do NOT forecast the race. We bet that the sharp market is closer to truth than SIG.
- Lesson from the wiki loss: only REAL-MONEY markets count as sharp. Wikipedia ratings caused a loss (~$3k of residue counted on 10-05). Order log 2026-11-20: the re-buying runaway (exit market-close + position drift) began 11:51Z, before the wiki launch at 11:57Z; the wiki benchmark then triggered exits on ~100 state-seat markets at 12:08Z. The total cost of that night isn't reconstructable.
  `wiki_enabled: false` (overlay, and the code default is also False).
- Kalshi: the client exists (`signals/kalshi_client.py`) but every leg is `liquidity_ok:false`, so it's dormant.
- `signals/sharp_benchmark.py` (multi-source agreement gate) is NOT wired into the live path.

## Files
- `strategies/cross_market/strategy.py`: `CrossMarketDivergenceStrategy.evaluate` (signal + intent).
- `sig/engine/cross_market_provider.py`: Polymarket poller, SIG price provider, `aligned_benchmark`, **`exit_decision`**.
- `signals/race_mapping.yaml` + `signals/mapping.py`: SIG exchange_id ↔ Polymarket market_id/yes_token + polarity.
  **122 entries, 119 active** (118 direct, 1 inverted = `us_house_control_dem`).
  Covers control, governors, senate, and some house races.
- `signals/cross_market.py`: `resolve_external_prices`. This is the ONE polarity flip (inverted ⇒ 1−p).
- `signals/conviction.py`: conviction + effective threshold.
- `signals/divergence.py`: `assess_divergence` confidence tier (favorite-longshot labelling).
- `signals/poly_client.py`: Polymarket gamma + CLOB read client (GET-only; we can't trade Polymarket).

## Data flow
1. **Polymarket poller** (`CrossMarketProvider.run_poller`, `poll_interval_s: 1`, concurrency 20) sweeps all 119
   active races about once a second. It reads the CLOB `/midpoint` of the yes_token. On a 404/400 (no CLOB book) it
   falls back to the gamma outcome price. Degenerate 0.0/1.0 are dropped (`_valid_prob`). Prices are cached RAW.
2. **SIG price** = mid of best_bid/best_ask from the shared `prices` store (WS feed). `sig_client=None`, so there's no
   self-fetch. Ages over `sig_stale_s` (300s) are dropped.
3. `evaluate()` per active race:
   - External quotes older than 30s are dropped. `resolve_external_prices` aligns polarity → `external_fair`.
   - `divergence = external_fair − sig_mid`.
   - **Threshold** (live `conviction_weighting: true`): `conv = 2·|external_fair − 0.5|` (quality 1.0 for Polymarket),
     `threshold = max(cost_floor, cost_floor/conv)` with `conviction_cost_floor = 0.05`.
     - Live `min_divergence: 0.05` is IGNORED when conviction weighting is on.
     - Effect: a sharp at 0.95 needs |gap| ≥ 0.056; at 0.85 ≥ 0.071; at 0.75 ≥ 0.10; at 0.60 ≥ 0.25.
       **Cross only trades near the extremes** (favorites/longshots), which matches the book.
   - Skepticism curve: full size for |gap| ≤ 0.25, linear shrink to 0 at 0.40, REFUSE beyond. Refusals log
     `IMPLAUSIBLE_GAP_mapping_audit`.
   - `conf = tier_base(assess_divergence tier) × agreement(0.7 single source) × min(|gap|/0.20, 1) × skep × conv`.
     Then `sigma_p = 0.25 − conf·(0.25−0.03)`.
   - **Direction**: gap > 0 ⇒ **buy YES** at the SIG mid (floored to tick). gap < 0 ⇒ **buy NO** at 1−SIG mid.
     Every view is a BUY (sell-YES ≡ buy-NO on this venue).
   - The intent sets `allow_stale_market: True` so the 15s fast-edge stale gate doesn't block it.
4. **Sizing**: pool-eligible, so budget = all available cash. Kelly mult 4.0 and ceiling 2.0 mean
   `notional = min(2·avail, avail)`. **The size is effectively set by the risk gate's per-market cap**
   (`max_per_market_frac 0.12` × NAV − existing cost) and by available cash. That's why positions cluster at ~12% NAV each.
5. **Placement**: priority 8000 + 1000·|div|. CHASE policy. Only ONE cross_market bundle can be in flight
   (pending-bundle guard), so the rest log `order_path_blocked_pending_bundle`.
   - **Note:** evaluate() has no notion of current holdings. It re-emits for every diverging race every 30s.
     Re-adds are stopped only by the gate (`no_room_to_add_exposure` once at the per-market cap).

## Exit — `CrossMarketProvider.exit_decision(ex, pnl_frac, is_long)` (overrides the default price stop)
Applies to every position on an active race. That includes legacy sniper positions and any MM inventory on
those markets. Runs each cycle and on the 45s exit timer.
1. `pnl ≤ −exit_hard_stop_pct` (0.25) → **hard_stop_backstop**.
2. No fresh benchmark or SIG price → HOLD.
3. `|gap| ≤ exit_convergence_tol` (0.01) → **convergence_take_profit**.
4. `thesis_ok = gap>0 if is_long else gap<0`. If OK → HOLD.
5. Adverse but `|gap| ≤ exit_thesis_reversal_tol` (0.05) → HOLD (noise band, the whipsaw fix).
6. Otherwise → **thesis_reversed** → sell the held leg.

### ⚠ BUG (2026-10-05, not fixed): shorts are judged as longs
`is_long` comes from the venue `option` field, which is always "YES". For a held-NO position (bought because
SIG > sharp, so gap < 0), step 4 tests `gap > 0`, which is false. Then any |gap| > 0.05 fires `thesis_reversed`.
That is the same condition that made cross_market enter. Result: **enter buy-NO → exit sell-NO → re-enter**,
paying the spread every lap.
- Evidence: 967 NC-sen R (691 cross orders / 653 exits) and 1061 (1,251 / 905). All their exits are `held=no reason=thesis_reversed`.
- Long positions are unaffected.
- **Fix**: pass `is_long = qty > 0` in `orchestrator._run_takeprofit_exits_inner` (the fallback path already does this).
  Expect realized churn to drop sharply once it's in.

## Sniper — folded into cross-market (NOT a separate edge)
`strategies/sniper.py` + `CoreSniperStrategy` (strategy_id `sniper`) were the "take stale SIG quotes vs the sharp"
idea: lift an ask < sharp − min_edge (buy YES), hit a bid > sharp + min_edge (buy NO), leashed by `max_cross`.
It turned out to be cross-market executed as churn. Measured ~−1.2c/take at short horizons, because SIG
doesn't snap to Polymarket in minutes; convergence happens over hours to settlement. So:
- Live: `sniper.enabled: true, place: false, record: true`. It still runs every fast cycle and logs `sniper_take`
  plus `data/sniper_shadow/snipes.jsonl`, but **places nothing**.
- Its legacy positions (e.g. 840 House-D, 1052, 994) sit on active races, so the cross-market `exit_decision` manages them.
- Dead config: the sniper keeps allocator prior 0.40 and pool eligibility. Neither does anything with place:false.
  The allocator is bypassed by the pool anyway.

## Live config (overlay)
`cross_market: enabled true, poll_interval_s 1, poll_concurrency 20, min_divergence 0.05 (ignored),
conviction_weighting true, conviction_cost_floor 0.05, wiki_enabled false`. Defaults are in effect for:
`cadence_s 30, sig_stale_s 300, external_stale_s 30, exit_convergence_tol 0.01, exit_hard_stop_pct 0.25,
exit_thesis_reversal_tol 0.05, target_gap 0.20`. Exit params need a restart; they're not hot-reloadable.

## Current book & performance (2026-10-05)
- 10 positions tagged cross ($61.4k MV, +$961) + 6 legacy sniper ($21.7k, +$323). All favorites around 0.85–0.92 except
  967 (short R at 0.13) and 885 (TX-gov R 0.705, −4.3%, the sharp itself slid −8pt/day on 10-04).
- Top 5 positions are each 12.6–13.6% of NAV (they grew past 12% at mark; the cap is on cost).
- Most of the realized ≈ −$14k is spread paid on ~$1.5M+ of cross+exit turnover. Much of it is likely the short-side churn bug above.

## Risks / open questions
- Correlated national-swing tail: all favorites, long-only, no hedge (Polymarket can't be traded).
- Convergence to settlement is still unproven. SIG and Polymarket have persistently disagreed (the wiki lesson:
  persistence could also mean SIG is right).
- Thin peer-to-peer book: large positions may not be exitable before settlement (fine if held to settlement).
