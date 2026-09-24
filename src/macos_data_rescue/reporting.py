from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from .activity import activity_snapshot, activity_text
from .errors import RescueError
from .manifest import scan_issues, COPIED_STATUSES, UNREADABLE_COMPRESSED, all_files, is_same_or_inside, load_config, migrate_manifest, status_summary, volume_root_for_home


STATUSES = ("pending", "copying", *COPIED_STATUSES, "failed", "timed_out", UNREADABLE_COMPRESSED, "skipped")
CUSTOMER_REPORT_LANGUAGES = ("en", "cs")
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
        "code": "scan_coverage",
        "title": "Scan coverage can be incomplete",
        "message": "Scan I/O runs in a bounded worker. Failed or timed-out paths are recorded separately; "
                   "unscanned subtrees have unknown contents. Initial path validation still depends on the OS.",
    },
)


@dataclass(frozen=True)
class CustomerReportText:
    title: str
    subtitle: str
    generated: str
    summary: str
    recovery_source: str
    recovery_destination: str
    complete_sentence: str
    unresolved_sentence: str
    result: str
    count: str
    copied_files: str
    copying_files: str
    fallback_files: str
    compressed_files: str
    compressed_note: str
    fallback_note: str
    failed_files: str
    timed_out_files: str
    pending_files: str
    skipped_entries: str
    copied_data_recorded: str
    what_recovered: str
    what_recovered_body: str
    destination_intro: str
    breakdown: str
    no_copied_content: str
    folder: str
    files: str
    size: str
    library_breakdown: str
    library_area: str
    items_not_copied: str
    unresolved_items: str
    no_failed_items: str
    skipped_note: str
    important_notes: str
    copied_note: str
    metadata_note: str
    icloud_note: str
    practical_note: str
    detailed_reports: str
    detailed_reports_intro: str
    status_complete_title: str
    status_unresolved_title: str
    status_complete_body: str
    status_unresolved_body: str
    copied_data_metric: str
    unresolved_metric: str
    locations: str
    recovered_data: str
    result_summary: str
    page: str


CUSTOMER_REPORT_TEXT = {
    "en": CustomerReportText(
        title="Data Recovery Report",
        subtitle="Customer handoff summary",
        generated="Generated",
        summary="Summary",
        recovery_source="Recovery source",
        recovery_destination="Recovery destination",
        complete_sentence="All recorded files have been processed. This does not certify complete source coverage.",
        unresolved_sentence="The recovery completed with unresolved files recorded by the rescue tool.",
        result="Result",
        count="Count",
        copied_files="Copied files",
        copying_files="Copying files",
        fallback_files="Recovered from an alternate source (included above)",
        compressed_files="Unreadable compressed files",
        compressed_note=("Unreadable compressed files failed with ENOTSUP: their compressed content is not "
                         "addressable on the mounted source and compression metadata is missing or unreadable. "
                         "This status does not diagnose bad sectors or an iCloud placeholder."),
        fallback_note=("Alternate-source recovery uses a technician-selected duplicate and verifies the byte count. "
                       "Equal size alone does not prove identical content; provenance is in the technician report."),
        failed_files="Failed files",
        timed_out_files="Timed-out files",
        pending_files="Pending files",
        skipped_entries="Skipped entries",
        copied_data_recorded="Copied data recorded in the manifest",
        what_recovered="What Was Recovered",
        what_recovered_body=(
            "The recovered folder contains the files copied from the selected source "
            "within this job's profile and exclusions. Empty directories and symbolic "
            "links are not recreated."
        ),
        destination_intro="The destination folder is:",
        breakdown="Recovered Data Breakdown",
        no_copied_content="No copied file content is recorded in the manifest yet.",
        folder="Folder",
        files="Files",
        size="Size",
        library_breakdown="Library Data Recovered",
        library_area="Library area",
        items_not_copied="Items Not Copied",
        unresolved_items=(
            "Some files were not fully copied yet or were recorded as failed/timed out. "
            "See the detailed technician reports for exact paths and errors."
        ),
        no_failed_items="No files are recorded as failed or timed out.",
        skipped_note=(
            "Skipped entries are usually symbolic links. The tool deliberately skips "
            "symbolic links so it does not follow links outside the selected source tree."
        ),
        important_notes="Important Notes",
        copied_note=(
            "A copied status means the rescue tool copied the file content and verified "
            "that the copied byte count matched the expected file size recorded from the source."
        ),
        metadata_note=(
            "macOS metadata such as extended attributes and resource forks is preserved "
            "on a best-effort basis. Some system attributes, including quarantine and MAC "
            "labels, are intentionally not restored."
        ),
        icloud_note=(
            "If any files were iCloud placeholders on the source Mac, the data may not "
            "have been physically present on disk. Such files may require export from a "
            "live signed-in Mac or iCloud account."
        ),
        practical_note="This is a practical file-level recovery copy, not a forensic disk image.",
        detailed_reports="Detailed Reports",
        detailed_reports_intro="Detailed technician reports can be generated with:",
        status_complete_title="Recorded files processed",
        status_unresolved_title="Recovery has unresolved files",
        status_complete_body="No failed, timed-out, pending, or interrupted files are recorded.",
        status_unresolved_body="Some files need technician review. See the detailed report for exact paths.",
        copied_data_metric="Copied data",
        unresolved_metric="Unresolved",
        locations="Recovery locations",
        recovered_data="Recovered data",
        result_summary="Result summary",
        page="Page",
    ),
    "cs": CustomerReportText(
        title="Zpráva o záchraně dat",
        subtitle="Souhrn pro zákazníka",
        generated="Vygenerováno",
        summary="Souhrn",
        recovery_source="Zdroj obnovy",
        recovery_destination="Cíl obnovy",
        complete_sentence="Všechny evidované soubory byly zpracovány. To nepotvrzuje úplnost prohledání zdroje.",
        unresolved_sentence="Záchrana obsahuje nedořešené soubory evidované nástrojem.",
        result="Výsledek",
        count="Počet",
        copied_files="Zkopírované soubory",
        copying_files="Rozpracované soubory",
        fallback_files="Obnoveno z náhradního zdroje (zahrnuto výše)",
        compressed_files="Nečitelné komprimované soubory",
        compressed_note=("Nečitelné komprimované soubory skončily chybou ENOTSUP: jejich obsah není na připojeném "
                         "zdroji dostupný a metadata komprese chybí nebo nejsou čitelná. "
                         "Tento stav neurčuje vadné sektory ani iCloud placeholder."),
        fallback_note=("Obnova z náhradního zdroje používá duplikát vybraný technikem a kontroluje počet bajtů. "
                       "Shodná velikost sama nedokazuje shodný obsah; původ kopie uvádí technický report."),
        failed_files="Neúspěšné soubory",
        timed_out_files="Soubory po timeoutu",
        pending_files="Čekající soubory",
        skipped_entries="Přeskočené položky",
        copied_data_recorded="Zkopírovaná data evidovaná v manifestu",
        what_recovered="Co bylo zachráněno",
        what_recovered_body=(
            "Cílová složka obsahuje zkopírované soubory z vybraného zdroje podle "
            "profilu a výjimek této zakázky. Prázdné adresáře a symbolické odkazy "
            "se neobnovují."
        ),
        destination_intro="Cílová složka je:",
        breakdown="Přehled zachráněných dat",
        no_copied_content="Manifest zatím neeviduje žádný zkopírovaný obsah souborů.",
        folder="Složka",
        files="Soubory",
        size="Velikost",
        library_breakdown="Zachráněná data z Library",
        library_area="Část Library",
        items_not_copied="Co nebylo zkopírováno",
        unresolved_items=(
            "Některé soubory zatím nebyly plně zkopírované nebo jsou evidované "
            "jako neúspěšné / po timeoutu. Přesné cesty a chyby jsou v detailních "
            "technických reportech."
        ),
        no_failed_items="Nástroj neeviduje žádné neúspěšné soubory ani soubory po timeoutu.",
        skipped_note=(
            "Přeskočené položky jsou obvykle symbolické odkazy. Nástroj je záměrně "
            "nepřenáší, aby nenásledoval odkazy mimo vybraný zdrojový strom."
        ),
        important_notes="Důležité poznámky",
        copied_note=(
            "Stav zkopírováno znamená, že nástroj zkopíroval obsah souboru a ověřil, "
            "že počet zkopírovaných bajtů odpovídá očekávané velikosti ze zdroje."
        ),
        metadata_note=(
            "macOS metadata, jako jsou rozšířené atributy a resource forks, se zachovávají "
            "best-effort. Některé systémové atributy včetně quarantine a MAC labels se "
            "záměrně neobnovují."
        ),
        icloud_note=(
            "Pokud byly některé soubory na zdrojovém Macu jen iCloud placeholdery, data "
            "nemusela být fyzicky na disku. Takové soubory mohou vyžadovat export z "
            "přihlášeného Macu nebo iCloud účtu."
        ),
        practical_note="Toto je praktická souborová záchrana dat, ne forenzní obraz disku.",
        detailed_reports="Detailní reporty",
        detailed_reports_intro="Detailní technické reporty lze vygenerovat příkazy:",
        status_complete_title="Evidované soubory zpracovány",
        status_unresolved_title="Záchrana má nedořešené soubory",
        status_complete_body="Nejsou evidované žádné neúspěšné, timeoutované, čekající ani přerušené soubory.",
        status_unresolved_body="Některé soubory vyžadují kontrolu technikem. Přesné cesty jsou v detailním reportu.",
        copied_data_metric="Zkopírovaná data",
        unresolved_metric="Nedořešené",
        locations="Umístění záchrany",
        recovered_data="Zachráněná data",
        result_summary="Souhrn výsledku",
        page="Strana",
    ),
}
PDF_CUSTOM_GLYPHS = (
    ("Á", "Aacute"),
    ("Č", "Ccaron"),
    ("Ď", "Dcaron"),
    ("É", "Eacute"),
    ("Ě", "Ecaron"),
    ("Í", "Iacute"),
    ("Ň", "Ncaron"),
    ("Ó", "Oacute"),
    ("Ř", "Rcaron"),
    ("Š", "Scaron"),
    ("Ť", "Tcaron"),
    ("Ú", "Uacute"),
    ("Ů", "Uring"),
    ("Ý", "Yacute"),
    ("Ž", "Zcaron"),
    ("á", "aacute"),
    ("č", "ccaron"),
    ("ď", "dcaron"),
    ("é", "eacute"),
    ("ě", "ecaron"),
    ("í", "iacute"),
    ("ň", "ncaron"),
    ("ó", "oacute"),
    ("ř", "rcaron"),
    ("š", "scaron"),
    ("ť", "tcaron"),
    ("ú", "uacute"),
    ("ů", "uring"),
    ("ý", "yacute"),
    ("ž", "zcaron"),
)
PDF_CUSTOM_CHAR_CODES = {
    char: 128 + index
    for index, (char, _glyph_name) in enumerate(PDF_CUSTOM_GLYPHS)
}


def status_text(job_dir: Path) -> str:
    summary = normalized_summary(job_dir)
    return " ".join(f"{status}={summary[status]['count']}" for status in STATUSES) + activity_text(job_dir)


def report(job_dir: Path, report_format: str) -> str:
    if report_format == "json":
        return json.dumps(report_payload(job_dir), indent=2, sort_keys=True) + "\n"
    return markdown_report(job_dir)


def write_customer_report(
    job_dir: Path,
    report_format: str,
    output_path: Path | None = None,
    language: str = "en",
) -> Path:
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    report_text = customer_report_text(language)
    if output_path is None:
        suffix = "pdf" if report_format == "pdf" else "md"
        stem = "recovery-report" if language == "en" else f"recovery-report-{language}"
        output_path = config.dest.parent / f"{stem}.{suffix}"
    output_path = output_path.resolve(strict=False)
    if is_same_or_inside(output_path, config.source):
        raise RescueError(f"customer report output must not be inside source: {output_path}")
    if config.profile == "customer-home":
        for root in (config.source / "Applications", volume_root_for_home(config.source) / "Applications"):
            if is_same_or_inside(output_path, root.resolve(strict=False)):
                raise RescueError(f"customer report output must not be inside application source: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = customer_markdown_report(job_dir, report_text)
    if report_format == "markdown":
        atomic_write_bytes(output_path, markdown.encode())
    elif report_format == "pdf":
        atomic_write_bytes(output_path, customer_pdf_bytes(job_dir, report_text))
    else:
        raise RescueError(f"unsupported customer report format: {report_format}")
    return output_path


def customer_report_text(language: str) -> CustomerReportText:
    try:
        return CUSTOMER_REPORT_TEXT[language]
    except KeyError as exc:
        raise RescueError(f"unsupported customer report language: {language}") from exc


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
            "excludes": list(config.excludes),
        },
        "summary": normalized_summary(job_dir),
        "verification": verification_counts(rows),
        "activity": activity_snapshot(job_dir),
        "scan_issues": scan_issues(job_dir),
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
    summary = cast(dict[str, dict[str, int]], payload["summary"])
    for status in STATUSES:
        item = summary[status]
        lines.append(f"| {status} | {item['count']} | {item['bytes']} |")
    if payload["scan_issues"]:
        lines.extend(["", "## Unscanned paths (incomplete coverage)", ""])
        for issue in payload["scan_issues"]:
            lines.append(f"- `{issue['path']}` ({issue['phase']}): {issue['error']}")
    lines.extend(["", "## Destination integrity", "", str(payload["verification"]),
                  "SHA256 covers file content only; unverified files are not certified."])
    lines.extend(["", "## Important warnings", ""])
    warnings = cast(list[dict[str, str]], payload["warnings"])
    for warning in warnings:
        lines.append(f"- **{warning['title']}**: {warning['message']}")
    lines.extend(["", "## Files", ""])
    files = cast(list[dict[str, object]], payload["files"])
    if not files:
        lines.append("No files scanned.")
    for item in files:
        error = f" - {item['error']}" if item["error"] else ""
        copied = (
            f" - copied {item['copied_bytes']}/{item['size']} bytes"
            if 0 < item["copied_bytes"] != item["size"]
            else ""
        )
        if item.get("fallback_source_path"):
            error += f" - fallback source: `{item['fallback_source_path']}`"
            error += f" - original error: {item.get('fallback_original_error') or 'not recorded'}"
        if item.get("verification_status"):
            error += f" - verification: {item['verification_status']} ({item.get('verification_error') or 'SHA256 matches'})"
        warning = f" - WARNING: {item['warning']}" if item["warning"] else ""
        lines.append(
            f"- `{item['relative_path']}` - {item['status']} - {item['size']} bytes{copied}{error}{warning}"
        )
    return "\n".join(lines) + "\n"


def customer_markdown_report(job_dir: Path, text: CustomerReportText | None = None) -> str:
    text = text or customer_report_text("en")
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    summary = normalized_summary(job_dir)
    rows = all_files(job_dir)
    breakdown = recovered_top_level_breakdown(rows)
    library_breakdown = recovered_library_breakdown(rows)
    copied_bytes = recovered_total(summary, "bytes")
    failed = summary["failed"]["count"]
    timed_out = summary["timed_out"]["count"]
    pending = summary["pending"]["count"]
    copying = summary["copying"]["count"]
    compressed = summary[UNREADABLE_COMPRESSED]["count"]
    fallback = summary["copied_from_fallback"]["count"]
    unresolved = failed + timed_out + pending + copying + compressed + verification_counts(rows)["failed"] + len(scan_issues(job_dir))
    lines = [
        f"# {text.title}",
        "",
        text.subtitle,
        "",
        f"{text.generated}: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"## {text.summary}",
        "",
        f"{text.recovery_source}: `{config.source}`  ",
        f"{text.recovery_destination}: `{config.dest}`",
        "",
    ]
    if unresolved:
        lines.append(text.unresolved_sentence)
    else:
        lines.append(text.complete_sentence)
    lines.extend(
        [
            "",
            f"| {text.result} | {text.count} |",
            "| --- | ---: |",
            f"| {text.copied_files} | {recovered_total(summary, 'count')} |",
            f"| {text.fallback_files} | {fallback} |",
            f"| {text.compressed_files} | {compressed} |",
            f"| {text.copying_files} | {copying} |",
            f"| {text.failed_files} | {failed} |",
            f"| {text.timed_out_files} | {timed_out} |",
            f"| {text.pending_files} | {pending} |",
            f"| {text.skipped_entries} | {summary['skipped']['count']} |",
            "",
            f"{text.copied_data_recorded}: {format_size(copied_bytes)}  ",
            "",
            f"## {text.what_recovered}",
            "",
            text.what_recovered_body,
            "",
            text.destination_intro,
            "",
            f"`{config.dest}`",
            "",
            f"## {text.breakdown}",
            "",
        ]
    )
    if breakdown:
        lines.extend(
            [
                f"| {text.folder} | {text.files} | {text.size} |",
                "| --- | ---: | ---: |",
            ]
        )
        for label, count, size in breakdown:
            lines.append(f"| {markdown_table_cell(label)} | {count} | {format_size(size)} |")
    else:
        lines.append(text.no_copied_content)
    if library_breakdown:
        lines.extend(
            [
                "",
                f"## {text.library_breakdown}",
                "",
                f"| {text.library_area} | {text.files} | {text.size} |",
                "| --- | ---: | ---: |",
            ]
        )
        for label, count, size in library_breakdown:
            lines.append(f"| {markdown_table_cell(label)} | {count} | {format_size(size)} |")
    lines.extend(
        [
            "",
            f"## {text.items_not_copied}",
            "",
        ]
    )
    if unresolved:
        lines.append(text.unresolved_items)
    else:
        lines.append(text.no_failed_items)
    lines.extend(
        [
            "",
            text.skipped_note,
            "",
            f"## {text.important_notes}",
            "",
            f"- {text.copied_note}",
            f"- {integrity_note(rows, text)}",
            f"- {coverage_note(job_dir, text)}",
            f"- {text.metadata_note}",
            f"- {text.icloud_note}",
            f"- {text.practical_note}",
            *([f"- {text.compressed_note}"] if compressed else []),
            *([f"- {text.fallback_note}"] if fallback else []),
            "",
            f"## {text.detailed_reports}",
            "",
            text.detailed_reports_intro,
            "",
            "`macos-data-rescue report --job-dir JOB --format markdown`",
            "`macos-data-rescue report --job-dir JOB --format json`",
        ]
    )
    return "\n".join(lines) + "\n"


def customer_pdf_bytes(job_dir: Path, text: CustomerReportText | None = None) -> bytes:
    text = text or customer_report_text("en")
    config = load_config(job_dir)
    migrate_manifest(job_dir)
    summary = normalized_summary(job_dir)
    rows = all_files(job_dir)
    failed = summary["failed"]["count"]
    timed_out = summary["timed_out"]["count"]
    pending = summary["pending"]["count"]
    copying = summary["copying"]["count"]
    compressed = summary[UNREADABLE_COMPRESSED]["count"]
    fallback = summary["copied_from_fallback"]["count"]
    unresolved = failed + timed_out + pending + copying + compressed + verification_counts(rows)["failed"] + len(scan_issues(job_dir))
    canvas = PdfCanvas()
    canvas.header(text.title, text.subtitle)
    canvas.status_card(
        text.status_complete_title if not unresolved else text.status_unresolved_title,
        text.status_complete_body if not unresolved else text.status_unresolved_body,
        ok=not unresolved,
    )
    canvas.metric_cards(
        (
            (text.copied_files, str(recovered_total(summary, "count"))),
            (text.copied_data_metric, format_size(recovered_total(summary, "bytes"))),
            (text.unresolved_metric, str(unresolved)),
        )
    )
    canvas.section(text.locations)
    canvas.key_value(text.recovery_source, str(config.source))
    canvas.key_value(text.recovered_data, str(config.dest))
    canvas.spacer(8)
    canvas.section(text.result_summary)
    canvas.table(
        (text.result, text.count),
        (
            (text.copied_files, str(recovered_total(summary, "count"))),
            (text.fallback_files, str(fallback)),
            (text.compressed_files, str(compressed)),
            (text.copying_files, str(copying)),
            (text.failed_files, str(failed)),
            (text.timed_out_files, str(timed_out)),
            (text.pending_files, str(pending)),
            (text.skipped_entries, str(summary["skipped"]["count"])),
        ),
        (330, 120),
    )
    canvas.section(text.breakdown)
    breakdown_rows = tuple(
        (label, str(count), format_size(size)) for label, count, size in recovered_top_level_breakdown(rows)
    )
    if breakdown_rows:
        canvas.table((text.folder, text.files, text.size), breakdown_rows, (240, 80, 130))
    else:
        canvas.paragraph(text.no_copied_content)
    library_rows = tuple(
        (label, str(count), format_size(size)) for label, count, size in recovered_library_breakdown(rows)
    )
    if library_rows:
        canvas.section(text.library_breakdown)
        canvas.table((text.library_area, text.files, text.size), library_rows, (240, 80, 130))
    canvas.section(text.important_notes)
    canvas.bullet(text.copied_note)
    canvas.bullet(integrity_note(rows, text))
    canvas.bullet(coverage_note(job_dir, text))
    canvas.bullet(text.metadata_note)
    canvas.bullet(text.icloud_note)
    canvas.bullet(text.practical_note)
    if compressed:
        canvas.bullet(text.compressed_note)
    if fallback:
        canvas.bullet(text.fallback_note)
    canvas.footer(text.page)
    return canvas.render()


def recovered_top_level_breakdown(rows) -> list[tuple[str, int, int]]:
    return aggregate_copied_rows(rows, top_level_label)


def recovered_library_breakdown(rows) -> list[tuple[str, int, int]]:
    return aggregate_copied_rows(rows, library_area_label)


def aggregate_copied_rows(rows, label_for) -> list[tuple[str, int, int]]:
    totals: dict[str, list[int]] = {}
    for row in rows:
        if row["status"] not in COPIED_STATUSES:
            continue
        label = label_for(str(row["relative_path"]))
        if label is None:
            continue
        item = totals.setdefault(label, [0, 0])
        item[0] += 1
        item[1] += copied_row_size(row)
    return sorted(
        ((label, count, size) for label, (count, size) in totals.items()),
        key=lambda item: (-item[2], item[0].lower()),
    )


def top_level_label(relative_path: str) -> str | None:
    parts = tuple(part for part in relative_path.split("/") if part)
    return parts[0] if parts else None


def library_area_label(relative_path: str) -> str | None:
    parts = tuple(part for part in relative_path.split("/") if part)
    if not parts or parts[0] != "Library":
        return None
    if len(parts) == 1:
        return "Library root"
    return parts[1]


def copied_row_size(row) -> int:
    copied_bytes = int(row["copied_bytes"])
    if copied_bytes > 0:
        return copied_bytes
    return int(row["size"])


def markdown_table_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


class PdfCanvas:
    page_width = 595
    page_height = 842
    margin = 42
    bottom = 58
    body_width = page_width - margin * 2

    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = 742

    @property
    def commands(self) -> list[str]:
        return self.pages[-1]

    def header(self, title: str, subtitle: str) -> None:
        self.rect(0, 766, self.page_width, 76, (0.12, 0.25, 0.38))
        self.text(self.margin, 812, title, "F2", 24, (1, 1, 1))
        self.text(self.margin, 792, subtitle, "F1", 11, (0.86, 0.92, 0.96))
        self.y = 734

    def status_card(self, title: str, body: str, *, ok: bool) -> None:
        self.ensure(70)
        color = (0.88, 0.96, 0.91) if ok else (1.0, 0.94, 0.82)
        accent = (0.12, 0.48, 0.28) if ok else (0.72, 0.38, 0.04)
        top = self.y
        self.rect(self.margin, top - 58, self.body_width, 58, color)
        self.rect(self.margin, top - 58, 6, 58, accent)
        self.text(self.margin + 18, top - 22, title, "F2", 14, (0.08, 0.12, 0.18))
        self.wrapped_text(self.margin + 18, top - 41, body, 10, self.body_width - 32)
        self.y = top - 76

    def metric_cards(self, metrics: tuple[tuple[str, str], ...]) -> None:
        self.ensure(86)
        gap = 10
        width = (self.body_width - gap * (len(metrics) - 1)) / len(metrics)
        top = self.y
        for index, (label, value) in enumerate(metrics):
            x = self.margin + index * (width + gap)
            self.rect(x, top - 62, width, 62, (0.95, 0.97, 0.99))
            self.text(x + 12, top - 20, label.upper(), "F1", 8, (0.37, 0.45, 0.53))
            self.text(x + 12, top - 44, value, "F2", 17, (0.08, 0.12, 0.18))
        self.y = top - 82

    def section(self, title: str) -> None:
        # Keep the heading with at least a table header and its first row.
        self.ensure(80)
        self.text(self.margin, self.y, title, "F2", 14, (0.12, 0.25, 0.38))
        self.line(self.margin, self.y - 7, self.margin + self.body_width, self.y - 7, (0.78, 0.84, 0.90))
        self.y -= 25

    def key_value(self, label: str, value: str) -> None:
        self.ensure(34)
        self.text(self.margin, self.y, label, "F2", 9, (0.27, 0.34, 0.42))
        consumed = self.wrapped_text(self.margin + 95, self.y, value, 9, self.body_width - 95)
        self.y -= max(18, consumed)

    def table(
        self,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
        widths: tuple[int, ...],
    ) -> None:
        row_height = 20
        remaining = list(rows)
        while remaining:
            self.ensure(row_height * 2 + 12)
            available = self.y - self.bottom - row_height - 18
            chunk_size = max(1, int(available // row_height))
            chunk = tuple(remaining[:chunk_size])
            remaining = remaining[chunk_size:]
            self.table_chunk(headers, chunk, widths, row_height)
            if remaining:
                self.pages.append([])
                self.y = 792

    def table_chunk(
        self,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
        widths: tuple[int, ...],
        row_height: int,
    ) -> None:
        x = self.margin
        top = self.y
        total_width = sum(widths)
        self.rect(x, top - row_height, total_width, row_height, (0.12, 0.25, 0.38))
        self.table_row(headers, widths, top - 14, "F2", 9, (1, 1, 1))
        for index, row in enumerate(rows):
            row_top = top - row_height * (index + 1)
            fill = (0.98, 0.99, 1.0) if index % 2 == 0 else (1, 1, 1)
            self.rect(x, row_top - row_height, total_width, row_height, fill)
            self.table_row(row, widths, row_top - 14, "F1", 9, (0.08, 0.12, 0.18))
        self.y = top - row_height * (len(rows) + 1) - 18

    def table_row(
        self,
        values: tuple[str, ...],
        widths: tuple[int, ...],
        y: float,
        font: str,
        size: int,
        color: tuple[float, float, float],
    ) -> None:
        x = self.margin + 8
        for value, width in zip(values, widths):
            self.text(x, y, fit_text(value, width, size), font, size, color)
            x += width

    def paragraph(self, text: str) -> None:
        self.ensure(len(wrap_pdf_lines([text], max_chars_for_width(self.body_width, 10))) * 13 + 6)
        consumed = self.wrapped_text(self.margin, self.y, text, 10, self.body_width)
        self.y -= consumed + 6

    def bullet(self, text: str) -> None:
        self.ensure(len(wrap_pdf_lines([text], max_chars_for_width(self.body_width - 16, 10))) * 13 + 5)
        self.text(self.margin, self.y, "*", "F2", 10, (0.12, 0.25, 0.38))
        consumed = self.wrapped_text(self.margin + 16, self.y, text, 10, self.body_width - 16)
        self.y -= consumed + 5

    def spacer(self, height: int) -> None:
        self.y -= height

    def wrapped_text(self, x: float, y: float, text: str, size: int, width: float) -> int:
        lines = wrap_pdf_lines([text], max_chars_for_width(width, size))
        leading = size + 3
        for index, line in enumerate(lines):
            self.text(x, y - index * leading, line, "F1", size, (0.12, 0.16, 0.22))
        return max(leading, len(lines) * leading)

    def ensure(self, height: float) -> None:
        if self.y - height >= self.bottom:
            return
        self.pages.append([])
        self.y = 792

    def footer(self, page_label: str) -> None:
        for index, commands in enumerate(self.pages, start=1):
            commands.append(
                pdf_text_command(self.margin, 28, f"{page_label} {index}", "F1", 8, (0.45, 0.52, 0.60))
            )

    def rect(self, x: float, y: float, width: float, height: float, color: tuple[float, float, float]) -> None:
        self.commands.append(f"{pdf_rgb(color)} rg {pdf_num(x)} {pdf_num(y)} {pdf_num(width)} {pdf_num(height)} re f")

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: tuple[float, float, float],
    ) -> None:
        self.commands.append(
            f"{pdf_rgb(color)} RG 0.8 w {pdf_num(x1)} {pdf_num(y1)} m {pdf_num(x2)} {pdf_num(y2)} l S"
        )

    def text(
        self,
        x: float,
        y: float,
        text: str,
        font: str,
        size: int,
        color: tuple[float, float, float],
    ) -> None:
        self.commands.append(pdf_text_command(x, y, text, font, size, color))

    def render(self) -> bytes:
        page_background = "1 1 1 rg 0 0 595 842 re f"
        streams = [
            "\n".join((page_background, *commands)).encode("latin-1")
            for commands in self.pages
        ]
        return pdf_document_bytes(streams)


def pdf_text_command(
    x: float,
    y: float,
    text: str,
    font: str,
    size: int,
    color: tuple[float, float, float],
) -> str:
    return (
        "BT "
        f"{pdf_rgb(color)} rg "
        f"/{font} {size} Tf "
        f"{pdf_num(x)} {pdf_num(y)} Td "
        f"({pdf_escape(text)}) Tj "
        "ET"
    )


def pdf_document_bytes(streams: list[bytes]) -> bytes:
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_object_ids = [6 + index * 2 for index in range(len(streams))]
    kids = b" ".join(f"{object_id} 0 R".encode("ascii") for object_id in page_object_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids.decode('ascii')}] /Count {len(streams)} >>".encode("ascii"))
    objects.append(pdf_custom_encoding_object())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding 3 0 R >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding 3 0 R >>")

    for index, stream in enumerate(streams):
        page_id = page_object_ids[index]
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PdfCanvas.page_width} {PdfCanvas.page_height}] "
                f"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
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


def pdf_custom_encoding_object() -> bytes:
    differences = " ".join(glyph_name for _char, glyph_name in PDF_CUSTOM_GLYPHS)
    return (
        "<< /Type /Encoding /BaseEncoding /WinAnsiEncoding "
        f"/Differences [128 /{differences.replace(' ', ' /')}] >>"
    ).encode("ascii")


def fit_text(text: str, width: int, size: int) -> str:
    max_chars = max_chars_for_width(width - 14, size)
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def max_chars_for_width(width: float, size: int) -> int:
    return max(8, int(width / (size * 0.52)))


def pdf_rgb(color: tuple[float, float, float]) -> str:
    return " ".join(pdf_num(component) for component in color)


def pdf_num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


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
    except OSError:
        pass
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
        custom_code = PDF_CUSTOM_CHAR_CODES.get(char)
        if custom_code is not None:
            safe_chars.append(chr(custom_code))
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


def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count} B"
    value = float(bytes_count)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def recovered_total(summary: dict[str, dict[str, int]], field: str) -> int:
    return sum(summary[status][field] for status in COPIED_STATUSES)


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
    for key in ("fallback_source_path", "fallback_mtime_ns", "fallback_original_error",
                "sha256", "verification_status", "verification_error", "verified_at"):
        if key in row.keys() and row[key] is not None:
            item[key] = row[key]
    return item


def verification_counts(rows) -> dict[str, int]:
    counts = dict.fromkeys(("verified", "failed", "unverifiable", "not_checked"), 0)
    for row in rows:
        if row["status"] in COPIED_STATUSES:
            counts[row["verification_status"] or "not_checked"] += 1
    return counts


def integrity_note(rows, text: CustomerReportText) -> str:
    counts = verification_counts(rows)
    unchecked = counts["not_checked"] + counts["unverifiable"]
    if text == CUSTOMER_REPORT_TEXT["cs"]:
        return (f"SHA256 obsahu cílových souborů: ověřeno {counts['verified']}, "
                f"chyba {counts['failed']}, neověřeno {unchecked}. Kontrola nepotvrzuje úplnost zdroje.")
    return (f"Destination content SHA256: verified {counts['verified']}, "
            f"failed {counts['failed']}, unchecked {unchecked}. This does not certify source coverage.")


def coverage_note(job_dir, text):
    count = len(scan_issues(job_dir))
    if text == CUSTOMER_REPORT_TEXT["cs"]:
        return f"Neprozkoumané cesty: {count}. Jejich obsah není zahrnut v počtu souborů; podrobnosti má technik."
    return f"Unscanned paths: {count}. Their unknown contents are not included in file counts; see the technician report."
