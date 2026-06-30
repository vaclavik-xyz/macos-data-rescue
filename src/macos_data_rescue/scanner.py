from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path

from .errors import RescueError
from .manifest import ScannedFile, load_config, migrate_manifest, upsert_scanned_files


IMPORTANT_DIRS = {"Desktop", "Documents", "Downloads"}
PHOTO_DIRS = {"Pictures", "Movies", "Music"}
CUSTOMER_PHASES = ("visible-home", "hidden-home", "app-data", "full-home")
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


def scan_job(job_dir: Path, *, phase: str = "all") -> int:
    if phase not in SCAN_PHASES:
        raise RescueError(f"unsupported scan phase: {phase}")
    config = load_config(job_dir)
    if config.profile != "customer-home":
        raise RescueError(f"unsupported profile: {config.profile}")
    if not config.source.exists():
        raise RescueError(f"source does not exist: {config.source}")
    migrate_manifest(job_dir)
    return upsert_scanned_files(job_dir, iter_source_files(config.source, phase=phase))


def iter_source_files(source: Path, *, phase: str = "all"):
    for scan_root in scan_roots(source, phase):
        yield from iter_tree(source, scan_root, phase)


def scan_roots(source: Path, phase: str) -> tuple[Path, ...]:
    if phase in {"all", "visible-home", "hidden-home", "full-home"}:
        return (source,)
    if phase == "app-data":
        library = source / "Library"
        return (library,) if is_real_directory(library) else ()
    if phase == "important":
        names = IMPORTANT_DIRS
    elif phase == "photos":
        names = PHOTO_DIRS
    elif phase == "library":
        names = {"Library"}
    else:
        raise RescueError(f"unsupported scan phase: {phase}")

    roots = []
    for name in sorted(names):
        root = source / name
        try:
            info = root.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            roots.append(root)
    return tuple(roots)


def iter_tree(source: Path, scan_root: Path, phase: str):
    for root, dirs, files in os.walk(scan_root, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs.sort()
        files.sort()
        kept_dirs = []
        for dirname in dirs:
            rel_parts = relative_parts(source, root_path / dirname)
            if should_descend(rel_parts, phase):
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for filename in files:
            path = root_path / filename
            rel_parts = relative_parts(source, path)
            if not should_include_file(rel_parts, phase):
                continue
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            yield ScannedFile(
                relative_path="/".join(rel_parts),
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                mode=stat.S_IMODE(info.st_mode),
                kind=file_kind(info.st_mode),
                phase=manifest_phase_for(rel_parts, phase),
                warning=warning_for(path, rel_parts, info),
            )


def relative_parts(source: Path, path: Path) -> tuple[str, ...]:
    return path.relative_to(source).parts


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


def is_real_directory(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def manifest_phase_for(parts: tuple[str, ...], requested_phase: str) -> str:
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
