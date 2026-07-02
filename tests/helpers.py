import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    src_path = str(ROOT / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "macos_data_rescue", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def init_and_scan(tmp_path: Path, source: Path | None = None) -> tuple[Path, Path, Path]:
    job_dir = tmp_path / "job"
    source_dir = source or tmp_path / "source-home"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source_dir), "--dest", str(dest_dir))
    # the legacy default scan includes ~/Library and is gated; these fixtures
    # represent jobs with the customer consent already recorded
    run_cli("approve", "--job-dir", str(job_dir), "--phase", "library")
    run_cli("scan", "--job-dir", str(job_dir))
    return job_dir, source_dir, dest_dir


def file_rows(job_dir: Path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("select * from files order by relative_path").fetchall()
    finally:
        conn.close()
    return {row["relative_path"]: row for row in rows}


def config_value(job_dir: Path, key: str) -> str | None:
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select value from config where key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None
