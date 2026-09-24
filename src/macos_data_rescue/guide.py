from __future__ import annotations

import shlex
from pathlib import Path

from .activity import activity_snapshot

from .manifest import (
    JobConfig,
    UNREADABLE_COMPRESSED,
    connect,
    scan_issues,
    load_approval,
    load_config,
    load_scan_cursor,
    load_scan_done,
    manifest_path,
)


CORE_PHASES = ("visible-home", "hidden-home")
# next suggests only the customer phases; the legacy library/all gate exists
# in scan/copy but is never part of the guided flow
SUGGESTED_GATES = ("app-data", "applications", "full-home")
LEGACY_PHASES = {"important", "photos", "library", "all"}
SCAN_TIMEOUT = "300"
COPY_TIMEOUT = "3600"
RETRY_LIMIT = 2


def next_text(job_dir: Path) -> str:
    if not manifest_path(job_dir).exists():
        return missing_job_text(job_dir)
    config = load_config(job_dir)
    stats = phase_stats(job_dir)
    lines = [f"job={job_dir} profile={config.profile}"]
    activity = activity_snapshot(job_dir)
    if activity.get("state") == "paused":
        return "\n".join(lines + [f"state: paused ({activity.get('reason')})",
                                  "Reconnect the original disks / free destination space, then run:",
                                  base_cmd("resume", "--job-dir", quoted(job_dir), "--phase", quoted(activity["phase"]))])
    conn = connect(job_dir)
    try:
        columns = {row[1] for row in conn.execute("pragma table_info(files)")}
        failed_integrity = (conn.execute("select phase from files where verification_status = 'failed' "
                                         "and status in ('copied', 'copied_from_fallback') order by relative_path limit 1").fetchone()
                            if "verification_status" in columns else None)
    finally:
        conn.close()
    if failed_integrity:
        return "\n".join(lines + ["state: destination integrity failed; reconnect the source, repeat copying, then verify again",
                                  "run:", base_cmd("resume", "--job-dir", quoted(job_dir), "--phase", quoted(failed_integrity[0]))])
    issues = scan_issues(job_dir)
    if issues:
        lines.append(f"review: {len(issues)} unscanned path(s); coverage is incomplete. Inspect report and retry scan after fixing access.")
    exhausted = sum(int(item["exhausted"]) for item in stats.values())
    if exhausted:
        lines.append(
            f"review: {exhausted} file(s) need manual review (exhausted retries or unreadable compression); inspect the technician report"
        )
    if config.profile in {"restore", "volume"}:
        lines.extend(restore_lines(job_dir, stats, profile=config.profile))
    else:
        lines.extend(customer_lines(job_dir, config, stats))
    return "\n".join(lines)


def phase_stats(job_dir: Path) -> dict[str, dict[str, int]]:
    conn = connect(job_dir)
    try:
        rows = conn.execute(
            """
            select phase,
                   sum(case when status in ('pending', 'copying') then 1 else 0 end) as work,
                   sum(case when status in ('failed', 'timed_out') and attempts < ? then 1 else 0 end)
                       as retryable,
                   sum(case when (status in ('failed', 'timed_out') and attempts >= ?)
                                 or status = ? then 1 else 0 end)
                       as exhausted,
                   count(*) as total
            from files
            group by phase
            """,
            (RETRY_LIMIT, RETRY_LIMIT, UNREADABLE_COMPRESSED),
        ).fetchall()
    finally:
        conn.close()
    return {
        row["phase"]: {
            "work": int(row["work"]),
            "retryable": int(row["retryable"]),
            "exhausted": int(row["exhausted"]),
            "total": int(row["total"]),
        }
        for row in rows
    }


def customer_lines(job_dir: Path, config: JobConfig, stats: dict[str, dict[str, int]]) -> list[str]:
    if stats and all(phase in LEGACY_PHASES for phase in stats):
        return [
            "state: legacy-phase job",
            "next guides only the default customer workflow; follow docs/agent-runbook.md manually",
            "run:",
            f"  {base_cmd('status', '--job-dir', quoted(job_dir))}",
        ]
    full_home_active = phase_touched(job_dir, stats, "full-home")
    for phase in CORE_PHASES + SUGGESTED_GATES:
        item = stats.get(phase)
        actionable = item["work"] + item["retryable"] if item else 0
        has_gated_work = phase in SUGGESTED_GATES and (
            actionable or load_scan_cursor(job_dir, phase) is not None
        )
        if has_gated_work and load_approval(job_dir, phase) is None:
            # rows or cursors from an older version exist, but the gate would
            # reject the copy/scan command; the approval must come first
            return [
                f"state: approval-required phase={phase}",
                "this job already contains rows or an interrupted scan cursor for a gated",
                "phase without a recorded approval; confirm the original customer consent",
                "and record it, then run next again:",
                f"  {base_cmd('approve', '--job-dir', quoted(job_dir), '--phase', phase)}",
            ]
        if actionable:
            return action_lines(job_dir, action="copy", phase=phase, rows=actionable)
        if load_scan_cursor(job_dir, phase) is not None:
            return action_lines(job_dir, action="scan", phase=phase, reason="resume-interrupted-scan")
        if phase in CORE_PHASES and not full_home_active and not phase_touched(job_dir, stats, phase):
            return action_lines(job_dir, action="scan", phase=phase, reason="unscanned")
        if (
            phase in SUGGESTED_GATES
            and not phase_touched(job_dir, stats, phase)
            and load_approval(job_dir, phase) is not None
        ):
            return action_lines(job_dir, action="scan", phase=phase, reason="approved-unscanned")
    return terminal_lines(job_dir, config, stats)


def restore_lines(job_dir: Path, stats: dict[str, dict[str, int]], *, profile: str = "restore") -> list[str]:
    item = stats.get(profile)
    actionable = item["work"] + item["retryable"] if item else 0
    if actionable:
        return action_lines(job_dir, action="copy", rows=actionable)
    scan_incomplete = load_scan_cursor(job_dir, profile) is not None or (
        item is None and load_scan_done(job_dir, profile) is None
    )
    if scan_incomplete:
        return action_lines(job_dir, action="scan", reason="scan-not-complete")
    if item is not None and item["exhausted"]:
        return [
            f"state: {profile}-needs-review",
            "resume would retry the exhausted rows; inspect these before handoff:",
            "inspect the technician report and resolve or acknowledge every failed row:",
            f"  {base_cmd('report', '--job-dir', quoted(job_dir), '--format', 'markdown')}"
            f" > {quoted(job_dir / 'report.md')}",
        ]
    if profile == "volume":
        return [
            "state: volume-copy-complete",
            "verify the selected scope and sample recovered files before handoff",
            f"  {base_cmd('status', '--job-dir', quoted(job_dir))}",
            *missing_report_cmds(job_dir, load_config(job_dir)),
        ]
    return [
        "state: restore-copy-complete",
        "verify:",
        f"  {base_cmd('resume', '--job-dir', quoted(job_dir), '--timeout', COPY_TIMEOUT)}",
        "  (must print processed=0)",
        f"  {base_cmd('status', '--job-dir', quoted(job_dir))}",
        "  (total row count must equal the rescue job's copied= plus copied_from_fallback= counts)",
        "evidence:",
        f"  {base_cmd('report', '--job-dir', quoted(job_dir), '--format', 'markdown')}"
        f" > {quoted(job_dir / 'report.md')}",
        "note: for Share Disk/TDM targets fix ownership on the new Mac "
        "(diskutil resetUserPermissions) before handoff",
    ]


def action_lines(
    job_dir: Path,
    *,
    action: str,
    phase: str | None = None,
    rows: int | None = None,
    reason: str | None = None,
) -> list[str]:
    state = f"state: action={action}"
    if phase is not None:
        state += f" phase={phase}"
    if rows is not None:
        state += f" rows={rows}"
    if reason is not None:
        state += f" reason={reason}"
    args = [action, "--job-dir", quoted(job_dir)]
    if phase is not None:
        args.extend(["--phase", phase])
    args.extend(["--timeout", COPY_TIMEOUT if action == "copy" else SCAN_TIMEOUT])
    return [state, "run:", f"  {base_cmd(*args)}", "then run next again"]


def terminal_lines(job_dir: Path, config: JobConfig, stats: dict[str, dict[str, int]]) -> list[str]:
    lines = ["state: core-phases-complete"]
    gates = [
        phase
        for phase in SUGGESTED_GATES
        if not phase_touched(job_dir, stats, phase) and load_approval(job_dir, phase) is None
    ]
    if gates:
        lines.append("ask-before: " + " ".join(gates))
        lines.append("ask the operator/customer, record the decision, then run next again:")
        for phase in gates:
            lines.append(f"  {base_cmd('approve', '--job-dir', quoted(job_dir), '--phase', phase)}")
        lines.append("scan refuses these phases until the approval is recorded")
    report_cmds = missing_report_cmds(job_dir, config)
    if report_cmds:
        lines.append("reports (missing):")
        lines.extend(f"  {command}" for command in report_cmds)
    if not gates and not report_cmds:
        lines.append("done: follow the handoff checklist in docs/agent-runbook.md")
    return lines


def missing_report_cmds(job_dir: Path, config: JobConfig) -> list[str]:
    commands: list[str] = []
    if not (config.dest.parent / "recovery-report.pdf").exists():
        commands.append(base_cmd("customer-report", "--job-dir", quoted(job_dir), "--format", "pdf"))
    if not (job_dir / "report.md").exists():
        commands.append(
            base_cmd("report", "--job-dir", quoted(job_dir), "--format", "markdown")
            + f" > {quoted(job_dir / 'report.md')}"
        )
    if not (job_dir / "report.json").exists():
        commands.append(
            base_cmd("report", "--job-dir", quoted(job_dir), "--format", "json")
            + f" > {quoted(job_dir / 'report.json')}"
        )
    return commands


def phase_touched(job_dir: Path, stats: dict[str, dict[str, int]], phase: str) -> bool:
    if phase in stats:
        return True
    if load_scan_cursor(job_dir, phase) is not None:
        return True
    return load_scan_done(job_dir, phase) is not None


def missing_job_text(job_dir: Path) -> str:
    job_quoted = quoted(job_dir)
    return "\n".join(
        [
            f"no job found at {job_dir}",
            "run:",
            f"  {base_cmd('preflight', '--job-dir', job_quoted)} --source SOURCE --dest DEST",
            f"  {base_cmd('init', '--job-dir', job_quoted)} --source SOURCE --dest DEST",
            "add --profile restore for a restore job or --profile volume for a folder/disk, then run next again",
        ]
    )


def base_cmd(*args: str) -> str:
    return " ".join(("uv", "run", "macos-data-rescue", *args))


def quoted(path: Path) -> str:
    return shlex.quote(str(path))
