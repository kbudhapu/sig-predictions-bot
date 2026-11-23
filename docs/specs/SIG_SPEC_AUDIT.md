# SIG Predictions Cup — Spec Audit (adversarial self-review of all seven documents)
### Is the research real, used correctly, and would it actually work? Honest findings.

**Status: AUDIT RECORD.** An adversarial review of the seven SIG design documents, done *after* the design
was complete, with the explicit goal of finding where the research was overclaimed, name-dropped, or used to
dress up something that would not actually work — not confirming the design is good. Findings below, including
two real overclaims (now fixed) and one fundamental caveat that is the honest headline.

**Documents audited:** SIG_API (extraction), mbappe inventory, SIG_TOURNAMENT_SIZING_SPEC,
SIG_RISK_GOVERNANCE_SPEC, SIG_INTEGRATION_SPEC, SIG_RESEARCH_SIGNAL_SPEC, SIG_CONFIG_SCHEMA.

---

## 1. The headline (read this first)

**The design is theoretically well-grounded but EMPIRICALLY UNVALIDATED.** ~10 of 13 load-bearing research
claims are real and used correctly; the risk spec is genuinely strong; the sizing core is sound. **But nothing
has been tested against real or simulated data — no backtest, no sim run, no live tick.** The specs are a
rigorous *design hypothesis* for how to win, not *evidence* that it works.

By the project's own standard (from the Numerai/edge-engine arc: *verify don't reason, beat the null, theory ≠
edge*), **the specs do not yet meet the bar** — because the one binding question (is the research signal's
edge real, or estimation noise?) is exactly the thing that cannot be answered until live/sim data exists. The
sizing/risk/integration machinery will *run* correctly; whether the system *wins* depends entirely on whether
the edge is real, which is unverified.

**So: the means are sound; the edge is a hypothesis.** Build it, run it in SIM, and treat the first live days
as the null-test — do not trust the edge until the data shows it beating SIG's own price.

---

## 2. Research backing — claim by claim

### Solid (real, correctly used) — the majority
| Claim | Source | Verdict |
|---|---|---|
| Quarter-Kelly for uncertain+correlated edges | Thorp; variance-math; multiple | ✅ Real, correct. (half/quarter contradiction caught in config.) |
| 1/√n_eff portfolio variance scaling | standard bankroll literature | ✅ Real, correct. |
| Baker–McHale uncertainty-based bet shrinkage | Decision Analysis 2013 | ✅ Real, correct (shrink more with σ_p). |
| Overbetting ≫ underbetting penalty; fractional Kelly | Thorp | ✅ Real, correct, governing posture. |
| Knight Capital ($440M/45min) as the "no human in time" bar | historical | ✅ Real, correctly used. |
| Deterministic gate separated from the LLM | SEC 15c3-5 / Lean4 paper; agentic-trading survey | ✅ Real, correct, genuinely important. |
| 13-layer-zero-trade → "fail small, not freeze" | memecoin-trading arxiv | ✅ Real, correct, strong. |
| Log-odds (logit) aggregation | Satopää et al. | ✅ Real, correct. |
| σ_p from source dispersion | ANOVA-updating literature | ✅ Real concept, reasonable use. |
| Failure-domain separation / supervisor | automated-trading risk literature | ✅ Real, correct. |

### Overclaimed (found and FIXED in this audit)
| Claim | Problem | Fix applied |
|---|---|---|
| **Browne's formula = our sizing** | Browne solves a *continuous* GBM asset with a *fixed* target. Our bets are *discrete binary settle-at-end*. The spec gave the literal formula `α=(1/σ√(T−t))·φ(ν)/Φ(ν)` then said "adapt" — a builder could implement the continuous equation on discrete bets (wrong). | **Sizing spec §1 rewritten:** `M(ρ,τ)` is now explicitly a **behavioral SHAPE to implement** (aggressive-behind, protect-ahead, escalate-as-1/√time, memoryless in ρ), NOT the literal continuous α. "Use Browne for the shape and direction; never pretend the closed form is a literal solution." |
| **Extremizing creates edge vs SIG** | Extremizing improves calibration *on average across many questions*; on a *single* market it can make you *more* wrong. And "edge vs SIG" assumes SIG is the un-extremized conservative crowd — **unverified**. Could manufacture fake edge. | **Signal spec §0 rewritten:** downgraded to **HYPOTHESIS to verify live**; extremizing labeled a *calibration tool, not a proven edge-generator*; must check the extremized p actually predicts SIG resolution better than SIG's price before trusting. |
| **Favorite–longshot bias holds here** | Well-documented in *sports/horse* betting; whether it holds in *this student political market* is a hypothesis, not established. Was cited too confidently. | **Signal spec §0 rewritten:** marked **hypothesis to verify** against live SIG prices-vs-outcomes, not assumed. |

### Unverified specifics (acceptable, but flagged)
| Claim | Status |
|---|---|
| Bloomberg O(N) multivariate-Kelly method is implementable | Real paper (arxiv 2604.24723); but implementing it is non-trivial. Spec already says **v1 uses a simple approximation, not the full method** — re-flagged: do NOT attempt the full integral-transform in v1. |
| Polymarket/Kalshi have usable public APIs | Both do exist (Polymarket gamma/CLOB; Kalshi trading/data API); exact endpoints/access **not verified** — marked TUNE-LIVE / build-dependency. Verify when building the signal. |

---

## 3. Would it actually WORK? — honest component-by-component

- **Sizing:** the *math* is sound and will compute correctly (quarter-Kelly base, the Browne-shaped tilt, the
  shrinks). Caveat: the Browne *shape* is right; the literal formula is not a drop-in (fixed). It sizes
  *correctly* — but only as well as the edge it is fed.
- **Risk:** the *strongest* spec. Deterministic gate, resize-not-halt, fail-small-not-freeze, failure-domain
  separation — all real, all will work as safety machinery. This part genuinely protects the bot.
- **Integration:** coherent architecture (two loops, handoff contracts, sim-first). Will run. The handoff
  contracts are specified enough to build. No overclaim.
- **Signal:** the *mechanism* works (aggregate, calibrate, σ_p, divergence). The *edge* is the weak point —
  the structural-edge hypotheses are now honestly downgraded; the cross-market real-money divergence is the
  better-grounded signal. **This is where "works" is genuinely uncertain and only live data resolves it.**
- **Config:** consolidation + consistency audit; caught the half/quarter-Kelly contradiction. Solid.

**The binding uncertainty is the signal's edge** — everything else is sound means. If the edge is real, the
system is well-built to exploit it; if the edge is noise, the sizing will carefully, safely size noise (and
the edge-reality gate + divergence-skepticism are the guards that *should* catch that, but are themselves
unvalidated).

---

## 4. What would make these meet the project's own bar

The specs are a design, not a validated system. To reach the "verify, don't reason" standard:
1. **Run in SIM** (mbappe SimulatedBroker, per integration §8) — validate the cycle, gate, halts, loop
   coordination *mechanically* before live.
2. **Treat the first live days as the null-test for the EDGE** — does the research signal's `p` (and the
   cross-market divergence) actually predict SIG resolution / price-moves better than SIG's own price? Until
   it demonstrably beats that baseline, the directional edge is *unproven* and should be sized minimally
   (lean on arb/flow, which is mechanical and does not depend on the edge hypothesis).
3. **Verify the structural hypotheses explicitly** — measure whether SIG shows the conservative-crowd and
   favorite-longshot biases *before* sizing them up. If the data does not show them, drop them.
4. **Keep the arb/flow path as the un-hypothesized floor** — arb is near-riskless and does not depend on any
   edge hypothesis; it is the part most likely to "work" on day one.

---

## 5. One-paragraph summary

**Audit verdict: the research backing is mostly solid (≈10/13 load-bearing claims real and correctly used; the
risk spec is genuinely strong; the sizing core and integration are sound), with TWO real overclaims now fixed
— Browne's continuous formula is downgraded to a behavioral SHAPE (not a literal drop-in for discrete bets),
and the signal's extremizing + favorite-longshot "edges" are downgraded to plausible-but-unproven HYPOTHESES
to verify against live SIG data (extremizing is a calibration tool that can manufacture fake edge if SIG is
not actually the conservative crowd; favorite-longshot is documented in sports betting, not established for
this market). The one fundamental caveat, and the honest headline: the entire design is THEORETICALLY GROUNDED
but EMPIRICALLY UNVALIDATED — no backtest, no sim, no live tick — so by the project's own "verify don't reason,
beat the null" standard it does not yet qualify; the means (sizing, risk, integration) are sound, but whether
the system WINS depends on whether the research signal's edge is real, which only live/sim data can resolve.
Build it, run it in SIM, treat the first live days as the edge's null-test, lean on the mechanical arb/flow
floor until the edge proves itself, and do not trust the directional edge until it demonstrably beats SIG's
own price.**

*Audit recorded 2026-10-01. Fixes applied to SIG_TOURNAMENT_SIZING_SPEC §1 (Browne-as-shape) and
SIG_RESEARCH_SIGNAL_SPEC §0 (structural-edge-as-hypothesis). All other specs confirmed sound as written.*
