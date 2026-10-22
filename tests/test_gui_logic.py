"""Display-free tests for the GUI's command vetting, state model, runner and monitor."""
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import init_and_scan, run_cli, write_file
from macos_data_rescue.cli import build_parser
from macos_data_rescue.guide import base_cmd, quoted, storage_escalation_lines
from macos_data_rescue.gui.commands import (
    ACK_FLAG,
    CommandSpec,
    GuideAction,
    ReconnectOptions,
    copy_spec,
    needs_storage_acknowledgement,
    parse_guide_action,
    parse_non_negative_float,
    parse_optional_positive_int,
    parse_positive_float,
    parse_positive_int,
    parse_preflight_line,
    parse_report_path,
    resume_from,
    approve_spec,
    with_acknowledgement,
    with_preflight,
    with_reconnect,
)
from macos_data_rescue.manifest import connect
from macos_data_rescue.storage_events import record_event
from macos_data_rescue.gui.i18n import elide_middle, format_count, format_percent, localize_decimal
from macos_data_rescue.gui.monitor import JobMonitor, JobSnapshot, Telemetry, read_job_snapshot, telemetry_from
from macos_data_rescue.gui.runner import CommandRunner, KeepAwake, OutputLine, RunFinished, RunnerNotice
from macos_data_rescue.gui.state import (
    OpState,
    control_states,
    info,
    reconcile_with_activity,
    state_after_exit,
    wait_view,
)
from macos_data_rescue.gui.status import action_label, counter_items, progress_summary, status_view


JOB = Path("/Volumes/RecoverySSD/Customer/.rescue")


# -- recommended-step allow-list ------------------------------------------------

def guide_text(*commands: str, state: str = "state: core-phases-complete") -> str:
    return "\n".join([f"job={JOB} profile=customer-home", state, "run:", *(f"  {c}" for c in commands)])


def test_guide_approve_is_never_runnable() -> None:
    text = guide_text(
        base_cmd("approve", "--job-dir", quoted(JOB), "--phase", "app-data"),
        base_cmd("approve", "--job-dir", quoted(JOB), "--phase", "applications"),
        base_cmd("customer-report", "--job-dir", quoted(JOB), "--format", "pdf"),
    )
    action = parse_guide_action(text, JOB)
    assert action.kind == "approve"
    assert action.phase == "app-data"
    assert action.spec is None


def test_guide_runs_allowed_scan_command() -> None:
    text = guide_text(base_cmd("scan", "--job-dir", quoted(JOB), "--phase", "visible-home", "--timeout", "300"),
                      state="state: action=scan phase=visible-home reason=unscanned")
    action = parse_guide_action(text, JOB)
    assert action.kind == "run"
    assert action.spec == CommandSpec(("scan", "--job-dir", str(JOB), "--phase", "visible-home", "--timeout", "300"))


@pytest.mark.parametrize("command", ["preflight", "init", "next", "find-duplicates", "gui"])
def test_guide_refuses_commands_outside_allow_list(command: str) -> None:
    action = parse_guide_action(guide_text(base_cmd(command, "--job-dir", quoted(JOB))), JOB)
    assert action.kind == "none"
    assert action.spec is None


def test_guide_report_redirect_is_captured_inside_job_only() -> None:
    inside = base_cmd("report", "--job-dir", quoted(JOB), "--format", "markdown") + f" > {quoted(JOB / 'report.md')}"
    action = parse_guide_action(guide_text(inside), JOB)
    assert action.kind == "run"
    assert action.spec.args == ("report", "--job-dir", str(JOB), "--format", "markdown")
    assert action.spec.stdout_path == JOB / "report.md"

    outside = base_cmd("report", "--job-dir", quoted(JOB), "--format", "markdown") + " > /etc/report.md"
    assert parse_guide_action(guide_text(outside), JOB).kind == "none"
    scan_redirect = base_cmd("scan", "--job-dir", quoted(JOB)) + f" > {quoted(JOB / 'x')}"
    assert parse_guide_action(guide_text(scan_redirect), JOB).kind == "none"


def test_guide_refuses_other_job_dir() -> None:
    other = base_cmd("copy", "--job-dir", "/tmp/other", "--phase", "visible-home")
    assert parse_guide_action(guide_text(other), JOB).kind == "none"


def test_guide_action_from_real_job_is_copy(tmp_path: Path) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "a.txt", b"a")
    job_dir = tmp_path / "job"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(tmp_path / "dest"))
    first = parse_guide_action(read_job_snapshot(job_dir).guide_text, job_dir)
    assert first.kind == "run" and first.spec.args[:5] == ("scan", "--job-dir", str(job_dir), "--phase", "visible-home")
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home")
    action = parse_guide_action(read_job_snapshot(job_dir).guide_text, job_dir)
    assert action.kind == "run"
    assert action.spec.subcommand == "copy" and action.spec.option("--phase") == "visible-home"
    assert with_preflight(action.spec, job_dir)[0].args == ("preflight", "--job-dir", str(job_dir))


def test_command_builders() -> None:
    spec = copy_spec(JOB, "visible-home", 3600.0, 5)
    assert spec.args == ("copy", "--job-dir", str(JOB), "--phase", "visible-home", "--timeout", "3600", "--limit", "5")
    assert resume_from(spec).args == ("resume", *spec.args[1:])
    fallback = copy_spec(JOB, "all", 1.5, path="a b", fallback_from="c")
    assert fallback.option("--fallback-from") == "c"
    assert fallback.option("--timeout") == "1.5"
    with pytest.raises(ValueError):
        resume_from(CommandSpec(("scan",)))
    with pytest.raises(ValueError):
        approve_spec(JOB, "app-data", "   ")
    assert approve_spec(JOB, "app-data", " Jana ").option("--by") == "Jana"
    assert [s.subcommand for s in with_preflight(CommandSpec(("verify",)), JOB)] == ["verify"]


# -- auto-reconnect options ----------------------------------------------------

def test_reconnect_defaults_equal_cli_defaults() -> None:
    args = build_parser().parse_args(["copy", "--job-dir", "/j"])
    options = ReconnectOptions()
    assert (options.cooldown, options.wait, options.max_reconnects) == (300, 1800, 6)
    assert (args.reconnect_cooldown, args.reconnect_wait, args.max_reconnects) == (300, 1800, 6)
    assert options.args() == ("--auto-reconnect",), "unchanged defaults are not passed"


def test_copy_spec_with_and_without_auto_reconnect() -> None:
    base = ("copy", "--job-dir", str(JOB), "--phase", "visible-home", "--timeout", "3600")
    assert copy_spec(JOB, "visible-home", 3600.0).args == base
    assert copy_spec(JOB, "visible-home", 3600.0, reconnect=ReconnectOptions()).args == (*base, "--auto-reconnect")
    custom = copy_spec(JOB, "visible-home", 3600.0, reconnect=ReconnectOptions(0, 90.5, 2))
    assert custom.args == (*base, "--auto-reconnect", "--reconnect-cooldown", "0", "--reconnect-wait", "90.5",
                           "--max-reconnects", "2")
    parsed = build_parser().parse_args(list(custom.args))
    assert parsed.auto_reconnect and (parsed.reconnect_cooldown, parsed.reconnect_wait,
                                      parsed.max_reconnects) == (0, 90.5, 2)
    # the CLI refuses --auto-reconnect with --path: a single-file recovery never gets it
    fallback = copy_spec(JOB, "all", 30.0, path="a", fallback_from="b", reconnect=ReconnectOptions())
    assert "--auto-reconnect" not in fallback.args


def test_with_reconnect_replaces_options_and_never_keeps_acknowledgement() -> None:
    old = CommandSpec(("resume", "--job-dir", str(JOB), "--phase", "all", "--timeout", "30", "--auto-reconnect",
                       "--reconnect-cooldown", "5", ACK_FLAG))
    assert with_reconnect(old, None).args == ("resume", "--job-dir", str(JOB), "--phase", "all", "--timeout", "30")
    assert with_reconnect(old, ReconnectOptions(wait=60)).args[-3:] == ("--auto-reconnect", "--reconnect-wait", "60")
    assert ACK_FLAG not in resume_from(old).args
    # option values are never mistaken for flags
    odd = CommandSpec(("copy", "--job-dir", str(JOB), "--path", "--auto-reconnect", "--fallback-from", ACK_FLAG))
    assert resume_from(odd).args == ("resume", *odd.args[1:])
    assert with_reconnect(odd, ReconnectOptions()).args == odd.args
    scan = CommandSpec(("scan", "--job-dir", str(JOB)))
    assert with_reconnect(scan, ReconnectOptions()) is scan


def test_acknowledgement_needs_auto_reconnect_and_a_reason() -> None:
    plain = copy_spec(JOB, "all", 30.0)
    looping = copy_spec(JOB, "all", 30.0, reconnect=ReconnectOptions())
    assert not needs_storage_acknowledgement(looping, blocked=False)
    assert needs_storage_acknowledgement(looping, blocked=True)
    assert needs_storage_acknowledgement(looping, blocked=False, escalation=True)
    assert not needs_storage_acknowledgement(plain, blocked=True, escalation=True)
    assert with_acknowledgement(looping).args[-1] == ACK_FLAG
    with pytest.raises(ValueError):
        with_acknowledgement(plain)


def test_guide_never_runs_storage_acknowledgement() -> None:
    lines = storage_escalation_lines(JOB, {"phase": "visible-home", "reason": "storage did not come back"})
    action = parse_guide_action("\n".join([f"job={JOB} profile=customer-home", *lines]), JOB)
    assert action.kind == "acknowledge"
    assert ACK_FLAG not in action.spec.args
    assert action.spec.args[:5] == ("resume", "--job-dir", str(JOB), "--phase", "visible-home")
    other = guide_text(base_cmd("verify", "--job-dir", quoted(JOB), ACK_FLAG))
    assert parse_guide_action(other, JOB).kind == "none"


def test_give_up_blocks_until_acknowledged_but_operator_stop_does_not(tmp_path: Path) -> None:
    from test_auto_reconnect import seed_identities

    write_file(tmp_path / "source-home" / "a", b"hello")
    job_dir, source, _ = init_and_scan(tmp_path)
    seed_identities(job_dir, source_uuid=None, dest_uuid=None)
    source.rename(source.with_name("gone"))
    result = run_cli("copy", "--job-dir", str(job_dir), "--auto-reconnect", "--reconnect-cooldown", "0",
                     "--reconnect-wait", "0.1", check=False)
    assert result.returncode == 3, result.stdout + result.stderr
    snapshot = read_job_snapshot(job_dir)
    assert snapshot.storage_blocked and snapshot.storage_losses == 1 and snapshot.reconnects == 0
    action = parse_guide_action(snapshot.guide_text, job_dir)
    assert action.kind == "acknowledge" and ACK_FLAG not in action.spec.args

    conn = connect(job_dir)
    try:
        record_event(conn, "acknowledged")
        record_event(conn, "lost")
        record_event(conn, "stopped")
    finally:
        conn.close()
    after_stop = read_job_snapshot(job_dir)
    assert not after_stop.storage_blocked
    assert not needs_storage_acknowledgement(with_reconnect(action.spec, ReconnectOptions()),
                                             blocked=after_stop.storage_blocked)


@pytest.mark.parametrize("text", ["0", "-1", "nan", "inf", "abc", ""])
def test_positive_float_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_positive_float(text)


def test_numeric_parsers_accept() -> None:
    assert parse_positive_float(" 2,5 ") == 2.5
    assert parse_optional_positive_int("") is None
    assert parse_optional_positive_int("7") == 7
    for bad in ("0", "-3", "1.5", "x"):
        with pytest.raises(ValueError):
            parse_optional_positive_int(bad)
    assert parse_non_negative_float("0") == 0 and parse_non_negative_float("1,5") == 1.5
    for bad in ("-1", "nan", "inf", ""):
        with pytest.raises(ValueError):
            parse_non_negative_float(bad)
    assert parse_positive_int("6") == 6
    for bad in ("", "0", "2.5"):
        with pytest.raises(ValueError):
            parse_positive_int(bad)


def test_output_parsers() -> None:
    assert parse_preflight_line("fail source-exists not an existing directory: /x y\n") == (
        "fail", "source-exists", "not an existing directory: /x y")
    assert parse_preflight_line("preflight=ok checks=9 warnings=0 failures=0") is None
    assert parse_report_path("wrote report=/a b/recovery-report.pdf\n") == Path("/a b/recovery-report.pdf")
    assert parse_report_path("something else") is None


# -- state model --------------------------------------------------------------

@pytest.mark.parametrize(
    ("code", "stop", "error", "expected"),
    [
        (0, False, False, OpState.DONE),
        (1, False, False, OpState.FAILED),
        (2, False, False, OpState.FAILED),
        (3, False, False, OpState.PAUSED_STORAGE),
        (3, True, False, OpState.PAUSED_STORAGE),
        (-2, True, False, OpState.STOPPED),
        # the CLI exits 130 on SIGINT/SIGTERM during a copy pass: stopped only if the operator asked
        (130, True, False, OpState.STOPPED),
        (130, False, False, OpState.FAILED),
        (None, True, False, OpState.STOPPED),
        (None, False, True, OpState.FAILED),
        (0, False, True, OpState.FAILED),
    ],
)
def test_state_after_exit(code, stop, error, expected) -> None:
    assert state_after_exit(code, stop_requested=stop, error=error) is expected


def test_paused_state_offers_resume_and_message() -> None:
    paused = info(OpState.PAUSED_STORAGE)
    assert paused.can_resume and not paused.busy
    assert paused.message_key == "msg_paused_storage"


def test_reconcile_with_persisted_activity() -> None:
    assert reconcile_with_activity(OpState.IDLE, "paused") is OpState.PAUSED_STORAGE
    assert reconcile_with_activity(OpState.FAILED, "paused") is OpState.PAUSED_STORAGE
    assert reconcile_with_activity(OpState.RUNNING, "paused") is OpState.RUNNING
    assert reconcile_with_activity(OpState.PAUSED_STORAGE, "finished") is OpState.IDLE
    assert reconcile_with_activity(OpState.PAUSED_STORAGE, None) is OpState.PAUSED_STORAGE


@pytest.mark.parametrize("state", [OpState.RUNNING, OpState.WAITING, OpState.STOPPING])
def test_busy_states_lock_every_job_mutation(state: OpState) -> None:
    controls = control_states(state, job_loaded=True)
    for name in ("job_fields", "init", "open_job", "close_job", "preflight", "phase", "approve", "scan",
                 "copy", "verify", "guide", "resume", "recover", "customer_report", "options"):
        assert controls[name] is False, name
    assert controls["stop"] is (state is not OpState.STOPPING)


def test_waiting_of_another_process_locks_the_job_but_cannot_be_stopped() -> None:
    controls = control_states(OpState.WAITING, job_loaded=True, own_child=False)
    assert controls["stop"] is False
    assert controls["open_job"] and controls["close_job"]
    assert not (controls["copy"] or controls["resume"] or controls["scan"] or controls["guide"])


def test_waiting_activity_is_never_shown_as_running_or_idle() -> None:
    # the console's own child
    assert reconcile_with_activity(OpState.RUNNING, "waiting", child_running=True) is OpState.WAITING
    assert reconcile_with_activity(OpState.WAITING, "running", child_running=True) is OpState.RUNNING
    assert reconcile_with_activity(OpState.WAITING, "waiting", child_running=True) is OpState.WAITING
    assert reconcile_with_activity(OpState.STOPPING, "waiting", child_running=True) is OpState.STOPPING
    # the exit code decides the final state of an own child, not the activity
    assert reconcile_with_activity(OpState.WAITING, "paused", child_running=True) is OpState.RUNNING
    # a writer outside the console (terminal session)
    for settled in (OpState.IDLE, OpState.DONE, OpState.FAILED, OpState.STOPPED, OpState.PAUSED_STORAGE):
        assert reconcile_with_activity(settled, "waiting") is OpState.WAITING
    assert reconcile_with_activity(OpState.WAITING, "paused") is OpState.PAUSED_STORAGE
    assert reconcile_with_activity(OpState.WAITING, "finished") is OpState.IDLE
    assert reconcile_with_activity(OpState.WAITING, "interrupted") is OpState.IDLE


def test_waiting_status_shows_countdown_attempt_and_who_can_stop() -> None:
    now = datetime(2026, 10, 22, 1, 0, 55, tzinfo=timezone.utc)
    activity = {"state": "waiting", "wait_phase": "cooldown", "wait_until": "2026-10-22T01:05:00+00:00",
                "attempt": 2, "reason": "source unavailable"}
    cooldown = wait_view(activity, now)
    assert (cooldown.phase, cooldown.remaining, cooldown.attempt) == ("cooldown", 245, 2)
    view = status_view(OpState.WAITING, "cs", job_loaded=True, activity=activity, now=now)
    assert view.headline == "Chladnutí disku — 4m 05s (pokus 2)"
    assert (view.tone, view.action) == ("orange", "none")
    assert "source unavailable" not in view.body, "the raw reason belongs to the details"
    waiting = {"wait_phase": "waiting", "wait_until": "2026-10-22T01:30:55+00:00", "attempt": 1}
    external = status_view(OpState.WAITING, "en", job_loaded=True, own_child=False, activity=waiting, now=now)
    assert external.headline == "Waiting for the disk — 30m 00s (attempt 1)"
    assert "this console cannot stop it" in external.body
    own = status_view(OpState.WAITING, "en", job_loaded=True, activity=waiting, now=now)
    assert "cannot stop" not in own.body
    assert wait_view({"wait_phase": "waiting", "wait_until": "2026-10-22T01:00:00+00:00"}, now).remaining == 0
    unknown = {"wait_phase": "waiting", "wait_until": "garbage"}
    assert wait_view(unknown, now).remaining is None
    assert status_view(OpState.WAITING, "cs", job_loaded=True, activity=unknown, now=now).headline == \
        "Čekám na disk — — (pokus ?)"


def recommendation(command: str, state: str = "state: action=copy phase=visible-home rows=3") -> tuple[str, GuideAction]:
    text = guide_text(command, state=state)
    return text, parse_guide_action(text, JOB)


def test_status_card_turns_next_into_one_sentence_and_one_action() -> None:
    copy = base_cmd("copy", "--job-dir", quoted(JOB), "--phase", "visible-home", "--timeout", "3600")
    text, action = recommendation(copy)
    view = status_view(OpState.IDLE, "cs", job_loaded=True, guide_text=text, action=action)
    assert (view.headline, view.action) == ("Připraveno", "guide")
    assert view.body == "Doporučený krok: zkopírovat fázi visible-home."
    assert action_label(action, "cs") == "Kopírovat visible-home"
    assert action_label(action, "en") == "Copy visible-home"

    text, action = recommendation(base_cmd("approve", "--job-dir", quoted(JOB), "--phase", "app-data"),
                                  state="state: approval-required phase=app-data")
    view = status_view(OpState.DONE, "en", job_loaded=True, guide_text=text, action=action)
    assert (view.headline, view.action) == ("Waiting for customer consent", "guide")
    assert action_label(action, "cs") == "Zaznamenat souhlas: app-data…"

    done = "\n".join([f"job={JOB} profile=customer-home", "state: core-phases-complete",
                      "done: follow the handoff checklist in docs/agent-runbook.md"])
    view = status_view(OpState.IDLE, "cs", job_loaded=True, guide_text=done, action=parse_guide_action(done, JOB))
    assert (view.headline, view.tone, view.action) == ("Hotovo", "green", "none")
    no_job = status_view(OpState.IDLE, "en", job_loaded=False)
    assert (no_job.headline, no_job.action) == ("No job open", "none")


def test_status_card_pause_escalation_and_failures() -> None:
    paused = status_view(OpState.PAUSED_STORAGE, "cs", job_loaded=True)
    assert (paused.headline, paused.tone, paused.action) == ("Pozastaveno: úložiště nedostupné", "red", "resume")
    assert "znovu připojte původní disk" in paused.body
    escalation = status_view(OpState.PAUSED_STORAGE, "en", job_loaded=True, escalation=True)
    assert (escalation.headline, escalation.action) == ("Automatic reconnect gave up", "resume")
    assert "ddrescue" in escalation.body
    # the acknowledging recommendation only gets a label; the click still asks first
    ack = GuideAction("acknowledge", spec=CommandSpec(("resume", "--job-dir", str(JOB), "--phase", "visible-home")))
    assert action_label(ack, "cs") == "Přesto pokračovat…"

    copy = CommandSpec(("copy", "--job-dir", str(JOB)))
    preflight = status_view(OpState.FAILED, "cs", job_loaded=True,
                            finish=RunFinished((CommandSpec(("preflight",)), copy), 0, 1, False))
    assert preflight.headline == "Kontrola prostředí selhala" and "Příprava" in preflight.body
    failed = status_view(OpState.FAILED, "en", job_loaded=True, finish=RunFinished((copy,), 0, 2, False))
    assert failed.headline == "Operation failed" and "copy failed (exit 2)" in failed.body


@pytest.mark.parametrize("state", [OpState.RUNNING, OpState.WAITING, OpState.STOPPING])
def test_busy_status_never_offers_a_primary_action(state: OpState) -> None:
    text, action = recommendation(base_cmd("copy", "--job-dir", quoted(JOB), "--phase", "all"))
    view = status_view(state, "cs", job_loaded=True, guide_text=text, action=action,
                       running=CommandSpec(("copy", "--job-dir", str(JOB), "--phase", "visible-home")))
    assert view.action == "none"
    if state is OpState.RUNNING:
        assert view.headline == "Kopíruji · visible-home"
    assert status_view(OpState.RUNNING, "en", job_loaded=True).headline == "Operation running"


def test_progress_summary_and_counters_are_localized() -> None:
    view = Telemetry(total_files=1280, rescued_files=1050, rescued_bytes=20_287_220, total_bytes=40_452_940,
                     rate_bytes_s=2_100_000, eta_seconds=725)
    assert progress_summary(view, "cs") == "19,3 MiB / 38,6 MiB · 1\u00a0050 / 1\u00a0280 souborů · 2,10 MB/s · zbývá 12m 05s"
    assert progress_summary(view, "en") == "19.3 MiB / 38.6 MiB · 1,050 / 1,280 files · 2.10 MB/s · 12m 05s left"
    assert progress_summary(Telemetry(), "en") == "0 / 0 files"
    items = {key: (text, tone) for key, text, tone in
             counter_items({"badge_copied": 5, "badge_failed": 2, "badge_timed_out": 0}, "cs")}
    assert items["badge_copied"] == ("Zkopírováno 5", "")
    assert items["badge_failed"] == ("Selhalo 2", "red")
    assert items["badge_timed_out"] == ("Vypršel čas 0", "muted")
    assert len(items) == 7


def test_locale_formatters() -> None:
    assert format_count(1_234_567, "cs") == "1\u00a0234\u00a0567"
    assert format_count(1_234_567, "en") == "1,234,567"
    assert localize_decimal("19.3 MiB", "cs") == "19,3 MiB" and localize_decimal("19.3 MiB", "en") == "19.3 MiB"
    assert format_percent(0.999, "cs") == "99\u00a0%" and format_percent(1.0, "en") == "100%"
    assert format_percent(57 / 100, "en") == "57%" and format_percent(29 / 100, "en") == "29%"
    assert format_percent(1 - 1e-12, "en") == "99%"
    assert elide_middle("short", 10, len) == "short"
    assert elide_middle("abcdefghij", 5, len) == "ab…ij"
    assert elide_middle("abcdefghij", 1, len) == "…"
    path = "Documents/Projects/2024/Customer/very/deep/folder/scan-0042.pdf"
    short = elide_middle(path, 30, len)
    assert len(short) == 30 and short.startswith("Documents/") and short.endswith("scan-0042.pdf")


def test_loaded_job_fields_are_read_only_when_idle() -> None:
    loaded = control_states(OpState.IDLE, job_loaded=True)
    assert loaded["job_fields"] is False and loaded["init"] is False
    assert loaded["scan"] and loaded["copy"] and loaded["approve"]
    assert loaded["resume"] is False
    assert control_states(OpState.PAUSED_STORAGE, job_loaded=True)["resume"] is True
    fresh = control_states(OpState.IDLE, job_loaded=False)
    assert fresh["job_fields"] and fresh["init"]
    assert not fresh["scan"]


# -- runner -------------------------------------------------------------------

class FakeProc:
    def __init__(self, lines=(), code=0, pid=4242, block: threading.Event | None = None):
        self.pid = pid
        self._lines = list(lines)
        self.code = code
        self.block = block
        self.done = threading.Event()
        self.stdout = self
        self.stderr = None
        self.closed = False

    def __iter__(self):
        for line in self._lines:
            if isinstance(line, BaseException):
                raise line
            yield line
        if self.block is not None:
            self.block.wait(5)

    def close(self):
        self.closed = True

    def wait(self, timeout=None):
        if self.block is not None and not self.block.wait(timeout):
            import subprocess
            raise subprocess.TimeoutExpired("fake", timeout)
        self.done.set()
        return self.code

    def poll(self):
        return self.code if self.done.is_set() else None


def drain(events: queue.Queue) -> list:
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            return items


def make_runner(procs, signals=None, **kwargs):
    events: queue.Queue = queue.Queue()
    procs = list(procs)
    calls = []

    def popen(cmd, **popen_kwargs):
        calls.append((cmd, popen_kwargs))
        return procs.pop(0)

    def killpg(pid, sig):
        (signals if signals is not None else []).append((pid, sig))

    runner = CommandRunner(events, popen=popen, killpg=kwargs.pop("killpg", killpg),
                           keep_awake_factory=kwargs.pop("keep_awake_factory", KeepAwake), **kwargs)
    return runner, events, calls


def test_runner_runs_steps_in_order_in_new_session() -> None:
    runner, events, calls = make_runner([FakeProc(["ok source\n"]), FakeProc(["scanned=1\n"])])
    steps = [CommandSpec(("preflight", "--job-dir", "J")), CommandSpec(("scan", "--job-dir", "J"))]
    runner.start(steps)
    runner.join(5)
    items = drain(events)
    finished = items[-1]
    assert isinstance(finished, RunFinished) and finished.completed_all
    assert [i.text for i in items if isinstance(i, OutputLine)] == ["ok source\n", "scanned=1\n"]
    assert calls[0][0][-3:] == ["preflight", "--job-dir", "J"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["errors"] == "replace"
    assert not runner.busy


def test_runner_stops_sequence_on_failed_preflight() -> None:
    runner, events, calls = make_runner([FakeProc(["fail source-exists gone\n"], code=1)])
    runner.start([CommandSpec(("preflight",)), CommandSpec(("copy",))])
    runner.join(5)
    finished = drain(events)[-1]
    assert len(calls) == 1
    assert finished.last_spec.subcommand == "preflight"
    assert state_after_exit(finished.returncode, stop_requested=finished.stop_requested) is OpState.FAILED


def test_runner_reports_storage_pause_exit_code() -> None:
    runner, events, _ = make_runner([FakeProc(["processed=1 paused=source unavailable\n"], code=3)])
    runner.start([CommandSpec(("copy",))])
    runner.join(5)
    finished = drain(events)[-1]
    assert state_after_exit(finished.returncode, stop_requested=finished.stop_requested) is OpState.PAUSED_STORAGE


def test_runner_stays_busy_until_child_reaped_after_reader_error() -> None:
    release = threading.Event()
    proc = FakeProc([UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")], code=-15, block=release)
    signals: list = []
    runner, events, _ = make_runner([proc], signals=signals, term_grace=0.05)
    runner.start([CommandSpec(("copy",))])
    deadline = time.monotonic() + 2
    while signals[-1:] != [(4242, signal.SIGKILL)] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (4242, signal.SIGTERM) in signals and (4242, signal.SIGKILL) in signals
    assert runner.busy, "child not yet reaped; runner must not report idle"
    assert not any(isinstance(item, RunFinished) for item in drain(events))
    release.set()
    runner.join(5)
    finished = [item for item in drain(events) if isinstance(item, RunFinished)]
    assert finished and finished[0].error and "UnicodeDecodeError" in finished[0].error
    assert not runner.busy


def test_runner_refuses_second_start_while_busy() -> None:
    release = threading.Event()
    runner, _, _ = make_runner([FakeProc(block=release)])
    runner.start([CommandSpec(("copy",))])
    from macos_data_rescue.gui.runner import RunnerBusy
    with pytest.raises(RunnerBusy):
        runner.start([CommandSpec(("copy",))])
    release.set()
    runner.join(5)


def test_runner_stop_escalates_sigint_sigterm_sigkill() -> None:
    release = threading.Event()
    signals: list = []

    def killpg(pid, sig):
        signals.append(sig)
        if sig == signal.SIGKILL:
            release.set()

    runner, events, _ = make_runner([FakeProc(block=release, code=-9)], killpg=killpg,
                                    stop_grace=0.05, term_grace=0.05)
    runner.start([CommandSpec(("copy",)), CommandSpec(("verify",))])
    time.sleep(0.05)
    assert runner.request_stop()
    runner.join(5)
    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    finished = drain(events)[-1]
    assert finished.stop_requested and finished.last_step == 0
    assert state_after_exit(finished.returncode, stop_requested=True) is OpState.STOPPED


def test_keep_awake_watches_child_pid() -> None:
    calls = []

    class Proc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kwargs):
        calls.append(cmd)
        return Proc()

    awake = KeepAwake(popen=popen, exists=lambda _path: True)
    awake.start(1234)
    awake.stop()
    assert calls == [["/usr/bin/caffeinate", "-dims", "-w", "1234"]]
    missing = KeepAwake(popen=popen, exists=lambda _path: False)
    missing.start(1)
    assert len(calls) == 1


def python_command(code: str):
    return lambda spec: [sys.executable, "-c", code]


def test_real_child_invalid_utf8_and_graceful_stop(tmp_path: Path) -> None:
    events: queue.Queue = queue.Queue()
    pids = []

    class FakeAwake:
        def start(self, pid):
            pids.append(pid)

        def stop(self):
            pids.append("stopped")

    code = ("import sys, time\n"
            "sys.stdout.buffer.write(b'name=\\xff\\xfe ok\\n'); sys.stdout.flush()\n"
            "try:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n    print('interrupted', flush=True); sys.exit(130)\n")
    runner = CommandRunner(events, command=python_command(code), keep_awake_factory=FakeAwake, stop_grace=5)
    runner.start([CommandSpec(("copy",))], keep_awake=True)
    deadline = time.monotonic() + 10
    lines: list[str] = []
    while not lines and time.monotonic() < deadline:
        lines = [i.text for i in drain(events) if isinstance(i, OutputLine)]
        time.sleep(0.02)
    assert lines == ["name=�� ok\n"]
    assert runner.request_stop()
    runner.join(10)
    items = drain(events)
    assert "interrupted\n" in [i.text for i in items if isinstance(i, OutputLine)]
    finished = items[-1]
    assert finished.returncode == 130 and finished.stop_requested
    assert isinstance(pids[0], int) and pids[-1] == "stopped"
    assert any(isinstance(i, RunnerNotice) and i.key == "log_stop_sigint" for i in items)


def test_stdout_capture_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    events: queue.Queue = queue.Queue()
    runner = CommandRunner(events, command=python_command("print('# report')"))
    runner.start([CommandSpec(("report",), stdout_path=target)])
    runner.join(10)
    assert target.read_text() == "# report\n"

    failing = CommandRunner(events, command=python_command("import sys; print('partial'); sys.exit(1)"))
    failing.start([CommandSpec(("report",), stdout_path=tmp_path / "other.md")])
    failing.join(10)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.md"]


# -- monitor ------------------------------------------------------------------

def test_snapshot_and_telemetry_from_real_job(tmp_path: Path) -> None:
    source = tmp_path / "home"
    write_file(source / "Desktop" / "a.txt", b"abc")
    write_file(source / "Documents" / "b.txt", b"defg")
    job_dir, _, dest = init_and_scan(tmp_path, source)
    snapshot = read_job_snapshot(job_dir)
    assert snapshot.profile == "customer-home"
    assert snapshot.dest == dest.resolve()
    assert snapshot.approvals["library"] is not None and snapshot.approvals["app-data"] is None
    telemetry = telemetry_from(snapshot)
    assert telemetry.total_files == 2
    assert telemetry.counts["badge_pending"] == 2
    assert telemetry.rescued_files == 0
    assert telemetry.bytes_fraction == 0.0 and telemetry.total_bytes > 0


def snapshot_with(activity: dict, counts: dict, byte_counts: dict | None = None) -> JobSnapshot:
    return JobSnapshot(JOB, Path("/s"), Path("/d"), "customer-home", (), counts, activity, "", {}, (),
                       byte_counts=byte_counts or {})


def test_telemetry_running_and_paused() -> None:
    running = telemetry_from(snapshot_with(
        {"state": "running", "rate_bytes_s": 2e6, "eta_seconds": 30, "path": "a", "current_bytes": 5,
         "transferred_bytes": 25, "remaining_bytes": 75},
        {"copied": 3, "copied_from_fallback": 1, "pending": 2, "copying": 1, "failed": 1},
        {"copied": 30, "copied_from_fallback": 10, "pending": 40, "copying": 10, "failed": 10}))
    assert running.rescued_files == 4 and running.total_files == 8
    assert running.counts["badge_pending"] == 3
    # whole-manifest bytes (like the file counter), not the current invocation's transfer
    assert running.bytes_fraction == 0.4 and running.rescued_bytes == 40 and running.total_bytes == 100
    assert running.current_path == "a" and running.eta_seconds == 30
    paused = telemetry_from(snapshot_with({"state": "paused", "reason": "source unavailable", "rate_bytes_s": 9,
                                           "path": "a"}, {}))
    assert paused.pause_reason == "source unavailable"
    assert paused.rate_bytes_s == 0 and paused.current_path is None
    assert paused.storage_losses == 0
    waiting = JobSnapshot(JOB, Path("/s"), Path("/d"), "customer-home", (), {}, {"state": "waiting", "rate_bytes_s": 9},
                          "", {}, (), storage_losses=3, reconnects=2)
    view = telemetry_from(waiting)
    assert (view.storage_losses, view.reconnects, view.rate_bytes_s, view.eta_seconds) == (3, 2, 0, None)


def test_monitor_coalesces_hung_reads() -> None:
    events: queue.Queue = queue.Queue()
    release = threading.Event()
    now = [100.0]

    def reader(job_dir):
        release.wait(5)
        raise OSError("disk gone")

    monitor = JobMonitor(events, reader=reader, clock=lambda: now[0])
    assert monitor.request(JOB) is True
    assert monitor.request(JOB) is False
    now[0] = 112.5
    assert monitor.stalled_for() == pytest.approx(12.5)
    release.set()
    result = events.get(timeout=5)
    assert result.snapshot is None and "disk gone" in result.error
    assert result.requested_at == 100.0, "results say when their read started (stale-read guard)"
    assert monitor.stalled_for() == 0
    assert monitor.request(JOB) is True


# -- auto-reconnect through the real CLI ---------------------------------------

def waiting_copy(tmp_path: Path, cooldown: str) -> tuple[Path, Path, CommandSpec]:
    """A job whose source is gone; network-style identities, so diskutil is never needed."""
    from test_auto_reconnect import seed_identities

    for name in ("a", "b"):
        write_file(tmp_path / "source-home" / name, b"hello")
    job_dir, source, _ = init_and_scan(tmp_path)
    seed_identities(job_dir, source_uuid=None, dest_uuid=None)
    source.rename(source.with_name("unplugged"))
    spec = copy_spec(job_dir, "all", 30.0, reconnect=ReconnectOptions(cooldown=float(cooldown)))
    return job_dir, source, spec


def wait_for_activity(job_dir: Path, runner: CommandRunner, state: str, timeout: float = 20.0) -> JobSnapshot:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert runner.busy, "the copy ended before reaching the expected activity"
        snapshot = read_job_snapshot(job_dir)
        if snapshot.activity.get("state") == state:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"activity never became {state}")


def finished_event(events: queue.Queue, runner: CommandRunner) -> RunFinished:
    runner.join(30)
    assert not runner.busy
    return [item for item in drain(events) if isinstance(item, RunFinished)][-1]


def test_stop_while_waiting_is_a_storage_pause_without_acknowledgement(tmp_path: Path) -> None:
    job_dir, _, spec = waiting_copy(tmp_path, cooldown="60")
    events: queue.Queue = queue.Queue()
    runner = CommandRunner(events)
    runner.start([spec])
    snapshot = wait_for_activity(job_dir, runner, "waiting")
    assert reconcile_with_activity(OpState.RUNNING, "waiting", child_running=runner.busy) is OpState.WAITING
    assert snapshot.activity["wait_phase"] == "cooldown" and snapshot.activity["attempt"] == 1
    assert snapshot.storage_losses == 1 and not snapshot.storage_blocked
    assert runner.request_stop()  # the console's Stop: SIGINT to the process group
    finished = finished_event(events, runner)
    assert finished.returncode == 3 and finished.stop_requested
    assert state_after_exit(finished.returncode, stop_requested=True) is OpState.PAUSED_STORAGE
    after = read_job_snapshot(job_dir)
    assert after.activity["state"] == "paused" and not after.storage_blocked
    resume = with_reconnect(resume_from(spec), ReconnectOptions())
    assert not needs_storage_acknowledgement(resume, blocked=after.storage_blocked)


def test_disk_back_during_cooldown_resumes_and_finishes(tmp_path: Path) -> None:
    job_dir, source, spec = waiting_copy(tmp_path, cooldown="1")
    events: queue.Queue = queue.Queue()
    runner = CommandRunner(events)
    runner.start([spec])
    wait_for_activity(job_dir, runner, "waiting")
    source.with_name("unplugged").rename(source)
    finished = finished_event(events, runner)
    assert finished.returncode == 0 and finished.completed_all
    after = read_job_snapshot(job_dir)
    assert after.activity["state"] == "finished"
    assert (after.storage_losses, after.reconnects) == (1, 1)
    assert after.counts.get("copied") == 2
