# SIG Predictions Cup — Full SIM Test Plan
### What the end-to-end SIM run must exercise before the bot is trusted with real (simulated-money) trading

**Status: TEST PLAN.** Every module passes its own unit tests. This plan covers the thing unit tests
*cannot* catch: whether the modules **actually work together** — the integration seams, the handoff
contracts, the timing, and (critically) whether the modules built BLIND in the sandbox actually wire to the
real modules.

**Run everything against the `SimulatedBroker`** (no live SIG orders) with the full orchestrator wiring —
this is the real validation that the pile of unit-tested modules is actually a working bot.

---

## 0. THE #1 PRIORITY — do the blind-built modules actually integrate?

Several modules (at least the **OMS** and the **orchestrator**) were built in the sandbox in
`*_deliverable/` folders, could NOT import the real sig-bot modules, and were tested against *assumed*
interfaces and *mocked* dependencies. They have **never imported each other's actual code.** This is the most
likely source of integration failure.

**Before any behavioral test, verify the wiring resolves:**
- [ ] Every module is relocated into the real sig-bot repo (no code left in `*_deliverable/` folders or in
      the mbappe repo — see the displacement audit).
- [ ] The orchestrator actually imports and calls the REAL OMS (not an inlined/faked fill-policy).
- [ ] The OMS actually imports and calls the REAL broker (sig/broker.py) and REAL state (infra/state).
- [ ] The orchestrator actually imports and calls the REAL risk gate, sizing, allocator, and strategies.
- [ ] `python -c "import <the orchestrator entrypoint>"` succeeds — every import resolves, no missing module,
      no signature mismatch at import time.
- [ ] A single real cycle runs with ALL real modules wired (not mocks) against the SimulatedBroker.

**If the assumed interfaces mismatch** (orchestrator expected OMS method `X(a,b)` but OMS has `X(a,b,c)`),
this is where it surfaces — fix the contract, re-run. Expect several of these; that is what integration
testing is for. The unit tests mocked these boundaries, so this is the first time they meet.

---

## 1. The order path — end to end, all real modules

A strategy intent must flow all the way to a (simulated) fill and back to state:
- [ ] A strategy's `evaluate()` produces an intent → SIZING sizes it (real sizing module) → RISK GATE
      validates/clamps (real gate) → OMS places it (real OMS) → SimulatedBroker fills → OMS updates lifecycle
      → fill reconciles to state (real state) → the position appears in state.
- [ ] The sized order respects the allocator's budget for that edge AND the capital ledger's available
      balance (the two compose correctly).
- [ ] The risk gate actually clamps an over-sized order in the live path (not just in its unit test).
- [ ] `signals/` (plural) imports resolve wherever the orchestrator references the signal module.
- [ ] All order/position writes go to the SIG `state.db`, NOT the inherited mbappe `orders` table.

---

## 2. The capital arbiter — the lock-free guarantee holds in the real cycle

- [ ] Two strategies in one cycle, each wanting more than half the balance → the second is capped by the
      ledger's remaining available (the double-spend-prevention, but now with the REAL sizing + gate +
      strategies in the loop, not the unit mock).
- [ ] Exposure caps (risk gate) bind against the reserved-aware available balance across the combined book.
- [ ] Arb (short cadence) and directional (long cadence) both size against the SAME ledger in the SAME cycle
      without double-spending.

---

## 3. The two cadences — unified cycle, cadence-gated

- [ ] Arb (cadence_s short) runs most cycles; directional (cadence_s long) runs every N cycles — confirm the
      due-gating actually produces the different frequencies over a multi-cycle run.
- [ ] Market-making is idempotent — the same touch does NOT produce a re-write every cycle (confirm it's not
      burning the write budget re-quoting unchanged quotes).

---

## 4. The feed → state → loop flow

- [ ] The feed (real module) writes prices to state; the loop reads them from state (not polling the API).
- [ ] A simulated feed gap → mark-stale → refetch → resync (the feed's core invariant) → the loop sees stale
      markers during the gap and degrades (sizing low-confidence / gate stale-halt).
- [ ] A dead feed → state ages to stale → the risk gate's stale-data halt fires → trading pauses safely (NOT
      trading on dead prices).

---

## 5. Error isolation — one failure doesn't cascade

- [ ] A strategy's `evaluate()` throws → caught, logged, that strategy skipped, the OTHER strategies and the
      loop continue (confirmed in unit test — reconfirm with real modules wired).
- [ ] Repeated failures of one strategy → degrade (block that strategy) + alert, bot runs on.
- [ ] The sizing or allocator throwing → that order/cycle skipped, no crash.
- [ ] A simulated process-level crash → systemd/supervisor restarts → reconcile-on-boot runs → the bot
      resumes safely (test the restart path, even if just the reconcile-on-boot gate).

---

## 6. Startup & shutdown

- [ ] Startup gate sequence: flock acquired → config loads (fails loud on a bad key) → state opens →
      supervisor armed BEFORE trading → OMS reconcile hard-gate (a simulated mismatch → the loop does NOT
      start) → feed → signal → strategies registered → allocator seeded → cycle starts.
- [ ] A second instance can't start (flock blocks it).
- [ ] Graceful shutdown → positions HELD (not flattened), connections released, flock freed.
- [ ] Halt-but-HOLD on an autonomous trigger → new orders stop, positions held, auto-recovers when clear.

---

## 7. The mechanical floor actually trades (the launch-critical path)

Since launch is on the mechanical floor (arb + market-making), prove those specifically work end-to-end in
SIM, because the directional half comes later:
- [ ] Given a simulated constraint violation (or a computed complementary-pair mispricing), the arb path
      detects it → sizes capture-fillable → multi-leg atomic through the OMS → SimulatedBroker fills all legs
      → position in state. A PARTIAL arb fill → full unwind (no naked leg).
- [ ] Market-making posts a two-sided resting quote inside the spread → a simulated fill on one side →
      inventory updates → quotes skew → adverse-selection pull on a simulated fast move.
- [ ] The allocator funds arb + MM (the CORE prior) and starves the directional tiers (no directional data
      yet) — confirm the mechanical floor gets the capital.

---

## 8. A multi-cycle soak (let it run)

- [ ] Run the wired bot in SIM for many cycles (an hour of simulated time) → no crash, no deadlock, no
      runaway write-rate, no memory growth, state stays consistent, the dead-man doesn't false-fire.
- [ ] Inject a few adverse events during the soak (feed gap, strategy crash, a reject) → confirm each is
      handled autonomously and the bot keeps running.

---

## 9. What SIM CANNOT test (defer to the go-live checklist)

SIM proves the *wiring and logic*. It cannot prove the *live-contract assumptions* — those are verified at
open (see `GO_LIVE_CHECKLIST.md`): the real SIG write-response shapes, the real feed wire-format, whether SIG
publishes arb constraints, the real orderbook depth, the live leaderboard for ρ. SIM uses assumed/simulated
versions of all of these. **SIM clean ≠ live-ready; SIM clean = the bot works against its assumptions, and
the go-live checklist verifies the assumptions.**

---

## 10. Pass criteria

The SIM test passes when: every module is wired (no blind-built mismatches), the order path works end to end
with real modules, the capital arbiter prevents double-spend in the real cycle, the mechanical floor (arb +
MM) actually trades in SIM, error isolation holds (a strategy crash doesn't cascade), startup/shutdown gates
work, and a multi-cycle soak runs clean with injected adverse events handled. **Then — and only then — push
to the droplet and work the go-live checklist.**

*Test plan. The integration validation between "modules pass unit tests" and "bot is trusted." Leads with the
blind-built-module wiring check because that's the highest-risk seam. Recorded 2026-10-01.*
