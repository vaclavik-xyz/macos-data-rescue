"""Thin, timeout-bounded adapter around macOS `diskutil`.

Only three verbs are ever issued: `info -plist`, `mount [readOnly]` and plain
`unmount`. A forced unmount is never constructed. A timeout, a non-zero exit or
unparsable output means "not available yet"; nothing here raises into the
copy loop. Tests replace the runner (or the whole class) and never reach the
real binary.
"""
from __future__ import annotations

import plistlib
# only fixed diskutil argv lists are executed, never through a shell
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str], float], tuple[int, bytes]]

NETWORK_FS = frozenset({"smbfs", "afpfs", "nfs", "webdav"})
TIMED_OUT = 124
# Grace period for a killed diskutil to exit before it is abandoned.
KILL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class VolumeInfo:
    uuid: str | None
    mount_point: str | None
    fs_type: str | None
    writable: bool | None
    locked: bool


def default_runner(argv: list[str], timeout: float) -> tuple[int, bytes]:
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,  # nosec B603 - fixed argv, no shell
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return TIMED_OUT, b""
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass  # abandon it; a hung diskutil must never block the job
        return TIMED_OUT, b""
    except BaseException:
        process.kill()
        raise
    return process.returncode, stdout or b""


class Diskutil:
    def __init__(self, run: Runner = default_runner, timeout: float = 60.0) -> None:
        self.run = run
        self.timeout = timeout

    def _call(self, *args: str) -> tuple[int, bytes]:
        argv = ["diskutil", *args]
        if any(arg == "force" for arg in argv):
            raise ValueError("forced diskutil operations are never issued")
        return self.run(argv, self.timeout)

    def info(self, target: str) -> VolumeInfo | None:
        if not valid_target(target):
            return None
        code, output = self._call("info", "-plist", target)
        if code != 0:
            return None
        try:
            data = plistlib.loads(output)
        except Exception:  # malformed output is "not available yet"
            return None
        if not isinstance(data, dict):
            return None
        mount_point = data.get("MountPoint") or None
        writable = data.get("WritableVolume")
        if "Locked" in data:
            locked = bool(data["Locked"])
        else:
            locked = bool(data.get("FileVault")) and mount_point is None
        return VolumeInfo(
            uuid=str(data["VolumeUUID"]) if data.get("VolumeUUID") else None,
            mount_point=str(mount_point) if mount_point else None,
            fs_type=str(data["FilesystemType"]) if data.get("FilesystemType") else None,
            writable=writable if isinstance(writable, bool) else None,
            locked=locked,
        )

    def mount(self, uuid: str, *, read_only: bool) -> bool:
        if not valid_target(uuid):
            return False
        args = ("mount", "readOnly", uuid) if read_only else ("mount", uuid)
        return self._call(*args)[0] == 0

    def unmount(self, uuid: str) -> bool:
        if not valid_target(uuid):
            return False
        return self._call("unmount", uuid)[0] == 0


def valid_target(target: object) -> bool:
    # A recorded value must never be interpreted as a diskutil option or verb.
    return (isinstance(target, str) and bool(target) and not target.startswith("-")
            and target != "force" and "\0" not in target)


def volume_identity(path: Path, diskutil: Diskutil) -> dict:
    """Identity of the volume holding `path`.

    Call only while the storage probe has just succeeded: on a vanished mount,
    the mount-point walk would climb to the boot volume.
    """
    from .preflight import find_mount_point

    mount_point = find_mount_point(path)
    info = diskutil.info(mount_point)
    fs_type = info.fs_type if info else None
    network = fs_type in NETWORK_FS
    return {
        "uuid": info.uuid if info and not network else None,
        "mount_point": (info.mount_point if info and info.mount_point else mount_point),
        "fs_type": fs_type,
        "network": network,
        "read_only": bool(info and info.writable is False),
    }
