# Shadow / record-only components (place nothing)

These run live but never place orders. They write jsonl for offline validation. Row counts are as of 2026-10-05.

| component | strategy_id | what it records | file (rows) | can it trade? |
|---|---|---|---|---|
| **sniper** | `sniper` | stale SIG touch vs sharp (≥ `min_edge` 0.06): would-be take side/price/edge | `data/sniper_shadow/snipes.jsonl` (419,809) | only if `sniper.place: true`. **Folded into cross-market; keep OFF** (see `cross_market.md`) |
| **book_pressure** | `book_pressure` | queue imbalance QI=(bid−ask)/(bid+ask) over top-5 levels, \|QI\| ≥ 0.33, depth ≥ 100 → predicted side | `data/book_pressure_shadow/signals.jsonl` (65,044) | **No, by construction.** `evaluate()` always returns `[]`, even with `place: true` |
| **dutch_book recorder** | `dutch_book` | every would-be complement arb | `data/dutch_book_shadow/arbs.jsonl` (9,252) | yes, the strategy itself places when `place: true` (see `dutch_book.md`) |
| **mm_shadow** | (task) | vol-scaled MM half-width vs fixed k (tape toxicity off) | `data/mm_shadow/decisions.jsonl` (7,832) | no |
| **favorite-longshot scan** | (cron 13:00 UTC) | `tools/favorite_longshot_scan.py` via `tools/fl_shadow_cron.sh` | `data/fl_shadow/*.log` | no |

Inputs: sniper and book_pressure use the bounded L2 set (`order_flow.markets`, 8 markets) from `_orderbook_pump`.
Sniper triggers on NBBO for all 119 active cross races.

## Validators (read-only tools, run on the droplet)
`tools/sniper_validate.py`, `tools/book_pressure_validate.py`, `tools/mm_shadow_validate.py`, `tools/mm_fill_efficiency.py`,
`tools/position_classify.py`, `tools/strategy_pnl.py` (strategy_pnl is limited by the unsigned/unattributed ledger,
see `00_pipeline.md`). Run with: `PYTHONPATH=/opt/sig-bot .venv/bin/python tools/<x>.py`.

## Housekeeping note
`snipes.jsonl` grows ~400k rows in ~2 days because the sniper evaluates 119 races every 5s. It's harmless, but disk
on the 1GB droplet is worth watching. Setting `sniper.record: false` would stop it (hot-reloadable).
