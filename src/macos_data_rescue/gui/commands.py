"""Build and vet the CLI invocations the GUI is allowed to run.

The GUI never executes arbitrary text. Recommendations from ``next`` pass an
allow-list; ``approve`` is never executed from a recommendation because it
records customer consent and must go through the explicit approval dialog.
"""
from __future__ import annotations

import math
import shlex
from dataclasses import dataclass
from pathlib import Path

CLI_NAME = "macos-data-rescue"

# Subcommands a recommendation may run directly.
GUIDE_RUNNABLE = frozenset({"scan", "copy", "resume", "verify", "status", "report", "customer-report"})
# Subcommands that read the source or write the destination; the environment
# is re-checked with preflight right before each of them.
NEEDS_PREFLIGHT = frozenset({"scan", "copy", "resume"})


@dataclass(frozen=True)
class CommandSpec:
    """One CLI invocation: ``macos-data-rescue <args>``.

    ``stdout_path`` captures stdout into that file (written atomically), the
    equivalent of ``... > file`` in the printed recommendations.
    """

    args: tuple[str, ...]
    stdout_path: Path | None = None

    @property
    def subcommand(self) -> str:
        return self.args[0] if self.args else ""

    def option(self, name: str) -> str | None:
        args = self.args
        for index, value in enumerate(args[:-1]):
            if value == name:
                return args[index + 1]
        return None

    def display(self) -> str:
        text = shlex.join((CLI_NAME, *self.args))
        if self.stdout_path is not None:
            text += f" > {shlex.quote(str(self.stdout_path))}"
        return text


@dataclass(frozen=True)
class GuideAction:
    """What the "recommended step" button does for the current ``next`` output."""

    kind: str  # "run" | "approve" | "none"
    spec: CommandSpec | None = None
    phase: str | None = None
    line: str | None = None


def parse_guide_action(text: str, job_dir: Path) -> GuideAction:
    """Pick the first command recommended by ``next`` and vet it."""
    for raw in text.splitlines():
        try:
            tokens = shlex.split(raw)
        except ValueError:
            continue
        if CLI_NAME not in tokens:
            continue
        args = tokens[tokens.index(CLI_NAME) + 1:]
        if not args:
            continue
        line = raw.strip()
        spec = CommandSpec(tuple(args))
        if spec.subcommand == "approve":
            phase = spec.option("--phase")
            return GuideAction("approve", phase=phase, line=line) if phase else GuideAction("none", line=line)
        if spec.subcommand not in GUIDE_RUNNABLE:
            return GuideAction("none", line=line)
        vetted = _vet(args, job_dir)
        return GuideAction("run", spec=vetted, line=line) if vetted else GuideAction("none", line=line)
    return GuideAction("none")


def _vet(args: list[str], job_dir: Path) -> CommandSpec | None:
    stdout_path: Path | None = None
    if ">" in args:
        index = args.index(">")
        target = args[index + 1:]
        args = args[:index]
        # only "report ... > JOB/<file>" is ever printed by next
        if args[0] != "report" or len(target) != 1:
            return None
        stdout_path = Path(target[0])
        if stdout_path.parent != job_dir or not stdout_path.name or stdout_path.name.startswith("."):
            return None
    spec = CommandSpec(tuple(args), stdout_path)
    if spec.option("--job-dir") != str(job_dir):
        return None
    if any(token in {"|", ";", "&&", "||", "<", ">>"} for token in args):
        return None
    return spec


def with_preflight(spec: CommandSpec, job_dir: Path) -> list[CommandSpec]:
    """Prefix work that touches source or destination with a fresh preflight."""
    if spec.subcommand in NEEDS_PREFLIGHT:
        return [preflight_spec(job_dir), spec]
    return [spec]


def preflight_spec(job_dir: Path, source: Path | None = None, dest: Path | None = None) -> CommandSpec:
    args = ["preflight", "--job-dir", str(job_dir)]
    if source is not None and dest is not None:
        args += ["--source", str(source), "--dest", str(dest)]
    return CommandSpec(tuple(args))


def init_spec(job_dir: Path, source: Path, dest: Path, profile: str, excludes: tuple[str, ...] = ()) -> CommandSpec:
    args = ["init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest), "--profile", profile]
    for pattern in excludes:
        args += ["--exclude", pattern]
    return CommandSpec(tuple(args))


def scan_spec(job_dir: Path, phase: str, budget: float | None, limit: int | None) -> CommandSpec:
    args = ["scan", "--job-dir", str(job_dir), "--phase", phase]
    if budget is not None:
        args += ["--timeout", _number(budget)]
    if limit is not None:
        args += ["--limit", str(limit)]
    return CommandSpec(tuple(args))


def copy_spec(job_dir: Path, phase: str, timeout: float, limit: int | None = None, *,
              path: str | None = None, fallback_from: str | None = None, command: str = "copy") -> CommandSpec:
    args = [command, "--job-dir", str(job_dir), "--phase", phase, "--timeout", _number(timeout)]
    if limit is not None:
        args += ["--limit", str(limit)]
    if path is not None and fallback_from is not None:
        args += ["--path", path, "--fallback-from", fallback_from]
    return CommandSpec(tuple(args))


def resume_from(spec: CommandSpec) -> CommandSpec:
    """Resume with exactly the options of the paused copy/resume."""
    if spec.subcommand not in {"copy", "resume"}:
        raise ValueError(f"not a copy command: {spec.subcommand}")
    return CommandSpec(("resume", *spec.args[1:]))


def verify_spec(job_dir: Path, phase: str, timeout: float) -> CommandSpec:
    return CommandSpec(("verify", "--job-dir", str(job_dir), "--phase", phase, "--timeout", _number(timeout)))


def approve_spec(job_dir: Path, phase: str, by: str) -> CommandSpec:
    by = by.strip()
    if not by:
        raise ValueError("approval requires the name of the approving person")
    return CommandSpec(("approve", "--job-dir", str(job_dir), "--phase", phase, "--by", by))


def customer_report_spec(job_dir: Path, report_format: str, language: str) -> CommandSpec:
    return CommandSpec(("customer-report", "--job-dir", str(job_dir), "--format", report_format,
                        "--language", language))


def parse_positive_float(text: str) -> float:
    """Mirror the CLI's positive_float so invalid input never reaches argparse."""
    value = float(text.strip().replace(",", "."))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(text)
    return value


def parse_optional_positive_int(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    value = int(text)
    if value <= 0:
        raise ValueError(text)
    return value


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def parse_preflight_line(line: str) -> tuple[str, str, str] | None:
    """Parse one ``preflight`` check line: ``<level> <name> <detail>``."""
    parts = line.rstrip("\n").split(" ", 2)
    if len(parts) < 2 or parts[0] not in {"ok", "warn", "fail"}:
        return None
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def parse_report_path(line: str) -> Path | None:
    """Extract the file written by ``customer-report`` (``wrote report=PATH``)."""
    prefix = "wrote report="
    line = line.strip()
    return Path(line[len(prefix):]) if line.startswith(prefix) else None
