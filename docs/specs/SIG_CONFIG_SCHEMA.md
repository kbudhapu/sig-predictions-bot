# SIG Predictions Cup — Master Config Schema & Consistency Audit
### Every tunable parameter in one place, each marked safe-default vs tune-live; inconsistencies across the specs flagged and resolved

**Status: CONFIG SCHEMA + CONSISTENCY AUDIT.** This consolidates every parameter referenced across the five
SIG specs (sizing, risk, integration, signal, plus the plumbing) into one schema the builder wires from, so
no value is hunted across documents or left implicit. The act of consolidating is also a **consistency
audit** — §0 lists the contradictions/gaps the consolidation surfaced and resolves them (the real value of
doing this on paper before build).

**Two markings on every parameter:**
- **[DEFAULT]** — has a sensible launch value, usable immediately; refine later if wanted.
- **[TUNE-LIVE]** — *requires* live data to set correctly (field shape, calibration vs outcomes, real market
  structure); ships with a conservative placeholder and is tuned once live.

**Governing posture (from the risk/sizing specs): when a value is uncertain, choose the more conservative
(smaller-bet) one.** Overbetting is far worse than underbetting; "fail small, not freeze" bounds that so the
conservatism never reaches zero-trade.

---

## 0. CONSISTENCY AUDIT — what the consolidation surfaced (fix these)

1. **half-Kelly vs quarter-Kelly contradiction (REAL — must resolve).** The sizing spec's early sections say
   "half-Kelly base"; the later estimation-robustness round changed it to "quarter-Kelly" (because our edges
   are both uncertain *and* correlated). Both terms remain in the doc. **RESOLUTION: quarter-Kelly is the
   base** (the later, research-grounded decision supersedes the earlier half-Kelly). The config has ONE
   parameter, `kelly_base_fraction = 0.25`, and the sizing module uses it everywhere — "half-Kelly" in the
   older passages should read as "the fractional-Kelly base," now 0.25. *(Flagged for a one-line cleanup in
   the sizing spec; the config is the source of truth.)*

2. **Two different edge formulas appear** — `f* = (bp−q)/b` (odds form) and `f* = (p−p_m)/(1−p_m)`
   (prediction-market/price form). These are the *same* formula in different notations; the price form is
   the one that matches SIG's 0–1 priced contracts. **RESOLUTION: use the price form** `f* = (p−p_m)/(1−p_m)`
   (buy YES) throughout — it is the SIG-native one.

3. **σ_p is REQUIRED by sizing but PRODUCED by the signal** — consistent, but the *contract* must be pinned:
   the signal emits `σ_p` in **log-odds dispersion units**, and the sizing module's shrinkage `k(σ_p)`
   consumes those units. Pin the units so they match (log-odds, not probability-space). Flagged so the two
   modules agree on units.

4. **"Exposure cap" referenced in both risk (Layer A) and integration (shared-state)** — consistent (same
   cap, enforced at the gate against the combined book), but ensure ONE parameter `max_deploy_frac` drives
   both, not two separate values. Resolved: one parameter (§2).

5. **Loop cadences referenced qualitatively ("every few seconds / few minutes") but never pinned** — gap.
   Resolved: explicit `fast_loop_interval_s`, `slow_loop_interval_s` (§4), marked DEFAULT (tune to observed
   arb frequency + rate budget).

6. **Divergence-skepticism curve shape referenced but never parameterized** — gap. Resolved: an explicit
   concave-curve parameter set (§3), marked TUNE-LIVE (calibrate to observed divergence-vs-reality).

7. **The arb path's sizing was said to "skip Layer B / skip tournament sizing" but its OWN caps were only
   implied** — gap. Resolved: explicit arb params (§5): `arb_min_violation`, `arb_max_capital_frac`.

No *logical* contradictions beyond #1 (which is a doc-wording artifact of the research rounds, resolved to
quarter-Kelly). The specs compose cleanly; the gaps were unpinned values, now pinned below.

---

## 1. GLOBAL / MODE

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `mode` | `SIM` → `LIVE` | DEFAULT | integration §8 | Run SIM (mbappe SimulatedBroker) first; swap to LIVE after validating the cycle. |
| `tournament_slug` | (the SIG midterm slug) | DEFAULT | API | From `/tournaments`. |
| `cutoff_utc` | 2026-11-04 16:00Z (noon ET) | DEFAULT | rules | Competition end; drives τ. |
| `competition_start_utc` | 2026-10-01 16:00Z | DEFAULT | rules | For τ = time-fraction-remaining. |

## 2. CAPITAL / EXPOSURE (Layer A hard gate)

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `max_deploy_frac` | 0.85 | DEFAULT | risk §2, integ §5 | Max fraction of balance in open positions; keep a reserve. ONE value drives risk + integration. |
| `max_per_market_frac` | 0.15 | DEFAULT | risk §2 | Per-market cap — *except* the sizing-authorized endgame concentration (§3 override). |
| `max_cluster_frac` | 0.25 | DEFAULT | risk §2 | Per-correlated-cluster cap (the binding control for correlated books; n_eff-aware). |
| `min_order_frac` | 0.005 | DEFAULT | lit. | Don't place a bet below ~0.5% of bankroll (Kelly-after-adjustments floor). |
| `erroneous_max_order_notional` | (hard sanity ceiling) | DEFAULT | risk §2 | Reject absurd orders (the Knight-Capital bug catcher). |

## 3. SIZING — Kelly base, Browne tilt, risk shrinks

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `kelly_base_fraction` | **0.25** | DEFAULT | sizing §2.1 | Quarter-Kelly (RESOLVES audit #1). The fractional-Kelly base before Browne tilt. |
| `edge_formula` | `(p − p_m)/(1 − p_m)` | fixed | sizing §2.1 | Price form (RESOLVES audit #2). |
| `M_0` (Browne normalization) | **1.1284** | **TUNE-LIVE** | sizing §2.2,§7 | As-built default = `(1/√0.5)·φ(0)/Φ(0)`, which pins M≈1 at the reference point (ρ=1, τ=0.5) under the odds transform. Still re-tunable to the real field distribution. |
| `tau_floor` | **0.01** | DEFAULT | sizing §2.2 | Caps the 1/√τ divergence in the final hours. (As-built `risk.tau_floor = 0.01`.) |
| `rho_clamp` | [0.01, 0.99] | DEFAULT | sizing §2.2 | Config `risk.rho_min`/`risk.rho_max`. Backstop on `u = ρ/(1+ρ)` (the odds transform), NOT on ρ directly — see sizing §2.2. |
| `sigma_opportunity` | placeholder | **TUNE-LIVE** | sizing §2.2,§7 | Opportunity volatility; from observed price/field volatility. |
| `n_eff_method` | participation-ratio `(Σw)²/(wᵀΣw)` | DEFAULT | sizing §6 | Effective independent-bet count for 1/√n_eff scaling. |
| `kelly_shrinkage_k(σ_p)` | k→0.5 confident, →≤0.1 shaky | DEFAULT (shape) / TUNE-LIVE (breakpoints) | sizing §2.1a | Per-position uncertainty shrinkage (Baker–McHale); consumes σ_p in log-odds units (audit #3). |
| `divergence_skepticism_curve` | concave: linear to `div_trust`, sub-linear to `div_suspect`, shrink beyond | DEFAULT (shape) / **TUNE-LIVE** (breakpoints) | risk §4 | `div_trust` ≈ 0.08, `div_suspect` ≈ 0.20 placeholders — big "edges" shrink (likely broken estimate). |
| `volatility_scaling_ref` | placeholder | **TUNE-LIVE** | risk §4 | Reference volatility; size shrinks as market vol rises above it. |
| `confidence_floor_σp` | (max σ_p to act on) | DEFAULT | risk §4, signal §3 | Above this σ_p → shrink to zero (estimation noise, not edge). Single-source/no-σ_p ⇒ maximally uncertain ⇒ zero. |

## 4. LOOPS / CADENCE / WRITE-BUDGET (integration)

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `fast_loop_interval_s` | 5 | DEFAULT | integ §2 (audit #5) | Arb-watch cadence; bounded by read limit. |
| `slow_loop_interval_s` | 300 | DEFAULT | integ §2 (audit #5) | Strategy re-evaluation cadence. |
| `writes_per_min_per_key` | 30 | fixed | API | Hard SIG limit. The outbound write-gate. |
| `reads_per_min_per_key` | 100 | fixed | API | Hard SIG limit. |
| `arb_write_reservation_frac` | 0.4 | DEFAULT | integ §6 | Fraction of each write-window reserved for the fast/arb loop so quoting can't starve it. |
| `n_keys` | 1 → 3 | DEFAULT | integ §6 | Start 1; add up to 3 (fast/strategy/overflow). Per-key budgets. |
| `verify_by_reread` | true | fixed | integ §5 | Each loop re-reads state from disk at cycle top (disk is truth). |

## 5. ARB PATH (fast loop — near-riskless, skips Browne/Layer-B)

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `arb_min_violation` | 0.02 | DEFAULT | integ §4 (audit #7) | Minimum constraint `violationAmount` worth capturing (after cost). |
| `arb_max_capital_frac` | 0.20 | DEFAULT | integ §4 (audit #7) | Cap on capital in arb at once (still inside `max_deploy_frac`). |
| `arb_atomic_only` | true | fixed | integ §4 | Multi-leg fills atomically or aborts — never leg into a naked position. |
| `arb_fillability_check` | true | fixed | integ §4 | Confirm orderbook depth fills the suggestedCorrectiveTrades before firing. |

## 6. RESEARCH SIGNAL (aggregation, calibration, sources)

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `signal_refresh_interval_min` | 30 (grind) → fast (endgame/news) | DEFAULT | signal §5 | Slow normally; news-triggered + faster near Nov 3–4. |
| `aggregation_space` | log-odds | fixed | signal §2 | Not probability-space averaging. |
| `source_weighting` | equal-weight across distinct classes | DEFAULT | signal §1 | No 2026 track record → simple averaging; model+poll cluster counts as one. |
| `extremize_factor_a` | 1.2 | DEFAULT / **TUNE-LIVE** | signal §2 | Push away from 0.5 (conservative-aggregate correction); 1.1–1.3 range; tune to calibration. Over-extremizing is its own error → when unsure, lower. |
| `sigma_p_shared_info_inflation` | by effective-independent-classes | DEFAULT | signal §3 | Inflate σ_p because sources share polls (agreement overstates confidence). |
| `sigma_p_staleness_inflation` | rises with `updated_at` age | DEFAULT | signal §3 | Stale estimate → higher σ_p. |
| `cross_market_divergence_weight` | up-weight when real-money venues agree | DEFAULT | signal §4 | SIG-vs-(Polymarket & Kalshi agreeing) = high-confidence edge. |
| `favorite_longshot_flag` | on | fixed | signal §4 | Flag underpriced-favorite / overpriced-longshot (trusted more than mid-range divergence). |
| `endgame_source_priority` | Kalshi/AP first (Nov 3–4) | DEFAULT | signal §4 | AP live vote data drives `p` during result repricing. |
| `source_endpoints` | Polymarket API, Kalshi API, 538/Cook/Sabato (web), trackers (web) | **TUNE-LIVE** | signal §1 | API endpoints/keys + per-race mapping set when markets visible. |

## 7. RISK / SAFETY / SUPERVISOR

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `drawdown_throttle_grind` | tight (e.g. shrink sizes on >X% fast drop) | DEFAULT / TUNE-LIVE | risk §4 | Standing-aware; tight in grind, loosened in authorized endgame. |
| `drawdown_throttle_endgame` | loosened | DEFAULT | risk §4 | Endgame concentration is *expected* to swing; don't stop-loss it. |
| `stale_data_halt_s` | (max data age before halt-hold) | DEFAULT | risk §5 | Feed older than this → halt-hold-auto-recover. |
| `recon_mismatch_halt` | any mismatch → halt | fixed | risk §5 | Never silently adopt either side; reconcile then resume. |
| `repeated_error_halt_count` | e.g. N consecutive order errors | DEFAULT | risk §5 | → halt-hold + informational alert. |
| `runaway_detect` | rate-pinned / rapid-position / rapid-balance | DEFAULT / TUNE-LIVE | risk §5 | Layer-C supervisor trip → halt-but-HOLD. |
| `killswitch_default_mode` | HALT-BUT-HOLD | fixed | risk §6 | Autonomous triggers never auto-flatten; manual-only flatten. |
| `undertrading_alarm` | flag if trades << expected over window | DEFAULT | risk §0 | The "fail small, NOT freeze" monitor — under-trading is a failure. |
| `supervisor_independent` | true | fixed | risk §1 | Separate failure domain; acts even if main loop deadlocks. |

## 8. STARTUP / ELIGIBILITY / OPS (ported mbappe + SIG)

| Parameter | Value | Mark | Source | Notes |
|---|---|---|---|---|
| `single_instance_flock` | on | fixed | inventory | One instance. |
| `preflight_recon_hard_gate` | on | fixed | inventory | Mismatch → halt+alert, refuse to trade. |
| `eligibility_checks` | scope/email/residence | fixed | API errors | RESIDENCE_UPDATE_REQUIRED etc. before trading. |
| `heartbeat_liveness` | three-layer + daily positive ping | fixed | inventory | Informational; bot doesn't depend on human. |
| `alert_channels` | ntfy/Telegram/email/… | DEFAULT | inventory | Money-redaction fence on public channels. |
| `llm_explanation_async` | on | fixed | risk §7 | LLM cause-attribution AFTER the fact, never in the decision path. |

---

## 9. The tune-live shortlist (what MUST be set from live data, nothing else blocks)

Only these genuinely require live data; everything else ships with a usable default:
- `M_0`, `sigma_opportunity` — the Browne calibration (needs the real field distribution / leaderboard).
- `divergence_skepticism_curve` breakpoints, `volatility_scaling_ref` — need observed divergence-vs-outcome
  and price volatility.
- `extremize_factor_a` — refine against observed SIG-price calibration (ships at 1.2).
- `source_endpoints` + per-race mapping — need the live market list + Polymarket/Kalshi API setup.
- `kelly_shrinkage_k` breakpoints, `drawdown_throttle` thresholds, `runaway_detect` thresholds — refine to
  observed behavior (ship conservative).

Everything else is DEFAULT and runs immediately (in SIM and at launch).

---

## 10. One-paragraph summary

**One config file, one source of truth for every knob across the five specs.** The consolidation resolved a
real contradiction (half-Kelly → **quarter-Kelly** base, audit #1), unified the edge formula to the SIG-native
price form (#2), pinned the σ_p units (log-odds) between signal and sizing (#3), unified the exposure cap to
one parameter (#4), and pinned the previously-unspecified loop cadences (#5), divergence-skepticism curve
(#6), and arb caps (#7). Every parameter is marked **DEFAULT** (usable now) or **TUNE-LIVE** (needs live
field/market data); the tune-live shortlist (§9) is small — M_0, σ, the skepticism/volatility breakpoints,
the extremize factor, and the source endpoints/mapping — and everything else ships ready, so the bot runs in
SIM and at launch on defaults, with the handful of live-calibrated knobs set from the first days of real
data. Governing posture throughout: **when uncertain, the smaller/more-conservative value — bounded by the
"fail small, not freeze" undertrading alarm so conservatism never reaches zero-trade.**

*Config schema + consistency audit. Consolidates: SIG_TOURNAMENT_SIZING_SPEC, SIG_RISK_GOVERNANCE_SPEC,
SIG_INTEGRATION_SPEC, SIG_RESEARCH_SIGNAL_SPEC, and the ported mbappe plumbing. Recorded 2026-10-01.*
