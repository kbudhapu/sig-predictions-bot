# SIG Predictions Cup — Dynamic Capital Allocation Spec
### Allocating capital across the edge portfolio by realized performance: the live null-test, operationalized

**Status: STRATEGY SPEC (the composing layer).** This sits *above* the edge portfolio
(`SIG_EDGE_PORTFOLIO_SPEC.md`) and decides **how much capital each edge gets**, dynamically, based on what
each edge *actually earns* — not a fixed split. It is the layer that makes the portfolio *adaptive*: capital
flows toward edges that demonstrate performance and away from edges that don't.

**Why this is the right answer to the audit's core problem.** The audit (`SIG_SPEC_AUDIT.md`) said the design
is theoretically grounded but the directional edge is *empirically unproven* — and that by the project's own
"verify, don't reason" standard, you can't know if the edge is real until live data. **Dynamic allocation IS
that verification, operationalized.** Instead of *deciding* upfront whether the directional edge is real, the
allocator *measures* it live: an edge that earns gets more capital; an edge that doesn't gets starved. The
unproven hypotheses (extremizing, favorite-longshot) *cannot* hurt you much, because they never get sized up
unless they *earn* it. This is strictly better than any amount of upfront reasoning about whether the edge
works — it lets the P&L decide.

**Composes with:** the edge portfolio (what the edges ARE), the sizing spec (how a given edge's *allocation*
is turned into position sizes — Browne for directional, capture-fillable for arb, inventory-managed for MM),
and the risk gate (the hard caps the allocator operates within).

---

## 0a. Reconciliation with mbappe's allocator (copy / adapt / diverge — per dimension)

mbappe has **two** allocators: a LIVE one (`PortfolioBrain.allocate_risk_budgets` — inverse-vol × drawdown-
penalty × marginal-Sharpe, recomputed every cycle) and an UNWIRED, more careful design (`cluster_brain.py` /
"PAD" — shrinkage, admission gate, monthly step-capped rebalance). A read-only review established that
**mbappe's *live* allocator is weak in exactly the dimensions that matter here** (noisy small-sample Sharpe
with no min-trades gate, no smoothing → documented 0.84↔0.20 oscillation, and a cold-start bug where a
dataless leg's vol floors to minimum so inverse-vol hands it the *max* weight — a ~4× non-deterministic
position). Its *unwired* PAD design is better but never battle-tested. This spec therefore takes mbappe's
proven parts and diverges from its buggy ones:

| Dimension | Decision | Why |
|---|---|---|
| **Risk-interaction** (propose → cap) | **COPY from mbappe live** | Its "allocate → resolve conflicts → cap by exposure → clamp by governor → zero on halts" flow is solid and battle-tested; the allocator only ever gets *reduced* downstream. Matches §5 here. Also copy mbappe's `sizing_authority.py` *discipline* — one explicit, parity-tested map of the full multiplier stack. |
| **Loss-attribution penalty** | **STEAL from mbappe live** | mbappe weights *down* the leg driving *current* book losses (its `drawdown_contribution`), not just the low-historical-Sharpe one. Good idea — added to the metric (§2). |
| **Rebalance step-cap** | **STEAL from mbappe PAD** | PAD's `max_step = 0.20` (move at most 20% of the gap per rebalance) is the concrete smoothing the live path lacks. Added to §4. |
| **Mechanism (primary driver)** | **ADAPT (diverge from live)** | mbappe is *inverse-vol-first* (right for sizing across volatile equity symbols; wrong for us). We allocate across *edge types by whether they work*, so the primary driver is **performance × mechanical-prior**, with inverse-vol demoted to a within-edge secondary adjustment, not the backbone. |
| **Small-sample handling** | **DIVERGE from live (use shrinkage)** | mbappe live has no min-trades gate / no shrinkage — its softest spot. We use shrinkage-toward-prior (§2), the instinct PAD also has. |
| **Prior** | **DIVERGE from live** | mbappe has no principled prior (de-facto inverse-vol cold-start — the source of its 4× bug). We use the mechanical-core-heavy prior (§3), which *also structurally avoids mbappe's cold-start bug*. |
| **Rebalance cadence** | **DIVERGE from live** | mbappe live recomputes every cycle with no smoothing (→ oscillation). We rebalance cadenced + smoothed + step-capped (§4). |
| **Equities machinery** | **DROP** | BETA/SECTOR/FACTOR tables, `252×26` bars/year, overnight-inventory caps — all equities-specific, do not transfer. |

**The cold-start bug to NOT reintroduce:** mbappe's live allocator reads "no data" as "lowest vol = biggest
weight" (because weight ∝ 1/σ and dataless σ floors to the minimum). **Our guard (§3): a dataless edge
defaults to its PRIOR, never to a vol-derived weight.** This is structural — our backbone is performance×prior,
not inverse-vol, so "no data" means "sit at the mechanical prior," which is the safe default, not "get max
weight."

---

## 1. The core principle: allocate by risk-adjusted realized performance, with a mechanical prior

Each edge gets a capital allocation `w_i` (fraction of deployable capital). The allocation is a function of:
- **Realized performance** — what the edge has actually earned per unit of capital-at-risk, over a rolling
  window (not total P&L — *risk-adjusted*, so a high-variance edge that got lucky doesn't dominate).
- **A prior** — the starting allocation before there's performance data, which is **mechanical-core-heavy**
  (arb + market-making start funded; the directional edges start near-zero and must earn their way up).
- **Confidence/maturity** — how much *data* the edge has produced (an edge with 3 trades hasn't proven
  anything; an edge with 300 has). Low-data edges stay near their prior regardless of early luck.

```
w_i ∝ prior_i × performance_multiplier_i(realized_sharpe_i, n_trades_i)
       normalized so Σ w_i ≤ max_deploy_frac, within the risk gate's caps
```

The mechanical prior is the key safety feature: **the directional edges start starved and earn capital only
by performing**, so the unproven hypotheses can't bleed the bankroll while they fail to prove out.

---

## 2. The performance metric — risk-adjusted, shrunk for small samples

**Do NOT allocate on raw P&L.** Raw P&L rewards an edge that got lucky with high variance. Allocate on a
**risk-adjusted** metric, **shrunk toward the prior for small samples** (so early noise doesn't cause
wild swings):

- **Base metric:** realized return per unit risk over a rolling window — a Sharpe-like ratio
  (mean edge-P&L / std edge-P&L) or, more robustly for skewed prediction-market payoffs, realized P&L per
  unit of capital-deployed with a drawdown penalty.
- **Small-sample shrinkage (critical):** blend the measured metric with the prior, weighted by how much data
  exists: `score_i = (n_i · measured_i + k · prior_i) / (n_i + k)` where `n_i` = trades/observations and `k`
  = a shrinkage constant (an edge needs ~k observations before its measured performance outweighs its prior).
  This stops a 2-trade lucky streak from grabbing all the capital, and stops a 2-trade unlucky start from
  killing a good edge. **This is the same estimation-error discipline as the sizing spec's fractional-Kelly —
  don't trust a noisy estimate.**
- **Rolling window, not all-time:** recent performance matters more (an edge can decay — e.g. arb dries up as
  others compete it, a market-making regime ends when volatility spikes). Exponential decay on older data.
- **Loss-attribution penalty (stolen from mbappe):** multiply the score *down* for an edge that is driving the
  *current* book losses — not just an edge with low historical Sharpe, but the one *actively bleeding right
  now*. `penalty_i = 1 − min(loss_share_i, cap)` where `loss_share_i` = this edge's share of the book's
  current realized losses. This catches a *decaying* edge faster than the rolling Sharpe alone (an edge that
  was good and just turned bad gets penalized immediately by its loss share, before the rolling window fully
  updates). Composes as: `score_i = shrunk_risk_adjusted_i × loss_attribution_penalty_i`.

---

## 3. The mechanical-core prior (the starting allocation)

Before performance data, capital is allocated by the edge-portfolio tiers (confidence-ranked):

| Tier | Edges | Prior allocation (pre-data) | Rationale |
|---|---|---|---|
| **CORE** | arb, market-making, resolution-mechanics | **Bulk** (e.g. 70%) | Mechanical, work day-one, no hypothesis. The income floor. |
| **SIGNAL** | cross-market divergence, joint-structure, order-flow | **Modest** (e.g. 25%) | Medium-confidence; funded enough to generate data to prove themselves. |
| **TAIL** | extremizing, favorite-longshot | **Minimal** (e.g. 5%) | Unproven; just enough to test, can't hurt much. |

These are *priors*, not fixed — they're where each edge *starts* and what its score shrinks toward with low
data. As edges prove out, the allocation *moves* from these starting points. (The percentages are
`config/sig.yaml` DEFAULTs, tuned live.)

**Important asymmetry:** an edge can earn its way UP from its prior, but the mechanical CORE edges have a
**floor** they don't drop below as long as they're still working (arb that's capturing keeps its allocation
even if a flashy directional edge appears) — because abandoning a working riskless edge to chase a
higher-variance one is the overbetting trap. The CORE floor protects the income base.

**The dataless-defaults-to-prior guard (avoiding mbappe's cold-start bug).** mbappe's live allocator reads
"no data" as "lowest vol → biggest weight" (weight ∝ 1/σ, and a dataless σ floors to the minimum) — a bug
that 4×'d positions non-deterministically. **We must not reintroduce it.** The guard is structural: our
backbone is performance × prior, NOT inverse-vol, so **a dataless edge sits at its PRIOR, never at a
vol-derived weight.** "No data" → "stay at the mechanical prior" (arb/MM funded, directional starved) — the
*safe* default, not "get max weight." Any inverse-vol adjustment is a *within-edge secondary* term that only
applies once there IS data; with no data, it is inert and the prior governs.

---

## 4. Rebalancing cadence and smoothing

- **Rebalance on a cadence** (e.g. every few hours / daily), NOT every trade — churning the allocation on
  every fill is noise-chasing and burns the write budget re-sizing positions.
- **Smooth the transitions with a STEP-CAP (stolen from mbappe's PAD):** move at most `max_step` of the gap
  toward the new target per rebalance (PAD uses `max_step = 0.20` — move ≤20% of the distance each time), with
  freed budget going to cash/reserve rather than being instantly redeployed. This is the concrete fix for
  mbappe's *live* bug (per-cycle recompute with no smoothing → the documented 0.84↔0.20 oscillation). A
  step-cap means even a wild single-period performance estimate can only nudge the allocation, never whipsaw
  it. `max_step` is a config DEFAULT.
- **Tournament-phase awareness** — the rebalance interacts with the quarter schedule / Browne standing:
  - **Grind (Q1–Q3):** allocate for steady risk-adjusted growth — the CORE-heavy performance-weighted mix.
  - **Endgame (Q4, if behind):** the allocator *defers to the Browne endgame override* — when the sizing
    spec says "concentrate the verified directional convictions for the skyrocket," the allocator *permits*
    that concentration (shifts capital to the highest-performing directional edge) even though it raises
    variance, because P(top-3) > growth then. When *ahead*, the allocator tightens toward the low-variance
    CORE (protect). So late-game, the allocator and the Browne multiplier agree: concentrate-if-behind,
    protect-if-ahead.

---

## 5. Interaction with the other layers (who decides what)

Clean separation of responsibilities:
- **The allocator decides `w_i`** — the *fraction of capital* each edge gets.
- **Each edge's own sizing decides position sizes WITHIN its allocation** — arb sizes by capture-fillable, MM
  by inventory-management, directional by Browne-tournament-×-σ_p-shrinkage. The allocator doesn't size
  individual positions; it sets the *budget* each edge sizes within.
- **The risk gate caps everything** — the allocator proposes `w_i`; the hard exposure caps (max_deploy,
  per-market, per-cluster) still bind. The allocator operates *inside* the risk envelope, never overrides it.
- **The dynamic allocation is itself gated** — a newly-promoted edge (one that just started performing) still
  faces the edge-reality gate and divergence-skepticism; good recent performance doesn't bypass the per-trade
  safety checks.

---

## 6. The honest failure modes (and guards)

- **Performance-chasing a lucky streak** → the small-sample shrinkage (§2) and the rebalance smoothing (§4)
  guard against it; an edge needs sustained data to move far from its prior.
- **Abandoning a decaying edge too slowly** → the rolling window with exponential decay (§2) catches decay
  (arb drying up, a regime ending) faster than an all-time metric would.
- **The CORE floor masking a genuinely-dead mechanical edge** → if arb *stops working entirely* (e.g. the
  market gets efficient), even the floor should release — the floor is "don't abandon a *working* edge," not
  "fund a dead one forever." Monitor: if a CORE edge's realized performance goes to ~zero over a full window,
  its floor is lifted and capital reallocates.
- **Over-reacting in the thin-data early days** → early in the competition there's little performance data,
  so allocations stay near the mechanical-core prior (which is the *right* default anyway — lean on arb/MM
  while everything proves out). The allocator naturally starts conservative and *becomes* adaptive as data
  accumulates.
- **The allocator and Browne fighting in the endgame** → resolved in §4: late-game they agree by
  construction (concentrate-if-behind defers to Browne; protect-if-ahead tightens to CORE).

---

## 7. What's market-independent (designed here) vs live-dependent

- **Designed now:** the whole mechanism — performance-weighted allocation, small-sample shrinkage, the
  mechanical-core prior structure, rolling-window decay, rebalance cadence/smoothing, the layer separation,
  the endgame deferral to Browne.
- **Live-dependent (config DEFAULTs, tuned live):** the exact prior percentages (70/25/5 placeholder), the
  shrinkage constant k, the rolling-window decay rate, the rebalance interval, the CORE-floor levels. All
  `config/sig.yaml` DEFAULTs — ship reasonable, tune to observed behavior.

---

## 8. One-paragraph summary

**Capital is allocated across the edge portfolio dynamically, by risk-adjusted REALIZED performance, starting
from a mechanical-core-heavy prior (arb + market-making funded, directional edges starved until they earn
their way up).** The performance metric is risk-adjusted (not raw P&L) and **shrunk toward the prior for
small samples** (an edge needs sustained data, not a lucky streak, to move far from its starting allocation —
the same estimation-error discipline as fractional-Kelly), over a **rolling window with decay** (so decaying
edges — arb competed away, a market-making regime ending — lose allocation fast). Rebalancing is **cadenced
and smoothed** (not per-trade), and **phase-aware**: in the grind it's the CORE-heavy performance mix; in the
endgame it **defers to the Browne override** (concentrate the best directional edge if behind, tighten to the
low-variance CORE if ahead). The allocator sets each edge's *budget*; each edge's own sizing (capture-fillable
/ inventory / Browne) sizes positions *within* that budget; the risk gate's hard caps bind over everything.
**This is the audit's "verify the edge live" made mechanical: the directional edges can't bleed the bankroll
while unproven (they start starved), and they earn capital only by actually performing — the P&L decides
whether the edge is real, not upfront reasoning.** The CORE mechanical edges keep a floor (don't abandon a
working riskless edge to chase variance) that lifts only if the edge genuinely dies. Market-independent
mechanism designed here; the prior percentages, shrinkage constant, window, and cadence are config DEFAULTs
tuned live.

*Strategy spec (composing layer). Composes with SIG_EDGE_PORTFOLIO_SPEC (the edges allocated across),
SIG_TOURNAMENT_SIZING_SPEC (within-edge sizing + the endgame override the allocator defers to),
SIG_RISK_GOVERNANCE_SPEC (the caps it operates within), SIG_CONFIG_SCHEMA (its tunable DEFAULTs). The
operationalization of the audit's "null-test the edge live." Reconciled against mbappe's allocator (§0a):
COPIES its proven propose-then-cap risk-interaction and sizing_authority discipline; STEALS its
loss-attribution penalty and PAD's step-cap; DIVERGES from its live inverse-vol-first mechanism, its
missing small-sample shrinkage, its unprincipled prior, and its un-smoothed per-cycle rebalance (all
documented weaknesses — including the cold-start 4× bug we structurally avoid). Recorded 2026-10-01.*
