#!/usr/bin/env bash
set -euo pipefail

R="${VM_ROOT:?VM_ROOT is required}"

if [ "${VM_MODE:-fresh}" != "persistent" ]; then
    echo "[LAYERS] Fresh mode, skipping persistence"
    exit 0
fi

test -f "$R/clean"

"$R/venv/bin/python" \
    "$GITHUB_WORKSPACE/scripts/persistence.py" \
    save
