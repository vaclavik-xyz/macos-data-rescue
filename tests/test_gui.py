import tomllib
from pathlib import Path
import tkinter as tk

import pytest

from helpers import ROOT, write_file, init_and_scan
from macos_data_rescue.cli import build_parser
from macos_data_rescue.gui.app import CaffeinateManager, DataRescueApp
from macos_data_rescue.gui.i18n import MESSAGES, PHASE_DESCRIPTIONS, format_eta, format_rate, tr
from macos_data_rescue.manifest import manifest_path, record_approval


@pytest.fixture(autouse=True)
def mock_messagebox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tkinter.messagebox.showinfo", lambda *a, **kw: "ok")
    monkeypatch.setattr("tkinter.messagebox.showwarning", lambda *a, **kw: "ok")
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **kw: "ok")
    monkeypatch.setattr("tkinter.messagebox.askyesno", lambda *a, **kw: True)


def test_pyproject_declares_gui_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["macos-data-rescue"] == "macos_data_rescue.cli:main"
    assert data["project"]["scripts"]["macos-data-rescue-gui"] == "macos_data_rescue.gui:main"


def test_gui_cli_argument_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["gui", "--job-dir", "/tmp/job", "--source", "/tmp/src", "--dest", "/tmp/dst", "--lang", "cs"])
    assert args.command == "gui"
    assert args.job_dir == Path("/tmp/job")
    assert args.source == Path("/tmp/src")
    assert args.dest == Path("/tmp/dst")
    assert args.lang == "cs"

    args_en = parser.parse_args(["gui", "--lang", "en"])
    assert args_en.lang == "en"


def test_formatting_helpers() -> None:
    assert format_rate(0) == "0.0 MB/s"
    assert format_rate(500_000) == "0.50 MB/s"
    assert format_rate(125_000_000) == "125.00 MB/s"
    assert format_rate(2_500_000_000) == "2.50 GB/s"

    assert format_eta(None, "cs") == "počítám..."
    assert format_eta(None, "en") == "calculating..."
    assert format_eta(-5, "cs") == "počítám..."
    assert format_eta(45, "cs") == "45s"
    assert format_eta(125, "cs") == "2m 05s"
    assert format_eta(3665, "cs") == "1h 01m 05s"

    assert "visible-home" in PHASE_DESCRIPTIONS["cs"]
    assert "visible-home" in PHASE_DESCRIPTIONS["en"]
    assert "app-data" in PHASE_DESCRIPTIONS["cs"]
    assert "app-data" in PHASE_DESCRIPTIONS["en"]


def test_translation_completeness() -> None:
    cs_keys = set(MESSAGES["cs"].keys())
    en_keys = set(MESSAGES["en"].keys())
    missing_in_cs = en_keys - cs_keys
    missing_in_en = cs_keys - en_keys
    assert not missing_in_cs, f"Missing translations in Czech: {missing_in_cs}"
    assert not missing_in_en, f"Missing translations in English: {missing_in_en}"

    assert tr("btn_scan", "cs") == "1. Skenovat fázi"
    assert tr("btn_scan", "en") == "1. Scan Phase"


def test_caffeinate_manager() -> None:
    manager = CaffeinateManager()
    manager.start()
    manager.stop()
    assert manager._proc is None


def test_gui_app_initialization_and_preflight(tmp_path: Path) -> None:
    source = tmp_path / "source-user"
    dest = tmp_path / "dest-user"
    job_dir = tmp_path / ".rescue"
    source.mkdir()
    write_file(source / "Desktop" / "doc.txt", b"content")

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tkinter display not available in this test environment")

    try:
        root.withdraw()
        app = DataRescueApp(root, initial_job_dir=job_dir, initial_source=source, initial_dest=dest, lang="cs")

        # Check paths populated
        assert app.source_var.get() == str(source)
        assert app.dest_var.get() == str(dest)
        assert app.job_dir_var.get() == str(job_dir)
        assert app.lang == "cs"

        # Check Czech header / tabs
        assert "Příprava" in app.notebook.tab(0, "text")

        # Run preflight
        app.run_preflight()
        children = app.preflight_tree.get_children()
        assert len(children) > 0
        summary = app.preflight_summary_label.cget("text")
        assert "V POŘÁDKU" in summary or "PASSED" in summary

        # Initialize job
        app.init_job()
        assert manifest_path(job_dir).exists()

        # Telemetry update
        app._update_telemetry()
        assert "Zkopírováno: 0" in app.badge_copied.cget("text")
        assert "Ve frontě: 0" in app.badge_pending.cget("text")

        # Test phase selection & description
        app.phase_var.set("app-data")
        app._on_phase_selected(None)
        assert "Library" in app.phase_desc_label.cget("text")
        assert str(app.btn_approve.cget("state")) in ("normal", "")

        # Record approval and verify button updates
        record_approval(job_dir, "app-data", by="service-tech")
        app._on_phase_selected(None)
        assert "Schváleno" in app.btn_approve.cget("text")

        # Test dynamic language switch to English and back to Czech
        app.set_language("en")
        assert app.lang == "en"
        assert "Setup" in app.notebook.tab(0, "text")
        assert "Copied: 0" in app.badge_copied.cget("text")
        assert "Pending: 0" in app.badge_pending.cget("text")

        app.set_language("cs")
        assert app.lang == "cs"
        assert "Příprava" in app.notebook.tab(0, "text")
        assert "Zkopírováno: 0" in app.badge_copied.cget("text")

        # Test customer report generation via GUI
        app.cust_lang_var.set("cs")
        app.cust_format_var.set("markdown")
        app.generate_customer_report()
        assert app.last_customer_report_path is not None
        assert app.last_customer_report_path.exists()
        assert app.last_customer_report_path.suffix == ".md"

        # Test technician report loading
        app.load_tech_report()
        tech_text = app.tech_report_text.get("1.0", "end")
        assert "macOS Data Rescue Report" in tech_text

    finally:
        root.destroy()


def test_gui_load_existing_job(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "data.txt", b"important data")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tkinter display not available in this test environment")

    try:
        root.withdraw()
        app = DataRescueApp(root, initial_job_dir=job_dir, lang="cs")

        assert app.job_dir_var.get() == str(job_dir)
        assert app.source_var.get() == str(source)
        assert app.dest_var.get() == str(dest_dir)

        # Telemetry should count the scanned file as pending in Czech
        app._update_telemetry()
        assert "Ve frontě: 1" in app.badge_pending.cget("text")

        # Guide should recommend copying
        app._update_guide()
        assert len(app.guide_text_var.get()) > 0
    finally:
        root.destroy()
