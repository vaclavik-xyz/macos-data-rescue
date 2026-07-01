import json
import os
import stat
import sqlite3
import time

from pathlib import Path

from helpers import run_cli, write_file, init_and_scan, file_rows, config_value



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
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "app-data", "--timeout", "2")

    rows = file_rows(job_dir)
    assert sorted(rows) == ["Library"]
    assert rows["Library"]["kind"] == "symlink"
    assert rows["Library"]["status"] == "skipped"
    assert not (dest_dir / "Library").exists()


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
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    rows = file_rows(job_dir)
    assert sorted(rows) == ["Home Applications", "Volume Applications"]
    assert {row["kind"] for row in rows.values()} == {"symlink"}
    assert {row["status"] for row in rows.values()} == {"skipped"}
    assert not (dest_dir / "Home Applications").exists()
    assert not (dest_dir / "Volume Applications").exists()


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


def test_scan_phase_important_records_symlinked_root_without_following(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    outside = tmp_path / "outside-docs"
    write_file(outside / "secret.txt", b"host secret")
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    os.symlink(outside, source / "Documents")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    rows = file_rows(job_dir)
    assert sorted(rows) == ["Desktop/invoice.txt", "Documents"]
    assert rows["Documents"]["kind"] == "symlink"
    assert rows["Documents"]["status"] == "skipped"
    assert not (dest_dir / "Documents").exists()


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
