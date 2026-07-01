import json
import shutil
import sqlite3
import pytest

from pathlib import Path

from helpers import run_cli, write_file, init_and_scan, file_rows, config_value



def test_init_rejects_job_or_dest_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    source.mkdir()

    job_inside = source / ".rescue"
    result = run_cli(
        "init",
        "--job-dir",
        str(job_inside),
        "--source",
        str(source),
        "--dest",
        str(tmp_path / "dest"),
        check=False,
    )
    assert result.returncode != 0
    assert "must not be inside source" in result.stderr
    assert not job_inside.exists()

    dest_inside = source / "rescued-output"
    result = run_cli(
        "init",
        "--job-dir",
        str(tmp_path / "job"),
        "--source",
        str(source),
        "--dest",
        str(dest_inside),
        check=False,
    )
    assert result.returncode != 0
    assert "must not be inside source" in result.stderr
    assert not dest_inside.exists()


def test_init_rejects_dest_inside_source_spelled_with_different_case(tmp_path: Path) -> None:
    source = tmp_path / "Source-Home"
    source.mkdir()
    aliased = tmp_path / "sOURCE-hOME"
    if not aliased.exists():
        pytest.skip("filesystem is case-sensitive")

    dest_inside = aliased / "rescued-output"
    result = run_cli(
        "init",
        "--job-dir",
        str(tmp_path / "job"),
        "--source",
        str(source),
        "--dest",
        str(dest_inside),
        check=False,
    )

    assert result.returncode != 0
    assert "must not be inside source" in result.stderr
    assert not dest_inside.exists()


def test_existing_manifest_revalidates_source_write_guards(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    safe_job = tmp_path / "job"
    run_cli("init", "--job-dir", str(safe_job), "--source", str(source), "--dest", str(tmp_path / "dest"))

    conn = sqlite3.connect(safe_job / "manifest.sqlite")
    try:
        conn.execute("update config set value = ? where key = 'dest'", (str(source / "rescued-output"),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("scan", "--job-dir", str(safe_job), check=False)
    assert result.returncode != 0
    assert "dest must not be inside source" in result.stderr

    unsafe_job = source / ".rescue"
    shutil.copytree(safe_job, unsafe_job)
    conn = sqlite3.connect(unsafe_job / "manifest.sqlite")
    try:
        conn.execute("update config set value = ? where key = 'dest'", (str(tmp_path / "dest"),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("scan", "--job-dir", str(unsafe_job), check=False)
    assert result.returncode != 0
    assert "job-dir must not be inside source" in result.stderr


def test_unsafe_legacy_manifest_is_not_migrated_before_source_guard(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    unsafe_job = source / ".rescue"
    unsafe_job.mkdir()
    conn = sqlite3.connect(unsafe_job / "manifest.sqlite")
    try:
        conn.executescript(
            """
            create table config (
                key text primary key,
                value text not null
            );
            create table files (
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
            """
        )
        conn.executemany(
            "insert into config(key, value) values(?, ?)",
            {
                "source": str(source.resolve()),
                "dest": str((tmp_path / "dest").resolve(strict=False)),
                "profile": "customer-home",
            }.items(),
        )
        conn.commit()
    finally:
        conn.close()

    result = run_cli("scan", "--job-dir", str(unsafe_job), check=False)

    conn = sqlite3.connect(unsafe_job / "manifest.sqlite")
    try:
        columns = {row[1] for row in conn.execute("pragma table_info(files)")}
    finally:
        conn.close()
    assert result.returncode != 0
    assert "job-dir must not be inside source" in result.stderr
    assert "warning" not in columns


def test_scan_batches_are_committed_before_generator_finishes(tmp_path: Path) -> None:
    from macos_data_rescue.manifest import (
        ScannedFile,
        init_manifest,
        scan_cursor_key,
        upsert_scanned_files,
    )

    source = tmp_path / "source-home"
    source.mkdir()
    job_dir = tmp_path / "job"
    init_manifest(job_dir, source, tmp_path / "dest", "customer-home")

    def interrupted_files():
        for name in ("a.txt", "b.txt", "c.txt"):
            yield ScannedFile(
                relative_path=name,
                size=1,
                mtime_ns=1,
                mode=0o644,
                kind="file",
                phase="all",
            )
        raise RuntimeError("scan interrupted")

    try:
        upsert_scanned_files(
            job_dir,
            interrupted_files(),
            batch_size=2,
            cursor_key=scan_cursor_key("all"),
        )
    except RuntimeError:
        pass

    rows = file_rows(job_dir)
    assert sorted(rows) == ["a.txt", "b.txt"]
    assert config_value(job_dir, "scan_cursor:all") == "b.txt"


def test_manifest_migration_adds_source_path_for_application_rows(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    write_file(app_file, b"volume app")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    job_dir.mkdir()
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.executescript(
            """
            create table config (
                key text primary key,
                value text not null
            );
            create table files (
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
                warning text,
                copied_bytes integer not null default 0,
                scanned_at text not null,
                started_at text,
                finished_at text,
                updated_at text not null
            );
            """
        )
        conn.executemany(
            "insert into config(key, value) values(?, ?)",
            {
                "source": str(source.resolve()),
                "dest": str(dest_dir.resolve(strict=False)),
                "profile": "customer-home",
            }.items(),
        )
        conn.execute(
            """
            insert into files(
                relative_path, size, mtime_ns, mode, kind, phase, status,
                warning, copied_bytes, scanned_at, updated_at
            )
            values('Volume Applications/Legacy.app/Contents/Info.plist', 10, 0, 33188,
                   'file', 'applications', 'pending', null, 0, '2026-01-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    run_cli("status", "--job-dir", str(job_dir))

    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("pragma table_info(files)")}
        row = conn.execute("select * from files").fetchone()
    finally:
        conn.close()
    assert "source_path" in columns
    assert row["source_path"] == str(app_file)

    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    assert (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").read_bytes() == b"volume app"


def test_scan_migrates_existing_manifest_for_file_warnings(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    placeholder = (
        source
        / "Library"
        / "Mobile Documents"
        / "com~apple~CloudDocs"
        / "Customer"
        / "legacy.pages"
    )
    write_file(placeholder, b"")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.executescript(
            """
            create table config (
                key text primary key,
                value text not null
            );
            create table files (
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
            """
        )
        conn.executemany(
            "insert into config(key, value) values(?, ?)",
            {
                "source": str(source.resolve()),
                "dest": str((tmp_path / "dest").resolve(strict=False)),
                "profile": "customer-home",
            }.items(),
        )
        conn.commit()
    finally:
        conn.close()

    run_cli("scan", "--job-dir", str(job_dir))
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    assert payload["files"][0]["warning"].startswith("suspected iCloud dataless placeholder")


def test_mark_helpers_reuse_provided_connection(tmp_path: Path) -> None:
    from macos_data_rescue import manifest

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    file_id = int(file_rows(job_dir)["Desktop/a.txt"]["id"])

    conn = manifest.connect(job_dir)
    try:
        manifest.mark_copying(job_dir, file_id, conn=conn)
        manifest.mark_result(job_dir, file_id, "copied", copied_bytes=1, conn=conn)
        status = conn.execute("select status from files where id = ?", (file_id,)).fetchone()[0]
    finally:
        conn.close()

    assert status == "copied"
    assert file_rows(job_dir)["Desktop/a.txt"]["status"] == "copied"


def test_manifest_selected_files_are_streamed(tmp_path: Path) -> None:
    from macos_data_rescue.manifest import iter_selected_files

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    write_file(source / "Desktop" / "b.txt", b"b")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    rows = iter_selected_files(job_dir, "important")

    assert not isinstance(rows, list)
    iterator = iter(rows)
    assert next(iterator)["relative_path"] == "Desktop/a.txt"
