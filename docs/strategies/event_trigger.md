# event_trigger — abnormal SIG move × GDELT news volume (follow / fade). Registered, INERT

strategy_id `event_trigger`, tier TAIL, strategy_type `directional` (Browne sizer, CHASE), cadence 300s.
Lifetime: **0 orders**. Last 60 min on 2026-10-05: 0 abnormal moves, ~9 GDELT fetch failures.

## Thesis
When a SIG market makes an abnormal move:
- **with** a news-volume spike on that race (GDELT) → JUSTIFIED → FOLLOW the move.
- **without** news (especially a reverting spike) → OVERDONE → FADE it.
Direction comes only from the move plus justified/overdone, never from reading news content. Where a Polymarket
sharp exists, it is used as a confirm/overshoot check.

## Files
- `strategies/event_trigger/strategy.py` `EventTriggerStrategy`: builds the intent. σ_p maps from confidence,
  with a wide band (small bets).
- `signals/event_trigger.py` `classify_event`: the follow/fade + confidence rules (`min_confidence` 0.15, move_ratio cap 6).
- `signals/abnormal_move.py`: move detector types.
- `signals/news_volume.py`: GDELT DOC 2.0 `timelinevol` (free, no key, ≤1 req/5s).
- **`sig/engine/event_trigger_provider.py` — ⚠ EXISTS ONLY ON THE DROPLET, UNTRACKED (never committed, 254 lines,
  2026-10-02).** `main_sig.py` imports it. A fresh clone, `git clean`, or re-provision would make registration fail
  (`event_trigger_registration_failed`; the rest of the bot keeps running). **Commit it or delete the strategy.**

## Provider logic (droplet file)
- Builds race keys from SIG titles ("Will the X Party win the <state> <office>" → `office:state`) for all parseable markets.
- `run_poller`: every `price_interval_s` (20s) it appends each market's mid from the shared store to a 14-sample window.
  It also round-robins ONE GDELT query per `news_rate_limit_s` (5s), so the full sweep takes ~n_races×5s (≈20+ min).
- Abnormal move = |Δmid| ≥ `min_move` (0.03) AND ≥ `move_mult` (3) × the mean step size of the window.
  Firing markets get ONE `/price-history` call for a trade-count burst. That endpoint returns no data on this venue,
  so the burst never confirms.
- The sharp comes from `xm_provider.aligned_benchmark` where the race is mapped.

## Why it's inert
- Election mids rarely jump ≥3c between 20s samples with 3× their own baseline, so it almost never fires.
- GDELT intermittently returns HTTP errors or non-JSON.
- Not pool-eligible, so any signal would size to zero while cash < the $15k reserve.
- `/price-history` is dead on this venue, so trade-count enrichment never confirms.

## Live config
Overlay `event_trigger: enabled true`. Everything else is committed defaults (window 14, min_candles 6, move_mult 3.0,
min_move 0.03, enrich_tradecount true, news_rate_limit_s 5, news_mult 3, cadence_s 300, tier TAIL). No allocator prior entry.

## Verdict
Low-value tail edge, dormant. Either commit the provider file and keep it as a cheap watcher, or disable it.
