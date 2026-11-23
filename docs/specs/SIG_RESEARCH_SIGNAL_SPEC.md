# SIG Predictions Cup — Election-Research Signal Architecture Spec
### Producing (p, σ_p, divergence) per market: forecast aggregation, calibration, and edge detection

**Status: ARCHITECTURE SPEC** for the signal component the directional strategy depends on. The sizing module
(`SIG_TOURNAMENT_SIZING_SPEC.md`) is "only as good as its `p` and `σ_p` inputs" — this spec defines where
those come from. The signal's job, per SIG market: produce a **calibrated fair-value probability `p`**, a
**confidence `σ_p`** (the uncertainty of that estimate), and a **divergence** (`p − p_m`, the edge vs SIG's
price). It is market-structure-independent in *mechanism* (designable now); only the per-race source mapping
waits for the live market list.

**The key architectural principle (consistent with the risk spec's LLM/deterministic separation):** the LLM
does **search and extraction** (find the current numbers from each source for each race) — a role where it is
reliable. The **probability judgment is deterministic math** on the extracted numbers (aggregation,
calibration, σ_p). The LLM never *judges* the probability itself (where it would hallucinate or inherit
bias); it *gathers the inputs* and the deterministic pipeline computes the output.

---

## 0. The core framing: a structural-edge HYPOTHESIS (plausible, NOT yet proven — verify live)

A naive signal ("aggregate forecasts → compare to SIG price") mostly reproduces the public consensus, which
SIG's market already reflects — little edge. The forecast-aggregation literature suggests a *better* framing:
on a **student-driven market**, there *may* be systematic, exploitable crowd biases the signal can catch
*without out-predicting anyone*. **These are HYPOTHESES to verify against live SIG data, not established facts
about this market — treat them as plausible priors, and the sizing's edge-reality gate + divergence-
skepticism must guard against them being wrong:**

- **HYPOTHESIS 1 — SIG is an over-conservative crowd, so extremizing creates edge.** The literature shows
  aggregated forecasts tend too conservative (over-predict unlikely, under-predict near-certain), and
  extremizing away from 0.5 improves calibration **on average across many questions.** ⚠️ **Two honest
  caveats:** (a) extremizing improves calibration *in aggregate* — on any *single* market it can make you
  *more* wrong; (b) the claim that this creates *edge vs SIG* assumes **SIG itself is the un-extremized
  conservative crowd — which is UNVERIFIED.** If SIG's market is already well-calibrated (or over-extremized),
  extremizing *manufactures fake edge* and loses. So extremizing is a **calibration tool, not a proven
  edge-generator**; the edge claim must be checked: does the extremized `p` actually predict SIG-market
  resolution better than SIG's own price? Until verified live, size the resulting divergence *cautiously*
  (the divergence-skepticism curve applies).
- **HYPOTHESIS 2 — favorite–longshot bias holds in this market.** It is well-documented in **sports/horse
  betting** and some markets (underpriced favorites, overpriced longshots). ⚠️ **Whether it holds in THIS
  student political market is a hypothesis, not established** — political prediction markets are not horse
  races, and the direction/magnitude must be verified against live SIG prices-vs-outcomes before it is a
  trusted edge, not assumed.

**So the signal's value is twofold, with honest confidence levels:** (a) *informational* — fair-value and
especially the **cross-market divergence from real-money venues** (§4) is the better-grounded signal (two
independent real-money markets disagreeing with SIG is harder to explain away than a calibration-tool
artifact); and (b) *structural* — the conservative-crowd + favorite-longshot hypotheses, which are **plausible
but unproven for this market** and must earn trust from live data before being sized up. **Do not treat the
structural edge as a reliable floor until it is verified; treat the cross-market real-money divergence as the
more trustworthy signal in the interim.**

---

## 1. Sources — the ACTUAL, accessible source architecture (API-first, don't scrape JS apps)

The deepest research finding: **sources are NOT independent — 538-style models, polling aggregates, and
markets all read the same polls**, so averaging them does *not* give √n error reduction (shared information
and shared bias). Two consequences: (a) confidence must be *inflated* for shared-information (§3); (b) **the
real edge is a source that is genuinely *different*.** And critically — **nothing says SIG prices track any of
these**, so Polymarket/Kalshi are BOTH fair-value inputs AND *independent cross-market comparables* (a
SIG-vs-Polymarket price gap is a standalone edge, regardless of "true" probability).

**The accessibility reality (learned the hard way): do NOT try to scrape the JS-rendered platform pages**
(polymarket.com, kalshi.com render client-side — a fetch returns the shell, not the data). Instead, read each
source the way it is *actually* accessible:

| Source | How to access (ACTUAL) | Role | Independence |
|---|---|---|---|
| **Polymarket** | **Public data API** (CLOB/gamma — structured JSON, no scraping). Updates ~every 5 min. Covers Senate/House/Governor/ballot/specials. | Primary real-money signal AND cross-market comparable | **Sharpest distinct** — largest real-money venue, tens of $M on midterms |
| **Kalshi** | **Trading/data API** (structured JSON). Federally regulated. **Licensed AP election data for 2026** → live AP vote counts + race calls integrated. | Primary real-money signal AND comparable AND **the election-night resolution-grade source** | Distinct real-money; AP-backed |
| **Aggregator/tracker sites** (DeFi Rate, predictionmarketspicks, covers, tradetheoutcome) | **Plain-HTML, web-search/fetch readable** (tables of odds; updated 5–15 min). They **pre-compute the Polymarket-vs-Kalshi comparison** and flag gaps. | Fallback for market prices if APIs are hard; AND a ready-made cross-venue divergence cross-check | — |
| **Forecast models** (538 generic ballot, Cook Political Report, Sabato's Crystal Ball) | **Web search** (published readable pages/ratings) | The model layer | **Correlated cluster** — count as ~one |
| **AP vote data** (via Kalshi) | Through the Kalshi API, election night | **The endgame repricing driver** — fastest clean "who's actually winning" as results land (AP: ~7,000 races, 99.9% accuracy 2024) | Gold-standard resolution |
| **News / events** | **LLM web search** | Event-driven *updates* between numeric refreshes (candidate drops, scandal) | — |

**The LLM's narrowed role (sharper than before):** **hit the structured APIs for real-money prices (clean,
robust — no LLM needed for those numbers); web-search the readable sources for the model layer and trackers;
reserve LLM *reasoning* for synthesizing NEWS events** — not for scraping prices it can get cleanly from APIs.
This is more reliable than "LLM reads prediction-market websites" (which hits the JS-shell wall). APIs for
structured data, search for readable HTML, LLM-reasoning for news synthesis.

**Weighting:** start with **simple (equal-weight) aggregation across distinct classes** — the real-money
markets (Polymarket, Kalshi) each as their own class, the model+poll cluster (538/Cook/Sabato/polls) counted
as ~one — because we have **no per-source track record for 2026 races** and simple averaging is the robust
"reduces noise" baseline. Only move to quality-weighting if a track record is established live. Do **not**
over-engineer weights without data.

**Tournament-provided resources:** SIG's platform may show third-party news/polls *on its own site* (behind
the same access wall as its docs) and the SIG trading API does not obviously expose a news/polls endpoint.
**Do not depend on SIG-provided resources** — the external ecosystem above is richer, independent, and
accessible. (Worth a one-time check once read-only works: does any SIG endpoint expose news/polls? But plan
around the external sources regardless.)

---

## 2. Aggregation — in LOG-ODDS space, then EXTREMIZE

1. **Convert each source estimate to log-odds** `ℓ = ln(p/(1−p))`. (Averaging probabilities directly is wrong
   — 0.9 and 0.1 average to 0.5, destroying information; log-odds is the correct geometry.)
2. **Aggregate** (equal-weight mean of the distinct-class log-odds, §1): `ℓ̄ = mean(ℓ_classes)`.
3. **EXTREMIZE away from 0.5** to correct the conservative-aggregate bias: `ℓ_cal = a · ℓ̄` with an
   extremizing factor `a > 1` (the literature's standard; `a` is a calibration parameter, tuned — start
   modest, e.g. 1.1–1.3, since over-extremizing is its own error). Convert back: `p = 1/(1+e^{−ℓ_cal})`.
4. The result `p` is the **calibrated fair-value estimate.** The extremizing is where the *structural* edge
   against the conservative student crowd is created (§0).

---

## 3. σ_p — the confidence, from source DISPERSION (inflated for shared information)

The sizing module needs `σ_p`; the research gives the method (ANOVA / variance-of-predictions): **the
dispersion across sources IS the uncertainty.**

- **Base σ_p = the spread of the source estimates** in log-odds space (sources agree tightly → small σ_p,
  confident; sources disagree → large σ_p, uncertain).
- **Inflate σ_p for shared information:** because sources are correlated (§1), their agreement *overstates*
  true confidence — tight agreement among sources that all read the same polls is not as informative as tight
  agreement among *independent* sources. So apply a shared-information inflation (down-weight the apparent
  confidence). Concretely: weight the dispersion by the *effective* number of independent source classes
  (the real-money market counts as its own; the model+poll cluster counts as ~one), not the raw source count.
- **Inflate σ_p for staleness and thinness:** old estimates (not refreshed) and races with thin/sparse data
  → higher σ_p. A race with one stale poll is maximally uncertain.
- **Output σ_p directly to the sizing module**, which shrinks the bet by it (per-position uncertainty
  shrinkage — the shakier the estimate, the smaller the bet). **An estimate with no computable σ_p (e.g. a
  single source) is treated as maximally uncertain** → shrunk toward zero by the sizing module.

This makes `σ_p` principled (dispersion-based), honest (inflated for the correlation and staleness the naive
variance ignores), and directly consumable by the sizing stack.

---

## 4. Divergence detection — the edge (with favorite-longshot flagging)

For each SIG market, compute **divergence = `p − p_m`** (`p_m` = SIG's current price). This is the raw edge
the sizing module sizes. Two refinements from the research:

- **Flag favorite-longshot patterns specifically.** When `p_m` is a *favorite* (high) and `p > p_m` (SIG
  under-pricing the favorite) → the confirmed structural bias → a higher-confidence edge. When `p_m` is a
  *longshot* (low) and `p < p_m` (SIG over-pricing the longshot) → fade. These structural-bias divergences are
  *more trustworthy* than a mid-range divergence (which is more likely noise), and can be marked as such for
  the sizing/risk layers.
- **Feed the divergence through the risk layer's divergence-skepticism (not raw).** An *extreme* divergence
  on a famous, well-analyzed race is more likely a *broken estimate* than free money (risk spec §4). So the
  signal reports the divergence; the risk layer's concave divergence-skepticism curve shrinks the size on
  implausibly-large edges. The signal *detects*; the risk layer *discounts the suspicious ones*. (A
  favorite-longshot-consistent divergence is *less* suspect than a divergence against the known bias.)

**Two distinct uses of Polymarket/Kalshi (keep them separate):**
- **As fair-value INPUTS:** their prices feed the log-odds aggregate → `p` (§2).
- **As independent COMPARABLES:** `p_m^SIG − p_m^Polymarket` (and vs Kalshi) is a *standalone* edge signal —
  the student market disagreeing with a *real-money* market is itself informative, independent of the
  aggregate `p`. A SIG price that diverges from *both* real-money venues (which agree with each other) is a
  **high-confidence** edge (two independent real-money venues agreeing, SIG the outlier → SIG is likely the
  one that is wrong, since it is student-driven). The signal computes and reports *both* the aggregate
  divergence (`p − p_m^SIG`) and the direct cross-market divergences (`p_m^SIG` vs each real-money venue),
  and the cross-market one is weighted up when the real-money venues agree with each other.

**The election-night endgame (Q4):** when results land (Nov 3–4), markets reprice violently — and this is
where the tournament is decided (sizing spec positioning phase). The fastest clean "who is actually winning"
signal is **Kalshi's AP-integrated vote data** (live counts + race calls). In the endgame, the signal
prioritizes AP-via-Kalshi over slow-moving forecast models (which lag), so the bot's `p` tracks the *actual
unfolding result* and the directional convictions held into the freeze are anchored on real returns, not
pre-election models.

---

## 5. The component architecture (LLM-on-droplet, async)

```
  ┌───────────────────────────────────────────────────────────────┐
  │ RESEARCH SIGNAL COMPONENT (async, own cadence, LLM + web)       │
  │                                                                 │
  │  per race:                                                       │
  │   1. LLM + web search → EXTRACT current numbers per source      │  (LLM = search/extract only)
  │      class (real-money markets, models, polls, news)            │
  │   2. DETERMINISTIC: log-odds aggregate (equal-weight classes)   │  (no LLM in the math)
  │   3. DETERMINISTIC: extremize away from 0.5                     │
  │   4. DETERMINISTIC: σ_p = dispersion, inflated (shared-info,    │
  │      staleness, thinness)                                       │
  │   5. DETERMINISTIC: divergence = p − p_m (SIG price from state) │
  │      + favorite-longshot flag                                   │
  │   6. WRITE {market_id, exchange_id, p, σ_p, divergence, flags,  │
  │      updated_at} → shared local state                           │
  └───────────────────────────────────────────────────────────────┘
                         │ writes (p, σ_p, divergence) per market
                         ▼
          shared local SQLite state  →  consumed by the SLOW loop's sizing (integration spec §7)
```

- **Cadence:** periodic refresh (e.g. every N minutes/hours per race — more frequent near events, approaching
  election night). *Not* every cycle — the LLM/web calls are expensive and the numbers move slowly except on
  news. News-driven re-pulls on detected events.
- **The LLM/brain:** the free-hosted-model path (OpenAI-compatible endpoint — Groq/Cerebras/etc., same client
  as the edge-engine) or a stronger API, doing *extraction* (find the current Polymarket/Kalshi/538 number
  for race X), not judgment. Its uncertainty is bounded because it is reading-and-reporting numbers, not
  estimating probabilities.
- **Writes to the same shared state** the integration spec defines (§7: `{market_id, exchange_id, p, σ_p,
  updated_at}` + the divergence/flags). Staleness tracked via `updated_at` → old entries treated as
  low-confidence by the sizing module.

---

## 6. Honest limits and the live-dependent parts

- **The per-race source MAPPING waits for the live market list** — which SIG markets exist, and which
  Polymarket/Kalshi/538 contracts correspond to each ("SIG's Kansas Senate market = Polymarket's Kansas
  Senate contract = Kalshi's = 538's Kansas rating"). The *mechanism* (extract → aggregate → extremize → σ_p
  → divergence) is fixed here; the *mapping* is built once the markets are visible. This is the main
  genuinely-blocked part.
- **A build dependency: Polymarket + Kalshi API access.** Both have public data APIs (Polymarket CLOB/gamma;
  Kalshi trading/data API) — a small, concrete integration to set up (endpoints, any keys), parallel to the
  SIG one. Not hard, but a real prerequisite for the clean real-money-price path (the fallback is reading the
  tracker sites via web search, which works but is less clean).
- **The extremizing factor `a` and the shared-info inflation are calibration parameters** — start with
  literature-standard values (a ≈ 1.1–1.3), tune against observed SIG-price-vs-outcome calibration once there
  is data. (Over-extremizing is its own error — the overbetting-asymmetry posture applies: when unsure, less
  extremizing.)
- **No per-source track record for 2026** → equal-weight-across-classes, not quality-weighted, until a record
  exists. Do not fabricate weights.
- **The structural edge (§0) is the robust floor; the informational edge is the upside.** Even if our raw
  forecasts just match the consensus, the extremizing + favorite-longshot flagging catches the student
  market's predictable biases. That is the edge that does not depend on being a better forecaster.

---

## 7. One-paragraph summary

**Per SIG market, the signal produces `(p, σ_p, divergence)`. Sources are read the way they are ACTUALLY
accessible: Polymarket + Kalshi via their public APIs (structured JSON — no scraping the JS apps), forecast
models (538/Cook/Sabato) + tracker sites via web search, news via LLM reasoning — the LLM does
search/extraction and news synthesis, NEVER price-scraping it can get cleanly from an API, and NEVER the
probability judgment (which is deterministic math). The DETERMINISTIC pipeline: aggregate in LOG-ODDS space
(equal-weight across distinct classes — each real-money market its own, the model+poll cluster as one, no
track record to weight by), EXTREMIZE away from 0.5 (correcting the conservative-aggregate bias — structural
edge against the student crowd), set σ_p from source DISPERSION inflated for shared-information + staleness +
thinness, and compute divergence with FAVORITE-LONGSHOT flagging. Polymarket/Kalshi serve TWO roles: as
fair-value INPUTS to `p`, AND as independent COMPARABLES — a SIG price diverging from *both* real-money venues
(which agree with each other) is a high-confidence edge (the student market is the likely-wrong outlier).
The edge is partly STRUCTURAL (extremizing + favorite-longshot catch known student biases without
out-forecasting anyone — the robust floor) and partly INFORMATIONAL (divergence from sharp real-money markets
— the upside). In the Nov 3–4 ENDGAME, the signal prioritizes Kalshi's AP-integrated live vote data (the
fastest clean "who's actually winning") so the held convictions track real returns. Runs async, slow/
news-triggered, writing to shared state with staleness tracked. Only the per-race source mapping and the
Polymarket/Kalshi API setup wait; the mechanism is fixed here.**

*Spec grounded in: the forecast-aggregation literature (log-odds/logit aggregation — Satopää et al.;
extremizing-away-from-0.5 for calibration — Ariely/Turner/meta-beliefs recalibration; variance-of-predictions
for σ_p — ANOVA updating; shared-information caps on wisdom-of-crowds — Palley-Soll; favorite-longshot and
domain-specific calibration decomposition in prediction markets; simple-averaging robustness without track
records vs quality-weighting with them). Composes with SIG_TOURNAMENT_SIZING_SPEC.md (consumes p, σ_p),
SIG_RISK_GOVERNANCE_SPEC.md (divergence → skepticism curve), SIG_INTEGRATION_SPEC.md (async component,
shared-state handoff). LLM in search/extract role only, per the deterministic-decision principle. Recorded
2026-10-01.*
