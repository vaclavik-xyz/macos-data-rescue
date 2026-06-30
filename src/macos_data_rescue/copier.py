from __future__ import annotations

import multiprocessing
import os
import queue
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import iter_selected_files, load_config, mark_copying, mark_result, migrate_manifest


CHUNK_SIZE = 1024 * 1024
RESCUE_TMP_SUFFIX = ".rescue-tmp"
WORK_STATUSES = ("pending", "copying", "failed", "timed_out")
DONE_STATUSES = ("copied", "skipped")


@dataclass
class CopySummary:
    processed: int = 0
    copied: int = 0
    failed: int = 0
    timed_out: int = 0
    skipped: int = 0

    def as_line(self) -> str:
        return (
            f"processed={self.processed} copied={self.copied} failed={self.failed} "
            f"timed_out={self.timed_out} skipped={self.skipped}"
        )


def copy_job(job_dir: Path, *, phase: str, timeout: float, limit: int | None = None) -> CopySummary:
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    cleanup_stale_temps(job_dir, phase, config.dest)
    summary = CopySummary()
    attempted = 0
    handled_ids: set[int] = set()
    for row in iter_selected_files(job_dir, phase, statuses=WORK_STATUSES):
        if limit is not None and attempted >= limit:
            break
        process_row(job_dir, config.source, config.dest, row, timeout, summary)
        handled_ids.add(int(row["id"]))
        attempted += 1

    if limit is not None and attempted >= limit:
        return summary

    for row in iter_selected_files(job_dir, phase, statuses=DONE_STATUSES):
        if int(row["id"]) in handled_ids:
            continue
        dest = config.dest / row["relative_path"]
        if row["status"] == "skipped" or destination_matches(dest, row):
            summary.skipped += 1
            continue
        if limit is not None and attempted >= limit:
            break
        process_row(job_dir, config.source, config.dest, row, timeout, summary)
        handled_ids.add(int(row["id"]))
        attempted += 1
    return summary


def process_row(
    job_dir: Path,
    source_root: Path,
    dest_root: Path,
    row: Any,
    timeout: float,
    summary: CopySummary,
) -> None:
    source = source_root / row["relative_path"]
    dest = dest_root / row["relative_path"]
    summary.processed += 1
    mark_copying(job_dir, row["id"])
    if row["kind"] == "symlink":
        mark_result(
            job_dir,
            row["id"],
            "skipped",
            error="symlink skipped to avoid following external targets",
        )
        summary.skipped += 1
        return

    result = copy_one_with_timeout(source, dest, timeout)
    status = str(result["status"])
    if status == "copied":
        copied_bytes = int(result.get("copied_bytes", 0))
        mark_result(job_dir, row["id"], "copied", copied_bytes=copied_bytes)
        summary.copied += 1
    elif status == "timed_out":
        mark_result(job_dir, row["id"], "timed_out", error=str(result["error"]))
        summary.timed_out += 1
    else:
        mark_result(job_dir, row["id"], "failed", error=str(result["error"]))
        summary.failed += 1


def destination_matches(dest: Path, row: Any) -> bool:
    try:
        info = dest.lstat() if row["kind"] == "symlink" else dest.stat()
    except OSError:
        return False
    return info.st_size == row["size"] and info.st_mtime_ns == row["mtime_ns"]


def copy_one_with_timeout(source: Path, dest: Path, timeout: float) -> dict[str, object]:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = make_temp_path(dest)
    process = ctx.Process(
        target=_copy_file_child,
        args=(str(source), str(dest), str(temp), result_queue),
    )
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.kill()
        process.join(1)
        cleanup_path(temp)
        if process.is_alive():
            return {
                "status": "timed_out",
                "error": f"copy timed out after {timeout:g} seconds; worker did not exit after kill",
            }
        return {"status": "timed_out", "error": f"copy timed out after {timeout:g} seconds"}

    try:
        return result_queue.get_nowait()
    except queue.Empty:
        if process.exitcode == 0:
            return {"status": "failed", "error": "copy worker exited without a result"}
        return {"status": "failed", "error": f"copy worker exited with code {process.exitcode}"}


def _copy_file_child(
    source_text: str,
    dest_text: str,
    temp_text: str,
    result_queue: multiprocessing.Queue,
) -> None:
    source = Path(source_text)
    dest = Path(dest_text)
    temp = Path(temp_text)
    copied_bytes = 0
    try:
        with source.open("rb") as src, temp.open("wb") as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
                copied_bytes += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        copy_basic_metadata(source, temp)
        copy_xattrs(source, temp)
        os.replace(temp, dest)
        fsync_directory(dest.parent)
        result_queue.put({"status": "copied", "copied_bytes": copied_bytes})
    except BaseException as exc:
        cleanup_path(temp)
        result_queue.put({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})


def make_temp_path(dest: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=RESCUE_TMP_SUFFIX, dir=dest.parent)
    os.close(fd)
    return Path(name)


def cleanup_stale_temps(job_dir: Path, phase: str, dest_root: Path) -> None:
    protected_names_by_dir: dict[Path, set[str]] = {}
    for row in iter_selected_files(job_dir, phase):
        dest = dest_root / row["relative_path"]
        protected_names_by_dir.setdefault(dest.parent, set()).add(dest.name)

    for directory, protected_names in protected_names_by_dir.items():
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in protected_names:
                continue
            if is_internal_temp_name(entry.name):
                cleanup_path(entry)


def is_internal_temp_name(name: str) -> bool:
    if not name.startswith(".") or not name.endswith(RESCUE_TMP_SUFFIX):
        return False
    prefix = name[: -len(RESCUE_TMP_SUFFIX)]
    return "." in prefix[1:]


def cleanup_path(temp: Path) -> None:
    try:
        if temp.exists() or temp.is_symlink():
            temp.unlink()
    except OSError:
        pass


def copy_basic_metadata(source: Path, dest: Path) -> None:
    """Copy safe metadata without APFS/BSD flags or ownership.

    shutil.copystat() can copy macOS flags such as UF_IMMUTABLE onto the
    temporary destination file before os.replace(). An immutable temp can then
    fail to publish with EPERM. For rescue we preserve mode and timestamps only.
    """
    info = source.stat(follow_symlinks=True)
    os.chmod(dest, info.st_mode & 0o777)
    os.utime(dest, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=True)


def copy_xattrs(source: Path, dest: Path) -> None:
    if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")):
        return
    try:
        names = os.listxattr(source)
    except OSError:
        return
    for name in names:
        try:
            os.setxattr(dest, name, os.getxattr(source, name))
        except OSError:
            continue


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
