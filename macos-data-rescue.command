#!/bin/bash
set -e

# Change to repository directory
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Launch GUI using uv or available python3
if command -v uv >/dev/null 2>&1; then
    exec uv run macos-data-rescue-gui "$@"
elif [ -x "/tmp/uv-bin/uv" ]; then
    exec /tmp/uv-bin/uv run macos-data-rescue-gui "$@"
elif [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python -m macos_data_rescue.gui "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 -m macos_data_rescue.gui "$@"
else
    echo "Error: Python 3 or uv is required to run macOS Data Rescue GUI." >&2
    exit 1
fi
