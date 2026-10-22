"""Read job state off the Tk thread.

The job directory lives on an external recovery disk; a hung disk can block
any filesystem call indefinitely. All manifest reads therefore run in daemon
threads and report back through the GUI event queue. At most one snapshot
read is in flight: a read that never returns blocks further polling instead
of piling up threads, and the GUI shows how long the job has been
unresponsive.
"""
from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..activity import activity_snapshot
from ..guide import next_text
from ..manifest import GATED_PHASES, UNREADABLE_COMPRESSED, connect, load_approval, load_config, status_summary
from ..storage_events import blocked_by_give_up, list_events

LOG = logging.getLogger(__name__)

FAILED_ROW_LIMIT = 500
FAILED_STATUSES = ("failed", "timed_out", UNREADABLE_COMPRESSED)


@dataclass(frozen=True)
class JobSnapshot:
    job_dir: Path
    source: Path
    dest: Path
    profile: str
    excludes: tuple[str, ...]
    counts: dict[str, int]
    activity: dict[str, Any]
    guide_text: str
    approvals: dict[str, str | None]
    failed_rows: tuple[tuple[str, str, int], ...]
    # storage_events summary: disconnects, successful automatic reconnects, and
    # whether a reconnect give-up still waits for the operator's acknowledgement
    storage_losses: int = 0
    reconnects: int = 0
    storage_blocked: bool = False


@dataclass(frozen=True)
class SnapshotResult:
    job_dir: Path
    snapshot: JobSnapshot | None
    error: str | None = None


@dataclass(frozen=True)
class TaskDone:
    token: int
    result: Any = None
    error: BaseException | None = None


def read_job_snapshot(job_dir: Path) -> JobSnapshot:
    """Read everything the console shows; never writes to the job."""
    config = load_config(job_dir)
    summary = status_summary(job_dir)
    conn = connect(job_dir)
    try:
        placeholders = ",".join("?" for _ in FAILED_STATUSES)
        rows = conn.execute(
            f"select relative_path, status, size from files where kind = 'file' and status in ({placeholders}) "  # nosec B608
            "order by relative_path limit ?",
            (*FAILED_STATUSES, FAILED_ROW_LIMIT),
        ).fetchall()
        events = list_events(conn)
        storage_blocked = blocked_by_give_up(conn)
    finally:
        conn.close()
    return JobSnapshot(
        job_dir=job_dir,
        source=config.source,
        dest=config.dest,
        profile=config.profile,
        excludes=config.excludes,
        counts={status: item["count"] for status, item in summary.items()},
        activity=activity_snapshot(job_dir),
        guide_text=next_text(job_dir),
        approvals={phase: load_approval(job_dir, phase) for phase in GATED_PHASES},
        failed_rows=tuple((row["relative_path"], row["status"], int(row["size"] or 0)) for row in rows),
        storage_losses=sum(1 for event in events if event["kind"] == "lost"),
        reconnects=sum(1 for event in events if event["kind"] == "resumed"),
        storage_blocked=storage_blocked,
    )


class JobMonitor:
    def __init__(self, events: queue.Queue, *, reader: Callable[[Path], JobSnapshot] = read_job_snapshot,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.events = events
        self._reader = reader
        self._clock = clock
        self._lock = threading.Lock()
        self._started: float | None = None

    def request(self, job_dir: Path) -> bool:
        """Start a snapshot read unless one is still running."""
        with self._lock:
            if self._started is not None:
                return False
            self._started = self._clock()
        threading.Thread(target=self._read, args=(job_dir,), name="job-monitor", daemon=True).start()
        return True

    def stalled_for(self) -> float:
        """Seconds the in-flight read has been running (0 when idle)."""
        with self._lock:
            return 0.0 if self._started is None else self._clock() - self._started

    def _read(self, job_dir: Path) -> None:
        try:
            result = SnapshotResult(job_dir, self._reader(job_dir))
        except Exception as exc:
            result = SnapshotResult(job_dir, None, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._started = None
        self.events.put(result)


class BackgroundTasks:
    """Run one-off blocking reads off the Tk thread; results arrive as TaskDone."""

    def __init__(self, events: queue.Queue) -> None:
        self.events = events
        self._tokens = itertools.count(1)

    def submit(self, function: Callable[[], Any]) -> int:
        token = next(self._tokens)

        def run() -> None:
            try:
                self.events.put(TaskDone(token, result=function()))
            except Exception as exc:
                self.events.put(TaskDone(token, error=exc))

        threading.Thread(target=run, name="gui-task", daemon=True).start()
        return token


@dataclass(frozen=True)
class Telemetry:
    counts: dict[str, int] = field(default_factory=dict)
    total_files: int = 0
    rescued_files: int = 0
    activity_state: str | None = None
    operation: str | None = None
    rate_bytes_s: float = 0.0
    eta_seconds: float | None = None
    current_path: str | None = None
    current_bytes: int = 0
    transferred_bytes: int = 0
    remaining_bytes: int = 0
    pause_reason: str | None = None

    @property
    def bytes_fraction(self) -> float | None:
        total = self.transferred_bytes + self.remaining_bytes
        return self.transferred_bytes / total if total > 0 else None


BADGE_STATUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("badge_copied", ("copied",)),
    ("badge_fallback", ("copied_from_fallback",)),
    ("badge_pending", ("pending", "copying")),
    ("badge_timed_out", ("timed_out",)),
    ("badge_failed", ("failed",)),
    ("badge_compressed", (UNREADABLE_COMPRESSED,)),
    ("badge_skipped", ("skipped",)),
)


def telemetry_from(snapshot: JobSnapshot) -> Telemetry:
    counts = snapshot.counts
    activity = snapshot.activity or {}
    live = activity.get("state") == "running"
    return Telemetry(
        counts={key: sum(counts.get(status, 0) for status in statuses) for key, statuses in BADGE_STATUSES},
        total_files=sum(counts.values()),
        rescued_files=counts.get("copied", 0) + counts.get("copied_from_fallback", 0),
        activity_state=activity.get("state"),
        operation=activity.get("operation"),
        rate_bytes_s=float(activity.get("rate_bytes_s") or 0) if live else 0.0,
        eta_seconds=activity.get("eta_seconds") if live else None,
        current_path=activity.get("path") if live else None,
        current_bytes=int(activity.get("current_bytes") or 0),
        transferred_bytes=int(activity.get("transferred_bytes") or 0),
        remaining_bytes=int(activity.get("remaining_bytes") or 0),
        pause_reason=activity.get("reason") if activity.get("state") == "paused" else None,
    )
