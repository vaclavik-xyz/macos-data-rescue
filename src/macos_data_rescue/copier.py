from __future__ import annotations

import ctypes
import errno
import hashlib
import multiprocessing
import multiprocessing.process
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import RescueError
from .locking import exclusive_job
from .manifest import (
    COPIED_STATUSES,
    UNREADABLE_COMPRESSED,
    approval_required_message,
    connect,
    iter_selected_files,
    load_config,
    mark_copying,
    mark_result,
    migrate_manifest,
    unapproved_gated_phases,
    validate_application_layout,
)


CHUNK_SIZE = 1024 * 1024
RESCUE_TMP_SUFFIX = ".rescue-tmp"
# FAT stores mtimes with 2-second resolution and exFAT with 10 ms, so a
# destination on a recovery SSD can round the mtime copy_basic_metadata set.
DEST_MTIME_TOLERANCE_NS = 2_000_000_000
WORK_STATUSES = ("pending", "copying", "failed", "timed_out", UNREADABLE_COMPRESSED)
DONE_STATUSES = (*COPIED_STATUSES, "skipped")
UF_COMPRESSED = getattr(stat, "UF_COMPRESSED", 0x20)
XATTR_SHOWCOMPRESSION = 0x0020
XATTR_NOFOLLOW = 0x0001
XATTR_SKIP_NAMES = frozenset({"com.apple.quarantine", "com.apple.macl", "com.apple.decmpfs"})
XATTR_WARNING_PREFIX = "extended attributes not fully preserved"


@dataclass
class CopySummary:
    processed: int = 0
    copied: int = 0
    copied_from_fallback: int = 0
    unreadable_compressed: int = 0
    failed: int = 0
    timed_out: int = 0
    skipped: int = 0

    def as_line(self) -> str:
        return (
            f"processed={self.processed} copied={self.copied} failed={self.failed} "
            f"timed_out={self.timed_out} skipped={self.skipped} "
            f"copied_from_fallback={self.copied_from_fallback} "
            f"{UNREADABLE_COMPRESSED}={self.unreadable_compressed}"
        )


@exclusive_job
def copy_job(job_dir: Path, *, phase: str, timeout: float, limit: int | None = None,
             path: str | None = None, fallback_from: str | None = None) -> CopySummary:
    if (path is None) != (fallback_from is None):
        raise RescueError("--path and --fallback-from must be supplied together")
    config = load_config(job_dir)
    if config.profile == "customer-home":
        blocked = unapproved_gated_phases(job_dir, phase)
        if blocked:
            raise RescueError(approval_required_message(job_dir, blocked))
    if phase in {"all", "applications"}:
        with connect(job_dir) as check_conn:
            has_apps = check_conn.execute("select 1 from files where phase = 'applications' limit 1").fetchone()
        check_conn.close()
        if has_apps:
            validate_application_layout(config)
    migrate_manifest(job_dir)
    cleanup_stale_temps(job_dir, phase, config.dest)
    summary = CopySummary()
    attempted = 0
    handled_ids: set[int] = set()
    conn = connect(job_dir)
    worker = CopyWorker()
    try:
        if path is not None:
            row = conn.execute("select * from files where relative_path = ?", (path,)).fetchone()
            if row is None or row["kind"] != "file" or row["status"] not in ("failed", "timed_out", UNREADABLE_COMPRESSED):
                raise RescueError("--path must name a failed, timed-out, or unreadable compressed regular file")
            if phase != "all" and row["phase"] != phase:
                raise RescueError("--path is outside the selected phase")
            # A mapping is an explicit operator choice, never a filename/size guess.
            try:
                fallback_source(config.source, fallback_from)
                if fallback_from == path:
                    raise ValueError("fallback must name a different source file")
            except ValueError as exc:
                raise RescueError(str(exc)) from exc
            conn.execute("update files set fallback_source_path = ?, fallback_mtime_ns = null, "
                         "fallback_original_error = coalesce(fallback_original_error, error) where id = ?",
                         (fallback_from, row["id"]))
            conn.commit()
            row = conn.execute("select * from files where id = ?", (row["id"],)).fetchone()
            process_row(job_dir, config.source, config.dest, row, timeout, summary, conn=conn, worker=worker)
            return summary
        for row in iter_selected_files(job_dir, phase, statuses=WORK_STATUSES):
            if limit is not None and attempted >= limit:
                break
            process_row(job_dir, config.source, config.dest, row, timeout, summary, conn=conn, worker=worker)
            handled_ids.add(int(row["id"]))
            attempted += 1

        if limit is not None and attempted >= limit:
            return summary

        for row in iter_selected_files(job_dir, phase, statuses=DONE_STATUSES):
            if int(row["id"]) in handled_ids:
                continue
            try:
                dest = resolve_relative_path(config.dest, row["relative_path"], kind=row["kind"])
            except ValueError:
                if limit is not None and attempted >= limit:
                    break
                process_row(job_dir, config.source, config.dest, row, timeout, summary, conn=conn, worker=worker)
                handled_ids.add(int(row["id"]))
                attempted += 1
                continue
            if row["status"] == "skipped" or destination_matches(dest, row):
                summary.skipped += 1
                continue
            if limit is not None and attempted >= limit:
                break
            process_row(job_dir, config.source, config.dest, row, timeout, summary, conn=conn, worker=worker)
            handled_ids.add(int(row["id"]))
            attempted += 1
    finally:
        worker.close()
        conn.close()
    return summary


def process_row(
    job_dir: Path,
    source_root: Path,
    dest_root: Path,
    row: Any,
    timeout: float,
    summary: CopySummary,
    *,
    conn=None,
    worker: CopyWorker | None = None,
) -> None:
    scan_warning = without_copy_xattr_warnings(row["warning"])
    summary.processed += 1
    mark_copying(job_dir, row["id"], conn=conn)

    # Non-regular rows never touch source or destination, so they are skipped
    # before any path resolution; a symlinked application root would otherwise
    # fail the source containment check instead of being reported as skipped.
    if row["kind"] != "file":
        note = (
            "symlink skipped to avoid following external targets"
            if row["kind"] == "symlink"
            else f"{row['kind']} skipped: not a regular file"
        )
        mark_result(
            job_dir,
            row["id"],
            "skipped",
            error=note,
            warning=scan_warning,
            conn=conn,
        )
        summary.skipped += 1
        return

    try:
        dest = resolve_relative_path(dest_root, row["relative_path"], kind=row["kind"])
        fallback = row["fallback_source_path"] if "fallback_source_path" in row.keys() else None
        source = fallback_source(source_root, fallback) if fallback else resolve_row_source(source_root, row)
    except ValueError as exc:
        mark_result(
            job_dir,
            row["id"],
            "failed",
            error=f"ValueError: {exc}",
            warning=scan_warning,
            conn=conn,
        )
        summary.failed += 1
        return

    result = copy_one_with_timeout(source, dest, timeout, expected_size=int(row["size"]), worker=worker, registry_conn=conn)
    status = str(result["status"])
    if status == "copied":
        copied_bytes = int(result.get("copied_bytes", 0))
        mark_result(
            job_dir,
            row["id"],
            "copied_from_fallback" if fallback else "copied",
            copied_bytes=copied_bytes,
            sha256=result.get("sha256"),
            fallback_mtime_ns=int(result["source_mtime_ns"]) if fallback else None,
            warning=combine_warnings(scan_warning, result.get("warning")),
            conn=conn,
        )
        if fallback:
            summary.copied_from_fallback += 1
        else:
            summary.copied += 1
    elif status == UNREADABLE_COMPRESSED:
        mark_result(job_dir, row["id"], status, error=str(result["error"]), warning=scan_warning, conn=conn)
        summary.unreadable_compressed += 1
    elif status == "timed_out":
        mark_result(job_dir, row["id"], "timed_out", error=str(result["error"]), warning=scan_warning, conn=conn)
        summary.timed_out += 1
    else:
        copied_bytes = int(result.get("copied_bytes", 0))
        mark_result(
            job_dir,
            row["id"],
            "failed",
            error=str(result["error"]),
            copied_bytes=copied_bytes,
            warning=scan_warning,
            conn=conn,
        )
        summary.failed += 1


def fallback_source(source_root: Path, relative_path: str) -> Path:
    source = resolve_relative_path(source_root, relative_path, kind="file")
    current = source_root
    for part in safe_relative_parts(relative_path):
        current = current / part
        if current.is_symlink():
            raise ValueError("fallback source must not traverse symlinks")
    return source


def resolve_row_source(source_root: Path, row: Any) -> Path:
    source_path = row["source_path"] if "source_path" in row.keys() else None
    if not source_path:
        return resolve_relative_path(source_root, row["relative_path"], kind=row["kind"])
    if row["phase"] != "applications":
        raise ValueError("manifest source_path is only allowed for applications phase")

    source = Path(source_path)
    candidate = safe_containment_path(source, row["kind"], label=str(source_path))
    roots = application_source_roots_for_home(source_root)
    if not any(is_same_or_inside(candidate, root) for root in roots):
        raise ValueError(f"manifest source_path outside allowed application roots: {source_path}")
    return source


def resolve_relative_path(root: Path, relative_path: object, *, kind: object) -> Path:
    parts = safe_relative_parts(relative_path)
    path = root.joinpath(*parts)
    candidate = safe_containment_path(path, kind, label=str(relative_path))
    root_resolved = safe_resolve(root, label=str(relative_path))
    if not is_same_or_inside(candidate, root_resolved):
        raise ValueError(f"unsafe manifest relative_path outside root: {relative_path}")
    return path


def safe_relative_parts(relative_path: object) -> tuple[str, ...]:
    rel = PurePosixPath(str(relative_path))
    parts = rel.parts
    if rel.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe manifest relative_path: {relative_path}")
    return parts


def safe_containment_path(path: Path, kind: object, *, label: str) -> Path:
    try:
        return containment_path(path, kind)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"unsafe manifest relative_path cannot be resolved: {label}") from exc


def safe_resolve(path: Path, *, label: str) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"unsafe manifest relative_path cannot be resolved: {label}") from exc


def containment_path(path: Path, kind: object) -> Path:
    if kind == "symlink":
        return path.parent.resolve(strict=False) / path.name
    return path.resolve(strict=False)


def application_source_roots_for_home(source_root: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    volume_applications = volume_root_for_home(source_root) / "Applications"
    user_applications = source_root / "Applications"
    for root in (volume_applications, user_applications):
        if is_real_directory(root):
            try:
                roots.append(root.resolve(strict=False))
            except (OSError, RuntimeError):
                continue
    return tuple(roots)


def volume_root_for_home(source: Path) -> Path:
    parts = source.parts
    users_indexes = [index for index, part in enumerate(parts) if part == "Users"]
    if users_indexes:
        users_index = users_indexes[-1]
        if users_index > 0 and users_index + 1 < len(parts):
            return Path(*parts[:users_index])
    return source.parent


def is_real_directory(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def is_same_or_inside(candidate: Path, parent: Path) -> bool:
    return candidate == parent or parent in candidate.parents


def destination_matches(dest: Path, row: Any) -> bool:
    try:
        info = dest.lstat()
        if not stat.S_ISREG(info.st_mode):
            return False
    except OSError:
        return False
    if info.st_size != row["size"]:
        return False
    mtime = row["mtime_ns"]
    if "fallback_mtime_ns" in row.keys() and row["fallback_source_path"]:
        mtime = row["fallback_mtime_ns"]
        if mtime is None:
            return False
    return abs(info.st_mtime_ns - int(mtime)) <= DEST_MTIME_TOLERANCE_NS


def copy_one_with_timeout(
    source: Path,
    dest: Path,
    timeout: float,
    *,
    expected_size: int,
    worker: CopyWorker | None = None,
    registry_conn=None,
) -> dict[str, object]:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = make_temp_path(dest)
    except OSError as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "copied_bytes": 0}
    if registry_conn is not None:
        try:
            info = temp.lstat()
        except OSError as exc:
            cleanup_path(temp)
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "copied_bytes": 0}
        registry_conn.execute("insert into temporary_files values(?, ?, ?)", (str(temp), info.st_dev, info.st_ino))
        registry_conn.commit()
    owns_worker = worker is None
    if worker is None:
        worker = CopyWorker()
    try:
        result = worker.copy_one(source, dest, temp, expected_size, timeout)
    finally:
        if owns_worker:
            worker.close()
    if str(result.get("status")) != "copied":
        cleanup_path(temp)
    if registry_conn is not None and not temp.exists():
        registry_conn.execute("delete from temporary_files where path = ?", (str(temp),))
        registry_conn.commit()
    return result


class CopyWorker:
    """One long-lived spawn worker copying files sequentially.

    The worker is killed and replaced when a file hits its timeout or when
    it dies mid-job, so one hung or crashed copy never poisons later files.
    A fresh pipe is created with every worker generation; a killed worker
    can never leave partial garbage in the channel of its successor.
    """

    def __init__(self) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        self._process: multiprocessing.process.BaseProcess | None = None
        self._conn = None

    def copy_one(
        self,
        source: Path,
        dest: Path,
        temp: Path,
        expected_size: int,
        timeout: float,
    ) -> dict[str, object]:
        for _attempt in range(2):
            try:
                self._ensure_worker()
                self._conn.send((str(source), str(dest), str(temp), int(expected_size)))
            except OSError:
                self._discard()
                continue
            return self._wait_result(timeout)
        return {"status": "failed", "error": "copy worker could not be started", "copied_bytes": 0}

    def verify_one(self, root: Path, relative_path: str, timeout: float) -> dict[str, object]:
        self._ensure_worker()
        self._conn.send({"verify": str(root), "path": relative_path})
        return self._wait_result(timeout)

    def close(self) -> None:
        process, conn = self._process, self._conn
        self._process = None
        self._conn = None
        if conn is not None:
            try:
                conn.send(None)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if process is not None:
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)

    def _ensure_worker(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._discard()
        parent_conn, child_conn = self._ctx.Pipe()
        process = self._ctx.Process(target=_copy_worker_loop, args=(child_conn,), daemon=True)
        process.start()
        # The parent must drop its copy of the child end, or a dead worker
        # would never surface as EOF on parent_conn.
        child_conn.close()
        self._process = process
        self._conn = parent_conn

    def _discard(self) -> None:
        process, conn = self._process, self._conn
        self._process = None
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(1)

    def _wait_result(self, timeout: float) -> dict[str, object]:
        conn = self._conn
        process = self._process
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._timed_out_result(timeout)
            try:
                ready = conn.poll(remaining)
            except OSError:
                ready = True
            if ready:
                break
        try:
            result = conn.recv()
        except (EOFError, OSError):
            process.join(1)
            exitcode = process.exitcode
            self._discard()
            if exitcode == 0:
                return {"status": "failed", "error": "copy worker exited without a result"}
            return {"status": "failed", "error": f"copy worker exited with code {exitcode}"}
        return result

    def _timed_out_result(self, timeout: float) -> dict[str, object]:
        process = self._process
        self._discard()
        if process is not None and process.is_alive():
            return {
                "status": "timed_out",
                "error": f"copy timed out after {timeout:g} seconds; worker did not exit after kill",
            }
        return {"status": "timed_out", "error": f"copy timed out after {timeout:g} seconds"}


def _copy_worker_loop(conn) -> None:
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError, KeyboardInterrupt):
            return
        if job is None:
            return
        if isinstance(job, dict) and "verify" in job:
            from .verification import hash_destination
            result = hash_destination(Path(job["verify"]), job["path"])
        else:
            source_text, dest_text, temp_text, expected_size = job
            result = _copy_one_in_worker(Path(source_text), Path(dest_text), Path(temp_text), expected_size)
        result["worker_pid"] = os.getpid()
        try:
            conn.send(result)
        except (EOFError, OSError, KeyboardInterrupt):
            return


def _copy_one_in_worker(source: Path, dest: Path, temp: Path, expected_size: int) -> dict[str, object]:
    copied_bytes = 0
    digest = hashlib.sha256()
    source_info = None
    reading_source = False
    try:
        source_info = source.stat(follow_symlinks=False)
        if stat.S_ISLNK(source_info.st_mode):
            raise ValueError("source became a symlink after scan")
        reading_source = True
        with source.open("rb") as src:
            reading_source = False
            with temp.open("wb") as dst:
                while True:
                    reading_source = True
                    chunk = src.read(CHUNK_SIZE)
                    reading_source = False
                    if not chunk:
                        break
                    dst.write(chunk)
                    digest.update(chunk)
                    copied_bytes += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
        if copied_bytes != expected_size:
            cleanup_path(temp)
            return {
                "status": "failed",
                "error": f"incomplete copy: expected {expected_size} bytes, copied {copied_bytes} bytes",
                "copied_bytes": copied_bytes,
            }
        xattr_warning = copy_xattrs(source, temp)
        copy_basic_metadata(source, temp)
        os.replace(temp, dest)
        fsync_directory(dest.parent)
        result: dict[str, object] = {"status": "copied", "copied_bytes": copied_bytes,
                                     "source_mtime_ns": source_info.st_mtime_ns, "sha256": digest.hexdigest()}
        if xattr_warning:
            result["warning"] = xattr_warning
        return result
    except BaseException as exc:
        cleanup_path(temp)
        reason = compressed_read_failure(source, source_info, exc) if reading_source else None
        return {"status": UNREADABLE_COMPRESSED if reason else "failed",
                "error": reason or f"{type(exc).__name__}: {exc}", "copied_bytes": copied_bytes}


def compressed_read_failure(source: Path, info, exc: BaseException) -> str | None:
    # Inspect only after a real ENOTSUP read failure, inside the timed worker.
    # Normal listxattr hides compression metadata; use XATTR_SHOWCOMPRESSION.
    if not isinstance(exc, OSError) or exc.errno != errno.ENOTSUP:
        return None
    if not info or not getattr(info, "st_flags", 0) & UF_COMPRESSED:
        return None
    try:
        header = MacOSXattrOps().get(source, "com.apple.decmpfs", show_compression=True)
        if header:
            return None
        detail = "empty com.apple.decmpfs"
    except (OSError, AttributeError) as metadata_error:
        detail = f"missing or unreadable com.apple.decmpfs ({metadata_error})"
    return ("UF_COMPRESSED content is not addressable on the mounted source (ENOTSUP); "
            f"{detail}. This is not an I/O-sector diagnosis; use an independently verified duplicate if available.")


def make_temp_path(dest: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".rescue.", suffix=RESCUE_TMP_SUFFIX, dir=dest.parent)
    os.close(fd)
    return Path(name)


def cleanup_stale_temps(job_dir: Path, phase: str, dest_root: Path) -> None:
    # A suffix is not proof of ownership. Delete only files registered by this
    # job, with the same inode, and never a path belonging to a manifest row.
    conn = connect(job_dir)
    try:
        protected = {row["relative_path"] for row in conn.execute("select relative_path from files")}
        for row in conn.execute("select * from temporary_files").fetchall():
            path = Path(row["path"])
            try:
                rel = str(path.relative_to(dest_root))
                safe = resolve_relative_path(dest_root, rel, kind="file")
                info = safe.lstat()
                if rel not in protected and info.st_dev == row["device"] and info.st_ino == row["inode"]:
                    cleanup_path(safe)
                    if safe.exists():
                        continue
            except FileNotFoundError:
                pass
            except OSError:
                continue
            except ValueError:
                pass
            conn.execute("delete from temporary_files where path = ?", (row["path"],))
        conn.commit()
    finally:
        conn.close()


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


class MacOSXattrOps:
    def __init__(self) -> None:
        libc = ctypes.CDLL("libSystem.dylib", use_errno=True)
        self._listxattr = libc.listxattr
        self._listxattr.argtypes = (
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        )
        self._listxattr.restype = ctypes.c_ssize_t
        self._getxattr = libc.getxattr
        self._getxattr.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        )
        self._getxattr.restype = ctypes.c_ssize_t
        self._setxattr = libc.setxattr
        self._setxattr.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        )
        self._setxattr.restype = ctypes.c_int

    def list(self, path: Path) -> tuple[str, ...]:
        path_bytes = os.fsencode(path)
        size = self._listxattr(path_bytes, None, 0, XATTR_NOFOLLOW)
        if size < 0:
            raise_os_error(path)
        if size == 0:
            return ()
        buffer = ctypes.create_string_buffer(size)
        read = self._listxattr(path_bytes, buffer, size, XATTR_NOFOLLOW)
        if read < 0:
            raise_os_error(path)
        return tuple(
            name.decode(errors="replace")
            for name in buffer.raw[:read].split(b"\0")
            if name
        )

    def get(self, path: Path, name: str, *, show_compression: bool = False) -> bytes:
        path_bytes = os.fsencode(path)
        name_bytes = name.encode()
        options = XATTR_NOFOLLOW | (XATTR_SHOWCOMPRESSION if show_compression else 0)
        size = self._getxattr(path_bytes, name_bytes, None, 0, 0, options)
        if size < 0:
            raise_os_error(path)
        if size == 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = self._getxattr(path_bytes, name_bytes, buffer, size, 0, options)
        if read < 0:
            raise_os_error(path)
        return buffer.raw[:read]

    def set(self, path: Path, name: str, value: bytes) -> None:
        value_buffer = ctypes.create_string_buffer(value, len(value)) if value else None
        rc = self._setxattr(
            os.fsencode(path),
            name.encode(),
            value_buffer,
            len(value),
            0,
            XATTR_NOFOLLOW,
        )
        if rc != 0:
            raise_os_error(path)


def copy_xattrs(source: Path, dest: Path, ops=None) -> str | None:
    try:
        info = source.stat(follow_symlinks=False)
    except OSError:
        return f"{XATTR_WARNING_PREFIX}: failed to inspect source file"
    if not stat.S_ISREG(info.st_mode):
        return None
    if ops is None:
        try:
            ops = MacOSXattrOps()
        except (AttributeError, OSError):
            return None
    try:
        names = ops.list(source)
    except OSError:
        return f"{XATTR_WARNING_PREFIX}: failed to list source xattrs"
    failures: list[str] = []
    for name in names:
        if name in XATTR_SKIP_NAMES:
            continue
        try:
            ops.set(dest, name, ops.get(source, name))
        except OSError:
            failures.append(name)
    if failures:
        return f"{XATTR_WARNING_PREFIX}: failed " + ", ".join(failures)
    return None


def raise_os_error(path: Path) -> None:
    errno = ctypes.get_errno()
    raise OSError(errno, os.strerror(errno), str(path))


def combine_warnings(existing: object, new: object) -> str | None:
    existing_text = str(existing) if existing else None
    new_text = str(new) if new else None
    if existing_text and new_text:
        return f"{existing_text}; {new_text}"
    return existing_text or new_text


def without_copy_xattr_warnings(warning: object) -> str | None:
    if not warning:
        return None
    parts = [
        part
        for part in str(warning).split("; ")
        if not part.startswith(XATTR_WARNING_PREFIX)
    ]
    return "; ".join(parts) or None


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
