from __future__ import annotations

import fnmatch
import ctypes
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import RescueError
from .locking import exclusive_job
from .manifest import (
    GATED_PHASES,
    connect,
    scan_issues,
    approval_required_message,
    begin_scan,
    load_approval,
    load_config,
    load_scan_cursor,
    mark_scan_complete,
    migrate_manifest,
    scan_cursor_key,
    upsert_scanned_files,
    validate_application_layout,
)


IMPORTANT_DIRS = {"Desktop", "Documents", "Downloads"}
PHOTO_DIRS = {"Pictures", "Movies", "Music"}
CUSTOMER_PHASES = ("visible-home", "hidden-home", "app-data", "applications", "full-home")
LEGACY_PHASES = ("important", "photos", "library", "all")
SCAN_PHASES = CUSTOMER_PHASES + LEGACY_PHASES
APP_DATA_LIBRARY_PREFIXES = (
    ("Library", "Application Support"),
    ("Library", "Calendars"),
    ("Library", "Containers", "com.apple.Notes"),
    ("Library", "Group Containers", "group.com.apple.notes"),
    ("Library", "Keychains"),
    ("Library", "Mail"),
    ("Library", "Messages"),
    ("Library", "MobileSync", "Backup"),
    ("Library", "Safari"),
)
CUSTOMER_HOME_TOP_LEVEL_EXCLUDES = {
    "Cache",
    "Caches",
    "Temp",
    ".cache",
    ".npm",
    ".pnpm-store",
    ".Trash",
    ".yarn",
    "Logs",
    "tmp",
}
VISIBLE_HOME_TOP_LEVEL_EXCLUDES = CUSTOMER_HOME_TOP_LEVEL_EXCLUDES | {
    ".Spotlight-V100",
    ".fseventsd",
    "Applications",
    "Library",
}
HIDDEN_HOME_TOP_LEVEL_EXCLUDES = {
    ".Trash",
    ".cache",
    ".npm",
    ".pnpm-store",
    ".yarn",
    ".gradle",
    ".Spotlight-V100",
    ".fseventsd",
}
HIDDEN_HOME_EXCLUDE_PARTS = {
    "node_modules",
    "__pycache__",
    "Cache",
    "Caches",
    "cache",
    "caches",
    "tmp",
    "Temp",
}
TOP_LEVEL_EXCLUDES = {".Trash", ".Spotlight-V100", ".fseventsd", "node_modules"}
EXCLUDED_PARTS = {"node_modules", "__pycache__"}
LIBRARY_EXCLUDE_PREFIXES = (
    ("Library", "Caches"),
    ("Library", "Logs"),
    ("Library", "Containers", "com.apple.Safari", "Data", "Library", "Caches"),
    ("Library", "Application Support", "Google", "Chrome", "Default", "Cache"),
)
LIBRARY_EXCLUDE_PARTS = {"Cache", "Caches", "tmp", "Temp"}
ICLOUD_PATH_MARKERS = (
    "Mobile Documents",
    "CloudDocs",
    "iCloud Drive",
    "File Provider Storage",
    "FileProvider",
)
ICLOUD_XATTR_MARKERS = ("icloud", "ubiquity", "clouddocs", "fileprovider", "dataless")
ICLOUD_TINY_FILE_BYTES = 4096
SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)
ICLOUD_PLACEHOLDER_WARNING = (
    "suspected iCloud dataless placeholder: data may not have been physically "
    "present on disk"
)
DEFAULT_SCAN_BATCH_SIZE = 100


@dataclass(frozen=True)
class ScanSummary:
    scanned: int
    stopped: str | None = None
    issues: int = 0

    def as_line(self) -> str:
        line = f"scanned={self.scanned}"
        if self.stopped:
            line += f" stopped={self.stopped}"
        return line + (f" scan_issues={self.issues}" if self.issues else "")


@exclusive_job
def scan_job(
    job_dir: Path,
    *,
    phase: str = "all",
    limit: int | None = None,
    timeout: float | None = None,
    batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
    io_timeout: float = 30,
) -> ScanSummary:
    config = load_config(job_dir)
    scan_phase = resolve_scan_phase(config.profile, phase)
    if config.profile == "customer-home" and scan_phase in GATED_PHASES:
        if load_approval(job_dir, scan_phase) is None:
            raise RescueError(approval_required_message(job_dir, [scan_phase]))
    if config.profile == "customer-home" and scan_phase == "all":
        # the legacy default scan walks the whole home including ~/Library,
        # so it needs the library approval like the explicit library phase
        if load_approval(job_dir, "library") is None:
            raise RescueError(
                "the default scan includes ~/Library; "
                + approval_required_message(job_dir, ["library"])
            )
    if not config.source.exists():
        raise RescueError(f"source does not exist: {config.source}")
    if scan_phase == "applications":
        validate_application_layout(config)
    migrate_manifest(job_dir)
    conn = connect(job_dir)
    try:
        if conn.execute("select value from config where key = 'scan_engine'").fetchone() is None:
            # The traversal order changed. Revisit old partial scans, preserving rows/statuses.
            conn.execute("delete from config where key like 'scan_cursor:%'")
            conn.execute("insert into config values('scan_engine', 'isolated-v1')")
            conn.commit()
    finally:
        conn.close()
    limiter = ScanLimiter(limit=limit, timeout=timeout)
    previous_issues = tuple(issue["path"] for issue in scan_issues(job_dir, scan_phase))
    cursor = None if previous_issues else (load_scan_cursor(job_dir, scan_phase) or None)
    begin_scan(job_dir, scan_phase)
    count = upsert_scanned_files(
        job_dir,
        limiter.wrap(iter_source_files(config.source, phase=scan_phase, excludes=config.excludes,
                                                 io_timeout=io_timeout, deadline=limiter.deadline, previous_issues=previous_issues), skip_until_after=cursor),
        batch_size=batch_size,
        cursor_key=scan_cursor_key(scan_phase),
    )
    if cursor is not None and not limiter.found_cursor and limiter.stopped is None:
        limiter = ScanLimiter(limit=limit, deadline=limiter.deadline)
        count = upsert_scanned_files(
            job_dir,
            limiter.wrap(iter_source_files(config.source, phase=scan_phase, excludes=config.excludes,
                                                 io_timeout=io_timeout, deadline=limiter.deadline, previous_issues=previous_issues)),
            batch_size=batch_size,
            cursor_key=scan_cursor_key(scan_phase),
        )
    issues = len(scan_issues(job_dir, scan_phase))
    if limiter.stopped is None and not issues:
        mark_scan_complete(job_dir, scan_phase)
    return ScanSummary(scanned=count, stopped=limiter.stopped, issues=issues)


def resolve_scan_phase(profile: str, phase: str) -> str:
    if profile in {"restore", "volume"}:
        if phase != "all":
            raise RescueError(f"{profile} profile scans the whole source tree; omit --phase")
        return profile
    if profile != "customer-home":
        raise RescueError(f"unsupported profile: {profile}")
    if phase == "restore":
        raise RescueError("phase restore requires a restore profile job")
    if phase not in SCAN_PHASES:
        raise RescueError(f"unsupported scan phase: {phase}")
    return phase


class ScanLimiter:
    def __init__(
        self,
        *,
        limit: int | None,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self.limit = limit
        if deadline is not None:
            self.deadline = deadline
        elif timeout is not None:
            self.deadline = time.monotonic() + timeout
        else:
            self.deadline = None
        self.scanned = 0
        self.stopped: str | None = None
        self.found_cursor = True

    def wrap(self, files, *, skip_until_after: str | None = None):
        from .scan_worker import ScanIssue, ScanStopped
        iterator = iter(files)
        self.found_cursor = skip_until_after is None
        try:
            while True:
                if self.limit is not None and self.scanned >= self.limit:
                    self.stopped = "limit"
                    return
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.stopped = "timeout"
                    return
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                if isinstance(item, ScanStopped):
                    self.stopped = item.reason
                    return
                if isinstance(item, ScanIssue):
                    yield item
                    continue
                if not self.found_cursor:
                    if item.relative_path == skip_until_after:
                        self.found_cursor = True
                    continue
                yield item
                self.scanned += 1
        finally:
            if hasattr(iterator, "close"):
                iterator.close()


def iter_source_files(source: Path, *, phase="all", excludes=(), io_timeout=30, deadline=None, previous_issues=()):
    from .scan_worker import isolated_files
    yield from isolated_files(source, phase=phase, excludes=excludes, io_timeout=io_timeout, deadline=deadline,
                              previous_issues=previous_issues)


def volume_root_for_home(source: Path) -> Path:
    parts = source.parts
    users_indexes = [index for index, part in enumerate(parts) if part == "Users"]
    if users_indexes:
        users_index = users_indexes[-1]
        if users_index > 0 and users_index + 1 < len(parts):
            return Path(*parts[:users_index])
    return source.parent


def matches_exclude(parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    # Match the full path and each parent: excluding a directory prunes its tree.
    return any(fnmatch.fnmatchcase("/".join(parts[:end]), pattern)
               for end in range(1, len(parts) + 1) for pattern in patterns)


def is_excluded(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    if parts[0] in TOP_LEVEL_EXCLUDES:
        return True
    if any(part in EXCLUDED_PARTS for part in parts):
        return True
    for prefix in LIBRARY_EXCLUDE_PREFIXES:
        if parts[: len(prefix)] == prefix:
            return True
    if parts[0] == "Library" and any(part in LIBRARY_EXCLUDE_PARTS for part in parts[1:]):
        return True
    return False


def should_descend(parts: tuple[str, ...], phase: str) -> bool:
    if phase in {"restore", "volume"}:
        return True
    if is_excluded(parts):
        return False
    if phase in CUSTOMER_PHASES and is_customer_home_ballast(parts):
        return False
    if phase == "app-data":
        return path_could_match_prefix(parts, APP_DATA_LIBRARY_PREFIXES)
    if phase == "visible-home":
        return is_visible_home_path(parts)
    if phase == "hidden-home":
        return is_hidden_home_path(parts)
    if phase == "full-home":
        return True
    return True


def should_include_file(parts: tuple[str, ...], phase: str) -> bool:
    if phase in {"restore", "volume"}:
        return True
    if is_excluded(parts):
        return False
    if phase in CUSTOMER_PHASES and is_customer_home_ballast(parts):
        return False
    if phase == "app-data":
        return is_app_data_path(parts)
    if phase == "visible-home":
        return is_visible_home_path(parts)
    if phase == "hidden-home":
        return is_hidden_home_path(parts)
    if phase == "full-home":
        return True
    return True


def is_visible_home_path(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    first = parts[0]
    return not first.startswith(".") and first not in VISIBLE_HOME_TOP_LEVEL_EXCLUDES


def is_customer_home_ballast(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    first = parts[0]
    return first in CUSTOMER_HOME_TOP_LEVEL_EXCLUDES


def is_hidden_home_path(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    first = parts[0]
    if not first.startswith(".") or first in HIDDEN_HOME_TOP_LEVEL_EXCLUDES:
        return False
    return not any(part in HIDDEN_HOME_EXCLUDE_PARTS for part in parts)


def is_app_data_path(parts: tuple[str, ...]) -> bool:
    if is_excluded(parts):
        return False
    return any(parts[: len(prefix)] == prefix for prefix in APP_DATA_LIBRARY_PREFIXES)


def path_could_match_prefix(
    parts: tuple[str, ...],
    prefixes: tuple[tuple[str, ...], ...],
) -> bool:
    return any(
        parts == prefix[: len(parts)] or parts[: len(prefix)] == prefix
        for prefix in prefixes
    )


def manifest_phase_for(parts: tuple[str, ...], requested_phase: str) -> str:
    if requested_phase in {"restore", "volume"}:
        return requested_phase
    if requested_phase in CUSTOMER_PHASES:
        return requested_phase
    return phase_for(parts)


def phase_for(parts: tuple[str, ...]) -> str:
    if not parts:
        return "all"
    if parts[0] in IMPORTANT_DIRS:
        return "important"
    if parts[0] in PHOTO_DIRS:
        return "photos"
    if parts[0] == "Library":
        return "library"
    return "all"


def file_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    return "other"


def warning_for(path: Path, rel_parts: tuple[str, ...], info: os.stat_result) -> str | None:
    if suspected_icloud_placeholder(path, rel_parts, info):
        return ICLOUD_PLACEHOLDER_WARNING
    return None


def suspected_icloud_placeholder(
    path: Path,
    rel_parts: tuple[str, ...],
    info: os.stat_result,
) -> bool:
    if not stat.S_ISREG(info.st_mode):
        return False
    if has_sf_dataless_flag(info):
        return True
    xattr_names = list_xattr_names(path)
    if any(has_icloud_marker(name) for name in xattr_names):
        return True
    rel_text = "/".join(rel_parts)
    if has_icloud_marker(rel_text) and info.st_size <= ICLOUD_TINY_FILE_BYTES:
        return True
    return False


def has_icloud_marker(value: str) -> bool:
    lowered = value.lower()
    return any(marker.lower() in lowered for marker in ICLOUD_PATH_MARKERS + ICLOUD_XATTR_MARKERS)


def has_sf_dataless_flag(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_flags", 0) & SF_DATALESS)


def list_xattr_names(path: Path) -> tuple[str, ...]:
    if hasattr(os, "listxattr"):
        try:
            return tuple(os.listxattr(path))
        except OSError:
            return ()
    return list_xattr_names_libsystem(path)


def list_xattr_names_libsystem(path: Path) -> tuple[str, ...]:
    try:
        libc = ctypes.CDLL("libSystem.dylib", use_errno=True)
    except OSError:
        return ()
    try:
        listxattr = libc.listxattr
        listxattr.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
        listxattr.restype = ctypes.c_ssize_t
        path_bytes = os.fsencode(path)
        size = listxattr(path_bytes, None, 0, 0)
        if size <= 0:
            return ()
        buffer = ctypes.create_string_buffer(size)
        read = listxattr(path_bytes, buffer, size, 0)
        if read <= 0:
            return ()
        return tuple(
            name.decode(errors="replace")
            for name in buffer.raw[:read].split(b"\0")
            if name
        )
    except (AttributeError, OSError):
        return ()
