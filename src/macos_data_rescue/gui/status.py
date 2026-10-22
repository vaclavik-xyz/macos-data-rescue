"""What the rescue tab shows, independent of any widget toolkit.

The status card turns the operation state and the raw ``next`` output into one
localized headline, one short sentence and at most one primary action. The
raw text stays available in the card's detail section. Nothing here decides
safety: ``StatusView.action`` only names which existing, guarded handler the
primary button calls (``execute_recommended_step`` or ``resume_paused``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..reporting import format_size
from .commands import CommandSpec, GuideAction
from .i18n import format_count, format_eta, format_rate, localize_decimal, tr
from .monitor import BADGE_STATUSES, Telemetry
from .runner import RunFinished
from .state import OpState, info, wait_view

# headline while the console's own command runs, by subcommand
RUN_HEADLINES = {
    "preflight": "status_run_preflight", "init": "status_run_init", "scan": "status_run_scan",
    "copy": "status_run_copy", "resume": "status_run_copy", "verify": "status_run_verify",
    "approve": "status_run_approve", "report": "status_run_report", "customer-report": "status_run_report",
}
# "what to do" sentence and primary-button label for a runnable recommendation
NEXT_STEP = {
    "scan": "status_next_scan", "copy": "status_next_copy", "resume": "status_next_resume",
    "verify": "status_next_verify", "report": "status_next_reports", "customer-report": "status_next_reports",
    "status": "status_next_status",
}
ACTION_LABELS = {
    "scan": "do_scan", "copy": "do_copy", "resume": "do_resume", "verify": "do_verify", "report": "do_report",
    "customer-report": "do_customer_report", "status": "do_status",
}
# counters that mean lost or unreadable data; colored only when non-zero
PROBLEM_COLORS = {"badge_timed_out": "amber", "badge_failed": "red", "badge_compressed": "red"}
COUNTER_KEYS = tuple(key for key, _statuses in BADGE_STATUSES)


@dataclass(frozen=True)
class StatusView:
    headline: str
    body: str
    tone: str = ""  # palette color name; "" is the default text color
    action: str = "none"  # "guide" (recommended step) | "resume" | "none"


def guide_state(text: str) -> str:
    """The ``state:`` value of a ``next`` output ("" when there is none)."""
    for line in text.splitlines():
        if line.startswith("state:"):
            return line[len("state:"):].strip()
    return ""


def _phase(spec: CommandSpec) -> str:
    return spec.option("--phase") or "all"


def next_step_sentence(action: GuideAction, lang: str) -> str:
    if action.kind == "approve" and action.phase:
        return tr("status_next_approve", lang, phase=action.phase)
    if action.spec is not None and action.spec.subcommand in NEXT_STEP:
        return tr(NEXT_STEP[action.spec.subcommand], lang, phase=_phase(action.spec))
    return tr("status_next_none", lang)


def action_label(action: GuideAction, lang: str) -> str:
    """Label of the recommended-step button; "" when there is nothing to run."""
    if action.kind == "approve" and action.phase:
        return tr("btn_guide_approve", lang, phase=action.phase)
    if action.spec is None:
        return ""
    if action.kind == "acknowledge":
        return tr("btn_resume_anyway", lang)
    return tr(ACTION_LABELS.get(action.spec.subcommand, "do_status"), lang, phase=_phase(action.spec))


def status_view(state: OpState, lang: str, *, job_loaded: bool, own_child: bool = True, guide_text: str = "",
                action: GuideAction = GuideAction("none"), escalation: bool = False,
                activity: dict | None = None, now: datetime | None = None,
                running: CommandSpec | None = None, finish: RunFinished | None = None) -> StatusView:
    """Headline, sentence, tone and primary action for the status card."""
    def t(key: str, **kwargs: object) -> str:
        return tr(key, lang, **kwargs)

    state_info = info(state)
    if state is OpState.RUNNING:
        headline = t(RUN_HEADLINES.get(running.subcommand if running else "", "status_run_other"))
        phase = running.option("--phase") if running is not None else None
        return StatusView(f"{headline} · {phase}" if phase else headline, t("status_running_body"))
    if state is OpState.WAITING:
        view = wait_view(activity or {}, now or datetime.now(timezone.utc))
        cooldown = view.phase == "cooldown"
        headline = t("status_cooldown" if cooldown else "status_waiting", remaining=format_eta(view.remaining, lang),
                     attempt=view.attempt if view.attempt is not None else "?")
        body = t("status_cooldown_body" if cooldown else state_info.message_key or "msg_waiting")
        if not own_child:
            body += " " + t("msg_waiting_external")
        return StatusView(headline, body, state_info.color)
    if state is OpState.STOPPING:
        return StatusView(t("status_stopping"), t("msg_stopping"), state_info.color)
    if state is OpState.PAUSED_STORAGE:
        if escalation:
            return StatusView(t("status_escalation"), t("msg_storage_escalation"), "red", "resume")
        return StatusView(t("status_paused"), t("msg_paused_storage"), state_info.color, "resume")
    if not job_loaded:
        return StatusView(t("status_no_job"), t("status_no_job_body"))

    step = "guide" if action.kind != "none" else "none"
    if state is OpState.FAILED:
        spec = finish.last_spec if finish is not None else None
        if spec is not None and spec.subcommand == "preflight" and len(finish.steps) > 1:
            return StatusView(t("status_preflight_failed"), t("msg_preflight_failed"), "red", step)
        code = "?"
        if finish is not None:
            code = finish.returncode if finish.returncode is not None else finish.error
        return StatusView(t("status_failed"), t("msg_failed", command=spec.subcommand if spec else "?", code=code),
                          "red", step)
    if state is OpState.STOPPED:
        return StatusView(t("status_stopped"), t("msg_stopped"), state_info.color, step)

    # idle or done: describe the job by its recommendation
    job_state = guide_state(guide_text)
    sentence = next_step_sentence(action, lang)
    if "integrity failed" in job_state:
        return StatusView(t("status_integrity_failed"), t("status_integrity_failed_body"), "red", step)
    if job_state.endswith("needs-review"):
        return StatusView(t("status_needs_review"), t("status_needs_review_body"), "amber", step)
    if job_state.endswith("complete"):
        if step == "none":
            return StatusView(t("status_done"), t("status_done_body"), "green")
        if job_state == "restore-copy-complete":
            sentence = t("status_restore_check_body")
        return StatusView(t("status_copy_complete"), sentence, "green", step)
    if action.kind == "approve":
        return StatusView(t("status_needs_consent"), sentence, "", step)
    return StatusView(t("status_ready"), sentence, "", step)


def format_bytes(count: int, lang: str) -> str:
    return localize_decimal(format_size(count), lang)


def progress_summary(view: Telemetry, lang: str) -> str:
    """One line: data, files, speed and time left (speed and time only while copying)."""
    parts = []
    if view.total_bytes > 0:
        parts.append(f"{format_bytes(view.rescued_bytes, lang)} / {format_bytes(view.total_bytes, lang)}")
    parts.append(tr("summary_files", lang, done=format_count(view.rescued_files, lang),
                    total=format_count(view.total_files, lang)))
    if view.rate_bytes_s > 0:
        parts.append(localize_decimal(format_rate(view.rate_bytes_s), lang))
    if view.eta_seconds is not None and view.eta_seconds >= 0:
        parts.append(tr("summary_eta", lang, eta=format_eta(view.eta_seconds, lang)))
    return " · ".join(parts)


def counter_items(counts: dict[str, int], lang: str) -> list[tuple[str, str, str]]:
    """``(key, text, tone)`` per status counter; tone "muted" for zero, a color for a problem."""
    items = []
    for key in COUNTER_KEYS:
        value = counts.get(key, 0)
        tone = "muted" if value == 0 else PROBLEM_COLORS.get(key, "")
        items.append((key, f"{tr(key, lang)} {format_count(value, lang)}", tone))
    return items
