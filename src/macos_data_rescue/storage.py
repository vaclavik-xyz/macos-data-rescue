"""Conservative mount-loss guards; all probes are read-only."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from .volumes import Diskutil, volume_identity


class StoragePaused(Exception):
    """Storage became unavailable; the active file was returned to pending.

    `reconnectable` is False for a full destination (ENOSPC/EDQUOT): waiting
    for a disk cannot fix it. `file_id` names the row being copied, if any.
    """

    def __init__(self, message: str, *, reconnectable: bool = True, file_id: int | None = None) -> None:
        super().__init__(message)
        self.reconnectable = reconnectable
        self.file_id = file_id


def anchor(path: Path) -> dict:
    current = path
    while not current.exists():
        if current == current.parent:
            raise OSError(f"no existing ancestor for {path}")
        current = current.parent
    info = current.stat()
    return dict(path=str(current), inode=info.st_ino)


def storage_anchors(source: Path, dest: Path) -> dict:
    return dict(source=anchor(source), destination=anchor(dest))


def probe_storage(anchors: dict) -> dict:
    try:
        for label, item in anchors.items():
            path = Path(item["path"])
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_ino != item["inode"]:
                raise OSError(f"{label} directory changed: {path}; reconnect the original disk")
        return {"status": "available"}
    except OSError as exc:
        return {"status": "paused", "error": f"Storage unavailable: {exc}"}


def load_anchors(conn, source: Path, dest: Path) -> dict:
    row = conn.execute("select value from config where key = 'storage_anchors'").fetchone()
    if row:
        return json.loads(row[0])
    # Upgrade older jobs only while source and destination parent are available.
    if not source.is_dir():
        raise StoragePaused(f"Source is disconnected: {source}")
    result = storage_anchors(source, dest)
    conn.execute("insert into config values('storage_anchors', ?)", (json.dumps(result),))
    conn.commit()
    return result


def stored_volume_identities(conn) -> dict | None:
    row = conn.execute("select value from config where key = 'volume_identities'").fetchone()
    return json.loads(row[0]) if row else None


def wait_only_identities() -> dict:
    """Identities for a run without a recorded volume: only wait, never remount."""
    unknown = dict(uuid=None, mount_point=None, fs_type=None, network=False, read_only=False)
    return {"source": dict(unknown), "destination": dict(unknown)}


def load_volume_identities(conn, source: Path, dest: Path, diskutil: Diskutil) -> dict:
    """Record the source/destination volume identity once, like storage_anchors.

    Call only right after a successful storage probe. An identity is persisted
    only when every role resolved; a local volume whose diskutil lookup failed
    gets a transient wait-only identity for this run and is retried next time.
    A stored identity is never overwritten.
    """
    stored = stored_volume_identities(conn)
    if stored is not None:
        return stored
    result = {"source": volume_identity(source, diskutil), "destination": volume_identity(dest, diskutil)}
    if all(item["uuid"] is not None or item["network"] for item in result.values()):
        conn.execute("insert or ignore into config values('volume_identities', ?)", (json.dumps(result),))
        conn.commit()
        return stored_volume_identities(conn) or result
    return result
