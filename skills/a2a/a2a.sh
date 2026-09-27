#!/bin/bash
# Shim for skills/a2a. Resolves its own real path through the discovery symlink,
# so it can be called from anywhere inside the Bridge.
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
exec python3 "$here/scripts/a2a.py" "$@"
