from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import RescueError


MANIFEST_NAME = "manifest.sqlite"


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
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
            size integer not null,
            mtime_ns integer not null,
            mode integer not null,
            kind text not null,
            phase text not null,
            status text not null default 'pending',
            attempts integer not null default 0,
            error text,
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


def init_manifest(job_dir: Path, source: Path, dest: Path, profile: str) -> JobConfig:
    if not source.exists() or not source.is_dir():
        raise RescueError(f"source must be an existing directory: {source}")
    job_dir.mkdir(parents=True, exist_ok=True)
    db_path = manifest_path(job_dir)
    conn = sqlite3.connect(db_path)
    try:
        create_schema(conn)
        now = utc_now()
        values = {
            "source": str(source.resolve()),
            "dest": str(dest.resolve(strict=False)),
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
    return JobConfig(job_dir=job_dir, source=source.resolve(), dest=dest.resolve(strict=False), profile=profile)


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
    return JobConfig(
        job_dir=job_dir,
        source=Path(values["source"]),
        dest=Path(values["dest"]),
        profile=values["profile"],
    )


def upsert_scanned_files(job_dir: Path, files: Iterable[ScannedFile]) -> int:
    conn = connect(job_dir)
    now = utc_now()
    count = 0
    try:
        for item in files:
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
                    relative_path, size, mtime_ns, mode, kind, phase, status,
                    attempts, error, copied_bytes, scanned_at, started_at, finished_at, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                on conflict(relative_path) do update set
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    mode = excluded.mode,
                    kind = excluded.kind,
                    phase = excluded.phase,
                    status = ?,
                    error = ?,
                    copied_bytes = ?,
                    started_at = ?,
                    finished_at = ?,
                    scanned_at = excluded.scanned_at,
                    updated_at = excluded.updated_at
                """,
                (
                    item.relative_path,
                    item.size,
                    item.mtime_ns,
                    item.mode,
                    item.kind,
                    item.phase,
                    status,
                    error,
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
        conn.execute(
            """
            insert into config(key, value) values('updated_at', ?)
            on conflict(key) do update set value = excluded.value
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    return count


def selected_files(job_dir: Path, phase: str, limit: int | None = None) -> list[sqlite3.Row]:
    conn = connect(job_dir)
    try:
        params: list[object] = []
        where = ""
        if phase != "all":
            where = "where phase = ?"
            params.append(phase)
        sql = f"select * from files {where} order by relative_path"
        if limit is not None:
            sql += " limit ?"
            params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def mark_copying(job_dir: Path, file_id: int) -> None:
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
        conn.close()


def mark_result(
    job_dir: Path,
    file_id: int,
    status: str,
    *,
    error: str | None = None,
    copied_bytes: int = 0,
) -> None:
    conn = connect(job_dir)
    now = utc_now()
    try:
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
        conn.commit()
    finally:
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
