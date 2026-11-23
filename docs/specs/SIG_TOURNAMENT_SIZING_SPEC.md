# SIG Predictions Cup — Tournament Sizing / Risk-Manager Module Spec
### Position sizing that maximizes P(finish top-3), grounded in Browne's probability-maximizing theorem

**Status: MODULE SPEC** for the SIG bot's risk manager. This is the component that decides *how much* to
put on each position, given the bot's tournament standing. It is the piece that most separates a tournament
*winner* from a good *trader* — because the objective is **not** "maximize expected balance" (what Kelly
does) but **maximize the probability of finishing in the top 3** of the field by the Nov 4 noon cutoff.

**Why this is a different problem than normal trading.** Standard Kelly sizing maximizes the long-run growth
rate of wealth (expected log-wealth). That is the *wrong* objective here: finishing with a safely-grown but
mid-pack balance pays **zero** (only top-3 pays), and going "broke" when you weren't going to place anyway
costs **nothing**. The right objective — maximize P(final balance ≥ the 3rd-place threshold) — is a solved
problem in the literature (Browne's probability-maximizing strategy), and it prescribes systematically
*different* bet sizes than Kelly: **more aggressive than Kelly when behind, less aggressive when ahead,
escalating as the deadline nears.**

---

## 0. The competition mechanics this sizing must respect (from the rules + API)

- **No settlement before the cutoff.** No market pays out $1/$0 during the competition. Balance grows only
  by (a) **realized** gains — selling a position for more than you paid (locks SUSQies) — and (b)
  **unrealized** mark-to-market on held positions (counts for interim rank, but is *not locked* until sold).
- **Final standings settle the frozen positions.** Trading stops at noon Nov 4; positions freeze; final rank
  is based on how those frozen positions *settle* to $1/$0 after the election resolves. So a correct
  outcome-conviction held through the freeze pays the full $1 at final settlement.
- **Interim rank is mark-to-market; final rank is settlement.** This creates two phases (§4).
- **Prices are 0–1 on a 0.005 tick; the metric is final SUSQie balance; winner-take-all top-3 of a large
  field.** The leaderboard API (`/tournaments/{slug}/leaderboard`) gives all ranks + balances → we can read
  the **3rd-place threshold** live. The bot's own balance is in `/portfolio/*`.

**Implication the sizing is built around:** gains must be *actively managed* (locked by selling, or held as
convictions into the freeze). Passive holding gives unrealized MTM that can reverse. So sizing works
alongside a hold/sell rule (§3) and splits into a grind phase and a positioning phase (§4).

---

## 1. The theoretical foundation (Browne's probability-maximizing strategy)

The problem "maximize P(wealth reaches target b by time T)" has a known optimal solution (Browne). For
current wealth π ≤ b, the value function (the probability of success under optimal play) is

```
J(π, t) = Φ( Φ⁻¹(π/b) + (μ−r)/σ · √(T−t) )
```

and the optimal fraction-at-risk is

```
α(π, t) = 1/(σ√(T−t)) · φ(ν)/Φ(ν),   ν = Φ⁻¹(π/b)
```

with φ, Φ the standard-normal density and CDF. **This is exactly our objective** (π = our balance, b = the
top-3 threshold, T−t = time left). Its behavior *is* the correct tournament policy:

- **Aggressiveness ∝ 1/√(time remaining)** — escalates smoothly then sharply toward the deadline (the exact
  "ramp up late" schedule; √2× aggressive at the halfway point, 2× at the three-quarter point, diverging in
  the final hours).
- **φ(ν)/Φ(ν) encodes distance-to-target.** Our standing ρ = π/b = balance/threshold is *unbounded* (it
  can exceed 1 when ahead), so we map it into the (0,1) argument Φ⁻¹ needs with the **odds transform**
  `u = ρ/(1+ρ)`, then `ν = Φ⁻¹(u)`. This is monotone on all ρ > 0 and places **ν = 0 exactly at ρ = 1**.
  (The literal `ν = Φ⁻¹(ρ)` is inconsistent here — Φ⁻¹(1) = +∞, not 0, and every ρ > 1 would clamp to the
  same value, erasing "the further ahead." The as-built code uses the odds transform; see §2.2.)
  - **ρ < 1 (behind):** u < 0.5, ν < 0, φ(ν)/Φ(ν) large → **over-bet; more aggressive the further behind.**
  - **ρ = 1 (at threshold):** u = 0.5, ν = 0 → moderate (φ(0)/Φ(0) ≈ 0.80).
  - **ρ > 1 (safely in):** u > 0.5, ν > 0, φ(ν)/Φ(ν) → 0 → **de-risk; protect; approach cash the further ahead.**
- **Memoryless:** the optimal choice depends *only* on ρ (distance to target) and time remaining — not on
  path. The bot needs only (balance, threshold, time-left) to size. (Viswanathan: the optimal choice at
  every point depends only on the probability of reaching that point.)
- **Extreme case (Viswanathan):** late + far behind → optimal bet → **1.00 (all-in)**, because there is
  little downside to betting everything when the deadline is near and a sub-threshold balance is worth zero.

**Honest scope of the theory — READ THIS BEFORE IMPLEMENTING `M`.** Browne's closed form assumes one
continuously-rebalanced risky asset with constant volatility σ and a *fixed* target in a Black–Scholes world.
**Our bets are discrete, binary, settle-at-the-end — so the literal formula `α = (1/σ√(T−t))·φ(ν)/Φ(ν)` does
NOT apply to our contracts.** Do **not** implement that continuous equation directly on discrete prediction-
market bets — that would be wrong. What transfers from Browne is the **behavioral SHAPE**, which is robust and
correct for our objective:
- aggressiveness **increases as the deadline nears** (the 1/√(time-remaining) shape),
- aggressiveness **increases the further BEHIND the target** (ρ = B/T₃ < 1 → over-bet),
- aggressiveness **decreases the further AHEAD** (ρ > 1 → protect, toward cash),
- the response depends **only on ρ and time-remaining** (memoryless).

**Implement `M(ρ, τ)` as a function with that shape** — the φ(ν)/Φ(ν)·(1/√τ) form below is the *reference
shape* to reproduce (and a reasonable discrete approximation of it), NOT a literal continuous-time control to
apply to settle-at-end contracts. The three further gaps the continuous form ignores — (1) discrete binary
settle-at-end bets, (2) multiple *correlated* positions, (3) a *moving* target (the 3rd-place threshold
shifts as others trade) — are handled by the adaptations in §2–§5 (the multivariate base, the live re-read of
ρ/T₃, the endgame override). **Use Browne for the shape and the direction; never pretend the closed form is a
literal solution to our discrete problem.**

### 1a. The multivariate layer (why per-position Kelly is wrong, and the tractable fix)

Browne gives the *tournament tilt* (how aggressive, by standing). But the *base allocation* — how to split
capital across our **simultaneous, correlated** book of election positions — is its own solved problem, and
naive per-position sizing gets it wrong. Three research facts drive the base:

- **Simultaneous ≠ sequential, and not just smaller.** For multiple bets held *at once* (our case — we hold
  many election positions simultaneously), the optimal wagers are not only *smaller* than sequential-Kelly
  but their **distribution across positions is drastically different** — it is a *joint* optimization over the
  outcome space, not a per-position Kelly computed independently then capped. Sizing each position alone and
  summing is a real error.
- **Portfolio variance scales with the number of concurrent bets.** n simultaneous independent bets multiply
  the book's standard deviation by ≈√n; the control is to bet **1/√n of full Kelly** so total book variance
  stays at the single-bet level. For **correlated** bets (ours — many races share the national-swing factor),
  the effective count `n_eff` is *smaller* than the raw count, and correlation requires *further* reduction
  (quarter-Kelly-grade) because the positions "share the same underlying risk factor."
- **The joint optimization is now tractable (previously it was not).** Exact multivariate Kelly scales
  O(2ᴺ) (enumerate all outcome combinations) — intractable for a real book. The Bloomberg integral-transform
  method (*Efficient Multivariate Kelly Optimization*, 2026) reduces the independent-bet case to **O(N)**,
  solving hundreds of positions accurately, with a decomposition approach for the correlated case carrying
  bounded suboptimality. So we can compute the *correct* joint allocation, not a crude approximation.

**So the base is a multivariate (joint) quarter-Kelly allocation across the correlated book — not per-position
Kelly** — and Browne then tilts *that* by tournament standing (§2).

### 1b. The governing posture: robustness to estimation error (overbetting is the enemy)

This is the most important principle in the spec, and it governs everything below. Our edges `p` are
**estimated** (by the election-research LLM aggregating public forecasts) — they are *not* known truth. The
literature is emphatic about what this means:

- **The penalty for OVERbetting is far worse than the penalty for UNDERbetting** (Thorp, who operationalized
  Kelly in practice, has *consistently* recommended half-Kelly or less for exactly this reason). Overbetting
  interacts catastrophically with the tail events that estimation error produces; underbetting merely leaves
  some growth on the table. **Therefore every ambiguous sizing decision resolves toward SMALLER.**
- **Estimation error is a SYSTEMATIC bias, not random noise that cancels.** When you size by Kelly on
  estimated probabilities, "outcomes with overestimated values consistently look more favourable than in
  reality, with larger wagers placed on them than should be." The bot will tend to bet *biggest* on exactly
  the positions where it *overestimated* the edge. This does not average out — it is a persistent tilt toward
  the worst positions. **This is the single most dangerous property of Kelly-on-estimates and the module must
  actively counter it** (via uncertainty-based shrinkage, §2.1a).
- **The binding constraint is edge REALITY, not the sizing formula.** "The task is not merely to construct an
  accurate model, but to identify *relative* edge against the market, and to distinguish genuine opportunity
  from estimation error." A perfect sizing formula on a *fake* edge loses; a crude formula on a *real* edge
  wins. The sizing is already more than good enough; the module's job is to be **robust to the edge being
  wrong**, because it often will be.

**Everything downstream is biased toward conservatism by this posture.** The aggression Browne adds
(§2.2–2.3) is permitted *only* on edges that pass the reality/confidence gate (§2.1, §5) — never on shaky
ones.

---

## 2. The sizing rule (what the module computes)

The rule is **a multivariate (joint) quarter-Kelly base allocation, tilted by Browne's tournament
multiplier, with a Browne-authorized variance override in the endgame-if-behind:**

```
base_alloc      = multivariate_quarter_kelly(edges, correlations, prices)   # §2.1 — joint, O(N)
tournament_size = base_alloc × M(ρ, τ)                                      # §2.2 — Browne tilt
final_size      = endgame_override(tournament_size, ρ, τ)                   # §2.3 — variance when behind+late
                  (then caps/liquidity/edge-gate, §5)
```

### 2.1 The base — multivariate (joint) quarter-Kelly across the correlated book
Per-position edge (Kelly-for-prediction-markets): with fair-value estimate `p` and market price `p_m`,

```
f* = (p − p_m) / (1 − p_m)          # buy YES; symmetric form for NO
shares q* = f* · W / p_m
```

But do **not** size positions independently. Compute the **joint** allocation across the whole simultaneous
book:
- **Joint optimization** over the correlated positions (the Bloomberg O(N) integral-transform method for the
  independent factors, its decomposition for the correlated residual) — because the simultaneous-optimal
  allocation is a *different distribution* than per-position Kelly, not just a scaled-down one (§1a).
- **QUARTER-Kelly, not half,** as the fraction — because our edges are **both** uncertain (estimated from the
  election-research LLM signal, §6) **and** correlated (shared national-swing factor), and the research says
  *each* of those pushes toward quarter: half-Kelly cuts variance 75% at 25% growth cost, quarter-Kelly keeps
  ~75% of growth at ~25% of variance, and quarter is the standard for *uncertain-edge* and *correlated*
  books. Half was slightly too hot for our estimation-and-correlation reality; quarter is the honest base.
- **Portfolio variance scaling 1/√(n_eff):** scale the book so total variance stays controlled, where `n_eff`
  is the *effective* number of independent bets (correlation makes `n_eff` < raw position count — compute it
  from the correlation structure, §6; a book of 10 positions all loading on one swing factor has `n_eff` near
  1, so it is scaled as ~one bet, not ten).
- **Edge gate — absolute and REALITY-based:** zero allocation unless **both** `f* > 0` **and** the research
  signal's confidence in `p` clears a threshold (§5). "Positive estimated edge" is not enough — the estimate
  must be *trustworthy enough to act on*, because the binding constraint is distinguishing real edge from
  estimation noise (§1b). A positive but low-confidence edge is estimation noise and gets zero.

#### 2.1a Per-position uncertainty shrinkage (Baker–McHale — the key robustness fix)
Do not apply a *blanket* quarter-Kelly. Shrink **each position by an amount that increases with that
estimate's uncertainty** `σ_p` (the research signal reports a confidence / standard error per market, not
just a point `p`). The optimal shrinkage factor rises with `σ_p`: confident estimates (sources agree, stable
polling, thick history) are shrunk *less*; shaky estimates (sources disagree, thin/volatile data) are shrunk
*much more*. Form:

```
f_shrunk,i = k(σ_p,i) · f*_i ,   with k(σ_p) decreasing in σ_p   (k → ~0.5 confident, → ~0.1 or less shaky)
```

This is strictly better than a constant fraction because our edge-confidence *varies across markets*, and it
directly counters the systematic overbetting bias (§1b): the positions most likely to be overestimated are
the uncertain ones, and those get shrunk hardest. (Quarter-Kelly is the *average* this produces across a
mixed-confidence book — not a floor applied uniformly.)

#### 2.1b Market-impact adjustment (your own fills move the price)
SIG's prediction markets are thin. Your own orders push the price toward fair value as you fill, so a large
bet *destroys its own edge*. Size against the **impact-adjusted edge** — the edge remaining after your fill
moves `p_m` — not the observed top-of-book edge. Practically: estimate the fill-through price from the
orderbook depth, use the *average* fill price (not the current best) in `f*`, and never size beyond the depth
that keeps the impact-adjusted edge positive. (Benter/Woods — who actually won at scale — sized with explicit
own-market-impact limits. On a thin book this materially reduces the "optimal" size, in the safe direction.)

### 2.2 The tournament tilt — Browne's aggressiveness
Let **ρ = B / T₃** (bot balance ÷ live 3rd-place threshold from the leaderboard) and **τ = fraction of
competition time remaining** (1.0 at start → 0 at the Nov 4 cutoff). Define

```
u = ρ / (1 + ρ)                            # ODDS TRANSFORM: map unbounded ρ>0 into (0,1) for Φ⁻¹
ν = Φ⁻¹( clamp(u, ρ_min, ρ_max) )          # clamp is a numerical backstop on u (e.g. [0.01, 0.99])
M_raw(ρ, τ) = (1 / √(max(τ, τ_floor))) · ( φ(ν) / Φ(ν) )
M(ρ, τ)     = M_raw(ρ, τ) / M_0             # normalized so M ≈ 1 at the reference point (calibration, §7)
```

**Why the odds transform `u = ρ/(1+ρ)`, NOT the literal `ν = Φ⁻¹(ρ)`** (this is the as-built form in
`strategies/sizing/browne.py` — a deliberate, load-bearing deviation): `ν = Φ⁻¹(ρ)` is internally
inconsistent because ρ = B/T₃ is unbounded while Φ⁻¹ needs an argument in (0,1). Taken literally it breaks
twice — (1) ρ = 1 would clamp to ρ_max = 0.99 and give M ≈ 0.034 at the very point `M_0` defines as M ≈ 1;
(2) every ρ > 1 clamps to the same value, so "aggressiveness decreases the further ahead" becomes
unrepresentable. The odds transform is monotone on all ρ > 0 and places **ν = 0 exactly at ρ = 1**, so the
reference point, `M_0`, and all four behavioural requirements hold simultaneously (ρ→0 ⇒ u→0 ⇒ ν→−∞ ⇒ over-
bet; ρ=1 ⇒ u=0.5 ⇒ ν=0 ⇒ M≈1; ρ→∞ ⇒ u→1 ⇒ ν→+∞ ⇒ de-risk). This is the §1 licence: reproduce the SHAPE and
direction, never pretend the closed form is a literal solution.

- `τ_floor` (e.g. a few hours as a fraction of the 34 days) caps the 1/√τ divergence so the final hours do
  not produce an infinite number — the cap is where "bet essentially everything" takes over (§5).
- `M_0` is the calibration constant pinning M = 1 at the reference point (ρ = 1, τ = 0.5). It equals
  `(1/√0.5)·φ(0)/Φ(0) ≈ 1.1284` (the value of `M_raw` at that reference, since the odds transform puts
  ν = 0 at ρ = 1); config ships `M_0 = 1.1284` and re-tunes it once the live field is visible (§7).

Behavior (what M does):
- **Behind + late** (ρ ≪ 1, τ small): **M ≫ 1** → concentrate hard (up to the all-in cap).
- **Mid-field + time left** (ρ ≈ 1, τ ≈ 0.5): **M ≈ 1** → half-Kelly, compound.
- **Ahead** (ρ > 1): **M → 0** → protect, de-risk, prefer selling-to-lock (§3).

### 2.3 The endgame override (the one place variance-control is deliberately violated)
The §2.1 base is **variance-controlled** (quarter-Kelly, 1/√n_eff, joint) — correct during the GRIND, where
you want to compound without blowing up. But Browne's tilt and the variance-control base optimize *opposite*
things in the endgame-if-behind, and the resolution is explicit:

- **Grind, or any time M ≤ 1:** the variance-controlled base holds. Diversify across edges, keep book
  variance at the single-bet level, compound. Do not seek variance.
- **Endgame + behind (M > 1, ρ < 1, τ small): Browne is AUTHORIZED to OVERRIDE the base's variance control**
  — and *this is correct*, because when safe-finish pays zero you *want* the variance the base would avoid.
  (This does not contradict the overbetting-penalty posture of §1b: that posture is about *growth-*
  maximizing, where overbetting is self-defeating; the endgame override serves *P(top-3)*, a different
  objective where concentration into the tail is the only path to the prize.) Here the module deliberately
  **inverts** the 1/√n_eff diversification: it **concentrates on the single highest-edge correlated
  *scenario*** (the national-swing cluster), producing the *bimodal* distribution (all-hit → skyrocket past
  the target, all-miss → crater — and crater costs nothing since you were not placing anyway). N *independent*
  edges converge (LLN) to a *medium* balance that never reaches a far target; the *correlated concentrated*
  bet is the only shape that reaches it.
- **The sharpened condition (the one place this round tightens the aggression):** the override concentrates
  **only on high-edge *AND HIGH-CONFIDENCE* convictions** — never onto a shaky estimate. A big concentrated
  bet on an *uncertain* edge with no recovery time is the overbetting trap in its most lethal form (large
  size × bad estimate × no time to recover). So: rank convictions by edge *gated on confidence* (§2.1a),
  concentrate into the highest-edge *trustworthy* scenario only, **treat the cluster as one bet**, hold it
  through the Nov 4 freeze (§3) so it settles to $1 if right. The further behind and later, the harder the
  concentration — up to the capped all-in (§5) — but *only ever* on edges confident enough to stake the
  tournament on.

**The rule, stated once:** variance-controlled base normally; Browne-authorized variance-*seeking*
(concentrated correlated cluster) only in the endgame-if-behind. The override is not a bug in the
variance-control — it is the whole point of a probability-of-top-3 objective diverging from a growth
objective.

### 2.4 De-risk when ahead (protect)
When `M < 1` (ρ > 1, in contention): the base's variance control *tightens further* — bias hard toward
**selling / locking** (§3). Minimize settlement variance — convert unrealized-and-risky into
realized-and-safe. Hold only the very highest-edge, highest-confidence convictions; sell everything whose
price has reached fair value, and even some positive-edge positions, because locked certainty is worth more
than extra EV when defending a prize rank. (Ahead, you never invoke the §2.3 override — variance is your
enemy.)

---

## 3. The hold / sell rule (edge-based, standing-conditioned) — NOT level-based

**Core correction to the naive instinct:** the hold/sell decision is driven by **edge (your estimate `p`
vs the current price `p_m`), NOT by the probability level** ("hold the 85%+ ones"). And **holding is the
*variance* choice, not the safe one** — an 85¢ position that settles YES pays $1 (gain 15¢) but has a 15%
chance of settling NO (lose the 85¢ you could have locked). Selling locks the value; holding gambles it.

- **Sell (lock)** when the price has reached or exceeded your fair value (`p_m ≥ p`): the edge is gone,
  realize the gain, redeploy the capital into something that still has edge.
- **Hold** only when you still have edge (`p > p_m`): the market is still underpricing the true outcome
  probability, so the expected settlement value exceeds the sell price. (A 60% position priced at 45¢ — 15¢
  of edge — is a *better hold* than a 90% position priced at 90¢ — zero edge, sell it.)
- **Standing modifies the bias:**
  - **Ahead (ρ > 1):** bias toward selling even some positive-edge positions — reduce settlement variance,
    protect the rank. Holding is the gamble; locking is safety.
  - **Behind (ρ < 1, late):** bias toward *holding* the concentrated correlated convictions *through the
    freeze* — they settle to $1 if right, and that frozen-settlement is the skyrocket. Do not sell these into
    strength; you want them frozen and settling.

---

## 4. The two phases (because interim rank is MTM, final rank is settlement)

- **GRIND phase (most of the competition, roughly Q1–Q3):** grow *realized* balance via active trading —
  capture price moves, **sell to lock** when price reaches fair value (§3), redeploy, compound. Sizing here
  is **half-Kelly × M(ρ,τ)** with M near 1 (unless already far behind/ahead). The goal is to *grow the
  capital that the endgame will deploy*, without blowing up.
- **POSITIONING phase (final stretch into the Nov 4 freeze, roughly Q4 and especially the final 48h):** size
  to **be frozen holding the right outcome-convictions**, because those settle to $1 and set the final rank.
  M is large if behind (concentrate correlated convictions, hold through the freeze — the skyrocket) or small
  if ahead (de-risk, lock, protect). The 1/√τ term makes the transition from grind to positioning *automatic*
  — aggressiveness naturally escalates as τ → 0.

The quarterly "ramp if behind" schedule the operator reasoned (low variance Q1; assess + begin escalating at
the halfway Q2/Q3 boundary if still behind; commit Q3; max Q4) **is the discrete read-out of the 1/√τ ×
φ(ν)/Φ(ν) surface** — it is not a separate rule, it is what the formula does. Escalation starts at the
halfway point (not the last week) precisely because 1/√τ has already grown by √2 there, *and* because
concentrated bets placed at halfway have time to work/compound or to be re-tried if they miss, whereas a
last-week concentration has no recovery runway.

---

## 5. Caps and guards (so the aggression never becomes recklessness)

- **Never literally 100%** on a single position, even at the all-in limit: keep a small reserve (e.g. ≤ 95%
  into one scenario) for optionality and against a single estimation error being fatal before the freeze.
- **The edge-REALITY gate is absolute:** no position is sized up unless **both** `f* > 0` **and** the
  research signal's confidence in `p` clears a threshold (the estimate must be trustworthy, not merely
  positive — §1b, §2.1). Aggression concentrates *real, confident* edge harder; it never manufactures a bet
  from no edge *or from a shaky one*. (The single most important guard — "over-bet when behind" means
  "over-bet your *confident* edges," never "gamble on noise or on a guess.")
- **The overbetting-asymmetry posture resolves all ties toward SMALLER** (§1b, Thorp): when uncertain between
  two sizes, take the smaller; when a parameter (σ, M_0, confidence) is uncertain, assume the more
  conservative value. Underbetting costs a little growth; overbetting a wrong edge loses.
- **Uncertainty shrinkage (§2.1a) and market-impact adjustment (§2.1b) are applied before any cap** — the cap
  is a backstop, not the primary control; the primary control is shrinking for estimate-uncertainty and
  own-impact *first*.
- **Rate/liquidity reality:** a computed size is clipped to what the orderbook can actually fill and what the
  30-writes/min-per-key governor permits; an unfillable "optimal" size is reduced to the fillable size
  (never half-fill a correlated multi-leg and end up with a naked leg).
- **ρ is mapped** through the odds transform `u = ρ/(1+ρ)` and `u` clamped into a Φ⁻¹-safe range (§2.2);
  `τ_floor` caps the 1/√τ divergence.
- **Half-Kelly base** (not full) absorbs estimation error before M amplifies.
- **Correlation awareness:** the sum of correlated-cluster sizes is governed as *one* position against the
  caps, so "concentrate the cluster" cannot secretly become an over-leveraged whole book.

---

## 6. The inputs this module needs (and where they come from)

- **B (bot balance), positions, fills** — `/portfolio/*` (live).
- **T₃ (3rd-place threshold) and the field shape** — `/tournaments/{slug}/leaderboard` (live). ρ = B/T₃.
- **τ (time remaining)** — competition clock (Nov 4 noon cutoff).
- **p AND σ_p (fair-value estimate AND its uncertainty) per market** — the **election-research signal** (the
  LLM + web-search aggregator). It must report *both* a fair-value probability `p` *and* a confidence /
  standard error `σ_p` per market (how much the sources agree, how stable/thick the data is) — because `σ_p`
  drives the per-position uncertainty shrinkage (§2.1a) and the reality gate (§2.1, §5). *The sizing module is
  only as good as these estimates* — no confident edge ⇒ no sizing ⇒ no directional bet (only the
  structural/arb flow trades without a directional view). **An estimate with no reported uncertainty is
  treated as maximally uncertain (shrunk to ~zero).**
- **Orderbook depth per exchange** — `/exchanges/{id}/orderbook` — for the market-impact adjustment (§2.1b):
  the fill-through / average-fill price, so size is set against the impact-adjusted edge, not the top-of-book
  edge.
- **Correlation structure + n_eff** — the relationship graph (`/relationships`, `/relationships/graph`) + the
  research signal's scenario factors (e.g. a national-swing factor loading per race). Used two ways: (a) to
  compute `n_eff`, the *effective* number of independent bets, for the 1/√(n_eff) portfolio scaling (§2.1) —
  positions all loading on one factor collapse toward n_eff ≈ 1; (b) to identify the correlated cluster for
  the endgame concentration (§2.3). A practical n_eff: from the positions' factor-loading covariance,
  `n_eff = (Σ wᵢ)² / (wᵀ Σ w)` (the participation-ratio / inverse-Herfindahl of the correlation-adjusted
  weights) — 1 when fully correlated, N when independent.
- **The multivariate solve** — the joint quarter-Kelly allocation (§2.1) via the Bloomberg O(N) integral-
  transform for the independent factor part + decomposition for the correlated residual. Implement from the
  published method; it is O(N), tractable for the whole book each cycle.
- **σ (opportunity volatility)** — estimated from the field's balance dispersion and price volatility;
  feeds M_0 calibration (§7).

---

## 7. Calibration (the honest "needs live data" part)

The *functional form* above is fixed (derived from Browne). Two things require live data and are tuned once
the competition is observable, not guessable now:
- **M_0** — the normalization pinning M ≈ 1 at the reference point. Set a reasonable launch value; re-tune
  once the real leaderboard distribution is visible (is the field bunched? one runaway leader? tight at the
  top?), because "how much to over-bet to catch the leader" depends on the *actual* gap and field spread.
- **σ and τ_floor** — the volatility scale and the end-game cap, tuned to the observed price/outcome
  volatility and the real time-granularity of the endgame.
- **Re-read ρ and T₃ every cycle** off the live leaderboard — the target moves as others trade, so sizing is
  recomputed continuously, never fixed.

Until live, the module is implemented with the derived formula and launch-estimate constants; the first days
of real leaderboard data calibrate M_0/σ. This is not a gap in the derivation — it is the irreducible fact
that "how hard to bet to reach the target" depends on where the target actually is.

---

## 8. One-paragraph summary (the rule the bot runs)

**Base × tilt × override.** The BASE is a **multivariate (joint) quarter-Kelly** allocation across the whole
simultaneous correlated book — not per-position Kelly — edge-gated (`f* = (p−p_m)/(1−p_m) ≤ 0` → zero),
scaled by **1/√(n_eff)** so book variance stays controlled (correlation shrinks n_eff toward 1), solved
jointly via the O(N) multivariate method. Quarter-Kelly (not half) because our edges are both estimated
(uncertain) and correlated — both push toward quarter. The TILT is **Browne's multiplier M(ρ, τ) =
(1/√τ)·φ(ν)/Φ(ν) / M_0, ν = Φ⁻¹(u), u = ρ/(1+ρ)** (the odds transform — §2.2; monotone on ρ>0, ν=0 at
ρ=1), ρ = balance ÷ live 3rd-place threshold, τ = time-fraction-remaining, normalized ≈1 at mid-field/mid-time — it over-bets when behind (more so the further behind and later),
sits at base mid-field, shrinks toward cash when ahead. The OVERRIDE: only in the **endgame-if-behind**
(M > 1, late) is Browne authorized to *invert* the variance-controlled base and **concentrate on the single
highest-edge correlated scenario** (the bimodal skyrocket), held through the Nov 4 freeze to settle to $1 —
because when safe-finish pays zero, variance is wanted. Hold/sell is **edge-based** (hold while `p > p_m`,
sell to lock when price reaches fair value) and **standing-conditioned** (ahead → sell/lock to cut
settlement variance, never override; behind → hold the concentrated correlated cluster through the freeze).
ρ, T₃, and n_eff are re-read live every cycle; M_0/σ calibrate to the real field. This is the
probability-maximizing strategy (Browne) on a correlation-aware multivariate base (quarter-Kelly, joint),
not growth-maximizing per-position Kelly — because the objective is **P(finish top-3)**, not expected balance.

**Governing it all (the robustness posture):** edges are *estimated and will be wrong*, overbetting is far
worse than underbetting, and the systematic bias is to bet biggest on the positions you overestimated. So the
module shrinks **per-position by estimate-uncertainty σ_p** (confident edges shrunk less, shaky ones shrunk
toward zero — Baker–McHale), sizes against the **impact-adjusted edge** (your own fills move thin prices —
Benter/Woods), gates on **edge REALITY not just positivity** (a positive-but-uncertain edge is noise → zero),
resolves **every tie toward smaller** (Thorp), and even the endgame skyrocket override concentrates **only on
high-edge-AND-high-confidence** convictions. The sizing formula was already more than good enough; the real
constraint is distinguishing genuine edge from estimation error, and the module is built to be robust to the
edge being wrong.

*Spec grounded in: Browne's probability-maximizing strategy (target-reaching by a deadline, the tournament
tilt); Kelly-for-prediction-markets (f* = (p−p_m)/(1−p_m)); the multivariate/simultaneous-Kelly literature
(joint allocation ≠ per-position; 1/√n portfolio scaling; Bloomberg's O(N) "Efficient Multivariate Kelly
Optimization" 2026 for tractable joint solving); Viswanathan (discrete/extraneous-wealth, all-in limit,
path-independence); and the estimation-robustness literature (Baker–McHale uncertainty-based bet shrinkage;
Thorp's overbetting-asymmetry and half-Kelly-or-less; Benter/Woods real-world fractional-Kelly + own-impact
limits; KellyBench's "distinguish genuine opportunity from estimation error"). The grind uses the variance-
controlled, uncertainty-shrunk, impact-adjusted base; the endgame-if-behind uses the Browne-authorized
variance override, restricted to high-confidence edges. Recorded 2026-10-01.*
