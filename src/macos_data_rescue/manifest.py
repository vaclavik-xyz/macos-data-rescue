from __future__ import annotations

import os
import shlex
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from .errors import RescueError


MANIFEST_NAME = "manifest.sqlite"
WARNING_UNCHANGED = object()
GATED_PHASES = ("app-data", "applications", "full-home", "library")


@dataclass(frozen=True)
class JobConfig:
    job_dir: Path
    source: Path
    dest: Path
    profile: str


@dataclass(frozen=True)
class ScannedFile:
    relative_path: str
    size: int
    mtime_ns: int
    mode: int
    kind: str
    phase: str
    source_path: str | None = None
    warning: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def manifest_path(job_dir: Path) -> Path:
    return job_dir / MANIFEST_NAME


def connect(job_dir: Path) -> sqlite3.Connection:
    db_path = manifest_path(job_dir)
    if not db_path.exists():
        raise RescueError(f"manifest not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate_manifest(job_dir: Path) -> None:
    conn = connect(job_dir)
    try:
        create_schema(conn)
        backfill_application_source_paths(conn)
        conn.commit()
    finally:
        conn.close()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists config (
            key text primary key,
            value text not null
        );

        create table if not exists files (
            id integer primary key,
            relative_path text not null unique,
            source_path text,
            size integer not null,
            mtime_ns integer not null,
            mode integer not null,
            kind text not null,
            phase text not null,
            status text not null default 'pending',
            attempts integer not null default 0,
            error text,
            warning text,
            copied_bytes integer not null default 0,
            scanned_at text not null,
            started_at text,
            finished_at text,
            updated_at text not null
        );

        create index if not exists idx_files_phase_status on files(phase, status);
        create index if not exists idx_files_status on files(status);
        """
    )
    ensure_column(conn, "files", "source_path", "text")
    ensure_column(conn, "files", "warning", "text")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def backfill_application_source_paths(conn: sqlite3.Connection) -> None:
    config = {row["key"]: row["value"] for row in conn.execute("select key, value from config")}
    source_text = config.get("source")
    if not source_text:
        return
    source = Path(source_text)
    rows = conn.execute(
        """
        select id, relative_path
        from files
        where phase = 'applications'
          and (source_path is null or source_path = '')
        """
    ).fetchall()
    for row in rows:
        source_path = application_source_path_for_relative(source, row["relative_path"])
        if source_path is None:
            continue
        conn.execute(
            "update files set source_path = ? where id = ?",
            (str(source_path), row["id"]),
        )


def application_source_path_for_relative(source: Path, relative_path: str) -> Path | None:
    rel = PurePosixPath(relative_path)
    if rel.is_absolute():
        return None
    parts = rel.parts
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    if parts[0] == "Volume Applications":
        return volume_root_for_home(source).joinpath("Applications", *parts[1:])
    if parts[0] == "Home Applications":
        return source.joinpath("Applications", *parts[1:])
    return None


def volume_root_for_home(source: Path) -> Path:
    parts = source.parts
    users_indexes = [index for index, part in enumerate(parts) if part == "Users"]
    if users_indexes:
        users_index = users_indexes[-1]
        if users_index > 0 and users_index + 1 < len(parts):
            return Path(*parts[:users_index])
    return source.parent


def init_manifest(job_dir: Path, source: Path, dest: Path, profile: str) -> JobConfig:
    if not source.exists() or not source.is_dir():
        raise RescueError(f"source must be an existing directory: {source}")
    source_resolved = source.resolve()
    job_resolved = job_dir.resolve(strict=False)
    dest_resolved = dest.resolve(strict=False)
    for label, candidate in (("job-dir", job_resolved), ("dest", dest_resolved)):
        if is_same_or_inside(candidate, source_resolved):
            raise RescueError(f"{label} must not be inside source: {candidate}")
    job_dir.mkdir(parents=True, exist_ok=True)
    db_path = manifest_path(job_dir)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        create_schema(conn)
        now = utc_now()
        values = {
            "source": str(source_resolved),
            "dest": str(dest_resolved),
            "profile": profile,
            "created_at": now,
            "updated_at": now,
        }
        conn.executemany(
            """
            insert into config(key, value) values(?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            values.items(),
        )
        conn.commit()
    finally:
        conn.close()
    return JobConfig(job_dir=job_dir, source=source_resolved, dest=dest_resolved, profile=profile)


def is_same_or_inside(candidate: Path, parent: Path) -> bool:
    if candidate == parent or parent in candidate.parents:
        return True
    return has_ancestor_with_same_inode(candidate, parent)


def has_ancestor_with_same_inode(candidate: Path, parent: Path) -> bool:
    """Inode-based containment for paths the string comparison cannot catch.

    macOS volumes are case-insensitive by default, so `/volumes/ssd/x` is the
    same directory as `/Volumes/SSD/x` while comparing unequal as strings.
    Missing ancestors (dest may not exist yet) are skipped.
    """
    try:
        parent_info = parent.stat()
    except OSError:
        return False
    for ancestor in (candidate, *candidate.parents):
        try:
            info = ancestor.stat()
        except OSError:
            continue
        if os.path.samestat(info, parent_info):
            return True
    return False


def load_config(job_dir: Path) -> JobConfig:
    conn = connect(job_dir)
    try:
        rows = conn.execute("select key, value from config").fetchall()
    finally:
        conn.close()
    values = {row["key"]: row["value"] for row in rows}
    missing = {"source", "dest", "profile"} - values.keys()
    if missing:
        raise RescueError(f"manifest config missing: {', '.join(sorted(missing))}")
    source = Path(values["source"]).resolve()
    dest = Path(values["dest"]).resolve(strict=False)
    job_resolved = job_dir.resolve(strict=False)
    for label, candidate in (("job-dir", job_resolved), ("dest", dest)):
        if is_same_or_inside(candidate, source):
            raise RescueError(f"{label} must not be inside source: {candidate}")
    return JobConfig(
        job_dir=job_dir,
        source=source,
        dest=dest,
        profile=values["profile"],
    )


def scan_cursor_key(phase: str) -> str:
    return f"scan_cursor:{phase}"


def load_scan_cursor(job_dir: Path, phase: str) -> str | None:
    conn = connect(job_dir)
    try:
        row = conn.execute(
            "select value from config where key = ?",
            (scan_cursor_key(phase),),
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def clear_scan_cursor(job_dir: Path, phase: str) -> None:
    conn = connect(job_dir)
    try:
        conn.execute("delete from config where key = ?", (scan_cursor_key(phase),))
        commit_scan_batch(conn)
    finally:
        conn.close()


def approval_key(phase: str) -> str:
    return f"approved:{phase}"


def load_approval(job_dir: Path, phase: str) -> str | None:
    conn = connect(job_dir)
    try:
        row = conn.execute(
            "select value from config where key = ?",
            (approval_key(phase),),
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def record_approval(job_dir: Path, phase: str, by: str | None = None) -> str:
    value = utc_now()
    if by:
        value += f" by={by}"
    conn = connect(job_dir)
    try:
        conn.execute(
            """
            insert into config(key, value) values(?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            (approval_key(phase), value),
        )
        conn.commit()
    finally:
        conn.close()
    return value


def unapproved_gated_phases(job_dir: Path, phase: str) -> list[str]:
    if phase in GATED_PHASES:
        candidates = [phase]
    elif phase == "all":
        conn = connect(job_dir)
        try:
            rows = conn.execute("select distinct phase from files").fetchall()
        finally:
            conn.close()
        present = {row["phase"] for row in rows}
        candidates = [gated for gated in GATED_PHASES if gated in present]
    else:
        return []
    return [gated for gated in candidates if load_approval(job_dir, gated) is None]


def approval_required_message(job_dir: Path, phases: list[str]) -> str:
    job_quoted = shlex.quote(str(job_dir))
    commands = "; ".join(
        f"macos-data-rescue approve --job-dir {job_quoted} --phase {phase}" for phase in phases
    )
    return f"approval required for phase {', '.join(phases)}; run: {commands}"


def scan_done_key(phase: str) -> str:
    return f"scan_done:{phase}"


def load_scan_done(job_dir: Path, phase: str) -> str | None:
    conn = connect(job_dir)
    try:
        row = conn.execute(
            "select value from config where key = ?",
            (scan_done_key(phase),),
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def mark_scan_complete(job_dir: Path, phase: str) -> None:
    conn = connect(job_dir)
    try:
        conn.execute(
            """
            insert into config(key, value) values(?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            (scan_done_key(phase), utc_now()),
        )
        commit_scan_batch(conn)
    finally:
        conn.close()


def upsert_scanned_files(
    job_dir: Path,
    files: Iterable[ScannedFile],
    *,
    batch_size: int = 100,
    cursor_key: str | None = None,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    conn = connect(job_dir)
    count = 0
    pending = 0
    batch_cursor: str | None = None
    try:
        for item in files:
            now = utc_now()
            existing = conn.execute(
                """
                select size, mtime_ns, status, error, copied_bytes, started_at, finished_at
                from files
                where relative_path = ?
                """,
                (item.relative_path,),
            ).fetchone()
            keep_status = bool(
                existing
                and existing["size"] == item.size
                and existing["mtime_ns"] == item.mtime_ns
            )
            status = existing["status"] if keep_status else "pending"
            error = existing["error"] if keep_status else None
            copied_bytes = existing["copied_bytes"] if keep_status else 0
            started_at = existing["started_at"] if keep_status else None
            finished_at = existing["finished_at"] if keep_status else None
            conn.execute(
                """
                insert into files(
                    relative_path, source_path, size, mtime_ns, mode, kind, phase, status,
                    attempts, error, warning, copied_bytes, scanned_at,
                    started_at, finished_at, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                on conflict(relative_path) do update set
                    source_path = excluded.source_path,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    mode = excluded.mode,
                    kind = excluded.kind,
                    phase = excluded.phase,
                    status = ?,
                    error = ?,
                    warning = excluded.warning,
                    copied_bytes = ?,
                    started_at = ?,
                    finished_at = ?,
                    scanned_at = excluded.scanned_at,
                    updated_at = excluded.updated_at
                """,
                (
                    item.relative_path,
                    item.source_path,
                    item.size,
                    item.mtime_ns,
                    item.mode,
                    item.kind,
                    item.phase,
                    status,
                    error,
                    item.warning,
                    copied_bytes,
                    now,
                    started_at,
                    finished_at,
                    now,
                    status,
                    error,
                    copied_bytes,
                    started_at,
                    finished_at,
                ),
            )
            count += 1
            pending += 1
            batch_cursor = item.relative_path
            if pending >= batch_size:
                commit_scan_batch(conn, cursor_key=cursor_key, cursor_value=batch_cursor)
                pending = 0
                batch_cursor = None
        if pending or count == 0:
            commit_scan_batch(conn, cursor_key=cursor_key, cursor_value=batch_cursor)
    finally:
        conn.close()
    return count


def commit_scan_batch(
    conn: sqlite3.Connection,
    *,
    cursor_key: str | None = None,
    cursor_value: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        insert into config(key, value) values('updated_at', ?)
        on conflict(key) do update set value = excluded.value
        """,
        (now,),
    )
    if cursor_key is not None and cursor_value is not None:
        conn.execute(
            """
            insert into config(key, value) values(?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            (cursor_key, cursor_value),
        )
    conn.commit()


def selected_files(job_dir: Path, phase: str, limit: int | None = None) -> list[sqlite3.Row]:
    return list(iter_selected_files(job_dir, phase, limit=limit))


def iter_selected_files(
    job_dir: Path,
    phase: str,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = None,
    batch_size: int = 100,
):
    yielded = 0
    last_path: str | None = None
    while limit is None or yielded < limit:
        chunk_limit = batch_size if limit is None else min(batch_size, limit - yielded)
        rows = fetch_selected_batch(
            job_dir,
            phase,
            statuses=statuses,
            last_path=last_path,
            limit=chunk_limit,
        )
        if not rows:
            break
        last_path = rows[-1]["relative_path"]
        yielded += len(rows)
        yield from rows


def fetch_selected_batch(
    job_dir: Path,
    phase: str,
    *,
    statuses: tuple[str, ...] | None,
    last_path: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    conn = connect(job_dir)
    try:
        where_parts: list[str] = []
        params: list[object] = []
        if phase != "all":
            where_parts.append("phase = ?")
            params.append(phase)
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            where_parts.append(f"status in ({placeholders})")
            params.extend(statuses)
        if last_path is not None:
            where_parts.append("relative_path > ?")
            params.append(last_path)
        where = f"where {' and '.join(where_parts)}" if where_parts else ""
        params.append(limit)
        sql = f"""
            select *
            from files
            {where}
            order by relative_path
            limit ?
        """
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def mark_copying(job_dir: Path, file_id: int, *, conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = connect(job_dir)
    now = utc_now()
    try:
        conn.execute(
            """
            update files
            set status = 'copying',
                attempts = attempts + 1,
                error = null,
                started_at = ?,
                finished_at = null,
                updated_at = ?
            where id = ?
            """,
            (now, now, file_id),
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def mark_result(
    job_dir: Path,
    file_id: int,
    status: str,
    *,
    error: str | None = None,
    warning=WARNING_UNCHANGED,
    copied_bytes: int = 0,
    conn: sqlite3.Connection | None = None,
) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = connect(job_dir)
    now = utc_now()
    try:
        if warning is WARNING_UNCHANGED:
            conn.execute(
                """
                update files
                set status = ?,
                    error = ?,
                    copied_bytes = ?,
                    finished_at = ?,
                    updated_at = ?
                where id = ?
                """,
                (status, error, copied_bytes, now, now, file_id),
            )
        else:
            conn.execute(
                """
                update files
                set status = ?,
                    error = ?,
                    warning = ?,
                    copied_bytes = ?,
                    finished_at = ?,
                    updated_at = ?
                where id = ?
                """,
                (status, error, warning, copied_bytes, now, now, file_id),
            )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def status_summary(job_dir: Path) -> dict[str, dict[str, int]]:
    conn = connect(job_dir)
    try:
        rows = conn.execute(
            """
            select status,
                   count(*) as count,
                   coalesce(sum(case when status = 'copied' then copied_bytes else size end), 0) as bytes
            from files
            group by status
            order by status
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        row["status"]: {"count": int(row["count"]), "bytes": int(row["bytes"])}
        for row in rows
    }


def all_files(job_dir: Path) -> list[sqlite3.Row]:
    conn = connect(job_dir)
    try:
        return conn.execute("select * from files order by relative_path").fetchall()
    finally:
        conn.close()
