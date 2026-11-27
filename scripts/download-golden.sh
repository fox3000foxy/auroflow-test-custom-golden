#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/golden.env"

REPO="${GOLDEN_REPO:-fox3000foxy/auroflow-engine}"
TAG="$GOLDEN_TAG"
HASH="$GOLDEN_HASH"
R="${VM_ROOT:?VM_ROOT is required}"
CACHE="${RUNNER_TEMP:-/tmp}/golden-cache"

mkdir -p "$CACHE"

if [[ -f "$CACHE/windows10-golden.qcow2" && -f "$CACHE/vm-state.tar.gz" ]]; then
    if echo "$HASH  $CACHE/windows10-golden.qcow2" | sha256sum -c - &&
       qemu-img check "$CACHE/windows10-golden.qcow2"; then

        ln "$CACHE/windows10-golden.qcow2" "$R/windows10-golden.qcow2"
        tar -xzf "$CACHE/vm-state.tar.gz" -C "$R"
        chmod 444 "$R/windows10-golden.qcow2"

        echo "[GOLDEN] Restored from cache"
        exit 0
    fi

    echo "[GOLDEN] Invalid cache, downloading again"
    rm -f "$CACHE/windows10-golden.qcow2" "$CACHE/vm-state.tar.gz"
fi

mkdir -p "$R/download"
cd "$R/download"

URL="https://github.com/$REPO/releases/download/$TAG"

echo "[GOLDEN] Downloading $REPO@$TAG"

curl -fsSL --retry 3 \
    "https://api.github.com/repos/$REPO/releases/tags/$TAG" \
    -o release.json

python3 - <<'PY' > parts.txt
import json
import re

with open("release.json") as f:
    release = json.load(f)

parts = sorted(
    asset["name"]
    for asset in release["assets"]
    if re.fullmatch(
        r"windows10-golden\.qcow2\.part-\d{2}",
        asset["name"]
    )
)

if not parts:
    raise RuntimeError("No Golden Image parts found")

expected = [
    f"windows10-golden.qcow2.part-{i:02d}"
    for i in range(len(parts))
]

if parts != expected:
    raise RuntimeError("Missing or invalid image parts")

print("\n".join(parts))
PY

mapfile -t parts < parts.txt

echo "[GOLDEN] Found ${#parts[@]} parts"

pids=()

for part in "${parts[@]}"; do
    curl -fL --retry 3 "$URL/$part" -o "$part" &
    pids+=("$!")
done

failed=0

for pid in "${pids[@]}"; do
    wait "$pid" || failed=1
done

test "$failed" -eq 0

curl -fsSL --retry 3 \
    "$URL/windows10-golden.qcow2.sha256" \
    -o windows10-golden.qcow2.sha256

curl -fsSL --retry 3 \
    "$URL/vm-state.tar.gz" \
    -o vm-state.tar.gz

cat "${parts[@]}" > windows10-golden.qcow2

sha256sum -c windows10-golden.qcow2.sha256
echo "$HASH  windows10-golden.qcow2" | sha256sum -c -

qemu-img check windows10-golden.qcow2

mv windows10-golden.qcow2 "$CACHE/"
cp vm-state.tar.gz "$CACHE/"

ln "$CACHE/windows10-golden.qcow2" "$R/windows10-golden.qcow2"
tar -xzf vm-state.tar.gz -C "$R"

chmod 444 "$R/windows10-golden.qcow2"

cd "$R"
rm -rf "$R/download"

echo "[GOLDEN] Image ready"
