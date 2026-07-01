import tomllib

from pathlib import Path

from helpers import ROOT, run_cli, write_file, init_and_scan



def test_pyproject_declares_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert data["project"]["scripts"]["macos-data-rescue"] == "macos_data_rescue.cli:main"


def test_rescue_error_returns_one_but_argparse_usage_returns_two(tmp_path: Path) -> None:
    missing_job = tmp_path / "missing-job"

    job_error = run_cli("status", "--job-dir", str(missing_job), check=False)
    usage_error = run_cli("copy", check=False)

    assert job_error.returncode == 1
    assert "manifest not found" in job_error.stderr
    assert usage_error.returncode == 2
    assert "usage:" in usage_error.stderr


def test_copy_rejects_non_finite_timeout_as_usage_error(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir, _, dest_dir = init_and_scan(tmp_path, source)

    for value in ("nan", "inf"):
        result = run_cli(
            "copy",
            "--job-dir",
            str(job_dir),
            "--phase",
            "important",
            "--timeout",
            value,
            check=False,
        )

        assert result.returncode == 2
        assert "finite number greater than zero" in result.stderr
        assert not (dest_dir / "Desktop" / "invoice.txt").exists()
