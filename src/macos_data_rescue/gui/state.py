"""Operation state model for the GUI, independent of any widget toolkit.

Every state carries its presentation and locking policy in ``STATE_INFO`` so a
new state is one enum member, one ``StateInfo`` row and its translations.

``WAITING`` (waiting for storage) comes from the persisted activity, not from an
exit code: a ``copy --auto-reconnect`` lost its disk and is cooling down or
waiting for the volume. It is busy and stoppable like ``RUNNING``; Stop is the
same graceful SIGINT, after which the CLI records ``stopped`` and exits 3
(paused, not failed). Exit code mapping (README "Exit codes"): 3 is always the
storage pause; any other non-zero code, including 130 from an interrupted copy
pass, is "stopped" only when the operator asked for it and "failed" otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

# CLI exit codes (README "Exit codes" / docs/agent-runbook.md)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_STORAGE_PAUSED = 3


class OpState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
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
    color: str  # palette name (gui/palette.py), resolved for light/dark appearance
    # Optional banner explaining what the operator should do next.
    message_key: str | None = None
    # Offer the Resume action (copy paused by the CLI with exit code 3).
    can_resume: bool = False


STATE_INFO: dict[OpState, StateInfo] = {
    OpState.IDLE: StateInfo(False, "state_idle", "gray"),
    OpState.RUNNING: StateInfo(True, "state_running", "blue"),
    OpState.WAITING: StateInfo(True, "state_waiting", "orange", "msg_waiting"),
    OpState.STOPPING: StateInfo(True, "state_stopping", "amber", "msg_stopping"),
    OpState.STOPPED: StateInfo(False, "state_stopped", "amber", "msg_stopped"),
    OpState.PAUSED_STORAGE: StateInfo(False, "state_paused_storage", "red", "msg_paused_storage",
                                      can_resume=True),
    OpState.FAILED: StateInfo(False, "state_failed", "red", "msg_failed"),
    OpState.DONE: StateInfo(False, "state_done", "green"),
}

# States that only describe the last finished command; a persisted storage
# pause found in the manifest may replace them.
_SETTLED = frozenset({OpState.IDLE, OpState.DONE, OpState.FAILED, OpState.STOPPED})
# States in which the console's own child can be stopped.
STOPPABLE = frozenset({OpState.RUNNING, OpState.WAITING})


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


def reconcile_with_activity(current: OpState, activity_state: str | None, *,
                            child_running: bool = False) -> OpState:
    """Align the GUI with the copy activity persisted in the manifest.

    A job opened after a storage pause (or paused by a terminal session) shows
    the paused state and offers Resume; once the activity is no longer paused
    the banner is cleared. A waiting copy (the snapshot reports ``waiting`` only
    while its writer holds the job lock) is never shown as running or idle:
    the console's own child switches between RUNNING and WAITING, a waiting
    writer outside the console (e.g. a terminal session) shows WAITING while
    the console is otherwise idle.
    """
    if activity_state == "waiting":
        if current is OpState.RUNNING or (not child_running and (current in _SETTLED
                                                                  or current is OpState.PAUSED_STORAGE)):
            return OpState.WAITING
        return current
    if current is OpState.WAITING:
        if child_running:
            return OpState.RUNNING
        return OpState.PAUSED_STORAGE if activity_state == "paused" else OpState.IDLE
    if activity_state == "paused" and current in _SETTLED:
        return OpState.PAUSED_STORAGE
    if current is OpState.PAUSED_STORAGE and activity_state not in (None, "paused"):
        return OpState.IDLE
    return current


def control_states(state: OpState, *, job_loaded: bool, own_child: bool = True) -> dict[str, bool]:
    """Which control groups are enabled. Pure so the locking policy is testable.

    ``own_child`` is False when a busy state stems from a writer outside the
    console (a terminal session waiting for storage): the job stays locked,
    but the console cannot stop it and may switch to another job.
    """
    idle = not STATE_INFO[state].busy
    job_idle = idle and job_loaded
    switch = idle or not own_child
    return {
        # a loaded job owns source/dest/profile; they are shown read-only
        "job_fields": idle and not job_loaded,
        "init": idle and not job_loaded,
        "open_job": switch,
        "close_job": switch and job_loaded,
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
        "stop": state in STOPPABLE and own_child,
    }


@dataclass(frozen=True)
class WaitView:
    """What the waiting banner shows, parsed from the activity snapshot."""

    phase: str | None  # "cooldown" | "waiting"
    remaining: float | None  # seconds until ``wait_until`` (None: unknown)
    attempt: int | None
    reason: str | None


def wait_view(activity: dict, now: datetime) -> WaitView:
    """Countdown to ``wait_until`` (ISO 8601, UTC) as of ``now`` (timezone-aware)."""
    remaining = None
    until = activity.get("wait_until")
    if isinstance(until, str):
        try:
            moment = datetime.fromisoformat(until)
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            remaining = max(0.0, (moment - now).total_seconds())
    return WaitView(activity.get("wait_phase"), remaining, activity.get("attempt"), activity.get("reason"))
