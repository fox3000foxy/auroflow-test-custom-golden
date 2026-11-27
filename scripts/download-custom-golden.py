#!/usr/bin/env python3

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid

from pathlib import Path
from urllib.parse import urlsplit, unquote
from huggingface_hub import HfApi, hf_hub_download

root = Path(os.environ["VM_ROOT"])
url = urlsplit(os.environ["GOLDEN_URL"])
expected = os.environ["GOLDEN_SHA256"].strip().lower()
token = os.environ.get("HF_TOKEN") or False

if (
    url.scheme != "https"
    or url.netloc != "huggingface.co"
    or url.query
    or url.fragment
):
    raise RuntimeError("Invalid Hugging Face URL")

if not re.fullmatch(r"[a-f0-9]{64}", expected):
    raise RuntimeError("Invalid Golden Image SHA-256")

parts = unquote(url.path).strip("/").split("/")

if (
    len(parts) < 6
    or parts[0] != "datasets"
    or parts[3] != "resolve"
):
    raise RuntimeError("Expected a Hugging Face dataset resolve URL")

repo = "/".join(parts[1:3])
revision = parts[4]
filename = "/".join(parts[5:])

if (
    not filename.lower().endswith(".qcow2")
    or any(p in ("", ".", "..") for p in parts)
):
    raise RuntimeError("Invalid QCOW2 filename")

api = HfApi(token=token)
revision = api.repo_info(
    repo,
    repo_type="dataset",
    revision=revision,
).sha

print("[GOLDEN] Downloading custom image", flush=True)

downloaded = Path(hf_hub_download(
    repo_id=repo,
    repo_type="dataset",
    filename=filename,
    revision=revision,
    token=token,
    local_dir=root / "custom-download",
))

with downloaded.open("rb") as f:
    actual = hashlib.file_digest(f, "sha256").hexdigest()

if actual != expected:
    raise RuntimeError("Custom Golden Image checksum mismatch")

info = json.loads(subprocess.check_output([
    "qemu-img", "info", "-f", "qcow2",
    "--output=json", str(downloaded)
]))

specific = info.get("format-specific", {}).get("data", {})

if (
    info.get("format") != "qcow2"
    or info.get("backing-filename")
    or specific.get("data-file")
    or info.get("encrypted")
):
    raise RuntimeError("Expected a standalone QCOW2 image")

subprocess.run([
    "qemu-img", "check", "-f", "qcow2",
    str(downloaded)
], check=True)

target = root / "windows10-golden.qcow2"
downloaded.rename(target)
target.chmod(0o444)

firmware = Path("/usr/share/OVMF/OVMF_VARS_4M.ms.fd")
shutil.copyfile(firmware, root / "OVMF_VARS.fd")

(root / "tpm").mkdir(exist_ok=True)

identity = uuid.uuid5(uuid.NAMESPACE_URL, expected)
(root / "uuid").write_text(str(identity) + "\n")

mac = "52:54:00:" + ":".join(
    expected[i:i+2] for i in range(0, 6, 2)
)
(root / "mac").write_text(mac + "\n")

shutil.rmtree(root / "custom-download")

print("[GOLDEN] Custom image ready", flush=True)
