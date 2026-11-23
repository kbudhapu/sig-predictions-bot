# mean_reversion — minute-scale MR as a TAKER (mr_taker) + as an MM maker-skew

Two separate pieces share the idea that a mid that overshoots its ~60-min mean by ≥2σ tends to revert.
Evidence base: ONE day of tape (venue keeps ~24h only), W≈60 / k≈2σ looked like Sharpe ~6.7. **Unvalidated.**

## A) MR maker-skew (inside market_making). LIVE, active
`strategies/market_making/mr_skew.py`. It tilts the MM quote center ±1 tick toward reversion when |z| ≥ 2
(z over a 3600s mid buffer, min 20 samples), staying inside the touch. The buffer persists across restarts.
Config: `market_making.mr_skew_enabled true, mr_window_seconds 3600, mr_k_enter 2.0, mr_max_ticks 1`.
See `market_making.md`.

## B) mr_taker. Registered, signalling, but **sizes to zero**
strategy_id `mean_reversion`, tier SIGNAL, strategy_type `mean_reversion` (→ directional sizer), cadence 5s.
Files: `strategies/mr_taker.py` (pure `MRTakerEngine`), `CoreMeanReversionTakerStrategy` in `sig/engine/live_strategies.py`.

### Logic
- Markets: `mr_taker.markets` = [843 Senate-R, 841 House-R, 840 House-D, 948]. These are the tight-spread liquid set.
- Each cycle: observe the mid, z = (mid − mean_60min)/sd.
- Flat and spread ≤ 0.012:
  - z ≥ +2 → `enter_short` = buy NO at 1−best_bid (marketable).
  - z ≤ −2 → `enter_long` = buy YES at best_ask.
- Holding: exit (reduce-only sell of the held leg) when |z| ≤ 0.5 (reverted), when z crosses zero against us (flipped),
  or after 3600s (time stop).
- Sizing: σ_p = `sizing.k_reference_sigma` (0.06) → k = 0.25, × default Kelly mult 1.
  Budget = available cash − $15k pool reserve (NOT pool-eligible).

### ⚠ Defects (2026-10-05, not fixed)
1. **Starved**: with cash ≈ $900, the budget is `max(900 − 15000, 0) = 0`, so every signal ends in
   `order_path_zero_size allocator_budget=0.0` (seen live 06:17 and 07:05 on 840/841). It effectively hasn't traded since
   the pool reserve went in. Lifetime: 35 orders, 41 fills, ~$829 notional.
2. **`on_fill` is never called** by the orchestrator/OMS (grep: no caller anywhere). The strategy's own position tracker
   `_pos` therefore stays 0 forever. It believes it is always flat: it never sends its own exits and would re-enter on
   every |z|≥2 signal. Any MR position closes only via the shared exit pass. 840/841/843 are cross-market races, so that
   means the cross thesis exit.
3. It shares markets with MM and cross_market, so positions commingle at the venue (attribution is fuzzy).

### Verdict
Dormant in practice. To actually run it, it needs pool eligibility or a smaller reserve, and the `on_fill` wiring
(or tracking its position from the venue). It was never validated beyond 1 day, so leave it starved unless re-validated
on `mid_history` (14-day retention now accumulating; `tools/mr_backtest.py`).
