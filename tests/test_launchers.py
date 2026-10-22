"""Static guards for the fresh-Mac setup script and the Finder launcher."""
import re
import subprocess

from helpers import ROOT

SETUP = ROOT / "setup-fresh-mac.sh"
COMMAND = ROOT / "macos-data-rescue.command"


def test_setup_script_is_strict_and_pinned() -> None:
    text = SETUP.read_text()
    assert "set -euo pipefail" in text
    assert re.search(r'^UV_VERSION="\d+\.\d+\.\d+"$', text, re.MULTILINE)
    assert 'UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"' in text
    assert "astral.sh/uv/install.sh" not in text, "unpinned installer"
    assert "| sh" not in text, "installer must be downloaded completely before it runs"
    assert "uv sync --locked" in text
    assert "/tmp/uv-bin" not in text


def test_finder_launcher_delegates_to_setup_script() -> None:
    text = COMMAND.read_text()
    assert "set -euo pipefail" in text
    assert 'exec "$DIR/setup-fresh-mac.sh" "$@"' in text
    assert "/tmp" not in text and "python3 -m" not in text


def test_scripts_parse() -> None:
    for script in (SETUP, COMMAND):
        subprocess.run(["bash", "-n", str(script)], check=True)
