"""Central config loader for sig-bot — the single validated entry every entrypoint builds once.

Copies the proven mbappe patterns (a scan established them) AND fixes the two weaknesses mbappe's own code
reveals:

  COPIED:
    • YAML values (config/sig.yaml) / typed dataclasses (here) / .env secrets split.
    • One central `load_config()` → a resolved `AppConfig` passed down (built once at the entrypoint).
    • `load_env()`-first discipline (mbappe R3: a process that never loaded .env had every secret silently
      empty on the droplet) — called first here, explicit path, override=False, idempotent (sig/env.py).
    • Single data-dir seam `SIG_DATA_DIR` (infra/config.py) so tests never touch production state.

  FIXED (mbappe weaknesses — do NOT inherit):
    1. SCHEMA VALIDATION IS WIRED INTO THE LOAD PATH. mbappe HAS json-schema validation but load_config
       never calls it, so a typo'd key silently uses a default. Here `validate_config()` runs on EVERY
       load and FAILS LOUD on a missing required key, an unknown/typo'd key, or a cross-field violation.
    2. CENTRAL FAIL-LOUD SECRET ACCESS. mbappe reads secrets via os.getenv(NAME, "") in ~100 files, so a
       missing secret silently becomes "". Here `require_secret(NAME)` raises loudly (reusing sig/env.py's
       `require`) — no empty-string-default secrets anywhere.

CRITICAL cross-module invariant enforced here: the risk gate and the sizing module MUST share ONE copy of
the Browne constants (ρ clamp, τ_floor, M_0, M_full, all_in_cap_frac). They live ONLY in the `risk:` block;
`validate_config` REFUSES a duplicate copy under `sizing:` (a second copy could drift, and the gate would
then re-derive a ceiling the sizer never used — breaking the authorized-endgame-vs-runaway distinction).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

from sig.env import load_env, require as require_secret  # fail-loud secret access (weakness #2 fix)
from strategies.sizing.config import SizingConfig
from sig.feed.feed import FeedConfig

_REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _REPO / "config" / "sig.yaml"

# Secrets that MUST be present to trade for real. In SIM mode none are required (the SimulatedBroker needs
# no key); in LIVE mode a missing one fails the load LOUD rather than trading with an empty "" key.
REQUIRED_LIVE_SECRETS = ("SIG_API_KEY_READ", "SIG_API_KEY_WRITE")

# The Browne constants — ONE source of truth, in `risk:`. Never duplicated under `sizing:`.
BROWNE_KEYS = frozenset({"rho_min", "rho_max", "tau_floor", "M_0", "M_full", "all_in_cap_frac"})

# The schema the loader validates against (mirrors SIG_CONFIG_SCHEMA.md + the as-built module reads).
# required = must be present; optional = allowed; anything else in the section = a typo → FAIL LOUD.
_SCHEMA: dict[str, dict[str, set]] = {
    "global": {"required": {"mode", "tournament_slug", "competition_start_utc", "cutoff_utc"}, "optional": set()},
    "capital": {
        "required": {"max_deploy_frac", "max_per_market_frac", "max_cluster_frac"},
        # pool_edge_reserve>0 => POOLED capital: every strategy shares available cash, but only the
        # pool_eligible edge strategies may dip into the last `pool_edge_reserve` dollars.
        "optional": {"max_edge_frac", "min_order_frac", "erroneous_max_order_notional",
                     "pool_edge_reserve", "pool_eligible", "min_order_qty",
                     # NAV-fraction of free cash only market_making may spend (pooled mode)
                     "mm_cash_reserve_frac"},
    },
    "allocator": {
        "required": {"shrinkage_k", "decay", "rolling_window", "gamma", "mult_min", "mult_max",
                     "loss_attribution_cap", "rebalance_interval_s", "max_step", "core_floor_release_eps",
                     "core_floor_release_min_n", "edges"},
        "optional": {"static"},   # static=true → bypass dynamic perf-weighting, allocate by priors only
    },
    "risk": {
        # NB: no hard_max_quantity (share-count) and no hard_notional_abs (absolute-$) — the gate sizes
        # off CAPITAL only; the sole erroneous-order ceiling is hard_notional_frac (a fraction of balance).
        "required": {"tick", "price_min", "price_max", "tick_tol",
                     "hard_notional_frac", "all_in_cap_frac", "rho_min", "rho_max", "tau_floor", "M_0", "M_full"},
        "optional": set(),
    },
    "sizing": {
        "required": {"kelly_base_fraction", "M_cap", "sigma_grid", "k_grid", "k_reference_sigma",
                     "k_missing_sigma", "min_edge_f", "max_sigma_to_act", "min_impact_adjusted_f",
                     "min_quantity", "default_factor_loading", "endgame_tau", "endgame_min_confidence_sigma"},
        # per-tier Kelly aggressiveness (catch-up posture): a multiplier on k(σ_p) per edge provenness,
        # and a ceiling on the targeted fraction-of-edge-budget. Optional ⇒ absent means the prior
        # behaviour (×1, ceiling 1.0). Applies to the directional/Browne path only.
        "optional": {"kelly_multipliers", "kelly_frac_ceiling"},
    },
    # Market-making sizing (strategies/market_making). Every size knob is a FRACTION — of equity or
    # of the market's depth — so each scales. There is deliberately NO fixed maximum size and no
    # fallback share count: a share ceiling is $2 of risk at 0.02 and $95 at 0.95, which is the
    # inconsistency the capital-% sizing exists to remove. Size must also never be edge-scaled (see
    # the section comment in config/sig.yaml). A typo here fails loud rather than silently reverting
    # to a hardcoded size — which is exactly how the quote stayed stuck at 100.
    "market_making": {"required": {"mm_capital_frac", "mm_max_quote_frac",
                                   "mm_max_inventory_frac", "max_frac_of_depth",
                                   "min_quote_size", "size_skew_strength",
                                   "skew_ticks_at_cap",
                                   # order-volume controls: the only lever that shortens the cycle
                                   "max_quote_age_s", "cadence_s", "requote_threshold"},
                      # mean-reversion alpha skew (maker-only, bounded; disabled unless set) + an
                      # optional curated market subset (quote fewer markets when MM is a small sleeve)
                      "optional": {"enabled", "mr_skew_enabled", "mr_window_seconds", "mr_min_samples",
                                   "mr_k_enter", "mr_max_ticks", "markets",
                                   "quote_k", "join_when_tight",
                                   # MM ↔ cross-market overlap mode (dynamic|exclude|allow)
                                   "cross_overlap",
                                   # sharp-anchored quoting (off|shadow|live) + its tunables
                                   "sharp_anchor", "sharp_anchor_margin", "sharp_lean_strength",
                                   "sharp_lean_gap", "sharp_anchor_record_path"}},
    "feed": {
        "required": {"token_ttl_s", "token_refresh_lead_s", "reconnect_backoff_base_s",
                     "reconnect_backoff_factor", "reconnect_backoff_cap_s", "reconnect_reset_after_s",
                     "price_stale_after_s", "snapshot_max_ids_per_call"},
        # ws_max_markets: escape hatch capping how many market channels are subscribed (one socket).
        # Unset = subscribe the whole board, sharded. See cmd_feed.
        "optional": {"ws_max_markets", "rest_safety_net_interval_s"},
    },
    "loops": {
        "required": {"fast_loop_interval_s", "slow_loop_interval_s", "writes_per_min_per_key",
                     "reads_per_min_per_key", "arb_write_reservation_frac", "n_keys", "verify_by_reread"},
        # dead_man_threshold_s: the heartbeat beats once per CYCLE, so this must cover the worst
        # legitimate cycle (venue-paced order writes), not the tick interval.
        "optional": {"dead_man_threshold_s", "placement_worker", "placement_intent_ttl_s",
                     "placement_batch_max_legs", "placement_max_per_drain", "position_resync_interval_s",
                     "config_reload_interval_s", "state_snapshot_interval_s", "fill_ingest_interval_s",
                     "mid_history_interval_s", "mid_history_retention_days", "attribution_flush_interval_s",
                     "trade_tape_mode", "trade_tape_reads_per_min", "trade_tape_retention_days"},
    },
    "chamber_model": {
        "required": {"national_swing_rho", "min_divergence_to_flag",
                     "min_observations_before_sizing", "max_size_multiplier", "tier",
                     "enabled_for_sizing", "assume_unpriced_held_by_incumbent_party",
                     "quadrature_nodes"},
        "optional": set(),
    },
    "arb_sizing": {
        "required": {"arb_max_per_arb_frac", "arb_total_max_frac", "arb_min_notional",
                     "arb_min_edge_to_cost_ratio", "arb_fees", "arb_max_frac_of_exit_depth"},
        "optional": set(),
    },
    "arb": {"required": {"arb_min_violation", "arb_max_capital_frac", "arb_atomic_only", "arb_fillability_check"},
            "optional": {"enabled", "cadence_s"}},   # enabled=false ⇒ not registered (default true)
    # §5b arb EXIT path (strategies/exit). Thresholds only — the module hardcodes nothing, and a
    # typo'd key here must fail loud rather than silently default `min_recycle_advantage` to 0 and
    # turn the exit rule into "always sell when green" (which loses money: a hard arb's unrealised
    # profit is contracted, so exiting early forfeits a guaranteed remainder).
    "exit": {
        "required": {"fees", "min_recycle_advantage", "liquidity_safety_margin", "min_realizable_gain",
                     "days_to_settlement_floor", "alternative_horizon_days", "settlement_utc"},
        "optional": set(),
    },
    # §5c DIRECTIONAL exit (sig/engine/orchestrator._run_takeprofit_exits) — the trailing stop +
    # stop-loss for HELD single-leg directional positions (incl. MM drift). Thresholds only; the
    # orchestrator falls back to safe built-in defaults if the whole section is absent, but a typo'd
    # KEY here must fail loud rather than silently disable the trailing stop and let a winner bleed
    # back to zero (the exact class of silent-default bug this schema exists to catch).
    "directional_exit": {
        "required": {"trail_arm_pct", "trail_pct", "stop_loss_pct",
                     "exit_check_interval_s"},
        "optional": set(),
    },
    # §5d ORDER-FLOW (sig.engine.live_strategies.CoreOrderFlowStrategy). Directional fade edge sized via
    # the Browne sizer (k_reference_sigma → sigma_p). A typo'd key must fail loud rather than silently
    # revert to a built-in default (e.g. a dropped `enabled` that silently leaves the edge off, or a
    # dropped `extreme_threshold` that fades on noise).
    "order_flow": {
        "required": {"enabled", "k_reference_sigma", "extreme_threshold", "window_seconds",
                     "min_volume", "min_trades", "momentum_window_s", "momentum_min_move",
                     "min_confidence", "max_markets"},
        "optional": {"cadence_s", "pull_on_unconfirmed", "emit_follow", "book_poll_interval_s",
                     "stream_sizes", "markets"},
    },
    # Cross-market divergence (Polymarket-vs-SIG lag). enabled is the master switch; the rest tune the
    # provider cadence + the strategy's DivergenceConfig (all optional → strategy defaults apply).
    "cross_market": {
        "required": {"enabled"},
        "optional": {"poll_interval_s", "poll_min_interval_s", "poll_concurrency", "cadence_s",
                     "min_divergence", "target_gap", "external_stale_s",
                     "conviction_weighting", "conviction_cost_floor", "sig_stale_s", "sig_fetch_interval_s",
                     "exit_convergence_tol", "exit_hard_stop_pct", "exit_support_conviction",
                     "exit_thesis_reversal_tol",
                     "wiki_enabled",    # Wikipedia SECONDARY coarse benchmark (~71 Senate/Gov seats)
                     # capital rotation (sig/engine/rotation.py): off|shadow|live + tunables
                     "rotation_mode", "rotation_margin", "rotation_max_per_hour", "rotation_interval_s",
                     "rotation_min_position_usd", "rotation_record_path",
                     # leg-type shadow (NO-only filter research): off|shadow + jsonl path
                     "leg_shadow_mode", "leg_shadow_path",
                     # D+R parity reversion (sig/engine/parity.py): off|shadow + tunables
                     "parity_mode", "parity_entry_edge", "parity_exit_pnl", "parity_interval_s",
                     "parity_max_quote_age_s", "parity_record_path",
                     # rotation v2 shadow (sig/engine/rotation_v2.py)
                     "rotation_v2_mode", "rotation_v2_persist_s", "rotation_v2_record_path",
                     "rotation_v2_topup", "rotation_v2_partial", "rotation_v2_margin", "rotation_v2_freeze_s",
                     # fresh-dislocation reversion shadow (sig/engine/fresh_gap.py)
                     "fresh_gap_mode", "fresh_gap_path", "fresh_gap_th_move", "fresh_gap_th_gap",
                     "fresh_gap_t2_mode", "fresh_gap_t2_path", "fresh_gap_t2_th_move", "fresh_gap_t2_th_gap",
                     # capital rotation LIVE trader (sig/engine/rotation_live.py)
                     "rotation_live_mode", "rotation_live_max_swap_usd", "rotation_live_max_swaps_per_day",
                     "rotation_live_big_cap_frac", "rotation_live_big_cap_gap", "rotation_live_freeze_s",
                     "rotation_live_persist_s", "rotation_live_min_sell_depth_usd", "rotation_live_kalshi_fallback",
                     # D+R parity LIVE pilot (sig/engine/parity_live.py)
                     "parity_live_mode", "parity_live_pairs", "parity_live_budget_usd", "parity_live_max_open",
                     "parity_live_entry_edge", "parity_live_exit_pnl", "parity_live_max_loss_usd",
                     "parity_live_interval_s",
                     "fresh_gap_live_mode", "fresh_gap_live_budget_usd", "fresh_gap_live_trade_usd",
                     "fresh_gap_live_max_open", "fresh_gap_live_max_loss_usd", "fresh_gap_live_entry_mode",
                     "fresh_gap_live_exit_mode", "fresh_gap_live_lookback_h", "fresh_gap_live_th_move",
                     "fresh_gap_live_th_gap", "fresh_gap_lookback_h", "fresh_gap_t2_lookback_h",
                     # poly-led lag shadow (sig/engine/fresh_gap.poly_lead_signal)
                     "poly_lead_mode", "poly_lead_th_move", "poly_lead_lookbacks_h", "poly_lead_max_hold_h",
                     "poly_lead_path",
                     # election-night LIVE trader (sig/engine/election_night_live.py)
                     "election_night_live_mode", "election_night_live_budget_usd", "election_night_live_usd_per_race",
                     "election_night_live_window_start", "election_night_live_window_end",
                     "election_night_live_reserve_from", "election_night_live_theta",
                     "election_night_live_max_writes_per_min", "election_night_live_tick_s",
                     # maker-entry shadow (sig/engine/maker_shadow.py)
                     "maker_shadow_mode", "maker_shadow_path",
                     # Kalshi second-benchmark shadow (signals/kalshi_map.yaml)
                     "kalshi_shadow_mode", "kalshi_map_path", "kalshi_shadow_interval_s",
                     # fair-band maker shadow (sig/engine/fair_band.py)
                     "fair_band_mode", "fair_band_w", "fair_band_path",
                     # election-night race-call dry run (sig/engine/election_night.py)
                     "election_night_mode", "election_night_theta", "election_night_usd", "election_night_path",
                     # fair-band maker LIVE pilot (sig/engine/fair_band_live.py)
                     "fair_band_live_mode", "fair_band_live_w", "fair_band_live_quote_usd",
                     "fair_band_live_market_cap_usd", "fair_band_live_budget_usd", "fair_band_live_markets",
                     "fair_band_live_writes_per_min", "fair_band_live_max_loss_usd",
                     # maker entries for cross_market_divergence (strategies/cross_market)
                     "entry_mode", "maker_entry_timeout_s",
                     # order_flow / dutch_book never trade cross races (churn guard, default true)
                     "exclusive_races"},
    },
    # Dutch-book complement arb (near-riskless D+R>1 short-both). enabled is the master switch.
    # Stale-quote sniper — take other bots' mispriced-vs-sharp quotes marketable before they re-quote.
    "sniper": {
        "required": {"enabled"},
        "optional": {"min_edge", "max_sharp_age_s", "record", "record_path", "place", "k_reference_sigma",
                     "cadence_s", "sweep_to_edge", "max_cross"},
    },
    # Order-book pressure (queue imbalance) — SHADOW by default (place:false). enabled is the master switch.
    "book_pressure": {
        "required": {"enabled"},
        "optional": {"levels", "min_imbalance", "min_depth", "tape_confirm", "tape_min",
                     "record", "record_path", "place", "cadence_s"},
    },
    # MM SHADOW research (vol-aware spread + tape toxicity). RECORD-ONLY — never changes a live quote.
    # enabled gates the whole recorder; toxicity_enabled gates the per-market tape reads (429 budget).
    "mm_shadow": {
        "required": {"enabled"},
        "optional": {"interval_s", "window", "base_k", "vol_ref", "k_min", "k_max",
                     "toxicity_enabled", "tox_min_volume", "tox_threshold", "tape_limit", "record_path"},
    },
    "dutch_book": {
        "required": {"enabled"},
        "optional": {"min_edge", "max_pairs", "per_pair_frac", "max_qty_per_leg", "cadence_s",
                     "record", "record_path", "place"},   # record=shadow log; place=false => record-only
    },
    # Mean-reversion TAKER (operator bet). enabled is the master switch; rest tune the MRTakerConfig.
    # Safe-by-construction: enter-when-flat, reduce-only sell exits (gate-clamped).
    "mr_taker": {
        "required": {"enabled"},
        "optional": {"markets", "window_seconds", "min_samples", "k_enter", "k_exit", "hold_max_s",
                     "max_spread", "k_reference_sigma", "cadence_s"},
    },
    # Event-trigger (all-237 SIG abnormal-move × GDELT news). enabled is the master switch; the rest tune the
    # store-window move detector, the hybrid trade-count enrichment, the paced GDELT sweep, and the sizing band.
    "event_trigger": {
        "required": {"enabled"},
        "optional": {"tier", "cadence_s", "min_confidence", "min_sigma", "max_sigma", "price_interval_s",
                     "window", "min_candles", "move_mult", "min_move", "enrich_tradecount",
                     "enrich_resolution", "news_rate_limit_s", "news_mult"},
    },
    "signal": {
        "required": {"signal_refresh_interval_min", "aggregation_space", "source_weighting",
                     "extremize_factor_a", "sigma_p_shared_info_inflation", "sigma_p_staleness_inflation",
                     "cross_market_divergence_weight", "favorite_longshot_flag", "endgame_source_priority"},
        "optional": set(),
    },
    "safety": {
        "required": {"drawdown_throttle_grind", "drawdown_throttle_endgame", "stale_data_halt_s",
                     "repeated_error_halt_count", "recon_mismatch_halt", "runaway_detect",
                     "killswitch_default_mode", "undertrading_alarm", "supervisor_independent"},
        "optional": set(),
    },
    "ops": {
        "required": {"single_instance_flock", "preflight_recon_hard_gate", "eligibility_checks",
                     "heartbeat_liveness", "alert_channels", "llm_explanation_async"},
        "optional": {"alert_cooldown_seconds"},
    },
}


class ConfigError(ValueError):
    """Raised when config is missing a required key, has an unknown/typo'd key, or violates a cross-check.
    Carries ALL errors found (not just the first) so one load surfaces every problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("invalid config/sig.yaml:\n  - " + "\n  - ".join(self.errors))


def validate_config(cfg: dict) -> None:
    """Validate the WHOLE config at load. FAIL LOUD on: unknown top-level section, missing required key,
    unknown/typo'd key within a section, Browne-constant duplication, and a few range sanity checks.
    This is the weakness-#1 fix: nothing silently defaults a typo."""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        raise ConfigError(["top-level config is not a mapping"])

    # unknown top-level sections (a typo'd section name is a bug, not a silent no-op)
    for section in cfg:
        if section not in _SCHEMA:
            errors.append(f"unknown config section '{section}' (typo? not in the SIG schema)")

    for section, spec in _SCHEMA.items():
        if section not in cfg:
            errors.append(f"missing required section '{section}'")
            continue
        block = cfg[section]
        if not isinstance(block, dict):
            errors.append(f"section '{section}' must be a mapping")
            continue
        allowed = spec["required"] | spec["optional"]
        for key in spec["required"]:
            if key not in block:
                errors.append(f"{section}.{key} is REQUIRED but missing")
        for key in block:
            if key not in allowed:
                errors.append(f"unknown key '{section}.{key}' (typo? not in the SIG schema)")

    # cross-field: Browne constants must live ONLY in risk: — never duplicated under sizing: (drift guard)
    sizing_block = cfg.get("sizing", {})
    if isinstance(sizing_block, dict):
        dup = BROWNE_KEYS & set(sizing_block)
        if dup:
            errors.append(
                f"Browne constants {sorted(dup)} must NOT be duplicated under 'sizing:' — they live ONLY in "
                "'risk:' (one source of truth so the gate re-derives exactly the sizer's authorization)")

    # light range sanity (fractions in [0,1]; ordered bands) — enough to catch a fat-fingered value
    risk = cfg.get("risk", {}) if isinstance(cfg.get("risk"), dict) else {}
    cap = cfg.get("capital", {}) if isinstance(cfg.get("capital"), dict) else {}
    for sec, key in [("capital", "max_deploy_frac"), ("capital", "max_per_market_frac"),
                     ("capital", "max_cluster_frac"), ("risk", "all_in_cap_frac"),
                     ("risk", "rho_min"), ("risk", "rho_max")]:
        v = cfg.get(sec, {}).get(key) if isinstance(cfg.get(sec), dict) else None
        if v is not None and not (0.0 <= float(v) <= 1.0):
            errors.append(f"{sec}.{key}={v} out of range [0,1]")
    if {"rho_min", "rho_max"} <= set(risk) and float(risk["rho_min"]) >= float(risk["rho_max"]):
        errors.append(f"risk.rho_min ({risk['rho_min']}) must be < risk.rho_max ({risk['rho_max']})")
    if {"price_min", "price_max"} <= set(risk) and float(risk["price_min"]) >= float(risk["price_max"]):
        errors.append(f"risk.price_min must be < risk.price_max")

    if errors:
        raise ConfigError(errors)


@dataclass(frozen=True)
class AppConfig:
    """Fully-resolved, validated runtime config. Built once at the entrypoint and passed down."""

    mode: str
    global_: dict
    capital: dict
    risk: dict
    allocator: dict
    sizing: SizingConfig
    feed: FeedConfig
    loops: dict
    arb: dict
    signal: dict
    safety: dict
    ops: dict
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def browne(self) -> dict:
        """The ONE Browne parameter set both the risk gate and the sizer read (from risk:)."""
        return {k: self.risk[k] for k in sorted(BROWNE_KEYS)}


def load_yaml(path: str | pathlib.Path | None = None) -> dict:
    p = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _overlay_path(base: pathlib.Path) -> pathlib.Path:
    """Resolve the GITIGNORED local override file. `SIG_CONFIG_OVERLAY` (a path) wins; otherwise the
    sibling `<stem>.local<suffix>` — config/sig.yaml → config/sig.local.yaml. The overlay carries the
    operator's LIVE/deploy state (mode, tournament_slug, per-strategy enables, markets, …) so it lives
    OUTSIDE git: no `git checkout`/`reset --hard`/sync can revert the droplet to the committed SIM
    defaults or drop an override. A MISSING overlay is a safe no-op (the committed SIM defaults stand)."""
    import os
    env = os.environ.get("SIG_CONFIG_OVERLAY", "").strip()
    if env:
        return pathlib.Path(env)
    return base.with_name(base.stem + ".local" + base.suffix)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` INTO a copy of `base`. Nested mappings merge key-by-key; every other
    value — scalars AND lists (e.g. order_flow.markets) — is REPLACED wholesale. The overlay always wins."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | pathlib.Path | None = None, *, check_secrets: bool | None = None) -> AppConfig:
    """Read + VALIDATE config/sig.yaml and return a resolved AppConfig. load_env() runs first so any secret
    read later is populated. Raises ConfigError on any schema violation (fail loud). If the resolved mode is
    LIVE (or check_secrets=True), the required live secrets must be present or the load fails loud too.

    Layering: the committed `config/sig.yaml` holds SAFE defaults (mode: SIM, strategies off). A gitignored
    `config/sig.local.yaml` overlay (see `_overlay_path`) is deep-merged ON TOP before validation, so the
    LIVE state lives outside git and survives any `git` operation. Overlay absent ⇒ the SIM defaults run."""
    load_env()  # weakness-#2 discipline: populate .env BEFORE anything reads a secret
    base_path = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = load_yaml(base_path)
    overlay_path = _overlay_path(base_path)
    overlay_applied = False
    if overlay_path.exists():
        overlay = load_yaml(overlay_path)  # a PRESENT-but-broken overlay raises → fail loud (never silently SIM)
        if overlay:
            cfg = _deep_merge(cfg, overlay)
            overlay_applied = True
    validate_config(cfg)  # weakness-#1 fix: validation runs on the MERGED config

    mode = str(cfg["global"]["mode"]).strip().upper()
    try:
        import structlog
        structlog.get_logger().info("config_loaded", mode=mode, overlay_applied=overlay_applied,
                                    overlay_path=(str(overlay_path) if overlay_applied else None))
    except Exception:  # noqa: BLE001 — logging must never break the load
        pass
    app = AppConfig(
        mode=mode,
        global_=cfg["global"],
        capital=cfg["capital"],
        risk=cfg["risk"],
        allocator=cfg["allocator"],
        sizing=SizingConfig.from_cfg(cfg),   # reuse the module's own typed loader (one source of truth)
        feed=FeedConfig.from_config(cfg),     # ditto
        loops=cfg["loops"],
        arb=cfg["arb"],
        signal=cfg["signal"],
        safety=cfg["safety"],
        ops=cfg["ops"],
        raw=cfg,
    )

    do_check = (mode == "LIVE") if check_secrets is None else bool(check_secrets)
    if do_check:
        for name in REQUIRED_LIVE_SECRETS:
            require_secret(name)  # raises loudly (never returns "") if a live key is absent
    return app


__all__ = ["AppConfig", "ConfigError", "load_config", "load_yaml", "validate_config",
           "require_secret", "load_env", "BROWNE_KEYS", "REQUIRED_LIVE_SECRETS"]
