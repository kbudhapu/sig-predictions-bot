"""build_live_stack must flip the exit MASTER SWITCH on.

The arb-basket exit pass (_run_exits) returns early when the orchestrator has no ExitConfig, so for a
long time NOTHING recycled live: build_live_stack built the orchestrator WITHOUT exit_config. These
tests pin the wiring so it cannot silently regress again:

  • build_live_stack produces an orchestrator with a real ExitConfig  → _run_exits actually runs live;
  • the basket-exit depth provider is threaded through;
  • a malformed exit section degrades to "exits disabled" but STILL builds (fail-safe, never crash-start).

The exit DECISION mechanics are covered by test_exit_integration.py; this is purely the live-wiring guard.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
import yaml

from infra.state.store import StateStore
from sig.engine.composition import build_live_stack, SimulatedBroker

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "sig.yaml")


def _load_cfg() -> dict:
    with open(_CFG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def test_build_live_stack_enables_exit_pass(tmp_path):
    """The master switch: a live orchestrator carries an ExitConfig (so _run_exits does not early-return)
    AND the depth provider is wired."""
    async def _run():
        store = await StateStore(tmp_path / "state.db").open()
        try:
            sentinel = lambda ids: {}            # noqa: E731 — identity check only
            orch, *_ = build_live_stack(
                config=_load_cfg(), strategy_contracts={}, store=store,
                broker=SimulatedBroker(), exit_books_provider=sentinel,
            )
            assert orch._exit_cfg is not None, "exit pass still DISABLED live (master switch off)"
            # the orchestrator reads these on the exit path; prove they survived the wiring
            assert orch._exit_cfg.settlement_utc.startswith("2026-11-04")
            assert orch._exit_books_provider is sentinel
            assert orch._max_exits_per_cycle >= 1
        finally:
            await store.close()

    asyncio.run(_run())


def test_build_live_stack_survives_a_broken_exit_section(tmp_path):
    """A malformed `exit:` section disables basket recycling but must NOT stop the bot from starting —
    the directional/MM single-leg exits do not depend on it and keep running."""
    async def _run():
        cfg = copy.deepcopy(_load_cfg())
        cfg["exit"] = {"fees": 0.0}              # missing required keys → ExitConfig.from_cfg raises
        store = await StateStore(tmp_path / "state2.db").open()
        try:
            orch, *_ = build_live_stack(
                config=cfg, strategy_contracts={}, store=store, broker=SimulatedBroker(),
            )
            assert orch._exit_cfg is None        # basket recycling disabled (fail-safe)
            # but the stack still built — the bot starts and the single-leg exits still run
            assert orch is not None
        finally:
            await store.close()

    asyncio.run(_run())
