"""Host recovery: verified cleanup before any crashed row closes, a barrier every command honours,
and an unknown lock holder that turns `reconciling` after 30 seconds without being touched.

Cleanup checks are fakes here; nothing starts a container, a systemd unit or a model.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import pluginbench
import resume_support
from pluginbench import cli, control_store, judge, run_lock, study
from pluginbench.control_store import CleanupBlocked, ControlStore, OwnerWatch
from pluginbench.ledger import LEDGER_NAME, Ledger
from test_resume import _killed_run

_SOURCE = str(Path(pluginbench.__file__).parents[1])


def _host(state: Path) -> control_store.HostState:
    with ControlStore.open(state) as store:
        return store.host()


def _still_running(operation: Mapping[str, Any]) -> dict[str, Any]:
    raise CleanupBlocked(f"process 7 of {operation['stage']} invocation still runs")


def _crashed(tmp_path: Path) -> Path:
    """A one-slot run killed during its coding call: one open coding invocation."""
    return _killed_run(tmp_path, boundary="during-call", at=1, tasks=1, treatments=0, repeats=1)


def _rows(root: Path) -> list[dict[str, Any]]:
    ledger = Ledger.read(root / LEDGER_NAME)
    try:
        return ledger.invocations()
    finally:
        ledger.close()


def _owner(root: Path) -> run_lock.ExecutionOwner:
    return run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root))


def test_unverified_cleanup_keeps_crashed_rows_open_and_blocks_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    root = _crashed(tmp_path)
    [crashed] = _rows(root)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    monkeypatch.setattr(control_store, "verify_invocation", _still_running)
    with control_store.execute(_owner(root)):
        with pytest.raises(CleanupBlocked, match="process 7 of coding invocation still runs"):
            study.resume_study(root)
    # No row closed, no new intent, and the barrier names the crashed invocation.
    assert calls == [] and _rows(root) == [crashed]
    assert _host(isolated_host_state).barrier == control_store.HOST_CLEANUP
    with ControlStore.open(isolated_host_state) as store:
        assert [row["invocation_id"] for row in store.unclean()] == [crashed["id"]]

    # Once cleanup verifies, it does so before saved output is reconciled, and only then is the
    # crashed row marked unresolved.
    trace: list[str] = []

    def verified(operation: Mapping[str, Any]) -> dict[str, Any]:
        trace.append("verify")
        return {"invocation_id": operation["invocation_id"]}

    reconcile = study._reconcile

    def reconciled(*args: Any) -> Any:
        trace.append("reconcile")
        return reconcile(*args)

    resolve = Ledger.resolve_crashed

    def resolved(self: Ledger, stage: str, *, cleaned: frozenset[str]) -> int:
        trace.append(f"resolve {stage}")
        return resolve(self, stage, cleaned=cleaned)

    monkeypatch.setattr(control_store, "verify_invocation", verified)
    monkeypatch.setattr(study, "_reconcile", reconciled)
    monkeypatch.setattr(Ledger, "resolve_crashed", resolved)
    with control_store.execute(_owner(root)):
        # Taking the lock verified the barrier's invocation again and lifted it.
        assert trace == ["verify"] and _host(isolated_host_state).barrier is None
        study.resume_study(root)
    assert trace[:5] == ["verify", "verify", "reconcile", "resolve coding", "resolve grading"]
    [resolved_row, *new] = _rows(root)
    assert resolved_row["id"] == crashed["id"] and resolved_row["state"] == "unresolved"
    assert len(calls) == 1 and len(new) == 1


def test_sweeping_crashed_rows_needs_the_host_execution_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _crashed(tmp_path)
    resume_support.install(monkeypatch, [])
    with pytest.raises(RuntimeError, match="needs the host execution lock"):
        study.resume_study(root)
    assert [row["state"] for row in _rows(root)] == ["intent"]


def test_the_recorded_process_is_cleaned_up_only_once_it_is_gone() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    operation = {
        "invocation_id": "a1",
        "stage": "review",
        "identity": {
            "pid": child.pid,
            "start_time": run_lock.process_start(child.pid),
            "process_group": child.pid,
        },
    }
    try:
        with pytest.raises(CleanupBlocked, match=f"kill -TERM -- -{child.pid}"):
            control_store.verify_invocation(operation)
    finally:
        child.kill()
        child.wait(timeout=5)
    assert control_store.verify_invocation(operation)["invocation_id"] == "a1"
    # Killed before its child was recorded: nothing of it can still be found by identity.
    assert control_store.verify_invocation({**operation, "identity": None})


def _seed_barrier(state: Path, run: Path) -> None:
    with ControlStore.open(state) as store:
        store.block(
            "process 7 of coding invocation a1 still runs",
            [
                {
                    "invocation_id": "a1",
                    "ledger": str(run / LEDGER_NAME),
                    "stage": "coding",
                    "scope": "pb-inv-a1.scope",
                    "label": "pluginbench.invocation=a1",
                    "identity": {"pid": 7, "start_time": "1", "process_group": 7},
                }
            ],
        )


def _entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: list[str]
) -> dict[str, list[str]]:
    """Each execution command aimed at a run other than the one that left the barrier."""
    config = tmp_path / "evaluation.yaml"
    config.write_text("{}")
    other = tmp_path / "other-run"
    other.mkdir()

    def record(name: str) -> Callable[..., Any]:
        def called(*args: Any, **kwargs: Any) -> Any:
            work.append(name)
            raise AssertionError(f"{name} ran while cleanup is unverified")

        return called

    monkeypatch.setattr(cli, "load_evaluation", lambda path: object())
    monkeypatch.setattr(cli, "plan_evaluation", lambda evaluation: {"coding_calls": 1})
    monkeypatch.setattr(cli, "_plan_summary", lambda plan: "plan")
    for name in (
        "prepare_evaluation", "plan_resume", "check_evaluation", "plan_review", "plan_measure"
    ):
        monkeypatch.setattr(cli, name, record(name))
    monkeypatch.setattr(judge, "run", record("judge"))
    return {
        "run config": ["run", str(config), "--yes", "--output", str(tmp_path / "new")],
        "run directory": ["run", str(other), "--yes", "--foreground"],
        "check": ["check", str(config), "--output", str(tmp_path / "checked")],
        "review": ["review", str(other), "--revision", "review-2", "--yes"],
        "measure": [
            "measure", str(other), "--measurements", "cyclomatic", "--revision", "m-2", "--yes"
        ],
        "judge run": ["judge", "run", str(other), "--judge", "codex:m:high"],
    }


@pytest.mark.parametrize(
    "command", ["run config", "run directory", "check", "review", "measure", "judge run"]
)
def test_a_cleanup_barrier_blocks_every_execution_command_whatever_run_it_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path, command: str
) -> None:
    _seed_barrier(isolated_host_state, tmp_path / "crashed-run")
    monkeypatch.setattr(control_store, "verify_invocation", _still_running)
    work: list[str] = []
    argv = _entry_points(tmp_path, monkeypatch, work)[command]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 2, result.output
    assert "cleanup of 1 crashed invocation is not verified" in result.stderr
    assert work == []
    assert _host(isolated_host_state).barrier == control_store.HOST_CLEANUP
    assert not run_lock.execution_owner(isolated_host_state).held


def test_a_barrier_lifts_once_its_cleanup_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    _seed_barrier(isolated_host_state, tmp_path / "crashed-run")
    monkeypatch.setattr(
        control_store, "verify_invocation", lambda operation: {"invocation_id": "a1"}
    )
    judged: list[Path] = []

    def judged_once(directory: Path, **kwargs: Any) -> int:
        judged.append(directory)
        return 0

    monkeypatch.setattr(judge, "run", judged_once)
    argv = ["judge", "run", str(tmp_path), "--judge", "codex:m:high"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert judged == [tmp_path]
    assert _host(isolated_host_state).barrier is None
    with ControlStore.open(isolated_host_state) as store:
        assert store.unclean() == []


# Takes the execution lock's flock and never publishes an owner record, as a process paused or
# killed between the two would leave it.
_SILENT_HOLDER = """
import sys, time
from pathlib import Path
from pluginbench import run_lock
state = Path(sys.argv[1])
run_lock.named_flock(state / run_lock.EXECUTION_LOCK, RuntimeError)
(state / "ready").write_text("ready")
time.sleep(30)
"""


def test_an_unverified_holder_turns_reconciling_after_30_seconds_and_is_never_touched(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c", _SILENT_HOLDER, str(state)],
        env={**os.environ, "PYTHONPATH": _SOURCE},
        start_new_session=True,
    )
    now = [100.0]
    watch = OwnerWatch(state, clock=lambda: now[0])
    try:
        deadline = time.monotonic() + 10
        while not (state / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert watch.poll() == "unknown"
        now[0] = 129.9
        assert watch.poll() == "unknown" and _host(state).reconciling is None
        now[0] = 130.0
        assert watch.poll() == "reconciling" and _host(state).reconciling == "owner_unknown"
        assert holder.poll() is None
    finally:
        holder.kill()
        holder.wait(timeout=5)
    assert watch.poll() == "free" and _host(state).reconciling is None
