"""A deterministic fake coding backend, shared by a resume test and the child process it kills.

The child runs a study and SIGKILLs itself at one named boundary, so the run directory is left
exactly as a killed run leaves it: no except or finally block runs.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from native_support import execution as native_execution, synthetic_contract, task_entry
from pluginbench import harbor_backend, ledger, study, submissions

# Where the child kills itself, relative to its Nth native call or Nth completed attempt.
BOUNDARIES = {
    "before-receipt": "before the invocation receipt is written",
    "during-call": "during the native call, after its invocation receipt",
    "after-execution": "after execution.json, before the record receipt",
    "after-record": "after the record receipt, before the ledger marks it complete",
    "after-ledger": "after the ledger marks it complete, before run.json is rewritten",
    "two-in-flight": "while two native calls run at once (jobs=2)",
}


def recipe(directory: Path, *, tasks: int, treatments: int, repeats: int, jobs: int) -> Path:
    suite = directory / "suite"
    suite.mkdir(parents=True)
    entries = [task_entry(suite, f"task-{index}") for index in range(tasks)]
    (suite / "tasks.yaml").write_text(yaml.safe_dump({"schema_version": 1, "tasks": entries}))
    guidance = {}
    for index in range(treatments):
        (directory / f"guidance-{index}.md").write_text(f"Follow rule {index}.\n")
        guidance[f"arm-{index}"] = [f"guidance-{index}.md"]
    path = directory / "recipe.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "Resume fixture",
                "tasks": "suite/tasks.yaml",
                "guidance": guidance,
                "harness_version": "1.2.3",
                "repeats": repeats,
                "jobs": jobs,
            }
        )
    )
    return path


def _kill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def install(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    boundary: str | None = None,
    at: int = 0,
) -> None:
    """Fake every Harbor entry point a study reaches; record each native call's attempt ID."""
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)
    identity = {"resources": {"cpus": 1, "memory_mb": 2048}, "offline": True}
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: identity)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    lock = threading.Lock()
    pair = threading.Barrier(2)
    counts = {"receipt": 0, "verify": 0, "complete": 0}

    def counted(name: str) -> int:
        with lock:
            counts[name] += 1
            return counts[name]

    def assert_runtime(expected: dict[str, object]) -> None:
        # study calls this just before it writes each invocation receipt.
        if boundary == "before-receipt" and counted("receipt") == at:
            _kill()

    def invoke(**kwargs: Any) -> dict[str, Any]:
        with lock:
            calls.append(str(kwargs["entry"]["id"]))
            ordinal = len(calls)
        if boundary == "during-call" and ordinal == at:
            _kill()
        if boundary == "two-in-flight" and ordinal in (at, at + 1):
            pair.wait(timeout=10)
            _kill()
        area = kwargs["area"]
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        result = native_execution(area, kwargs["protocol"]["native_task_identity"], reward=1)
        result["startup"] = [{"type": "session_meta", "payload": {"cli_version": "observed-1"}}]
        return result

    verify = study._verify_snapshot

    def verify_snapshot(**kwargs: Any) -> dict[str, Any]:
        if boundary == "after-execution" and counted("verify") == at:
            _kill()
        return verify(**kwargs)

    complete = ledger.Ledger.complete

    def ledger_complete(self: ledger.Ledger, *args: Any, **kwargs: Any) -> None:
        ordinal = counted("complete")
        if boundary == "after-record" and ordinal == at:
            _kill()
        complete(self, *args, **kwargs)
        if boundary == "after-ledger" and ordinal == at:
            _kill()

    monkeypatch.setattr(harbor_backend, "assert_runtime", assert_runtime)
    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    monkeypatch.setattr(study, "_verify_snapshot", verify_snapshot)
    monkeypatch.setattr(ledger.Ledger, "complete", ledger_complete)


def child() -> None:
    """argv: recipe, output, boundary, at. Runs a fresh study that kills itself at `boundary`."""
    recipe_path, output, boundary, at = sys.argv[1:5]
    install(pytest.MonkeyPatch(), [], boundary=boundary, at=int(at))
    study.run_study(study.load_study(Path(recipe_path)), Path(output))
    raise SystemExit("the study finished without reaching its kill boundary")
