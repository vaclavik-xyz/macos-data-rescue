"""Suggest alternatives from recorded metadata without reading damaged files."""
from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath

from .copier import safe_relative_parts
from .errors import RescueError
from .manifest import COPIED_STATUSES, UNREADABLE_COMPRESSED, connect


NOTICE = ("Candidates are metadata matches, not proof of identical contents. No source files were read. "
          "Inspect the proposed duplicate and explicitly select --fallback-from; nothing is copied automatically.")


def duplicate_candidates(job_dir: Path, path: str, *, limit: int = 20) -> dict:
    conn = connect(job_dir)
    try:
        target = conn.execute("select * from files where relative_path = ?", (path,)).fetchone()
        if target is None or target["kind"] != "file" or target["status"] not in ("failed", "timed_out", UNREADABLE_COMPRESSED):
            raise RescueError("--path must name a failed, timed-out, or unreadable compressed regular file")
        source = Path(conn.execute("select value from config where key = 'source'").fetchone()[0])
        original_source = (Path(target["source_path"]) if "source_path" in target.keys() and target["source_path"]
                           else source / path)
        rows = conn.execute("select * from files where kind = 'file' and size = ? and id != ? order by relative_path",
                            (target["size"], target["id"]))
        candidates = {}
        for row in rows:
            relative = row["relative_path"]
            if "fallback_source_path" in row.keys() and row["fallback_source_path"]:
                relative = row["fallback_source_path"]
            elif "source_path" in row.keys() and row["source_path"]:
                try:
                    relative = Path(row["source_path"]).relative_to(source).as_posix()
                except ValueError:
                    continue  # The fallback command only accepts paths inside the selected source.
            try:
                safe_relative_parts(relative)
            except ValueError:
                continue
            if source / relative == original_source:
                continue
            score = 0
            evidence = ["same recorded size"]
            name = PurePosixPath(relative).name
            if name.casefold() == PurePosixPath(path).name.casefold():
                score += 20
                evidence.append("same filename (case-insensitive)")
            elif PurePosixPath(name).suffix.casefold() == PurePosixPath(path).suffix.casefold():
                score += 5
                evidence.append("same extension")
            if row["status"] in COPIED_STATUSES:
                score += 2
                evidence.append("a copy of this candidate completed previously")
            item = dict(path=relative, size=row["size"], recorded_status=row["status"],
                        evidence=evidence, score=score,
                        command=shlex.join(["macos-data-rescue", "copy", "--job-dir", str(job_dir),
                                           "--path", path, "--fallback-from", relative]))
            if "sha256" in row.keys() and row["sha256"]:
                item["candidate_copy_sha256"] = row["sha256"]
            if relative not in candidates or candidates[relative]["score"] < score:
                candidates[relative] = item
    finally:
        conn.close()
    ordered = sorted(candidates.values(), key=lambda item: (-item["score"], item["path"]))
    return dict(path=path, notice=NOTICE, total_candidates=len(ordered), candidates=ordered[:limit])


def candidates_text(payload: dict, format: str = "text") -> str:
    if format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    lines = [payload["notice"], f"candidates={payload['total_candidates']}"]
    for item in payload["candidates"]:
        lines.extend([f"- {json.dumps(item['path'])}: {', '.join(item['evidence'])}",
                      f"  Explicit selection: {item['command']}"])
    return "\n".join(lines) + "\n"
