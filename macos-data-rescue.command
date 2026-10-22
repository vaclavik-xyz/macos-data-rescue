#!/bin/bash
# Finder double-click launcher: same setup and launch path as setup-fresh-mac.sh.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/setup-fresh-mac.sh" "$@"
