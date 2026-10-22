"""Operation state model for the GUI, independent of any widget toolkit.

Every state carries its presentation and locking policy in ``STATE_INFO`` so a
new state is one enum member, one ``StateInfo`` row and its translations. A
planned ``WAITING_FOR_STORAGE`` state (CLI retrying after the original disk
reappears) would be ``busy=True`` with a stop action, mapped from a new exit
code or activity state in ``state_after_exit`` / ``reconcile_with_activity``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# CLI exit codes (README "Exit codes" / docs/agent-runbook.md)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_STORAGE_PAUSED = 3


class OpState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    PAUSED_STORAGE = "paused-storage"
    FAILED = "failed"
    DONE = "done"


@dataclass(frozen=True)
class StateInfo:
    # True while a child process may still be alive: every control that can
    # write to the job (or start another writer) must stay locked.
    busy: bool
    badge_key: str
    color: str
    # Optional banner explaining what the operator should do next.
    message_key: str | None = None
    # Offer the Resume action (copy paused by the CLI with exit code 3).
    can_resume: bool = False


STATE_INFO: dict[OpState, StateInfo] = {
    OpState.IDLE: StateInfo(False, "state_idle", "#4b5563"),
    OpState.RUNNING: StateInfo(True, "state_running", "#1d4ed8"),
    OpState.STOPPING: StateInfo(True, "state_stopping", "#b45309", "msg_stopping"),
    OpState.STOPPED: StateInfo(False, "state_stopped", "#b45309", "msg_stopped"),
    OpState.PAUSED_STORAGE: StateInfo(False, "state_paused_storage", "#b91c1c", "msg_paused_storage",
                                      can_resume=True),
    OpState.FAILED: StateInfo(False, "state_failed", "#b91c1c", "msg_failed"),
    OpState.DONE: StateInfo(False, "state_done", "#15803d"),
}

# States that only describe the last finished command; a persisted storage
# pause found in the manifest may replace them.
_SETTLED = frozenset({OpState.IDLE, OpState.DONE, OpState.FAILED, OpState.STOPPED})


def info(state: OpState) -> StateInfo:
    return STATE_INFO[state]


def state_after_exit(returncode: int | None, *, stop_requested: bool, error: bool = False) -> OpState:
    """Map how a command sequence ended to the state shown to the operator."""
    if returncode == EXIT_STORAGE_PAUSED:
        # The CLI paused cleanly and recorded why; that matters more than a
        # concurrent stop request.
        return OpState.PAUSED_STORAGE
    if stop_requested:
        return OpState.STOPPED
    if error or returncode != EXIT_OK:
        return OpState.FAILED
    return OpState.DONE


def reconcile_with_activity(current: OpState, activity_state: str | None) -> OpState:
    """Align an idle GUI with the copy activity persisted in the manifest.

    A job opened after a storage pause (or paused by a terminal session) shows
    the paused state and offers Resume; once the activity is no longer paused
    the banner is cleared.
    """
    if activity_state == "paused" and current in _SETTLED:
        return OpState.PAUSED_STORAGE
    if current is OpState.PAUSED_STORAGE and activity_state not in (None, "paused"):
        return OpState.IDLE
    return current


def control_states(state: OpState, *, job_loaded: bool) -> dict[str, bool]:
    """Which control groups are enabled. Pure so the locking policy is testable."""
    idle = not STATE_INFO[state].busy
    job_idle = idle and job_loaded
    return {
        # a loaded job owns source/dest/profile; they are shown read-only
        "job_fields": idle and not job_loaded,
        "init": idle and not job_loaded,
        "open_job": idle,
        "close_job": job_idle,
        "preflight": idle,
        "phase": job_idle,
        "options": idle,
        "approve": job_idle,
        "scan": job_idle,
        "copy": job_idle,
        "verify": job_idle,
        "guide": job_idle,
        "resume": job_idle and STATE_INFO[state].can_resume,
        "recover": job_idle,
        "customer_report": job_idle,
        "stop": state is OpState.RUNNING,
    }
