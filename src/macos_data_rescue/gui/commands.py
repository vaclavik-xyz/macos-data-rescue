"""Build and vet the CLI invocations the GUI is allowed to run.

The GUI never executes arbitrary text. Recommendations from ``next`` pass an
allow-list; ``approve`` is never executed from a recommendation because it
records customer consent and must go through the explicit approval dialog.
Likewise a recommendation carrying ``--acknowledge-storage-loss`` is never run
as is: it re-enables automatic reconnect after a give-up, so the operator must
confirm it in a dialog first (``GuideAction("acknowledge")``).
"""
from __future__ import annotations

import math
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..reconnect import ReconnectPolicy

CLI_NAME = "macos-data-rescue"

# Subcommands a recommendation may run directly.
GUIDE_RUNNABLE = frozenset({"scan", "copy", "resume", "verify", "status", "report", "customer-report"})
# Subcommands that read the source or write the destination; the environment
# is re-checked with preflight right before each of them.
NEEDS_PREFLIGHT = frozenset({"scan", "copy", "resume"})
COPY_COMMANDS = frozenset({"copy", "resume"})

RECONNECT_FLAG = "--auto-reconnect"
ACK_FLAG = "--acknowledge-storage-loss"
# value options of the reconnect loop; passed only when they differ from the CLI defaults
COOLDOWN_OPTION = "--reconnect-cooldown"
WAIT_OPTION = "--reconnect-wait"
MAX_RECONNECTS_OPTION = "--max-reconnects"
_RECONNECT_VALUE_OPTIONS = (COOLDOWN_OPTION, WAIT_OPTION, MAX_RECONNECTS_OPTION)
_DEFAULT_POLICY = ReconnectPolicy()
DEFAULT_COOLDOWN = _DEFAULT_POLICY.cooldown
DEFAULT_WAIT = _DEFAULT_POLICY.wait
DEFAULT_MAX_RECONNECTS = _DEFAULT_POLICY.limits.max_reconnects


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

    kind: str  # "run" | "approve" | "acknowledge" | "none"
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
        if ACK_FLAG in args:
            # never run silently: the GUI asks for confirmation and adds the flag itself
            if spec.subcommand not in COPY_COMMANDS:
                return GuideAction("none", line=line)
            vetted = _vet([arg for arg in args if arg != ACK_FLAG], job_dir)
            return GuideAction("acknowledge", spec=vetted, line=line) if vetted else GuideAction("none", line=line)
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
        args += ["--timeout", number_text(budget)]
    if limit is not None:
        args += ["--limit", str(limit)]
    return CommandSpec(tuple(args))


@dataclass(frozen=True)
class ReconnectOptions:
    """``--auto-reconnect`` and its limits; defaults equal the CLI defaults."""

    cooldown: float = DEFAULT_COOLDOWN
    wait: float = DEFAULT_WAIT
    max_reconnects: int = DEFAULT_MAX_RECONNECTS

    def args(self) -> tuple[str, ...]:
        args = [RECONNECT_FLAG]
        if self.cooldown != DEFAULT_COOLDOWN:
            args += [COOLDOWN_OPTION, number_text(self.cooldown)]
        if self.wait != DEFAULT_WAIT:
            args += [WAIT_OPTION, number_text(self.wait)]
        if self.max_reconnects != DEFAULT_MAX_RECONNECTS:
            args += [MAX_RECONNECTS_OPTION, str(self.max_reconnects)]
        return tuple(args)


def copy_spec(job_dir: Path, phase: str, timeout: float, limit: int | None = None, *,
              path: str | None = None, fallback_from: str | None = None, command: str = "copy",
              reconnect: ReconnectOptions | None = None) -> CommandSpec:
    args = [command, "--job-dir", str(job_dir), "--phase", phase, "--timeout", number_text(timeout)]
    if limit is not None:
        args += ["--limit", str(limit)]
    if path is not None and fallback_from is not None:
        args += ["--path", path, "--fallback-from", fallback_from]
    return with_reconnect(CommandSpec(tuple(args)), reconnect)


# copy/resume options that take a value (a value is never mistaken for a flag)
_COPY_VALUE_OPTIONS = frozenset({"--job-dir", "--phase", "--timeout", "--limit", "--path", "--fallback-from",
                                 *_RECONNECT_VALUE_OPTIONS})


def _without(args: tuple[str, ...], flags: frozenset[str], value_options: frozenset[str] = frozenset()) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _COPY_VALUE_OPTIONS:
            if arg not in value_options:
                kept += args[index:index + 2]
            index += 2
            continue
        if arg not in flags:
            kept.append(arg)
        index += 1
    return kept


def _without_reconnect(args: tuple[str, ...]) -> list[str]:
    return _without(args, frozenset({RECONNECT_FLAG, ACK_FLAG}), frozenset(_RECONNECT_VALUE_OPTIONS))


def with_reconnect(spec: CommandSpec, reconnect: ReconnectOptions | None) -> CommandSpec:
    """Replace the reconnect options of a copy/resume with the operator's current choice.

    Any earlier ``--acknowledge-storage-loss`` is dropped: it is only ever added by
    ``with_acknowledgement`` after an explicit confirmation. A ``--path`` recovery
    never gets ``--auto-reconnect`` (the CLI refuses that combination).
    """
    if spec.subcommand not in COPY_COMMANDS:
        return spec
    args = _without_reconnect(spec.args)
    if reconnect is not None and "--path" not in args:
        args += reconnect.args()
    return CommandSpec(tuple(args), spec.stdout_path)


def needs_storage_acknowledgement(spec: CommandSpec, *, blocked: bool, escalation: bool = False) -> bool:
    """A copy/resume asking for auto-reconnect after a give-up needs the operator's confirmation.

    ``blocked`` mirrors ``storage_events.blocked_by_give_up``; ``escalation`` is set
    when the recommended step itself asked for the acknowledgement.
    """
    return spec.subcommand in COPY_COMMANDS and RECONNECT_FLAG in spec.args and (blocked or escalation)


def with_acknowledgement(spec: CommandSpec) -> CommandSpec:
    """Add ``--acknowledge-storage-loss``; call only after the operator confirmed it."""
    if spec.subcommand not in COPY_COMMANDS or RECONNECT_FLAG not in spec.args:
        raise ValueError("acknowledgement only applies to copy/resume with --auto-reconnect")
    if ACK_FLAG in spec.args:
        return spec
    return CommandSpec((*spec.args, ACK_FLAG), spec.stdout_path)


def resume_from(spec: CommandSpec) -> CommandSpec:
    """Resume with the options of the paused copy/resume.

    The one-time ``--acknowledge-storage-loss`` is never carried over.
    """
    if spec.subcommand not in COPY_COMMANDS:
        raise ValueError(f"not a copy command: {spec.subcommand}")
    return CommandSpec(("resume", *_without(spec.args[1:], frozenset({ACK_FLAG}))))


def verify_spec(job_dir: Path, phase: str, timeout: float) -> CommandSpec:
    return CommandSpec(("verify", "--job-dir", str(job_dir), "--phase", phase, "--timeout", number_text(timeout)))


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


def parse_non_negative_float(text: str) -> float:
    """Mirror the CLI's non_negative_float (``--reconnect-cooldown``)."""
    value = float(text.strip().replace(",", "."))
    if not math.isfinite(value) or value < 0:
        raise ValueError(text)
    return value


def parse_positive_int(text: str) -> int:
    value = parse_optional_positive_int(text)
    if value is None:
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


def number_text(value: float) -> str:
    """Format a number the way the GUI passes it to the CLI (``300``, ``1.5``)."""
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
