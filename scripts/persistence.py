#!/usr/bin/env python3

import os
import sys
import json
import uuid
import shutil
import hashlib
import pathlib
import subprocess

from huggingface_hub import (
    HfApi,
    hf_hub_download,
    CommitOperationAdd
)

root = pathlib.Path(os.environ["VM_ROOT"])
repo = os.environ["HF_REPO"]
token = os.environ["HF_TOKEN"]

api = HfApi(token=token)
opts = {"repo_type": "dataset"}

base = root / "windows10-golden.qcow2"
disk = root / "disk.qcow2"
nvram = root / "OVMF_VARS.fd"
tpm = root / "tpm/tpm2-00.permall"

layers = root / "layers"
layers.mkdir(exist_ok=True)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def info(path):
    return json.loads(subprocess.check_output([
        "qemu-img", "info", "--output=json", str(path)
    ]))


def download(path, revision):
    return pathlib.Path(hf_hub_download(
        repo_id=repo,
        filename=path,
        revision=revision,
        local_dir=root,
        token=token,
        **opts
    ))


def restore():
    metadata = api.repo_info(repo, **opts)

    if not metadata.private:
        raise RuntimeError("Persistence dataset must be private")

    revision = metadata.sha
    paths = set(api.list_repo_files(
        repo, revision=revision, **opts
    ))

    base_hash = sha(base)

    state = {
        "base_sha256": base_hash,
        "chain": []
    }

    if "latest.json" in paths:
        state = json.loads(
            download("latest.json", revision).read_text()
        )

    if state["base_sha256"] != base_hash:
        raise RuntimeError("Golden Image checksum mismatch")

    parent = base

    for item in state["chain"]:
        ident = item["id"]

        if uuid.UUID(ident).hex != ident:
            raise RuntimeError("Invalid layer identifier")

        prefix = f"layers/{ident}"

        manifest = json.loads(
            download(prefix + "/manifest.json", revision)
            .read_text()
        )

        restored = {}

        for name in (
            "disk.qcow2",
            "OVMF_VARS.fd",
            "tpm2-00.permall"
        ):
            path = download(prefix + "/" + name, revision)

            if sha(path) != manifest[name]:
                raise RuntimeError(f"Checksum mismatch: {path}")

            restored[name] = path

        layer = restored["disk.qcow2"]

        if sha(layer) != item["sha256"]:
            raise RuntimeError("Layer chain checksum mismatch")

        expected = os.path.relpath(parent, layer.parent)

        if info(layer).get("backing-filename") != expected:
            raise RuntimeError("Invalid backing file")

        run("qemu-img", "check", str(layer))

        shutil.copyfile(restored["OVMF_VARS.fd"], nvram)
        shutil.copyfile(restored["tpm2-00.permall"], tpm)

        parent = layer

        print(f"[HF] Restored layer {ident}", flush=True)

    run(
        "qemu-img", "create",
        "-f", "qcow2",
        "-F", "qcow2",
        "-b", os.path.relpath(parent, root),
        str(disk),
        cwd=root
    )

    write(root / "session.json", {
        "revision": revision,
        "state": state,
        "id": uuid.uuid4().hex
    })

    print("[HF] Persistent overlay ready", flush=True)


def save():
    if not (root / "clean").exists():
        raise RuntimeError("No confirmed clean shutdown")

    session = json.loads(
        (root / "session.json").read_text()
    )

    ident = session["id"]
    out = layers / ident
    out.mkdir()

    target = out / "disk.qcow2"

    backing = (
        root / info(disk)["backing-filename"]
    ).resolve()

    relative = os.path.relpath(backing, out)

    run("qemu-img", "check", str(disk))

    run(
        "qemu-img", "convert",
        "-O", "qcow2",
        "-c",
        "-o", "compression_type=zstd",
        "-B", relative,
        "-F", "qcow2",
        str(disk),
        str(target),
        cwd=out
    )

    run("qemu-img", "check", str(target))
    run("qemu-img", "compare", str(disk), str(target))

    shutil.copyfile(nvram, out / "OVMF_VARS.fd")
    shutil.copyfile(tpm, out / "tpm2-00.permall")

    names = [
        "disk.qcow2",
        "OVMF_VARS.fd",
        "tpm2-00.permall"
    ]

    hashes = {
        name: sha(out / name)
        for name in names
    }

    write(out / "manifest.json", hashes)

    state = session["state"]

    state["chain"].append({
        "id": ident,
        "sha256": hashes["disk.qcow2"]
    })

    write(root / "latest.json", state)

    operations = [
        CommitOperationAdd(
            path_in_repo=f"layers/{ident}/{name}",
            path_or_fileobj=str(out / name)
        )
        for name in names + ["manifest.json"]
    ]

    operations.append(CommitOperationAdd(
        path_in_repo="latest.json",
        path_or_fileobj=str(root / "latest.json")
    ))

    if not api.repo_info(repo, **opts).private:
        raise RuntimeError("Persistence dataset must be private")

    api.create_commit(
        repo_id=repo,
        operations=operations,
        commit_message=f"Save VM layer {ident}",
        parent_commit=session["revision"],
        **opts
    )

    print(f"[HF] Published layer {ident}", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "restore":
        restore()
    elif sys.argv[1] == "save":
        save()
    else:
        raise RuntimeError("Unknown persistence operation")
