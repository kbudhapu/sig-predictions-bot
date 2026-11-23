"""PORTED from mbappe src/engine/degradation_manager.py — the 4-state operational degradation ladder.

NORMAL -> SOFT_DEGRADE -> RECON_DEGRADED_MANAGE -> HARD_CRITICAL_DEGRADE, with ASYMMETRIC proven-recovery
(SOFT needs 3 consecutive OK verdicts, HARD needs 6 and only steps DOWN to SOFT) so a safety state must
re-earn health; the mode survives a restart (hydrate_from_vault) but the recovery streak resets to 0.

SIG adaptations: imports point at infra.*; the SLO verdict / SignalAction shapes come from infra.slo_types;
the critical-page dispatch imports infra.control.alerts best-effort (logs if alerts not yet wired), so the
ladder is functional before and after the alerts module is present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from infra.config import DB_PATH
from infra.slo_types import DataIntegrityVerdict, IntegritySeverity, SignalAction
from infra.persistence.governance_state_store import load_governance_state, persist_governance_state

SOFT_RECOVERY_STREAK = 3
HARD_RECOVERY_STREAK = 6


class OperationalMode(str, Enum):
    NORMAL = "NORMAL"
    SOFT_DEGRADE = "SOFT_DEGRADE"
    RECON_DEGRADED_MANAGE = "RECON_DEGRADED_MANAGE"
    HARD_CRITICAL_DEGRADE = "HARD_CRITICAL_DEGRADE"


@dataclass(frozen=True)
class DegradationState:
    mode: OperationalMode
    block_new_entries: bool
    ignore_model_signals: bool
    force_emergency_flatten: bool
    bypass_standard_timers: bool
    allow_exit_tracking: bool
    reason: str
    activated_at: str


@dataclass
class DegradationManager:
    """Central operational state transitions driven by SLO integrity breaches."""

    db_path: Path = DB_PATH
    notifier: Any = field(default=None, repr=False)
    _mode: OperationalMode = OperationalMode.NORMAL
    _reason: str = ""
    _activated_at: str | None = None
    _recovery_streak: int = 0
    _flatten_latched: bool = False
    _hydrated: bool = field(default=False, repr=False)

    _DEGRADE_COMPONENT = "degradation_manager"
    _DEGRADE_KIND = "hard_critical_degrade"

    def hydrate_from_vault(self) -> DegradationState:
        if self._hydrated:
            return self.current_state()
        persisted = load_governance_state(self.db_path)
        if persisted is not None:
            try:
                self._mode = OperationalMode(persisted.degradation_mode)
            except ValueError:
                self._mode = OperationalMode.NORMAL
            self._reason = persisted.trigger_reason
            self._activated_at = persisted.entered_at
            self._flatten_latched = persisted.flatten_latched
            self._recovery_streak = 0   # health is re-earned after a restart; no stale credit
        self._hydrated = True
        return self.current_state()

    def current_state(self) -> DegradationState:
        return self._build_state(self._mode, self._reason)

    def evaluate_from_slo(self, verdict: DataIntegrityVerdict) -> DegradationState:
        if verdict.severity == IntegritySeverity.HARD_BREACH:
            return self.apply_hard_critical_degrade("|".join(verdict.reasons) or "hard_slo_breach")

        if self._mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            if verdict.severity == IntegritySeverity.OK:
                self._recovery_streak += 1
            if self._recovery_streak >= HARD_RECOVERY_STREAK:
                return self._downgrade_hard_to_soft("hard_critical_recovered_to_soft")
            self._persist_state()
            return self.current_state()

        if self._mode == OperationalMode.SOFT_DEGRADE:
            if verdict.severity == IntegritySeverity.OK:
                self._recovery_streak += 1
            if self._recovery_streak >= SOFT_RECOVERY_STREAK:
                return self.reset_to_normal("slo_recovered")
            self._persist_state()
            return self.current_state()

        if verdict.severity == IntegritySeverity.SOFT_BREACH:
            return self.apply_soft_degrade("|".join(verdict.reasons) or "soft_slo_breach")

        if self._mode == OperationalMode.RECON_DEGRADED_MANAGE:
            return self.current_state()

        self._recovery_streak = 0
        self._persist_state()
        return self.current_state()

    def apply_soft_degrade(self, reason: str) -> DegradationState:
        if self._mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            return self.current_state()
        entering = self._mode != OperationalMode.SOFT_DEGRADE
        self._mode = OperationalMode.SOFT_DEGRADE
        self._reason = reason
        self._flatten_latched = False
        if entering:
            self._activated_at = datetime.now(timezone.utc).isoformat()
            self._recovery_streak = 0
        self._persist_state()
        return self._build_state(self._mode, reason)

    def apply_recon_degraded_manage(self, reason: str) -> DegradationState:
        if self._mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            return self.current_state()
        self._mode = OperationalMode.RECON_DEGRADED_MANAGE
        self._reason = reason
        self._activated_at = datetime.now(timezone.utc).isoformat()
        self._recovery_streak = 0
        self._flatten_latched = False
        self._persist_state()
        return self._build_state(self._mode, reason)

    def apply_hard_critical_degrade(self, reason: str) -> DegradationState:
        entering = self._mode != OperationalMode.HARD_CRITICAL_DEGRADE
        self._mode = OperationalMode.HARD_CRITICAL_DEGRADE
        self._reason = reason
        if entering:
            self._activated_at = datetime.now(timezone.utc).isoformat()
        self._recovery_streak = 0
        self._flatten_latched = True
        self._persist_state()
        if entering:
            self._dispatch_degradation_entered(reason)
        return self._build_state(self._mode, reason)

    def _dispatch_degradation_entered(self, reason: str) -> None:
        """Page URGENT immediately on entering HARD_CRITICAL_DEGRADE. Prefers the injected notifier; also
        fires the webhook page + durable log so the incident is recorded even if push is unconfigured."""
        if self.notifier is not None:
            try:
                self.notifier.notify({"kind": self._DEGRADE_KIND, "severity": "urgent",
                                      "message": f"Engine entered HARD_CRITICAL_DEGRADE: {reason}",
                                      "detail": {"component": self._DEGRADE_COMPONENT, "reason": reason, "mode": self._mode.value}})
            except Exception as exc:
                import logging; logging.getLogger(__name__).warning("degrade_notify_failed: %s", exc)
        import asyncio
        try:
            from infra.control.alerts import IncidentType, dispatch_critical_page  # best-effort: may not be wired yet
        except Exception:
            import logging; logging.getLogger(__name__).critical("HARD_CRITICAL_DEGRADE (alerts unwired): %s", reason); return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(dispatch_critical_page(IncidentType.HARD_CRITICAL_DEGRADE,
                             f"Engine entered HARD_CRITICAL_DEGRADE: {reason}", {"reason": reason, "mode": self._mode.value}))
        except RuntimeError:
            import logging; logging.getLogger(__name__).critical("HARD_CRITICAL_DEGRADE (no loop, webhook suppressed): %s", reason)

    def _dispatch_degradation_recovered(self, reason: str) -> None:
        """A POSITIVE 'recovered' message on EVERY downgrade (HARD->SOFT and SOFT->NORMAL)."""
        if self.notifier is not None:
            try:
                self.notifier.resolve(component=self._DEGRADE_COMPONENT, kind=self._DEGRADE_KIND)
                self.notifier.notify({"kind": self._DEGRADE_KIND + "_recovered", "severity": "info",
                                      "message": f"Engine degradation recovered -> {self._mode.value}: {reason}",
                                      "detail": {"component": self._DEGRADE_COMPONENT, "reason": reason, "mode": self._mode.value}})
            except Exception as exc:
                import logging; logging.getLogger(__name__).warning("degrade_recovered_notify_failed: %s", exc)
        import logging; logging.getLogger(__name__).warning("degradation_recovered -> %s (%s)", self._mode.value, reason)

    def _downgrade_hard_to_soft(self, reason: str) -> DegradationState:
        self._mode = OperationalMode.SOFT_DEGRADE
        self._reason = reason
        self._activated_at = datetime.now(timezone.utc).isoformat()
        self._recovery_streak = 0
        self._flatten_latched = False
        self._persist_state()
        self._dispatch_degradation_recovered(reason)
        return self._build_state(self._mode, reason)

    def reset_to_normal(self, reason: str = "manual_reset") -> DegradationState:
        was_degraded = self._mode != OperationalMode.NORMAL
        self._mode = OperationalMode.NORMAL
        self._reason = reason
        self._activated_at = None
        self._recovery_streak = 0
        self._flatten_latched = False
        self._persist_state()
        if was_degraded:
            self._dispatch_degradation_recovered(reason)
        return self._build_state(self._mode, reason)

    def should_block_entry_action(self, action: SignalAction | None) -> bool:
        state = self.current_state()
        if not state.block_new_entries or action is None:
            return False
        return action in (SignalAction.LONG, SignalAction.SHORT)

    def should_ignore_model_signal(self) -> bool:
        return self.current_state().ignore_model_signals

    def consume_flatten_request(self) -> bool:
        if not self._flatten_latched:
            return False
        self._flatten_latched = False
        self._persist_state()
        return True

    def override_routing_params(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self.current_state()
        if state.mode == OperationalMode.NORMAL:
            return params
        adjusted = dict(params)
        adjusted["operational_mode"] = state.mode.value
        adjusted["degradation_reason"] = state.reason
        if state.bypass_standard_timers:
            adjusted["bypass_standard_timers"] = True
            adjusted["poll_interval_seconds"] = 0
        if state.block_new_entries:
            adjusted["block_new_entries"] = True
        return adjusted

    def _persist_state(self) -> None:
        persist_governance_state(degradation_mode=self._mode.value, trigger_reason=self._reason,
                                 entered_at=self._activated_at, flatten_latched=self._flatten_latched,
                                 recovery_streak=self._recovery_streak, db_path=self.db_path)
        self._hydrated = True

    def _build_state(self, mode: OperationalMode, reason: str) -> DegradationState:
        now = self._activated_at or datetime.now(timezone.utc).isoformat()
        if mode == OperationalMode.SOFT_DEGRADE:
            return DegradationState(mode, True, False, False, False, True, reason, now)
        if mode == OperationalMode.RECON_DEGRADED_MANAGE:
            return DegradationState(mode, True, False, False, False, True, reason, now)
        if mode == OperationalMode.HARD_CRITICAL_DEGRADE:
            return DegradationState(mode, True, True, True, True, False, reason, now)
        return DegradationState(OperationalMode.NORMAL, False, False, False, False, True, reason, self._activated_at or "")
