import json
import os
import stat
import sqlite3
import subprocess

from pathlib import Path
from shutil import which

from helpers import run_cli, write_file, init_and_scan, file_rows



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
    run_cli("approve", "--job-dir", str(job_dir), "--phase", "applications")
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
    run_cli("approve", "--job-dir", str(job_dir), "--phase", "applications")
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
    run_cli("approve", "--job-dir", str(job_dir), "--phase", "applications")
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
    error = rows["Desktop/a/nested.txt"]["error"]
    assert (
        "unsafe manifest relative_path cannot be resolved" in error
        or "FileExistsError" in error
    )
    assert rows["Desktop/z.txt"]["status"] == "copied"
    assert (dest_dir / "Desktop" / "z.txt").read_bytes() == b"after"


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

    assert orphan_temp.read_text() == "orphan"
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


def test_copy_returns_zero_when_individual_files_fail(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "denied.txt", b"denied")
    os.chmod(source / "Desktop" / "denied.txt", 0)
    job_dir, _, _ = init_and_scan(tmp_path, source)

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2", check=False)

    assert result.returncode == 0
    assert "failed=1" in result.stdout


def test_restore_copy_round_trip_records_symlinks_and_resumes_clean(tmp_path: Path) -> None:
    source = tmp_path / "rescued-user-data"
    write_file(source / "Desktop" / "faktura.txt", b"desktop")
    write_file(source / "Projects" / "web" / "node_modules" / "pkg" / "index.js", b"js")
    write_file(source / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist", b"app")
    outside = tmp_path / "outside-dir"
    write_file(outside / "secret.txt", b"secret")
    os.symlink(outside, source / "Desktop" / "rucne-pridany-odkaz")
    job_dir = tmp_path / "restore-job"
    dest_dir = tmp_path / "new-home"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(dest_dir), "--profile", "restore",
    )
    run_cli("scan", "--job-dir", str(job_dir))

    copy = run_cli("copy", "--job-dir", str(job_dir), "--phase", "restore", "--timeout", "5")
    resume = run_cli("resume", "--job-dir", str(job_dir), "--timeout", "5")

    rows = file_rows(job_dir)
    assert "copied=3" in copy.stdout
    assert "skipped=1" in copy.stdout
    assert (dest_dir / "Desktop" / "faktura.txt").read_bytes() == b"desktop"
    assert (dest_dir / "Projects" / "web" / "node_modules" / "pkg" / "index.js").read_bytes() == b"js"
    assert (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").read_bytes() == b"app"
    assert rows["Desktop/rucne-pridany-odkaz"]["kind"] == "symlink"
    assert rows["Desktop/rucne-pridany-odkaz"]["status"] == "skipped"
    assert not (dest_dir / "Desktop" / "rucne-pridany-odkaz").exists()
    assert "processed=0" in resume.stdout
    assert "skipped=4" in resume.stdout
