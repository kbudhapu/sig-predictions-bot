# SIG Predictions Cup — Go-Live Checklist
### Everything to verify, fix, or fill before and at the tournament open (16:00Z)

**Status: CHECKLIST.** The accumulating "must verify/fix at open" items from across the whole build. Worked
in order: first the pre-flight (relocation, commits, SIM), then the at-open live-contract verifications, then
launch. The bot launches on the **mechanical floor (arb + market-making)**; directional wires in after.

---

## PHASE A — Pre-flight (before the droplet, before open)

### A1. Displacement & relocation (HIGH RISK — do first)
- [ ] Run the displacement audit (both repos). Find every SIG file in the mbappe repo and every `*_deliverable/`
      folder.
- [ ] Relocate ALL displaced modules into the real sig-bot repo (OMS, orchestrator, and anything else the
      audit finds). Nothing SIG-related left in the mbappe repo or in deliverable folders.
- [ ] Clean mbappe's git history of any SIG commits (if the audit found them).
- [ ] **Verify imports resolve:** the blind-built modules (OMS, orchestrator) actually import the REAL broker/
      state/risk-gate/sizing/strategies — not mocks, not inlined fakes. `python -c "import <entrypoint>"`
      succeeds.

### A2. Commit everything
- [ ] Every module committed (market-making, order-flow, resolution were reported uncommitted — confirm).
- [ ] Working tree clean. `git status` shows nothing uncommitted that should be in.
- [ ] The specs are committed in docs/specs/ (including the newest: orchestrator, feed, sizing, dynamic
      allocation, edge portfolio, audit, this checklist, the SIM test plan).

### A3. Spec/code consistency fixes
- [ ] **Sizing spec §2.2 is WRONG** — it says `ν = Φ⁻¹(ρ)` but that's internally inconsistent (Φ⁻¹(1)=∞, every
      ρ>1 collapses to the same clamp, M≈0.034 at the reference point). The CODE uses the correct odds
      transform `u = ρ/(1+ρ)`. Align the spec to the code so nobody reintroduces the bug.
- [ ] **Browne constants are shared:** confirm the risk gate AND the sizing module both read `M_0`, `tau_floor`,
      `rho_min/max`, `M_full` from the SAME `risk:` config block (not duplicated under `sizing:`). The sizing
      agent confirmed it reads from `risk:` — verify the gate does too, so the authorized-concentration ceiling
      matches exactly. (`M_0 = 1.1284` is the shared value.)

### A4. Config complete & validated
- [ ] config/sig.yaml fully populated (every module's keys present, DEFAULTs sane).
- [ ] Config validation is WIRED INTO the load path (fails loud on a typo'd/missing key — don't inherit
      mbappe's unwired-validation weakness).
- [ ] Secrets in a gitignored .env (NOT in sig.yaml); .env.example committed documenting every key.
- [ ] Central require_secret fails loud on a missing secret (no silent empty-string — don't inherit mbappe's
      weakness).

### A5. Full SIM test (gate — do NOT proceed until clean)
- [ ] Run the full SIM test plan (`SIG_SIM_TEST_PLAN.md`) end to end. All modules wired, order path works,
      capital arbiter prevents double-spend, mechanical floor trades, error isolation holds, multi-cycle soak
      clean. **SIM clean is the gate to the droplet.**

### A6. Directional-upside data (NOT launch-critical — defer if time-pressed)
- [ ] `ChamberModel` seat baselines (2026 Senate/House/Governor: which seats up, who holds the rest, seats =
      control) — for the joint-structure soft-mispricing edge.
- [ ] `race_mapping.yaml` — SIG markets → Polymarket/Kalshi contracts — for the signal's cross-market edge.
- [ ] (The bot launches WITHOUT these on the mechanical floor; they enable the directional edges.)

---

## PHASE B — Droplet (after SIM clean)

- [ ] Provision the droplet (1 core/1GB per the plan).
- [ ] Follow deploy/README.md runbook: clone → venv → create .env (secrets) → copy systemd units →
      daemon-reload → enable.
- [ ] Confirm the systemd units match the settled process model (trader + feed + signal + supervisor).
- [ ] Confirm the flock single-instance guard works (can't start two traders).
- [ ] Confirm the external watchdog (systemd WatchdogSec) + the in-process dead-man thread + the supervisor
      process — all three liveness layers active.
- [ ] Confirm log retention is capped (don't fill the 1GB disk).
- [ ] `/health` endpoint responds (alive, trading, last-cycle-age, feed-fresh, state-writable).
- [ ] Run the bot in SIM mode ON the droplet for a bit — confirm it runs in the real environment, not just
      locally.

---

## PHASE C — At open (16:00Z) — the live-contract verifications SIM couldn't test

These are the assumptions SIM used simulated versions of; verify each against the LIVE SIG API the moment the
tournament opens:

### C1. The arb-constraints fork (THE #1 at-open question)
- [ ] Re-pull `/relationships/constraints` and `/relationships/graph` at open. Does SIG PUBLISH constraints
      (→ the arb detector reads them) or are they still empty (→ we compute complementary-pair arbs
      ourselves)? The relationship engine handles both, but confirm WHICH world we're in — it determines the
      whole arb income path. Log the raw responses.

### C2. Broker write-response shapes
- [ ] Place ONE tiny test order live → confirm the broker's assumed response-parsing matches SIG's ACTUAL
      response (order id, status, fill fields). The broker was tested against mocked responses; the live shape
      is unverified.
- [ ] Confirm idempotency actually works live (a re-submit with the same key does NOT double-place).
- [ ] Confirm the 429/Retry-After handler fires correctly against the real rate limiter.

### C3. The feed wire-adapter
- [ ] Confirm SIG's realtime stream IS Supabase Realtime (as assumed) and the message format matches the
      feed's wire adapter (the one injectable, untested-against-live piece). Confirm `previousRevision`
      continuity works on the real stream. If the format differs, swap the wire adapter (the feed's resync/
      staleness/backoff logic stays).
- [ ] Note: "Supabase Realtime" here is SIG's feed transport we connect TO — NOT a database we run. Our state
      is local SQLite. (No Supabase to provision.)

### C4. Live market & standing data
- [ ] Confirm the orderbook depth is as recon found (or re-measure — organic depth may differ from the
      pre-open synthetic 1000-lot seeding). The sizing's impact-adjustment depends on real depth.
- [ ] Confirm the leaderboard API returns the 3rd-place threshold (→ ρ for the Browne sizing). Until the field
      populates, ρ may be undefined — confirm the sizing handles that (read_rho returns None → directional
      sizing holds).
- [ ] Confirm eligibility (email/residence/scope) is satisfied — the startup gate checks this, but confirm it
      passes live.

### C5. 3 API keys
- [ ] Confirm all 3 keys are created, have the right scopes (read/trade), and the multi-key write-gate routes
      across them.

---

## PHASE D — Launch

- [ ] Flip from SIM to LIVE (the broker adapter swaps; everything else identical).
- [ ] Start on the MECHANICAL FLOOR: arb + market-making funded (the allocator's CORE prior), directional
      starved until it proves out (or until its data — ChamberModel/race_mapping — is filled).
- [ ] Watch the first live cycles closely (the one time you DO watch): confirm orders place, fills reconcile,
      no runaway, the dead-man is green, the /health endpoint is healthy.
- [ ] Confirm the first arb capture and the first MM quote actually happen live.

---

## PHASE E — After launch (the directional upside, as you build it)

- [ ] Fill ChamberModel + race_mapping → enable joint-structure + cross-market edges.
- [ ] Confirm the research signal is producing (p, σ_p) and the sizing is consuming them.
- [ ] Watch the allocator shift capital toward whatever's actually performing (the live null-test of the
      directional edge — it earns capital by performing, or stays starved).
- [ ] As election night (Nov 3-4) approaches: confirm the endgame logic (resolution-mechanics, the Browne
      endgame override, the Kalshi-AP vote data for repricing), the hold-vs-exit decisions, and the Nov 4
      17:00Z settlement wall timing.

---

## The honest framing

**SIM clean = the bot works against its assumptions. Go-live = verify the assumptions against reality.** The
highest-risk items are front-loaded: the blind-built-module relocation/wiring (A1 — could break everything),
and the arb-constraints fork (C1 — determines the income path). The mechanical floor is what launches; the
directional half is upside layered on after, as the data and the live-proven edge come in.

*Checklist. Consolidates every "verify/fix at open" item tracked across the build. Recorded 2026-10-01.*
