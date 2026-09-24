"""Durable operator-facing state. Watchers never touch rescue source files."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from .manifest import connect, utc_now


class Activity:
    def __init__(self, conn, operation: str, phase: str):
        self.conn = conn
        self.started = time.monotonic()
        self.completed_bytes = 0
        self.data = dict(operation=operation, phase=phase, state="running", path=None,
                         current_bytes=0, transferred_bytes=0, rate_bytes_s=0,
                         started_at=utc_now(), updated_at=utc_now())
        self.save()

    def save(self):
        self.data["updated_at"] = utc_now()
        self.conn.execute("insert or replace into config values('activity', ?)", (json.dumps(self.data),))
        self.conn.commit()

    def file(self, path):
        self.data.update(path=path, current_bytes=0)
        self.save()

    def progress(self, count):
        transferred = self.completed_bytes + count
        self.data.update(current_bytes=count, transferred_bytes=transferred,
                         rate_bytes_s=transferred / max(time.monotonic() - self.started, 0.001))
        self.save()

    def completed(self, count):
        self.progress(count)
        self.completed_bytes += count

    def finish(self, state="finished", reason=None):
        self.data.update(state=state, reason=reason, finished_at=utc_now())
        self.save()


def writer_running(job_dir: Path) -> bool:
    try:
        fd = os.open(job_dir / ".writer.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def activity_snapshot(job_dir: Path) -> dict:
    conn = connect(job_dir)
    try:
        row = conn.execute("select value from config where key = 'activity'").fetchone()
        if not row:
            return {}
        data = json.loads(row[0])
        if data["state"] == "running" and not writer_running(job_dir):
            data["state"] = "interrupted"
        phase = data["phase"]
        row = conn.execute("select count(*), coalesce(sum(size), 0) from files where kind = 'file' "
                           "and status in ('pending','copying','failed','timed_out','unreadable-compressed-flag') "
                           "and (? = 'all' or phase = ?)", (phase, phase)).fetchone()
        data["remaining_files"] = row[0]
        data["remaining_bytes"] = max(0, row[1] - (data["current_bytes"] if data["state"] == "running" else 0))
        data["eta_seconds"] = (round(data["remaining_bytes"] / data["rate_bytes_s"])
                               if data["state"] == "running" and data["rate_bytes_s"] > 0 else None)
        return data
    finally:
        conn.close()


def activity_text(job_dir: Path) -> str:
    data = activity_snapshot(job_dir)
    if not data:
        return ""
    return (f"\nstate={data['state']} operation={data['operation']} phase={data['phase']} "
            f"file={json.dumps(data['path'], ensure_ascii=False)} bytes={data['current_bytes']} "
            f"transferred_GB={data['transferred_bytes'] / 1e9:.3f} "
            f"MB/s={data['rate_bytes_s'] / 1e6:.2f} remaining_files={data['remaining_files']} "
            f"remaining_GB={data['remaining_bytes'] / 1e9:.3f} eta_seconds={data['eta_seconds']} "
            f"reason={json.dumps(data.get('reason'))} (remaining = known manifest queue)")


def watch_status(job_dir: Path, *, interval: float, count: int | None = None):
    from .reporting import status_text
    index = 0
    try:
        while count is None or index < count:
            print(status_text(job_dir), flush=True)
            index += 1
            if count is None or index < count:
                time.sleep(interval)
    except KeyboardInterrupt:
        return
