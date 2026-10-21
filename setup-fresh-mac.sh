#!/bin/bash
set -e

# ==============================================================================
# macOS Data Rescue — Fresh Mac Setup & Launcher Script
#
# Sets up everything needed on a clean / factory-reset Mac:
# 1. Installs 'uv' (standalone fast Python manager, no sudo or Xcode needed)
# 2. Automatically provisions Python 3.12+ environment with zero runtime dependencies
# 3. Reminds technician about macOS Full Disk Access (Plný přístup k disku)
# 4. Launches the Graphical User Interface (Technician Console)
# ==============================================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=================================================================="
echo " macOS Data Rescue — Automatická příprava a spuštění na čistém Macu"
echo "=================================================================="
echo ""

# 1. Check / Install uv
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/tmp/uv-bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "[1/3] Instaluji nástroj 'uv' (nevyžaduje heslo správce ani Xcode)..."
    mkdir -p "$HOME/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "[1/3] Nástroj 'uv' je již připraven: $(command -v uv)"
fi

# 2. Provision Python environment
echo "[2/3] Příprava běhového prostředí Pythonu a kontrola závislostí..."
uv sync

# 3. Full Disk Access check / reminder
echo "[3/3] Důležité upozornění pro záchranu dat na macOS:"
echo "      Ujistěte se, že aplikace Terminál má povolen 'Plný přístup k disku'"
echo "      (Nastavení systému -> Soukromí a zabezpečení -> Plný přístup k disku)."
echo ""

# 4. Launch GUI
echo "Spouštím grafické rozhraní (Technician Console)..."
exec uv run macos-data-rescue-gui "$@"
