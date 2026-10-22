"""Test doubles. Tests must never reach the real diskutil or real volumes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from macos_data_rescue.volumes import Diskutil, VolumeInfo


def _refuse_real_diskutil(argv, timeout):
    raise AssertionError(f"tests must not run the real diskutil: {argv}")


@dataclass
class FakeVolume:
    uuid: str
    present: bool = True
    mount_point: str | None = None
    writable: bool = False
    locked: bool = False
    fs_type: str = "apfs"
    # where `mount` lands the volume; defaults to the first mount point seen
    landing: str | None = None


class FakeDiskutil(Diskutil):
    """In-memory diskutil: uuid -> volume state, with a call log.

    `calls` records only state-changing calls: ("mount", uuid, read_only) and
    ("unmount", uuid). Hooks run after a successful mount/unmount so tests can
    rename tmp directories to simulate a volume reappearing or vanishing.
    """

    def __init__(self, *, unmount_ok: bool = True, mount_ok: bool = True,
                 on_mount: Callable[[FakeVolume], None] | None = None,
                 on_unmount: Callable[[FakeVolume], None] | None = None) -> None:
        super().__init__(run=_refuse_real_diskutil)
        self.volumes: dict[str, FakeVolume] = {}
        self.calls: list[tuple] = []
        self.info_calls: list[str] = []
        self.unmount_ok = unmount_ok
        self.mount_ok = mount_ok
        self.on_mount = on_mount
        self.on_unmount = on_unmount

    def add(self, uuid: str, **state) -> FakeVolume:
        volume = FakeVolume(uuid, **state)
        if volume.landing is None:
            volume.landing = volume.mount_point
        self.volumes[uuid] = volume
        return volume

    def _find(self, target: str) -> FakeVolume | None:
        if target in self.volumes:
            return self.volumes[target]
        return next((volume for volume in self.volumes.values() if volume.mount_point == target), None)

    def info(self, target: str) -> VolumeInfo | None:
        self.info_calls.append(target)
        volume = self._find(target)
        if volume is None or not volume.present:
            return None
        return VolumeInfo(volume.uuid, volume.mount_point, volume.fs_type,
                          volume.writable if volume.mount_point else None, volume.locked)

    def mount(self, uuid: str, *, read_only: bool) -> bool:
        self.calls.append(("mount", uuid, read_only))
        volume = self.volumes.get(uuid)
        if not self.mount_ok or volume is None or not volume.present or volume.locked:
            return False
        volume.mount_point = volume.landing
        volume.writable = not read_only
        if self.on_mount is not None:
            self.on_mount(volume)
        return True

    def unmount(self, uuid: str) -> bool:
        self.calls.append(("unmount", uuid))
        volume = self.volumes.get(uuid)
        if not self.unmount_ok or volume is None or volume.mount_point is None:
            return False
        volume.mount_point = None
        if self.on_unmount is not None:
            self.on_unmount(volume)
        return True
