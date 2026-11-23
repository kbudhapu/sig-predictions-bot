# SIG Predictions Cup — Edge Portfolio Spec
### All edges, ranked by confidence/dependency/variance: a mechanical core, medium-confidence signals, and a speculative tail

**Status: STRATEGY SPEC.** This corrects a **premature narrowing**: over the design rounds, the directional
signal spec collapsed ~11 distinct edges down to 2–3 behavioral hypotheses (conservative-crowd/extremizing +
favorite-longshot) — and those two are the *lowest-confidence* ones. The mechanical edges (arb,
market-making, resolution-mechanics) were under-weighted or dropped without justification. This spec pulls
**all** edges back in and ranks them honestly, so the bot leans hardest on the edges most likely to work and
treats the speculative hypotheses as a minimally-sized tail.

**The governing principle (why this structure matters): the edge stack is a PORTFOLIO of independent
sources, not a single bet.** They have different dependencies, confidences, and variances. The project is
**de-risked by the mechanical edges** — which work on day one, depend on no hypothesis, and generate income
while the speculative edges prove themselves or don't. (This is the key insight: the directional edge is
*upside, not foundation* — arb and market-making work regardless of whether any behavioral hypothesis holds.)

**Consistency with the audit:** the audit (`SIG_SPEC_AUDIT.md`) flagged the two behavioral hypotheses as
unproven and named arb as the "un-hypothesized floor." This spec generalizes that: there is a whole
*mechanical tier* that is the floor, not just arb.

---

## 0. The confidence-ranked portfolio (the whole picture in one table)

| # | Edge | Depends on | Confidence | Variance | Tier |
|---|---|---|---|---|---|
| 1 | **Arb** (constraint violations → atomic multi-leg) | nothing (platform-computed) | **Highest** (mechanical) | ~zero | **CORE** |
| 2 | **Market-making** (spread capture, resting limits) | nothing (just the spread) | **High** (mechanical) | Low | **CORE** |
| 3 | **Resolution-mechanics** (decided-vs-officially-resolved) | knowing resolution rules | **High** (factual) | Low | **CORE** |
| 4 | **Cross-market divergence** (SIG vs Polymarket/Kalshi) | real-money markets being sharper | Medium-high | Medium | **SIGNAL** |
| 5 | **Joint-structure mispricing** (joint vs marginal pricing) | modeling correlations | Medium | Medium | **SIGNAL** |
| 6 | **Order-flow inference** (trade tape → fade extremes) | reading student sentiment | Medium | Medium | **SIGNAL** |
| 7 | **Conservative-crowd / extremizing** | SIG being an un-extremized biased crowd | **Low** (unproven) | Medium | **TAIL** |
| 8 | **Favorite–longshot bias** | the bias holding in *this* market | **Low** (unproven) | Medium | **TAIL** |
| — | **Tournament-variance sizing** (Browne) | — | *(not an edge — the sizing that scales all of the above)* | — | *(sizing)* |
| — | **Endgame/election-night positioning** | — | *(not a separate edge — it's edges 1–8 applied in the Q4 repricing window)* | — | *(timing)* |
| — | **Leaderboard game theory** (differentiate from leaders) | leaderboard visibility | *(a sizing/direction modifier, in the sizing spec's §5)* | — | *(sizing)* |

**The structure:** three **CORE** mechanical edges (the income floor, no hypothesis), three **SIGNAL** edges
(medium-confidence, earn their sizing as they prove out), two **TAIL** hypotheses (sized minimally until
verified live). Plus the sizing/timing machinery (Browne, endgame, leaderboard-game-theory) that *scales and
times* all of them but are not themselves edge sources.

**The correction in one line:** the signal spec had the TAIL as the core. The real core is MECHANICAL.

---

## 1. CORE tier — mechanical edges (the income floor, depend on NO hypothesis)

These work on day one, regardless of whether any behavioral/directional hypothesis is true. **They are the
foundation; the project's viability rests here, not on the directional edge.**

### 1.1 Arb (constraint-violation capture)
- **Mechanism:** the platform *itself* computes and publishes constraint violations with
  `suggestedCorrectiveTrades` (`/relationships/constraints`). Capture them via atomic multi-leg orders.
- **Why highest-confidence:** near-riskless by construction (the violation IS the edge, platform-computed);
  no view on any outcome required.
- **Design:** fast loop (integration spec §2/§4). `arb_min_violation` threshold, `arb_max_capital_frac` cap,
  atomic-or-abort, fillability-check against book depth. Most capturable off-hours when attention is thin.
- **Honest limit:** competed (other bots capture too), so *speed* and *off-hours presence* matter; and its
  *size* depends on how often/large violations appear — a recon question (how much arb income is actually
  available). Could be a steady trickle or rare — verify in recon.

### 1.2 Market-making (spread capture) — **wrongly dropped, now restored**
- **Mechanism:** post resting limit orders on *both* sides of a market's spread; earn the spread when both
  fill. Pure liquidity provision — no directional view.
- **Why high-confidence and attractive HERE specifically:** (a) depends on NO hypothesis — you profit from
  the spread existing, not from being right about the outcome; (b) **rate-limit-friendly** — resting orders
  don't burn writes continuously (post once, wait), which matters hugely given 30 writes/min/key; (c)
  low-variance, Sharpe-friendly — the steady-income base; (d) on a *student* market, spreads are likely
  *wider* than on efficient markets → more spread to capture.
- **Design (net-new, market-independent parts):** post symmetric limits inside the current spread; manage
  inventory (if one side fills repeatedly you accumulate a position → skew quotes or widen to rebalance);
  pull quotes on adverse selection (if the market is moving hard one way, your resting order is picking up a
  losing position — widen/pull). Inventory cap feeds the Layer-A exposure gate. **Adverse-selection
  management is the hard part** (you don't want to be the liquidity that informed traders pick off before a
  move), and it connects to order-flow inference (edge #6 — if the tape shows informed flow, pull quotes).
- **Honest limit:** on *very* thin books, there may not be enough two-sided flow to capture; and in the
  volatile endgame, adverse selection spikes (pull back then). Best in the mid-competition grind, on the more
  liquid markets.

### 1.3 Resolution-mechanics — **wrongly dropped, now restored**
- **Mechanism:** know each market's *exact* resolution source and timing. Edge from the gap between
  "effectively decided" and "officially resolved," and from knowing *which markets resolve relative to the
  Nov 4 cutoff* (the sizing spec's endgame depends on this).
- **Why high-confidence:** it's *factual*, not predictive — you're not forecasting, you're knowing the rules
  better than a careless student field. Examples: a race AP has effectively called but SIG hasn't repriced
  to ~1.0 yet (buy toward the known outcome); a market whose resolution criterion is unambiguous once a
  threshold is crossed.
- **Design (market-independent mechanism):** maintain a per-market resolution-rules table (source, timing,
  criterion); flag markets where the *known* resolution state diverges from the SIG price; in the endgame,
  this table drives which convictions are holdable-to-settlement vs must-be-sold-before-cutoff.
- **Honest limit:** the *value* is concentrated around resolution events (election night), and the per-market
  rules are a recon/data-gathering task (pull the resolution info per market). Low steady value, high endgame
  value.

---

## 2. SIGNAL tier — medium-confidence edges (earn their sizing as they prove out)

These depend on a model/signal being right, but are better-grounded than the behavioral hypotheses. Sized
moderately, scaled up only as they demonstrate edge against SIG's price (the audit's "null-test the edge").

### 2.1 Cross-market divergence (SIG vs Polymarket/Kalshi)
- **Mechanism:** real-money markets (Polymarket — **free public API**; Kalshi — AP-backed) price the same
  races. A SIG price diverging from *both* real-money venues (which agree with each other) is a high-
  confidence edge — the student market is the likely-wrong outlier (signal spec §4).
- **Why medium-high:** real-money markets have skin-in-the-game incentives; two independent venues agreeing
  is hard to dismiss. The *most* trustworthy of the directional signals.
- **Design:** signal spec §4 (the cross-market comparable, distinct from the fair-value input). Up-weight
  when the real-money venues agree.
- **Honest limit:** assumes real-money markets are sharper than SIG — *probably* true (real money vs student
  play-money) but still an assumption; and the venues must have a *matching* contract for the race (mapping,
  recon-dependent).

### 2.2 Joint-structure mispricing — **was conflated into favorite-longshot, now its own edge**
- **Mechanism:** markets priced *independently* that are logically *linked* (Senate control = a function of
  the individual Senate races; a candidate winning implies party-control shifts). If the *marginals* are
  priced but the *joint* is mispriced, trade the inconsistency. This is a *quant* edge the student field
  likely can't model.
- **Why medium and distinct:** it's not a crowd-bias (that's the tail); it's a *modeling* edge — build the
  joint distribution from the marginals and find where the composite/control markets are inconsistent with
  their components. Related to arb (#1) but *softer* — arb is a *hard* platform-computed constraint violation;
  this is a *statistical* mispricing the platform doesn't flag.
- **Design:** use the relationship graph (`/relationships/graph`) to find linked markets; model the joint
  (even a simple correlation model of races sharing the national-swing factor); flag where the control-market
  price ≠ the implied-from-components price. The national-swing factor is the same one the sizing's n_eff /
  correlated-cluster uses — shared machinery.
- **Honest limit:** needs a correlation model (which needs data), and the "soft" mispricings may be smaller /
  noisier than hard arb. Medium confidence.

### 2.3 Order-flow inference — **wrongly dropped, now restored**
- **Mechanism:** the trade tape shows student sentiment/flow. Read it; fade extremes (when flow is
  one-sidedly piling into a position, especially a longshot, the student crowd is likely over-reacting).
- **Why medium:** flow-reading is a real signal, but noisy, and "fade the crowd" only works when the crowd is
  *wrong* (not when the flow is *informed*). Distinguishing dumb flow from informed flow is the hard part.
- **Design:** ingest the trade tape (realtime feed); compute flow imbalance per market; flag one-sided
  extremes; *combine with the other signals* (fade flow only when it's *against* the cross-market/fair-value
  view — don't fade flow that agrees with the real-money markets, that's informed). **Also feeds
  market-making (#2) adverse-selection** — if the tape shows informed flow, pull the MM quotes.
- **Honest limit:** noisy; "fade extremes" can be wrong when the flow is informed (right before a real move);
  only trust it in confirmation with other signals.

---

## 3. TAIL tier — speculative behavioral hypotheses (sized MINIMALLY until verified)

These are the two the signal spec wrongly treated as the core. They are *plausible* but **unproven for this
market** (audit §2). Size them *minimally* until live data verifies them; drop them if the data doesn't show
them.

### 3.1 Conservative-crowd / extremizing
- **Hypothesis:** SIG is an over-conservative crowd (pulled toward 0.5), so an extremized fair-value estimate
  has edge. ⚠️ **Unverified** — extremizing improves calibration *on average across questions* but can be
  *more* wrong on a single market, and the edge-vs-SIG claim assumes SIG is the un-extremized crowd, which is
  not established (signal spec §0). Could manufacture *fake* edge.
- **Handling:** compute it, but size the resulting divergence *minimally* and *only* with the divergence-
  skepticism guard, until it demonstrably beats SIG's price at predicting resolution. **Verify before
  trusting; drop if it fails.**

### 3.2 Favorite–longshot bias
- **Hypothesis:** students under-price favorites / over-price longshots. ⚠️ Well-documented in *sports/horse*
  betting; **not established for this student political market** (audit §2).
- **Handling:** flag favorite-longshot-consistent divergences (they're *less* suspect than mid-range ones
  *if* the bias holds), but **measure whether SIG actually exhibits the bias** before sizing it up. Verify,
  then trust or drop.

---

## 4. The sizing/timing machinery (scales and times the edges — not edges themselves)

For completeness, three menu items that are *not* independent edge sources but govern *how* the edges above
are sized and timed (they live in the sizing spec):
- **Tournament-variance sizing (Browne):** scales *all* edges by standing (aggressive-behind, protect-ahead).
  Applies to the SIGNAL and TAIL tiers (directional); the CORE mechanical edges are sized by their own logic
  (arb = capture-fillable, MM = inventory-managed, resolution = conviction-to-settlement).
- **Endgame/election-night positioning:** not a separate edge — it is edges #1–8 applied in the Q4 repricing
  window, with resolution-mechanics (#3) and cross-market/AP data (#4) driving the held convictions.
- **Leaderboard game theory:** a direction modifier (differentiate from visible leaders late) in the sizing
  spec §5.

---

## 5. How the tiers combine (the portfolio in operation)

- **Grind (Q1–Q3):** the **CORE** runs continuously — arb captures violations (fast loop), market-making
  earns spread on the liquid markets (resting limits, rate-cheap), resolution-mechanics maintains the table.
  This is the **income that compounds the bankroll** regardless of any directional view. The **SIGNAL** tier
  layers directional positions at moderate size as it proves out. The **TAIL** is sized minimally / in
  verification.
- **Endgame (Q4 / election night):** market-making *pulls back* (adverse selection spikes); arb continues;
  **resolution-mechanics + cross-market/AP data drive the conviction positioning** (the Browne endgame
  override concentrates the *verified* directional convictions); the TAIL is only in play if it verified.
- **The de-risking:** even if *every* directional edge (SIGNAL + TAIL) fails to prove out, the **CORE
  mechanical edges still generate income** — the bot is not betting its viability on any hypothesis. The
  directional edges are *upside* layered on a mechanical floor.

---

## 6. What's market-independent (designed here) vs recon-dependent

- **Designed now (market-independent):** the taxonomy, the tier structure, the mechanisms for market-making
  (quoting, inventory, adverse-selection), resolution-mechanics (the rules-table approach), order-flow
  (imbalance + confirmation), and how the tiers combine.
- **Waits for recon:** *which* markets support *which* edges — how much arb actually appears (is the CORE arb
  a trickle or a stream?), which markets are liquid enough for market-making, each market's resolution rules,
  which races have Polymarket/Kalshi comparables, the composite/control structure for joint-mispricing. The
  recon pull (the Claude Code reconnaissance) answers these and populates the edge portfolio with real
  targets.

---

## 7. One-paragraph summary

**The edge stack is a confidence-ranked PORTFOLIO, not two hypotheses — correcting a premature narrowing that
had the weakest edges as the core.** The CORE is three MECHANICAL edges that depend on no hypothesis and work
day one: **arb** (platform-computed constraint capture, near-riskless), **market-making** (spread capture via
resting limits — rate-cheap, low-variance, wider spreads on a student market — *wrongly dropped, restored*),
and **resolution-mechanics** (knowing resolution rules/timing better than a careless field — factual, not
predictive — *wrongly dropped, restored*). These are the income floor that compounds the bankroll regardless
of any directional view. The SIGNAL tier is three medium-confidence edges sized as they prove out:
**cross-market divergence** (SIG vs real-money Polymarket/Kalshi — the best-grounded directional signal),
**joint-structure mispricing** (joint-vs-marginal, a quant edge — *restored as its own edge*), and
**order-flow inference** (fade informed-confirmed extremes — *restored*). The TAIL is the two behavioral
hypotheses the signal spec had wrongly centered — **conservative-crowd/extremizing** and **favorite-longshot**
— now sized MINIMALLY and verified-before-trusted (dropped if the data doesn't show them). Tournament-variance
sizing (Browne), endgame positioning, and leaderboard game-theory are the sizing/timing machinery that scales
and times these, not edges themselves. **The project is de-risked by the mechanical core: even if every
directional edge fails, arb + market-making still earn — the directional edge is upside, not foundation.**
Market-independent mechanisms are designed here; which markets support which edges waits for the recon pull.

*Strategy spec. Restores all 11 edges from the original menu, ranked by confidence/dependency/variance.
Corrects the signal spec's premature narrowing (TAIL was treated as CORE). Composes with:
SIG_TOURNAMENT_SIZING_SPEC (scales SIGNAL/TAIL), SIG_RESEARCH_SIGNAL_SPEC (produces the SIGNAL/TAIL inputs),
SIG_INTEGRATION_SPEC (fast loop = arb+MM, slow loop = directional), SIG_RISK_GOVERNANCE_SPEC (gates all),
SIG_CONFIG_SCHEMA (parameters). Polymarket free public API noted (confirm endpoints at build). Recorded
2026-10-01.*
