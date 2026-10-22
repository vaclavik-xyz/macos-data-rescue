"""Tk widgets of the technician console.

All rescue work runs through the ``macos-data-rescue`` CLI in a child process
(see ``runner``); the console only builds vetted commands, shows progress and
locks controls. Threading rule: only this module touches Tk, and only from the
main loop. Worker threads communicate exclusively through ``self.events``,
drained by ``_pump``; manifest reads run off the main thread (``monitor``).
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
from typing import Any

from ..candidates import duplicate_candidates
from ..manifest import GATED_PHASES
from ..reporting import format_size, report
from . import commands as cmd
from .commands import CommandSpec, GuideAction
from .i18n import LANGUAGE_NAMES, PHASE_DESCRIPTIONS, format_eta, format_rate, format_waiting, phases_for_profile, tr
from .monitor import BackgroundTasks, JobMonitor, JobSnapshot, SnapshotResult, TaskDone, telemetry_from
from .runner import CommandRunner, OutputLine, RunFinished, RunnerBusy, RunnerNotice
from .palette import is_dark_appearance, palette
from .state import STOPPABLE, OpState, control_states, info, reconcile_with_activity, state_after_exit, wait_view

LOG = logging.getLogger(__name__)

PUMP_MS = 100
POLL_MS = 1000
MAX_EVENTS_PER_PUMP = 300
UNRESPONSIVE_AFTER_S = 5.0
# palette names (gui/palette.py), resolved for the current light/dark appearance
BADGES = (("badge_copied", "green"), ("badge_fallback", "cyan"), ("badge_pending", "blue"),
          ("badge_timed_out", "amber"), ("badge_failed", "red"), ("badge_compressed", "violet"),
          ("badge_skipped", "gray"))
LEVEL_COLORS = {"ok": "green", "warn": "amber", "fail": "red"}


def set_enabled(widget: Any, enabled: bool, *, readonly: bool = False) -> None:
    """Enable a ttk widget; disabled entries become read-only (selectable)."""
    if readonly:
        widget.state(["!readonly", "!disabled"] if enabled else ["readonly"])
    else:
        widget.state(["!disabled"] if enabled else ["disabled"])


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
        self._on_success: Callable[[], None] | None = None
        self._close_requested = False
        self._destroyed = False
        self.last_copy_spec: CommandSpec | None = None
        self.last_customer_report_path: Path | None = None
        self._failed_rows: tuple = ()
        self._candidates: list[dict] = []
        self._preflight_summary: tuple[str, dict] | None = None
        self._unresponsive_s = 0

        style = ttk.Style()
        if "aqua" in style.theme_names():
            style.theme_use("aqua")

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

    def _build(self) -> None:
        root = self.root
        root.minsize(1000, 720)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root, padding=(16, 10, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(header, text="macOS Data Rescue", font=("System", 18, "bold"))
        self.title_label.grid(row=0, column=0, sticky="w")
        self.subtitle_label = ttk.Label(header, font=("System", 11))
        self.subtitle_label.grid(row=1, column=0, sticky="w")
        right = ttk.Frame(header)
        right.grid(row=0, column=1, rowspan=2, sticky="e")
        self.lbl_lang = ttk.Label(right)
        self.lbl_lang.grid(row=0, column=0, padx=(0, 6))
        self.lang_combo = ttk.Combobox(right, textvariable=self.lang_var, values=list(LANGUAGE_NAMES.values()),
                                       state="readonly", width=9)
        self.lang_combo.grid(row=0, column=1, padx=(0, 16))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_changed)
        self.state_badge = ttk.Label(right, font=("System", 12, "bold"), padding=(10, 4), relief="groove")
        self.state_badge.grid(row=0, column=2)
        self.lbl_health = ttk.Label(right, foreground=self.colors["red"], font=("System", 10))
        self.lbl_health.grid(row=1, column=0, columnspan=3, sticky="e")

        # state banner (paused storage / failure / stopping), visible on every tab
        self.banner = ttk.Frame(root, padding=(16, 2, 16, 2))
        self.banner.grid(row=1, column=0, sticky="ew")
        self.banner.columnconfigure(0, weight=1)
        self.banner_label = ttk.Label(self.banner, font=("System", 12, "bold"), wraplength=760)
        self.banner_label.grid(row=0, column=0, sticky="w")
        self.btn_resume = ttk.Button(self.banner, command=self.resume_paused)
        self.btn_resume.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=16, pady=(4, 12))
        self.tab_setup = ttk.Frame(self.notebook, padding=12)
        self.tab_rescue = ttk.Frame(self.notebook, padding=12)
        self.tab_verify = ttk.Frame(self.notebook, padding=12)
        self.tab_reports = ttk.Frame(self.notebook, padding=12)
        for tab in (self.tab_setup, self.tab_rescue, self.tab_verify, self.tab_reports):
            self.notebook.add(tab, text="")
        self._build_setup_tab()
        self._build_rescue_tab()
        self._build_verify_tab()
        self._build_reports_tab()

    def _build_setup_tab(self) -> None:
        frame = self.tab_setup
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        self.paths_group = ttk.LabelFrame(frame, padding=12)
        self.paths_group.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.paths_group.columnconfigure(1, weight=1)
        self.path_widgets: dict[str, tuple[ttk.Label, ttk.Entry, ttk.Button, ttk.Label]] = {}
        rows = (("source", self.source_var, self._browse_source), ("dest", self.dest_var, self._browse_dest),
                ("job", self.job_dir_var, self._browse_job_dir))
        for index, (name, var, browse) in enumerate(rows):
            label = ttk.Label(self.paths_group)
            label.grid(row=index * 2, column=0, sticky="w", pady=(4, 0))
            entry = ttk.Entry(self.paths_group, textvariable=var)
            entry.grid(row=index * 2, column=1, sticky="ew", padx=6, pady=(4, 0))
            button = ttk.Button(self.paths_group, command=browse)
            button.grid(row=index * 2, column=2, pady=(4, 0))
            hint = ttk.Label(self.paths_group, font=("System", 10), foreground="gray")
            hint.grid(row=index * 2 + 1, column=1, sticky="w", padx=6)
            self.path_widgets[name] = (label, entry, button, hint)

        profile_row = ttk.Frame(self.paths_group)
        profile_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.lbl_profile = ttk.Label(profile_row)
        self.lbl_profile.pack(side="left", padx=(0, 8))
        self.profile_buttons: dict[str, ttk.Radiobutton] = {}
        for value in ("customer-home", "volume", "restore"):
            button = ttk.Radiobutton(profile_row, value=value, variable=self.profile_var,
                                     command=self._on_profile_change)
            button.pack(side="left", padx=6)
            self.profile_buttons[value] = button
        self.excludes_row = ttk.Frame(self.paths_group)
        self.lbl_excludes = ttk.Label(self.excludes_row)
        self.lbl_excludes.pack(side="left", padx=(0, 8))
        self.excludes_entry = ttk.Entry(self.excludes_row, textvariable=self.excludes_var)
        self.excludes_entry.pack(side="left", fill="x", expand=True)
        self.lbl_job_loaded = ttk.Label(self.paths_group, foreground=self.colors["blue"], wraplength=820)

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.btn_preflight = ttk.Button(buttons, command=self.run_preflight)
        self.btn_preflight.pack(side="left", padx=(0, 8))
        self.btn_init = ttk.Button(buttons, command=self.init_job)
        self.btn_init.pack(side="left", padx=(0, 8))
        self.btn_open_job = ttk.Button(buttons, command=self.open_existing_job)
        self.btn_open_job.pack(side="left", padx=(0, 8))
        self.btn_close_job = ttk.Button(buttons, command=self.close_job)
        self.btn_close_job.pack(side="left")

        self.results_group = ttk.LabelFrame(frame, padding=8)
        self.results_group.grid(row=2, column=0, sticky="nsew")
        self.results_group.columnconfigure(0, weight=1)
        self.results_group.rowconfigure(0, weight=1)
        self.preflight_tree = ttk.Treeview(self.results_group, columns=("level", "check", "detail"),
                                           show="headings", height=8)
        self.preflight_tree.column("level", width=110, anchor="center")
        self.preflight_tree.column("check", width=170)
        self.preflight_tree.column("detail", width=560)
        for level, color in LEVEL_COLORS.items():
            self.preflight_tree.tag_configure(level, foreground=self.colors[color])
        scroll = ttk.Scrollbar(self.results_group, orient="vertical", command=self.preflight_tree.yview)
        self.preflight_tree.configure(yscrollcommand=scroll.set)
        self.preflight_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.preflight_summary_label = ttk.Label(self.results_group, font=("System", 11))
        self.preflight_summary_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_rescue_tab(self) -> None:
        frame = self.tab_rescue
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        self.guide_group = ttk.LabelFrame(frame, padding=10)
        self.guide_group.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.guide_group.columnconfigure(0, weight=1)
        self.guide_label = ttk.Label(self.guide_group, font=("Menlo", 11), wraplength=640, justify="left")
        self.guide_label.grid(row=0, column=0, sticky="w", padx=4)
        self.btn_guide = ttk.Button(self.guide_group, command=self.execute_recommended_step)
        self.btn_guide.grid(row=0, column=1, sticky="ne", padx=4)

        self.control_group = ttk.LabelFrame(frame, padding=10)
        self.control_group.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.control_group.columnconfigure(2, weight=1)
        self.lbl_phase = ttk.Label(self.control_group)
        self.lbl_phase.grid(row=0, column=0, sticky="w")
        self.phase_combo = ttk.Combobox(self.control_group, textvariable=self.phase_var,
                                        values=phases_for_profile(None), state="readonly", width=16)
        self.phase_combo.grid(row=0, column=1, sticky="w", padx=6)
        self.phase_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_phase())
        self.phase_desc_label = ttk.Label(self.control_group, font=("System", 10), foreground=self.colors["blue"],
                                          wraplength=520)
        self.phase_desc_label.grid(row=0, column=2, sticky="w", padx=6)
        self.btn_approve = ttk.Button(self.control_group, command=self.approve_phase_dialog)
        self.btn_approve.grid(row=0, column=3, sticky="e")

        actions = ttk.Frame(self.control_group)
        actions.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        self.btn_scan = ttk.Button(actions, command=self.start_scan)
        self.btn_scan.pack(side="left", padx=(0, 8))
        self.btn_copy = ttk.Button(actions, command=self.start_copy)
        self.btn_copy.pack(side="left", padx=(0, 8))
        self.btn_stop = ttk.Button(actions, command=self.stop_current_operation)
        self.btn_stop.pack(side="left", padx=(0, 16))
        self.chk_caffeinate = ttk.Checkbutton(actions, variable=self.caffeinate_var)
        self.chk_caffeinate.pack(side="left")

        options = ttk.Frame(self.control_group)
        options.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        self.option_widgets: list[tuple[ttk.Label, ttk.Entry]] = []
        for var in (self.file_timeout_var, self.scan_budget_var, self.limit_var):
            label = ttk.Label(options)
            label.pack(side="left", padx=(0, 4))
            entry = ttk.Entry(options, textvariable=var, width=7)
            entry.pack(side="left", padx=(0, 14))
            self.option_widgets.append((label, entry))

        reconnect = ttk.Frame(self.control_group)
        reconnect.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        self.chk_auto_reconnect = ttk.Checkbutton(reconnect, variable=self.auto_reconnect_var,
                                                  command=self._apply_controls)
        self.chk_auto_reconnect.pack(side="left", padx=(0, 14))
        self.reconnect_widgets: list[tuple[ttk.Label, ttk.Entry]] = []
        for var in (self.reconnect_cooldown_var, self.reconnect_wait_var, self.max_reconnects_var):
            label = ttk.Label(reconnect)
            label.pack(side="left", padx=(0, 4))
            entry = ttk.Entry(reconnect, textvariable=var, width=6)
            entry.pack(side="left", padx=(0, 14))
            self.reconnect_widgets.append((label, entry))

        self.dash_group = ttk.LabelFrame(frame, padding=10)
        self.dash_group.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.dash_group.columnconfigure(1, weight=1)
        self.lbl_bytes_title = ttk.Label(self.dash_group, width=10)
        self.lbl_bytes_title.grid(row=0, column=0, sticky="w")
        self.prog_bytes = ttk.Progressbar(self.dash_group, mode="determinate", maximum=1.0)
        self.prog_bytes.grid(row=0, column=1, sticky="ew", padx=6)
        self.lbl_bytes = ttk.Label(self.dash_group, width=30, anchor="e")
        self.lbl_bytes.grid(row=0, column=2, sticky="e")
        self.lbl_files_title = ttk.Label(self.dash_group, width=10)
        self.lbl_files_title.grid(row=1, column=0, sticky="w")
        self.prog_files = ttk.Progressbar(self.dash_group, mode="determinate", maximum=1.0)
        self.prog_files.grid(row=1, column=1, sticky="ew", padx=6)
        self.lbl_files = ttk.Label(self.dash_group, width=30, anchor="e")
        self.lbl_files.grid(row=1, column=2, sticky="e")
        metrics = ttk.Frame(self.dash_group)
        metrics.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        metrics.columnconfigure(2, weight=1)
        self.lbl_speed = ttk.Label(metrics, font=("System", 11, "bold"))
        self.lbl_speed.grid(row=0, column=0, sticky="w", padx=(0, 16))
        self.lbl_eta = ttk.Label(metrics, font=("System", 11))
        self.lbl_eta.grid(row=0, column=1, sticky="w", padx=(0, 16))
        self.lbl_current = ttk.Label(metrics, font=("System", 10), foreground=self.colors["muted"])
        self.lbl_current.grid(row=0, column=2, sticky="ew")
        self.lbl_storage = ttk.Label(metrics, font=("System", 10, "bold"), foreground=self.colors["orange"])
        self.lbl_storage.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        badges = ttk.Frame(self.dash_group)
        badges.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.badges: dict[str, ttk.Label] = {}
        for key, color in BADGES:
            badge = ttk.Label(badges, font=("System", 10, "bold"), foreground=self.colors[color], padding=(6, 2),
                              relief="groove")
            badge.pack(side="left", padx=3)
            self.badges[key] = badge

        self.log_group = ttk.LabelFrame(frame, padding=8)
        self.log_group.grid(row=3, column=0, sticky="nsew")
        self.log_group.columnconfigure(0, weight=1)
        self.log_group.rowconfigure(0, weight=1)
        self.log_text = tk.Text(self.log_group, wrap="char", height=9, font=("Menlo", 10), bg="#1e1e1e",
                                fg="#e0e0e0", state="disabled")
        scroll = ttk.Scrollbar(self.log_group, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        for tag, color in (("stdout", "#d4d4d4"), ("info", "#60a5fa"), ("success", "#4ade80"),
                           ("warn", "#facc15"), ("error", "#f87171")):
            self.log_text.tag_configure(tag, foreground=color)
        self.btn_clear_log = ttk.Button(self.log_group, command=self._clear_log)
        self.btn_clear_log.grid(row=1, column=0, sticky="e", pady=(4, 0))

    def _build_verify_tab(self) -> None:
        frame = self.tab_verify
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        self.verify_group = ttk.LabelFrame(frame, padding=10)
        self.verify_group.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.verify_info = ttk.Label(self.verify_group, wraplength=820)
        self.verify_info.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.btn_verify = ttk.Button(self.verify_group, command=self.start_verify)
        self.btn_verify.grid(row=1, column=0, sticky="w")

        self.dup_group = ttk.LabelFrame(frame, padding=10)
        self.dup_group.grid(row=1, column=0, sticky="nsew")
        self.dup_group.columnconfigure(0, weight=1)
        self.dup_group.columnconfigure(1, weight=1)
        self.dup_group.rowconfigure(1, weight=1)
        self.dups_info = ttk.Label(self.dup_group, wraplength=820)
        self.dups_info.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        left = ttk.Frame(self.dup_group)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        self.lbl_failed = ttk.Label(left, font=("System", 11, "bold"))
        self.lbl_failed.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.failed_tree = ttk.Treeview(left, columns=("path", "status", "size"), show="headings", height=8,
                                        selectmode="browse")
        self.failed_tree.column("path", width=240)
        self.failed_tree.column("status", width=100, anchor="center")
        self.failed_tree.column("size", width=80, anchor="e")
        self.failed_tree.grid(row=1, column=0, sticky="nsew")
        self.failed_tree.bind("<<TreeviewSelect>>", lambda _e: self._clear_candidates())
        self.btn_find_dups = ttk.Button(left, command=self.find_duplicates_for_selected)
        self.btn_find_dups.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        right = ttk.Frame(self.dup_group)
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.lbl_cands = ttk.Label(right, font=("System", 11, "bold"))
        self.lbl_cands.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.cand_tree = ttk.Treeview(right, columns=("path", "evidence"), show="headings", height=8,
                                      selectmode="browse")
        self.cand_tree.column("path", width=240)
        self.cand_tree.column("evidence", width=220)
        self.cand_tree.grid(row=1, column=0, sticky="nsew")
        self.btn_recover = ttk.Button(right, command=self.recover_from_candidate)
        self.btn_recover.grid(row=2, column=0, sticky="ew", pady=(6, 0))

    def _build_reports_tab(self) -> None:
        frame = self.tab_reports
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        self.cust_group = ttk.LabelFrame(frame, padding=10)
        self.cust_group.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        options = ttk.Frame(self.cust_group)
        options.grid(row=0, column=0, sticky="w")
        self.lbl_rep_fmt = ttk.Label(options)
        self.lbl_rep_fmt.pack(side="left", padx=(0, 6))
        ttk.Radiobutton(options, text="PDF", value="pdf", variable=self.cust_format_var).pack(side="left", padx=4)
        ttk.Radiobutton(options, text="Markdown", value="markdown",
                        variable=self.cust_format_var).pack(side="left", padx=4)
        self.lbl_rep_lang = ttk.Label(options)
        self.lbl_rep_lang.pack(side="left", padx=(16, 6))
        self.rb_rep_cs = ttk.Radiobutton(options, value="cs", variable=self.cust_lang_var)
        self.rb_rep_cs.pack(side="left", padx=4)
        self.rb_rep_en = ttk.Radiobutton(options, value="en", variable=self.cust_lang_var)
        self.rb_rep_en.pack(side="left", padx=4)
        buttons = ttk.Frame(self.cust_group)
        buttons.grid(row=1, column=0, sticky="w", pady=(8, 2))
        self.btn_customer_report = ttk.Button(buttons, command=self.generate_customer_report)
        self.btn_customer_report.pack(side="left", padx=(0, 8))
        self.btn_open_report = ttk.Button(buttons, command=self.open_last_report)
        self.btn_open_report.pack(side="left")
        self.lbl_customer_report = ttk.Label(self.cust_group, font=("System", 10), wraplength=820)
        self.lbl_customer_report.grid(row=2, column=0, sticky="w", pady=(4, 0))

        self.tech_group = ttk.LabelFrame(frame, padding=10)
        self.tech_group.grid(row=1, column=0, sticky="nsew")
        self.tech_group.columnconfigure(0, weight=1)
        self.tech_group.rowconfigure(1, weight=1)
        buttons = ttk.Frame(self.tech_group)
        buttons.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.btn_refresh_tech = ttk.Button(buttons, command=self.load_tech_report)
        self.btn_refresh_tech.pack(side="left", padx=(0, 8))
        self.btn_save_tech = ttk.Button(buttons, command=self.save_tech_report)
        self.btn_save_tech.pack(side="left")
        self.tech_report_text = tk.Text(self.tech_group, wrap="word", font=("Menlo", 10), bg="#f8f9fa",
                                        fg="#1f2937")
        scroll = ttk.Scrollbar(self.tech_group, orient="vertical", command=self.tech_report_text.yview)
        self.tech_report_text.configure(yscrollcommand=scroll.set)
        self.tech_report_text.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

    # ------------------------------------------------------------ localization

    def set_language(self, lang: str) -> None:
        self.lang = lang if lang in LANGUAGE_NAMES else "cs"
        t = self._t
        self.lang_var.set(LANGUAGE_NAMES[self.lang])
        self.root.title(t("app_title"))
        self.subtitle_label.configure(text=t("app_subtitle"))
        self.lbl_lang.configure(text=t("language_label"))
        for index, key in enumerate(("tab_setup", "tab_rescue", "tab_verify", "tab_reports")):
            self.notebook.tab(index, text=t(key))

        self.paths_group.configure(text=t("paths_title"))
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
            self.preflight_tree.heading(column, text=t(key))
        for item in self.preflight_tree.get_children():
            level = self.preflight_tree.item(item, "tags")[0]
            self.preflight_tree.set(item, "level", t(f"level_{level}"))

        self.guide_group.configure(text=t("guide_title"))
        self.control_group.configure(text=t("control_title"))
        self.lbl_phase.configure(text=t("select_phase"))
        self.btn_scan.configure(text=t("btn_scan"))
        self.btn_copy.configure(text=t("btn_copy"))
        self.btn_stop.configure(text=t("btn_stop"))
        self.btn_resume.configure(text=t("btn_resume"))
        self.chk_caffeinate.configure(text=t("caffeinate"))
        for (label, _entry), key in zip(self.option_widgets, ("per_file_timeout", "scan_budget", "limit_optional")):
            label.configure(text=t(key))
        self.chk_auto_reconnect.configure(text=t("auto_reconnect"))
        for (label, _entry), key in zip(self.reconnect_widgets,
                                        ("reconnect_cooldown", "reconnect_wait", "max_reconnects")):
            label.configure(text=t(key))
        self.dash_group.configure(text=t("telemetry_title"))
        self.lbl_bytes_title.configure(text=t("data_volume"))
        self.lbl_files_title.configure(text=t("file_count"))
        self.log_group.configure(text=t("log_title"))
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
        self.tech_group.configure(text=t("tech_report_title"))
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
        self.state_badge.configure(text=self._t(state_info.badge_key), foreground=self.colors[state_info.color])
        message = self._banner_text()
        if message:
            self.banner_label.configure(text=message, foreground=self.colors[state_info.color])
            self.banner.grid()
        else:
            self.banner.grid_remove()
        if state_info.can_resume:
            self.btn_resume.grid()
        else:
            self.btn_resume.grid_remove()
        self.btn_stop.configure(text=self._t("btn_stop_waiting" if self.state is OpState.WAITING else "btn_stop"))
        self._apply_controls()

    def _banner_text(self) -> str:
        key = info(self.state).message_key
        if key is None:
            return ""
        finish = self._last_finish
        if self.state is OpState.FAILED and finish is not None:
            spec = finish.last_spec
            if spec is not None and spec.subcommand == "preflight" and len(finish.steps) > 1:
                return self._t("msg_preflight_failed")
            command = spec.subcommand if spec is not None else "?"
            code = finish.returncode if finish.returncode is not None else finish.error
            return self._t("msg_failed", command=command, code=code)
        if self.state is OpState.WAITING:
            activity = (self.snapshot.activity if self.snapshot is not None else None) or {}
            return format_waiting(wait_view(activity, datetime.now(timezone.utc)), self.lang,
                                  stoppable=self._own_run)
        if self.state is OpState.PAUSED_STORAGE and self._escalation_pending():
            key = "msg_storage_escalation"
        text = self._t(key)
        if self.state is OpState.PAUSED_STORAGE and self.snapshot is not None:
            reason = telemetry_from(self.snapshot).pause_reason
            if reason:
                text += "\n" + self._t("msg_pause_reason", reason=reason)
        return text

    def _render_health(self) -> None:
        text = self._t("msg_job_unresponsive", seconds=self._unresponsive_s) if self._unresponsive_s else ""
        self.lbl_health.configure(text=text)

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
        set_enabled(self.btn_init, c["init"] and not loading)
        set_enabled(self.btn_open_job, c["open_job"] and not loading)
        set_enabled(self.btn_close_job, c["close_job"])
        set_enabled(self.btn_preflight, c["preflight"] and not loading)
        self.phase_combo.configure(state="readonly" if c["phase"] else "disabled")
        for _label, entry in self.option_widgets:
            set_enabled(entry, c["options"])
        set_enabled(self.chk_caffeinate, c["options"])
        set_enabled(self.chk_auto_reconnect, c["options"])
        for _label, entry in self.reconnect_widgets:
            set_enabled(entry, c["options"] and bool(self.auto_reconnect_var.get()))
        set_enabled(self.btn_scan, c["scan"])
        set_enabled(self.btn_copy, c["copy"])
        set_enabled(self.btn_verify, c["verify"])
        set_enabled(self.btn_stop, c["stop"])
        set_enabled(self.btn_resume, c["resume"])
        set_enabled(self.btn_guide, c["guide"] and self._guide_action().kind != "none")
        set_enabled(self.btn_approve, c["approve"] and self._phase_needs_approval())
        set_enabled(self.btn_find_dups, c["recover"])
        set_enabled(self.btn_recover, c["recover"] and bool(self._candidates))
        restore = self.snapshot is not None and self.snapshot.profile == "restore"
        set_enabled(self.btn_customer_report, c["customer_report"] and not restore)
        set_enabled(self.btn_open_report, self.last_customer_report_path is not None)
        set_enabled(self.btn_refresh_tech, self.job_dir is not None)
        if self.job_dir is not None:
            self.lbl_job_loaded.grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))
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
        self._render_guide()
        self._render_failed_rows()
        self._render_phase()
        if self.state in (OpState.PAUSED_STORAGE, OpState.WAITING):
            self._render_state()

    def _render_telemetry(self) -> None:
        t = self._t
        if self.snapshot is None:
            for key, badge in self.badges.items():
                badge.configure(text=f"{t(key)}: 0")
            self.prog_files.configure(value=0)
            self.prog_bytes.configure(value=0)
            self.lbl_files.configure(text="0 / 0")
            self.lbl_bytes.configure(text="—")
            self.lbl_speed.configure(text=t("speed", rate=format_rate(0)))
            self.lbl_eta.configure(text=t("eta", eta=t("eta_unknown")))
            self.lbl_current.configure(text=t("current_file", file="—"))
            self.lbl_storage.configure(text="")
            return
        view = telemetry_from(self.snapshot)
        for key, badge in self.badges.items():
            badge.configure(text=f"{t(key)}: {view.counts.get(key, 0):,}")
        self.prog_files.configure(value=view.rescued_files / view.total_files if view.total_files else 0)
        self.lbl_files.configure(text=f"{view.rescued_files:,} / {view.total_files:,}")
        fraction = view.bytes_fraction
        if fraction is None:
            self.prog_bytes.configure(value=0)
            self.lbl_bytes.configure(text="—")
        else:
            self.prog_bytes.configure(value=fraction)
            self.lbl_bytes.configure(
                text=f"{format_size(view.rescued_bytes)} / {format_size(view.total_bytes)} ({fraction * 100:.1f}%)")
        self.lbl_speed.configure(text=t("speed", rate=format_rate(view.rate_bytes_s)))
        self.lbl_eta.configure(text=t("eta", eta=format_eta(view.eta_seconds, self.lang)))
        current = f"{view.current_path} ({format_size(view.current_bytes)})" if view.current_path else "—"
        self.lbl_current.configure(text=t("current_file", file=current))
        self.lbl_storage.configure(text=t("storage_events_summary", losses=view.storage_losses,
                                          reconnects=view.reconnects) if view.storage_losses else "")

    def _guide_action(self) -> GuideAction:
        if self.snapshot is None or self.job_dir is None:
            return GuideAction("none")
        return cmd.parse_guide_action(self.snapshot.guide_text, self.job_dir)

    def _render_guide(self) -> None:
        if self.snapshot is None:
            self.guide_label.configure(text=self._t("guide_default"))
            self.btn_guide.configure(text=self._t("btn_guide_none"))
            self._apply_controls()
            return
        lines = [line.strip().replace("uv run macos-data-rescue ", "")
                 for line in self.snapshot.guide_text.splitlines()[1:] if line.strip()]
        self.guide_label.configure(text="\n".join(lines))
        action = self._guide_action()
        if action.kind == "run":
            label = self._t("btn_guide_run",
                            command=" ".join([action.spec.subcommand, *self._phase_args(action.spec)]))
        elif action.kind == "acknowledge":
            label = self._t("btn_guide_acknowledge",
                            command=" ".join([action.spec.subcommand, *self._phase_args(action.spec)]))
        elif action.kind == "approve":
            label = self._t("btn_guide_approve", phase=action.phase)
        else:
            label = self._t("btn_guide_none")
        self.btn_guide.configure(text=label)
        self._apply_controls()

    @staticmethod
    def _phase_args(spec: CommandSpec) -> list[str]:
        phase = spec.option("--phase")
        return [phase] if phase else []

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
        if not self._is_gated(phase):
            self.btn_approve.configure(text=self._t("no_approval_needed"))
        elif approved:
            self.btn_approve.configure(text=self._t("approved_badge", by=approved))
        else:
            self.btn_approve.configure(text=self._t("btn_approve"))
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
            self.preflight_summary_label.configure(text=self._t("preflight_none"), foreground="")
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
