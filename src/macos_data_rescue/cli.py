from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from .activity import watch_status
from .copier import copy_job
from .errors import RescueError
from .guide import next_text
from .manifest import GATED_PHASES, init_manifest, record_approval
from .preflight import preflight_job
from .reporting import CUSTOMER_REPORT_LANGUAGES, report, status_text, write_customer_report
from .scanner import scan_job
from .verification import verify_job


PHASES = (
    "visible-home",
    "hidden-home",
    "app-data",
    "applications",
    "full-home",
    "important",
    "photos",
    "library",
    "restore",
    "volume",
    "all",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macos-data-rescue",
        description="Resumable macOS user data rescue CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a rescue job manifest.")
    init_parser.add_argument("--job-dir", required=True, type=Path)
    init_parser.add_argument("--source", required=True, type=Path)
    init_parser.add_argument("--dest", required=True, type=Path)
    init_parser.add_argument("--profile", default="customer-home", choices=("customer-home", "restore", "volume"))

    init_parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                             help="Volume profile only: exclude a source-relative glob (repeatable).")

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Check the environment before init or before continuing a job.",
    )
    preflight_parser.add_argument("--job-dir", required=True, type=Path)
    preflight_parser.add_argument("--source", type=Path)
    preflight_parser.add_argument("--dest", type=Path)

    scan_parser = subparsers.add_parser("scan", help="Scan source files into the manifest.")
    scan_parser.add_argument("--job-dir", required=True, type=Path)
    scan_parser.add_argument("--phase", default="all", choices=PHASES)
    scan_parser.add_argument("--timeout", type=positive_float, help="Overall scan budget in seconds.")
    scan_parser.add_argument("--io-timeout", default=30.0, type=positive_float, help="Maximum seconds per scan filesystem operation.")
    scan_parser.add_argument("--limit", type=positive_int)

    copy_parser = subparsers.add_parser("copy", help="Copy files recorded in the manifest.")
    add_copy_options(copy_parser)

    resume_parser = subparsers.add_parser("resume", help="Resume a copy job.")
    add_copy_options(resume_parser)

    verify_parser = subparsers.add_parser("verify", help="Verify destination SHA256 without rereading source files.")
    verify_parser.add_argument("--job-dir", required=True, type=Path)
    verify_parser.add_argument("--phase", default="all", choices=PHASES)
    verify_parser.add_argument("--timeout", default=30.0, type=positive_float)
    verify_parser.add_argument("--limit", type=positive_int)

    status_parser = subparsers.add_parser("status", help="Print manifest status counts.")
    status_parser.add_argument("--job-dir", required=True, type=Path)
    status_parser.add_argument("--watch", action="store_true", help="Refresh progress until Ctrl-C.")
    status_parser.add_argument("--interval", default=1.0, type=positive_float)
    status_parser.add_argument("--count", type=positive_int, help="Stop watch after N snapshots.")

    next_parser = subparsers.add_parser("next", help="Print the next recommended command for a job.")
    next_parser.add_argument("--job-dir", required=True, type=Path)

    approve_parser = subparsers.add_parser(
        "approve",
        help="Record operator/customer approval for a gated phase.",
    )
    approve_parser.add_argument("--job-dir", required=True, type=Path)
    approve_parser.add_argument("--phase", required=True, choices=GATED_PHASES)
    approve_parser.add_argument("--by")

    report_parser = subparsers.add_parser("report", help="Print a rescue report.")
    report_parser.add_argument("--job-dir", required=True, type=Path)
    report_parser.add_argument("--format", default="markdown", choices=("markdown", "json"))

    customer_report_parser = subparsers.add_parser(
        "customer-report",
        help="Write a short customer-facing recovery report.",
    )
    customer_report_parser.add_argument("--job-dir", required=True, type=Path)
    customer_report_parser.add_argument("--format", default="pdf", choices=("markdown", "pdf"))
    customer_report_parser.add_argument("--language", default="en", choices=CUSTOMER_REPORT_LANGUAGES)
    customer_report_parser.add_argument("--output", type=Path)

    return parser


def add_copy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--phase", default="all", choices=PHASES)
    parser.add_argument("--timeout", default=30.0, type=positive_float)
    parser.add_argument("--limit", type=positive_int)
    parser.add_argument("--path", help="Failed manifest path to recover from an explicitly selected duplicate.")
    parser.add_argument("--fallback-from", help="Alternate source-relative file; requires --path.")


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            config = init_manifest(args.job_dir, args.source, args.dest, args.profile, excludes=tuple(args.exclude))
            print(f"initialized job={config.job_dir} source={config.source} dest={config.dest}")
        elif args.command == "preflight":
            summary = preflight_job(args.job_dir, args.source, args.dest)
            for line in summary.as_lines():
                print(line)
            if summary.failed:
                return 1
        elif args.command == "scan":
            summary = scan_job(args.job_dir, phase=args.phase, limit=args.limit, timeout=args.timeout, io_timeout=args.io_timeout)
            print(summary.as_line())
            if summary.issues:
                return 1
        elif args.command in {"copy", "resume"}:
            summary = copy_job(
                args.job_dir,
                phase=args.phase,
                timeout=args.timeout,
                limit=args.limit,
                path=args.path,
                fallback_from=args.fallback_from,
            )
            print(summary.as_line())
            if summary.paused:
                return 3
        elif args.command == "verify":
            summary = verify_job(args.job_dir, phase=args.phase, timeout=args.timeout, limit=args.limit)
            print(summary.as_line())
            if summary.failed or summary.unverifiable:
                return 1
        elif args.command == "status":
            if args.watch:
                watch_status(args.job_dir, interval=args.interval, count=args.count)
            else:
                print(status_text(args.job_dir))
        elif args.command == "next":
            print(next_text(args.job_dir))
        elif args.command == "approve":
            value = record_approval(args.job_dir, args.phase, args.by)
            print(f"approved phase={args.phase} at={value}")
        elif args.command == "report":
            sys.stdout.write(report(args.job_dir, args.format))
        elif args.command == "customer-report":
            output_path = write_customer_report(args.job_dir, args.format, args.output, args.language)
            print(f"wrote report={output_path}")
        else:
            parser.error(f"unknown command: {args.command}")
    except RescueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
