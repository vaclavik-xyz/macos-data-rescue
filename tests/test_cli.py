import json
import os
import shutil
import stat
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from shutil import which

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    src_path = str(ROOT / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "macos_data_rescue", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def init_and_scan(tmp_path: Path, source: Path | None = None) -> tuple[Path, Path, Path]:
    job_dir = tmp_path / "job"
    source_dir = source or tmp_path / "source-home"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source_dir), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir))
    return job_dir, source_dir, dest_dir


def file_rows(job_dir: Path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("select * from files order by relative_path").fetchall()
    finally:
        conn.close()
    return {row["relative_path"]: row for row in rows}


def config_value(job_dir: Path, key: str) -> str | None:
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select value from config where key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def test_pyproject_declares_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert data["project"]["scripts"]["macos-data-rescue"] == "macos_data_rescue.cli:main"


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


def test_scan_creates_manifest_with_phases_and_excludes(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Library" / "Application Support" / "Example" / "prefs.plist", b"prefs")
    write_file(source / "Projects" / "notes.txt", b"notes")
    write_file(source / "Library" / "Caches" / "cache.bin", b"cache")
    write_file(source / "node_modules" / "pkg" / "index.js", b"module")
    write_file(source / ".Trash" / "old.txt", b"trash")

    job_dir, _, _ = init_and_scan(tmp_path, source)

    rows = file_rows(job_dir)
    assert rows["Desktop/invoice.txt"]["phase"] == "important"
    assert rows["Pictures/photo.jpg"]["phase"] == "photos"
    assert rows["Library/Application Support/Example/prefs.plist"]["phase"] == "library"
    assert rows["Projects/notes.txt"]["phase"] == "all"
    assert rows["Desktop/invoice.txt"]["status"] == "pending"
    assert "Library/Caches/cache.bin" not in rows
    assert "node_modules/pkg/index.js" not in rows
    assert ".Trash/old.txt" not in rows


def test_scan_phase_important_only_records_high_value_dirs(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Documents" / "nested" / "contract.txt", b"contract")
    write_file(source / "Downloads" / "installer.dmg", b"download")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Library" / "Application Support" / "Example" / "prefs.plist", b"prefs")
    write_file(source / "Projects" / "notes.txt", b"notes")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Desktop/invoice.txt",
        "Documents/nested/contract.txt",
        "Downloads/installer.dmg",
    ]
    assert {row["phase"] for row in rows.values()} == {"important"}


def test_scan_limit_creates_partial_manifest_that_can_be_copied(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    write_file(source / "Desktop" / "b.txt", b"b")
    write_file(source / "Desktop" / "c.txt", b"c")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    scan = run_cli("scan", "--job-dir", str(job_dir), "--phase", "important", "--limit", "2")
    copy = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    rows = file_rows(job_dir)
    assert "scanned=2" in scan.stdout
    assert "stopped=limit" in scan.stdout
    assert len(rows) == 2
    assert "copied=2" in copy.stdout
    assert sorted(path.name for path in (dest_dir / "Desktop").iterdir()) == ["a.txt", "b.txt"]


def test_limited_scan_resumes_from_persisted_phase_cursor(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    for name in ("a.txt", "b.txt", "c.txt"):
        write_file(source / "Desktop" / name, name.encode())
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    first = run_cli("scan", "--job-dir", str(job_dir), "--phase", "important", "--limit", "2")
    first_rows = file_rows(job_dir)
    first_cursor = config_value(job_dir, "scan_cursor:important")
    second = run_cli("scan", "--job-dir", str(job_dir), "--phase", "important", "--limit", "2")

    rows = file_rows(job_dir)
    assert "scanned=2" in first.stdout
    assert "stopped=limit" in first.stdout
    assert sorted(first_rows) == ["Desktop/a.txt", "Desktop/b.txt"]
    assert first_cursor == "Desktop/b.txt"
    assert "scanned=1" in second.stdout
    assert "stopped=" not in second.stdout
    assert sorted(rows) == [
        "Desktop/a.txt",
        "Desktop/b.txt",
        "Desktop/c.txt",
    ]
    assert config_value(job_dir, "scan_cursor:important") is None


def test_scan_restarts_when_persisted_cursor_file_disappears(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    for name in ("a.txt", "b.txt", "c.txt"):
        write_file(source / "Desktop" / name, name.encode())
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important", "--limit", "2")
    (source / "Desktop" / "b.txt").unlink()

    result = run_cli("scan", "--job-dir", str(job_dir), "--phase", "important", "--limit", "5")

    rows = file_rows(job_dir)
    assert "scanned=2" in result.stdout
    assert "stopped=" not in result.stdout
    assert sorted(rows) == [
        "Desktop/a.txt",
        "Desktop/b.txt",
        "Desktop/c.txt",
    ]
    assert config_value(job_dir, "scan_cursor:important") is None


def test_resumed_scan_timeout_checks_skipped_cursor_items(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import scanner
    from macos_data_rescue.manifest import ScannedFile

    source = tmp_path / "source-home"
    source.mkdir()
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute(
            "insert into config(key, value) values(?, ?)",
            ("scan_cursor:all", "cursor.txt"),
        )
        conn.commit()
    finally:
        conn.close()

    def slow_files(source_path: Path, *, phase: str):
        for name in ("skip-1.txt", "skip-2.txt", "cursor.txt", "after.txt"):
            time.sleep(0.02)
            yield ScannedFile(
                relative_path=name,
                size=1,
                mtime_ns=1,
                mode=0o644,
                kind="file",
                phase=phase,
            )

    monkeypatch.setattr(scanner, "iter_source_files", slow_files)

    summary = scanner.scan_job(job_dir, phase="all", timeout=0.03, batch_size=1)

    assert summary.scanned == 0
    assert summary.stopped == "timeout"
    assert file_rows(job_dir) == {}
    assert config_value(job_dir, "scan_cursor:all") == "cursor.txt"


def test_stale_cursor_restart_keeps_original_scan_deadline(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import scanner
    from macos_data_rescue.manifest import ScannedFile

    source = tmp_path / "source-home"
    source.mkdir()
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute(
            "insert into config(key, value) values(?, ?)",
            ("scan_cursor:all", "missing.txt"),
        )
        conn.commit()
    finally:
        conn.close()

    monotonic_values = iter([100.0, 105.0, 109.0, 111.0, 112.0, 113.0])

    def fake_monotonic() -> float:
        return next(monotonic_values, 113.0)

    calls = 0

    def files(source_path: Path, *, phase: str):
        nonlocal calls
        calls += 1
        name = "before.txt" if calls == 1 else "after.txt"
        yield ScannedFile(
            relative_path=name,
            size=1,
            mtime_ns=1,
            mode=0o644,
            kind="file",
            phase=phase,
        )

    monkeypatch.setattr(scanner.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(scanner, "iter_source_files", files)

    summary = scanner.scan_job(job_dir, phase="all", timeout=10, batch_size=1)

    assert summary.scanned == 0
    assert summary.stopped == "timeout"
    assert file_rows(job_dir) == {}
    assert config_value(job_dir, "scan_cursor:all") == "missing.txt"


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


def test_scan_timeout_commits_partial_manifest(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import scanner
    from macos_data_rescue.manifest import ScannedFile

    source = tmp_path / "source-home"
    source.mkdir()
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    def slow_files(source_path: Path, *, phase: str):
        for index in range(5):
            time.sleep(0.02)
            yield ScannedFile(
                relative_path=f"{index}.txt",
                size=1,
                mtime_ns=1,
                mode=0o644,
                kind="file",
                phase=phase,
            )

    monkeypatch.setattr(scanner, "iter_source_files", slow_files)

    summary = scanner.scan_job(job_dir, phase="all", timeout=0.01, batch_size=1)

    rows = file_rows(job_dir)
    assert summary.scanned == 1
    assert summary.stopped == "timeout"
    assert sorted(rows) == ["0.txt"]


def test_scan_phase_visible_home_records_non_hidden_home_without_library_or_dot_items(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Projects" / "client" / "brief.txt", b"brief")
    write_file(source / "Applications" / "UserOnly.app" / "Contents" / "Info.plist", b"app")
    write_file(source / "Library" / "Mail" / "mailbox", b"mail")
    write_file(source / "Caches" / "blob", b"cache")
    write_file(source / "Temp" / "scratch", b"temp")
    write_file(source / ".ssh" / "config", b"ssh")
    write_file(source / ".zshrc", b"zsh")
    write_file(source / ".Trash" / "old.txt", b"trash")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Desktop/invoice.txt",
        "Pictures/photo.jpg",
        "Projects/client/brief.txt",
    ]
    assert {row["phase"] for row in rows.values()} == {"visible-home"}


def test_scan_phase_hidden_home_records_dot_items_without_cache_ballast(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / ".ssh" / "config", b"ssh")
    write_file(source / ".config" / "app" / "settings.json", b"{}")
    write_file(source / ".zshrc", b"zsh")
    write_file(source / ".cache" / "browser" / "cache.bin", b"cache")
    write_file(source / ".npm" / "_cacache" / "blob", b"cache")
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "hidden-home")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        ".config/app/settings.json",
        ".ssh/config",
        ".zshrc",
    ]
    assert {row["phase"] for row in rows.values()} == {"hidden-home"}


def test_scan_phase_photos_adds_rows_without_duplicating_existing_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Movies" / "clip.mov", b"movie")
    write_file(source / "Music" / "song.m4a", b"music")
    write_file(source / "Library" / "Application Support" / "Example" / "prefs.plist", b"prefs")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")
    first_rows = file_rows(job_dir)
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "photos")

    rows = file_rows(job_dir)
    assert len(first_rows) == 1
    assert sorted(rows) == [
        "Desktop/invoice.txt",
        "Movies/clip.mov",
        "Music/song.m4a",
        "Pictures/photo.jpg",
    ]
    assert rows["Desktop/invoice.txt"]["id"] == first_rows["Desktop/invoice.txt"]["id"]
    assert rows["Desktop/invoice.txt"]["phase"] == "important"
    assert rows["Pictures/photo.jpg"]["phase"] == "photos"
    assert rows["Movies/clip.mov"]["phase"] == "photos"
    assert rows["Music/song.m4a"]["phase"] == "photos"


def test_copy_after_important_scan_does_not_require_full_scan(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Library" / "Application Support" / "Example" / "prefs.plist", b"prefs")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    status = run_cli("status", "--job-dir", str(job_dir)).stdout
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    assert "copied=1" in result.stdout
    assert (dest_dir / "Desktop" / "invoice.txt").read_bytes() == b"desktop"
    assert not (dest_dir / "Pictures" / "photo.jpg").exists()
    assert status.startswith("pending=0 copying=0 copied=1")
    assert [item["relative_path"] for item in payload["files"]] == ["Desktop/invoice.txt"]


def test_scan_phase_app_data_records_curated_library_without_cache_ballast(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Library" / "Mail" / "V10" / "mailbox", b"mail")
    write_file(source / "Library" / "Messages" / "chat.db", b"messages")
    write_file(source / "Library" / "Safari" / "Bookmarks.plist", b"bookmarks")
    write_file(source / "Library" / "Keychains" / "login.keychain-db", b"keychain")
    write_file(source / "Library" / "Application Support" / "Example" / "data.sqlite", b"data")
    write_file(source / "Library" / "Application Support" / "Example" / "Caches" / "blob", b"cache")
    write_file(source / "Library" / "Caches" / "cache.bin", b"cache")
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "app-data")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Library/Application Support/Example/data.sqlite",
        "Library/Keychains/login.keychain-db",
        "Library/Mail/V10/mailbox",
        "Library/Messages/chat.db",
        "Library/Safari/Bookmarks.plist",
    ]
    assert {row["phase"] for row in rows.values()} == {"app-data"}


def test_scan_phase_app_data_does_not_follow_symlinked_library_root(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    outside = tmp_path / "outside-library"
    write_file(outside / "Mail" / "mailbox", b"outside")
    source.mkdir()
    os.symlink(outside, source / "Library")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "app-data")

    assert file_rows(job_dir) == {}


def test_scan_phase_full_home_records_visible_hidden_and_library_with_full_home_phase(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Applications" / "UserOnly.app" / "Contents" / "Info.plist", b"app")
    write_file(source / ".ssh" / "config", b"ssh")
    write_file(source / "Library" / "Mail" / "mailbox", b"mail")
    write_file(source / "Library" / "Caches" / "cache.bin", b"cache")
    write_file(source / ".cache" / "browser" / "blob", b"cache")
    write_file(source / ".npm" / "_cacache" / "blob", b"cache")
    write_file(source / "tmp" / "scratch", b"tmp")
    write_file(source / "Logs" / "debug.log", b"log")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "full-home")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        ".ssh/config",
        "Applications/UserOnly.app/Contents/Info.plist",
        "Desktop/invoice.txt",
        "Library/Mail/mailbox",
    ]
    assert {row["phase"] for row in rows.values()} == {"full-home"}


def test_scan_without_phase_keeps_legacy_all_classification(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Library" / "Mail" / "mailbox", b"mail")
    write_file(source / "Projects" / "notes.txt", b"notes")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    rows = file_rows(job_dir)
    assert rows["Desktop/invoice.txt"]["phase"] == "important"
    assert rows["Pictures/photo.jpg"]["phase"] == "photos"
    assert rows["Library/Mail/mailbox"]["phase"] == "library"
    assert rows["Projects/notes.txt"]["phase"] == "all"


def test_scan_and_copy_applications_reads_volume_applications_outside_home(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    volume_app = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    user_app = source / "Applications" / "UserOnly.app" / "Contents" / "Info.plist"
    write_file(volume_app, b"volume app")
    write_file(user_app, b"user app")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Home Applications/UserOnly.app/Contents/Info.plist",
        "Volume Applications/Legacy.app/Contents/Info.plist",
    ]
    assert {row["phase"] for row in rows.values()} == {"applications"}
    assert (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").read_bytes() == b"volume app"
    assert (dest_dir / "Home Applications" / "UserOnly.app" / "Contents" / "Info.plist").read_bytes() == b"user app"


def test_scan_phase_applications_does_not_follow_symlinked_application_roots(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    outside = tmp_path / "outside-apps"
    write_file(outside / "External.app" / "Contents" / "Info.plist", b"outside")
    source.mkdir(parents=True)
    os.symlink(outside, volume / "Applications")
    os.symlink(outside, source / "Applications")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")

    assert file_rows(job_dir) == {}


def test_scan_phase_applications_uses_last_users_segment_for_volume_root(tmp_path: Path) -> None:
    host_like_root = tmp_path / "Users" / "admin"
    volume = host_like_root / "Mounted Air"
    source = volume / "Users" / "dan"
    wrong_host_app = tmp_path / "Applications" / "Host.app" / "Contents" / "Info.plist"
    correct_volume_app = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    write_file(wrong_host_app, b"host app")
    write_file(correct_volume_app, b"volume app")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")

    rows = file_rows(job_dir)
    assert sorted(rows) == ["Volume Applications/Legacy.app/Contents/Info.plist"]


def test_applications_json_report_includes_source_path(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    write_file(app_file, b"volume app")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")

    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    [item] = payload["files"]
    assert item["relative_path"] == "Volume Applications/Legacy.app/Contents/Info.plist"
    assert item["source_path"] == str(app_file)


def test_scan_and_copy_applications_preserves_bundle_directories_named_cache(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Caches" / "keep.dat"
    write_file(app_file, b"bundle data")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    assert (
        dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Caches" / "keep.dat"
    ).read_bytes() == b"bundle data"


def test_copy_rejects_manifest_source_path_outside_application_roots(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    outside = tmp_path / "outside.txt"
    write_file(app_file, b"volume app")
    write_file(outside, b"outside")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set source_path = ?", (str(outside),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")
    row = file_rows(job_dir)["Volume Applications/Legacy.app/Contents/Info.plist"]

    assert "failed=1" in result.stdout
    assert row["status"] == "failed"
    assert "outside allowed application roots" in row["error"]
    assert not (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").exists()


def test_copy_rejects_manifest_source_path_under_symlinked_user_applications(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    outside_app = tmp_path / "outside-apps" / "External.app" / "Contents" / "Info.plist"
    write_file(app_file, b"volume app")
    write_file(outside_app, b"outside")
    source.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "outside-apps", source / "Applications")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set source_path = ?", (str(outside_app),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")
    row = file_rows(job_dir)["Volume Applications/Legacy.app/Contents/Info.plist"]

    assert "failed=1" in result.stdout
    assert row["status"] == "failed"
    assert "outside allowed application roots" in row["error"]


def test_copy_rejects_manifest_relative_path_outside_destination(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    outside = tmp_path / "outside-publish.txt"
    write_file(app_file, b"volume app")
    write_file(outside, b"original")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set relative_path = ?", (str(outside),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")
    row = file_rows(job_dir)[str(outside)]

    assert "failed=1" in result.stdout
    assert row["status"] == "failed"
    assert "unsafe manifest relative_path" in row["error"]
    assert outside.read_bytes() == b"original"


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


def test_copy_copies_files_preserves_content_and_updates_status(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Documents" / "nested" / "contract.txt", b"contract")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    os.chmod(source / "Desktop" / "invoice.txt", 0o640)

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    assert (dest_dir / "Desktop" / "invoice.txt").read_bytes() == b"desktop"
    assert (dest_dir / "Documents" / "nested" / "contract.txt").read_bytes() == b"contract"
    assert not (dest_dir / "Pictures" / "photo.jpg").exists()
    assert oct((dest_dir / "Desktop" / "invoice.txt").stat().st_mode & 0o777) == "0o640"
    rows = file_rows(job_dir)
    assert rows["Desktop/invoice.txt"]["status"] == "copied"
    assert rows["Documents/nested/contract.txt"]["status"] == "copied"
    assert rows["Pictures/photo.jpg"]["status"] == "pending"


def test_copy_fails_when_worker_reads_less_than_manifest_size(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    target = source / "Desktop" / "shrinking.txt"
    write_file(target, b"abcdef")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    target.write_bytes(b"abc")

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)
    markdown = run_cli("report", "--job-dir", str(job_dir), "--format", "markdown").stdout

    row = file_rows(job_dir)["Desktop/shrinking.txt"]
    assert result.returncode == 0
    assert "copied=0" in result.stdout
    assert "failed=1" in result.stdout
    assert row["status"] == "failed"
    assert row["copied_bytes"] == 3
    assert "expected 6 bytes, copied 3 bytes" in row["error"]
    assert not (dest_dir / "Desktop" / "shrinking.txt").exists()
    [file_payload] = payload["files"]
    assert file_payload["size"] == 6
    assert file_payload["copied_bytes"] == 3
    assert file_payload["status"] == "failed"
    assert "copied 3/6 bytes" in markdown


def test_copy_does_not_make_destination_immutable_before_publish(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    immutable = source / "Desktop" / "locked.txt"
    write_file(immutable, b"locked")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    os.chflags(immutable, stat.UF_IMMUTABLE)
    try:
        run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    finally:
        os.chflags(immutable, 0)

    copied = dest_dir / "Desktop" / "locked.txt"
    assert copied.read_bytes() == b"locked"
    assert file_rows(job_dir)["Desktop/locked.txt"]["status"] == "copied"
    if hasattr(os.stat(copied), "st_flags"):
        assert not (os.stat(copied).st_flags & stat.UF_IMMUTABLE)


def test_xattr_copy_helper_reads_source_and_writes_destination_only(tmp_path: Path) -> None:
    from macos_data_rescue import copier

    class FakeXattrOps:
        def __init__(self) -> None:
            self.reads: list[Path] = []
            self.writes: list[tuple[Path, str, bytes]] = []

        def list(self, path: Path) -> tuple[str, ...]:
            self.reads.append(path)
            return ("user.keep", "com.apple.ResourceFork", "com.apple.quarantine", "com.apple.macl")

        def get(self, path: Path, name: str) -> bytes:
            self.reads.append(path)
            return f"value:{name}".encode()

        def set(self, path: Path, name: str, value: bytes) -> None:
            self.writes.append((path, name, value))

    source = tmp_path / "source"
    dest = tmp_path / "dest"
    write_file(source, b"source")
    write_file(dest, b"dest")
    ops = FakeXattrOps()

    warning = copier.copy_xattrs(source, dest, ops=ops)

    assert warning is None
    assert ops.reads == [source, source, source]
    assert ops.writes == [
        (dest, "user.keep", b"value:user.keep"),
        (dest, "com.apple.ResourceFork", b"value:com.apple.ResourceFork"),
    ]


def test_xattr_copy_failure_marks_per_file_warning_without_failing_content(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import copier

    def fake_copy_one_with_timeout(source: Path, dest: Path, timeout: float, *, expected_size: int, **_kwargs) -> dict[str, object]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
        return {
            "status": "copied",
            "copied_bytes": expected_size,
            "warning": "extended attributes not fully preserved: failed user.test",
        }

    monkeypatch.setattr(copier, "copy_one_with_timeout", fake_copy_one_with_timeout)
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)

    result = copier.copy_job(job_dir, phase="important", timeout=2)
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)
    markdown = run_cli("report", "--job-dir", str(job_dir), "--format", "markdown").stdout

    assert result.copied == 1
    assert result.failed == 0
    assert (dest_dir / "Desktop" / "invoice.txt").read_bytes() == b"desktop"
    assert payload["files"][0]["warning"] == "extended attributes not fully preserved: failed user.test"
    assert "extended attributes not fully preserved: failed user.test" in markdown


def test_xattr_copy_warning_is_scoped_to_current_attempt(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import copier

    outcomes = [
        {
            "status": "copied",
            "warning": "extended attributes not fully preserved: failed user.test",
        },
        {"status": "failed", "error": "simulated retry failure"},
        {"status": "copied"},
    ]

    def fake_copy_one_with_timeout(source: Path, dest: Path, timeout: float, *, expected_size: int, **_kwargs) -> dict[str, object]:
        outcome = outcomes.pop(0)
        if outcome["status"] == "copied":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            return {"copied_bytes": expected_size, **outcome}
        return {**outcome, "copied_bytes": 0}

    monkeypatch.setattr(copier, "copy_one_with_timeout", fake_copy_one_with_timeout)
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)

    copier.copy_job(job_dir, phase="important", timeout=2)
    assert file_rows(job_dir)["Desktop/invoice.txt"]["warning"] == (
        "extended attributes not fully preserved: failed user.test"
    )

    (dest_dir / "Desktop" / "invoice.txt").unlink()
    copier.copy_job(job_dir, phase="important", timeout=2)
    failed_row = file_rows(job_dir)["Desktop/invoice.txt"]
    assert failed_row["status"] == "failed"
    assert failed_row["warning"] is None

    copier.copy_job(job_dir, phase="important", timeout=2)
    copied_row = file_rows(job_dir)["Desktop/invoice.txt"]
    assert copied_row["status"] == "copied"
    assert copied_row["warning"] is None


def test_xattr_cli_preserves_regular_attribute_when_available(tmp_path: Path) -> None:
    if which("xattr") is None:
        import pytest

        pytest.skip("xattr CLI not available")

    source = tmp_path / "source-home"
    source_file = source / "Desktop" / "invoice.txt"
    write_file(source_file, b"desktop")
    subprocess.run(
        ["xattr", "-w", "user.macos_data_rescue_test", "hello", str(source_file)],
        check=True,
    )
    os.chmod(source_file, 0o400)
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)

    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    dest_file = dest_dir / "Desktop" / "invoice.txt"
    result = subprocess.run(
        ["xattr", "-p", "user.macos_data_rescue_test", str(dest_file)],
        text=True,
        capture_output=True,
        check=True,
    )
    first_row = file_rows(job_dir)["Desktop/invoice.txt"]
    resume = run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    second_row = file_rows(job_dir)["Desktop/invoice.txt"]

    assert result.stdout.strip() == "hello"
    assert oct(dest_file.stat().st_mode & 0o777) == "0o400"
    assert "copied=0" in resume.stdout
    assert "skipped=1" in resume.stdout
    assert second_row["attempts"] == first_row["attempts"]


def test_resume_skips_already_copied_files_with_matching_source_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    before = file_rows(job_dir)["Desktop/invoice.txt"]

    result = run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    after = file_rows(job_dir)["Desktop/invoice.txt"]
    assert (dest_dir / "Desktop" / "invoice.txt").read_bytes() == b"desktop"
    assert after["status"] == "copied"
    assert after["attempts"] == before["attempts"]
    assert "skipped=1" in result.stdout


def test_resume_tolerates_coarse_destination_mtime_granularity(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    before = file_rows(job_dir)["Desktop/invoice.txt"]

    # Simulate an exFAT/FAT destination: stored mtime is rounded to a coarse
    # granularity, so it no longer equals the manifest mtime_ns exactly.
    dest_file = dest_dir / "Desktop" / "invoice.txt"
    info = dest_file.stat()
    coarse_mtime_ns = (info.st_mtime_ns // 2_000_000_000) * 2_000_000_000
    os.utime(dest_file, ns=(info.st_atime_ns, coarse_mtime_ns))

    result = run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    after = file_rows(job_dir)["Desktop/invoice.txt"]
    assert "skipped=1" in result.stdout
    assert "processed=0" in result.stdout
    assert after["attempts"] == before["attempts"]


def test_resume_still_recopies_destination_with_clearly_different_mtime(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    before = file_rows(job_dir)["Desktop/invoice.txt"]

    dest_file = dest_dir / "Desktop" / "invoice.txt"
    info = dest_file.stat()
    os.utime(dest_file, ns=(info.st_atime_ns, info.st_mtime_ns - 60_000_000_000))

    result = run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    after = file_rows(job_dir)["Desktop/invoice.txt"]
    assert "processed=1" in result.stdout
    assert "copied=1" in result.stdout
    assert after["attempts"] == before["attempts"] + 1


def test_copy_summary_does_not_count_freshly_copied_files_as_skipped(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    assert "copied=1" in result.stdout
    assert "skipped=0" in result.stdout


def test_copy_skips_fifo_without_burning_timeout(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    (source / "Desktop").mkdir(parents=True)
    os.mkfifo(source / "Desktop" / "stuck.fifo")
    write_file(source / "Desktop" / "regular.txt", b"regular")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    copy = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "5")
    first_row = file_rows(job_dir)["Desktop/stuck.fifo"]
    resume = run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "5")
    second_row = file_rows(job_dir)["Desktop/stuck.fifo"]

    assert first_row["kind"] == "fifo"
    assert first_row["status"] == "skipped"
    assert "not a regular file" in first_row["error"]
    assert "skipped=1" in copy.stdout
    assert "timed_out=0" in copy.stdout
    # resume: fifo stays skipped, regular.txt verifies as already copied
    assert "skipped=2" in resume.stdout
    assert "processed=0" in resume.stdout
    assert second_row["attempts"] == first_row["attempts"]
    assert not (dest_dir / "Desktop" / "stuck.fifo").exists()
    assert (dest_dir / "Desktop" / "regular.txt").read_bytes() == b"regular"


def test_timeout_and_failure_mark_file_and_continue_to_next_file(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    (source / "Desktop").mkdir(parents=True)
    os.mkfifo(source / "Desktop" / "aaa-stuck")
    write_file(source / "Desktop" / "bbb-denied.txt", b"denied")
    os.chmod(source / "Desktop" / "bbb-denied.txt", 0)
    write_file(source / "Desktop" / "ccc-after.txt", b"after")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        # Simulate a regular file whose read hangs in the kernel: the fifo blocks
        # open()/read() exactly like a severe disk I/O hang would.
        conn.execute("update files set kind = 'file' where relative_path = 'Desktop/aaa-stuck'")
        conn.commit()
    finally:
        conn.close()
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "0.2")

    rows = file_rows(job_dir)
    assert rows["Desktop/aaa-stuck"]["status"] == "timed_out"
    assert rows["Desktop/bbb-denied.txt"]["status"] == "failed"
    assert rows["Desktop/ccc-after.txt"]["status"] == "copied"
    assert (dest_dir / "Desktop" / "ccc-after.txt").read_bytes() == b"after"


def test_destination_setup_failure_marks_file_and_continues_to_next_file(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a" / "nested.txt", b"nested")
    write_file(source / "Desktop" / "z.txt", b"after")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    blocker = dest_dir / "Desktop" / "a"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"not a directory")

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    rows = file_rows(job_dir)
    assert result.returncode == 0
    assert "failed=1" in result.stdout
    assert "copied=1" in result.stdout
    assert rows["Desktop/a/nested.txt"]["status"] == "failed"
    assert "FileExistsError" in rows["Desktop/a/nested.txt"]["error"]
    assert rows["Desktop/z.txt"]["status"] == "copied"
    assert (dest_dir / "Desktop" / "z.txt").read_bytes() == b"after"


def test_destination_symlink_loop_marks_file_failed_and_continues(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a" / "nested.txt", b"nested")
    write_file(source / "Desktop" / "z.txt", b"after")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    loop = dest_dir / "Desktop" / "a"
    loop.parent.mkdir(parents=True)
    os.symlink(loop, loop)

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    rows = file_rows(job_dir)
    assert result.returncode == 0
    assert "failed=1" in result.stdout
    assert "copied=1" in result.stdout
    assert rows["Desktop/a/nested.txt"]["status"] == "failed"
    assert "unsafe manifest relative_path cannot be resolved" in rows["Desktop/a/nested.txt"]["error"]
    assert rows["Desktop/z.txt"]["status"] == "copied"
    assert (dest_dir / "Desktop" / "z.txt").read_bytes() == b"after"


def test_scan_records_directory_symlink_without_following_it(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    outside = tmp_path / "outside-dir"
    write_file(outside / "secret.txt", b"host secret")
    write_file(source / "Desktop" / "regular.txt", b"regular")
    os.symlink(outside, source / "Desktop" / "linked-folder")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    rows = file_rows(job_dir)
    assert rows["Desktop/linked-folder"]["kind"] == "symlink"
    assert rows["Desktop/linked-folder"]["status"] == "skipped"
    assert "Desktop/linked-folder/secret.txt" not in rows
    assert not (dest_dir / "Desktop" / "linked-folder").exists()
    assert (dest_dir / "Desktop" / "regular.txt").read_bytes() == b"regular"


def test_scan_applications_records_bundle_directory_symlinks(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    framework = volume / "Applications" / "Legacy.app" / "Contents" / "Frameworks" / "Foo.framework"
    write_file(framework / "Versions" / "A" / "Foo", b"binary")
    os.symlink("A", framework / "Versions" / "Current")
    source.mkdir(parents=True)
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    rows = file_rows(job_dir)
    current = "Volume Applications/Legacy.app/Contents/Frameworks/Foo.framework/Versions/Current"
    binary = "Volume Applications/Legacy.app/Contents/Frameworks/Foo.framework/Versions/A/Foo"
    assert rows[current]["kind"] == "symlink"
    assert rows[current]["status"] == "skipped"
    assert rows[binary]["status"] == "copied"


def test_copy_skips_symlink_without_copying_target_content(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("host secret")
    (source / "Desktop").mkdir(parents=True)
    os.symlink(outside, source / "Desktop" / "host-link")
    write_file(source / "Desktop" / "regular.txt", b"regular")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    rows = file_rows(job_dir)
    assert rows["Desktop/host-link"]["kind"] == "symlink"
    assert rows["Desktop/host-link"]["status"] == "skipped"
    assert not (dest_dir / "Desktop" / "host-link").exists()
    assert (dest_dir / "Desktop" / "regular.txt").read_bytes() == b"regular"


def test_temp_path_does_not_clobber_real_mirrored_file(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / ".foo.rescue-tmp", b"real hidden file")
    write_file(source / "Desktop" / "foo", b"main file")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    assert (dest_dir / "Desktop" / ".foo.rescue-tmp").read_bytes() == b"real hidden file"
    assert (dest_dir / "Desktop" / "foo").read_bytes() == b"main file"


def test_copy_cleans_stale_internal_temp_without_removing_manifest_file(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / ".foo.rescue-tmp", b"real hidden file")
    write_file(source / "Desktop" / "foo", b"main file")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    dest_desktop = dest_dir / "Desktop"
    dest_desktop.mkdir(parents=True)
    orphan_temp = dest_desktop / ".foo.crash.rescue-tmp"
    orphan_temp.write_text("orphan")

    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    assert not orphan_temp.exists()
    assert (dest_dir / "Desktop" / ".foo.rescue-tmp").read_bytes() == b"real hidden file"
    assert (dest_dir / "Desktop" / "foo").read_bytes() == b"main file"


def test_resume_limit_counts_files_that_need_work_not_skipped_matches(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    write_file(source / "Desktop" / "b.txt", b"b")
    write_file(source / "Desktop" / "c.txt", b"c")

    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--limit", "1")
    run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--limit", "1")

    rows = file_rows(job_dir)
    assert rows["Desktop/a.txt"]["status"] == "copied"
    assert rows["Desktop/b.txt"]["status"] == "copied"
    assert rows["Desktop/c.txt"]["status"] == "pending"


def test_resume_limit_reaches_copied_file_with_missing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    write_file(source / "Desktop" / "b.txt", b"b")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")
    (dest_dir / "Desktop" / "b.txt").unlink()

    run_cli("resume", "--job-dir", str(job_dir), "--phase", "important", "--limit", "1")

    assert (dest_dir / "Desktop" / "a.txt").read_bytes() == b"a"
    assert (dest_dir / "Desktop" / "b.txt").read_bytes() == b"b"


def test_copy_worker_reuses_one_process_across_files(tmp_path: Path) -> None:
    from macos_data_rescue import copier

    worker = copier.CopyWorker()
    results = []
    try:
        for name in ("a", "b", "c"):
            source = tmp_path / f"{name}.txt"
            source.write_bytes(b"x")
            dest = tmp_path / "out" / f"{name}.txt"
            dest.parent.mkdir(exist_ok=True)
            temp = copier.make_temp_path(dest)
            results.append(worker.copy_one(source, dest, temp, 1, timeout=10))
    finally:
        worker.close()

    assert [result["status"] for result in results] == ["copied", "copied", "copied"]
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"x"
    pids = {result["worker_pid"] for result in results}
    assert len(pids) == 1
    assert pids != {os.getpid()}


def test_copy_worker_recovers_after_worker_death(tmp_path: Path) -> None:
    from macos_data_rescue import copier

    worker = copier.CopyWorker()
    try:
        worker._ensure_worker()
        worker._process.kill()
        worker._process.join()

        source = tmp_path / "a.txt"
        source.write_bytes(b"x")
        dest = tmp_path / "out" / "a.txt"
        dest.parent.mkdir()
        temp = copier.make_temp_path(dest)
        result = worker.copy_one(source, dest, temp, 1, timeout=10)
    finally:
        worker.close()

    assert result["status"] == "copied"
    assert dest.read_bytes() == b"x"


def test_copy_worker_reports_mid_job_crash_as_failure_and_recovers(tmp_path: Path) -> None:
    import threading

    from macos_data_rescue import copier

    worker = copier.CopyWorker()
    try:
        stuck = tmp_path / "stuck.fifo"
        os.mkfifo(stuck)
        dest = tmp_path / "out" / "stuck"
        dest.parent.mkdir()
        temp = copier.make_temp_path(dest)

        killer = threading.Timer(0.3, lambda: worker._process.kill())
        killer.start()
        try:
            crashed = worker.copy_one(stuck, dest, temp, 1, timeout=30)
        finally:
            killer.cancel()

        source = tmp_path / "after.txt"
        source.write_bytes(b"x")
        after_dest = tmp_path / "out" / "after.txt"
        after_temp = copier.make_temp_path(after_dest)
        recovered = worker.copy_one(source, after_dest, after_temp, 1, timeout=10)
    finally:
        worker.close()

    assert crashed["status"] == "failed"
    assert "copy worker exited" in crashed["error"]
    assert recovered["status"] == "copied"
    assert after_dest.read_bytes() == b"x"


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


def test_copy_job_connection_count_does_not_scale_with_file_count(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import copier, manifest

    def count_connects(job_dir: Path) -> int:
        counter = {"connects": 0}
        original_connect = manifest.connect

        def counting_connect(target: Path):
            counter["connects"] += 1
            return original_connect(target)

        monkeypatch.setattr(manifest, "connect", counting_connect)
        monkeypatch.setattr(copier, "connect", counting_connect)
        try:
            copier.copy_job(job_dir, phase="important", timeout=2)
        finally:
            monkeypatch.setattr(manifest, "connect", original_connect)
            monkeypatch.setattr(copier, "connect", original_connect)
        return counter["connects"]

    small = tmp_path / "small"
    write_file(small / "source-home" / "Desktop" / "a.txt", b"a")
    small_job, _, _ = init_and_scan(small, small / "source-home")

    large = tmp_path / "large"
    for index in range(5):
        write_file(large / "source-home" / "Desktop" / f"{index}.txt", b"x")
    large_job, _, _ = init_and_scan(large, large / "source-home")

    assert count_connects(large_job) == count_connects(small_job)


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


def test_limited_copy_does_not_lock_rollback_journal_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    write_file(source / "Desktop" / "b.txt", b"b")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        journal_mode = conn.execute("pragma journal_mode = delete").fetchone()[0]
    finally:
        conn.close()
    assert journal_mode == "delete"

    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--limit", "1")

    rows = file_rows(job_dir)
    assert rows["Desktop/a.txt"]["status"] == "copied"
    assert rows["Desktop/b.txt"]["status"] == "pending"


def test_report_outputs_markdown_and_json_summary(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Desktop" / "denied.txt", b"denied")
    os.chmod(source / "Desktop" / "denied.txt", 0)
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    markdown = run_cli("report", "--job-dir", str(job_dir), "--format", "markdown").stdout
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    assert "# macOS Data Rescue Report" in markdown
    assert "Desktop/invoice.txt" in markdown
    assert "iCloud Optimize Mac Storage placeholders" in markdown
    assert payload["warnings"][0]["code"] == "icloud_dataless_placeholders"
    assert payload["summary"]["copied"]["count"] == 1
    assert payload["summary"]["failed"]["count"] == 1
    assert {item["relative_path"] for item in payload["files"]} == {
        "Desktop/denied.txt",
        "Desktop/invoice.txt",
    }


def test_customer_report_writes_markdown_and_pdf_to_recovery_root(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    recovery_root = tmp_path / "recovery"
    job_dir = recovery_root / ".rescue"
    dest_dir = recovery_root / "user-data"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    markdown_result = run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    pdf_result = run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    markdown_path = recovery_root / "recovery-report.md"
    pdf_path = recovery_root / "recovery-report.pdf"
    assert str(markdown_path) in markdown_result.stdout
    assert str(pdf_path) in pdf_result.stdout
    assert markdown_path.exists()
    assert pdf_path.exists()
    markdown = markdown_path.read_text()
    assert "# Data Recovery Report" in markdown
    assert f"Recovery destination: `{dest_dir.resolve(strict=False)}`" in markdown
    assert "| Copied files | 1 |" in markdown
    assert "| Failed files | 0 |" in markdown
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def test_customer_report_writes_czech_markdown_and_pdf_without_overwriting_english(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "faktura.txt", b"desktop")
    recovery_root = tmp_path / "recovery"
    job_dir = recovery_root / ".rescue"
    dest_dir = recovery_root / "user-data"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")
    cs_markdown_result = run_cli(
        "customer-report",
        "--job-dir",
        str(job_dir),
        "--format",
        "markdown",
        "--language",
        "cs",
    )
    cs_pdf_result = run_cli(
        "customer-report",
        "--job-dir",
        str(job_dir),
        "--format",
        "pdf",
        "--language",
        "cs",
    )

    english_markdown = recovery_root / "recovery-report.md"
    english_pdf = recovery_root / "recovery-report.pdf"
    czech_markdown = recovery_root / "recovery-report-cs.md"
    czech_pdf = recovery_root / "recovery-report-cs.pdf"
    assert str(czech_markdown) in cs_markdown_result.stdout
    assert str(czech_pdf) in cs_pdf_result.stdout
    assert english_markdown.exists()
    assert english_pdf.exists()
    czech_text = czech_markdown.read_text()
    czech_pdf_bytes = czech_pdf.read_bytes()
    assert "# Zpráva o záchraně dat" in czech_text
    assert "Souhrn pro zákazníka" in czech_text
    assert "| Zkopírované soubory | 1 |" in czech_text
    assert "Přehled zachráněných dat" in czech_text
    assert czech_pdf_bytes.startswith(b"%PDF-")
    assert b"\\u" not in czech_pdf_bytes
    assert b"/ccaron" in czech_pdf_bytes


def test_customer_report_rejects_output_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    source_output = source / "recovery-report.pdf"

    result = run_cli(
        "customer-report",
        "--job-dir",
        str(job_dir),
        "--format",
        "pdf",
        "--output",
        str(source_output),
        check=False,
    )

    assert result.returncode == 1
    assert "must not be inside source" in result.stderr
    assert not source_output.exists()


def test_customer_report_marks_pending_and_copying_as_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set status = 'copying' where relative_path = 'Desktop/invoice.txt'")
        conn.commit()
    finally:
        conn.close()

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    report_text = (tmp_path / "recovery-report.md").read_text()
    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert "completed with unresolved files" in report_text
    assert "| Copying files | 1 |" in report_text
    assert "| Pending files | 0 |" in report_text
    assert "not fully copied yet" in report_text
    assert b"Copying files" in pdf_bytes


def test_customer_report_preserves_existing_file_when_atomic_replace_fails(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import reporting

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    output_path = tmp_path / "recovery-report.md"
    output_path.write_text("existing report")

    def fail_replace(src: str | bytes | os.PathLike[str], dst: str | bytes | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(reporting.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        reporting.write_customer_report(job_dir, "markdown", output_path)

    assert output_path.read_text() == "existing report"
    assert not list(tmp_path.glob("*.rescue-report-tmp"))


def test_customer_report_temp_write_does_not_follow_stale_symlink_into_source(tmp_path: Path) -> None:
    from macos_data_rescue import reporting

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    victim = source / "Desktop" / "source-victim.txt"
    write_file(victim, b"source must stay untouched")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    output_path = tmp_path / "recovery-report.md"
    stale_predictable_temp = tmp_path / f".{output_path.name}.{os.getpid()}.rescue-report-tmp"
    os.symlink(victim, stale_predictable_temp)

    reporting.write_customer_report(job_dir, "markdown", output_path)

    assert output_path.exists()
    assert victim.read_bytes() == b"source must stay untouched"
    assert stale_predictable_temp.is_symlink()


def test_customer_pdf_report_escapes_unsupported_unicode_without_corrupting_markdown(tmp_path: Path) -> None:
    source = tmp_path / "zdroj-🙂"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")

    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"zdroj-\\\\U0001f642" in pdf_bytes
    assert "zdroj-🙂" in (tmp_path / "recovery-report.md").read_text()


def test_customer_report_pdf_has_layout_and_recovered_data_breakdown(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Projects" / "site" / "index.html", b"custom")
    write_file(source / "Library" / "Mail" / "V10" / "message.emlx", b"mail")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    markdown = (tmp_path / "recovery-report.md").read_text()
    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert "## Recovered Data Breakdown" in markdown
    assert "| Desktop | 1 |" in markdown
    assert "| Projects | 1 |" in markdown
    assert "| Library | 1 |" in markdown
    assert "## Library Data Recovered" in markdown
    assert "| Mail | 1 |" in markdown
    assert b"Recovered Data Breakdown" in pdf_bytes
    assert b"Desktop" in pdf_bytes
    assert b"Projects" in pdf_bytes
    assert b"Library Data Recovered" in pdf_bytes
    assert b"/F2" in pdf_bytes
    assert b" re f" in pdf_bytes
    assert b"1 1 1 rg 0 0 595 842 re f" in pdf_bytes


def test_customer_report_escapes_breakdown_markdown_table_cells(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Customer|Project" / "invoice.txt", b"invoice")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")

    markdown = (tmp_path / "recovery-report.md").read_text()
    assert "| Customer\\|Project | 1 |" in markdown
    assert "| Customer|Project | 1 |" not in markdown


def test_customer_pdf_breakdown_includes_more_than_twelve_rows(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    for index in range(14):
        write_file(source / f"Folder{index:02d}" / "file.txt", b"x")
        write_file(source / "Library" / f"Area{index:02d}" / "file.txt", b"x")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert b"Folder13" in pdf_bytes
    assert b"Area13" in pdf_bytes


def test_rescue_error_returns_one_but_argparse_usage_returns_two(tmp_path: Path) -> None:
    missing_job = tmp_path / "missing-job"

    job_error = run_cli("status", "--job-dir", str(missing_job), check=False)
    usage_error = run_cli("copy", check=False)

    assert job_error.returncode == 1
    assert "manifest not found" in job_error.stderr
    assert usage_error.returncode == 2
    assert "usage:" in usage_error.stderr


def test_copy_returns_zero_when_individual_files_fail(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "denied.txt", b"denied")
    os.chmod(source / "Desktop" / "denied.txt", 0)
    job_dir, _, _ = init_and_scan(tmp_path, source)

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2", check=False)

    assert result.returncode == 0
    assert "failed=1" in result.stdout


def test_copy_rejects_non_finite_timeout_as_usage_error(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)

    for value in ("nan", "inf"):
        result = run_cli(
            "copy",
            "--job-dir",
            str(job_dir),
            "--phase",
            "important",
            "--timeout",
            value,
            check=False,
        )

        assert result.returncode == 2
        assert "finite number greater than zero" in result.stderr
        assert not (dest_dir / "Desktop" / "invoice.txt").exists()


def test_report_marks_specific_suspected_icloud_placeholder_file(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    placeholder = (
        source
        / "Library"
        / "Mobile Documents"
        / "com~apple~CloudDocs"
        / "Customer"
        / "contract.pages"
    )
    write_file(placeholder, b"")

    job_dir, _, _ = init_and_scan(tmp_path, source)

    markdown = run_cli("report", "--job-dir", str(job_dir), "--format", "markdown").stdout
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    relative_path = "Library/Mobile Documents/com~apple~CloudDocs/Customer/contract.pages"
    warning = (
        "suspected iCloud dataless placeholder: data may not have been physically "
        "present on disk"
    )
    assert relative_path in markdown
    assert warning in markdown
    [file_payload] = payload["files"]
    assert file_payload["relative_path"] == relative_path
    assert file_payload["warning"] == warning


def test_symlink_warning_detection_does_not_read_target_xattrs(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import scanner

    source = tmp_path / "source-home"
    write_file(source / "target.txt", b"target")
    link = source / "Desktop" / "iCloud-link"
    link.parent.mkdir(parents=True)
    os.symlink(source / "target.txt", link)

    def fail_if_called(path: Path) -> tuple[str, ...]:
        raise AssertionError(f"must not read xattrs for symlink: {path}")

    monkeypatch.setattr(scanner, "list_xattr_names", fail_if_called)

    assert scanner.warning_for(link, ("Desktop", "iCloud-link"), link.stat(follow_symlinks=False)) is None


def test_sf_dataless_flag_marks_suspected_icloud_placeholder_without_path_marker(tmp_path: Path) -> None:
    from macos_data_rescue import scanner

    class FakeStat:
        st_mode = stat.S_IFREG | 0o644
        st_size = 1024 * 1024
        st_flags = scanner.SF_DATALESS

    warning = scanner.warning_for(tmp_path / "Desktop" / "contract.pages", ("Desktop", "contract.pages"), FakeStat())

    assert warning == scanner.ICLOUD_PLACEHOLDER_WARNING


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
