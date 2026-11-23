"""Central-config-loader tests. Validation is WIRED INTO the load path (mbappe weakness #1 fixed) and
secret access is central + fail-loud (weakness #2 fixed). Also pins the CRITICAL cross-module invariant:
the risk gate and the sizing module read the SAME Browne constants.
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sig.config import (
    load_config,
    load_yaml,
    validate_config,
    require_secret,
    ConfigError,
    BROWNE_KEYS,
)
from strategies.risk_gate.gate import RiskGate
from strategies.sizing.config import SizingConfig


def base_cfg():
    """The real, committed config/sig.yaml — must be valid as shipped."""
    return load_yaml()


# ── the config loads clean as shipped ──
def test_real_config_loads_and_resolves():
    app = load_config()  # SIM mode → no live secrets required
    assert app.mode == "SIM"
    assert isinstance(app.sizing, SizingConfig)
    assert app.feed.token_ttl_s == 10800
    assert 0.0 < app.capital["max_deploy_frac"] <= 1.0
    # every schema section resolved
    for sec in ("global_", "capital", "risk", "allocator", "loops", "arb", "signal", "safety", "ops"):
        assert getattr(app, sec)


# ── weakness #1: a MISSING required key fails loud ──
def test_missing_required_key_fails_loud():
    cfg = base_cfg()
    del cfg["risk"]["M_0"]
    with pytest.raises(ConfigError) as ei:
        validate_config(cfg)
    assert any("risk.M_0 is REQUIRED" in e for e in ei.value.errors)


def test_missing_whole_section_fails_loud():
    cfg = base_cfg()
    del cfg["sizing"]
    with pytest.raises(ConfigError) as ei:
        validate_config(cfg)
    assert any("missing required section 'sizing'" in e for e in ei.value.errors)


# ── weakness #1: a TYPO'd key fails loud (does NOT silently default) ──
def test_typod_key_fails_loud():
    cfg = base_cfg()
    cfg["risk"]["tau_floorr"] = 0.01  # typo of tau_floor
    with pytest.raises(ConfigError) as ei:
        validate_config(cfg)
    assert any("unknown key 'risk.tau_floorr'" in e for e in ei.value.errors)


def test_typod_section_fails_loud():
    cfg = base_cfg()
    cfg["sizingg"] = {}  # typo'd section name
    with pytest.raises(ConfigError) as ei:
        validate_config(cfg)
    assert any("unknown config section 'sizingg'" in e for e in ei.value.errors)


def test_out_of_range_value_fails_loud():
    cfg = base_cfg()
    cfg["capital"]["max_deploy_frac"] = 1.5  # > 1
    with pytest.raises(ConfigError):
        validate_config(cfg)


# ── weakness #2: a MISSING required secret fails loud (never returns "") ──
def test_require_secret_fails_loud_on_missing(monkeypatch):
    monkeypatch.delenv("SIG_API_KEY_ABSENT_FOR_TEST", raising=False)
    with pytest.raises(RuntimeError) as ei:
        require_secret("SIG_API_KEY_ABSENT_FOR_TEST")
    # loud + redacted (never leaks a value, never returns "")
    assert "SIG_API_KEY_ABSENT_FOR_TEST" in str(ei.value)


def test_require_secret_returns_value_when_present(monkeypatch):
    monkeypatch.setenv("SIG_SOME_TOKEN", "abc123")
    assert require_secret("SIG_SOME_TOKEN") == "abc123"


def test_live_mode_requires_live_secrets(monkeypatch, tmp_path):
    # LIVE mode with the required SIG keys ABSENT → load fails loud (not a silent "" key)
    cfg = base_cfg()
    cfg["global"]["mode"] = "LIVE"
    p = tmp_path / "sig.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr("sig.env._loaded", True, raising=False)  # skip reading a real .env
    monkeypatch.delenv("SIG_API_KEY_READ", raising=False)
    monkeypatch.delenv("SIG_API_KEY_WRITE", raising=False)
    with pytest.raises(RuntimeError):
        load_config(p)  # check_secrets defaults to True in LIVE


# ── CRITICAL: risk-gate and sizing share ONE Browne config ──
def test_risk_gate_and_sizing_browne_match():
    cfg = base_cfg()
    gate = RiskGate(config=cfg)
    sizing = SizingConfig.from_cfg(cfg)
    for k in sorted(BROWNE_KEYS):
        gv, sv = getattr(gate, k), getattr(sizing, k)
        assert gv == sv, f"Browne constant {k} diverges: gate={gv} sizing={sv}"


def test_browne_duplication_under_sizing_is_rejected():
    # someone copies M_0 into sizing: → the drift risk the one-source-of-truth rule exists to prevent
    cfg = base_cfg()
    cfg["sizing"]["M_0"] = 1.0
    with pytest.raises(ConfigError) as ei:
        validate_config(cfg)
    assert any("must NOT be duplicated under 'sizing:'" in e for e in ei.value.errors)


def test_browne_values_are_exactly_the_shipped_risk_block():
    app = load_config()
    assert set(app.browne) == set(BROWNE_KEYS)
    assert app.browne["M_0"] == app.risk["M_0"]  # the AppConfig view is the risk block itself


def test_every_cross_market_key_main_reads_is_in_the_schema():
    """2026-10-09: rotation_v2_* retune keys were read via xm.get(...) in main_sig.py but missing from the schema;
    setting them in the overlay made the trader refuse to start (~1 min down). Every key the code reads must be
    accepted by the schema."""
    import re
    from sig.config import _SCHEMA
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "main_sig.py"), encoding="utf-8").read()
    allowed = _SCHEMA["cross_market"]["required"] | _SCHEMA["cross_market"]["optional"]
    read = set(re.findall(r'\bxm\.get\("([a-z0-9_]+)"', src))
    assert read, "pattern found nothing"
    assert sorted(read - allowed) == []
