"""Load allocator parameters from config/sig.yaml (never hardcode — SIG_CONFIG_SCHEMA.md is the source
of truth). Returns the parsed dict; the allocator/priors/rebalancer read `capital` and `allocator`."""
from __future__ import annotations

import pathlib

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO / "config" / "sig.yaml"


def load_config(path: str | pathlib.Path | None = None) -> dict:
    p = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "allocator" not in cfg or "capital" not in cfg:
        raise ValueError(f"{p} is missing required 'allocator'/'capital' sections")
    return cfg
