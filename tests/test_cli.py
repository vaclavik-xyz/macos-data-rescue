import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path


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


def test_pyproject_declares_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert data["project"]["scripts"]["macos-data-rescue"] == "macos_data_rescue.cli:main"


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


def test_copy_summary_does_not_count_freshly_copied_files_as_skipped(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    assert "copied=1" in result.stdout
    assert "skipped=0" in result.stdout


def test_timeout_and_failure_mark_file_and_continue_to_next_file(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    (source / "Desktop").mkdir(parents=True)
    os.mkfifo(source / "Desktop" / "aaa-stuck")
    write_file(source / "Desktop" / "bbb-denied.txt", b"denied")
    os.chmod(source / "Desktop" / "bbb-denied.txt", 0)
    write_file(source / "Desktop" / "ccc-after.txt", b"after")

    job_dir, _, dest_dir = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "0.2")

    rows = file_rows(job_dir)
    assert rows["Desktop/aaa-stuck"]["status"] == "timed_out"
    assert rows["Desktop/bbb-denied.txt"]["status"] == "failed"
    assert rows["Desktop/ccc-after.txt"]["status"] == "copied"
    assert (dest_dir / "Desktop" / "ccc-after.txt").read_bytes() == b"after"


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
    assert payload["summary"]["copied"]["count"] == 1
    assert payload["summary"]["failed"]["count"] == 1
    assert {item["relative_path"] for item in payload["files"]} == {
        "Desktop/denied.txt",
        "Desktop/invoice.txt",
    }
