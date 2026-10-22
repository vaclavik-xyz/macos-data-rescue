"""Graphical User Interface for macOS Data Rescue.

Built with Python standard library tkinter/ttk to provide a native macOS
technician console with zero external runtime dependencies.
Supports full localization in Czech (Čeština) and English.
"""
from __future__ import annotations

import os
import queue
import shlex
import subprocess  # nosec B404
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..activity import activity_snapshot
from ..candidates import duplicate_candidates
from ..guide import next_text
from ..manifest import (
    GATED_PHASES,
    connect,
    init_manifest,
    load_approval,
    load_config,
    manifest_path,
    record_approval,
    status_summary,
)
from ..preflight import preflight_job
from ..reporting import (
    format_size,
    report,
    write_customer_report,
)
from .i18n import CORE_PHASES, PHASE_DESCRIPTIONS, format_eta, format_rate, tr



class CaffeinateManager:
    """Inhibit macOS sleep during long data rescue operations."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        caffeinate_bin = "/usr/bin/caffeinate"
        if os.path.exists(caffeinate_bin):
            try:
                # -d: prevent display sleep, -i: prevent idle sleep, -m: prevent disk idle, -s: prevent system sleep
                self._proc = subprocess.Popen(  # nosec B603
                    [caffeinate_bin, "-d", "-i", "-m", "-s"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                self._proc = None

    def stop(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None


class CommandRunner:
    """Asynchronously execute macos-data-rescue CLI commands with live output streaming."""

    def __init__(self, log_queue: queue.Queue[tuple[str, str]]) -> None:
        self.log_queue = log_queue
        self.proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._cancel_requested = False

    @property
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def run_command(self, cmd_args: list[str], on_finish: callable) -> None:
        if self.is_running:
            raise RuntimeError("A command is already running.")
        self._cancel_requested = False

        def worker() -> None:
            full_cmd = [sys.executable, "-m", "macos_data_rescue"] + cmd_args
            display_cmd = "macos-data-rescue " + " ".join(shlex.quote(a) for a in cmd_args)
            self.log_queue.put(("info", f"\n>>> Running: {display_cmd}\n"))
            try:
                self.proc = subprocess.Popen(  # nosec B603
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if self.proc.stdout:
                    for line in iter(self.proc.stdout.readline, ""):
                        self.log_queue.put(("stdout", line))
                self.proc.wait()
                code = self.proc.returncode
                if self._cancel_requested:
                    self.log_queue.put(("warn", "\n[!] Operation cancelled by operator.\n"))
                elif code == 0:
                    self.log_queue.put(("success", f"\n[✓] Finished successfully (exit {code}).\n"))
                elif code == 3:
                    self.log_queue.put(("warn", f"\n[!] Paused due to storage loss or full disk (exit {code}).\n"))
                else:
                    self.log_queue.put(("error", f"\n[✗] Exited with error code {code}.\n"))
                on_finish(code)
            except Exception as exc:
                self.log_queue.put(("error", f"\n[✗] Process execution error: {exc}\n"))
                on_finish(1)
            finally:
                self.proc = None

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        if self.is_running and self.proc:
            self._cancel_requested = True
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class DataRescueApp:
    """Main technician GUI application with full bilingual (cs/en) support."""

    def __init__(self, root: tk.Tk, initial_job_dir: Path | None = None,
                 initial_source: Path | None = None, initial_dest: Path | None = None,
                 lang: str = "cs") -> None:
        self.root = root
        self.lang = lang if lang in ("cs", "en") else "cs"

        # Style & Aqua look
        self.style = ttk.Style()
        if "aqua" in self.style.theme_names():
            self.style.theme_use("aqua")

        # Runtime helpers
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.runner = CommandRunner(self.log_queue)
        self.caffeinate = CaffeinateManager()

        # Variables
        self.source_var = tk.StringVar(value=str(initial_source) if initial_source else "")
        self.dest_var = tk.StringVar(value=str(initial_dest) if initial_dest else "")
        self.job_dir_var = tk.StringVar(value=str(initial_job_dir) if initial_job_dir else "")
        self.profile_var = tk.StringVar(value="customer-home")
        self.volume_excludes_var = tk.StringVar(value="")

        self.phase_var = tk.StringVar(value="visible-home")
        self.timeout_var = tk.StringVar(value="3600")
        self.file_timeout_var = tk.StringVar(value="30")
        self.scan_timeout_var = tk.StringVar(value="300")
        self.limit_var = tk.StringVar(value="")
        self.caffeinate_var = tk.BooleanVar(value=True)

        self.status_state_var = tk.StringVar(value=tr("state_ready", self.lang))
        self.guide_text_var = tk.StringVar(value=tr("guide_default", self.lang))

        # Build GUI widgets
        self._create_widgets()
        self.set_language(self.lang)
        self._setup_logging_poller()
        self._setup_periodic_status_checker()

        if initial_job_dir and manifest_path(initial_job_dir).exists():
            self.load_job(initial_job_dir)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _t(self, key: str, **kwargs: object) -> str:
        return tr(key, self.lang, **kwargs)

    def _create_widgets(self) -> None:
        self.root.minsize(980, 700)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # Top Bar: Header and Active State
        header_frame = ttk.Frame(self.root, padding=(16, 10, 16, 8))
        header_frame.grid(row=0, column=0, sticky="ew")
        header_frame.columnconfigure(1, weight=1)

        title_box = ttk.Frame(header_frame)
        title_box.grid(row=0, column=0, sticky="w")

        self.title_label = ttk.Label(
            title_box,
            text="",
            font=("System", 18, "bold")
        )
        self.title_label.pack(anchor="w")

        self.subtitle_label = ttk.Label(
            title_box,
            text="",
            font=("System", 11)
        )
        self.subtitle_label.pack(anchor="w")

        # Top Right Controls: Language Switcher and Status Badge
        top_right_box = ttk.Frame(header_frame)
        top_right_box.grid(row=0, column=1, sticky="e")

        self.lbl_lang_switch = ttk.Label(top_right_box, text="", font=("System", 11))
        self.lbl_lang_switch.pack(side="left", padx=(0, 6))

        self.lang_var = tk.StringVar(value="Čeština" if self.lang == "cs" else "English")
        self.lang_combo = ttk.Combobox(
            top_right_box,
            textvariable=self.lang_var,
            values=["Čeština", "English"],
            state="readonly",
            width=9
        )
        self.lang_combo.pack(side="left", padx=(0, 16))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_changed)

        self.top_status_badge = ttk.Label(
            top_right_box,
            textvariable=self.status_state_var,
            font=("System", 12, "bold"),
            padding=(10, 4)
        )
        self.top_status_badge.pack(side="left")

        # Notebook (Tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=16, pady=(4, 12))

        self.tab_setup = ttk.Frame(self.notebook, padding=12)
        self.tab_rescue = ttk.Frame(self.notebook, padding=12)
        self.tab_verify = ttk.Frame(self.notebook, padding=12)
        self.tab_reports = ttk.Frame(self.notebook, padding=12)

        self.notebook.add(self.tab_setup, text="")
        self.notebook.add(self.tab_rescue, text="")
        self.notebook.add(self.tab_verify, text="")
        self.notebook.add(self.tab_reports, text="")

        self._build_setup_tab()
        self._build_rescue_tab()
        self._build_verify_tab()
        self._build_reports_tab()

    # -------------------------------------------------------------------------
    # TAB 1: Setup & Preflight
    # -------------------------------------------------------------------------
    def _build_setup_tab(self) -> None:
        frame = self.tab_setup
        frame.columnconfigure(1, weight=1)

        # Paths Group
        self.paths_group = ttk.LabelFrame(frame, text="", padding=12)
        self.paths_group.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.paths_group.columnconfigure(1, weight=1)

        # Source
        self.lbl_src = ttk.Label(self.paths_group, text="")
        self.lbl_src.grid(row=0, column=0, sticky="w", pady=4)
        source_entry = ttk.Entry(self.paths_group, textvariable=self.source_var)
        source_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        self.btn_browse_src = ttk.Button(self.paths_group, text="", command=self._browse_source)
        self.btn_browse_src.grid(row=0, column=2, pady=4)

        self.source_hint = ttk.Label(
            self.paths_group,
            text="",
            font=("System", 10),
            foreground="gray"
        )
        self.source_hint.grid(row=1, column=1, sticky="w", padx=6, pady=(0, 4))

        # Destination
        self.lbl_dst = ttk.Label(self.paths_group, text="")
        self.lbl_dst.grid(row=2, column=0, sticky="w", pady=4)
        dest_entry = ttk.Entry(self.paths_group, textvariable=self.dest_var)
        dest_entry.grid(row=2, column=1, sticky="ew", padx=6, pady=4)
        self.btn_browse_dst = ttk.Button(self.paths_group, text="", command=self._browse_dest)
        self.btn_browse_dst.grid(row=2, column=2, pady=4)

        self.dest_hint = ttk.Label(
            self.paths_group,
            text="",
            font=("System", 10),
            foreground="gray"
        )
        self.dest_hint.grid(row=3, column=1, sticky="w", padx=6, pady=(0, 4))

        # Job Directory
        self.lbl_job = ttk.Label(self.paths_group, text="")
        self.lbl_job.grid(row=4, column=0, sticky="w", pady=4)
        job_entry = ttk.Entry(self.paths_group, textvariable=self.job_dir_var)
        job_entry.grid(row=4, column=1, sticky="ew", padx=6, pady=4)
        self.btn_browse_job = ttk.Button(self.paths_group, text="", command=self._browse_job_dir)
        self.btn_browse_job.grid(row=4, column=2, pady=4)

        self.job_hint = ttk.Label(
            self.paths_group,
            text="",
            font=("System", 10),
            foreground="gray"
        )
        self.job_hint.grid(row=5, column=1, sticky="w", padx=6, pady=(0, 4))

        # Profile & Excludes
        profile_frame = ttk.Frame(self.paths_group)
        profile_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.lbl_profile = ttk.Label(profile_frame, text="")
        self.lbl_profile.pack(side="left", padx=(0, 8))
        self.rb_profile_home = ttk.Radiobutton(profile_frame, text="", value="customer-home",
                                               variable=self.profile_var, command=self._on_profile_change)
        self.rb_profile_home.pack(side="left", padx=6)
        self.rb_profile_volume = ttk.Radiobutton(profile_frame, text="", value="volume",
                                                 variable=self.profile_var, command=self._on_profile_change)
        self.rb_profile_volume.pack(side="left", padx=6)
        self.rb_profile_restore = ttk.Radiobutton(profile_frame, text="", value="restore",
                                                  variable=self.profile_var, command=self._on_profile_change)
        self.rb_profile_restore.pack(side="left", padx=6)

        # Excludes entry for volume profile
        self.excludes_frame = ttk.Frame(self.paths_group)
        self.lbl_vol_excludes = ttk.Label(self.excludes_frame, text="")
        self.lbl_vol_excludes.pack(side="left", padx=(0, 8))
        ttk.Entry(self.excludes_frame, textvariable=self.volume_excludes_var, width=40).pack(side="left", fill="x", expand=True)

        # Action Buttons (Preflight / Init / Open Existing)
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))

        self.btn_preflight = ttk.Button(btn_frame, text="", command=self.run_preflight)
        self.btn_preflight.pack(side="left", padx=(0, 8))

        self.btn_init = ttk.Button(btn_frame, text="", command=self.init_job)
        self.btn_init.pack(side="left", padx=(0, 8))

        self.btn_open_job = ttk.Button(btn_frame, text="", command=self.open_existing_job)
        self.btn_open_job.pack(side="left", padx=(0, 8))

        # Preflight Results Table
        self.results_group = ttk.LabelFrame(frame, text="", padding=8)
        self.results_group.grid(row=2, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(2, weight=1)
        self.results_group.columnconfigure(0, weight=1)
        self.results_group.rowconfigure(0, weight=1)

        cols = ("level", "check", "detail")
        self.preflight_tree = ttk.Treeview(self.results_group, columns=cols, show="headings", height=8)
        self.preflight_tree.column("level", width=90, anchor="center")
        self.preflight_tree.column("check", width=190)
        self.preflight_tree.column("detail", width=550)

        scrollbar = ttk.Scrollbar(self.results_group, orient="vertical", command=self.preflight_tree.yview)
        self.preflight_tree.configure(yscrollcommand=scrollbar.set)
        self.preflight_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.preflight_summary_label = ttk.Label(self.results_group, text="", font=("System", 11))
        self.preflight_summary_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # -------------------------------------------------------------------------
    # TAB 2: Phased Rescue & Live Progress
    # -------------------------------------------------------------------------
    def _build_rescue_tab(self) -> None:
        frame = self.tab_rescue
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        # 1. Guided Next Step Card
        self.guide_group = ttk.LabelFrame(frame, text="", padding=10)
        self.guide_group.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.guide_group.columnconfigure(0, weight=1)

        self.guide_label = ttk.Label(
            self.guide_group,
            textvariable=self.guide_text_var,
            font=("System", 12),
            wraplength=700
        )
        self.guide_label.grid(row=0, column=0, sticky="w", padx=4, pady=4)

        self.btn_execute_next = ttk.Button(
            self.guide_group,
            text="",
            command=self.execute_recommended_step
        )
        self.btn_execute_next.grid(row=0, column=1, sticky="e", padx=4, pady=4)

        # 2. Phase Selector & Control Center
        self.control_group = ttk.LabelFrame(frame, text="", padding=10)
        self.control_group.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.control_group.columnconfigure(1, weight=1)

        # Phase selection
        self.lbl_select_phase = ttk.Label(self.control_group, text="")
        self.lbl_select_phase.grid(row=0, column=0, sticky="w", pady=4)
        self.phase_combo = ttk.Combobox(self.control_group, textvariable=self.phase_var, values=CORE_PHASES, state="readonly", width=18)
        self.phase_combo.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        self.phase_combo.bind("<<ComboboxSelected>>", self._on_phase_selected)

        self.phase_desc_label = ttk.Label(self.control_group, text="", font=("System", 10), foreground="#0055aa")
        self.phase_desc_label.grid(row=0, column=2, sticky="w", padx=6, pady=4)

        # Gated Phase Approval button
        self.btn_approve = ttk.Button(self.control_group, text="", command=self.approve_phase_dialog)
        self.btn_approve.grid(row=0, column=3, sticky="e", padx=4, pady=4)

        # Execution buttons
        btn_action_frame = ttk.Frame(self.control_group)
        btn_action_frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 4))

        self.btn_scan = ttk.Button(btn_action_frame, text="", command=self.start_scan)
        self.btn_scan.pack(side="left", padx=(0, 8))

        self.btn_copy = ttk.Button(btn_action_frame, text="", command=self.start_copy)
        self.btn_copy.pack(side="left", padx=(0, 8))

        self.btn_stop = ttk.Button(btn_action_frame, text="", command=self.stop_current_operation, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 16))

        # Checkbox & Options
        self.chk_caffeinate = ttk.Checkbutton(
            btn_action_frame,
            text="",
            variable=self.caffeinate_var
        )
        self.chk_caffeinate.pack(side="left", padx=8)

        # Timeouts config frame
        opt_frame = ttk.Frame(self.control_group)
        opt_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))

        self.lbl_file_timeout = ttk.Label(opt_frame, text="")
        self.lbl_file_timeout.pack(side="left", padx=(0, 4))
        ttk.Entry(opt_frame, textvariable=self.file_timeout_var, width=6).pack(side="left", padx=(0, 12))

        self.lbl_scan_timeout = ttk.Label(opt_frame, text="")
        self.lbl_scan_timeout.pack(side="left", padx=(0, 4))
        ttk.Entry(opt_frame, textvariable=self.scan_timeout_var, width=6).pack(side="left", padx=(0, 12))

        self.lbl_limit = ttk.Label(opt_frame, text="")
        self.lbl_limit.pack(side="left", padx=(0, 4))
        ttk.Entry(opt_frame, textvariable=self.limit_var, width=6).pack(side="left", padx=(0, 12))

        # 3. Telemetry & Live Dashboard
        self.dash_group = ttk.LabelFrame(frame, text="", padding=10)
        self.dash_group.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.dash_group.columnconfigure(0, weight=1)

        # Progress bar: Transferred Bytes
        prog_bytes_frame = ttk.Frame(self.dash_group)
        prog_bytes_frame.grid(row=0, column=0, sticky="ew", pady=2)
        prog_bytes_frame.columnconfigure(1, weight=1)
        self.lbl_data_vol_title = ttk.Label(prog_bytes_frame, text="", width=15)
        self.lbl_data_vol_title.grid(row=0, column=0, sticky="w")
        self.prog_bytes = ttk.Progressbar(prog_bytes_frame, mode="determinate")
        self.prog_bytes.grid(row=0, column=1, sticky="ew", padx=6)
        self.lbl_bytes_prog = ttk.Label(prog_bytes_frame, text="0 B / 0 B (0%)", width=24, anchor="e")
        self.lbl_bytes_prog.grid(row=0, column=2, sticky="e")

        # Progress bar: Files
        prog_files_frame = ttk.Frame(self.dash_group)
        prog_files_frame.grid(row=1, column=0, sticky="ew", pady=2)
        prog_files_frame.columnconfigure(1, weight=1)
        self.lbl_file_cnt_title = ttk.Label(prog_files_frame, text="", width=15)
        self.lbl_file_cnt_title.grid(row=0, column=0, sticky="w")
        self.prog_files = ttk.Progressbar(prog_files_frame, mode="determinate")
        self.prog_files.grid(row=0, column=1, sticky="ew", padx=6)
        self.lbl_files_prog = ttk.Label(prog_files_frame, text="0 / 0", width=24, anchor="e")
        self.lbl_files_prog.grid(row=0, column=2, sticky="e")

        # Metrics row: Speed, ETA, Current File
        metrics_row = ttk.Frame(self.dash_group)
        metrics_row.grid(row=2, column=0, sticky="ew", pady=(6, 4))
        metrics_row.columnconfigure(2, weight=1)

        self.lbl_speed = ttk.Label(metrics_row, text="", font=("System", 11, "bold"))
        self.lbl_speed.grid(row=0, column=0, sticky="w", padx=(0, 16))

        self.lbl_eta = ttk.Label(metrics_row, text="", font=("System", 11))
        self.lbl_eta.grid(row=0, column=1, sticky="w", padx=(0, 16))

        self.lbl_current_file = ttk.Label(metrics_row, text="", font=("System", 10), foreground="#444444")
        self.lbl_current_file.grid(row=0, column=2, sticky="ew")

        # Metric Badges Grid
        badges_frame = ttk.Frame(self.dash_group)
        badges_frame.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        self.badge_copied = self._create_badge(badges_frame, "#1b8a36")
        self.badge_copied.pack(side="left", padx=4)

        self.badge_fallback = self._create_badge(badges_frame, "#0e7490")
        self.badge_fallback.pack(side="left", padx=4)

        self.badge_pending = self._create_badge(badges_frame, "#2563eb")
        self.badge_pending.pack(side="left", padx=4)

        self.badge_timed_out = self._create_badge(badges_frame, "#d97706")
        self.badge_timed_out.pack(side="left", padx=4)

        self.badge_failed = self._create_badge(badges_frame, "#dc2626")
        self.badge_failed.pack(side="left", padx=4)

        self.badge_compressed = self._create_badge(badges_frame, "#7c3aed")
        self.badge_compressed.pack(side="left", padx=4)

        self.badge_skipped = self._create_badge(badges_frame, "#6b7280")
        self.badge_skipped.pack(side="left", padx=4)

        # 4. Live Log Console
        self.log_group = ttk.LabelFrame(frame, text="", padding=8)
        self.log_group.grid(row=3, column=0, sticky="nsew")
        self.log_group.columnconfigure(0, weight=1)
        self.log_group.rowconfigure(0, weight=1)

        self.log_text = tk.Text(self.log_group, wrap="char", height=9, font=("Menlo", 10), bg="#1e1e1e", fg="#e0e0e0")
        log_scroll = ttk.Scrollbar(self.log_group, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        # Log colors
        self.log_text.tag_configure("stdout", foreground="#d4d4d4")
        self.log_text.tag_configure("info", foreground="#60a5fa")
        self.log_text.tag_configure("success", foreground="#4ade80")
        self.log_text.tag_configure("warn", foreground="#facc15")
        self.log_text.tag_configure("error", foreground="#f87171")

        log_controls = ttk.Frame(self.log_group)
        log_controls.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.btn_clear_log = ttk.Button(log_controls, text="", command=self._clear_log)
        self.btn_clear_log.pack(side="right")

    def _create_badge(self, parent: ttk.Frame, color: str) -> ttk.Label:
        lbl = ttk.Label(
            parent,
            text="",
            font=("System", 10, "bold"),
            foreground=color,
            padding=(6, 2),
            relief="groove"
        )
        return lbl

    # -------------------------------------------------------------------------
    # TAB 3: Verification & Duplicates
    # -------------------------------------------------------------------------
    def _build_verify_tab(self) -> None:
        frame = self.tab_verify
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        # Verification Card
        self.verify_group = ttk.LabelFrame(frame, text="", padding=10)
        self.verify_group.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.verify_group.columnconfigure(1, weight=1)

        self.verify_info = ttk.Label(
            self.verify_group,
            text="",
            font=("System", 11)
        )
        self.verify_info.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.btn_verify = ttk.Button(self.verify_group, text="", command=self.start_verify)
        self.btn_verify.grid(row=1, column=0, sticky="w", padx=(0, 10))

        self.lbl_verify_summary = ttk.Label(self.verify_group, text="", font=("System", 11))
        self.lbl_verify_summary.grid(row=1, column=1, sticky="w")

        # Duplicates / Failed Files Card
        self.dup_group = ttk.LabelFrame(frame, text="", padding=10)
        self.dup_group.grid(row=1, column=0, sticky="nsew")
        self.dup_group.columnconfigure(0, weight=1)
        self.dup_group.columnconfigure(1, weight=1)
        self.dup_group.rowconfigure(1, weight=1)

        self.lbl_dup_info = ttk.Label(
            self.dup_group,
            text="",
            font=("System", 11)
        )
        self.lbl_dup_info.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # Left: Failed Files List
        left_frame = ttk.Frame(self.dup_group)
        left_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(1, weight=1)

        self.lbl_failed_title = ttk.Label(left_frame, text="", font=("System", 11, "bold"))
        self.lbl_failed_title.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.failed_tree = ttk.Treeview(left_frame, columns=("path", "status", "size"), show="headings", height=8)
        self.failed_tree.column("path", width=220)
        self.failed_tree.column("status", width=80, anchor="center")
        self.failed_tree.column("size", width=80, anchor="e")

        failed_scroll = ttk.Scrollbar(left_frame, orient="vertical", command=self.failed_tree.yview)
        self.failed_tree.configure(yscrollcommand=failed_scroll.set)
        self.failed_tree.grid(row=1, column=0, sticky="nsew")
        failed_scroll.grid(row=1, column=1, sticky="ns")

        self.btn_find_dups = ttk.Button(left_frame, text="", command=self.find_duplicates_for_selected)
        self.btn_find_dups.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        # Right: Candidate Matches List
        right_frame = ttk.Frame(self.dup_group)
        right_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        right_frame.columnconfigure(0, weight=1)
        right_frame.rowconfigure(1, weight=1)

        self.lbl_cand_title = ttk.Label(right_frame, text="", font=("System", 11, "bold"))
        self.lbl_cand_title.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.cand_tree = ttk.Treeview(right_frame, columns=("path", "mtime"), show="headings", height=8)
        self.cand_tree.column("path", width=250)
        self.cand_tree.column("mtime", width=110)

        cand_scroll = ttk.Scrollbar(right_frame, orient="vertical", command=self.cand_tree.yview)
        self.cand_tree.configure(yscrollcommand=cand_scroll.set)
        self.cand_tree.grid(row=1, column=0, sticky="nsew")
        cand_scroll.grid(row=1, column=1, sticky="ns")

        self.btn_recover_candidate = ttk.Button(
            right_frame,
            text="",
            command=self.recover_from_candidate,
            state="disabled"
        )
        self.btn_recover_candidate.grid(row=2, column=0, sticky="ew", pady=(6, 0))

    # -------------------------------------------------------------------------
    # TAB 4: Customer & Service Reports
    # -------------------------------------------------------------------------
    def _build_reports_tab(self) -> None:
        frame = self.tab_reports
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        # Customer Report Box
        self.cust_group = ttk.LabelFrame(frame, text="", padding=10)
        self.cust_group.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.cust_group.columnconfigure(1, weight=1)

        # Options: Format & Language
        fmt_frame = ttk.Frame(self.cust_group)
        fmt_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)

        self.cust_format_var = tk.StringVar(value="pdf")
        self.lbl_rep_fmt = ttk.Label(fmt_frame, text="")
        self.lbl_rep_fmt.pack(side="left", padx=(0, 6))
        ttk.Radiobutton(fmt_frame, text="PDF (.pdf)", value="pdf", variable=self.cust_format_var).pack(side="left", padx=6)
        ttk.Radiobutton(fmt_frame, text="Markdown (.md)", value="markdown", variable=self.cust_format_var).pack(side="left", padx=6)

        self.cust_lang_var = tk.StringVar(value="cs" if self.lang == "cs" else "en")
        self.lbl_rep_lang = ttk.Label(fmt_frame, text="")
        self.lbl_rep_lang.pack(side="left", padx=(16, 6))
        self.rb_lang_cs = ttk.Radiobutton(fmt_frame, text="", value="cs", variable=self.cust_lang_var)
        self.rb_lang_cs.pack(side="left", padx=6)
        self.rb_lang_en = ttk.Radiobutton(fmt_frame, text="", value="en", variable=self.cust_lang_var)
        self.rb_lang_en.pack(side="left", padx=6)

        btn_cust_frame = ttk.Frame(self.cust_group)
        btn_cust_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 2))

        self.btn_gen_cust_report = ttk.Button(
            btn_cust_frame,
            text="",
            command=self.generate_customer_report
        )
        self.btn_gen_cust_report.pack(side="left", padx=(0, 8))

        self.btn_open_report_file = ttk.Button(
            btn_cust_frame,
            text="",
            command=self.open_last_report,
            state="disabled"
        )
        self.btn_open_report_file.pack(side="left")

        self.lbl_cust_report_status = ttk.Label(self.cust_group, text="", font=("System", 10), foreground="#007700")
        self.lbl_cust_report_status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Technician Full Report Box
        self.tech_group = ttk.LabelFrame(frame, text="", padding=10)
        self.tech_group.grid(row=1, column=0, sticky="nsew")
        self.tech_group.columnconfigure(0, weight=1)
        self.tech_group.rowconfigure(1, weight=1)

        tech_btn_frame = ttk.Frame(self.tech_group)
        tech_btn_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.btn_refresh_tech = ttk.Button(tech_btn_frame, text="", command=self.load_tech_report)
        self.btn_refresh_tech.pack(side="left", padx=(0, 8))
        self.btn_save_tech = ttk.Button(tech_btn_frame, text="", command=self.save_tech_report)
        self.btn_save_tech.pack(side="left")

        self.tech_report_text = tk.Text(self.tech_group, wrap="word", font=("Menlo", 10), bg="#f8f9fa", fg="#1f2937")
        tech_scroll = ttk.Scrollbar(self.tech_group, orient="vertical", command=self.tech_report_text.yview)
        self.tech_report_text.configure(yscrollcommand=tech_scroll.set)
        self.tech_report_text.grid(row=1, column=0, sticky="nsew")
        tech_scroll.grid(row=1, column=1, sticky="ns")

        self.last_customer_report_path: Path | None = None

    # -------------------------------------------------------------------------
    # Localization Engine
    # -------------------------------------------------------------------------
    def set_language(self, lang: str) -> None:
        """Switch language dynamically between 'cs' and 'en'."""
        if lang not in ("cs", "en"):
            lang = "cs"
        self.lang = lang
        self.lang_var.set("Čeština" if lang == "cs" else "English")
        self.cust_lang_var.set(lang)

        # Window & Header
        self.root.title(self._t("app_title"))
        self.title_label.configure(text="macOS Data Rescue")
        self.subtitle_label.configure(text=self._t("app_subtitle"))
        self.lbl_lang_switch.configure(text=self._t("language_label"))

        # Notebook tabs
        self.notebook.tab(0, text=self._t("tab_setup"))
        self.notebook.tab(1, text=self._t("tab_rescue"))
        self.notebook.tab(2, text=self._t("tab_verify"))
        self.notebook.tab(3, text=self._t("tab_reports"))

        # Tab 1: Setup & Preflight
        self.paths_group.configure(text=self._t("paths_title"))
        self.lbl_src.configure(text=self._t("source_path"))
        self.source_hint.configure(text=self._t("source_hint"))
        self.btn_browse_src.configure(text=self._t("browse"))

        self.lbl_dst.configure(text=self._t("dest_path"))
        self.dest_hint.configure(text=self._t("dest_hint"))
        self.btn_browse_dst.configure(text=self._t("browse"))

        self.lbl_job.configure(text=self._t("job_path"))
        self.job_hint.configure(text=self._t("job_hint"))
        self.btn_browse_job.configure(text=self._t("browse"))

        self.lbl_profile.configure(text=self._t("profile"))
        self.rb_profile_home.configure(text=self._t("profile_home"))
        self.rb_profile_volume.configure(text=self._t("profile_volume"))
        self.rb_profile_restore.configure(text=self._t("profile_restore"))
        self.lbl_vol_excludes.configure(text=self._t("volume_excludes"))

        self.btn_preflight.configure(text=self._t("btn_preflight"))
        self.btn_init.configure(text=self._t("btn_init"))
        self.btn_open_job.configure(text=self._t("btn_open_job"))

        self.results_group.configure(text=self._t("preflight_title"))
        self.preflight_tree.heading("level", text=self._t("col_status"))
        self.preflight_tree.heading("check", text=self._t("col_check"))
        self.preflight_tree.heading("detail", text=self._t("col_detail"))

        # Tab 2: Phased Rescue
        self.guide_group.configure(text=self._t("guide_title"))
        self.btn_execute_next.configure(text=self._t("btn_execute_next"))

        self.control_group.configure(text=self._t("control_title"))
        self.lbl_select_phase.configure(text=self._t("select_phase"))
        self.btn_scan.configure(text=self._t("btn_scan"))
        self.btn_copy.configure(text=self._t("btn_copy"))
        self.btn_stop.configure(text=self._t("btn_stop"))
        self.chk_caffeinate.configure(text=self._t("caffeinate"))
        self.lbl_file_timeout.configure(text=self._t("per_file_timeout"))
        self.lbl_scan_timeout.configure(text=self._t("scan_budget"))
        self.lbl_limit.configure(text=self._t("limit_optional"))

        self.dash_group.configure(text=self._t("telemetry_title"))
        self.lbl_data_vol_title.configure(text=self._t("data_volume"))
        self.lbl_file_cnt_title.configure(text=self._t("file_count"))
        self.log_group.configure(text=self._t("log_title"))
        self.btn_clear_log.configure(text=self._t("clear_log"))

        # Tab 3: Verification & Duplicates
        self.verify_group.configure(text=self._t("verify_title"))
        self.verify_info.configure(text=self._t("verify_info"))
        self.btn_verify.configure(text=self._t("btn_verify"))
        self.lbl_verify_summary.configure(text=self._t("verify_status_default"))

        self.dup_group.configure(text=self._t("dups_title"))
        self.lbl_dup_info.configure(text=self._t("dups_info"))
        self.lbl_failed_title.configure(text=self._t("failed_files_label"))
        self.failed_tree.heading("path", text=self._t("col_path"))
        self.failed_tree.heading("status", text=self._t("col_status"))
        self.failed_tree.heading("size", text=self._t("col_size"))
        self.btn_find_dups.configure(text=self._t("btn_find_dups"))

        self.lbl_cand_title.configure(text=self._t("cands_label"))
        self.cand_tree.heading("path", text=self._t("col_cand_path"))
        self.cand_tree.heading("mtime", text=self._t("col_mtime"))
        self.btn_recover_candidate.configure(text=self._t("btn_recover_cand"))

        # Tab 4: Customer & Tech Reports
        self.cust_group.configure(text=self._t("cust_report_title"))
        self.lbl_rep_fmt.configure(text=self._t("report_format"))
        self.lbl_rep_lang.configure(text=self._t("report_lang"))
        self.rb_lang_cs.configure(text=self._t("lang_cs"))
        self.rb_lang_en.configure(text=self._t("lang_en"))
        self.btn_gen_cust_report.configure(text=self._t("btn_gen_cust_report"))
        self.btn_open_report_file.configure(text=self._t("btn_open_report"))

        self.tech_group.configure(text=self._t("tech_report_title"))
        self.btn_refresh_tech.configure(text=self._t("btn_refresh_tech"))
        self.btn_save_tech.configure(text=self._t("btn_save_tech"))

        # Dynamic state refreshes
        self._on_phase_selected(None)
        self._update_telemetry()
        self._update_guide()

    def _on_language_changed(self, _event: object) -> None:
        val = self.lang_var.get()
        new_lang = "cs" if val == "Čeština" else "en"
        self.set_language(new_lang)

    # -------------------------------------------------------------------------
    # UI Callbacks & Operations
    # -------------------------------------------------------------------------
    def _browse_source(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_source_title"))
        if path:
            self.source_var.set(path)

    def _browse_dest(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_dest_title"))
        if path:
            self.dest_var.set(path)
            # Auto-suggest job directory if empty
            if not self.job_dir_var.get().strip():
                p = Path(path)
                self.job_dir_var.set(str(p.parent / ".rescue"))

    def _browse_job_dir(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_job_title"))
        if path:
            self.job_dir_var.set(path)
            if manifest_path(Path(path)).exists():
                self.load_job(Path(path))

    def _on_profile_change(self) -> None:
        if self.profile_var.get() == "volume":
            self.excludes_frame.grid(row=7, column=0, columnspan=3, sticky="ew", pady=4)
        else:
            self.excludes_frame.grid_forget()

    def _on_phase_selected(self, _event: object) -> None:
        phase = self.phase_var.get()
        desc_dict = PHASE_DESCRIPTIONS.get(self.lang, PHASE_DESCRIPTIONS["cs"])
        desc = desc_dict.get(phase, "")
        self.phase_desc_label.configure(text=desc)
        job_dir = self.get_job_dir()
        if job_dir and manifest_path(job_dir).exists():
            if phase in GATED_PHASES:
                approved = load_approval(job_dir, phase)
                if approved:
                    self.btn_approve.configure(text=self._t("approved_badge", by=approved), state="disabled")
                else:
                    self.btn_approve.configure(text=self._t("btn_approve"), state="normal")
            else:
                self.btn_approve.configure(text=self._t("no_approval_needed"), state="disabled")

    def get_job_dir(self) -> Path | None:
        val = self.job_dir_var.get().strip()
        return Path(val) if val else None

    def get_source(self) -> Path | None:
        val = self.source_var.get().strip()
        return Path(val) if val else None

    def get_dest(self) -> Path | None:
        val = self.dest_var.get().strip()
        return Path(val) if val else None

    def run_preflight(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir:
            messagebox.showerror("Error", "Please specify a Job Directory.")
            return

        source = self.get_source()
        dest = self.get_dest()

        has_manifest = manifest_path(job_dir).exists()
        if not has_manifest and (not source or not dest):
            messagebox.showerror("Error", "Preflight before initialization requires Source and Destination paths.")
            return

        self.preflight_tree.delete(*self.preflight_tree.get_children())
        try:
            summary = preflight_job(job_dir, source if not has_manifest else None, dest if not has_manifest else None)
            for chk in summary.checks:
                tag = chk.level
                sym = "✓ OK" if chk.level == "ok" else ("⚠ WARN" if chk.level == "warn" else "✗ FAIL")
                self.preflight_tree.insert("", "end", values=(sym, chk.name, chk.detail), tags=(tag,))

            self.preflight_tree.tag_configure("ok", foreground="#1b8a36")
            self.preflight_tree.tag_configure("warn", foreground="#d97706")
            self.preflight_tree.tag_configure("fail", foreground="#dc2626")

            warn_count = sum(1 for c in summary.checks if c.level == "warn")
            fail_count = sum(1 for c in summary.checks if c.level == "fail")
            if summary.failed:
                self.preflight_summary_label.configure(
                    text=self._t("preflight_fail", fail=fail_count, warn=warn_count),
                    foreground="#dc2626"
                )
            else:
                self.preflight_summary_label.configure(
                    text=self._t("preflight_ok", checks=len(summary.checks), warn=warn_count),
                    foreground="#1b8a36"
                )
        except Exception as exc:
            messagebox.showerror("Preflight Error", str(exc))

    def init_job(self) -> None:
        job_dir = self.get_job_dir()
        source = self.get_source()
        dest = self.get_dest()
        profile = self.profile_var.get()

        if not job_dir or not source or not dest:
            messagebox.showerror("Error", "Source, Destination, and Job Directory are all required.")
            return

        excludes = tuple(p.strip() for p in self.volume_excludes_var.get().split(",") if p.strip())

        try:
            # Run preflight first
            summary = preflight_job(job_dir, source, dest)
            if summary.failed:
                messagebox.showerror("Preflight Failed", "Preflight safety checks failed. Please inspect the Preflight table.")
                return

            init_manifest(job_dir, source, dest, profile=profile, excludes=excludes)
            messagebox.showinfo("Success", f"Job manifest initialized successfully at:\n{job_dir}")
            self.load_job(job_dir)
            self.notebook.select(self.tab_rescue)
        except Exception as exc:
            messagebox.showerror("Initialization Error", str(exc))

    def open_existing_job(self) -> None:
        path = filedialog.askdirectory(title=self._t("select_job_title"))
        if path:
            p = Path(path)
            if manifest_path(p).exists():
                self.job_dir_var.set(str(p))
                self.load_job(p)
                self.notebook.select(self.tab_rescue)
            else:
                messagebox.showerror("No Manifest", f"No manifest.sqlite found in:\n{path}")

    def load_job(self, job_dir: Path) -> None:
        try:
            cfg = load_config(job_dir)
            self.source_var.set(str(cfg.source))
            self.dest_var.set(str(cfg.dest))
            self.job_dir_var.set(str(cfg.job_dir))
            self.profile_var.set(cfg.profile)
            self._on_profile_change()
            self._update_telemetry()
            self._update_guide()
            self._on_phase_selected(None)
            self.load_tech_report()
            self.refresh_failed_files()
        except Exception as exc:
            messagebox.showwarning("Warning", f"Could not load job configuration: {exc}")

    def approve_phase_dialog(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir:
            messagebox.showerror("Error", "No active job.")
            return

        phase = self.phase_var.get()
        if phase not in GATED_PHASES:
            messagebox.showinfo("Info", f"Phase '{phase}' does not require approval.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(self._t("approve_dialog_title", phase=phase))
        dialog.geometry("480x210")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text=self._t("approve_dialog_text", phase=phase),
            font=("System", 11)
        ).pack(padx=16, pady=12, anchor="w")

        entry_frame = ttk.Frame(dialog)
        entry_frame.pack(padx=16, pady=6, fill="x")
        ttk.Label(entry_frame, text=self._t("approve_by_label")).pack(anchor="w", pady=(0, 4))
        name_entry = ttk.Entry(entry_frame)
        name_entry.pack(fill="x")
        name_entry.focus()

        def submit() -> None:
            name = name_entry.get().strip() or "technician"
            try:
                record_approval(job_dir, phase, by=name)
                dialog.destroy()
                messagebox.showinfo("Approved", f"Phase '{phase}' approved by '{name}'.")
                self._on_phase_selected(None)
                self._update_guide()
            except Exception as exc:
                messagebox.showerror("Error", str(exc))

        btn_box = ttk.Frame(dialog)
        btn_box.pack(padx=16, pady=16, fill="x")
        ttk.Button(btn_box, text=self._t("btn_confirm_approval"), command=submit).pack(side="right", padx=(8, 0))
        ttk.Button(btn_box, text=self._t("btn_cancel"), command=dialog.destroy).pack(side="right")

    # -------------------------------------------------------------------------
    # Command Executions
    # -------------------------------------------------------------------------
    def _execute_cli(self, args: list[str]) -> None:
        if self.runner.is_running:
            messagebox.showwarning("Busy", "A rescue operation is already running.")
            return

        if self.caffeinate_var.get():
            self.caffeinate.start()

        self._set_ui_running(True)

        def on_finish(_returncode: int) -> None:
            self.root.after(0, self._on_operation_done)

        self.runner.run_command(args, on_finish)

    def _set_ui_running(self, running: bool) -> None:
        self.btn_scan.configure(state="disabled" if running else "normal")
        self.btn_copy.configure(state="disabled" if running else "normal")
        self.btn_execute_next.configure(state="disabled" if running else "normal")
        self.btn_verify.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.status_state_var.set(self._t("state_running") if running else self._t("state_idle"))

    def _on_operation_done(self) -> None:
        self.caffeinate.stop()
        self._set_ui_running(False)
        self._update_telemetry()
        self._update_guide()
        self.refresh_failed_files()

    def stop_current_operation(self) -> None:
        if self.runner.is_running:
            if messagebox.askyesno(self._t("confirm_stop_title"), self._t("confirm_stop_msg")):
                self.runner.cancel()

    def start_scan(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            messagebox.showerror("Error", "Please initialize or select a valid job first.")
            return

        phase = self.phase_var.get()
        cmd = ["scan", "--job-dir", str(job_dir), "--phase", phase]
        if self.scan_timeout_var.get().strip():
            cmd += ["--timeout", self.scan_timeout_var.get().strip()]
        if self.limit_var.get().strip():
            cmd += ["--limit", self.limit_var.get().strip()]

        self._execute_cli(cmd)

    def start_copy(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            messagebox.showerror("Error", "Please initialize or select a valid job first.")
            return

        phase = self.phase_var.get()
        cmd = ["copy", "--job-dir", str(job_dir), "--phase", phase]
        if self.file_timeout_var.get().strip():
            cmd += ["--timeout", self.file_timeout_var.get().strip()]
        if self.limit_var.get().strip():
            cmd += ["--limit", self.limit_var.get().strip()]

        self._execute_cli(cmd)

    def start_verify(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            messagebox.showerror("Error", "Please initialize or select a valid job first.")
            return

        phase = self.phase_var.get()
        cmd = ["verify", "--job-dir", str(job_dir), "--phase", phase]
        self._execute_cli(cmd)

    def execute_recommended_step(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            messagebox.showerror("Error", "Please initialize or select a valid job first.")
            return

        text = next_text(job_dir)
        target_line = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("macos-data-rescue ") or line.startswith("uv run macos-data-rescue "):
                target_line = line
                break

        if not target_line:
            messagebox.showinfo("Guide", f"{text}")
            return

        parts = shlex.split(target_line)
        if "macos-data-rescue" in parts:
            idx = parts.index("macos-data-rescue")
            cli_args = parts[idx + 1:]
            self._execute_cli(cli_args)
        else:
            messagebox.showinfo("Guide", f"{target_line}")

    # -------------------------------------------------------------------------
    # Duplicates & Fallbacks
    # -------------------------------------------------------------------------
    def refresh_failed_files(self) -> None:
        self.failed_tree.delete(*self.failed_tree.get_children())
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            return

        try:
            conn = connect(job_dir)
            try:
                rows = conn.execute(
                    "select relative_path, status, size from files where status in ('failed', 'timed_out', 'unreadable-compressed-flag') order by relative_path"
                ).fetchall()
            finally:
                conn.close()

            for row in rows:
                self.failed_tree.insert("", "end", values=(row["relative_path"], row["status"], format_size(row["size"])))
        except Exception:  # nosec B110
            pass

    def find_duplicates_for_selected(self) -> None:
        sel = self.failed_tree.selection()
        if not sel:
            messagebox.showinfo("Select File", "Please select a failed file from the left table.")
            return

        item = self.failed_tree.item(sel[0])
        rel_path = item["values"][0]
        job_dir = self.get_job_dir()
        if not job_dir:
            return

        self.cand_tree.delete(*self.cand_tree.get_children())
        try:
            payload = duplicate_candidates(job_dir, rel_path, limit=20)
            cands = payload.get("candidates", [])
            if not cands:
                messagebox.showinfo("No Duplicates", f"No candidate duplicates found on the source volume for:\n{rel_path}")
                self.btn_recover_candidate.configure(state="disabled")
                return

            for c in cands:
                self.cand_tree.insert("", "end", values=(c["source_path"], c.get("mtime", "")))
            self.btn_recover_candidate.configure(state="normal")
        except Exception as exc:
            messagebox.showerror("Duplicate Search Error", str(exc))

    def recover_from_candidate(self) -> None:
        sel_fail = self.failed_tree.selection()
        sel_cand = self.cand_tree.selection()
        if not sel_fail or not sel_cand:
            messagebox.showinfo("Selection Required", "Select both a failed file and a candidate.")
            return

        fail_path = self.failed_tree.item(sel_fail[0])["values"][0]
        cand_path = self.cand_tree.item(sel_cand[0])["values"][0]

        job_dir = self.get_job_dir()
        if not job_dir:
            return

        msg = f"Recover:\n{fail_path}\n\nUsing fallback candidate:\n{cand_path}?"
        if messagebox.askyesno("Confirm Fallback Recovery", msg):
            cmd = ["copy", "--job-dir", str(job_dir), "--path", str(fail_path), "--fallback-from", str(cand_path)]
            self._execute_cli(cmd)

    # -------------------------------------------------------------------------
    # Reports
    # -------------------------------------------------------------------------
    def generate_customer_report(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            messagebox.showerror("Error", "Please select an active job first.")
            return

        fmt = self.cust_format_var.get()
        lang = self.cust_lang_var.get()
        try:
            out = write_customer_report(job_dir, report_format=fmt, language=lang)
            self.last_customer_report_path = out
            self.lbl_cust_report_status.configure(text=f"Report generated: {out.name} ({format_size(out.stat().st_size)})")
            self.btn_open_report_file.configure(state="normal")
            messagebox.showinfo("Report Ready", f"Customer report written to:\n{out}")
        except Exception as exc:
            messagebox.showerror("Report Error", str(exc))

    def open_last_report(self) -> None:
        if self.last_customer_report_path and self.last_customer_report_path.exists():
            subprocess.run(["/usr/bin/open", str(self.last_customer_report_path)], check=False)  # nosec B603

    def load_tech_report(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            return
        try:
            content = report(job_dir, report_format="markdown")
            self.tech_report_text.delete("1.0", "end")
            self.tech_report_text.insert("end", content)
        except Exception as exc:
            self.tech_report_text.delete("1.0", "end")
            self.tech_report_text.insert("end", f"Error loading report: {exc}")

    def save_tech_report(self) -> None:
        content = self.tech_report_text.get("1.0", "end")
        path = filedialog.asksaveasfilename(
            title="Save Service Report",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")]
        )
        if path:
            Path(path).write_text(content, encoding="utf-8")
            messagebox.showinfo("Saved", f"Report saved to:\n{path}")

    # -------------------------------------------------------------------------
    # Background Pollers & Telemetry
    # -------------------------------------------------------------------------
    def _setup_logging_poller(self) -> None:
        def poll() -> None:
            while not self.log_queue.empty():
                try:
                    tag, text = self.log_queue.get_nowait()
                    self.log_text.insert("end", text, tag)
                    self.log_text.see("end")
                except queue.Empty:
                    break
            self.root.after(100, poll)

        self.root.after(100, poll)

    def _setup_periodic_status_checker(self) -> None:
        def check() -> None:
            if not self.runner.is_running:
                self._update_telemetry()
                self._update_guide()
                self.root.after(2000, check)
            else:
                self._update_telemetry()
                self.root.after(500, check)

        self.root.after(1000, check)

    def _update_telemetry(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            return

        try:
            # 1. Summary status
            summary = status_summary(job_dir)
            total_files = sum(item.get("count", 0) for item in summary.values())

            copied_cnt = summary.get("copied", {}).get("count", 0)
            fallback_cnt = summary.get("copied_from_fallback", {}).get("count", 0)
            pending_cnt = summary.get("pending", {}).get("count", 0) + summary.get("copying", {}).get("count", 0)
            timed_out_cnt = summary.get("timed_out", {}).get("count", 0)
            failed_cnt = summary.get("failed", {}).get("count", 0)
            compressed_cnt = summary.get("unreadable-compressed-flag", {}).get("count", 0)
            skipped_cnt = summary.get("skipped", {}).get("count", 0)

            total_rescued_cnt = copied_cnt + fallback_cnt

            self.badge_copied.configure(text=f"{self._t('badge_copied')}: {copied_cnt}")
            self.badge_fallback.configure(text=f"{self._t('badge_fallback')}: {fallback_cnt}")
            self.badge_pending.configure(text=f"{self._t('badge_pending')}: {pending_cnt}")
            self.badge_timed_out.configure(text=f"{self._t('badge_timed_out')}: {timed_out_cnt}")
            self.badge_failed.configure(text=f"{self._t('badge_failed')}: {failed_cnt}")
            self.badge_compressed.configure(text=f"{self._t('badge_compressed')}: {compressed_cnt}")
            self.badge_skipped.configure(text=f"{self._t('badge_skipped')}: {skipped_cnt}")

            # File count progress
            self.prog_files.configure(maximum=max(total_files, 1), value=total_rescued_cnt)
            self.lbl_files_prog.configure(text=f"{total_rescued_cnt:,} / {total_files:,}")

            # 2. Activity snapshot (current live transfer)
            snap = activity_snapshot(job_dir)
            if snap:
                state = snap.get("state", "idle").upper()
                if not self.runner.is_running:
                    self.status_state_var.set(state)

                rate = snap.get("rate_bytes_s", 0)
                self.lbl_speed.configure(text=self._t("speed", rate=format_rate(rate)))

                eta = snap.get("eta_seconds")
                self.lbl_eta.configure(text=self._t("eta", eta=format_eta(eta, self.lang)))

                cur_file = snap.get("path") or "—"
                cur_bytes = snap.get("current_bytes", 0)
                if cur_file != "—":
                    self.lbl_current_file.configure(
                        text=self._t("current_file", file=f"{cur_file} ({format_size(cur_bytes)})")
                    )
                else:
                    self.lbl_current_file.configure(text=self._t("current_file", file="—"))

                # Transferred bytes progress
                transferred = snap.get("transferred_bytes", 0)
                remaining = snap.get("remaining_bytes", 0)
                total_known_bytes = transferred + remaining
                if total_known_bytes > 0:
                    pct = (transferred / total_known_bytes) * 100
                    self.prog_bytes.configure(maximum=total_known_bytes, value=transferred)
                    self.lbl_bytes_prog.configure(
                        text=f"{format_size(transferred)} / {format_size(total_known_bytes)} ({pct:.1f}%)"
                    )
            else:
                self.lbl_speed.configure(text=self._t("speed", rate="0.0 MB/s"))
                self.lbl_eta.configure(text=self._t("eta", eta="—"))
                self.lbl_current_file.configure(text=self._t("current_file", file="—"))
        except Exception:  # nosec B110
            pass

    def _update_guide(self) -> None:
        job_dir = self.get_job_dir()
        if not job_dir or not manifest_path(job_dir).exists():
            self.guide_text_var.set(self._t("guide_default"))
            return
        try:
            text = next_text(job_dir)
            lines = [line_item.strip() for line_item in text.splitlines() if line_item.strip()]
            summary_lines = []
            for line in lines:
                if line.startswith("run:"):
                    summary_lines.append("Next: " + line[4:].strip())
                elif line.startswith("state:") or line.startswith("review:"):
                    summary_lines.append(line)
            if not summary_lines and lines:
                summary_lines = lines[:2]
            self.guide_text_var.set("\n".join(summary_lines) or "Job up to date.")
        except Exception:  # nosec B110
            pass

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", "end")

    def _on_close(self) -> None:
        if self.runner.is_running:
            if not messagebox.askyesno(self._t("confirm_close_title"), self._t("confirm_close_msg")):
                return
            self.runner.cancel()
        self.caffeinate.stop()
        self.root.destroy()


def run_gui(job_dir: Path | None = None, source: Path | None = None, dest: Path | None = None, lang: str = "cs") -> int:
    """Launch the Tkinter application."""
    root = tk.Tk()
    _app = DataRescueApp(root, initial_job_dir=job_dir, initial_source=source, initial_dest=dest, lang=lang)
    root.mainloop()
    return 0
