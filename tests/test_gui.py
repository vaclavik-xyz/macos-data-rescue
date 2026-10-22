"""GUI tests. Widget tests need a display and skip cleanly without one.

Commands run through the real CLI in a child process; sleep inhibition is
replaced by a no-op so tests never start /usr/bin/caffeinate.
"""
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from helpers import ROOT, init_and_scan, write_file
from macos_data_rescue.cli import build_parser
from macos_data_rescue.gui.commands import CommandSpec
from macos_data_rescue.gui.i18n import MESSAGES, PHASE_DESCRIPTIONS, HOME_PHASES, format_eta, format_rate, tr
from macos_data_rescue.gui.runner import CommandRunner, RunFinished
from macos_data_rescue.gui.state import STATE_INFO, OpState

GUI_SRC = ROOT / "src" / "macos_data_rescue" / "gui"


def test_pyproject_declares_gui_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["macos-data-rescue-gui"] == "macos_data_rescue.gui:main"


def test_gui_cli_argument_parser() -> None:
    args = build_parser().parse_args(["gui", "--job-dir", "/tmp/job", "--source", "/tmp/src", "--dest", "/tmp/dst"])
    assert (args.command, args.job_dir, args.source, args.dest, args.lang) == (
        "gui", Path("/tmp/job"), Path("/tmp/src"), Path("/tmp/dst"), "cs")
    assert build_parser().parse_args(["gui", "--lang", "en"]).lang == "en"


def test_logic_modules_do_not_import_tkinter() -> None:
    code = ("import sys\nimport macos_data_rescue.gui, macos_data_rescue.gui.runner, macos_data_rescue.gui.monitor\n"
            "import macos_data_rescue.gui.state, macos_data_rescue.gui.commands, macos_data_rescue.gui.i18n\n"
            "assert 'tkinter' not in sys.modules, 'tkinter imported'\n")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


def test_formatting_helpers() -> None:
    assert format_rate(0) == "0.0 MB/s"
    assert format_rate(500_000) == "0.50 MB/s"
    assert format_rate(2_500_000_000) == "2.50 GB/s"
    assert format_eta(None, "cs") == "—"
    assert format_eta(-5, "en") == "—"
    assert format_eta(45) == "45s"
    assert format_eta(125) == "2m 05s"
    assert format_eta(3665) == "1h 01m 05s"


def test_translations_are_complete() -> None:
    assert MESSAGES["cs"].keys() == MESSAGES["en"].keys()
    assert PHASE_DESCRIPTIONS["cs"].keys() == PHASE_DESCRIPTIONS["en"].keys() == set(HOME_PHASES)
    used: set[str] = set()
    for path in GUI_SRC.glob("*.py"):
        text = path.read_text()
        used |= set(re.findall(r'_t\(\s*"([a-z_]+)"', text))
        used |= set(re.findall(r'\bt\(\s*"([a-z_]+)"', text))
        used |= set(re.findall(r'RunnerNotice\("\w+", "([a-z_]+)"', text))
        used |= set(re.findall(r'"(log_stop_[a-z]+)"', text))
    used |= {item.badge_key for item in STATE_INFO.values()}
    used |= {item.message_key for item in STATE_INFO.values() if item.message_key}
    missing = sorted(key for key in used if key not in MESSAGES["cs"])
    assert not missing, missing
    assert tr("state_paused_storage", "en") == "PAUSED – STORAGE"
    assert "znovu připojte původní disk" in tr("msg_paused_storage", "cs")


# -- widget tests ---------------------------------------------------------------

tk = pytest.importorskip("tkinter")


class NoKeepAwake:
    def start(self, pid: int) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display not available")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def dialogs(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(f"tkinter.messagebox.{name}", lambda *a, _n=name, **kw: calls.append((_n, a)) or "ok")
    monkeypatch.setattr("tkinter.messagebox.askyesno", lambda *a, **kw: calls.append(("askyesno", a)) or True)
    return calls


def make_app(root, **kwargs):
    from macos_data_rescue.gui.app import DataRescueApp

    factory = kwargs.pop("runner_factory", lambda events: CommandRunner(events, keep_awake_factory=NoKeepAwake))
    return DataRescueApp(root, runner_factory=factory, **kwargs)


def pump(root, until, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        root.update()
        time.sleep(0.02)


def disabled(widget) -> bool:
    return widget.instate(["disabled"])


def finish(app, root, state: OpState) -> None:
    pump(root, lambda: app.state is state and not app.runner.busy)


def test_guided_rescue_flow_through_real_cli(root, dialogs, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "doc.txt", b"content")
    dest = tmp_path / "rescue" / "user-data"
    job_dir = tmp_path / "rescue" / ".rescue"
    app = make_app(root, initial_job_dir=job_dir, initial_source=source, initial_dest=dest, lang="cs")
    pump(root, lambda: app._loading is None)
    assert app.job_dir is None and not disabled(app.btn_init)
    assert "Příprava" in app.notebook.tab(0, "text")

    app.init_job()
    assert disabled(app.btn_init) and disabled(app.btn_open_job) and disabled(app.btn_preflight)
    pump(root, lambda: app.snapshot is not None)
    assert app.job_dir == job_dir and app.state is OpState.DONE
    levels = {app.preflight_tree.item(i, "tags")[0] for i in app.preflight_tree.get_children()}
    assert "ok" in levels and "fail" not in levels
    entry = app.path_widgets["source"][1]
    assert entry.instate(["readonly"]) and disabled(app.btn_init) and disabled(app.profile_buttons["volume"])

    # unscanned visible-home: the guide runs preflight + scan
    assert app._guide_action().spec.args[:5] == ("scan", "--job-dir", str(job_dir), "--phase", "visible-home")
    app.execute_recommended_step()
    assert app.state is OpState.RUNNING
    assert disabled(app.btn_scan) and disabled(app.btn_approve) and disabled(app.btn_close_job)
    assert disabled(app.btn_guide) and not disabled(app.btn_stop)
    assert str(app.phase_combo.cget("state")) == "disabled"
    finish(app, root, OpState.DONE)
    pump(root, lambda: app._guide_action().kind == "run" and app._guide_action().spec.subcommand == "copy")
    app.execute_recommended_step()
    finish(app, root, OpState.DONE)
    pump(root, lambda: app.badges["badge_copied"].cget("text") == "Zkopírováno: 1")
    assert (dest / "Desktop" / "doc.txt").read_bytes() == b"content"

    # hidden-home is still unscanned, then the gated phases come up
    pump(root, lambda: app._guide_action().spec is not None and app._guide_action().spec.subcommand == "scan")
    app.execute_recommended_step()
    finish(app, root, OpState.DONE)
    pump(root, lambda: app._guide_action().kind == "approve")
    assert app._guide_action().phase == "app-data"

    opened: list = []
    import macos_data_rescue.gui.app as app_module
    monkeypatch.setattr(app_module, "ApprovalDialog", lambda *args: opened.append(args))
    app.execute_recommended_step()
    assert [args[1:] for args in opened] == [(job_dir, "app-data")]
    assert app.state is OpState.DONE and not app.runner.busy, "approve must never run from the guide"
    monkeypatch.undo()

    # the real dialog requires a name and an explicit confirmation
    dialog = app_module.ApprovalDialog(app, job_dir, "app-data")
    assert disabled(dialog.btn_confirm)
    dialog.name_var.set("Jana Nováková")
    assert disabled(dialog.btn_confirm)
    dialog.consent_var.set(True)
    dialog._update()
    assert not disabled(dialog.btn_confirm)
    dialog.confirm()
    finish(app, root, OpState.DONE)
    pump(root, lambda: (app.snapshot.approvals.get("app-data") or "").endswith("by=Jana Nováková"))

    app.set_language("en")
    assert app.badges["badge_copied"].cget("text") == "Copied: 1"
    assert app.state_badge.cget("text") == "DONE"
    assert "Setup" in app.notebook.tab(0, "text")

    app.cust_format_var.set("markdown")
    app.cust_lang_var.set("cs")
    app.generate_customer_report()
    finish(app, root, OpState.DONE)
    assert app.last_customer_report_path == dest.parent / "recovery-report-cs.md"
    assert app.last_customer_report_path.exists()
    pump(root, lambda: "macOS Data Rescue Report" in app.tech_report_text.get("1.0", "end"))
    assert not [c for c in dialogs if c[0] == "showerror"]


def test_storage_pause_offers_resume_with_same_options(root, dialogs, tmp_path: Path) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "a.txt", b"a")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    app = make_app(root, initial_job_dir=job_dir, lang="cs")
    pump(root, lambda: app.snapshot is not None)
    copy = CommandSpec(("copy", "--job-dir", str(job_dir), "--phase", "all", "--timeout", "120", "--limit", "9"))
    app.last_copy_spec = copy
    app.state = OpState.RUNNING
    app.events.put(RunFinished((CommandSpec(("preflight",)), copy), 1, 3, False))
    pump(root, lambda: app.state is OpState.PAUSED_STORAGE)
    assert app.state_badge.cget("text") == "POZASTAVENO – ÚLOŽIŠTĚ"
    assert "znovu připojte původní disk" in app.banner_label.cget("text")
    assert app.banner.winfo_manager() == "grid" and not disabled(app.btn_resume)
    assert not disabled(app.btn_scan), "a paused job is idle; only the child process locks controls"

    started: list = []
    app.runner.start = lambda steps, keep_awake=False: started.append(list(steps))
    app.resume_paused()
    assert started == [[CommandSpec(("preflight", "--job-dir", str(job_dir))),
                        CommandSpec(("resume", *copy.args[1:]))]]


def test_opening_paused_job_shows_storage_pause(root, dialogs, tmp_path: Path) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "a.txt", b"a")
    job_dir, _, dest = init_and_scan(tmp_path, source)
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    conn.execute("insert or replace into config values('activity', ?)",
                 ('{"operation": "copy", "phase": "all", "state": "paused", "reason": "source unavailable", '
                  '"path": null, "current_bytes": 0, "transferred_bytes": 0, "rate_bytes_s": 0}',))
    conn.commit()
    conn.close()
    app = make_app(root, initial_job_dir=job_dir, lang="en")
    pump(root, lambda: app.state is OpState.PAUSED_STORAGE)
    assert app.source_var.get() == str(source.resolve())
    assert app.dest_var.get() == str(dest.resolve())
    assert "Reason: source unavailable" in app.banner_label.cget("text")
    assert app.badges["badge_pending"].cget("text") == "Pending: 1"
    assert app.path_widgets["dest"][1].instate(["readonly"])


def test_invalid_numbers_are_rejected_before_running(root, dialogs, tmp_path: Path) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "a.txt", b"a")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    app = make_app(root, initial_job_dir=job_dir, lang="en")
    pump(root, lambda: app.snapshot is not None)
    app.file_timeout_var.set("-5")
    app.start_copy()
    app.file_timeout_var.set("30")
    app.limit_var.set("1.5")
    app.start_copy()
    app.limit_var.set("")
    app.scan_budget_var.set("nan")
    app.start_scan()
    assert [c[0] for c in dialogs] == ["showerror"] * 3
    assert app.state is OpState.IDLE and not app.runner.busy


def test_stop_is_graceful_and_keeps_controls_locked(root, dialogs, tmp_path: Path) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "a.txt", b"a")
    job_dir, _, _ = init_and_scan(tmp_path, source)
    sleeper = [sys.executable, "-c", "import time\ntry:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n    raise SystemExit(130)"]
    app = make_app(root, initial_job_dir=job_dir, lang="cs", runner_factory=lambda events: CommandRunner(
        events, command=lambda spec: sleeper, keep_awake_factory=NoKeepAwake))
    pump(root, lambda: app.snapshot is not None)
    assert app.btn_stop.cget("text") == "Zastavit"
    app._start([CommandSpec(("copy", "--job-dir", str(job_dir)))])
    pump(root, lambda: app.runner._proc is not None)
    app.stop_current_operation()
    assert app.state is OpState.STOPPING
    assert disabled(app.btn_stop) and disabled(app.btn_copy) and disabled(app.btn_open_job)
    finish(app, root, OpState.STOPPED)
    assert app._last_finish.returncode == 130
    assert not disabled(app.btn_copy)
