import json
import os
import stat
import sqlite3
import pytest

from pathlib import Path

from helpers import run_cli, write_file, init_and_scan



def test_report_outputs_markdown_and_json_summary(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Desktop" / "denied.txt", b"denied")
    os.chmod(source / "Desktop" / "denied.txt", 0)
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    markdown = run_cli("report", "--job-dir", str(job_dir), "--format", "markdown").stdout
    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    assert "# macOS Data Rescue Report" in markdown
    assert "Desktop/invoice.txt" in markdown
    assert "iCloud Optimize Mac Storage placeholders" in markdown
    assert payload["warnings"][0]["code"] == "icloud_dataless_placeholders"
    assert payload["summary"]["copied"]["count"] == 1
    assert payload["summary"]["failed"]["count"] == 1
    assert {item["relative_path"] for item in payload["files"]} == {
        "Desktop/denied.txt",
        "Desktop/invoice.txt",
    }


def test_customer_report_writes_markdown_and_pdf_to_recovery_root(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    recovery_root = tmp_path / "recovery"
    job_dir = recovery_root / ".rescue"
    dest_dir = recovery_root / "user-data"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    markdown_result = run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    pdf_result = run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    markdown_path = recovery_root / "recovery-report.md"
    pdf_path = recovery_root / "recovery-report.pdf"
    assert str(markdown_path) in markdown_result.stdout
    assert str(pdf_path) in pdf_result.stdout
    assert markdown_path.exists()
    assert pdf_path.exists()
    markdown = markdown_path.read_text()
    assert "# Data Recovery Report" in markdown
    assert f"Recovery destination: `{dest_dir.resolve(strict=False)}`" in markdown
    assert "| Copied files | 1 |" in markdown
    assert "| Failed files | 0 |" in markdown
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def test_customer_report_writes_czech_markdown_and_pdf_without_overwriting_english(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "faktura.txt", b"desktop")
    recovery_root = tmp_path / "recovery"
    job_dir = recovery_root / ".rescue"
    dest_dir = recovery_root / "user-data"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "important")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")
    cs_markdown_result = run_cli(
        "customer-report",
        "--job-dir",
        str(job_dir),
        "--format",
        "markdown",
        "--language",
        "cs",
    )
    cs_pdf_result = run_cli(
        "customer-report",
        "--job-dir",
        str(job_dir),
        "--format",
        "pdf",
        "--language",
        "cs",
    )

    english_markdown = recovery_root / "recovery-report.md"
    english_pdf = recovery_root / "recovery-report.pdf"
    czech_markdown = recovery_root / "recovery-report-cs.md"
    czech_pdf = recovery_root / "recovery-report-cs.pdf"
    assert str(czech_markdown) in cs_markdown_result.stdout
    assert str(czech_pdf) in cs_pdf_result.stdout
    assert english_markdown.exists()
    assert english_pdf.exists()
    czech_text = czech_markdown.read_text()
    czech_pdf_bytes = czech_pdf.read_bytes()
    assert "# Zpráva o záchraně dat" in czech_text
    assert "Souhrn pro zákazníka" in czech_text
    assert "| Zkopírované soubory | 1 |" in czech_text
    assert "Přehled zachráněných dat" in czech_text
    assert czech_pdf_bytes.startswith(b"%PDF-")
    assert b"\\u" not in czech_pdf_bytes
    assert b"/ccaron" in czech_pdf_bytes


def test_format_size_uses_adaptive_units() -> None:
    from macos_data_rescue.reporting import format_size

    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(2048) == "2.0 KiB"
    assert format_size(5 * 1024**2) == "5.0 MiB"
    assert format_size(3 * 1024**3) == "3.0 GiB"
    assert format_size(2 * 1024**4) == "2.0 TiB"


def test_customer_report_shows_small_sizes_with_adaptive_units(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"x" * 2048)
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "important", "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")

    markdown = (tmp_path / "recovery-report.md").read_text()
    assert "| Desktop | 1 | 2.0 KiB |" in markdown
    assert "0.0 GiB" not in markdown


def test_customer_report_rejects_output_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    source_output = source / "recovery-report.pdf"

    result = run_cli(
        "customer-report",
        "--job-dir",
        str(job_dir),
        "--format",
        "pdf",
        "--output",
        str(source_output),
        check=False,
    )

    assert result.returncode == 1
    assert "must not be inside source" in result.stderr
    assert not source_output.exists()


def test_customer_report_marks_pending_and_copying_as_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set status = 'copying' where relative_path = 'Desktop/invoice.txt'")
        conn.commit()
    finally:
        conn.close()

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    report_text = (tmp_path / "recovery-report.md").read_text()
    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert "completed with unresolved files" in report_text
    assert "| Copying files | 1 |" in report_text
    assert "| Pending files | 0 |" in report_text
    assert "not fully copied yet" in report_text
    assert b"Copying files" in pdf_bytes


def test_customer_report_preserves_existing_file_when_atomic_replace_fails(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import reporting

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    output_path = tmp_path / "recovery-report.md"
    output_path.write_text("existing report")

    def fail_replace(src: str | bytes | os.PathLike[str], dst: str | bytes | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(reporting.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        reporting.write_customer_report(job_dir, "markdown", output_path)

    assert output_path.read_text() == "existing report"
    assert not list(tmp_path.glob("*.rescue-report-tmp"))


def test_customer_report_survives_directory_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    from macos_data_rescue import reporting

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    output_path = tmp_path / "recovery-report.md"
    original_fsync = os.fsync

    def failing_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(reporting.os, "fsync", failing_directory_fsync)

    written = reporting.write_customer_report(job_dir, "markdown", output_path)

    assert written == output_path
    assert "# Data Recovery Report" in output_path.read_text()


def test_customer_report_temp_write_does_not_follow_stale_symlink_into_source(tmp_path: Path) -> None:
    from macos_data_rescue import reporting

    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    victim = source / "Desktop" / "source-victim.txt"
    write_file(victim, b"source must stay untouched")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    output_path = tmp_path / "recovery-report.md"
    stale_predictable_temp = tmp_path / f".{output_path.name}.{os.getpid()}.rescue-report-tmp"
    os.symlink(victim, stale_predictable_temp)

    reporting.write_customer_report(job_dir, "markdown", output_path)

    assert output_path.exists()
    assert victim.read_bytes() == b"source must stay untouched"
    assert stale_predictable_temp.is_symlink()


def test_customer_pdf_report_escapes_unsupported_unicode_without_corrupting_markdown(tmp_path: Path) -> None:
    source = tmp_path / "zdroj-🙂"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")

    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"zdroj-\\\\U0001f642" in pdf_bytes
    assert "zdroj-🙂" in (tmp_path / "recovery-report.md").read_text()


def test_customer_report_pdf_has_layout_and_recovered_data_breakdown(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Projects" / "site" / "index.html", b"custom")
    write_file(source / "Library" / "Mail" / "V10" / "message.emlx", b"mail")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")
    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    markdown = (tmp_path / "recovery-report.md").read_text()
    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert "## Recovered Data Breakdown" in markdown
    assert "| Desktop | 1 |" in markdown
    assert "| Projects | 1 |" in markdown
    assert "| Library | 1 |" in markdown
    assert "## Library Data Recovered" in markdown
    assert "| Mail | 1 |" in markdown
    assert b"Recovered Data Breakdown" in pdf_bytes
    assert b"Desktop" in pdf_bytes
    assert b"Projects" in pdf_bytes
    assert b"Library Data Recovered" in pdf_bytes
    assert b"/F2" in pdf_bytes
    assert b" re f" in pdf_bytes
    assert b"1 1 1 rg 0 0 595 842 re f" in pdf_bytes


def test_customer_report_escapes_breakdown_markdown_table_cells(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Customer|Project" / "invoice.txt", b"invoice")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "markdown")

    markdown = (tmp_path / "recovery-report.md").read_text()
    assert "| Customer\\|Project | 1 |" in markdown
    assert "| Customer|Project | 1 |" not in markdown


def test_customer_pdf_breakdown_includes_more_than_twelve_rows(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    for index in range(14):
        write_file(source / f"Folder{index:02d}" / "file.txt", b"x")
        write_file(source / "Library" / f"Area{index:02d}" / "file.txt", b"x")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    run_cli("copy", "--job-dir", str(job_dir), "--timeout", "2")

    run_cli("customer-report", "--job-dir", str(job_dir), "--format", "pdf")

    pdf_bytes = (tmp_path / "recovery-report.pdf").read_bytes()
    assert b"Folder13" in pdf_bytes
    assert b"Area13" in pdf_bytes
