import json
import base64
import os
import sys
import re
import subprocess
import tempfile
from pathlib import Path

repo = os.environ["GITHUB_REPOSITORY"]
run_id = os.environ["GITHUB_RUN_ID"]


def gh(*args):
    return subprocess.check_output(
        ["gh", *args], text=True
    ).strip()


def publish(endpoint):
    endpoint = endpoint.removeprefix("tcp://")

    if not re.fullmatch(r"[a-zA-Z0-9.-]+:[0-9]+", endpoint):
        raise RuntimeError("Invalid RDP endpoint")

    with tempfile.TemporaryDirectory() as tmp:
        def git(*args):
            subprocess.run(
                ["git", "-C", tmp, *args],
                check=True
            )

        git("init", "-b", "golden-control")
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com")

        metadata = {}
        try:
            metadata = json.loads(Path(os.environ["VM_ROOT"], "vm-metadata.json").read_text())
        except (OSError, ValueError, KeyError):
            pass

        public_key = os.environ.get("AUROFLOW_PUBLIC_KEY", "").strip()

        session = {
            "run_id": run_id,
            "schema_version": 3,
            "vm": metadata
        }

        if public_key:
            from nacl.public import PublicKey, SealedBox

            key = PublicKey(base64.b64decode(public_key, validate=True))

            payload = json.dumps({
                "repository": repo.lower(),
                "run_id": run_id,
                "endpoint": endpoint
            }).encode("utf-8")

            encrypted = SealedBox(key).encrypt(payload)

            session["encrypted_endpoint"] = base64.b64encode(
                encrypted
            ).decode("ascii")

        else:
            # Compatibility with existing workspaces.
            session["schema_version"] = 2
            session["endpoint"] = endpoint

        Path(tmp, "session.json").write_text(
            json.dumps(session, indent=2) + "\\n"
        )

        git("add", "session.json")
        git("commit", "-m", f"Session {run_id}")
        git("remote", "add", "origin",
            f"https://github.com/{repo}.git")

        subprocess.run(
            ["gh", "auth", "setup-git"],
            check=True
        )

        git("push", "--force", "origin", "golden-control")

    print("[PANEL] Session published", flush=True)


def shutdown_requested():
    issues = json.loads(gh(
        "api", "--paginate",
        f"repos/{repo}/issues?state=open&per_page=100",
        "--slurp"
    ))

    for issue in (item for page in issues for item in page):
        if issue.get("title") != f"Golden shutdown {run_id}":
            continue

        if issue.get("author_association") != "OWNER":
            continue

        if "pull_request" in issue:
            continue

        print(issue["number"])
        return


if __name__ == "__main__":
    if sys.argv[1] == "publish":
        publish(sys.argv[2])
    elif sys.argv[1] == "shutdown":
        shutdown_requested()
    else:
        raise RuntimeError("Unknown command")
