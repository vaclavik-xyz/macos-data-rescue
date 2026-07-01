from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .errors import RescueError
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


def write_customer_report(job_dir: Path, report_format: str, output_path: Path | None = None) -> Path:
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    if output_path is None:
        suffix = "pdf" if report_format == "pdf" else "md"
        output_path = config.dest.parent / f"recovery-report.{suffix}"
    output_path = output_path.resolve(strict=False)
    if output_path == config.source or config.source in output_path.parents:
        raise RescueError(f"customer report output must not be inside source: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = customer_markdown_report(job_dir)
    if report_format == "markdown":
        atomic_write_bytes(output_path, markdown.encode())
    elif report_format == "pdf":
        atomic_write_bytes(output_path, simple_pdf_bytes(customer_text_lines(markdown)))
    else:
        raise RescueError(f"unsupported customer report format: {report_format}")
    return output_path


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


def customer_markdown_report(job_dir: Path) -> str:
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    summary = normalized_summary(job_dir)
    copied_bytes = summary["copied"]["bytes"]
    failed = summary["failed"]["count"]
    timed_out = summary["timed_out"]["count"]
    pending = summary["pending"]["count"]
    copying = summary["copying"]["count"]
    unresolved = failed + timed_out + pending + copying
    lines = [
        "# Data Recovery Report",
        "",
        f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "## Summary",
        "",
        f"Recovery source: `{config.source}`  ",
        f"Recovery destination: `{config.dest}`",
        "",
    ]
    if unresolved:
        lines.append("The recovery completed with unresolved files recorded by the rescue tool.")
    else:
        lines.append("The recovery completed with no failed or timed-out files recorded by the rescue tool.")
    lines.extend(
        [
            "",
            "| Result | Count |",
            "| --- | ---: |",
            f"| Copied files | {summary['copied']['count']} |",
            f"| Copying files | {copying} |",
            f"| Failed files | {failed} |",
            f"| Timed-out files | {timed_out} |",
            f"| Pending files | {pending} |",
            f"| Skipped entries | {summary['skipped']['count']} |",
            "",
            f"Copied data recorded in the manifest: {format_gib(copied_bytes)}  ",
            "",
            "## What Was Recovered",
            "",
            (
                "The recovered folder contains the customer's selected home-folder data, "
                "including visible home folders, hidden home-folder items, and any selected "
                "application data phases that were copied for this job."
            ),
            "",
            "The destination folder is:",
            "",
            f"`{config.dest}`",
            "",
            "## Items Not Copied",
            "",
        ]
    )
    if unresolved:
        lines.append(
            "Some files were not fully copied yet or were recorded as failed/timed out. "
            "See the detailed technician reports for exact paths and errors."
        )
    else:
        lines.append("No files are recorded as failed or timed out.")
    lines.extend(
        [
            "",
            (
                "Skipped entries are usually symbolic links. The tool deliberately skips "
                "symbolic links so it does not follow links outside the selected source tree."
            ),
            "",
            "## Important Notes",
            "",
            (
                "- A copied status means the rescue tool copied the file content and verified "
                "that the copied byte count matched the expected file size recorded from the source."
            ),
            (
                "- macOS metadata such as extended attributes and resource forks is preserved "
                "on a best-effort basis. Some system attributes, including quarantine and MAC "
                "labels, are intentionally not restored."
            ),
            (
                "- If any files were iCloud placeholders on the source Mac, the data may not "
                "have been physically present on disk. Such files may require export from a "
                "live signed-in Mac or iCloud account."
            ),
            "- This is a practical file-level recovery copy, not a forensic disk image.",
            "",
            "## Detailed Reports",
            "",
            "Detailed technician reports can be generated with:",
            "",
            "`macos-data-rescue report --job-dir JOB --format markdown`",
            "`macos-data-rescue report --job-dir JOB --format json`",
        ]
    )
    return "\n".join(lines) + "\n"


def customer_text_lines(markdown: str) -> list[str]:
    lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("# "):
            lines.append(line[2:])
        elif line.startswith("## "):
            lines.append("")
            lines.append(line[3:])
        elif line.startswith("| ---"):
            continue
        elif line.startswith("| ") and line.endswith(" |"):
            parts = [part.strip() for part in line.strip("|").split("|")]
            if len(parts) == 2:
                lines.append(f"{parts[0]}: {parts[1]}")
            else:
                lines.append("  ".join(parts))
        elif line.startswith("- "):
            lines.append(f"* {line[2:]}")
        elif line.startswith("`") and line.endswith("`"):
            lines.append(line.strip("`"))
        else:
            lines.append(line.replace("`", ""))
    return lines


def write_simple_pdf(path: Path, lines: list[str]) -> None:
    atomic_write_bytes(path, simple_pdf_bytes(lines))


def simple_pdf_bytes(lines: list[str]) -> bytes:
    page_width = 595
    page_height = 842
    left = 50
    top = 790
    leading = 14
    max_chars = 92
    wrapped = wrap_pdf_lines(lines, max_chars)
    lines_per_page = max(1, int((top - 50) / leading))
    pages = [wrapped[index : index + lines_per_page] for index in range(0, len(wrapped), lines_per_page)]
    if not pages:
        pages = [[]]

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_object_ids = [4 + index * 2 for index in range(len(pages))]
    kids = b" ".join(f"{object_id} 0 R".encode("ascii") for object_id in page_object_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids.decode('ascii')}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, page_lines in enumerate(pages):
        page_id = page_object_ids[index]
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        stream = pdf_text_stream(page_lines, left, top, leading)
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".rescue-report-tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        fsync_directory(path.parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wrap_pdf_lines(lines: list[str], max_chars: int) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        current = line
        while len(current) > max_chars:
            split_at = current.rfind(" ", 0, max_chars + 1)
            if split_at <= 0:
                split_at = max_chars
            wrapped.append(current[:split_at])
            current = current[split_at:].lstrip()
        wrapped.append(current)
    return wrapped


def pdf_text_stream(lines: list[str], left: int, top: int, leading: int) -> bytes:
    commands = [
        "BT",
        "/F1 11 Tf",
        f"{leading} TL",
        f"{left} {top} Td",
    ]
    for line in lines:
        commands.append(f"({pdf_escape(line)}) Tj")
        commands.append("T*")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1")


def pdf_escape(text: str) -> str:
    safe = pdf_safe_text(text)
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_safe_text(text: str) -> str:
    safe_chars: list[str] = []
    for char in text:
        codepoint = ord(char)
        if codepoint < 32 and char != "\t":
            safe_chars.append(" ")
            continue
        try:
            char.encode("latin-1")
        except UnicodeEncodeError:
            if codepoint <= 0xFFFF:
                safe_chars.append(f"\\u{codepoint:04x}")
            else:
                safe_chars.append(f"\\U{codepoint:08x}")
        else:
            safe_chars.append(char)
    return "".join(safe_chars)


def format_gib(bytes_count: int) -> str:
    return f"{bytes_count / 1024 / 1024 / 1024:.1f} GiB"


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
