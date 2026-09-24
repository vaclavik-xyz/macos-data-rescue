"""Verify recovered content against the digest captured in the copy stream."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .copier import CHUNK_SIZE, CopyWorker, safe_relative_parts
from .locking import exclusive_job
from .manifest import COPIED_STATUSES, connect, iter_selected_files, migrate_manifest, utc_now


@dataclass
class VerifySummary:
    verified: int = 0
    failed: int = 0
    unverifiable: int = 0

    def as_line(self) -> str:
        return f"verified={self.verified} failed={self.failed} unverifiable={self.unverifiable}"


@exclusive_job
def verify_job(job_dir: Path, *, phase: str = "all", timeout: float = 30,
               limit: int | None = None) -> VerifySummary:
    migrate_manifest(job_dir)
    conn = connect(job_dir)
    worker = CopyWorker()
    summary = VerifySummary()
    try:
        # Do not stat/open the source here. Verification works with it disconnected.
        dest = Path(conn.execute("select value from config where key = 'dest'").fetchone()[0])
        for row in iter_selected_files(job_dir, phase, statuses=COPIED_STATUSES, limit=limit):
            if not row["sha256"]:
                status, error = "unverifiable", "No copy-time SHA256 recorded; destination alone cannot establish integrity."
                summary.unverifiable += 1
            else:
                result = worker.verify_one(dest, row["relative_path"], timeout)
                if result.get("sha256") == row["sha256"] and result.get("size") == row["copied_bytes"]:
                    status, error = "verified", None
                    summary.verified += 1
                elif result.get("status") in ("timed_out", "failed"):
                    status, error = "unverifiable", str(result.get("error", "Verification worker unavailable"))
                    summary.unverifiable += 1
                else:
                    status = "failed"
                    error = str(result.get("error", "Destination SHA256 or size differs from the copied stream."))
                    summary.failed += 1
            conn.execute("update files set verification_status = ?, verification_error = ?, verified_at = ? where id = ?",
                         (status, error, utc_now(), row["id"]))
            conn.commit()
    finally:
        worker.close()
        conn.close()
    return summary


def hash_destination(root: Path, relative_path: str) -> dict[str, object]:
    """Open each component relative to directory descriptors; never follow symlinks."""
    fds: list[int] = []
    try:
        parts = safe_relative_parts(relative_path)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            fds.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("destination is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError("destination changed during verification")
        return {"sha256": digest.hexdigest(), "size": size}
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        for fd in reversed(fds):
            os.close(fd)
