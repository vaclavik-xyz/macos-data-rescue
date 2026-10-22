"""Tk widgets of the technician console.

All rescue work runs through the ``macos-data-rescue`` CLI in a child process
(see ``runner``); the console only builds vetted commands, shows progress and
locks controls. Threading rule: only this module touches Tk, and only from the
main loop. Worker threads communicate exclusively through ``self.events``,
drained by ``_pump``; manifest reads run off the main thread (``monitor``).

Layout: a header (name + status pill) above three tabs. The rescue tab leads
with a status card (``status.status_view``: headline, one sentence, one
primary action; raw ``next`` text behind "Details"), then progress, then the
controls; rarely changed options and the activity log are collapsed. The
window keeps a fixed 1024×800 size: long texts wrap, elide or scroll.
"""
from __future__ import annotations

import logging
import queue
import subprocess  # nosec B404
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Any

from ..candidates import duplicate_candidates
from ..manifest import GATED_PHASES
from ..reporting import format_size, report
from . import commands as cmd
from .commands import CommandSpec, GuideAction
from .i18n import LANGUAGE_NAMES, PHASE_DESCRIPTIONS, elide_middle, format_percent, phases_for_profile, tr
from .monitor import BackgroundTasks, JobMonitor, JobSnapshot, SnapshotResult, TaskDone, telemetry_from
from .runner import CommandRunner, OutputLine, RunFinished, RunnerBusy, RunnerNotice
from .palette import is_dark_appearance, palette
from .state import STOPPABLE, OpState, control_states, info, reconcile_with_activity, state_after_exit
from .status import COUNTER_KEYS, action_label, counter_items, progress_summary, status_view

LOG = logging.getLogger(__name__)

PUMP_MS = 100
POLL_MS = 1000
MAX_EVENTS_PER_PUMP = 300
UNRESPONSIVE_AFTER_S = 5.0
WINDOW_SIZE = (1024, 800)
# spacing grid (px)
S1, S2, S3 = 8, 16, 24
LEVEL_COLORS = {"ok": "green", "warn": "amber", "fail": "red"}


def set_enabled(widget: Any, enabled: bool, *, readonly: bool = False) -> None:
    """Enable a ttk widget; disabled entries become read-only (selectable)."""
    if readonly:
        widget.state(["!readonly", "!disabled"] if enabled else ["readonly"])
    else:
        widget.state(["!disabled"] if enabled else ["disabled"])


def set_primary(button: ttk.Button, enabled: bool) -> None:
    """Enable a primary button; Aqua draws a default button blue even when disabled."""
    set_enabled(button, enabled)
    button.configure(default="active" if enabled else "normal")


class Disclosure:
    """A "Title ▸" link that shows or hides a gridded content widget.

    The content must already be gridded once (so ``grid()`` restores its
    options); it starts collapsed.
    """

    def __init__(self, parent: tk.Misc, content: tk.Widget, *, font: tkfont.Font, color: str,
                 on_toggle: Callable[[bool], None] | None = None) -> None:
        self.content = content
        self.expanded = False
        self._title = ""
        self._on_toggle = on_toggle
        self.label = ttk.Label(parent, font=font, foreground=color, cursor="pointinghand", takefocus=True)
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            self.label.bind(sequence, lambda _e: self.toggle())
        content.grid_remove()

    def set_title(self, title: str) -> None:
        self._title = title
        self._render()

    def toggle(self) -> None:
        self.set_expanded(not self.expanded)

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self.expanded:
            return
        self.expanded = expanded
        if expanded:
            self.content.grid()
        else:
            self.content.grid_remove()
        self._render()
        if self._on_toggle is not None:
            self._on_toggle(expanded)

    def _render(self) -> None:
        self.label.configure(text=f"{self._title} {'▾' if self.expanded else '▸'}")


class DataRescueApp:
    def __init__(self, root: tk.Tk, initial_job_dir: Path | None = None, initial_source: Path | None = None,
                 initial_dest: Path | None = None, lang: str = "cs", *,
                 runner_factory: Callable[[queue.Queue], CommandRunner] = CommandRunner) -> None:
        self.root = root
        self.colors = palette(is_dark_appearance(root))
        self.lang = lang if lang in LANGUAGE_NAMES else "cs"
        self.events: queue.Queue = queue.Queue()
        self.runner = runner_factory(self.events)
        self.monitor = JobMonitor(self.events)
        self.tasks = BackgroundTasks(self.events)
        self._task_callbacks: dict[int, Callable[[TaskDone], None]] = {}

        self.state = OpState.IDLE
        self.job_dir: Path | None = None
        self.snapshot: JobSnapshot | None = None
        self._loading: tuple[Path, bool] | None = None  # (job dir, report errors in a dialog)
        self._last_read_error: str | None = None
        self._last_finish: RunFinished | None = None
        self._finished_at: float | None = None  # monitor clock when the last command finished
        # True from a successful start until its RunFinished is handled (the
        # runner itself turns idle slightly earlier, as soon as the child is reaped)
        self._own_run = False
        self._running_spec: CommandSpec | None = None  # main command of the running sequence
        self._on_success: Callable[[], None] | None = None
        self._close_requested = False
        self._destroyed = False
        self.last_copy_spec: CommandSpec | None = None
        self.last_customer_report_path: Path | None = None
        self._failed_rows: tuple = ()
        self._candidates: list[dict] = []
        self._preflight_summary: tuple[str, dict] | None = None
        self._unresponsive_s = 0
        self._current_file = ""

        style = ttk.Style()
        if "aqua" in style.theme_names():
            style.theme_use("aqua")
        base = tkfont.nametofont("TkDefaultFont").actual()
        family, size = base["family"], abs(int(base["size"])) or 13
        # type scale: title / headline / section / body / caption; monospace only for log and details
        self.fonts = {
            "title": tkfont.Font(root, family=family, size=size + 4, weight="bold"),
            "headline": tkfont.Font(root, family=family, size=size + 7, weight="bold"),
            "section": tkfont.Font(root, family=family, size=size, weight="bold"),
            "body": tkfont.Font(root, family=family, size=size),
            "caption": tkfont.Font(root, family=family, size=size - 2),
            "mono": tkfont.Font(root, family="Menlo", size=size - 2),
        }

        self.source_var = tk.StringVar(value=str(initial_source) if initial_source else "")
        self.dest_var = tk.StringVar(value=str(initial_dest) if initial_dest else "")
        self.job_dir_var = tk.StringVar(value=str(initial_job_dir) if initial_job_dir else "")
        self.profile_var = tk.StringVar(value="customer-home")
        self.excludes_var = tk.StringVar()
        self.phase_var = tk.StringVar(value="visible-home")
        self.file_timeout_var = tk.StringVar(value="3600")
        self.scan_budget_var = tk.StringVar(value="300")
        self.limit_var = tk.StringVar()
        self.caffeinate_var = tk.BooleanVar(value=True)
        # on by default in the console (the CLI default is off); see docs/agent-runbook.md
        self.auto_reconnect_var = tk.BooleanVar(value=True)
        self.reconnect_cooldown_var = tk.StringVar(value=cmd.number_text(cmd.DEFAULT_COOLDOWN))
        self.reconnect_wait_var = tk.StringVar(value=cmd.number_text(cmd.DEFAULT_WAIT))
        self.max_reconnects_var = tk.StringVar(value=str(cmd.DEFAULT_MAX_RECONNECTS))
        self.lang_var = tk.StringVar(value=LANGUAGE_NAMES[self.lang])
        self.cust_format_var = tk.StringVar(value="pdf")
        self.cust_lang_var = tk.StringVar(value=self.lang)

        self._build()
        self.set_language(self.lang)
        self._set_state(OpState.IDLE)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(PUMP_MS, self._pump)
        self.root.after(POLL_MS, self._poll)
        if initial_job_dir:
            # quiet: the directory may be where a new job is about to be created
            self._open_job(initial_job_dir, quiet=True)

    def _t(self, key: str, **kwargs: object) -> str:
        return tr(key, self.lang, **kwargs)

    # ------------------------------------------------------------------ layout

    def _label(self, parent: tk.Misc, kind: str = "body", *, muted: bool = False, **kwargs: Any) -> ttk.Label:
        if muted:
            kwargs.setdefault("foreground", self.colors["muted"])
        return ttk.Label(parent, font=self.fonts[kind], **kwargs)

    def _disclosure(self, parent: tk.Misc, content: tk.Widget,
                    on_toggle: Callable[[bool], None] | None = None) -> Disclosure:
        return Disclosure(parent, content, font=self.fonts["body"], color=self.colors["blue"], on_toggle=on_toggle)

    @staticmethod
    def _wrap_to(label: ttk.Label, container: tk.Misc, margin: int = 0) -> None:
        """Wrap ``label`` to the current width of ``container`` (minus ``margin``)."""
        def resize(event: tk.Event) -> None:
            label.configure(wraplength=max(200, event.width - margin))
        container.bind("<Configure>", resize, add="+")

    def _section_gap(self, parent: tk.Misc, row: int) -> None:
        ttk.Separator(parent, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew",
                                                        pady=(S2, S1 + S1 // 2))

    def _build(self) -> None:
        root = self.root
        root.geometry("{}x{}".format(*WINDOW_SIZE))
        root.minsize(900, 700)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, padding=(S3, S2, S3, S1))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.title_label = self._label(header, "title", text="macOS Data Rescue")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.lang_combo = ttk.Combobox(header, textvariable=self.lang_var, values=list(LANGUAGE_NAMES.values()),
                                       state="readonly", width=9)
        self.lang_combo.grid(row=0, column=1, padx=(0, S2))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_changed)
        self.state_badge = tk.Label(header, font=self.fonts["section"], padx=12, pady=3, borderwidth=0)
        self.state_badge.grid(row=0, column=2, sticky="e")
        self.lbl_health = self._label(header, "caption", foreground=self.colors["red"])
        self.lbl_health.grid(row=1, column=0, columnspan=3, sticky="e", pady=(S1 // 2, 0))
        self.lbl_health.grid_remove()

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=S2, pady=(0, S2))
        self.tab_setup = ttk.Frame(self.notebook, padding=S2)
        self.tab_rescue = ttk.Frame(self.notebook, padding=S2)
        self.tab_results = ttk.Frame(self.notebook, padding=S2)
        for tab in (self.tab_setup, self.tab_rescue, self.tab_results):
            self.notebook.add(tab, text="")
        self._build_setup_tab()
        self._build_rescue_tab()
        self._build_results_tab()

    def _build_setup_tab(self) -> None:
        frame = self.tab_setup
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        paths = ttk.Frame(frame)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self.paths_group = paths
        self.path_widgets: dict[str, tuple[ttk.Label, ttk.Entry, ttk.Button, ttk.Label]] = {}
        rows = (("source", self.source_var, self._browse_source), ("dest", self.dest_var, self._browse_dest),
                ("job", self.job_dir_var, self._browse_job_dir))
        for index, (name, var, browse) in enumerate(rows):
            label = self._label(paths)
            label.grid(row=index * 2, column=0, sticky="w", padx=(0, S1), pady=(S1 if index else 0, 0))
            entry = ttk.Entry(paths, textvariable=var)
            entry.grid(row=index * 2, column=1, sticky="ew", pady=(S1 if index else 0, 0))
            button = ttk.Button(paths, command=browse)
            button.grid(row=index * 2, column=2, padx=(S1, 0), pady=(S1 if index else 0, 0))
            hint = self._label(paths, "caption", muted=True)
            hint.grid(row=index * 2 + 1, column=1, sticky="w", pady=(2, 0))
            self.path_widgets[name] = (label, entry, button, hint)

        self.lbl_profile = self._label(paths)
        self.lbl_profile.grid(row=6, column=0, sticky="w", padx=(0, S1), pady=(S2, 0))
        profile_row = ttk.Frame(paths)
        profile_row.grid(row=6, column=1, columnspan=2, sticky="w", pady=(S2, 0))
        self.profile_buttons: dict[str, ttk.Radiobutton] = {}
        for value in ("customer-home", "volume", "restore"):
            button = ttk.Radiobutton(profile_row, value=value, variable=self.profile_var,
                                     command=self._on_profile_change)
            button.pack(side="left", padx=(0, S2))
            self.profile_buttons[value] = button
        self.excludes_row = ttk.Frame(paths)
        self.lbl_excludes = self._label(self.excludes_row)
        self.lbl_excludes.pack(side="left", padx=(0, S1))
        self.excludes_entry = ttk.Entry(self.excludes_row, textvariable=self.excludes_var)
        self.excludes_entry.pack(side="left", fill="x", expand=True)
        self.lbl_job_loaded = self._label(paths, "caption", muted=True, wraplength=760, justify="left")

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="ew", pady=(S3, 0))
        self.btn_init = ttk.Button(buttons, command=self.init_job, default="active")
        self.btn_init.pack(side="left", padx=(0, S1))
        self.btn_open_job = ttk.Button(buttons, command=self.open_existing_job)
        self.btn_open_job.pack(side="left", padx=(0, S1))
        self.btn_close_job = ttk.Button(buttons, command=self.close_job)
        self.btn_close_job.pack(side="left")
        self.btn_preflight = ttk.Button(buttons, command=self.run_preflight)
        self.btn_preflight.pack(side="right")

        self._section_gap(frame, 2)
        results = ttk.Frame(frame)
        results.grid(row=3, column=0, sticky="ew", pady=(0, S1))
        results.columnconfigure(0, weight=1)
        self.results_group = self._label(results, "section")
        self.results_group.grid(row=0, column=0, sticky="w")
        self.preflight_summary_label = self._label(results, wraplength=900, justify="left")
        self.preflight_summary_label.grid(row=1, column=0, sticky="w", pady=(S1 // 2, 0))
        self._wrap_to(self.preflight_summary_label, results)

        tree_frame = ttk.Frame(frame)
        tree_frame.grid(row=4, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.preflight_tree = ttk.Treeview(tree_frame, columns=("level", "check", "detail"),
                                           show="headings", height=6)
        self.preflight_tree.column("level", width=110, anchor="w", stretch=False)
        self.preflight_tree.column("check", width=170, stretch=False)
        self.preflight_tree.column("detail", width=560)
        for level, color in LEVEL_COLORS.items():
            self.preflight_tree.tag_configure(level, foreground=self.colors[color])
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.preflight_tree.yview)
        self.preflight_tree.configure(yscrollcommand=scroll.set)
        self.preflight_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_rescue_tab(self) -> None:
        frame = self.tab_rescue
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)

        # status card: headline, one sentence, one primary action, raw details on demand
        card = ttk.LabelFrame(frame, padding=(S2, S1, S2, S2))
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        self.status_card = card
        self.status_headline = self._label(card, "headline")
        self.status_headline.grid(row=0, column=0, sticky="w")
        self.status_body = self._label(card, wraplength=700, justify="left")
        self.status_body.grid(row=1, column=0, sticky="w", pady=(S1 // 2, 0))
        self._wrap_to(self.status_body, card, margin=260)
        self.btn_guide = ttk.Button(card, command=self.execute_recommended_step, default="active")
        self.btn_guide.grid(row=0, column=1, rowspan=2, sticky="e", padx=(S2, 0))
        self.btn_resume = ttk.Button(card, command=self.resume_paused, default="active")
        self.btn_resume.grid(row=0, column=1, rowspan=2, sticky="e", padx=(S2, 0))
        detail = ttk.Frame(card)
        detail.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(S1, 0))
        detail.columnconfigure(0, weight=1)
        self.detail_text = tk.Text(detail, font=self.fonts["mono"], wrap="word", height=5, width=1,
                                   relief="flat", borderwidth=0, highlightthickness=0, padx=S1, pady=S1 // 2,
                                   state="disabled")
        self.detail_text.grid(row=0, column=0, sticky="ew")
        detail_scroll = ttk.Scrollbar(detail, orient="vertical", command=self.detail_text.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_disclosure = self._disclosure(card, detail)
        self.detail_disclosure.label.grid(row=2, column=0, sticky="w", pady=(S1, 0))

        # progress: one data bar, one summary line, the current file, one counter line
        progress = ttk.Frame(frame)
        progress.grid(row=1, column=0, sticky="ew", pady=(S2 + S1 // 2, 0))
        progress.columnconfigure(0, weight=1)
        self.progress_frame = progress
        self.prog_bytes = ttk.Progressbar(progress, mode="determinate", maximum=1.0)
        self.prog_bytes.grid(row=0, column=0, sticky="ew")
        self.lbl_percent = self._label(progress, "section", width=5, anchor="e")
        self.lbl_percent.grid(row=0, column=1, sticky="e", padx=(S1, 0))
        self.lbl_summary = self._label(progress)
        self.lbl_summary.grid(row=1, column=0, columnspan=2, sticky="w", pady=(S1, 0))
        # width=1: the label takes the column width instead of requesting the path's width
        self.lbl_current = self._label(progress, "caption", muted=True, width=1)
        self.lbl_current.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.lbl_current.grid_remove()
        progress.bind("<Configure>", lambda _e: self._render_current_file(), add="+")
        counters = ttk.Frame(progress)
        counters.grid(row=3, column=0, columnspan=2, sticky="w", pady=(S1, 0))
        self.counters: dict[str, ttk.Label] = {}
        for index, key in enumerate(COUNTER_KEYS):
            if index:
                self._label(counters, muted=True, text="·").pack(side="left", padx=S1 // 2)
            counter = self._label(counters)
            counter.pack(side="left")
            self.counters[key] = counter
        self.lbl_storage = self._label(progress, foreground=self.colors["orange"])
        self.lbl_storage.grid(row=4, column=0, columnspan=2, sticky="w", pady=(S1 // 2, 0))
        self.lbl_storage.grid_remove()

        self._section_gap(frame, 2)

        controls = ttk.Frame(frame)
        controls.grid(row=3, column=0, sticky="ew")
        controls.columnconfigure(5, weight=1)
        self.lbl_phase = self._label(controls)
        self.lbl_phase.grid(row=0, column=0, sticky="w", padx=(0, S1))
        self.phase_combo = ttk.Combobox(controls, textvariable=self.phase_var,
                                        values=phases_for_profile(None), state="readonly", width=14)
        self.phase_combo.grid(row=0, column=1, sticky="w", padx=(0, S2))
        self.phase_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_phase())
        self.btn_scan = ttk.Button(controls, command=self.start_scan)
        self.btn_scan.grid(row=0, column=2, sticky="w", padx=(0, S1))
        self.btn_copy = ttk.Button(controls, command=self.start_copy)
        self.btn_copy.grid(row=0, column=3, sticky="w", padx=(0, S1))
        self.btn_stop = ttk.Button(controls, command=self.stop_current_operation)
        self.btn_stop.grid(row=0, column=4, sticky="w")
        self.btn_approve = ttk.Button(controls, command=self.approve_phase_dialog)
        self.btn_approve.grid(row=0, column=6, sticky="e")
        self.phase_desc_label = self._label(controls, "caption", muted=True, wraplength=760, justify="left")
        self.phase_desc_label.grid(row=1, column=0, columnspan=7, sticky="w", pady=(S1 // 2, 0))
        self._wrap_to(self.phase_desc_label, controls)

        options_row = ttk.Frame(controls)
        options_row.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(S1, 0))
        advanced = ttk.Frame(controls, padding=(S2, S1, 0, 0))
        advanced.grid(row=3, column=0, columnspan=7, sticky="ew")
        # the options and the log share the bottom of the tab: one open at a time keeps
        # the window at its fixed size with the status card and progress always visible
        self.advanced_disclosure = self._disclosure(
            options_row, advanced, on_toggle=lambda expanded: expanded and self.log_disclosure.set_expanded(False))
        self.advanced_disclosure.label.pack(side="left")
        self.lbl_reconnect_hint = self._label(options_row, "caption")
        self.lbl_reconnect_hint.pack(side="left", padx=(S2, 0))

        self.option_widgets: list[tuple[ttk.Label, ttk.Entry]] = []
        for index, var in enumerate((self.file_timeout_var, self.scan_budget_var, self.limit_var)):
            label = self._label(advanced)
            label.grid(row=0, column=index * 2, sticky="w", padx=(S3 if index else 0, S1))
            entry = ttk.Entry(advanced, textvariable=var, width=7)
            entry.grid(row=0, column=index * 2 + 1, sticky="w")
            self.option_widgets.append((label, entry))
        self.chk_auto_reconnect = ttk.Checkbutton(advanced, variable=self.auto_reconnect_var,
                                                  command=self._apply_controls)
        self.chk_auto_reconnect.grid(row=1, column=0, columnspan=6, sticky="w", pady=(S1, 0))
        self.reconnect_widgets: list[tuple[ttk.Label, ttk.Entry]] = []
        for index, var in enumerate((self.reconnect_cooldown_var, self.reconnect_wait_var, self.max_reconnects_var)):
            label = self._label(advanced)
            label.grid(row=2, column=index * 2, sticky="w", padx=(S3 if index else S3, S1), pady=(S1 // 2, 0))
            entry = ttk.Entry(advanced, textvariable=var, width=7)
            entry.grid(row=2, column=index * 2 + 1, sticky="w", pady=(S1 // 2, 0))
            self.reconnect_widgets.append((label, entry))
        self.chk_caffeinate = ttk.Checkbutton(advanced, variable=self.caffeinate_var)
        self.chk_caffeinate.grid(row=3, column=0, columnspan=6, sticky="w", pady=(S1, 0))

        self._section_gap(frame, 4)

        log_header = ttk.Frame(frame)
        log_header.grid(row=5, column=0, sticky="ew")
        log_header.columnconfigure(0, weight=1)
        log_box = ttk.Frame(frame)
        log_box.grid(row=6, column=0, sticky="nsew", pady=(S1, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, wrap="char", height=4, width=1, font=self.fonts["mono"], bg="#1e1e1e",
                                fg="#e0e0e0", relief="flat", borderwidth=0, highlightthickness=0, padx=S1,
                                pady=S1 // 2, state="disabled")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        for tag, color in (("stdout", "#d4d4d4"), ("info", "#60a5fa"), ("success", "#4ade80"),
                           ("warn", "#facc15"), ("error", "#f87171")):
            self.log_text.tag_configure(tag, foreground=color)
        self.btn_clear_log = ttk.Button(log_header, command=self._clear_log)
        self.btn_clear_log.grid(row=0, column=1, sticky="e")
        self.btn_clear_log.grid_remove()

        def log_toggled(expanded: bool) -> None:
            if expanded:
                self.advanced_disclosure.set_expanded(False)
                self.btn_clear_log.grid()
            else:
                self.btn_clear_log.grid_remove()

        self.log_disclosure = self._disclosure(log_header, log_box, on_toggle=log_toggled)
        self.log_disclosure.label.grid(row=0, column=0, sticky="w")

    def _build_results_tab(self) -> None:
        frame = self.tab_results
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        verify = ttk.Frame(frame)
        verify.grid(row=0, column=0, sticky="ew")
        verify.columnconfigure(0, weight=1)
        self.verify_group = self._label(verify, "section")
        self.verify_group.grid(row=0, column=0, sticky="w")
        self.verify_info = self._label(verify, "caption", muted=True, wraplength=700, justify="left")
        self.verify_info.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._wrap_to(self.verify_info, verify, margin=200)
        self.btn_verify = ttk.Button(verify, command=self.start_verify)
        self.btn_verify.grid(row=0, column=1, rowspan=2, sticky="e", padx=(S2, 0))

        self._section_gap(frame, 1)

        dups_head = ttk.Frame(frame)
        dups_head.grid(row=2, column=0, sticky="ew")
        dups_head.columnconfigure(0, weight=1)
        self.dup_group = self._label(dups_head, "section")
        self.dup_group.grid(row=0, column=0, sticky="w")
        self.dups_info = self._label(dups_head, "caption", muted=True, wraplength=900, justify="left")
        self.dups_info.grid(row=1, column=0, sticky="w", pady=(2, S1))
        self._wrap_to(self.dups_info, dups_head)

        dups = ttk.Frame(frame)
        dups.grid(row=4, column=0, sticky="nsew")
        dups.columnconfigure(0, weight=1, uniform="dups")
        dups.columnconfigure(1, weight=1, uniform="dups")
        dups.rowconfigure(1, weight=1)
        self.lbl_failed = self._label(dups)
        self.lbl_failed.grid(row=0, column=0, sticky="w", pady=(0, S1 // 2))
        self.failed_tree = ttk.Treeview(dups, columns=("path", "status", "size"), show="headings", height=5,
                                        selectmode="browse")
        self.failed_tree.column("path", width=240)
        self.failed_tree.column("status", width=100, anchor="center")
        self.failed_tree.column("size", width=80, anchor="e")
        self.failed_tree.grid(row=1, column=0, sticky="nsew", padx=(0, S1))
        self.failed_tree.bind("<<TreeviewSelect>>", lambda _e: self._clear_candidates())
        self.btn_find_dups = ttk.Button(dups, command=self.find_duplicates_for_selected)
        self.btn_find_dups.grid(row=2, column=0, sticky="w", pady=(S1, 0))
        self.lbl_cands = self._label(dups)
        self.lbl_cands.grid(row=0, column=1, sticky="w", padx=(S1, 0), pady=(0, S1 // 2))
        self.cand_tree = ttk.Treeview(dups, columns=("path", "evidence"), show="headings", height=5,
                                      selectmode="browse")
        self.cand_tree.column("path", width=240)
        self.cand_tree.column("evidence", width=200)
        self.cand_tree.grid(row=1, column=1, sticky="nsew", padx=(S1, 0))
        self.btn_recover = ttk.Button(dups, command=self.recover_from_candidate)
        self.btn_recover.grid(row=2, column=1, sticky="w", padx=(S1, 0), pady=(S1, 0))

        self._section_gap(frame, 5)

        reports = ttk.Frame(frame)
        reports.grid(row=6, column=0, sticky="ew")
        reports.columnconfigure(5, weight=1)
        self.cust_group = self._label(reports, "section")
        self.cust_group.grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, S1))
        self.lbl_rep_fmt = self._label(reports)
        self.lbl_rep_fmt.grid(row=1, column=0, sticky="w", padx=(0, S1))
        formats = ttk.Frame(reports)
        formats.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(formats, text="PDF", value="pdf", variable=self.cust_format_var).pack(side="left")
        ttk.Radiobutton(formats, text="Markdown", value="markdown",
                        variable=self.cust_format_var).pack(side="left", padx=(S1, 0))
        self.lbl_rep_lang = self._label(reports)
        self.lbl_rep_lang.grid(row=1, column=2, sticky="w", padx=(S3, S1))
        langs = ttk.Frame(reports)
        langs.grid(row=1, column=3, sticky="w")
        self.rb_rep_cs = ttk.Radiobutton(langs, value="cs", variable=self.cust_lang_var)
        self.rb_rep_cs.pack(side="left")
        self.rb_rep_en = ttk.Radiobutton(langs, value="en", variable=self.cust_lang_var)
        self.rb_rep_en.pack(side="left", padx=(S1, 0))
        buttons = ttk.Frame(reports)
        buttons.grid(row=1, column=6, sticky="e")
        self.btn_customer_report = ttk.Button(buttons, command=self.generate_customer_report)
        self.btn_customer_report.pack(side="left", padx=(0, S1))
        self.btn_open_report = ttk.Button(buttons, command=self.open_last_report)
        self.btn_open_report.pack(side="left")
        self.lbl_customer_report = self._label(reports, "caption", muted=True, width=1)
        self.lbl_customer_report.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(S1 // 2, 0))

        tech_head = ttk.Frame(frame)
        tech_head.grid(row=7, column=0, sticky="ew", pady=(S2, 0))
        tech_head.columnconfigure(0, weight=1)
        tech = ttk.Frame(frame)
        tech.grid(row=8, column=0, sticky="nsew", pady=(S1, 0))
        tech.columnconfigure(0, weight=1)
        tech.rowconfigure(0, weight=1)
        self.tech_report_text = tk.Text(tech, wrap="word", height=6, width=1, font=self.fonts["mono"],
                                        relief="flat", borderwidth=0, highlightthickness=0, padx=S1,
                                        pady=S1 // 2)
        scroll = ttk.Scrollbar(tech, orient="vertical", command=self.tech_report_text.yview)
        self.tech_report_text.configure(yscrollcommand=scroll.set)
        self.tech_report_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tech_buttons = ttk.Frame(tech_head)
        tech_buttons.grid(row=0, column=1, sticky="e")
        self.btn_refresh_tech = ttk.Button(tech_buttons, command=self._show_tech_report)
        self.btn_refresh_tech.pack(side="left", padx=(0, S1))
        self.btn_save_tech = ttk.Button(tech_buttons, command=self.save_tech_report)
        self.btn_save_tech.pack(side="left")

        def tech_toggled(expanded: bool) -> None:
            # the expanded report shares the free height with the failed-file tables
            frame.rowconfigure(8, weight=1 if expanded else 0)

        self.tech_disclosure = self._disclosure(tech_head, tech, on_toggle=tech_toggled)
        self.tech_disclosure.label.grid(row=0, column=0, sticky="w")

    # ------------------------------------------------------------ localization

    def set_language(self, lang: str) -> None:
        self.lang = lang if lang in LANGUAGE_NAMES else "cs"
        t = self._t
        self.lang_var.set(LANGUAGE_NAMES[self.lang])
        self.root.title(t("app_title"))
        for index, key in enumerate(("tab_setup", "tab_rescue", "tab_results")):
            self.notebook.tab(index, text=f"  {t(key)}  ")

        for name, (label, _entry, button, hint) in self.path_widgets.items():
            label.configure(text=t(f"{name}_path"))
            hint.configure(text=t(f"{name}_hint"))
            button.configure(text=t("browse"))
        self.lbl_profile.configure(text=t("profile"))
        for value, key in (("customer-home", "profile_home"), ("volume", "profile_volume"),
                           ("restore", "profile_restore")):
            self.profile_buttons[value].configure(text=t(key))
        self.lbl_excludes.configure(text=t("volume_excludes"))
        self.lbl_job_loaded.configure(text=t("job_loaded_note"))
        self.btn_preflight.configure(text=t("btn_preflight"))
        self.btn_init.configure(text=t("btn_init"))
        self.btn_open_job.configure(text=t("btn_open_job"))
        self.btn_close_job.configure(text=t("btn_close_job"))
        self.results_group.configure(text=t("preflight_title"))
        for column, key in (("level", "col_status"), ("check", "col_check"), ("detail", "col_detail")):
            self.preflight_tree.heading(column, text=t(key), anchor="w")
        for item in self.preflight_tree.get_children():
            level = self.preflight_tree.item(item, "tags")[0]
            self.preflight_tree.set(item, "level", t(f"level_{level}"))

        self.detail_disclosure.set_title(t("detail"))
        self.lbl_phase.configure(text=t("select_phase"))
        self.btn_scan.configure(text=t("btn_scan"))
        self.btn_copy.configure(text=t("btn_copy"))
        self.btn_stop.configure(text=t("btn_stop"))
        self.chk_caffeinate.configure(text=t("caffeinate"))
        for (label, _entry), key in zip(self.option_widgets, ("per_file_timeout", "scan_budget", "limit_optional")):
            label.configure(text=t(key))
        self.chk_auto_reconnect.configure(text=t("auto_reconnect"))
        for (label, _entry), key in zip(self.reconnect_widgets,
                                        ("reconnect_cooldown", "reconnect_wait", "max_reconnects")):
            label.configure(text=t(key))
        self.advanced_disclosure.set_title(t("advanced"))
        self.log_disclosure.set_title(t("log_title"))
        self.btn_clear_log.configure(text=t("clear_log"))

        self.verify_group.configure(text=t("verify_title"))
        self.verify_info.configure(text=t("verify_info"))
        self.btn_verify.configure(text=t("btn_verify"))
        self.dup_group.configure(text=t("dups_title"))
        self.dups_info.configure(text=t("dups_info"))
        self.lbl_failed.configure(text=t("failed_files_label"))
        for column, key in (("path", "col_path"), ("status", "col_status"), ("size", "col_size")):
            self.failed_tree.heading(column, text=t(key))
        self.btn_find_dups.configure(text=t("btn_find_dups"))
        self.lbl_cands.configure(text=t("cands_label"))
        self.cand_tree.heading("path", text=t("col_cand_path"))
        self.cand_tree.heading("evidence", text=t("col_evidence"))
        self.btn_recover.configure(text=t("btn_recover_cand"))

        self.cust_group.configure(text=t("cust_report_title"))
        self.lbl_rep_fmt.configure(text=t("report_format"))
        self.lbl_rep_lang.configure(text=t("report_lang"))
        self.rb_rep_cs.configure(text=t("lang_cs"))
        self.rb_rep_en.configure(text=t("lang_en"))
        self.btn_customer_report.configure(text=t("btn_gen_cust_report"))
        self.btn_open_report.configure(text=t("btn_open_report"))
        self.tech_disclosure.set_title(t("tech_report_title"))
        self.btn_refresh_tech.configure(text=t("btn_refresh_tech"))
        self.btn_save_tech.configure(text=t("btn_save_tech"))

        self._render_preflight_summary()
        self._render_snapshot()
        self._render_state()

    def _on_language_changed(self, _event: object) -> None:
        names = {name: code for code, name in LANGUAGE_NAMES.items()}
        self.set_language(names.get(self.lang_var.get(), "cs"))

    # ----------------------------------------------------------- event loop

    def _pump(self) -> None:
        """Drain worker events on the Tk thread (the only place results are applied)."""
        for _ in range(MAX_EVENTS_PER_PUMP):
            if self._destroyed:
                return
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._dispatch(event)
            except Exception:
                LOG.exception("handling GUI event %s failed", type(event).__name__)
        if not self._destroyed:
            self.root.after(PUMP_MS, self._pump)

    def _dispatch(self, event: object) -> None:
        if isinstance(event, OutputLine):
            self._on_output(event)
        elif isinstance(event, RunnerNotice):
            self._log(self._t(event.key, **event.params) + "\n", event.level)
        elif isinstance(event, RunFinished):
            self._on_run_finished(event)
        elif isinstance(event, SnapshotResult):
            self._on_snapshot(event)
        elif isinstance(event, TaskDone):
            callback = self._task_callbacks.pop(event.token, None)
            if callback is not None:
                callback(event)

    def _poll(self) -> None:
        if self._destroyed:
            return
        target = self.job_dir or (self._loading[0] if self._loading else None)
        if target is not None:
            self.monitor.request(target)
        stalled = int(self.monitor.stalled_for())
        unresponsive = stalled if stalled >= UNRESPONSIVE_AFTER_S else 0
        if unresponsive != self._unresponsive_s:
            self._unresponsive_s = unresponsive
            self._render_health()
        if self.state is OpState.WAITING:
            self._render_state()  # live countdown, even while a manifest read hangs
        self.root.after(POLL_MS, self._poll)

    def _submit(self, function: Callable[[], Any], callback: Callable[[TaskDone], None]) -> None:
        self._task_callbacks[self.tasks.submit(function)] = callback

    # ------------------------------------------------------------- state

    def _set_state(self, state: OpState) -> None:
        self.state = state
        self._render_state()

    def _render_state(self) -> None:
        state_info = info(self.state)
        self.state_badge.configure(text=self._t(state_info.badge_key), bg=self.colors[state_info.color],
                                   fg=self.colors["on_accent"])
        self.btn_stop.configure(text=self._t("btn_stop_waiting" if self.state is OpState.WAITING else "btn_stop"))
        self._render_status()
        self._apply_controls()

    def _render_status(self) -> None:
        """The status card: headline, sentence, primary action and raw details."""
        activity = (self.snapshot.activity if self.snapshot is not None else None) or {}
        action = self._guide_action()
        escalation = self.state is OpState.PAUSED_STORAGE and self._escalation_pending()
        view = status_view(self.state, self.lang, job_loaded=self.job_dir is not None, own_child=self._own_run,
                           guide_text=self.snapshot.guide_text if self.snapshot is not None else "",
                           action=action, escalation=escalation, activity=activity,
                           now=datetime.now(timezone.utc), running=self._running_spec, finish=self._last_finish)
        self.status_headline.configure(text=view.headline, foreground=self.colors[view.tone] if view.tone else "")
        self.status_body.configure(text=view.body)
        if view.action == "resume":
            self.btn_resume.configure(text=self._t("btn_resume_anyway" if escalation else "btn_resume"))
            self.btn_guide.grid_remove()
            self.btn_resume.grid()
        elif view.action == "guide":
            self.btn_guide.configure(text=action_label(action, self.lang))
            self.btn_resume.grid_remove()
            self.btn_guide.grid()
        else:
            self.btn_guide.grid_remove()
            self.btn_resume.grid_remove()
        self._set_detail_text(self._detail_text(activity))

    def _detail_text(self, activity: dict) -> str:
        lines = []
        if activity.get("state") in ("paused", "waiting") and activity.get("reason"):
            lines.append(self._t("msg_pause_reason", reason=activity["reason"]))
        finish = self._last_finish
        if self.state is OpState.FAILED and finish is not None and finish.error:
            lines.append(finish.error)
        if self.snapshot is not None:
            if lines:
                lines.append("")
            lines.append(self.snapshot.guide_text)
        return "\n".join(lines)

    def _set_detail_text(self, text: str) -> None:
        if self.detail_text.get("1.0", "end-1c") == text:
            return  # keep the operator's scroll position across polls
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def _render_health(self) -> None:
        if self._unresponsive_s:
            self.lbl_health.configure(text=self._t("msg_job_unresponsive", seconds=self._unresponsive_s))
            self.lbl_health.grid()
        else:
            self.lbl_health.grid_remove()

    def _apply_controls(self) -> None:
        c = self._controls()
        loading = self._loading is not None
        fields = c["job_fields"] and not loading
        for name, (_label, entry, button, _hint) in self.path_widgets.items():
            set_enabled(entry, fields, readonly=True)
            set_enabled(button, fields)
        for button in self.profile_buttons.values():
            set_enabled(button, fields)
        set_enabled(self.excludes_entry, fields, readonly=True)
        set_primary(self.btn_init, c["init"] and not loading)
        set_enabled(self.btn_open_job, c["open_job"] and not loading)
        set_enabled(self.btn_close_job, c["close_job"])
        set_enabled(self.btn_preflight, c["preflight"] and not loading)
        self.phase_combo.configure(state="readonly" if c["phase"] else "disabled")
        for _label, entry in self.option_widgets:
            set_enabled(entry, c["options"])
        set_enabled(self.chk_caffeinate, c["options"])
        set_enabled(self.chk_auto_reconnect, c["options"])
        reconnect = bool(self.auto_reconnect_var.get())
        for _label, entry in self.reconnect_widgets:
            set_enabled(entry, c["options"] and reconnect)
        self.lbl_reconnect_hint.configure(text=self._t("reconnect_hint_on" if reconnect else "reconnect_hint_off"),
                                          foreground=self.colors["muted" if reconnect else "amber"])
        set_enabled(self.btn_scan, c["scan"])
        set_enabled(self.btn_copy, c["copy"])
        set_enabled(self.btn_verify, c["verify"])
        set_enabled(self.btn_stop, c["stop"])
        set_primary(self.btn_resume, c["resume"])
        set_primary(self.btn_guide, c["guide"] and self._guide_action().kind != "none")
        set_enabled(self.btn_approve, c["approve"] and self._phase_needs_approval())
        set_enabled(self.btn_find_dups, c["recover"])
        set_enabled(self.btn_recover, c["recover"] and bool(self._candidates))
        restore = self.snapshot is not None and self.snapshot.profile == "restore"
        set_enabled(self.btn_customer_report, c["customer_report"] and not restore)
        set_enabled(self.btn_open_report, self.last_customer_report_path is not None)
        set_enabled(self.btn_refresh_tech, self.job_dir is not None)
        if self.job_dir is not None:
            self.lbl_job_loaded.grid(row=8, column=1, columnspan=2, sticky="w", pady=(S1, 0))
        else:
            self.lbl_job_loaded.grid_remove()

    def _controls(self) -> dict[str, bool]:
        return control_states(self.state, job_loaded=self.job_dir is not None, own_child=self._own_run)

    # ------------------------------------------------------------ job loading

    def _open_job(self, job_dir: Path, *, quiet: bool = False) -> None:
        self._reset_job()
        self._loading = (job_dir, quiet)
        self.job_dir_var.set(str(job_dir))
        self.monitor.request(job_dir)
        self._apply_controls()

    def close_job(self) -> None:
        if self._controls()["close_job"]:
            self._reset_job()

    def _reset_job(self) -> None:
        """Forget everything that belongs to the previously open job."""
        self.job_dir = None
        self.snapshot = None
        self._loading = None
        self.last_copy_spec = None
        self.last_customer_report_path = None
        self._last_finish = None
        self._last_read_error = None
        self._clear_candidates()
        self._set_tech_report("")
        self.lbl_customer_report.configure(text="")
        self._set_state(OpState.IDLE)
        self._render_snapshot()

    def _on_snapshot(self, result: SnapshotResult) -> None:
        loading = self._loading
        if loading is not None and result.job_dir == loading[0]:
            self._loading = None
            if result.snapshot is None:
                if loading[1]:
                    self._log(self._t("log_job_read_error", error=result.error) + "\n", "warn")
                else:
                    messagebox.showerror(self._t("err_title"), self._t("err_job_load", error=result.error))
                self._apply_controls()
                return
            self.job_dir = result.job_dir
            self._apply_job_fields(result.snapshot)
            self.load_tech_report()
            self.notebook.select(self.tab_rescue)
        elif result.job_dir != self.job_dir:
            return  # stale read for a job that is no longer open
        if result.snapshot is None:
            if result.error != self._last_read_error:
                self._last_read_error = result.error
                self._log(self._t("log_job_read_error", error=result.error) + "\n", "warn")
            return
        self._last_read_error = None
        self.snapshot = result.snapshot
        # a read that started before the last command finished must not undo its final state
        stale = self._finished_at is not None and result.requested_at < self._finished_at
        if not stale:
            new_state = reconcile_with_activity(self.state, (result.snapshot.activity or {}).get("state"),
                                                child_running=self._own_run)
            if new_state is not self.state:
                self._set_state(new_state)
        self._render_snapshot()

    def _apply_job_fields(self, snapshot: JobSnapshot) -> None:
        self.source_var.set(str(snapshot.source))
        self.dest_var.set(str(snapshot.dest))
        self.job_dir_var.set(str(snapshot.job_dir))
        self.profile_var.set(snapshot.profile)
        self.excludes_var.set(", ".join(snapshot.excludes))
        self._on_profile_change()
        phases = phases_for_profile(snapshot.profile)
        self.phase_combo.configure(values=phases)
        if self.phase_var.get() not in phases:
            self.phase_var.set(phases[0])

    # ---------------------------------------------------------- rendering

    def _render_snapshot(self) -> None:
        self._render_telemetry()
        self._render_failed_rows()
        self._render_phase()
        self._render_status()

    def _render_telemetry(self) -> None:
        if self.snapshot is None:
            counts: dict[str, int] = {}
            fraction, summary, current, losses, reconnects = None, "", "", 0, 0
        else:
            view = telemetry_from(self.snapshot)
            counts = view.counts
            fraction = view.bytes_fraction
            if fraction is None and view.total_files:
                fraction = view.rescued_files / view.total_files
            summary = progress_summary(view, self.lang)
            current = f"{view.current_path} ({format_size(view.current_bytes)})" if view.current_path else ""
            losses, reconnects = view.storage_losses, view.reconnects
        self.prog_bytes.configure(value=fraction or 0)
        self.lbl_percent.configure(text=format_percent(fraction, self.lang) if fraction is not None else "")
        self.lbl_summary.configure(text=summary or "—")
        self._current_file = self._t("current_file", file=current) if current else ""
        self._render_current_file()
        for key, text, tone in counter_items(counts, self.lang):
            self.counters[key].configure(text=text, foreground=self.colors[tone] if tone else "")
        if losses or reconnects:
            self.lbl_storage.configure(text=self._t("storage_events_summary", losses=losses, reconnects=reconnects))
            self.lbl_storage.grid()
        else:
            self.lbl_storage.configure(text="")
            self.lbl_storage.grid_remove()

    def _render_current_file(self) -> None:
        if not self._current_file:
            self.lbl_current.grid_remove()
            return
        self.lbl_current.grid()
        width = self.lbl_current.winfo_width()
        text = elide_middle(self._current_file, width - 4, self.fonts["caption"].measure) if width > 1 \
            else self._current_file
        if self.lbl_current.cget("text") != text:
            self.lbl_current.configure(text=text)

    def _guide_action(self) -> GuideAction:
        if self.snapshot is None or self.job_dir is None:
            return GuideAction("none")
        return cmd.parse_guide_action(self.snapshot.guide_text, self.job_dir)

    def _render_failed_rows(self) -> None:
        rows = self.snapshot.failed_rows if self.snapshot is not None else ()
        if rows == self._failed_rows:
            return  # keep the operator's selection while polling
        self._failed_rows = rows
        self.failed_tree.delete(*self.failed_tree.get_children())
        for path, status, size in rows:
            self.failed_tree.insert("", "end", values=(path, status, format_size(size)))
        self._clear_candidates()

    def _render_phase(self) -> None:
        phase = self.phase_var.get()
        descriptions = PHASE_DESCRIPTIONS.get(self.lang, PHASE_DESCRIPTIONS["cs"])
        self.phase_desc_label.configure(text=descriptions.get(phase, ""))
        approved = self.snapshot.approvals.get(phase) if self.snapshot is not None else None
        # only consent-gated phases show the consent button
        if not self._is_gated(phase):
            self.btn_approve.configure(text=self._t("no_approval_needed"))
            self.btn_approve.grid_remove()
        else:
            self.btn_approve.configure(text=self._t("approved_badge", by=approved) if approved
                                       else self._t("btn_approve"))
            self.btn_approve.grid()
        self._apply_controls()

    def _is_gated(self, phase: str) -> bool:
        profile = self.snapshot.profile if self.snapshot is not None else self.profile_var.get()
        return profile == "customer-home" and phase in GATED_PHASES

    def _phase_needs_approval(self) -> bool:
        phase = self.phase_var.get()
        return (self.snapshot is not None and self._is_gated(phase)
                and self.snapshot.approvals.get(phase) is None)

    def _render_preflight_summary(self) -> None:
        if self._preflight_summary is None:
            self.preflight_summary_label.configure(text=self._t("preflight_none"), foreground=self.colors["muted"])
            return
        key, values = self._preflight_summary
        color = {"preflight_fail": self.colors["red"], "preflight_ok": self.colors["green"]}.get(key, "")
        self.preflight_summary_label.configure(text=self._t(key, **values), foreground=color)

    def _log(self, text: str, tag: str = "stdout") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text, tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------- running

    def _start(self, steps: list[CommandSpec], *, on_success: Callable[[], None] | None = None) -> bool:
        if info(self.state).busy or self.runner.busy:
            messagebox.showwarning(self._t("info_title"), self._t("err_busy"))
            return False
        if any(step.subcommand == "preflight" for step in steps):
            self.preflight_tree.delete(*self.preflight_tree.get_children())
            self._preflight_summary = ("preflight_running", {})
            self._render_preflight_summary()
        try:
            self.runner.start(steps, keep_awake=bool(self.caffeinate_var.get()))
        except RunnerBusy:
            messagebox.showwarning(self._t("info_title"), self._t("err_busy"))
            return False
        copies = [step for step in steps if step.subcommand in {"copy", "resume"}]
        if copies:
            self.last_copy_spec = copies[-1]
        self._on_success = on_success
        self._last_finish = None
        self._own_run = True
        self._running_spec = steps[-1]
        self._set_state(OpState.RUNNING)
        return True

    def _on_output(self, event: OutputLine) -> None:
        self._log(event.text if event.text.endswith("\n") else event.text + "\n")
        subcommand = event.spec.subcommand
        if subcommand == "preflight":
            parsed = cmd.parse_preflight_line(event.text)
            if parsed is not None:
                level, name, detail = parsed
                self.preflight_tree.insert("", "end", values=(self._t(f"level_{level}"), name, detail),
                                           tags=(level,))
            elif event.text.startswith("preflight="):
                fields = dict(part.split("=", 1) for part in event.text.split() if "=" in part)
                key = "preflight_fail" if fields.get("preflight") == "fail" else "preflight_ok"
                self._preflight_summary = (key, {"checks": fields.get("checks", "?"),
                                                 "warnings": fields.get("warnings", "?"),
                                                 "failures": fields.get("failures", "?")})
                self._render_preflight_summary()
        elif subcommand == "customer-report":
            path = cmd.parse_report_path(event.text)
            if path is not None:
                self.last_customer_report_path = path
                self.lbl_customer_report.configure(text=self._t("report_written", path=path))

    def _on_run_finished(self, event: RunFinished) -> None:
        state = state_after_exit(event.returncode, stop_requested=event.stop_requested, error=bool(event.error))
        if event.error:
            self._log(self._t("log_runner_error", error=event.error) + "\n", "error")
        elif state is OpState.PAUSED_STORAGE and event.stop_requested:
            self._log(self._t("log_stopped_waiting") + "\n", "warn")
        elif state is OpState.PAUSED_STORAGE:
            self._log(self._t("log_paused_storage") + "\n", "warn")
        elif state is OpState.STOPPED:
            self._log(self._t("log_stopped") + "\n", "warn")
        elif state is OpState.DONE:
            self._log(self._t("log_finished_ok") + "\n", "success")
        else:
            self._log(self._t("log_finished_code", code=event.returncode) + "\n", "error")
        if self._preflight_summary is not None and self._preflight_summary[0] == "preflight_running":
            self._preflight_summary = None
            self._render_preflight_summary()
        if state is OpState.FAILED and event.last_spec is not None and event.last_spec.subcommand == "preflight":
            self.notebook.select(self.tab_setup)
        on_success, self._on_success = self._on_success, None
        self._last_finish = event
        self._finished_at = self.monitor.now()
        self._own_run = False
        self._running_spec = None
        if state is OpState.FAILED:
            self.log_disclosure.set_expanded(True)  # the card points to the log for the details
        self._set_state(state)
        if event.completed_all and on_success is not None:
            on_success()
        if self.job_dir is not None:
            self.monitor.request(self.job_dir)
        if self._close_requested:
            self._destroy()

    def stop_current_operation(self) -> None:
        """Stop (or "Stop waiting"): the same graceful SIGINT to the child's process group."""
        if self.state not in STOPPABLE or not self._own_run:
            return
        message = "confirm_stop_waiting_msg" if self.state is OpState.WAITING else "confirm_stop_msg"
        if messagebox.askyesno(self._t("confirm_stop_title"), self._t(message)):
            if self.runner.request_stop():
                self._set_state(OpState.STOPPING)

    def _on_close(self) -> None:
        if self.state in STOPPABLE and self._own_run:
            if not messagebox.askyesno(self._t("confirm_close_title"), self._t("confirm_close_msg")):
                return
            self._close_requested = True
            if self.runner.request_stop():
                self._set_state(OpState.STOPPING)
            return
        if self.state is OpState.STOPPING:
            if messagebox.askyesno(self._t("confirm_close_title"), self._t("confirm_close_stopping_msg")):
                self._destroy()
            return
        self._destroy()

    def _destroy(self) -> None:
        self._destroyed = True
        self.root.destroy()

    # --------------------------------------------------------- user actions

    def _path(self, var: tk.StringVar) -> Path | None:
        value = var.get().strip()
        return Path(value).expanduser() if value else None

    def _file_timeout(self) -> float | None:
        try:
            return cmd.parse_positive_float(self.file_timeout_var.get())
        except ValueError:
            messagebox.showerror(self._t("err_title"),
                                 self._t("err_invalid_number", field=self._t("field_file_timeout")))
            return None

    def _limit(self) -> tuple[bool, int | None]:
        try:
            return True, cmd.parse_optional_positive_int(self.limit_var.get())
        except ValueError:
            messagebox.showerror(self._t("err_title"), self._t("err_invalid_limit", field=self._t("field_limit")))
            return False, None

    def _reconnect_options(self) -> tuple[bool, cmd.ReconnectOptions | None]:
        """The auto-reconnect choice; (False, None) after an invalid value was reported."""
        if not self.auto_reconnect_var.get():
            return True, None
        fields = ((self.reconnect_cooldown_var, cmd.parse_non_negative_float, "err_invalid_non_negative",
                   "field_reconnect_cooldown"),
                  (self.reconnect_wait_var, cmd.parse_positive_float, "err_invalid_number", "field_reconnect_wait"),
                  (self.max_reconnects_var, cmd.parse_positive_int, "err_invalid_count", "field_max_reconnects"))
        values = []
        for var, parse, error_key, field_key in fields:
            try:
                values.append(parse(var.get()))
            except ValueError:
                messagebox.showerror(self._t("err_title"), self._t(error_key, field=self._t(field_key)))
                return False, None
        return True, cmd.ReconnectOptions(*values)

    def _storage_blocked(self) -> bool:
        return self.snapshot is not None and self.snapshot.storage_blocked

    def _escalation_pending(self) -> bool:
        """The paused copy ended with a reconnect give-up (``next``: storage-escalation)."""
        return self._storage_blocked() and (self.snapshot.activity or {}).get("state") == "paused"

    def _start_copy_spec(self, spec: CommandSpec, *, escalation: bool = False) -> None:
        """Start a copy/resume after a fresh preflight.

        Re-enabling auto-reconnect after a give-up is the operator's decision:
        ``--acknowledge-storage-loss`` is only ever added after this confirmation.
        While the escalation is pending, continuing without auto-reconnect is
        confirmed as well.
        """
        if self.job_dir is None:
            return
        escalation = escalation or self._escalation_pending()
        if cmd.needs_storage_acknowledgement(spec, blocked=self._storage_blocked(), escalation=escalation):
            if not self._ask_warning("confirm_ack_title", "confirm_ack_msg"):
                return
            spec = cmd.with_acknowledgement(spec)
        elif escalation and spec.subcommand in cmd.COPY_COMMANDS:
            if not self._ask_warning("confirm_ack_title", "confirm_continue_msg"):
                return
        self._start(cmd.with_preflight(spec, self.job_dir))

    def _ask_warning(self, title_key: str, message_key: str) -> bool:
        return bool(messagebox.askyesno(self._t(title_key), self._t(message_key),
                                        icon=messagebox.WARNING, default=messagebox.NO))

    def _browse_source(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_source_title"))
        if path:
            self.source_var.set(path)

    def _browse_dest(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_dest_title"))
        if path:
            self.dest_var.set(path)
            if not self.job_dir_var.get().strip():
                self.job_dir_var.set(str(Path(path).parent / ".rescue"))

    def _browse_job_dir(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_job_title"))
        if path:
            self.job_dir_var.set(path)

    def _on_profile_change(self) -> None:
        if self.profile_var.get() == "volume":
            self.excludes_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=4)
        else:
            self.excludes_row.grid_remove()
        if self.job_dir is None:
            phases = phases_for_profile(self.profile_var.get())
            self.phase_combo.configure(values=phases)
            if self.phase_var.get() not in phases:
                self.phase_var.set(phases[0])
            self._render_phase()

    def run_preflight(self) -> None:
        if self.job_dir is not None:
            self._start([cmd.preflight_spec(self.job_dir)])
            return
        job_dir, source, dest = (self._path(v) for v in (self.job_dir_var, self.source_var, self.dest_var))
        if job_dir is None or source is None or dest is None:
            messagebox.showerror(self._t("err_title"), self._t("err_paths_required"))
            return
        self._start([cmd.preflight_spec(job_dir, source, dest)])

    def init_job(self) -> None:
        job_dir, source, dest = (self._path(v) for v in (self.job_dir_var, self.source_var, self.dest_var))
        if job_dir is None or source is None or dest is None:
            messagebox.showerror(self._t("err_title"), self._t("err_paths_required"))
            return
        profile = self.profile_var.get()
        excludes = tuple(p.strip() for p in self.excludes_var.get().split(",") if p.strip()) \
            if profile == "volume" else ()
        steps = [cmd.preflight_spec(job_dir, source, dest), cmd.init_spec(job_dir, source, dest, profile, excludes)]

        def opened() -> None:
            self._open_job(job_dir)
            self.notebook.select(self.tab_rescue)

        self._start(steps, on_success=opened)

    def open_existing_job(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_job_title"))
        if path:
            self._open_job(Path(path))

    def approve_phase_dialog(self, phase: str | None = None) -> None:
        job_dir = self.job_dir
        phase = phase or self.phase_var.get()
        if job_dir is None or info(self.state).busy:
            return
        if not self._is_gated(phase):
            messagebox.showinfo(self._t("info_title"), self._t("err_not_gated", phase=phase))
            return
        ApprovalDialog(self, job_dir, phase)

    def start_scan(self) -> None:
        if self.job_dir is None:
            return
        budget_text = self.scan_budget_var.get().strip()
        budget = None
        if budget_text:
            try:
                budget = cmd.parse_positive_float(budget_text)
            except ValueError:
                messagebox.showerror(self._t("err_title"),
                                     self._t("err_invalid_number", field=self._t("field_scan_budget")))
                return
        ok, limit = self._limit()
        if ok:
            spec = cmd.scan_spec(self.job_dir, self.phase_var.get(), budget, limit)
            self._start(cmd.with_preflight(spec, self.job_dir))

    def start_copy(self) -> None:
        if self.job_dir is None:
            return
        timeout = self._file_timeout()
        if timeout is None:
            return
        ok, limit = self._limit()
        if not ok:
            return
        ok, reconnect = self._reconnect_options()
        if ok:
            self._start_copy_spec(cmd.copy_spec(self.job_dir, self.phase_var.get(), timeout, limit,
                                                reconnect=reconnect))

    def resume_paused(self) -> None:
        if self.job_dir is None:
            return
        ok, reconnect = self._reconnect_options()
        if not ok:
            return
        last = self.last_copy_spec
        if last is not None and last.option("--job-dir") == str(self.job_dir):
            spec = cmd.with_reconnect(cmd.resume_from(last), reconnect)
        else:
            timeout = self._file_timeout()
            if timeout is None:
                return
            activity = (self.snapshot.activity if self.snapshot is not None else None) or {}
            phase = activity.get("phase") or self.phase_var.get()
            spec = cmd.copy_spec(self.job_dir, phase, timeout, command="resume", reconnect=reconnect)
        self._start_copy_spec(spec)

    def start_verify(self) -> None:
        if self.job_dir is None:
            return
        timeout = self._file_timeout()
        if timeout is not None:
            self._start([cmd.verify_spec(self.job_dir, self.phase_var.get(), timeout)])

    def execute_recommended_step(self) -> None:
        if self.job_dir is None:
            return
        action = self._guide_action()
        if action.kind == "approve" and action.phase:
            # consent is never recorded by the guide; the operator must confirm a name
            self.approve_phase_dialog(action.phase)
        elif action.kind in ("run", "acknowledge") and action.spec is not None:
            if action.spec.subcommand not in cmd.COPY_COMMANDS:
                self._start(cmd.with_preflight(action.spec, self.job_dir))
                return
            ok, reconnect = self._reconnect_options()
            if ok:
                # the checkbox decides; an "acknowledge" step always asks before re-enabling the loop
                self._start_copy_spec(cmd.with_reconnect(action.spec, reconnect),
                                      escalation=action.kind == "acknowledge")
        elif self.snapshot is not None:
            messagebox.showinfo(self._t("info_title"), self.snapshot.guide_text)

    # ---------------------------------------------------------- duplicates

    def _selected_failed_path(self) -> str | None:
        selection = self.failed_tree.selection()
        if not selection:
            return None
        return str(self.failed_tree.set(selection[0], "path"))

    def _clear_candidates(self) -> None:
        self._candidates = []
        self.cand_tree.delete(*self.cand_tree.get_children())
        self._apply_controls()

    def find_duplicates_for_selected(self) -> None:
        path = self._selected_failed_path()
        job_dir = self.job_dir
        if path is None or job_dir is None:
            messagebox.showinfo(self._t("info_title"), self._t("err_select_failed"))
            return
        self._clear_candidates()

        def done(result: TaskDone) -> None:
            if result.error is not None:
                messagebox.showerror(self._t("err_title"), str(result.error))
                return
            if job_dir != self.job_dir or path != self._selected_failed_path():
                return
            self._candidates = list(result.result.get("candidates", []))
            for item in self._candidates:
                self.cand_tree.insert("", "end", values=(item["path"], ", ".join(item.get("evidence", []))))
            if not self._candidates:
                messagebox.showinfo(self._t("info_title"), self._t("no_candidates", path=path))
            self._apply_controls()

        self._submit(lambda: duplicate_candidates(job_dir, path, limit=20), done)

    def recover_from_candidate(self) -> None:
        failed = self._selected_failed_path()
        selection = self.cand_tree.selection()
        if failed is None or not selection or self.job_dir is None:
            messagebox.showinfo(self._t("info_title"), self._t("err_select_both"))
            return
        candidate = self._candidates[self.cand_tree.index(selection[0])]
        timeout = self._file_timeout()
        if timeout is None:
            return
        message = self._t("confirm_fallback_msg", path=failed, candidate=candidate["path"],
                          evidence=", ".join(candidate.get("evidence", [])))
        if messagebox.askyesno(self._t("confirm_fallback_title"), message):
            # a single-file recovery never auto-reconnects (the CLI refuses --path with it)
            spec = cmd.copy_spec(self.job_dir, "all", timeout, path=failed, fallback_from=candidate["path"])
            self._start(cmd.with_preflight(spec, self.job_dir))

    # ------------------------------------------------------------- reports

    def generate_customer_report(self) -> None:
        if self.job_dir is None:
            return
        self._start([cmd.customer_report_spec(self.job_dir, self.cust_format_var.get(), self.cust_lang_var.get())])

    def open_last_report(self) -> None:
        path = self.last_customer_report_path
        if path is not None:
            try:
                proc = subprocess.Popen(["/usr/bin/open", str(path)])  # nosec B603
            except OSError as exc:
                messagebox.showerror(self._t("err_title"), str(exc))
                return
            threading.Thread(target=proc.wait, name="open-report", daemon=True).start()

    def _set_tech_report(self, text: str) -> None:
        self.tech_report_text.delete("1.0", "end")
        self.tech_report_text.insert("end", text)

    def _show_tech_report(self) -> None:
        self.tech_disclosure.set_expanded(True)
        self.load_tech_report()

    def load_tech_report(self) -> None:
        job_dir = self.job_dir
        if job_dir is None:
            return
        self._set_tech_report(self._t("tech_report_loading"))

        def done(result: TaskDone) -> None:
            if job_dir == self.job_dir:
                self._set_tech_report(str(result.error) if result.error is not None else result.result)

        self._submit(lambda: report(job_dir, "markdown"), done)

    def save_tech_report(self) -> None:
        content = self.tech_report_text.get("1.0", "end-1c")
        path = filedialog.asksaveasfilename(title=self._t("save_tech_title"), defaultextension=".md",
                                            filetypes=[("Markdown", "*.md"), ("All files", "*.*")])
        if not path:
            return

        def done(result: TaskDone) -> None:
            if result.error is not None:
                messagebox.showerror(self._t("err_title"), str(result.error))
            else:
                messagebox.showinfo(self._t("info_title"), self._t("saved_msg", path=path))

        self._submit(lambda: Path(path).write_text(content, encoding="utf-8"), done)


class ApprovalDialog:
    """Explicit consent dialog: a name and a confirmation are both required."""

    def __init__(self, app: DataRescueApp, job_dir: Path, phase: str) -> None:
        self.app = app
        self.job_dir = job_dir
        self.phase = phase
        t = app._t
        self.window = tk.Toplevel(app.root)
        self.window.title(t("approve_dialog_title", phase=phase))
        self.window.transient(app.root)
        self.window.resizable(False, False)
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=t("approve_dialog_text", phase=phase), wraplength=440, justify="left").pack(anchor="w")
        ttk.Label(body, text=t("approve_by_label")).pack(anchor="w", pady=(12, 4))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(body, textvariable=self.name_var, width=40)
        self.name_entry.pack(fill="x")
        self.consent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text=t("approve_consent_check"), variable=self.consent_var,
                        command=self._update).pack(anchor="w", pady=(10, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        self.btn_confirm = ttk.Button(buttons, text=t("btn_confirm_approval"), command=self.confirm)
        self.btn_confirm.pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text=t("btn_cancel"), command=self.window.destroy).pack(side="right")
        self.name_var.trace_add("write", lambda *_args: self._update())
        self._update()
        self.name_entry.focus_set()
        try:
            self.window.grab_set()
        except tk.TclError as exc:  # window not viewable yet on some window managers
            LOG.warning("approval dialog could not grab input: %s", exc)

    def _update(self) -> None:
        set_enabled(self.btn_confirm, bool(self.name_var.get().strip()) and bool(self.consent_var.get()))

    def confirm(self) -> None:
        if not (self.name_var.get().strip() and self.consent_var.get()):
            return
        spec = cmd.approve_spec(self.job_dir, self.phase, self.name_var.get())
        self.window.destroy()
        self.app._start([spec])


def run_gui(job_dir: Path | None = None, source: Path | None = None, dest: Path | None = None,
            lang: str = "cs") -> int:
    root = tk.Tk()
    DataRescueApp(root, initial_job_dir=job_dir, initial_source=source, initial_dest=dest, lang=lang)
    root.mainloop()
    return 0
