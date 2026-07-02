from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import RescueError
from .manifest import connect, is_same_or_inside, manifest_path
from .reporting import format_size


WORK_STATUSES = ("pending", "copying", "failed", "timed_out")
READ_PROBE_LIMIT = 1000


@dataclass(frozen=True)
class CheckResult:
    level: str
    name: str
    detail: str

    def as_line(self) -> str:
        return f"{self.level} {self.name} {self.detail}"


@dataclass(frozen=True)
class PreflightSummary:
    checks: list[CheckResult]

    @property
    def failed(self) -> bool:
        return any(check.level == "fail" for check in self.checks)

    def as_lines(self) -> list[str]:
        warnings = sum(1 for check in self.checks if check.level == "warn")
        failures = sum(1 for check in self.checks if check.level == "fail")
        verdict = "fail" if self.failed else "ok"
        summary = f"preflight={verdict} checks={len(self.checks)} warnings={warnings} failures={failures}"
        return [check.as_line() for check in self.checks] + [summary]


def preflight_job(job_dir: Path, source: Path | None, dest: Path | None) -> PreflightSummary:
    has_manifest = manifest_path(job_dir).exists()
    pending_bytes: int | None = None
    if has_manifest:
        if source is not None or dest is not None:
            raise RescueError("job already initialized; omit --source and --dest")
        source, dest = job_config_paths(job_dir)
        pending_bytes = manifest_pending_bytes(job_dir)
    elif source is None or dest is None:
        raise RescueError("preflight before init requires --source and --dest")
    return PreflightSummary(checks=run_checks(job_dir, source, dest, pending_bytes))


def job_config_paths(job_dir: Path) -> tuple[Path, Path]:
    conn = connect(job_dir)
    try:
        values = {row["key"]: row["value"] for row in conn.execute("select key, value from config")}
    finally:
        conn.close()
    missing = {"source", "dest"} - values.keys()
    if missing:
        raise RescueError(f"manifest config missing: {', '.join(sorted(missing))}")
    return Path(values["source"]), Path(values["dest"])


def manifest_pending_bytes(job_dir: Path) -> int:
    conn = connect(job_dir)
    try:
        placeholders = ", ".join("?" for _ in WORK_STATUSES)
        row = conn.execute(
            f"select coalesce(sum(size), 0) from files where status in ({placeholders})",
            WORK_STATUSES,
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def run_checks(
    job_dir: Path,
    source: Path,
    dest: Path,
    pending_bytes: int | None,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    source_resolved = source.resolve(strict=False)
    job_resolved = job_dir.resolve(strict=False)
    dest_resolved = dest.resolve(strict=False)

    source_is_dir = source.is_dir()
    if source_is_dir:
        checks.append(CheckResult("ok", "source-exists", str(source)))
    else:
        checks.append(CheckResult("fail", "source-exists", f"not an existing directory: {source}"))

    if os.path.realpath(source) == "/":
        checks.append(CheckResult("fail", "source-not-root", "source resolves to /"))
    else:
        checks.append(CheckResult("ok", "source-not-root", str(source_resolved)))

    checks.append(source_readable_check(source, source_is_dir))

    containment_ok: dict[str, bool] = {}
    for name, candidate in (("job-dir", job_resolved), ("dest", dest_resolved)):
        inside = is_same_or_inside(candidate, source_resolved)
        containment_ok[name] = not inside
        if inside:
            checks.append(CheckResult("fail", f"{name}-outside-source", f"{candidate} is the source or inside it"))
        else:
            checks.append(CheckResult("ok", f"{name}-outside-source", str(candidate)))

    if source_is_dir:
        checks.append(source_mount_check(source))

    for name, candidate in (("job", job_resolved), ("dest", dest_resolved)):
        if not containment_ok[f"job-dir" if name == "job" else "dest"]:
            checks.append(
                CheckResult("fail", f"{name}-writable", "skipped: containment check failed, refusing to probe")
            )
            continue
        checks.append(write_probe_check(name, candidate, source_resolved))

    checks.append(free_space_check(dest_resolved, pending_bytes))
    return checks


def source_readable_check(source: Path, source_is_dir: bool) -> CheckResult:
    if not source_is_dir:
        return CheckResult("fail", "source-readable", "skipped: source is not a directory")
    try:
        count = 0
        with os.scandir(source) as entries:
            for _entry in entries:
                count += 1
                if count >= READ_PROBE_LIMIT:
                    break
    except PermissionError:
        return CheckResult(
            "fail",
            "source-readable",
            "permission denied; grant the terminal app Full Disk Access",
        )
    except OSError as exc:
        return CheckResult("fail", "source-readable", f"{type(exc).__name__}: {exc}")
    suffix = "+" if count >= READ_PROBE_LIMIT else ""
    return CheckResult("ok", "source-readable", f"{count}{suffix} top-level entries")


def source_mount_check(source: Path) -> CheckResult:
    mount = find_mount_point(source)
    try:
        read_only = bool(os.statvfs(mount).f_flag & os.ST_RDONLY)
    except OSError as exc:
        return CheckResult("warn", "source-mount", f"cannot statvfs {mount}: {exc}")
    if read_only:
        return CheckResult("ok", "source-mount", f"{mount} (read-only)")
    return CheckResult(
        "warn",
        "source-mount",
        f"{mount} is writable; prefer a read-only source mount and treat the source as read-only",
    )


def find_mount_point(path: Path) -> str:
    current = os.path.realpath(path)
    while not os.path.ismount(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def write_probe_check(name: str, target: Path, source_resolved: Path) -> CheckResult:
    probe_dir = nearest_existing_ancestor(target)
    if is_same_or_inside(probe_dir, source_resolved):
        return CheckResult("fail", f"{name}-writable", "skipped: probe directory is inside source")
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".preflight-probe.", dir=probe_dir)
    except OSError as exc:
        return CheckResult("fail", f"{name}-writable", f"cannot write in {probe_dir}: {exc}")
    os.close(fd)
    try:
        os.unlink(temp_name)
    except OSError:
        pass
    return CheckResult("ok", f"{name}-writable", f"write probe succeeded in {probe_dir}")


def nearest_existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def free_space_check(dest_resolved: Path, pending_bytes: int | None) -> CheckResult:
    probe = nearest_existing_ancestor(dest_resolved)
    try:
        stats = os.statvfs(probe)
    except OSError as exc:
        return CheckResult("fail", "free-space", f"cannot statvfs {probe}: {exc}")
    free = stats.f_bavail * stats.f_frsize
    if pending_bytes is None:
        return CheckResult("ok", "free-space", f"{format_size(free)} free on dest volume")
    if free < pending_bytes:
        return CheckResult(
            "fail",
            "free-space",
            f"{format_size(free)} free but manifest still needs {format_size(pending_bytes)}",
        )
    return CheckResult(
        "ok",
        "free-space",
        f"{format_size(free)} free; manifest still needs {format_size(pending_bytes)}",
    )
