"""Technician GUI console for macos-data-rescue (tkinter, standard library only).

Tkinter is imported lazily so the display-free logic modules (and the CLI)
keep working on Python builds without Tk.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def run_gui(job_dir: Path | None = None, source: Path | None = None, dest: Path | None = None,
            lang: str = "cs") -> int:
    from .app import run_gui as _run_gui

    return _run_gui(job_dir=job_dir, source=source, dest=dest, lang=lang)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="macos-data-rescue-gui",
        description="Graphical technician console for macOS Data Rescue.",
    )
    parser.add_argument("--job-dir", type=Path, help="Open an existing job directory.")
    parser.add_argument("--source", type=Path, help="Pre-fill source directory.")
    parser.add_argument("--dest", type=Path, help="Pre-fill destination directory.")
    parser.add_argument("--lang", default="cs", choices=("cs", "en"), help="Interface language (cs or en).")
    args = parser.parse_args(argv)
    return run_gui(job_dir=args.job_dir, source=args.source, dest=args.dest, lang=args.lang)
