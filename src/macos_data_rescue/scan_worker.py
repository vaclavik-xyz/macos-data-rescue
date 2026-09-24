"""Bound each scan filesystem operation in a replaceable child process."""
from __future__ import annotations

import multiprocessing
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import RescueError
from .manifest import ScannedFile


@dataclass(frozen=True)
class ScanIssue:
    path: str
    phase: str
    error: str | None  # None resolves a previously recorded failure at this path.


@dataclass(frozen=True)
class ScanStopped:
    reason: str


class ScanDeadline(Exception):
    pass


class ScanWorker:
    def __init__(self, timeout, deadline=None, *, target=None):
        self.timeout = timeout
        self.deadline = deadline
        self.target = target or worker_loop
        self.process = None
        self.conn = None

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.kill()
            self.process.join(1)
            if self.process.is_alive():
                # Do not accumulate workers blocked in uninterruptible kernel I/O.
                raise RescueError("scan worker did not exit after kill; stop and inspect the mounted disk")
            self.process.close()
            self.process = None

    def request(self, operation, path, parts=()):
        now = time.monotonic()
        if self.deadline is not None and now >= self.deadline:
            raise ScanDeadline
        end = now + self.timeout
        if self.deadline is not None:
            end = min(end, self.deadline)
        if self.process is None:
            ctx = multiprocessing.get_context("spawn")
            self.conn, child = ctx.Pipe()
            self.process = ctx.Process(target=self.target, args=(child,), daemon=True)
            self.process.start()
            child.close()
        try:
            self.conn.send((operation, str(path), parts))
            if not self.conn.poll(max(0, end - time.monotonic())):
                self.close()
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise ScanDeadline
                return {"error": f"{operation} timed out after {self.timeout:g} seconds"}
            return self.conn.recv()
        except (EOFError, OSError) as exc:
            self.close()
            return {"error": f"scan worker failed: {exc}"}


def worker_loop(conn):
    from .scanner import warning_for
    try:
        while True:
            operation, text, parts = conn.recv()
            path = Path(text)
            try:
                if operation == "list":
                    # No stat/is_dir here: a blocked entry must not discard its siblings.
                    with os.scandir(path) as entries:
                        result = {"names": sorted(entry.name for entry in entries)}
                else:
                    info = path.stat(follow_symlinks=False)
                    result = {"info": info, "warning": warning_for(path, parts, info)}
            except OSError as exc:
                result = {"error": f"{type(exc).__name__}: {exc}", "missing": isinstance(exc, FileNotFoundError)}
            conn.send(result)
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    finally:
        conn.close()


def isolated_files(source: Path, *, phase: str, excludes=(), io_timeout=30, deadline=None, previous_issues=()):
    from .scanner import (IMPORTANT_DIRS, PHOTO_DIRS, file_kind, manifest_phase_for,
                          matches_exclude, should_descend, should_include_file, volume_root_for_home)
    if phase == "applications":
        roots = [(volume_root_for_home(source) / "Applications", ("Volume Applications",)),
                 (source / "Applications", ("Home Applications",))]
    elif phase in ("app-data", "library"):
        roots = [(source / "Library", ("Library",))]
    elif phase in ("important", "photos"):
        names = IMPORTANT_DIRS if phase == "important" else PHOTO_DIRS
        roots = [(source / name, (name,)) for name in sorted(names)]
    else:
        roots = [(source, ())]
    worker = ScanWorker(io_timeout, deadline)
    unvisited_issues = set(previous_issues)
    # Explicit stack avoids Python recursion limits on deeply nested source trees.
    stack = [(path, parts, True) for path, parts in reversed(roots)]
    try:
        while stack:
            path, parts, optional_root = stack.pop()
            if matches_exclude(parts, excludes):
                continue
            if parts and phase != "applications" and not (should_descend(parts, phase) or should_include_file(parts, phase)):
                continue
            unvisited_issues.discard(str(path))
            result = worker.request("stat", path, parts)
            if "error" in result:
                if optional_root and path != source and result.get("missing"):
                    yield ScanIssue(str(path), phase, None)
                else:
                    yield ScanIssue(str(path), phase, result["error"])
                continue
            info = result["info"]
            if not parts and not stat.S_ISDIR(info.st_mode):
                yield ScanIssue(str(path), phase, "source root is no longer a directory")
                continue
            if stat.S_ISDIR(info.st_mode):
                if parts and phase != "applications" and not should_descend(parts, phase):
                    continue
                listing = worker.request("list", path)
                if "error" in listing:
                    yield ScanIssue(str(path), phase, listing["error"])
                    continue
                yield ScanIssue(str(path), phase, None)
                # DFS is deterministic; failed entries leave the remaining stack intact.
                stack.extend((path / name, (*parts, name), False) for name in reversed(listing["names"]))
            else:
                include = phase == "applications" or should_include_file(parts, phase)
                if stat.S_ISLNK(info.st_mode) and should_descend(parts, phase):
                    include = True
                if not include:
                    continue
                yield ScanIssue(str(path), phase, None)
                yield ScannedFile(relative_path="/".join(parts), size=info.st_size,
                                  mtime_ns=info.st_mtime_ns, mode=stat.S_IMODE(info.st_mode),
                                  kind=file_kind(info.st_mode), phase=manifest_phase_for(parts, phase),
                                  source_path=str(path) if phase == "applications" else None,
                                  warning=result["warning"])
        # Previously failed entries may disappear from a successful parent listing.
        # Recheck only those old paths, in the same bounded worker. A confirmed
        # absence resolves current coverage; permission errors/timeouts stay visible.
        for text in sorted(unvisited_issues):
            result = worker.request("stat", Path(text))
            if result.get("missing"):
                yield ScanIssue(text, phase, None)
    except ScanDeadline:
        yield ScanStopped("timeout")
    finally:
        worker.close()
