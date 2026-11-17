"""Operations run in the test's own process, so a test's fakes of what the handlers call run.

Production runs each operation in a systemd scope (`invocation_runtime.run_operation`); the
fake and real scope tests mark themselves `real_operations` and use that instead.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from typing import Any

import pytest

from pluginbench import invocation_runtime
from pluginbench.invocation_runtime import OperationRequest

# The operation each thread runs in place, as the entry point would in its scope.
_operations = threading.local()
_current = invocation_runtime.current


def run_in_place(
    request: OperationRequest,
    *,
    started: Callable[[int], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Any:
    """`invocation_runtime.run_operation` without a scope: the arguments and the value go
    through JSON, and the exception comes back as the owner raises it. Before the handler,
    `started` gets the PID of a stand-in for the scope's entry point, which has exited by the
    time any recovery checks it, and `progress` gets what the handler reports."""
    if started is not None:
        stand_in = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            started(stand_in.pid)
        finally:
            stand_in.kill()
            stand_in.wait()
    previous = getattr(_operations, "current", None), getattr(_operations, "progress", None)
    _operations.current, _operations.progress = request.spec.id, progress
    try:
        value = invocation_runtime.handler(request.operation)(
            json.loads(json.dumps(request.arguments))
        )
    except Exception as exc:
        raise invocation_runtime.rebuild(invocation_runtime.failure(exc)) from exc
    finally:
        _operations.current, _operations.progress = previous
    return json.loads(json.dumps(value))


def _report(text: str) -> None:
    if (progress := getattr(_operations, "progress", None)) is not None:
        progress(text)


def in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every operation in place, on a host that can contain them."""
    monkeypatch.setattr(invocation_runtime, "run_operation", run_in_place)
    monkeypatch.setattr(invocation_runtime, "require_user_manager", lambda: "systemd-run")
    monkeypatch.setattr(
        invocation_runtime, "current", lambda: getattr(_operations, "current", None) or _current()
    )
    monkeypatch.setattr(invocation_runtime, "report", _report)
