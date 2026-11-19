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


class Unexpected(Exception):
    """An exception no caller handles by type."""


def fail(arguments: dict[str, Any]) -> Any:
    message, kind = arguments["message"], arguments.get("kind", "ValueError")
    if kind == "TimeoutExpired":
        raise subprocess.TimeoutExpired(["worker", message], 7)
    raise Unexpected(message) if kind == "Unexpected" else ValueError(message)


def chatty(arguments: dict[str, Any]) -> Any:
    for text in arguments["lines"]:
        invocation_runtime.report(text)
    return len(arguments["lines"])


def nested_collect(arguments: dict[str, Any]) -> Any:
    """A collect inside this operation, as a preflight's readiness probe runs one, with the
    collector's own command stubbed."""
    from pluginbench import collectors

    setattr(collectors, "_run_collector", lambda *args: ({"collected": True}, "test", "true"))
    collector = collectors.Collector(
        kind="coverage", config=Path(arguments["config"]), command=["true"], tool_version="test"
    )
    report, _, _ = collectors._contained(
        Path(arguments["root"]), collector, Path(arguments["destination"]), None
    )
    return report


def recorded(arguments: dict[str, Any]) -> Any:
    """The Docker daemon this operation may create containers on."""
    try:
        return invocation_runtime.container_invocation(arguments["runtime"])
    except ValueError as exc:
        return str(exc)


def descriptors(arguments: dict[str, Any]) -> Any:
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            found.append(os.readlink(f"/proc/self/fd/{name}"))
        except OSError:
            continue
    return found


def _fake_reference(mode: str) -> None:
    """Run the real `reference` operation with its measurement stubbed, as PB_FAKE_REFERENCE
    says: return no repeat, fail, or hang past its deadline. No container starts."""
    from pluginbench import reference_mutation

    def measure_repeats(**arguments: Any) -> dict[str, object]:
        if mode == "fail":
            raise ValueError("the reference measurement failed")
        if mode == "hang":
            hang({"pid_file": str(arguments["destination"] / "escaped")})
        return {}

    setattr(reference_mutation, "measure_repeats", measure_repeats)


if mode := os.environ.get("PB_FAKE_REFERENCE"):
    _fake_reference(mode)


invocation_runtime.HANDLERS.update(
    {name: f"sitecustomize:{name}" for name in ("echo", "escape", "hang", "fail", "chatty", "nested_collect", "recorded", "descriptors")}
)
