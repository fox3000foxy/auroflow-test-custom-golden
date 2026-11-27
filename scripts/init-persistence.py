#!/usr/bin/env python3

import os
from huggingface_hub import HfApi


def export_env(key, value):
    with open(os.environ["GITHUB_ENV"], "a") as f:
        f.write(f"{key}={value}\n")


token = os.environ.get("HF_TOKEN")

if not token:
    print("[HF] No token configured, using fresh mode")
    export_env("VM_MODE", "fresh")
    raise SystemExit(0)

api = HfApi(token=token)
username = api.whoami()["name"]

github_repo = os.environ["GITHUB_REPOSITORY"].split("/")[-1]

repo = (
    os.environ.get("HF_REPO")
    or f"{username}/{github_repo}-layers"
)

api.create_repo(
    repo_id=repo,
    repo_type="dataset",
    private=True,
    exist_ok=True
)

info = api.repo_info(repo, repo_type="dataset")

if not info.private:
    raise RuntimeError(
        "Persistence dataset must be private"
    )

export_env("HF_REPO", repo)
export_env("VM_MODE", "persistent")

print(f"[HF] Private dataset ready: {repo}")
