"""PORTED from mbappe src/engine/entry_authority.py (verbatim — fully self-contained, no edits
needed). The single composed entry authority: one total-ordered, pure, decision-only function that
answers "may this leg enter?". Wired as the one submission gate in execution/idempotent_execution.py
(IdempotentSubmitter.submit's entry_guard), so every order passes exactly one composed check.

SIG note: the gate is domain-general (booleans only). The SIG orchestrator populates EntryContext
from SIG's own halt/degradation/recon stores; the composition logic is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

ENTRY_ACTIONS = frozenset({"LONG", "SHORT", "BUY", "SELL"})


@dataclass(frozen=True)
class EntryContext:
    """Raw gate state read from the live system (this authority reads, never acts)."""
    action: str | None
    preemption_flatten_portfolio: bool = False
    preemption_blocks_strategy: bool = False
    portfolio_halted: bool = False
    strategy_halted: bool = False
    capital_gate_block_new_entries: bool = False
    degradation_block_new_entries: bool = False
    safe_mode: bool = False
    commissioning_gate_blocked: bool = False
    force_liquidation: bool = False
    pre_flight_recon_locked: bool = False
    risk_halted: bool = False


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    block_reason: str | None = None


def entry_allowed(ctx: EntryContext) -> EntryDecision:
    """Compose every entry gate in the registered precedence (first BLOCK wins)."""
    is_entry = ctx.action in ENTRY_ACTIONS

    if ctx.preemption_flatten_portfolio or ctx.preemption_blocks_strategy:
        return EntryDecision(False, "preemption_escalation")
    if ctx.pre_flight_recon_locked:
        return EntryDecision(False, "pre_flight_recon_lock")
    if ctx.risk_halted:
        return EntryDecision(False, "risk_halt")
    if ctx.portfolio_halted:
        return EntryDecision(False, "portfolio_halt")
    if ctx.strategy_halted and is_entry:
        return EntryDecision(False, "strategy_halt")
    if ctx.capital_gate_block_new_entries and is_entry:
        return EntryDecision(False, "capital_gate_block_new_entries")
    if ctx.degradation_block_new_entries and is_entry and not ctx.force_liquidation:
        return EntryDecision(False, "degradation_block_entries")
    if ctx.safe_mode:
        return EntryDecision(False, "safe_mode")
    if ctx.commissioning_gate_blocked:
        return EntryDecision(False, "commissioning_fail_closed")

    return EntryDecision(True, None)
