#!/bin/bash
# ==============================================================================
# macOS Data Rescue — set up and launch the technician console on a clean Mac
#
# 1. Installs a pinned uv release into ~/.local/bin if uv is missing
#    (no sudo, Homebrew or Xcode Command Line Tools needed).
# 2. Installs the locked environment with a uv-managed Python 3.11+
#    (includes Tk; the project itself has no runtime dependencies).
# 3. Reminds the technician about Full Disk Access.
# 4. Launches the GUI. Arguments are passed through, e.g. --lang en.
# ==============================================================================
set -euo pipefail

# Pinned uv release (installer URL is version-specific); bump deliberately.
UV_VERSION="0.12.23"
UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Only user-owned locations; never a world-writable directory such as /tmp.
export PATH="$HOME/.local/bin:$PATH"
# Prefer uv-managed Python: it ships Tk and avoids the /usr/bin/python3 stub
# that asks to install the Command Line Tools on a fresh Mac.
export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-only-managed}"

echo "=================================================================="
echo " macOS Data Rescue — příprava a spuštění / setup and launch"
echo "=================================================================="

if ! command -v uv >/dev/null 2>&1; then
    if [ -x ".venv/bin/python" ]; then
        # offline fallback: an environment from an earlier `uv sync` already has the package installed
        echo "[!] uv nenalezen, spouštím existující prostředí .venv / uv not found, using existing .venv"
        exec .venv/bin/python -m macos_data_rescue.gui "$@"
    fi
    echo "[1/3] Instaluji uv ${UV_VERSION} do ~/.local/bin / installing uv ${UV_VERSION}..."
    mkdir -p "$HOME/.local/bin"
    installer="$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")"
    trap 'rm -f "$installer"' EXIT
    # Download completely before running, so a broken connection never runs half a script.
    curl --proto '=https' --tlsv1.2 -LsSf "$UV_INSTALLER_URL" -o "$installer"
    env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$installer"
    rm -f "$installer"
    trap - EXIT
else
    echo "[1/3] uv: $(command -v uv) ($(uv --version))"
fi

echo "[2/3] Připravuji prostředí / preparing the locked environment..."
uv sync --locked --no-dev

echo "[3/3] Terminál potřebuje Plný přístup k disku / Terminal needs Full Disk Access:"
echo "      Nastavení systému → Soukromí a zabezpečení → Plný přístup k disku"
echo "      System Settings → Privacy & Security → Full Disk Access"
echo ""

echo "Spouštím konzoli / launching the console..."
exec uv run --locked --no-dev macos-data-rescue-gui "$@"
