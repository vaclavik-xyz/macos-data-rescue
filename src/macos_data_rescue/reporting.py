from __future__ import annotations

import json
from pathlib import Path

from .manifest import all_files, load_config, migrate_manifest, status_summary


STATUSES = ("pending", "copying", "copied", "failed", "timed_out", "skipped")
WARNINGS = (
    {
        "code": "icloud_dataless_placeholders",
        "title": "iCloud Optimize Mac Storage placeholders",
        "message": (
            "Files offloaded by iCloud Drive or Photos Optimize Mac Storage may be "
            "dataless placeholders on a mounted source volume. They can copy as "
            "empty or tiny files and cannot be downloaded from Share Disk / Target "
            "Disk Mode; verify suspicious zero-byte/tiny results with the customer."
        ),
    },
    {
        "code": "metadata_best_effort",
        "title": "macOS metadata is best-effort",
        "message": (
            "Extended attributes and resource forks are copied best-effort. "
            "Quarantine and MAC labels are skipped deliberately; any xattr copy "
            "failure is reported as a per-file warning, but copied content does "
            "not guarantee complete metadata preservation."
        ),
    },
    {
        "code": "scan_timeout_is_cooperative",
        "title": "Scan timeout is cooperative",
        "message": (
            "Scan commits manifest rows in batches and can stop at --timeout or "
            "--limit between files. It still walks and stats the mounted source "
            "directly, so a severe disk/kernel I/O hang inside one filesystem call "
            "can still stall scan."
        ),
    },
)


def status_text(job_dir: Path) -> str:
    load_config(job_dir)
    migrate_manifest(job_dir)
    summary = normalized_summary(job_dir)
    return " ".join(f"{status}={summary[status]['count']}" for status in STATUSES)


def report(job_dir: Path, report_format: str) -> str:
    if report_format == "json":
        return json.dumps(report_payload(job_dir), indent=2, sort_keys=True) + "\n"
    return markdown_report(job_dir)


def report_payload(job_dir: Path) -> dict[str, object]:
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    rows = all_files(job_dir)
    return {
        "job": {
            "job_dir": str(job_dir),
            "source": str(config.source),
            "dest": str(config.dest),
            "profile": config.profile,
        },
        "summary": normalized_summary(job_dir),
        "warnings": list(WARNINGS),
        "files": [row_to_dict(row) for row in rows],
    }


def markdown_report(job_dir: Path) -> str:
    payload = report_payload(job_dir)
    lines = [
        "# macOS Data Rescue Report",
        "",
        f"Source: `{payload['job']['source']}`",
        f"Destination: `{payload['job']['dest']}`",
        "",
        "## Summary",
        "",
        "| Status | Count | Bytes |",
        "| --- | ---: | ---: |",
    ]
    summary = payload["summary"]
    assert isinstance(summary, dict)
    for status in STATUSES:
        item = summary[status]
        lines.append(f"| {status} | {item['count']} | {item['bytes']} |")
    lines.extend(["", "## Important warnings", ""])
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    for warning in warnings:
        lines.append(f"- **{warning['title']}**: {warning['message']}")
    lines.extend(["", "## Files", ""])
    files = payload["files"]
    assert isinstance(files, list)
    if not files:
        lines.append("No files scanned.")
    for item in files:
        error = f" - {item['error']}" if item["error"] else ""
        copied = (
            f" - copied {item['copied_bytes']}/{item['size']} bytes"
            if 0 < item["copied_bytes"] != item["size"]
            else ""
        )
        warning = f" - WARNING: {item['warning']}" if item["warning"] else ""
        lines.append(
            f"- `{item['relative_path']}` - {item['status']} - {item['size']} bytes{copied}{error}{warning}"
        )
    return "\n".join(lines) + "\n"


def normalized_summary(job_dir: Path) -> dict[str, dict[str, int]]:
    summary = status_summary(job_dir)
    return {status: summary.get(status, {"count": 0, "bytes": 0}) for status in STATUSES}


def row_to_dict(row) -> dict[str, object]:
    item = {
        "relative_path": row["relative_path"],
        "size": row["size"],
        "mtime_ns": row["mtime_ns"],
        "kind": row["kind"],
        "phase": row["phase"],
        "status": row["status"],
        "attempts": row["attempts"],
        "error": row["error"],
        "warning": row["warning"],
        "copied_bytes": row["copied_bytes"],
    }
    if "source_path" in row.keys() and row["source_path"]:
        item["source_path"] = row["source_path"]
    return item
