#!/bin/bash
set -e

# Change to repository directory
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/tmp/uv-bin:$PATH"

# If uv is not available, install it automatically without asking questions
if ! command -v uv >/dev/null 2>&1 && [ ! -x ".venv/bin/python" ]; then
    echo "Pripravuji prostredi pro zachranu dat (instalace uv)..."
    mkdir -p "$HOME/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Launch GUI using uv, virtual environment, or python3
if command -v uv >/dev/null 2>&1; then
    exec uv run macos-data-rescue-gui "$@"
elif [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python -m macos_data_rescue.gui "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 -m macos_data_rescue.gui "$@"
else
    echo "Chyba: Nepodarilo se spustit prostredi Python 3 / uv." >&2
    exit 1
fi
