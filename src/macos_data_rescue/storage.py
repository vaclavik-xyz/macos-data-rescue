"""Conservative mount-loss guards; all probes are read-only."""
from __future__ import annotations

import json
import stat
from pathlib import Path


class StoragePaused(Exception):
    pass


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
