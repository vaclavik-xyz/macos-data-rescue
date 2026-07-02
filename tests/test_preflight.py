import sqlite3

from pathlib import Path

from helpers import run_cli, write_file, init_and_scan


def test_preflight_passes_before_init_with_all_checks_reported(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")

    result = run_cli(
        "preflight",
        "--job-dir",
        str(tmp_path / "job"),
        "--source",
        str(source),
        "--dest",
        str(tmp_path / "dest"),
    )

    assert "ok source-exists" in result.stdout
    assert "ok source-not-root" in result.stdout
    assert "ok source-readable" in result.stdout
    assert "ok job-dir-outside-source" in result.stdout
    assert "ok dest-outside-source" in result.stdout
    # mount read-only state depends on the host filesystem; only the check
    # itself must be present and must not fail
    assert "source-mount" in result.stdout
    assert "fail source-mount" not in result.stdout
    assert "ok job-writable" in result.stdout
    assert "ok dest-writable" in result.stdout
    assert "ok free-space" in result.stdout
    assert "preflight=ok" in result.stdout


def test_preflight_fails_on_missing_source(tmp_path: Path) -> None:
    result = run_cli(
        "preflight",
        "--job-dir",
        str(tmp_path / "job"),
        "--source",
        str(tmp_path / "missing-source"),
        "--dest",
        str(tmp_path / "dest"),
        check=False,
    )

    assert result.returncode == 1
    assert "fail source-exists" in result.stdout
    assert "preflight=fail" in result.stdout


def test_preflight_fails_on_dest_inside_source_without_writing_into_source(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    before = sorted(path.name for path in source.rglob("*"))

    result = run_cli(
        "preflight",
        "--job-dir",
        str(tmp_path / "job"),
        "--source",
        str(source),
        "--dest",
        str(source / "rescued-output"),
        check=False,
    )

    after = sorted(path.name for path in source.rglob("*"))
    assert result.returncode == 1
    assert "fail dest-outside-source" in result.stdout
    assert "preflight=fail" in result.stdout
    assert before == after


def test_preflight_on_existing_job_reports_remaining_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    result = run_cli("preflight", "--job-dir", str(job_dir))

    assert "ok free-space" in result.stdout
    assert "needs" in result.stdout
    assert "preflight=ok" in result.stdout


def test_preflight_fails_when_free_space_is_below_manifest_needs(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set size = 1000000000000000000 where status = 'pending'")
        conn.commit()
    finally:
        conn.close()

    result = run_cli("preflight", "--job-dir", str(job_dir), check=False)

    assert result.returncode == 1
    assert "fail free-space" in result.stdout
    assert "preflight=fail" in result.stdout


def test_preflight_without_manifest_requires_source_and_dest(tmp_path: Path) -> None:
    result = run_cli("preflight", "--job-dir", str(tmp_path / "job"), check=False)

    assert result.returncode == 1
    assert "requires --source and --dest" in result.stderr


def test_preflight_rejects_explicit_paths_for_existing_job(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    result = run_cli(
        "preflight",
        "--job-dir",
        str(job_dir),
        "--source",
        str(source),
        "--dest",
        str(tmp_path / "dest"),
        check=False,
    )

    assert result.returncode == 1
    assert "already initialized" in result.stderr
