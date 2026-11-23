# SIG Predictions Cup — Integration / Architecture Spec
### How the components connect into one coherent bot: the loops, the cycle order, the handoff contracts

**Status: ARCHITECTURE SPEC.** This is the connective tissue that turns the component specs
(`SIG_TOURNAMENT_SIZING_SPEC.md`, `SIG_RISK_GOVERNANCE_SPEC.md`), the ported mbappe plumbing, the arb logic,
and the (future) research signal into a single running bot. It defines the loops, the order of operations in
a cycle, the cadences, the handoff contracts between modules, and the shared-state model. It is market-
structure-independent (designable now) — the *values* and the research/arb internals come later, but the
*skeleton* is fixed here.

**Design constraint carried throughout:** the bot must run first against mbappe's `SimulatedBroker`
(sim/paper) and only then against live SIG — so **sim is a first-class mode**, selected by config, with the
identical loop/architecture. "Run simulations through it" is the validation path, and the architecture must
support it by construction (the `BrokerAPI` Protocol already makes the broker swappable).

---

## 1. The component map (what exists and where it plugs in)

```
                         ┌─────────────────────────────────────────────┐
   CONTINUOUS            │  Realtime feed (WS)  →  updates LOCAL STATE   │  (eyes: always updating)
   (always running)      │  Layer-C SUPERVISOR  →  watches for runaway   │  (independent brakes)
                         └─────────────────────────────────────────────┘
                                          │ reads/writes
                         ┌─────────────────────────────────────────────┐
   SHARED LOCAL STATE    │  SQLite (positions, orders, fills, prices,    │  (the single source of truth
   (ported mbappe)       │  books, pnl, leaderboard, research signals,   │   the loops coordinate through)
                         │  audit log)  +  AsyncDBWriter WAL             │
                         └─────────────────────────────────────────────┘
          ┌──────────────────────────┼───────────────────────────┐
   FAST LOOP (arb-watch)      SLOW LOOP (strategy)          ASYNC COMPONENTS
   check constraints          size directional book         research signal → (p, σ_p) per market
   → arb-size (fillable)      → Browne tournament-size       observability → logs/alerts/cause-attribution
   → gate → execute           → risk-resize → gate → execute
   → reconcile                → reconcile
```

- **Plumbing (ported mbappe):** the state store (SQLite + WAL + AppendOnlyStore), the execution/idempotency
  core, the reconciliation engine, the resilience (systemd/run-loop/flock), the alerting, the FastAPI
  sidecar. The substrate everything runs on. `SigBroker` (behind `BrokerAPI`) is the live adapter;
  `SimulatedBroker` is the sim adapter — swappable by config.
- **Research signal (async):** a separate component producing `(p, σ_p)` per market into local state; updates
  periodically. *Designed later (market-dependent).* Until it exists, only the arb/flow path trades.
- **Sizing module (`SIG_TOURNAMENT_SIZING_SPEC.md`):** called in the SLOW loop's decide step for the
  directional book. NOT used by the arb path (arb is near-riskless, §4).
- **Risk governance (`SIG_RISK_GOVERNANCE_SPEC.md`):** Layer A (deterministic gate) in the execution path of
  *both* loops; Layer B (resizing) in the SLOW loop's sizing path; Layer C (supervisor) continuous and
  independent.

---

## 2. The two loops (different cadences — this is the key architectural choice)

The two trading modes have fundamentally different time signatures, so they run as **two loops at different
frequencies**, sharing the state/gate/execution substrate but on different clocks:

- **FAST LOOP — arb-watch.** Runs frequently (e.g. every few seconds, bounded by the read limit). Its job:
  catch constraint violations *while they are still fillable* and capture them. Time-sensitive — an arb that
  appears must be acted on before it is competed away. It does NOT re-evaluate the directional book.
- **SLOW LOOP — strategy.** Runs periodically (e.g. every few minutes). Its job: re-evaluate the directional
  conviction book against the current tournament state (rank, ρ, τ) and the research signals, resize, and
  adjust positions. Deliberate, not time-critical — directional convictions are held, not churned.

Why two loops, not one: forcing arb to wait for the slow cycle misses arbs; forcing the directional book to
re-evaluate at arb speed wastes reads and churns positions (incurring spread cost and burning the write
budget). Separating them lets each run at its natural tempo. **They coordinate through the shared local
state** (§5) and share the write-budget governor (§6).

---

## 3. The cycle order (strict, within each loop)

Both loops follow the same strict order — the dependencies are hard (you cannot size before you know state;
cannot gate before you size; cannot reconcile before you execute):

```
1. REFRESH STATE   — read current state from the local store (kept fresh by the realtime feed);
                     pull the authoritative REST reads needed this cycle (sparingly, within read limits).
                     For the slow loop: current rank, balance, ρ = B/T₃, τ, open positions.
2. GATHER SIGNALS  — fast loop: the current constraint-violation set (/relationships/constraints) + book
                     depth for fillability. slow loop: the research signals (p, σ_p) + current prices +
                     correlations.
3. DECIDE / SIZE   — fast loop: arb-size (capture-fillable, §4). slow loop: the sizing module →
                     proposed sizes for the directional book (Browne × multivariate base).
4. RISK-RESIZE     — slow loop only: Layer B shrink factors (divergence-skepticism, volatility-scaling,
                     drawdown-throttle, confidence floor) applied to the proposed sizes. (Fast/arb path
                     skips Layer B — §4.)
5. RISK-GATE       — BOTH loops: Layer A deterministic pre-trade gate on every order (exposure caps,
                     erroneous-order rejection, cluster caps, authorized-vs-accidental concentration check).
                     Clamp or reject.
6. EXECUTE         — submit via the idempotency core (deterministic client key, query-before-submit,
                     ambiguous→reconcile). Fast loop: multi-leg atomic for arb. Respect the write governor.
7. RECONCILE       — confirm fills against broker truth; update local state; on mismatch → halt-hold-
                     reconcile-resume (safety, not a resize).
```

The realtime feed runs *underneath* this, continuously updating the local state between cycles, so step 1 is
mostly a local read (cheap) with targeted authoritative REST reads only where correctness requires them.

---

## 4. The two sizing paths (the important fork)

Orders reach the gate (step 5) from two different sizing paths that must not be confused:

- **ARB path (fast loop) — capture-fillable, NOT tournament-sized.** A constraint violation is *near-
  riskless* spread capture, not a tournament-variance bet — so it is **not** Browne-sized and **skips Layer
  B**. Its sizing is: capture as much of the violation as the orderbook can *actually fill* (impact-aware,
  §2.1b of the sizing spec applies for fillability), bounded by the Layer-A exposure caps. The multi-leg must
  fill *atomically* (`/orders/multi-leg`) or not at all — never end up holding one leg of an arb (a naked
  directional position). If the atomic multi-leg cannot fill, abort the arb (do not leg into it).
- **DIRECTIONAL path (slow loop) — Browne tournament-sized + Layer-B resized.** The full sizing stack:
  multivariate quarter-Kelly base × Browne multiplier M(ρ,τ) × Layer-B risk shrinks. These are the
  conviction bets held toward the freeze.

Both paths converge at the **same Layer-A deterministic gate** (step 5) and the **same executor + governor**
(step 6). The gate treats an arb order and a directional order identically for the hard limits (exposure,
erroneous-order, rate) — it does not care which path produced the order; it enforces the floor on both.

---

## 5. The shared-state model (how the loops coordinate without colliding)

Both loops + the feed + the supervisor read and write the **same local SQLite state** — this is the
coordination mechanism (the "communal database," local, no network, no rate limit). The discipline:

- **The realtime feed is the primary writer of market state** (prices, books, trade tape, your fills) — it
  continuously updates; the loops read it.
- **Each loop owns its own decisions** but **both consult the shared position/exposure state** before sizing/
  gating — so the fast loop's arb fills and the slow loop's directional positions are *both* counted against
  the shared exposure caps (an arb that consumes exposure reduces what the directional book can deploy, and
  vice versa — the caps are global, enforced at the gate against the *combined* book).
- **Single-writer discipline on orders:** the idempotency core (ported) is "the one order path" — both loops
  submit through it, so order state is consistent and double-placement is impossible regardless of which loop
  acts (the deterministic client key dedups across both).
- **The write-budget governor is shared and allocated by urgency** (§6) — the two loops draw from the same
  per-key write budget; arb (urgent) gets priority reservation so directional quoting cannot starve it.
- **Verify-by-reread (ported):** each loop re-reads state from disk at the top of its cycle — a long-running
  process's in-memory view drifts; the disk is truth. (Same principle as the edge-engine.)

This is one process holding the keys and running both loops as scheduled tasks against shared local state —
*not* separate programs fighting over a network DB (your earlier instinct, resolved: local SQLite, one
process, shared state).

---

## 6. The write-budget allocation (across loops and keys)

The binding resource is 30 writes/min **per key** (plumbing's governor). Allocation:
- **Keys by purpose** (when 3 keys exist): a "fast/arb" key reserves budget for the fast loop's urgent
  captures; a "strategy" key for the slow loop's directional adjustments; (a third for overflow/MM).
- **Within a key, arb has priority:** the governor reserves a slice of each window for the fast loop so a busy
  slow loop cannot consume the whole budget and leave no writes for an arb that appears.
- **The governor is a true outbound gate** (ported + the net-new SIG 429 handler): orders queue by priority
  when the window is full; arb-urgent jumps the queue; low-priority directional adjustments wait.
- **Reads (100/min/key)** are generous and mostly served by the realtime feed (push, not poll); REST reads
  are reserved for authoritative reconciliation and the sparing per-cycle refreshes.

---

## 7. The handoff contracts (what each step hands the next — makes integration concrete)

- **Research signal → state:** `{market_id, exchange_id, p (fair value), σ_p (uncertainty/confidence),
  updated_at}` per market, written to local state. Stale entries (old `updated_at`) are treated as low-
  confidence (shrunk) per the staleness rule.
- **Signals + state → sizing module:** `{per-exchange edge (p vs impact-adjusted p_m), σ_p, correlation/
  factor-loadings, tournament state (B, T₃, ρ, τ), current positions}` → sizing returns `{proposed orders:
  [{exchange_id, side, action, target_size, limit_price}]}`.
- **Proposed orders → Layer B:** applies shrink factors → `{resized orders}` (same shape, smaller sizes).
- **Orders → Layer A gate:** returns `{approved orders (possibly clamped), rejected orders (with reason)}`.
- **Approved orders → executor:** via idempotency core → `{order results: open/filled, quantityTraded,
  fillPrice, order_id}`.
- **Results → reconcile → state:** updates positions/pnl/audit; flags mismatches for the safety halt.

Defining these contracts is what makes the bot buildable as *composition* rather than improvisation. (Writing
them surfaces any underspecification in the component specs — e.g. the sizing spec must emit a *limit price*
and a *target size per exchange*, not just an abstract "allocation"; this spec pins that.)

---

## 8. Startup, modes, and the sim-first path

- **Mode (config):** `SIM` (mbappe `SimulatedBroker` — paper, for running simulations through the full
  architecture before live) or `LIVE` (`SigBroker` against SIG). Identical loops/gates/sizing in both — only
  the broker adapter swaps. **Run SIM first, validate the full cycle (sizing computes, gate clamps, halts
  recover, both loops coordinate), then switch to LIVE.**
- **Gated startup (ported mbappe):** flock single-instance → credential/scope check → email/eligibility/
  residence check → crash-recovery sweep → pre-flight reconciliation (hard gate: mismatch → halt+alert) →
  state rehydrate → arm the supervisor and the dead-man → begin the loops. Refuse to trade on any failed
  precondition.
- **The supervisor (Layer C) starts first and independently** — it is watching before the loops begin, and
  survives their failure.

---

## 9. The failure-mode flow (how a problem propagates, autonomously)

- **An edge is probably wrong (extreme divergence):** Layer B divergence-skepticism *shrinks the size*
  (resize, not halt). Bot keeps trading, smaller.
- **Concentration building accidentally:** Layer A clamps to the authorized ceiling (resize). Deliberate
  endgame concentration passes.
- **Fast drawdown:** Layer B standing-aware throttle shrinks new sizes (tight in grind, loose in authorized
  endgame). No halt.
- **Stale data / position mismatch / repeated errors:** safety halt-HOLD-auto-recover (pause new orders, hold
  positions, fix, resume). Never flatten, never wait-for-human.
- **Runaway pattern (orders pinned at cap, rapid position/balance change):** Layer-C supervisor trips the
  halt-but-HOLD kill-switch *independently of the main loop*. Fail small.
- **Trading too little (gate stack too strict):** monitored alarm (§0 of the risk spec — under-trading is a
  failure, not a safe default); surfaced for the operator to loosen parameters.

---

## 10. One-paragraph summary

**One process holds the keys and runs TWO loops against shared local SQLite state: a FAST loop (arb-watch —
catch and atomically capture fillable constraint violations, near-riskless, NOT tournament-sized, skips Layer
B) and a SLOW loop (strategy — Browne tournament-size the directional conviction book, apply Layer-B risk
resizes). Both follow the strict cycle order refresh-state → gather-signals → decide/size → risk-resize →
deterministic-gate → execute → reconcile, converging at the same Layer-A deterministic gate and the same
idempotent executor + write-governor. A realtime WebSocket feed continuously updates local state underneath
(push, saving the read budget); an independent Layer-C supervisor watches for runaway and trips a
halt-but-HOLD kill-switch even if the main loop fails. The modules plug in at defined handoff contracts
(research signal → (p,σ_p); sizing → proposed orders with limit prices and per-exchange sizes; Layer B →
resized; Layer A → approved/clamped; executor → fills; reconcile → state). The whole thing runs first in SIM
mode (mbappe SimulatedBroker) with the identical architecture — run simulations through it, validate the
cycle, then swap the broker adapter to LIVE. Composition, not improvisation: every arrow is a contract, every
box is an existing spec or ported component.**

*Architecture spec. Composes: SIG_TOURNAMENT_SIZING_SPEC.md (slow-loop directional sizing),
SIG_RISK_GOVERNANCE_SPEC.md (Layers A/B/C across both loops), the ported mbappe plumbing (state, idempotency,
reconciliation, resilience, alerting, SimulatedBroker), the arb logic (fast loop), and the future research
signal (async (p,σ_p) source). Market-structure-independent; values and research/arb internals come live.
Recorded 2026-10-01.*
