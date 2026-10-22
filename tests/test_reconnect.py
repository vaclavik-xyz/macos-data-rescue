import time

import pytest

from fakes import FakeDiskutil
from helpers import write_file
from macos_data_rescue.manifest import connect, init_manifest
from macos_data_rescue.reconnect import ReconnectGaveUp, Reconnector, ReconnectPolicy
from macos_data_rescue.storage import StoragePaused
from macos_data_rescue.storage_events import list_events, record_event

POLICY = ReconnectPolicy(cooldown=0, wait=0.2, poll=0.01)
SOURCE = "/Volumes/Data"
DEST = "/Volumes/Dest"


class Rig:
    def __init__(self, tmp_path):
        write_file(tmp_path / "source" / "a", b"a")
        init_manifest(tmp_path / "job", tmp_path / "source", tmp_path / "dest", "volume")
        self.conn = connect(tmp_path / "job")
        self.anchor = tmp_path / "anchor"
        self.anchor.mkdir()
        self.unplugged = tmp_path / "unplugged"
        self.fake = FakeDiskutil(on_mount=lambda volume: self.plug())
        self.identities = {
            "source": dict(uuid="SRC", mount_point=SOURCE, fs_type="apfs", network=False, read_only=False),
            "destination": dict(uuid="DST", mount_point=DEST, fs_type="apfs", network=False, read_only=False),
        }
        self.fake.add("DST", mount_point=DEST, writable=True)
        self.sleeps = []

    def unplug(self):
        self.anchor.rename(self.unplugged)

    def plug(self):
        if self.unplugged.exists():
            self.unplugged.rename(self.anchor)

    def probe(self):
        if not self.anchor.is_dir():
            raise StoragePaused(f"Storage unavailable: {self.anchor}")

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        time.sleep(seconds)

    def reconnector(self, policy=POLICY, sleep=None):
        return Reconnector(self.conn, self.identities, self.probe, policy, self.fake, sleep=sleep or self.sleep)

    def kinds(self):
        return [event["kind"] for event in list_events(self.conn)]


@pytest.fixture
def rig(tmp_path):
    result = Rig(tmp_path)
    yield result
    result.conn.close()


def test_volume_back_unmounted_is_mounted_read_only_and_resumes(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=None, landing=SOURCE)
    rig.reconnector().recover("Storage unavailable", 0)
    assert ("mount", "SRC", True) in rig.fake.calls
    assert rig.kinds()[-2:] == ["remounted", "resumed"]
    assert rig.kinds()[:2] == ["lost", "cooldown"]


def test_source_back_writable_is_remounted_read_only(rig):
    rig.fake.add("SRC", mount_point=SOURCE, writable=True)
    rig.reconnector().recover("Storage unavailable", 0)
    assert rig.fake.calls == [("unmount", "SRC"), ("mount", "SRC", True)]
    assert rig.fake.volumes["SRC"].writable is False


def test_mounted_elsewhere_unmount_fails_gives_up(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=SOURCE + " 1")
    rig.fake.unmount_ok = False
    with pytest.raises(ReconnectGaveUp, match="operator decision"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert not any(call[0] == "mount" for call in rig.fake.calls)
    assert rig.kinds()[-1] == "gave-up"


def test_mounted_elsewhere_lands_elsewhere_again_gives_up(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=SOURCE + " 1", landing=SOURCE + " 1")
    with pytest.raises(ReconnectGaveUp, match="operator decision"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert rig.fake.calls == [("unmount", "SRC"), ("mount", "SRC", True)]


def test_destination_is_never_unmounted(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=SOURCE)
    rig.fake.volumes["DST"].mount_point = DEST + " 1"
    with pytest.raises(ReconnectGaveUp, match="operator decision"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert ("unmount", "DST") not in rig.fake.calls


def test_destination_back_unmounted_is_mounted_writable(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=SOURCE)
    rig.fake.volumes["DST"].mount_point = None
    rig.reconnector().recover("Storage unavailable", 0)
    assert rig.fake.calls == [("mount", "DST", False)]


def test_volume_never_returns_gives_up_after_wait(rig):
    rig.unplug()
    with pytest.raises(ReconnectGaveUp, match="ddrescue"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert rig.kinds()[-1] == "gave-up"
    assert "resumed" not in rig.kinds()


def test_stale_mount_reason(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=SOURCE, writable=False)
    rig.fake.on_mount = None
    with pytest.raises(ReconnectGaveUp, match="stale mount"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert rig.fake.calls == []


def test_limit_breach_gives_up_before_cooldown(rig):
    now = time.time()
    for offset in (300, 200, 100):
        record_event(rig.conn, "lost", now=now - offset)
    sleeps = []
    with pytest.raises(ReconnectGaveUp, match="60 minutes"):
        rig.reconnector(policy=ReconnectPolicy(cooldown=5, wait=5, poll=1), sleep=sleeps.append).recover("lost", 0)
    assert sleeps == [] and rig.fake.calls == []
    assert rig.kinds()[-2:] == ["lost", "gave-up"]


def test_network_volume_is_only_waited_for(rig):
    rig.identities["source"] = dict(uuid=None, mount_point=None, fs_type="smbfs", network=True, read_only=False)
    rig.identities["destination"] = dict(uuid=None, mount_point=None, fs_type=None, network=False, read_only=False)
    rig.unplug()

    def sleep(seconds):
        rig.plug()

    rig.reconnector(sleep=sleep).recover("Storage unavailable", 0)
    assert rig.fake.calls == [] and rig.fake.info_calls == []
    assert rig.kinds()[-1] == "resumed" and "remounted" not in rig.kinds()


def test_volume_outside_volumes_is_never_remounted(rig):
    rig.identities["source"] = dict(uuid="SRC", mount_point="/System/Volumes/Data", fs_type="apfs",
                                    network=False, read_only=False)
    rig.fake.add("SRC", mount_point="/System/Volumes/Data", writable=True)
    rig.reconnector().recover("Storage unavailable", 0)
    assert rig.fake.calls == []


def test_locked_volume_gives_up(rig):
    rig.unplug()
    rig.fake.add("SRC", mount_point=None, locked=True)
    with pytest.raises(ReconnectGaveUp, match="unlock"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert rig.fake.calls == []


def test_different_volume_at_recorded_mount_point_gives_up(rig):
    rig.fake.add("OTHER", mount_point=SOURCE)
    with pytest.raises(ReconnectGaveUp, match="different volume"):
        rig.reconnector().recover("Storage unavailable", 0)
    assert "resumed" not in rig.kinds()


def test_wait_callback_reports_cooldown_and_attempt(rig):
    seen = []
    rig.fake.add("SRC", mount_point=SOURCE)
    Reconnector(rig.conn, rig.identities, rig.probe, ReconnectPolicy(cooldown=0.02, wait=0.2, poll=0.01),
                rig.fake, on_wait=seen.append).recover("Storage unavailable", 0)
    assert [item["phase"] for item in seen] == ["cooldown", "waiting"]
    assert seen[0]["attempt"] == 1 and seen[0]["reason"] == "Storage unavailable"
    assert seen[0]["until"] > time.time() - 1


def test_full_destination_pause_is_not_reconnectable(tmp_path, monkeypatch):
    import errno

    from helpers import init_and_scan
    from macos_data_rescue import copier

    write_file(tmp_path / "source-home" / "a", b"a")
    job, source, dest = init_and_scan(tmp_path)
    monkeypatch.setattr(copier, "copy_one_with_timeout", lambda *args, **kwargs: dict(
        status="failed", error="ENOSPC", pause_reason=copier.storage_error_reason(OSError(errno.ENOSPC, "full"))))
    conn = connect(job)
    try:
        row = conn.execute("select * from files").fetchone()
        with pytest.raises(StoragePaused) as raised:
            copier.process_row(job, source, dest, row, 2, copier.CopySummary(), conn=conn)
    finally:
        conn.close()
    assert raised.value.reconnectable is False
