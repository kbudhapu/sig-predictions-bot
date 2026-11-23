# SIG Predictions Cup — Autonomous Risk-Governance Module Spec
### Deterministic guardrails for a 24/7 bot with NO human in the loop — risk as automatic resizing, not human gates

**Status: MODULE SPEC** for the SIG bot's risk layer. This is the safety/enforcement layer wrapped around
the sizing module (`SIG_TOURNAMENT_SIZING_SPEC.md`). It is designed for the hard constraint that **there is
no human in the loop** — the operator cannot reliably check it — so every risk concern must resolve
*autonomously*, and the controls must be *structurally incapable* of permitting a runaway, because no human
will catch one in time.

**The two governing principles (from the autonomous-trading-failure literature):**

1. **Risk = automatic RESIZING, not human gates.** Every risk concern (edge might be wrong, concentration
   building, drawdown, volatility rising) resolves by *shrinking the bet automatically* — the bot keeps
   running and bets *more carefully*. The ONLY hard stops are **broken-information** safety failures (stale
   data, position mismatch), and even those are **halt-hold-and-auto-recover** (pause, fix, resume itself),
   never halt-and-wait-for-a-human. Continuous autonomous response beats binary human-gated halts.

2. **The risk gate is DETERMINISTIC and structurally SEPARATE from the LLM.** The sizing/research is
   probabilistic (LLM-derived edge estimates). The risk gate is **pure deterministic code that never calls
   the LLM** — because the LLM *will* be wrong sometimes (estimation error is the primary failure mode), and
   a risk layer that trusts it inherits the error. The deterministic gate is precisely the backstop that
   catches the probabilistic brain being wrong. *A probabilistic LLM as a safety filter is a severe
   vulnerability; the gate must be hard, deterministic, and unbypassable.*

**The cautionary tale that sets the bar: Knight Capital — an unchecked algorithm lost $440M in 45 minutes.**
That is the canonical "runaway autonomous trader, no human watching" disaster, and it is *exactly* this
bot's regime. 45 minutes is faster than any human intervention — so the guardrails are not a backup to human
oversight, they are the *only* thing between a logic bug and catastrophe. They must make a runaway
*structurally impossible*, not merely *alertable*.

---

## 0. The central tension: "fail small, NOT freeze" (the 13-layer trap)

A documented autonomous-trading study compared a streamlined risk variant against a 13-layer safety variant:
the streamlined one traded 190 times in 15 days; **the 13-layer variant made ZERO trades** — the gates were
so strict nothing ever passed. More safety layers improve per-trade quality but can reduce trade frequency to
*nothing*.

**This is a direct danger for this bot.** We have a sophisticated sizing stack (uncertainty-shrinkage ×
divergence-skepticism × impact-adjustment × drawdown-throttle × concentration-caps × reality-gate). Stacked
naively, these multiply toward *never trading, or trading so small it cannot win*. **We are aiming for FIRST
PLACE** — a bot so safe it makes no meaningful bets *loses by default*. So the governing calibration is
**"fail small, not freeze":** bound the downside, but *permit meaningful bets*. Every gate is a *resize*,
not a *veto*, specifically so the product of all gates is a smaller-but-nonzero bet, never zero. The risk
layer's success metric is *bounded loss with the bot still trading*, not *maximum caution*.

---

## 1. Architecture — three separated layers (must not share a failure domain)

The controls split into three layers that **do not share one failure domain**, so a deadlocked or runaway
main loop cannot disable its own brakes:

- **Layer A — the deterministic pre-trade GATE (in-path, before every order).** Hard, LLM-free checks that
  every order passes before it is sent: position limits, size/price sanity, exposure caps, rate limits. Pure
  deterministic code. Cannot be bypassed by the sizing module — the sizing module *proposes*, the gate
  *clamps*.
- **Layer B — the autonomous RESIZING layer (in the sizing path).** The "risk = resizing" mechanisms:
  divergence-skepticism, volatility-scaling, drawdown-throttle, uncertainty-shrinkage. These make the bot
  bet *smaller* under risk, continuously, without halting. (Composes with the sizing spec.)
- **Layer C — the independent SUPERVISOR (out-of-path, separate process).** A separate watchdog (ported from
  mbappe's external watchdog) that talks *directly* to the kill-switch and the broker, independent of the
  main loop. If the main bot deadlocks, runs away, or goes silent, the supervisor can halt it **even though
  the main loop is the thing that failed.** The thing that stops a runaway must be independent of the thing
  running away.

---

## 2. Layer A — the deterministic pre-trade gate (hard limits, checked before every order)

Pure deterministic, LLM-free, checked on *every* order (single and each leg of multi-leg) before send. Any
breach **clamps the order to the limit** (resize), or for erroneous-order checks, **rejects** it:

- **Aggregate exposure cap:** total capital in open positions ≤ `MAX_DEPLOY` (e.g. 85–90% of balance) — keep
  a reserve for arb opportunities and recovery. New orders that would breach are *resized down* to fit.
- **Per-market position cap:** no single market > `MAX_PER_MARKET` of the book — *except* the
  sizing-authorized endgame concentration (§3). Resize to the cap otherwise.
- **Per-correlated-cluster cap:** correlated positions (same national-swing factor, from the relationship
  graph / research factor loadings) are summed and capped *as one position* — because 10 positions on one
  factor are one bet, not ten (consistent with the sizing spec's n_eff). The cluster cap, not the per-market
  cap, is the binding exposure control for correlated books.
- **Erroneous-order rejection (the Knight-Capital class):** reject any order whose price is off-tick, outside
  [0.005, 0.995], whose size exceeds a hard sanity maximum, or whose implied notional is absurd. These are
  *bug catchers* — an order this wrong is a logic error, not a trade. Reject, log, alert (informational).
- **Order-rate limit (the cheapest runaway circuit-breaker):** the plumbing's write-governor (30 writes/min
  per key) *is* this control — a runaway loop that tries to fire 10,000 orders is capped at 30/min and the
  anomaly is detected (see §4). Enforcing our own cap is cleaner than handling the broker's throttle
  rejection as a failure.
- **Balance/collateral check:** never submit an order the balance cannot cover.

All of these are *account-configuration constants* set deterministically, re-read from config, never
computed by the LLM. The gate is the hard floor under everything the probabilistic layers do.

---

## 3. Layer A ↔ sizing cooperation: authorized vs accidental concentration (autonomous, no human)

The hard problem: the gate must *permit* the sizing module's **deliberate** endgame concentration (the
skyrocket) while *blocking* **accidental** runaway concentration — and it cannot ask a human which is which.

**Autonomous resolution: the gate recomputes the sizing module's own authorization from the shared
tournament state.** The gate reads the same (ρ, τ) the sizing module uses and computes the *maximum
concentration the sizing module would authorize at this standing* (the Browne-override ceiling, §2.3 of the
sizing spec). Then:
- Concentration ≤ authorized-ceiling(ρ, τ) → **permitted** (it is intended — the endgame skyrocket).
- Concentration > authorized-ceiling(ρ, τ) → **clamped down to the ceiling** (it is a bug/runaway — resize,
  do not halt).

So the gate is not a second opinion that fights the sizing module; it *enforces the sizing module's own
rules as a hard ceiling*, deterministically. Deliberate endgame concentration passes because it is *at* the
authorized level; accidental over-concentration is *automatically resized down*. No human needed — the two
layers agree by construction because the gate re-derives the authorization from shared state. (And because
the gate is deterministic while the authorization formula is fixed, the gate catches a *bug in the sizing
module itself* — if the sizing code computed a size above its own authorization, the gate still clamps it.)

---

## 4. Layer B — risk as automatic resizing (the "size it better" mechanisms)

Every non-safety risk concern becomes a *multiplicative shrink factor* on the proposed size, applied
continuously. These compose with the sizing spec (they are the risk-side shrinkage alongside the
estimation-uncertainty shrinkage already there) — and per §0, they are tuned so their *product* is
smaller-but-nonzero, never zero:

- **Divergence-skepticism (replaces the "flag a suspicious edge for human review" that no human can do):**
  the edge→size map is **concave** — moderate divergences (SIG price vs research estimate) size up roughly
  linearly, but *large* divergences size up *less than proportionally*, and *extreme* ones size *down*.
  Rationale: an implausibly-large claimed edge on a famous, well-analyzed race is *more* likely a broken
  estimate than free money. Suspicion = automatic shrinkage. This is the autonomous replacement for human
  review and it directly counters overbetting-on-estimation-error.
- **Volatility-scaling:** as a market's price volatility rises, *shrink* the size (maintain roughly constant
  risk exposure). More uncertain market → smaller bet, automatically.
- **Standing-aware drawdown throttle (NOT a stop-loss halt):** a fast book drawdown triggers *automatic
  reduction of all new position sizes* scaled by standing — **tight during the grind** (protect the capital
  needed for the endgame), **loosened in the sizing-authorized endgame concentration** (the concentrated bet
  is *expected* to swing hard; a stop-loss there would kill the exact bet the strategy intended). So drawdown
  is a continuous throttle keyed to (ρ, τ), never an on/off stop. The bot never *stops* on a drawdown (only
  on broken-info safety failures, §5) — it sizes down in the grind and *tolerates the swing* in the endgame.
- **Confidence/reality floor:** a position whose edge estimate is below the research signal's confidence
  threshold shrinks to zero (it is estimation noise, not opportunity — consistent with the sizing spec's
  reality gate). This is a resize-to-zero, applied deterministically on the reported `σ_p`, not an LLM
  judgment at trade time.

The composition rule (§0): these factors multiply, but each is bounded away from pathological strictness so
the product permits meaningful bets. If telemetry shows the bot trading far too little (approaching the
13-layer zero-trade failure), the factors are too tight and must be loosened — *trading-too-little is itself
a monitored failure mode*, not a safe default.

---

## 5. Hard safety stops — broken-information only, halt-HOLD-and-auto-recover

The *only* things that halt trading (as opposed to resize) are **broken-information** failures — because
acting on broken information is never right, regardless of standing. Crucially, each is **halt-hold-and-
auto-recover**: pause new trades, *hold existing positions*, fix the condition, and *resume itself* — never
halt-and-wait-for-a-human, and never auto-flatten:

- **Stale data:** prices / leaderboard / research-signal feed is older than a threshold → halt new orders,
  hold positions, auto-retry the feed, resume when data is fresh.
- **Position-reconciliation mismatch:** believed positions ≠ exchange truth → halt, reconcile (ported mbappe
  reconcile-boot discipline, SIG thresholds), resume when reconciled. **Never silently adopt the exchange
  state or the local state — reconcile, then resume.**
- **Repeated order errors:** if orders keep failing (structural problem) → halt-hold, alert (informational —
  "halted: orders failing," not a request to act), stay safe until the error clears, then resume.
- **Rate-limit danger:** handled by the governor (never a halt — the governor throttles and the anomaly
  detector, below, watches for the runaway pattern).

**Runaway detection (the Knight-Capital guard), on the independent supervisor (Layer C):** the supervisor
watches for the runaway *pattern* — order submission rate pinned at the cap, repeated identical orders, rapid
position growth, rapid balance change — and if detected, **trips the hard kill-switch independently of the
main loop.** This is the structural "fail small" guarantee: even if the main bot's logic is the thing gone
wrong, the separate supervisor stops it. The order-rate cap bounds the damage per minute (30 writes); the
supervisor catches the pattern and halts within that bound.

---

## 6. The kill-switch — modal, autonomous-default halt-but-HOLD

The kill-switch (mbappe's `touch HALT` sentinel ports) has **modes**, and the autonomous default is critical:

- **Autonomous triggers (supervisor runaway-detection, repeated errors) → HALT-BUT-HOLD:** stop all new
  orders, *keep positions*. **Never auto-flatten** — because auto-flattening would destroy the endgame
  convictions (which must stay frozen to settle to $1) and flattening is a deliberate decision, not a hiccup
  response.
- **Manual flatten (operator-invoked, when the operator *does* check and chooses to) → FLATTEN-ALL:** only
  when the operator explicitly invokes it. The bot never flattens itself.
- **The switch is on the independent supervisor (Layer C)** — reachable even if the main loop is deadlocked.

Default posture: a runaway or error → freeze-in-place safely → the operator *can* inspect when convenient,
but the bot is *already safe* without them and will not self-destruct the strategy.

---

## 7. Observability — autonomous-but-not-a-black-box

No human in the loop ≠ no visibility. The bot runs without the operator, but must be *inspectable* when they
do look:
- **Structured decision log** (ported mbappe `system_events` + the FastAPI token-auth sidecar): every order,
  resize-factor applied, gate clamp, halt, resume, and *why* — so a session can be reconstructed.
- **Async explanation layer (the one place the LLM is allowed near risk):** per the architecture principle,
  an LLM may *attribute causes* to risk events *asynchronously, after the fact, for the human to read* — but
  it is **never** in the deterministic decision path. Explanation, not control.
- **Liveness + state alerts** (ported mbappe three-layer dead-man + multi-channel paging): a daily positive
  "I'm alive and here's the state" ping, plus immediate alerts on halts/runaway-trips — **informational**,
  so the operator *can* look, never a dependency for safe operation.
- **Trading-too-little alarm:** telemetry that flags if the gate stack has strangled trading toward the
  13-layer zero-trade failure (§0) — because *under*-trading is a monitored failure, not a safe default.

---

## 8. One-paragraph summary

**Risk is automatic RESIZING, not human gates** — every concern (uncertain edge, building concentration,
drawdown, volatility) shrinks the bet continuously (Layer B: divergence-skepticism concave sizing,
volatility-scaling, standing-aware drawdown throttle, confidence floor), so the bot keeps running and bets
more carefully. The **only hard stops are broken-information failures** (stale data, position mismatch,
repeated errors), each **halt-hold-and-auto-recover** (never halt-and-wait-for-human, never auto-flatten).
A **deterministic, LLM-free pre-trade gate** (Layer A: aggregate / per-market / per-correlated-cluster
exposure caps, erroneous-order rejection, the order-rate governor) is the hard, unbypassable floor — it
catches the probabilistic sizing/LLM being wrong, and it permits the sizing module's *authorized* endgame
concentration while clamping *accidental* over-concentration by re-deriving the authorization from shared
(ρ, τ) state. An **independent supervisor** (Layer C, separate failure domain) watches for the runaway
pattern and trips a **halt-but-HOLD kill-switch** even if the main loop is the thing gone wrong — the
structural "fail small, not crash big" guarantee against the Knight-Capital failure. And the governing
calibration is **"fail small, NOT freeze"**: because we are aiming for FIRST PLACE, the gate stack is tuned
so its product permits meaningful bets — *under*-trading toward the 13-layer zero-trade trap is itself a
monitored failure, not a safe default. The operator is kept *informed* (decision log, async LLM cause-
attribution, liveness pings) but the bot never *depends* on them: autonomous, bounded, and recoverable.

*Spec grounded in: the autonomous-trading risk-control literature (deterministic gate separated from the
probabilistic/LLM layer; failure-domain separation; pre-trade position/size/rate limits; reconciliation and
feed-staleness gates — SEC 15c3-5 market-access controls, QuaTick, the agentic-trading survey); the
Knight-Capital $440M-in-45-minutes runaway as the bar for structural (not alertable) guardrails; the
13-layer-zero-trade result as the "fail small not freeze" calibration constraint; volatility-scaling and
drawdown circuit-breakers as the resize mechanism. Composes with SIG_TOURNAMENT_SIZING_SPEC.md (the
probabilistic sizing this layer gates). Recorded 2026-10-01.*
