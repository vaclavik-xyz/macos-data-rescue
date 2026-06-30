from __future__ import annotations

import multiprocessing
import os
import queue
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import load_config, mark_copying, mark_result, selected_files


CHUNK_SIZE = 1024 * 1024


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
    summary = CopySummary()
    for row in selected_files(job_dir, phase, limit):
        summary.processed += 1
        source = config.source / row["relative_path"]
        dest = config.dest / row["relative_path"]
        if row["status"] == "copied" and destination_matches(dest, row):
            summary.skipped += 1
            continue

        mark_copying(job_dir, row["id"])
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
    return summary


def destination_matches(dest: Path, row: Any) -> bool:
    try:
        info = dest.stat()
    except OSError:
        return False
    return info.st_size == row["size"] and info.st_mtime_ns == row["mtime_ns"]


def copy_one_with_timeout(source: Path, dest: Path, timeout: float) -> dict[str, object]:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_copy_file_child,
        args=(str(source), str(dest), result_queue),
    )
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join()
        cleanup_temp(dest)
        return {"status": "timed_out", "error": f"copy timed out after {timeout:g} seconds"}

    try:
        return result_queue.get_nowait()
    except queue.Empty:
        if process.exitcode == 0:
            return {"status": "failed", "error": "copy worker exited without a result"}
        return {"status": "failed", "error": f"copy worker exited with code {process.exitcode}"}


def _copy_file_child(source_text: str, dest_text: str, result_queue: multiprocessing.Queue) -> None:
    source = Path(source_text)
    dest = Path(dest_text)
    temp = temp_path_for(dest)
    copied_bytes = 0
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if temp.exists() or temp.is_symlink():
            temp.unlink()
        with source.open("rb") as src, temp.open("wb") as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
                copied_bytes += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, temp, follow_symlinks=True)
        copy_xattrs(source, temp)
        os.replace(temp, dest)
        fsync_directory(dest.parent)
        result_queue.put({"status": "copied", "copied_bytes": copied_bytes})
    except BaseException as exc:
        cleanup_temp(dest)
        result_queue.put({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})


def temp_path_for(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.rescue-tmp")


def cleanup_temp(dest: Path) -> None:
    temp = temp_path_for(dest)
    try:
        if temp.exists() or temp.is_symlink():
            temp.unlink()
    except OSError:
        pass


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
