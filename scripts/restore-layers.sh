#!/usr/bin/env bash
set -euo pipefail

R="${VM_ROOT:?VM_ROOT is required}"
MODE="${VM_MODE:-fresh}"

cd "$R"

case "$MODE" in
    fresh)
        echo "[LAYERS] Creating fresh overlay"

        qemu-img create \
            -f qcow2 \
            -F qcow2 \
            -b windows10-golden.qcow2 \
            disk.qcow2
        ;;

    persistent)
        echo "[LAYERS] Restoring persistent chain"

        "$R/venv/bin/python" \
            "$GITHUB_WORKSPACE/scripts/persistence.py" \
            restore
        ;;

    *)
        echo "[ERROR] Unknown VM mode: $MODE"
        exit 1
        ;;
esac

qemu-img info --backing-chain disk.qcow2
