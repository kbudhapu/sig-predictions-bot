# market_making — spread capture on the full board (+ MR maker-skew)

**Status (2026-10-05): LIVE, always registered** (no enable flag). strategy_id `market_making`, tier CORE,
strategy_type `market_making`, cadence 30s, fill policy LEAVE_RESTING.
Book: 98 small positions, ~$3.6k MV, +$517 unrealized. Each multi-leg group currently commits only ~$28
because free cash is ~$900.

## Thesis
Rest two-sided limit quotes and earn the spread when both sides trade. NO view on fair value: size depends only on
capital, liquidity and inventory, never edge or conviction (edge-scaled MM caused the old 13,144-contract blowup).

## Files
- `sig/engine/live_strategies.py` `CoreMarketMakingStrategy`: market selection, venue-truth inventory, emits intents.
- `strategies/market_making/market_maker.py` `MarketMaker.build`: per-market pipeline:
  quoter → base size → inventory cap/skew → adverse selection → MR skew → orders.
- `quoter.py`, `sizing.py`, `inventory.py`, `adverse_selection.py`, `mr_skew.py`, `vol_spread.py` (shadow only).

## Per-cycle logic
1. **Market selection**: all discovered exchange_ids (474 configured; `market_making.markets` can override).
   Quote only rows with a two-sided touch younger than `max_quote_age_s` (120s). Logged as `mm_market_selection`.
2. **Quote price** (`compute_quote`): mid ± `quote_k` (0.015), snapped to 0.005 and kept strictly inside the touch.
   If the spread is under ~3 ticks, **join the touch** (`join_when_tight: true`: bid = best_bid, ask = best_ask).
   Most SIG spreads are 1–2 ticks, so most quotes join the touch.
3. **Base size** (`base_quote_size`): `min(mm_capital_frac·equity / n_quotable, mm_max_quote_frac·equity) / price`,
   floor `min_quote_size` 10. Live: 0.35·~87.5k / n, so roughly $150 per quote at n≈200.
4. **Inventory** (`InventoryBook`, synced each cycle to the **venue position for that market**):
   - Cap = `mm_max_inventory_frac (0.05)·equity / price` shares (~5k shares at 0.87).
   - Skew: shift both quotes by `−4 ticks × pos/cap` (clamped ±1). Size mult: reducing side ×(1+0.6·frac),
     adding side ×(1−0.6·frac). At/over the cap the adding side is pulled.
   - **Ask = SELL YES, bounded by held YES.** MM only quotes the YES outcome, so it can never open a short. A held-NO
     (qty<0) position makes the bid the reducing side.
5. **Adverse selection**: if the mid moved ≥ 0.03 within 30s, pull the side being picked off.
6. **MR skew** (`mr_skew_enabled: true`): z-score of the mid over a 60-min window. When |z| ≥ 2, tilt both quotes 1 tick
   toward reversion, staying inside the touch. The buffer persists across restarts (`buf:market_making` in state_meta).
7. Emit single-leg intents (side=buy/sell, outcome yes, `mm_quote_qty`).

## Execution path
- Priority ~100 (new) / 50 (requote), the lowest in the queue. Up to 20 intents per drain, so MM gets whatever slots
  exits/cross/etc. leave free.
- Grouped up to 10 legs per `POST /orders/multi-leg` (`_process_mm_quote_group`). **The budget here comes from the
  allocator** (`budget_for('market_making')`, static prior 0.20 of capital), intersected with available cash.
  Unlike the single path, the pool reserve does not apply here.
- Each quote is gated (buy: caps; sell: cap-exempt, clamped to held).
- `_run_mm_requote` (orchestrator): each cycle, cancels resting MM BIDS that drifted >5% below the current best bid.
  Asks are never re-pegged.
- Multi-leg responses carry no per-leg order ids. Consequences: (a) fills are UNATTRIBUTED in `strategy_fills`;
  (b) the bundles can never resolve, so they sit in OMS `_pending` forever (log spam + memory growth).

## ⚠ CONFLICT WITH CROSS-MARKET (2026-10-05, code-confirmed, not fixed)
MM's inventory is the TOTAL venue position in a market, so it includes cross_market's convergence bets.
Every big cross position (12–14k shares, ≈12% NAV) is far over MM's 5%-of-equity cap. MM therefore:
- stops bidding,
- shifts its quotes **−2¢** (4 ticks), which can put the ask below the best bid (marketable),
- posts asks at 1.6× base size to **sell down cross-market's position**.
For cross shorts (held NO) it does the mirror: raised bid, buys back YES.
- Evidence: MM groups quote exactly the cross markets (967, 885, 880, 847, 1062, 1059, 1052…). Since
  2026-10-04 08:00 there have been **4,142 unattributed MM multi-leg fills (~$45k notional) on cross markets**
  (top: 1062 $9.7k, 1059 $8.2k, 987 $7.6k, 885 $5.9k).
- Cross then re-buys (gate room reopens), so the two strategies trade against each other and pay spread both ways.
- Fix options: exclude cross-owned exchanges from MM (`market_making.markets`, or skip markets where
  `xm_provider.owns_exchange`), or have MM track only its own inventory.

## Live config (resolved)
`mm_capital_frac 0.35, mm_max_quote_frac 0.08, mm_max_inventory_frac 0.05, max_frac_of_depth 0.25 (depth unknown →
skipped), max_quote_age_s 120, cadence_s 30, requote_threshold 0.1 (orchestrator uses mm_requote_threshold default
0.05), min_quote_size 10, quote_k 0.015, join_when_tight true, size_skew_strength 0.6, skew_ticks_at_cap 4,
mr_skew_enabled true, mr_window_seconds 3600, mr_k_enter 2.0, mr_max_ticks 1`. Adverse uses defaults
(0.03 / 30s / pull). Allocator prior 0.20.

## Shadow research (record-only, never touches quotes)
`_mm_shadow_loop` (main_sig) runs every 90s over the 8 `order_flow.markets`. It computes vol-scaled half-width
(`vol_spread.py`) from `mid_history` and writes `data/mm_shadow/decisions.jsonl`. Tape toxicity is off
(`toxicity_enabled: false`, it costs reads). Promote-gate: `tools/mm_shadow_validate.py`.
Tool: `tools/mm_fill_efficiency.py` (per-market fills × exec edge).

## Performance notes
- Historically the only strategy with consistently positive execution edge, but tiny and fill-limited.
- Open MM inventory: +$517 on $3.1k cost (FL-13 D +$249, RI-gov Ind +$199).
- Realized: unknown. Multi-leg fills are unattributed and the ledger is unsigned.

## UPDATE 2026-10-05 ~07:40Z — fixes deployed (c011c2b) + research readout
- **Cross overlap fixed (dynamic):** `market_making.cross_overlap: dynamic` (default). MM quotes cross races normally,
  EXCEPT where the venue position's held-leg notional > `mm_max_inventory_frac`·equity. That's the only zone where MM
  would misread cross's bet as its own inventory. Live right after deploy: excluded 11–12 markets, quotable 225.
  `_run_mm_requote` cancels MM resting orders on conflicted markets (orders known to be non-MM are spared:
  OMS in-flight, order_attribution, broker buffer). At deploy there were 0 MM orders resting there, so 0 cancels.
  Modes: `exclude` = never quote cross races; `allow` = old behaviour.
- **MR maker-skew premise (mid_history, 44.6h, 239 markets, 631k samples):** |z|≥2 vs the 60-min mean fires 9.1% of
  the time. After a trigger, the mid reverts on average +0.07c (5m), +0.11c (15m), +0.13c (60m). Revert:continue =
  0.28:0.14 (5m), 0.45:0.30 (60m). Premise **holds weakly**: the direction is right but the magnitude is small vs the
  1-tick (0.5c) tilt. MM P&L impact can't be measured (multi-leg fills are unattributed and the ledger is unsigned).
- **Vol-spread shadow (7,968 decisions, 8 markets):** 0 volatile regimes. vol_k is ~0.006 tighter but coverage drops
  0.99→0.89 at 15 min. It's also nearly moot: most spreads are 1–2 ticks, so `join_when_tight` quotes AT the touch and
  `quote_k` rarely binds. **Do not promote.** The toxicity half never ran (`toxicity_enabled: false`).

## UPDATE 2026-10-05 08:28Z — MM improvements DEPLOYED (1317d3c)
| # | change | config (overlay) | status |
|---|---|---|---|
| 1 | **Sharp-anchored quoting**: on cross races use the aligned Polymarket price as fair value. Fair-value guard (no bid > sharp−margin, no ask < sharp+margin) + size lean toward the convergence side (`1 ± sharp_lean_strength·clamp((sharp−mid)/sharp_lean_gap)`), sells still ≤ held | `market_making.sharp_anchor: shadow` (off/shadow/live), `sharp_anchor_margin` 0.005, `sharp_lean_strength` 0.5, `sharp_lean_gap` 0.05 | **SHADOW**: records would-be drops to `data/mm_shadow/sharp_anchor.jsonl`, changes nothing. First cycles: 72 races with a sharp, ~47 bids and ~1 ask would be dropped (MM's YES bids sit ABOVE Polymarket on most cross races). Promote gate: `tools/mm_sharp_anchor_validate.py` (dropped bids are good if the mid then fell, asks if it rose; need positive mean + good > bad at 15–60m over a few days) |
| 2 | **Measurable MM**: (a) `strategy_fills.signed_qty` (schema v3; YES-delta = +q for outcome yes, −q for no); (b) MM multi-leg leg ids recovered from one open-orders read per attribution flush (`leg_ids_recovered`) | — | LIVE: 25 MM legs claimed in the first 5 min. Legs that fill before ever resting stay unattributed |
| 3 | **MM cash carve-out**: pooled edges stop short of `mm_cash_reserve_frac`·NAV; MM may spend it (+ anything above both reserves); non-pool strategies get what's above both | `capital.mm_cash_reserve_frac: 0.05` (~$4.4k) | LIVE. Fills as exits free cash (cash was ~$900 at deploy, so groups are still $9–22 for now) |
| 4 | **Toxicity shadow on** (8 bounded markets, 1 tape read each per 90s) | `mm_shadow.toxicity_enabled: true` | LIVE. Validate with `tools/mm_shadow_validate.py` after ~1 day |
Health after deploy (5 min): 44 cycles, 0 cycle_failed, 0 × 429, 0 short-churn exits.
Rollback: overlay backups `config/sig.local.yaml.bak.predeploy-1317d3c` / `-c011c2b`; code `git revert`.
