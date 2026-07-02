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


def test_legacy_library_scan_requires_approval(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    blocked = run_cli("scan", "--job-dir", str(job_dir), "--phase", "library", check=False)
    run_cli("approve", "--job-dir", str(job_dir), "--phase", "library")
    unlocked = run_cli("scan", "--job-dir", str(job_dir), "--phase", "library")

    assert blocked.returncode == 1
    assert "approval required for phase library" in blocked.stderr
    assert "scanned=1" in unlocked.stdout


def test_default_scan_requires_library_approval(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    blocked = run_cli("scan", "--job-dir", str(job_dir), check=False)
    run_cli("approve", "--job-dir", str(job_dir), "--phase", "library")
    unlocked = run_cli("scan", "--job-dir", str(job_dir))

    assert blocked.returncode == 1
    assert "approval required for phase library" in blocked.stderr
    assert "~/Library" in blocked.stderr
    assert file_rows(job_dir) == {} or "scanned=" not in blocked.stdout
    assert "scanned=2" in unlocked.stdout


def test_legacy_important_scan_stays_ungated(tmp_path: Path) -> None:
    job_dir = init_job_with_library(tmp_path)

    result = run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")

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


def test_reinit_cannot_change_profile_of_existing_job(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Library" / "Mail" / "V10" / "mailbox", b"secret mail")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    # customer-home gate blocks the default scan
    assert run_cli("scan", "--job-dir", str(job_dir), check=False).returncode == 1

    reinit = run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(dest_dir), "--profile", "restore", check=False,
    )

    assert reinit.returncode == 1
    assert "already initialized" in reinit.stderr
    assert config_value(job_dir, "profile") == "customer-home"
    # the gate is still in force after the rejected re-init
    assert run_cli("scan", "--job-dir", str(job_dir), check=False).returncode == 1


def test_reinit_with_same_profile_is_allowed(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    reinit = run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    assert "initialized job=" in reinit.stdout
    assert config_value(job_dir, "profile") == "customer-home"


def test_reinit_fails_closed_on_unreadable_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    # corrupt the manifest so its profile cannot be read
    (job_dir / "manifest.sqlite").write_bytes(b"not a sqlite database at all")

    result = run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(dest_dir), "--profile", "restore", check=False,
    )

    assert result.returncode == 1
    assert "existing manifest cannot be read" in result.stderr
