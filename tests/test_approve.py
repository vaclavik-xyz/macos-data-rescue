import sqlite3

from pathlib import Path

from helpers import run_cli, write_file, config_value, file_rows


def init_job_with_library(tmp_path: Path) -> Path:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Library" / "Mail" / "V10" / "mailbox", b"mail")
    job_dir = tmp_path / "job"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(tmp_path / "dest"))
    return job_dir


def test_scan_gated_phase_without_approval_fails(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    result = run_cli("scan", "--job-dir", str(job_dir), "--phase", "app-data", check=False)

    assert result.returncode == 1
    assert "approval required" in result.stderr
    assert f"approve --job-dir {job_dir} --phase app-data" in result.stderr
    assert file_rows(job_dir) == {}


def test_approve_records_decision_and_unlocks_scan(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    approve = run_cli("approve", "--job-dir", str(job_dir), "--phase", "app-data", "--by", "Filip")
    scan = run_cli("scan", "--job-dir", str(job_dir), "--phase", "app-data")

    recorded = config_value(job_dir, "approved:app-data")
    assert "approved phase=app-data at=" in approve.stdout
    assert "by=Filip" in approve.stdout
    assert recorded is not None
    assert "by=Filip" in recorded
    assert "scanned=1" in scan.stdout


def test_approve_rejects_non_gated_phase(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    result = run_cli("approve", "--job-dir", str(job_dir), "--phase", "visible-home", check=False)

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_legacy_library_scan_stays_ungated(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    result = run_cli("scan", "--job-dir", str(job_dir), "--phase", "library")

    assert "scanned=1" in result.stdout


def test_copy_of_preexisting_gated_rows_requires_approval(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)
    # simulate a manifest written by an older version: gated rows exist
    # without any approval record
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute(
            """
            insert into files(relative_path, size, mtime_ns, mode, kind, phase,
                              status, scanned_at, updated_at)
            values('Library/Mail/V10/mailbox', 4, 0, 420, 'file', 'app-data',
                   'pending', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    direct = run_cli("copy", "--job-dir", str(job_dir), "--phase", "app-data", "--timeout", "5", check=False)
    via_all = run_cli("resume", "--job-dir", str(job_dir), "--phase", "all", "--timeout", "5", check=False)

    assert direct.returncode == 1
    assert "approval required" in direct.stderr
    assert via_all.returncode == 1
    assert f"approve --job-dir {job_dir} --phase app-data" in via_all.stderr
    assert not (tmp_path / "dest" / "Library").exists()

    run_cli("approve", "--job-dir", str(job_dir), "--phase", "app-data")
    unlocked = run_cli("copy", "--job-dir", str(job_dir), "--phase", "app-data", "--timeout", "5")

    assert "copied=1" in unlocked.stdout
