from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from .copier import copy_job
from .errors import RescueError
from .manifest import init_manifest
from .reporting import report, status_text
from .scanner import scan_job


PHASES = (
    "visible-home",
    "hidden-home",
    "app-data",
    "applications",
    "full-home",
    "important",
    "photos",
    "library",
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
    init_parser.add_argument("--profile", default="customer-home", choices=("customer-home",))

    scan_parser = subparsers.add_parser("scan", help="Scan source files into the manifest.")
    scan_parser.add_argument("--job-dir", required=True, type=Path)
    scan_parser.add_argument("--phase", default="all", choices=PHASES)

    copy_parser = subparsers.add_parser("copy", help="Copy files recorded in the manifest.")
    add_copy_options(copy_parser)

    resume_parser = subparsers.add_parser("resume", help="Resume a copy job.")
    add_copy_options(resume_parser)

    status_parser = subparsers.add_parser("status", help="Print manifest status counts.")
    status_parser.add_argument("--job-dir", required=True, type=Path)

    report_parser = subparsers.add_parser("report", help="Print a rescue report.")
    report_parser.add_argument("--job-dir", required=True, type=Path)
    report_parser.add_argument("--format", default="markdown", choices=("markdown", "json"))

    return parser


def add_copy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--phase", default="all", choices=PHASES)
    parser.add_argument("--timeout", default=30.0, type=positive_float)
    parser.add_argument("--limit", type=positive_int)


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
            config = init_manifest(args.job_dir, args.source, args.dest, args.profile)
            print(f"initialized job={config.job_dir} source={config.source} dest={config.dest}")
        elif args.command == "scan":
            count = scan_job(args.job_dir, phase=args.phase)
            print(f"scanned={count}")
        elif args.command in {"copy", "resume"}:
            summary = copy_job(
                args.job_dir,
                phase=args.phase,
                timeout=args.timeout,
                limit=args.limit,
            )
            print(summary.as_line())
        elif args.command == "status":
            print(status_text(args.job_dir))
        elif args.command == "report":
            sys.stdout.write(report(args.job_dir, args.format))
        else:
            parser.error(f"unknown command: {args.command}")
    except RescueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0
