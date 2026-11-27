import json
import os
import socket
import subprocess
import time
from pathlib import Path

root = os.environ["VM_ROOT"]
deadline = int(os.environ["DEADLINE"])

s = socket.socket(socket.AF_UNIX)
s.settimeout(10)
s.connect(root + "/qmp.sock")

f = s.makefile("rwb")
json.loads(f.readline())

def cmd(name):
    f.write(json.dumps({"execute": name}).encode() + b"\n")
    f.flush()

    while True:
        line = f.readline()

        if not line:
            raise RuntimeError("QMP disconnected")

        response = json.loads(line)

        if "error" in response:
            raise RuntimeError(response["error"])

        if "return" in response:
            return response["return"]

cmd("qmp_capabilities")

requested = False
next_poll = 0
shutdown_deadline = deadline + 120

print("[VM] Waiting for guest shutdown", flush=True)

while True:
    status = cmd("query-status")["status"]

    if status == "shutdown":
        qemu_pid = int(Path(root, "qemu.pid").read_text())

        cmd("quit")
        f.close()
        s.close()

        for _ in range(300):
            try:
                os.kill(qemu_pid, 0)
                stat = Path(f"/proc/{qemu_pid}/stat").read_text()
                running = stat.rsplit(") ", 1)[1][0] != "Z"
            except (ProcessLookupError, FileNotFoundError):
                running = False

            if not running and Path(root, "tpm-stopped").exists():
                Path(root, "clean").touch()
                print("[VM] Clean QEMU and TPM shutdown confirmed",
                      flush=True)
                break

            time.sleep(0.1)
        else:
            raise RuntimeError("QEMU or TPM shutdown timeout")

        break

    if not requested and time.monotonic() >= next_poll:
        next_poll = time.monotonic() + 15

        if os.environ.get("GH_TOKEN"):
            try:
                result = subprocess.check_output(
                    ["python3", str(Path(__file__).with_name("panel-control.py")),
                     "shutdown"],
                    text=True,
                    timeout=10
                ).strip()

                if result:
                    print("[PANEL] Remote shutdown requested", flush=True)
                    cmd("system_powerdown")
                    requested = True
                    shutdown_deadline = time.time() + 120

            except (subprocess.SubprocessError, OSError):
                print("[PANEL] Control polling failed", flush=True)

    if time.time() >= deadline and not requested:
        print("[VM] Requesting ACPI shutdown", flush=True)
        cmd("system_powerdown")
        requested = True

    if time.time() >= shutdown_deadline:
        raise RuntimeError("Guest shutdown timeout")

    time.sleep(2)
