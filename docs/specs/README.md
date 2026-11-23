# sig-bot design specs

- **SIG_TOURNAMENT_SIZING_SPEC.md** — position sizing to maximize P(finish top-3): multivariate quarter-Kelly base × Browne tournament tilt (aggressive-behind / protect-ahead / escalate-late) × endgame concentration override.
- **SIG_RISK_GOVERNANCE_SPEC.md** — deterministic, no-human-in-the-loop risk layer: risk as automatic resizing (not halts), three separated failure-domain layers (pre-trade gate / resizing / independent supervisor), "fail small, not freeze".
- **SIG_INTEGRATION_SPEC.md** — how the components connect into one bot: two loops (fast arb-watch, slow strategy), the strict cycle order, handoff contracts, shared-SQLite coordination, sim-first.
- **SIG_RESEARCH_SIGNAL_SPEC.md** — the directional signal producing (p, σ_p, divergence) per market: log-odds aggregation + extremizing + dispersion-based σ_p; the LLM extracts numbers, deterministic math makes the probability judgment.
- **SIG_CONFIG_SCHEMA.md** — every tunable parameter in one place (marked DEFAULT vs TUNE-LIVE) plus the cross-spec consistency audit that resolved the contradictions.
- **SIG_EDGE_PORTFOLIO_SPEC.md** — all edges ranked by confidence/dependency/variance: the mechanical CORE (arb, market-making, resolution-mechanics), the medium-confidence SIGNAL tier, and the speculative behavioral TAIL.
- **SIG_DYNAMIC_ALLOCATION_SPEC.md** — the dynamic capital allocation spec: performance-weighted allocation across the edge portfolio (shrinkage-to-prior → measured-performance weighting, tier floors, step-capped rebalancing).
- **SIG_SPEC_AUDIT.md** — the adversarial self-audit of all the specs: where research was overclaimed (two fixes applied) and the honest headline.
- **SIG_SIM_TEST_PLAN.md** — the end-to-end integration test plan; the gate between "modules pass unit tests" and "push to droplet." Leads with verifying the blind-built modules (OMS, orchestrator) actually wire to the real modules.
- **GO_LIVE_CHECKLIST.md** — the ordered checklist for launch: pre-flight (relocate/commit/SIM), droplet, at-open live-contract verifications, launch, and post-launch directional. Work it top to bottom.

These are the design specs for sig-bot. SIG_SPEC_AUDIT.md is the honest audit — read it to understand what's proven vs hypothesized. The mechanical edges (arb, market-making) are the income floor; the directional edges are unproven upside to verify live.
