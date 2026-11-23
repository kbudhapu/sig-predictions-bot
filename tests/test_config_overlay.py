"""Config overlay layering — the hardening that keeps LIVE state OUT of the tracked, git-revertable
config/sig.yaml. The committed file is SAFE (mode: SIM, strategies off); a gitignored
config/sig.local.yaml (or SIG_CONFIG_OVERLAY path) is deep-merged on top and wins. A missing overlay
must leave the SIM defaults standing — so no `git checkout`/`reset`/sync can knock the droplet to SIM
or drop an override, and a dropped overlay fails SAFE rather than trading live by accident.
"""
from __future__ import annotations

import pathlib
import textwrap

import pytest

from sig.config import DEFAULT_CONFIG_PATH, _deep_merge, _overlay_path, load_config


class TestDeepMerge:
    def test_scalar_override(self):
        assert _deep_merge({"a": 1, "b": 2}, {"b": 3}) == {"a": 1, "b": 3}

    def test_nested_merge_preserves_siblings(self):
        base = {"of": {"enabled": False, "k": 0.25, "max": 20}}
        ov = {"of": {"enabled": True, "max": 8}}
        assert _deep_merge(base, ov) == {"of": {"enabled": True, "k": 0.25, "max": 8}}

    def test_list_replaced_wholesale(self):
        # order_flow.markets must REPLACE, not append/union
        base = {"of": {"markets": [1, 2, 3, 4]}}
        assert _deep_merge(base, {"of": {"markets": [9]}})["of"]["markets"] == [9]

    def test_overlay_none_or_empty_is_noop(self):
        assert _deep_merge({"a": 1}, None) == {"a": 1}
        assert _deep_merge({"a": 1}, {}) == {"a": 1}

    def test_does_not_mutate_base(self):
        base = {"of": {"x": 1}}
        _deep_merge(base, {"of": {"y": 2}})
        assert base == {"of": {"x": 1}}  # nested base dict untouched


class TestOverlayPath:
    def test_sibling_default(self):
        p = _overlay_path(pathlib.Path("/x/config/sig.yaml"))
        assert p.name == "sig.local.yaml" and p.parent.name == "config"

    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("SIG_CONFIG_OVERLAY", "/custom/over.yaml")
        assert _overlay_path(pathlib.Path("/x/config/sig.yaml")) == pathlib.Path("/custom/over.yaml")


class TestLoadConfigOverlay:
    def test_committed_default_is_SIM_when_no_overlay(self, tmp_path, monkeypatch):
        # point the overlay at a nonexistent path so a stray sibling sig.local.yaml can't interfere
        monkeypatch.setenv("SIG_CONFIG_OVERLAY", str(tmp_path / "nope.yaml"))
        app = load_config(check_secrets=False)
        assert app.mode == "SIM", "committed config/sig.yaml MUST default to SIM (safety property)"

    def test_overlay_flips_live_and_replaces_markets(self, tmp_path, monkeypatch):
        ov = tmp_path / "ov.yaml"
        ov.write_text(textwrap.dedent(
            """
            global:
              mode: LIVE
            order_flow:
              enabled: true
              markets: [111, 222]
            """
        ), encoding="utf-8")
        monkeypatch.setenv("SIG_CONFIG_OVERLAY", str(ov))
        app = load_config(check_secrets=False)  # check_secrets=False so LIVE doesn't require live keys here
        assert app.mode == "LIVE"
        assert app.raw["order_flow"]["enabled"] is True
        assert app.raw["order_flow"]["markets"] == [111, 222]
        # base siblings survive the nested merge (required key not clobbered)
        assert "k_reference_sigma" in app.raw["order_flow"]

    def test_missing_overlay_falls_back_to_committed_sim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SIG_CONFIG_OVERLAY", str(tmp_path / "does_not_exist.yaml"))
        app = load_config(check_secrets=False)
        assert app.mode == "SIM"
