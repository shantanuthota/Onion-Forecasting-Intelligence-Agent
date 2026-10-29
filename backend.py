"""One-command backend launcher that couples the API server with the ingestion task.

Usage:  backend.bat   (or: .venv\\Scripts\\python.exe backend.py)

Behavior:
- While this process runs, the Windows scheduled task "OnionUPAgDash6h" is
  ENABLED (fires 00:00 / 06:00 / 12:00 / 18:00 local time).
- When the backend stops (Ctrl+C or a clean exit), the task is DISABLED again.
- A hard kill (power loss, taskkill) may leave the task enabled; the next
  `backend` start re-asserts the state, so it self-heals.
- If UPAG_DASH_ENABLED=0 in .env, the server starts without touching the task.
- Refuses to start (without touching the task) when the port is already in use,
  so a second launch can never disable the task of a live instance.

Forecast note: torch/chronos are optional; graph endpoints degrade with a clear
error if the model is not installed, everything else works.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent
TASK_NAME = "OnionUPAgDash6h"
TASK_CONFIG_SCRIPT = PROJECT / "configure_scheduler_6h.ps1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)


def _env_flag(name: str, default: str = "1") -> bool:
    from dotenv import dotenv_values
    values = dotenv_values(PROJECT / ".env")
    return str(values.get(name, default)).strip().lower() == "1"


def _schtasks(*args: str) -> int:
    result = subprocess.run(["schtasks", *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.returncode


def task_status() -> str:
    """'Enabled' | 'Disabled' | 'missing'."""
    result = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return "missing"
    for line in result.stdout.splitlines():
        if line.strip().startswith("Status:"):
            status = line.split(":", 1)[1].strip()
            return "Disabled" if "Disabled" in status else "Enabled"
    return "Enabled" if result.returncode == 0 else "missing"


def enable_task() -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(TASK_CONFIG_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Could not configure scheduled task {TASK_NAME} "
            f"(exit code {result.returncode}): {detail}"
        )
    return "configured and enabled task"


def disable_task() -> str:
    if task_status() == "missing":
        return "task absent, nothing to disable"
    rc = _schtasks("/change", "/tn", TASK_NAME, "/disable")
    return "disabled task" if rc == 0 else f"disable failed (rc={rc})"


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def smoke_test() -> int:
    """Validate the enable/disable cycle without starting the server."""
    print(f"[{_stamp()}] smoke-test: task is currently: {task_status()}")
    try:
        print(f"[{_stamp()}] smoke-test: {enable_task()} -> now: {task_status()}")
        time.sleep(2)
    finally:
        print(f"[{_stamp()}] smoke-test: {disable_task()} -> now: {task_status()}")
    print(f"[{_stamp()}] smoke-test: cycle OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backend + coupled ingestion-task lifecycle.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--smoke-test", action="store_true", help="test task enable/disable cycle and exit")
    args = parser.parse_args(argv)

    if args.smoke_test:
        return smoke_test()

    if port_in_use(args.host, args.port):
        print(f"[{_stamp()}] Backend already running on {args.host}:{args.port} - nothing to do.")
        return 0

    import uvicorn

    linkage = _env_flag("UPAG_DASH_ENABLED", "1")
    try:
        if linkage:
            message = enable_task()
            print(f"[{_stamp()}] Ingestion automation {message} ({TASK_NAME}: 00:00/06:00/12:00/18:00).")
        else:
            print(f"[{_stamp()}] UPAG_DASH_ENABLED=0 - automation left untouched; starting API only.")
        print(f"[{_stamp()}] Starting API on http://{args.host}:{args.port}  (Ctrl+C to stop)")
        uvicorn.run("main:app", host=args.host, port=args.port, log_level="info")
    finally:
        if linkage:
            print(f"[{_stamp()}] Backend stopped - {disable_task()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
