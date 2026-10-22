import json
import plistlib
from pathlib import Path

from fakes import FakeDiskutil
from helpers import config_value, write_file
from macos_data_rescue.manifest import connect, init_manifest
from macos_data_rescue.preflight import find_mount_point
from macos_data_rescue.storage import load_volume_identities
from macos_data_rescue.volumes import Diskutil, VolumeInfo, volume_identity

# Synthetic `diskutil info -plist` output. The keys mirror those observed on
# macOS 26 with `diskutil info -plist /` (2026-10-22); every value is invented.
FIXTURE = Path(__file__).parent / "fixtures" / "diskutil_info_apfs.plist"


class Recorder:
    def __init__(self, *results):
        self.results = list(results)
        self.argv = []

    def __call__(self, argv, timeout):
        self.argv.append(list(argv))
        return self.results.pop(0) if self.results else (0, b"")


def plist(**overrides):
    data = plistlib.loads(FIXTURE.read_bytes())
    data.update(overrides)
    return plistlib.dumps(data)


def test_info_parses_plist():
    runner = Recorder((0, FIXTURE.read_bytes()))
    assert Diskutil(runner).info("/Volumes/Data") == VolumeInfo("A-1", "/Volumes/Data", "apfs", False, False)
    assert runner.argv == [["diskutil", "info", "-plist", "/Volumes/Data"]]


def test_info_unmounted_volume_has_no_mount_point():
    info = Diskutil(Recorder((0, plist(MountPoint="")))).info("A-1")
    assert info is not None and info.mount_point is None


def test_info_returns_none_on_timeout_and_error():
    assert Diskutil(Recorder((124, b""))).info("A-1") is None
    assert Diskutil(Recorder((1, b""))).info("A-1") is None
    assert Diskutil(Recorder((0, b"not a plist"))).info("A-1") is None


def test_mount_source_uses_read_only_and_never_force():
    runner = Recorder((0, b""), (0, b""))
    diskutil = Diskutil(runner)
    assert diskutil.mount("A-1", read_only=True)
    assert diskutil.unmount("A-1")
    assert runner.argv == [["diskutil", "mount", "readOnly", "A-1"], ["diskutil", "unmount", "A-1"]]
    assert not any("force" in arg for argv in runner.argv for arg in argv)


def test_volume_operations_reject_option_like_targets():
    runner = Recorder()
    diskutil = Diskutil(runner)
    assert not diskutil.unmount("force")
    assert not diskutil.mount("-x", read_only=True)
    assert runner.argv == []


def test_network_volume_identity_has_no_uuid(tmp_path):
    fake = FakeDiskutil()
    fake.add("NET-1", mount_point=find_mount_point(tmp_path), fs_type="smbfs")
    identity = volume_identity(tmp_path, fake)
    assert identity["uuid"] is None and identity["network"] is True


def job(tmp_path):
    write_file(tmp_path / "source" / "a", b"a")
    init_manifest(tmp_path / "job", tmp_path / "source", tmp_path / "dest", "volume")
    return tmp_path / "job"


def test_failed_lookup_on_local_volume_is_not_persisted(tmp_path):
    job_dir = job(tmp_path)
    conn = connect(job_dir)
    try:
        identities = load_volume_identities(conn, tmp_path / "source", tmp_path / "dest", FakeDiskutil())
        assert identities["source"]["uuid"] is None and identities["source"]["network"] is False
        assert config_value(job_dir, "volume_identities") is None
        working = FakeDiskutil()
        working.add("LOCAL-1", mount_point=find_mount_point(tmp_path))
        identities = load_volume_identities(conn, tmp_path / "source", tmp_path / "dest", working)
    finally:
        conn.close()
    assert identities["source"]["uuid"] == "LOCAL-1"
    assert json.loads(config_value(job_dir, "volume_identities"))["destination"]["uuid"] == "LOCAL-1"


def test_identities_recorded_once(tmp_path):
    job_dir = job(tmp_path)
    fake = FakeDiskutil()
    volume = fake.add("FIRST", mount_point=find_mount_point(tmp_path))
    conn = connect(job_dir)
    try:
        first = load_volume_identities(conn, tmp_path / "source", tmp_path / "dest", fake)
        volume.uuid = "SECOND"
        fake.volumes = {"SECOND": volume}
        second = load_volume_identities(conn, tmp_path / "source", tmp_path / "dest", fake)
    finally:
        conn.close()
    assert first == second and second["source"]["uuid"] == "FIRST"
