import sqlite3

from pathlib import Path

from helpers import run_cli, write_file


def init_customer_job(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "a.txt", b"a")
    write_file(source / "Desktop" / "b.txt", b"b")
    write_file(source / "Desktop" / "c.txt", b"c")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    return job_dir, source, dest_dir


def test_next_without_job_prints_init_template(tmp_path: Path) -> None:
    result = run_cli("next", "--job-dir", str(tmp_path / "missing-job"))

    assert "no job found" in result.stdout
    assert "preflight" in result.stdout
    assert "init --job-dir" in result.stdout
    assert "--profile restore" in result.stdout


def test_next_on_fresh_customer_job_suggests_visible_home_scan(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "action=scan" in result.stdout
    assert f"scan --job-dir {job_dir} --phase visible-home --timeout 300" in result.stdout


def test_next_prefers_copying_committed_rows_over_resuming_scan(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    partial = run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home", "--limit", "1")

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "stopped=limit" in partial.stdout
    assert "action=copy" in result.stdout
    assert f"copy --job-dir {job_dir} --phase visible-home --timeout 3600" in result.stdout


def test_next_resumes_interrupted_scan_after_rows_are_copied(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home", "--limit", "1")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "visible-home", "--timeout", "5")

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "action=scan" in result.stdout
    assert f"scan --job-dir {job_dir} --phase visible-home --timeout 300" in result.stdout


def test_next_moves_to_hidden_home_after_visible_home_completes(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "visible-home", "--timeout", "5")

    result = run_cli("next", "--job-dir", str(job_dir))

    assert f"scan --job-dir {job_dir} --phase hidden-home --timeout 300" in result.stdout


def test_next_after_core_phases_lists_gates_and_missing_reports(tmp_path: Path) -> None:
    job_dir, source, _ = init_customer_job(tmp_path)
    write_file(source / ".zshrc", b"zsh")
    for phase in ("visible-home", "hidden-home"):
        run_cli("scan", "--job-dir", str(job_dir), "--phase", phase)
        run_cli("copy", "--job-dir", str(job_dir), "--phase", phase, "--timeout", "5")

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "core-phases-complete" in result.stdout
    assert "ask-before: app-data applications full-home" in result.stdout
    assert "only after operator/customer approval" in result.stdout
    assert "customer-report" in result.stdout
    assert "report --job-dir" in result.stdout


def test_next_moves_past_phase_that_scanned_zero_files(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "visible-home", "--timeout", "5")
    # no dotfiles exist, so this scan records zero rows but must still count
    # as completed coverage
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "hidden-home")

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "action=scan phase=hidden-home" not in result.stdout
    assert "core-phases-complete" in result.stdout


def test_next_reports_exhausted_failures_instead_of_retrying_forever(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home")
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set status = 'failed', attempts = 2")
        conn.commit()
    finally:
        conn.close()

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "action=copy" not in result.stdout
    assert "review:" in result.stdout
    assert "failed/timed_out after retries" in result.stdout


def test_next_guides_restore_job_to_verification(tmp_path: Path) -> None:
    rescued = tmp_path / "user-data"
    write_file(rescued / "Desktop" / "a.txt", b"a")
    job_dir = tmp_path / "restore-job"
    dest = tmp_path / "new-home"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(rescued),
        "--dest", str(dest), "--profile", "restore",
    )

    fresh = run_cli("next", "--job-dir", str(job_dir))
    run_cli("scan", "--job-dir", str(job_dir))
    scanned = run_cli("next", "--job-dir", str(job_dir))
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "5")
    done = run_cli("next", "--job-dir", str(job_dir))

    assert f"scan --job-dir {job_dir} --timeout 300" in fresh.stdout
    assert "action=copy" in scanned.stdout
    assert f"copy --job-dir {job_dir} --timeout 3600" in scanned.stdout
    assert "restore-copy-complete" in done.stdout
    assert "must print processed=0" in done.stdout
    assert "resume --job-dir" in done.stdout
    assert "ownership" in done.stdout


def test_next_restore_with_exhausted_failures_requires_review_not_verification(tmp_path: Path) -> None:
    rescued = tmp_path / "user-data"
    write_file(rescued / "Desktop" / "a.txt", b"a")
    job_dir = tmp_path / "restore-job"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(rescued),
        "--dest", str(tmp_path / "new-home"), "--profile", "restore",
    )
    run_cli("scan", "--job-dir", str(job_dir))
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set status = 'failed', attempts = 2")
        conn.commit()
    finally:
        conn.close()

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "restore-copy-complete" not in result.stdout
    assert "must print processed=0" not in result.stdout
    assert "review:" in result.stdout
    assert "state: restore-needs-review" in result.stdout


def test_next_notes_legacy_only_manifest(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    run_cli("scan", "--job-dir", str(job_dir))

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "legacy" in result.stdout
    assert "status --job-dir" in result.stdout


def test_next_does_not_resuggest_core_phases_after_full_home(tmp_path: Path) -> None:
    job_dir, _, _ = init_customer_job(tmp_path)
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "full-home")

    result = run_cli("next", "--job-dir", str(job_dir))

    assert "visible-home" not in result.stdout.split("ask-before")[0]
    assert "action=copy" in result.stdout
    assert f"copy --job-dir {job_dir} --phase full-home --timeout 3600" in result.stdout


def run_suggested(command: str) -> str:
    import shlex

    tokens = shlex.split(command)
    assert tokens[:3] == ["uv", "run", "macos-data-rescue"]
    tokens = tokens[3:]
    if ">" in tokens:
        redirect_at = tokens.index(">")
        target = Path(tokens[redirect_at + 1])
        result = run_cli(*tokens[:redirect_at])
        target.write_text(result.stdout)
        return result.stdout
    return run_cli(*tokens).stdout


def drive_with_next(job_dir: Path, max_steps: int = 30) -> str:
    for _step in range(max_steps):
        out = run_cli("next", "--job-dir", str(job_dir)).stdout
        if "done: follow the handoff checklist" in out:
            return "done"
        if "restore-copy-complete" in out:
            commands = [
                line.strip() for line in out.splitlines() if line.strip().startswith("uv run")
            ]
            for command in commands:
                result = run_suggested(command)
                if " resume " in f" {command} ":
                    assert "processed=0" in result
            return "restore-verified"
        commands = [line.strip() for line in out.splitlines() if line.strip().startswith("uv run")]
        assert commands, f"next offered no command:\n{out}"
        run_suggested(commands[0])
    raise AssertionError("next guidance did not converge")


def test_next_guidance_converges_end_to_end_for_rescue_and_restore(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "faktura.txt", b"faktura")
    write_file(source / ".zshrc", b"alias")
    write_file(source / "Library" / "Mail" / "V10" / "mailbox", b"mail")
    write_file(source / "Library" / "Caches" / "junk.bin", b"junk")
    rescue_job = tmp_path / "rescue-job"
    rescued = tmp_path / "user-data"
    run_cli("init", "--job-dir", str(rescue_job), "--source", str(source), "--dest", str(rescued))

    assert drive_with_next(rescue_job) == "done"
    assert (rescued / "Desktop" / "faktura.txt").read_bytes() == b"faktura"
    assert (tmp_path / "recovery-report.pdf").exists()

    restore_job = tmp_path / "restore-job"
    new_home = tmp_path / "new-home"
    run_cli(
        "init", "--job-dir", str(restore_job), "--source", str(rescued),
        "--dest", str(new_home), "--profile", "restore",
    )

    assert drive_with_next(restore_job) == "restore-verified"
    assert (new_home / "Desktop" / "faktura.txt").read_bytes() == b"faktura"
