import errno
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from fakes import FakeDiskutil
from helpers import ROOT, file_rows, init_and_scan, run_cli, write_file
from macos_data_rescue import copier
from macos_data_rescue.activity import activity_snapshot
from macos_data_rescue.errors import RescueError
from macos_data_rescue.manifest import connect
from macos_data_rescue.reconnect import ReconnectPolicy
from macos_data_rescue.storage_events import blocked_by_give_up, list_events, record_event

POLICY = ReconnectPolicy(cooldown=0, wait=2, poll=0.01)
SOURCE_MOUNT = "/Volumes/FakeSource"
DEST_MOUNT = "/Volumes/FakeDest"
LOST_TWICE = "storage lost twice while reading this file"


def seed_identities(job, *, source_uuid="SRC", dest_uuid="DST"):
    def identity(uuid, mount_point):
        return dict(uuid=uuid, mount_point=mount_point if uuid else None, fs_type="apfs" if uuid else "smbfs",
                    network=uuid is None, read_only=False)
    value = {"source": identity(source_uuid, SOURCE_MOUNT), "destination": identity(dest_uuid, DEST_MOUNT)}
    conn = connect(job)
    try:
        conn.execute("insert into config values('volume_identities', ?)", (json.dumps(value),))
        conn.commit()
    finally:
        conn.close()


class Disk:
    """Source directory plus a FakeDiskutil that remounts it by renaming it back."""

    def __init__(self, source):
        self.source = source
        self.unplugged = source.with_name("unplugged")
        self.fake = FakeDiskutil(on_mount=lambda volume: self.plug())
        self.volume = self.fake.add("SRC", mount_point=SOURCE_MOUNT, writable=False)
        self.fake.add("DST", mount_point=DEST_MOUNT, writable=True)

    def unplug(self):
        self.source.rename(self.unplugged)
        self.volume.mount_point = None

    def plug(self):
        if self.unplugged.exists():
            self.unplugged.rename(self.source)


@pytest.fixture
def rig(tmp_path):
    for name in ("a", "b", "c"):
        write_file(tmp_path / "source-home" / name, b"hello")
    job, source, dest = init_and_scan(tmp_path)
    seed_identities(job)
    return job, Disk(source), dest


def kinds(job):
    conn = connect(job)
    try:
        return [event["kind"] for event in list_events(conn)]
    finally:
        conn.close()


def unplug_on(monkeypatch, disk, names):
    """Unplug the source while copying each listed file (one entry per loss)."""
    original = copier.copy_one_with_timeout
    pending = list(names)

    def copy(source_file, target, timeout, **kwargs):
        if pending and source_file.name == pending[0]:
            pending.pop(0)
            disk.unplug()
            return dict(status="failed", error="device read failed")
        return original(source_file, target, timeout, **kwargs)

    monkeypatch.setattr(copier, "copy_one_with_timeout", copy)


def test_disconnect_mid_copy_auto_resumes(rig, monkeypatch):
    job, disk, dest = rig
    unplug_on(monkeypatch, disk, ["b"])
    result = copier.copy_job(job, phase="all", timeout=2, reconnect=POLICY, diskutil=disk.fake)
    assert result.copied == 3 and result.paused is None and result.reconnects == 1
    assert "reconnects=1" in result.as_line()
    assert kinds(job) == ["lost", "cooldown", "remount-attempt", "remounted", "resumed"]
    assert disk.fake.calls == [("mount", "SRC", True)]
    assert all(row["status"] == "copied" for row in file_rows(job).values())
    assert (dest / "c").read_bytes() == b"hello"
    assert activity_snapshot(job)["state"] == "finished"


def test_without_flag_behavior_is_unchanged(rig, monkeypatch):
    job, disk, _ = rig
    unplug_on(monkeypatch, disk, ["b"])
    result = copier.copy_job(job, phase="all", timeout=2, diskutil=disk.fake)
    assert result.paused and result.copied == 1 and result.reconnects == 0
    assert "reconnects" not in result.as_line()
    rows = file_rows(job)
    assert rows["b"]["status"] == rows["c"]["status"] == "pending"
    assert rows["b"]["attempts"] == rows["c"]["attempts"] == 0
    assert disk.fake.calls == [] and disk.fake.info_calls == [] and kinds(job) == []


def test_full_destination_never_reconnects(rig, monkeypatch):
    job, disk, _ = rig

    def full(source, target, timeout, **kwargs):
        return dict(status="failed", error="ENOSPC", pause_reason=copier.storage_error_reason(OSError(errno.ENOSPC, "full")))

    monkeypatch.setattr(copier, "copy_one_with_timeout", full)
    result = copier.copy_job(job, phase="all", timeout=2, reconnect=POLICY, diskutil=disk.fake)
    assert result.paused and "full" in result.paused
    assert "lost" not in kinds(job) and disk.fake.calls == []
    assert all(row["status"] == "pending" and row["attempts"] == 0 for row in file_rows(job).values())


def test_same_file_lost_twice_is_failed_and_queue_continues(rig, monkeypatch):
    job, disk, _ = rig
    unplug_on(monkeypatch, disk, ["b", "b"])
    result = copier.copy_job(job, phase="all", timeout=2, reconnect=POLICY, diskutil=disk.fake)
    rows = file_rows(job)
    assert rows["a"]["status"] == rows["c"]["status"] == "copied"
    assert rows["b"]["status"] == "failed" and rows["b"]["error"] == LOST_TWICE
    assert rows["b"]["attempts"] == 1
    assert result.paused is None and result.reconnects == 2 and result.failed == 1


def test_probe_losses_without_file_never_mark_failed(rig, monkeypatch):
    job, disk, _ = rig
    original = copier.copy_one_with_timeout
    pending = ["a", "b"]

    def copy_then_unplug(source_file, target, timeout, **kwargs):
        result = original(source_file, target, timeout, **kwargs)
        if pending and source_file.name == pending[0]:
            pending.pop(0)
            disk.unplug()
        return result

    monkeypatch.setattr(copier, "copy_one_with_timeout", copy_then_unplug)
    result = copier.copy_job(job, phase="all", timeout=2, reconnect=POLICY, diskutil=disk.fake)
    assert result.reconnects == 2 and result.paused is None
    assert all(row["status"] == "copied" and row["attempts"] == 1 for row in file_rows(job).values())


def test_auto_reconnect_cannot_be_combined_with_path(rig):
    job, disk, _ = rig
    with pytest.raises(RescueError, match="--auto-reconnect cannot be combined with --path"):
        copier.copy_job(job, phase="all", timeout=2, path="a", fallback_from="b", reconnect=POLICY, diskutil=disk.fake)


def test_restart_after_give_up_requires_acknowledgement(rig):
    job, disk, _ = rig
    conn = connect(job)
    record_event(conn, "gave-up", detail={"reason": "wait expired"})
    conn.close()
    disk.unplug()
    result = copier.copy_job(job, phase="all", timeout=2, reconnect=POLICY, diskutil=disk.fake)
    assert result.paused and result.reconnects == 0
    assert result.notes == ["auto-reconnect disabled until --acknowledge-storage-loss"]
    assert disk.fake.calls == [] and disk.fake.info_calls == []
    result = copier.copy_job(job, phase="all", timeout=2, reconnect=POLICY, diskutil=disk.fake,
                             acknowledge_storage_loss=True)
    assert result.paused is None and result.copied == 3 and result.reconnects == 1
    assert kinds(job)[:3] == ["gave-up", "acknowledged", "lost"]


def test_give_up_pauses_with_escalation_and_blocks(rig):
    job, disk, _ = rig
    disk.unplug()
    disk.fake.volumes["SRC"].present = False
    result = copier.copy_job(job, phase="all", timeout=2, diskutil=disk.fake,
                             reconnect=ReconnectPolicy(cooldown=0, wait=0.05, poll=0.01))
    assert result.paused and "ddrescue" in result.paused
    snapshot = activity_snapshot(job)
    assert snapshot["state"] == "paused" and "ddrescue" in snapshot["reason"]
    conn = connect(job)
    try:
        assert blocked_by_give_up(conn)
    finally:
        conn.close()


def test_cli_flags_and_exit_code(tmp_path):
    for name in ("a", "b"):
        write_file(tmp_path / "source-home" / name, b"hello")
    job, source, _ = init_and_scan(tmp_path)
    seed_identities(job, source_uuid=None, dest_uuid=None)
    source.rename(source.with_name("gone"))
    result = run_cli("copy", "--job-dir", str(job), "--auto-reconnect", "--reconnect-cooldown", "0",
                     "--reconnect-wait", "0.1", "--max-reconnects", "2", check=False)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "paused=" in result.stdout
    assert "state=paused" in run_cli("status", "--job-dir", str(job)).stdout
    assert kinds(job)[-1] == "gave-up"
    again = run_cli("resume", "--job-dir", str(job), "--auto-reconnect", "--reconnect-wait", "0.1", check=False)
    assert again.returncode == 3 and "note: auto-reconnect disabled until --acknowledge-storage-loss" in again.stdout


def test_cli_rejects_negative_cooldown(tmp_path):
    result = run_cli("copy", "--job-dir", str(tmp_path), "--auto-reconnect", "--reconnect-cooldown", "-1", check=False)
    assert result.returncode == 2


def spawn(*args, script=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    command = [sys.executable, str(script)] if script else [sys.executable, "-m", "macos_data_rescue"]
    return subprocess.Popen([*command, *args], cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for_status(job, needle, process, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, process.communicate()
        status = run_cli("status", "--job-dir", str(job)).stdout
        if needle in status:
            return status
        time.sleep(0.1)
    process.kill()
    raise AssertionError(f"{needle!r} never appeared in status:\n{status}")


def waiting_run(tmp_path):
    for name in ("a", "b"):
        write_file(tmp_path / "source-home" / name, b"hello")
    job, source, _ = init_and_scan(tmp_path)
    seed_identities(job, source_uuid=None, dest_uuid=None)
    source.rename(source.with_name("gone"))
    process = spawn("copy", "--job-dir", str(job), "--auto-reconnect", "--reconnect-cooldown", "30")
    return job, process


def test_sigterm_while_waiting_exits_3_and_records_stopped(tmp_path):
    job, process = waiting_run(tmp_path)
    try:
        wait_for_status(job, "state=waiting", process)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 3, stdout + stderr
    assert "stopped while waiting for storage" in stdout
    assert kinds(job)[-1] == "stopped"
    conn = connect(job)
    try:
        assert not blocked_by_give_up(conn)
    finally:
        conn.close()
    assert activity_snapshot(job)["state"] == "paused"


def test_status_shows_waiting(tmp_path):
    job, process = waiting_run(tmp_path)
    try:
        status = wait_for_status(job, "state=waiting", process)
    finally:
        process.terminate()
        process.communicate(timeout=15)
    assert "attempt=1" in status and "wait_phase=cooldown" in status and "wait_until=" in status


SLOW_COPY = """
import sys
import time

from macos_data_rescue import cli, copier


def slow(*args, **kwargs):
    time.sleep(60)


if __name__ == "__main__":  # the spawn copy worker re-imports this module
    copier.copy_one_with_timeout = slow
    sys.exit(cli.main(sys.argv[1:]))
"""


def test_sigterm_during_copy_pass_keeps_interrupted_path(tmp_path):
    write_file(tmp_path / "source-home" / "a", b"hello")
    job, _, _ = init_and_scan(tmp_path)
    seed_identities(job, source_uuid=None, dest_uuid=None)
    script = tmp_path / "slow_copy.py"
    script.write_text(SLOW_COPY)
    process = spawn("copy", "--job-dir", str(job), "--auto-reconnect", script=script)
    try:
        wait_for_status(job, 'file="a"', process)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 130, stdout + stderr
    assert activity_snapshot(job)["state"] == "interrupted"
    assert "stopped" not in kinds(job)
