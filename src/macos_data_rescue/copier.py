from __future__ import annotations

import ctypes
import multiprocessing
import os
import queue
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .manifest import iter_selected_files, load_config, mark_copying, mark_result, migrate_manifest


CHUNK_SIZE = 1024 * 1024
RESCUE_TMP_SUFFIX = ".rescue-tmp"
WORK_STATUSES = ("pending", "copying", "failed", "timed_out")
DONE_STATUSES = ("copied", "skipped")
XATTR_NOFOLLOW = 0x0001
XATTR_SKIP_NAMES = frozenset({"com.apple.quarantine", "com.apple.macl"})
XATTR_WARNING_PREFIX = "extended attributes not fully preserved"


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
        try:
            dest = resolve_relative_path(config.dest, row["relative_path"], kind=row["kind"])
        except ValueError:
            if limit is not None and attempted >= limit:
                break
            process_row(job_dir, config.source, config.dest, row, timeout, summary)
            handled_ids.add(int(row["id"]))
            attempted += 1
            continue
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
    scan_warning = without_copy_xattr_warnings(row["warning"])
    summary.processed += 1
    mark_copying(job_dir, row["id"])
    try:
        dest = resolve_relative_path(dest_root, row["relative_path"], kind=row["kind"])
        source = resolve_row_source(source_root, row)
    except ValueError as exc:
        mark_result(
            job_dir,
            row["id"],
            "failed",
            error=f"ValueError: {exc}",
            warning=scan_warning,
        )
        summary.failed += 1
        return

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
        )
        summary.skipped += 1
        return

    result = copy_one_with_timeout(source, dest, timeout, expected_size=int(row["size"]))
    status = str(result["status"])
    if status == "copied":
        copied_bytes = int(result.get("copied_bytes", 0))
        mark_result(
            job_dir,
            row["id"],
            "copied",
            copied_bytes=copied_bytes,
            warning=combine_warnings(scan_warning, result.get("warning")),
        )
        summary.copied += 1
    elif status == "timed_out":
        mark_result(job_dir, row["id"], "timed_out", error=str(result["error"]), warning=scan_warning)
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
        )
        summary.failed += 1


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
        info = dest.lstat() if row["kind"] == "symlink" else dest.stat()
    except OSError:
        return False
    return info.st_size == row["size"] and info.st_mtime_ns == row["mtime_ns"]


def copy_one_with_timeout(source: Path, dest: Path, timeout: float, *, expected_size: int) -> dict[str, object]:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = make_temp_path(dest)
    except OSError as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "copied_bytes": 0}
    process = ctx.Process(
        target=_copy_file_child,
        args=(str(source), str(dest), str(temp), expected_size, result_queue),
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
    expected_size: int,
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
        if copied_bytes != expected_size:
            cleanup_path(temp)
            result_queue.put(
                {
                    "status": "failed",
                    "error": f"incomplete copy: expected {expected_size} bytes, copied {copied_bytes} bytes",
                    "copied_bytes": copied_bytes,
                }
            )
            return
        xattr_warning = copy_xattrs(source, temp)
        copy_basic_metadata(source, temp)
        os.replace(temp, dest)
        fsync_directory(dest.parent)
        result = {"status": "copied", "copied_bytes": copied_bytes}
        if xattr_warning:
            result["warning"] = xattr_warning
        result_queue.put(result)
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
        try:
            dest = resolve_relative_path(dest_root, row["relative_path"], kind=row["kind"])
        except ValueError:
            continue
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

    def get(self, path: Path, name: str) -> bytes:
        path_bytes = os.fsencode(path)
        name_bytes = name.encode()
        size = self._getxattr(path_bytes, name_bytes, None, 0, 0, XATTR_NOFOLLOW)
        if size < 0:
            raise_os_error(path)
        if size == 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = self._getxattr(path_bytes, name_bytes, buffer, size, 0, XATTR_NOFOLLOW)
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
