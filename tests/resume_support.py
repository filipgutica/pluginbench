"""A deterministic fake coding backend, shared by a resume test and the child process it kills.

The child runs a study and SIGKILLs itself at one named boundary, so the run directory is left
exactly as a killed run leaves it: no except or finally block runs.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from native_support import (
    execution as native_execution,
    runtime_identity,
    synthetic_contract,
    task_entry,
)
from pluginbench import baselines, harbor_backend, ledger, study, submissions
from pluginbench.datasets import native_identity, portable_native_identity

# Where the child kills itself, relative to its Nth native call or Nth completed attempt.
BOUNDARIES = {
    "before-receipt": "before the invocation receipt is written",
    "during-call": "during the native call, after its invocation receipt",
    "after-execution": "after execution.json, before the record receipt",
    "after-record": "after the record receipt, before the ledger marks it complete",
    "after-ledger": "after the ledger marks it complete, before run.json is rewritten",
    "two-in-flight": "while two native calls run at once (jobs=2)",
    "after-plan-write": "right after a resume rewrites plan.json",
    "ledger-insert": "after a new ledger has its schema, before its attempt rows",
    "before-publish": "after the ledger marks a vanilla attempt complete, before it is cached",
}


def recipe(
    directory: Path, *, tasks: int, treatments: int, repeats: int, jobs: int, seed: int = 42
) -> Path:
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
                "seed": seed,
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
    identity = runtime_identity()
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: identity)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    lock = threading.Lock()
    pair = threading.Barrier(2)
    counts = {"receipt": 0, "verify": 0, "complete": 0, "publish": 0}

    def counted(name: str) -> int:
        with lock:
            counts[name] += 1
            return counts[name]

    def assert_runtime(expected: dict[str, object], **kwargs: object) -> dict[str, object]:
        return expected

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
        return native_execution(area, kwargs["protocol"]["native_task_identity"], reward=1)

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

    write = study._write

    def write_file(path: Path, value: object) -> None:
        # An invocation receipt is `invocations/<attempt>/<stage>.json`.
        receipt = path.parent.parent.name == "invocations"
        if boundary == "before-receipt" and receipt and counted("receipt") == at:
            _kill()
        write(path, value)
        if boundary == "after-plan-write" and path.name == "plan.json":
            _kill()

    class _KilledBeforeRows:
        """A real connection that dies when the ledger inserts its attempt rows."""

        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __enter__(self) -> Any:
            self._connection.__enter__()
            return self

        def __exit__(self, *details: Any) -> Any:
            return self._connection.__exit__(*details)

        def executemany(self, *args: Any) -> None:
            _kill()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

    publish = baselines.BaselineCache.publish

    def publish_attempt(self: baselines.BaselineCache, record: dict[str, Any]) -> None:
        if boundary == "before-publish" and counted("publish") == at:
            _kill()
        publish(self, record)

    if boundary in {"after-plan-write", "before-receipt"}:
        monkeypatch.setattr(study, "_write", write_file)
    if boundary == "ledger-insert":
        killing = types.SimpleNamespace(
            connect=lambda path: _KilledBeforeRows(sqlite3.connect(path))
        )
        monkeypatch.setattr(ledger, "sqlite3", killing)
    monkeypatch.setattr(baselines.BaselineCache, "publish", publish_attempt)
    monkeypatch.setattr(harbor_backend, "assert_runtime", assert_runtime)
    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    monkeypatch.setattr(study, "_verify_snapshot", verify_snapshot)
    monkeypatch.setattr(ledger.Ledger, "complete", ledger_complete)


def vanilla_identity(resolved: study.ResolvedStudy) -> dict[str, object]:
    """A vanilla cache identity the fake attempts above are eligible for."""
    return {
        "isolation": runtime_identity(),
        "harness_version": "1.2.3",
        "timeout_seconds": resolved.recipe.timeout_seconds,
        "task_environment": {
            task.task_id: portable_native_identity(native_identity(task)) for task in resolved.tasks
        },
    }


def child() -> None:
    """argv: recipe, output, boundary, at[, cache]. A fresh study that kills itself at `boundary`."""
    recipe_path, output, boundary, at = sys.argv[1:5]
    install(pytest.MonkeyPatch(), [], boundary=boundary, at=int(at))
    resolved = study.load_study(Path(recipe_path))
    cache = Path(sys.argv[5]) if len(sys.argv) > 5 else None
    study.run_study(
        resolved,
        Path(output),
        baseline_cache=cache,
        baseline_identity=vanilla_identity(resolved) if cache else None,
    )
    raise SystemExit("the study finished without reaching its kill boundary")


def child_resume() -> None:
    """argv: run, boundary. Resumes a study and kills itself at `boundary`."""
    root, boundary = sys.argv[1:3]
    install(pytest.MonkeyPatch(), [], boundary=boundary, at=1)
    study.resume_study(Path(root))
    raise SystemExit("the resume finished without reaching its kill boundary")
