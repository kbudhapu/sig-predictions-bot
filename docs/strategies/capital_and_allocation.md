# Capital, allocation & sizing — what ACTUALLY decides how much each strategy gets (2026-10-05)

TL;DR: the allocator priors in the overlay are mostly **decorative**. With `capital.pool_edge_reserve: 15000` set,
the per-intent path ignores the allocator. What really decides capital:
- **pool-eligible edges** (`sniper`, `cross_market_divergence`, `dutch_book`) can take ALL free cash;
- **everyone else** gets free cash − $15k (= $0 when cash < $15k);
- **the risk gate's per-market cap (12% of NAV, on cost basis)** is the real position sizer.

## Pieces
| piece | file | role now |
|---|---|---|
| Allocator (static) | `strategies/allocator/allocator.py` (`allocator.static: true`) | weights = overlay priors normalized to `max_deploy_frac` (1.0). **Only consumed by the MM multi-leg group path** (`budget_for('market_making')`) |
| AllocatorAdapter | `sig/engine/composition.py` | budget_for = weight × `capital`. **Capital is the equity at bot START** (frozen until restart) |
| Capital pool | `orchestrator._process_intent` | with reserve > 0: pool-eligible → all available cash; others → max(cash − 15000, 0) |
| CapitalLedger | `sig/engine/capital_ledger.py` | per-drain reservations against ONE `/account` snapshot (no double-spend). `begin_cycle` runs in the placement worker |
| SizingAdapter | `composition.py` | directional only: `frac = min(min(2.0, max(1,mult)), k(σ)·mult·Browne_M)`; `notional = min(frac·budget, cash)` |
| Risk gate | `strategies/risk_gate/gate.py` | caps on COST basis vs live NAV: deploy 1.0, **per-market 0.12**, cluster 0.20 (inert: no clusters tagged), `min_order_qty` 10, `hard_notional_frac` |
| MM own sizing | `strategies/market_making/sizing.py` | `mm_capital_frac 0.35 · equity / n_quoted`, per-quote ≤ 0.08·equity, inventory ≤ 0.05·equity |

## Overlay priors (static) vs reality
| edge | prior | does the prior matter? | actual capital driver |
|---|---|---|---|
| sniper | 0.40 | no (place:false) | — |
| cross_market_divergence | 0.25 | no (pool) | all free cash → per-market cap 12% NAV |
| market_making | 0.20 | **yes**, for grouped quotes (~0.19 × start-equity) | ∩ free cash (~$900) ⇒ ~$28/group now |
| dutch_book | 0.10 | no (pool, multi-leg) | never fires (min_edge 0.06) |
| mean_reversion | 0.05 | no (reserve) | cash − 15k = 0 ⇒ zero size |
| order_flow | 0.02 | no (reserve) | 0, and never signals |
| arb / resolution / joint / extremizing / FL | ~0 | — | arb is multi-leg; the others aren't registered |

## Sizing knobs (overlay)
`sizing.kelly_frac_ceiling 2.0`, `kelly_multipliers: cross_market_divergence 4.0, sniper 4.0` (committed: order_flow 1,
resolution 1, default 1). k(σ) grid σ [0.02,0.06,0.12,0.25] → k [0.50,0.25,0.10,0.04]. `k_reference_sigma` 0.06 is
used as σ_p by MM / order_flow / MR / sniper.
Browne M: no 3rd-place threshold is passed, so M is likely None ⇒ ×1. The gate also gets no tournament state,
so ρ=τ=1 and there's no endgame override.

## Consequences (current book)
- Cash $918 / NAV $87.6k: **fully deployed**. New cross entries only happen when exits free cash.
- Cross positions sit at ~12% NAV each (per-market cap on cost; several are 12.6–13.6% at mark).
- MM, MR, order_flow and event_trigger are cash-starved by design of the pool + reserve.
- `max_deploy_frac 1.0` = no cash reserve for the book overall. The $15k "edge reserve" only ring-fences cash *between*
  strategies; it doesn't keep cash idle.
- Hot-reloadable without restart: gate caps (`max_deploy_frac`, `max_per_market_frac`, `max_cluster_frac`,
  `min_order_qty`) and sniper/book_pressure tunables. Kelly multipliers, priors, pool reserve, and strategy
  enable/registration need a restart.
