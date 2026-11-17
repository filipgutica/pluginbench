"""Test-only operation handlers, registered in every interpreter that has this directory on its
PYTHONPATH: the owner that validates a request and the entry point inside the scope.

Each handler takes the request's JSON arguments, as production handlers do.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from pluginbench import invocation_runtime


def echo(arguments: dict[str, Any]) -> Any:
    if "marker" in arguments:
        Path(arguments["marker"]).write_text(str(os.getpid()))
    return arguments.get("value")


def _escape(path: str) -> None:
    """Start a `sleep` that leaves this process group with setsid, and save its PID."""
    child = subprocess.Popen(
        ["sleep", "300"], start_new_session=True, stdout=subprocess.DEVNULL
    )
    Path(path).write_text(str(child.pid))


def escape(arguments: dict[str, Any]) -> Any:
    _escape(arguments["pid_file"])
    return "done"


def hang(arguments: dict[str, Any]) -> Any:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _escape(arguments["pid_file"])
    while True:
        time.sleep(1)


def fail(arguments: dict[str, Any]) -> Any:
    raise ValueError(arguments["message"])


def descriptors(arguments: dict[str, Any]) -> Any:
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            found.append(os.readlink(f"/proc/self/fd/{name}"))
        except OSError:
            continue
    return found


invocation_runtime.HANDLERS.update(
    {name: f"sitecustomize:{name}" for name in ("echo", "escape", "hang", "fail", "descriptors")}
)
