# 00 — The shared pipeline (how ANY strategy's idea becomes a venue order)

Read this first. Every strategy only *proposes* `Intent`s. Sizing, gating, placement, fills and exits are shared
machinery, and most live behaviour (good and bad) lives here, not in the strategies.
Verified against code at `7087618` on 2026-10-05.

## Processes on the droplet (4 systemd units)
| unit | command | role |
|---|---|---|
| `sig-bot.service` | `main_sig.py run` | **the trader**: all strategies, orchestrator, exits, background loops |
| `sig-bot-feed.service` | `main_sig.py feed` | Supabase-Realtime WS price feed, sharded over ~3 sockets to cover all 237 markets, plus a 60s REST safety-net poll. Writes `prices` in `data/state.db` |
| `sig-bot-supervisor.service` | `main_sig.py supervisor` | monitor only: logs stale-price markets and heartbeat age every 30s |
| `sig-bot-signal.service` | `main_sig.py signal` | **idle**: heartbeat only ("signal sources not wired") |

The trader reads prices from the shared SQLite store and never polls prices itself (`--self-feed` is off).

## Trader startup (`main_sig.cmd_run`)
1. `load_config()`: committed `config/sig.yaml`, with the gitignored `config/sig.local.yaml` overlay deep-merged on top.
2. Discover the 237 exchange_ids, read `/account` balance as equity, and resolve the tournament id (orders carry `tournamentId`).
3. `build_live_stack` (sig/engine/composition.py) builds: SigBroker, StateStore adapter, OMS, RiskGate adapter,
   Sizing adapter, Allocator adapter, Orchestrator.
4. Register strategies, each behind its flag: MM (always), arb (always in LIVE), order_flow, mr_taker,
   dutch_book, cross_market (+ the thesis-exit override), sniper, book_pressure, event_trigger.
5. Startup hard gate (OMS pre-flight reconcile), then background tasks: price pump (5s, also pets the
   watchdog), orderbook pump (bounded L2 set), cross-market Polymarket poller (1s), event-trigger poller,
   position resync (60s), rolling-buffer snapshot (30s), config hot-reload (10s), mid_history (60s),
   fill ingest (60s), attribution flush (30s), mm_shadow (90s).

## The cycle (`Orchestrator._run_cycle`, tick 5s, each cycle ~4–5s)
In order:
1. `/account` snapshot (20s TTL cache; on a 429 it reuses the last snapshot).
2. Every 20s, `_sync_venue_positions`: venue positions are written into the local store with SIGNED qty
   (+ = YES, − = NO), and any local row not at the venue is zeroed.
3. OMS `advance_pending_bundles` + `sweep_unfilled_timeouts` (30s timeout for non-MM bundles).
4. Feed-staleness check: a stale market gets no new orders (cross_market can opt out via `allow_stale_market`).
5. `_sync_gate_positions`: holdings are pushed into the gate so sells count as reductions.
6. **`_run_takeprofit_exits`**: the directional exit pass (below). It also runs on its own 45s timer.
7. `_run_exits`: arb-basket recycling (basket exits; see `arb.md`).
8. `_run_mm_requote`: cancels resting MM **bids** that drifted more than 5% below the touch.
9. Every strategy that is due by cadence: `evaluate(snapshot)`, then its intents go to the placement queue.

## Placement worker (`placement_worker: true`)
The cycle only enqueues. One sequential worker drains the queue at venue pace:
- Drops intents older than 30s (TTL).
- **Value-priority sort** (`_intent_priority`):
  `exits 10000+ > dutch_book 9000+ > arb 8500 > sniper 8200+ > cross_market 8000+1000·|div| > order_flow 6000 > mean_reversion 5000 > MM new ~100 > MM requote ~50`.
- Keeps the top `placement_max_per_drain` (20); the rest are "deprioritized" and dropped. They re-propose next cycle.
- MM quotes are grouped up to 10 per `POST /orders/multi-leg`, which uses 1 write for 10 quotes.
- Venue limits: **30 writes/min**, **100 reads/min** per key, and the venue serializes ~5–9s per order, so
  realistic throughput is ~10–13 placed orders/min.

## Per-intent path (`_process_intent`)
1. Blocked if the OMS has a **pending (non-MM) bundle for the same strategy_id**. So cross_market has only
   ONE order in flight at a time (the `order_path_blocked_pending_bundle` lines).
2. Blocked if the WriteGate is out of budget.
3. **Budget**: with `capital.pool_edge_reserve > 0` (live: 15000) the allocator is bypassed. Pool-eligible
   strategies (`sniper, cross_market_divergence, dutch_book`) may use ALL available cash. Everyone else may use
   available cash minus $15k.
4. **Sizing**:
   - `arb`: qty comes from the intent.
   - `market_making`: quote qty from the quoter, clamped to the budget.
   - everything else (`directional`): `SizingAdapter.size`:
     `frac = min(ceiling, k(σ_p) · kelly_mult · browne_M)`, with `ceiling = min(kelly_frac_ceiling, max(1, mult))`,
     and `notional = min(frac · budget, available_cash)`.
     k(σ) grid: σ 0.02→0.50, 0.06→0.25, 0.12→0.10, 0.25→0.04. Live: cross mult 4.0 and ceiling 2.0.
     **So for cross_market the sizer effectively requests ALL available cash, and the risk gate is the real sizer.**
5. Ledger reserve, then the **risk gate** (`strategies/risk_gate/gate.py`):
   - Erroneous-order rejects: off-tick price, price outside [0.005, 0.995], non-integer qty, notional above `hard_notional_frac`·balance.
   - A `sell` is cap-exempt but clamped to held qty. A sell with no holding is rejected (`sell_without_holding`).
     Exposure-adding views therefore go in as `buy` YES or `buy` NO.
   - A `buy` is clamped by: free = NAV − Σcost, aggregate (max_deploy_frac·NAV − Σcost), **per-market
     (max_per_market_frac·NAV − this market's cost basis)**, and cluster.
   - **Cluster cap is inert**: nobody tags clusters, so each market is its own cluster.
   - Caps use **cost basis**, not mark. A winner can exceed 12% at mark; it just can't be added to.
   - `balance` = live NAV (`broker.last_equity`).
   - No tournament state is passed, so ρ=τ=1 and the endgame override never engages.
6. OMS `submit` → broker `place_order` / `place_multi_leg`. The pipeline's `side` (buy/sell) maps to SIG
   `action`, and `outcome` (yes/no) maps to SIG `side`.

## Fill policies (OMS)
| strategy_type | policy | behaviour |
|---|---|---|
| market_making | LEAVE_RESTING | quotes rest indefinitely; never timed out |
| directional / order_flow / resolution (cross_market, sniper, MR, exits) | CHASE | after 3 unresolved cycles: cancel and re-quote 1 tick toward liquidity (max 3 attempts), then accept partial |
| arb | UNWIND_ALL_LEGS | all-or-nothing; a partial fill is unwound |
| other | ACCEPT_PARTIAL | |

## Directional exit pass (`_run_takeprofit_exits_inner`), every cycle + every 45s
For every venue position (via `get_positions_valued`, 20s TTL cache), skipping arb-basket legs:
1. If the **cross-market override owns the exchange** (an active race in `race_mapping.yaml`), it decides.
   See `cross_market.md`.
2. Otherwise the default price stop applies (`directional_exit`):
   - stop-loss at −20%.
   - Trailing take-profit arms at +50% peak, then sells on a 20%-of-peak give-back.
3. The close is **SELL the held leg**: held side = sign(qty), and qty = |held|, so it is reduce-only.
   Price crosses the live book: held YES sells at min(mark−0.02, best_bid); held NO sells at min(mark−0.02, 1−best_ask).
   Fill policy CHASE, strategy_id `exit:<exchange>`.

## KNOWN DEFECTS in the shared pipeline (2026-10-05, not fixed)
1. **Short positions are evaluated as LONG by the cross-market exit override.** The orchestrator passes
   `_is_long = option not in ("no","short")`, but the venue `option` is ALWAYS "YES" (12/12 shorts on 10-05).
   Held-NO positions get exited as `thesis_reversed` whenever |gap| > 0.05, and cross_market then re-enters.
   This is the 967/1061 churn loop. The fix is to use sign(quantity), which the same function already does for `held_outcome`.
2. **The local `orders` table mislabels exits.** Exit legs carry `action="close"`, and
   `StateAdapter.write_order` coerces any non-buy/sell action to `"buy"`. So exit SELLS are stored as `buy <held outcome>`.
   Never derive fill direction from local orders.
3. **`strategy_fills.quantity` is stored unsigned** (`abs`), so the ledger loses direction. Per-strategy realized P&L can't be rebuilt.
4. **MM multi-leg slices have no venue order id.** Those bundles can never resolve, so they stay in OMS `_pending`
   forever. This is the source of the ~66k `bundle_awaiting_fill` + ~33k `market_making_leave_resting` debug lines per 30 min, and unbounded memory growth.
5. **Cross_market can only have 1 order in flight** because of the pending-bundle guard per strategy_id. Every other
   diverging race waits (`order_path_blocked_pending_bundle`).
