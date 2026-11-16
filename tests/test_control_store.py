"""Host recovery: verified cleanup before any crashed row closes, a barrier every command honours,
and an unknown lock holder that turns `reconciling` after 30 seconds without being touched.

Cleanup checks are fakes here; nothing starts a container, a systemd unit or a model.
"""

from __future__ import annotations

import os
import re
import signal
import sqlite3
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
from pluginbench import cli, control_store, harbor_backend, judge, run_lock, study
from pluginbench import trial_containers
from pluginbench.control_store import CleanupBlocked, ControlStore, OwnerWatch
from pluginbench.ledger import LEDGER_NAME, Ledger, child_identity
from test_resume import _REAL_RUNNING, _Docker, _container, _killed_run

_SOURCE = str(Path(pluginbench.__file__).parents[1])


def _host(state: Path) -> control_store.HostState:
    with ControlStore.open(state) as store:
        return store.host()


def _still_running(item: Mapping[str, Any]) -> dict[str, Any]:
    raise CleanupBlocked(f"process 7 of {item['detail']['stage']} invocation still runs")


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
        assert [row["id"] for row in store.items(unclean=True)] == [crashed["id"]]

    # Once cleanup verifies, it does so before saved output is reconciled, and only then is the
    # crashed row marked unresolved.
    trace: list[str] = []

    def verified(item: Mapping[str, Any]) -> dict[str, Any]:
        trace.append("verify")
        return {"id": item["id"]}

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


# Leads a new session, starts a child in its own process group, reports that child's PID and
# exits: the leader is gone while its group still runs.
_LEADER = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
print(child.pid, flush=True)
"""


def test_a_recorded_process_group_is_clean_only_once_every_member_is_gone(
    tmp_path: Path,
) -> None:
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    assert leader.stdout is not None
    member = int(leader.stdout.readline())
    identity = child_identity(leader.pid)
    leader.wait(timeout=10)
    item = {"id": "a1", "run": str(tmp_path), "kind": "invocation", "detail": {
        "stage": "review", "identity": identity,
    }}
    try:
        with pytest.raises(CleanupBlocked, match=rf"group {leader.pid} .* still has processes {member}"):
            control_store.verify_invocation(item)
    finally:
        os.kill(member, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while True:
        try:
            assert control_store.verify_invocation(item)["checked"] == "process group"
            break
        except CleanupBlocked:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    # What cannot be checked blocks: no record, or a PID from another namespace.
    for unknown in (None, {**identity, "pid_namespace": "pid:[1]"}):
        with pytest.raises(CleanupBlocked, match="repair --confirm-stopped"):
            control_store.verify_invocation({**item, "detail": {"stage": "review", "identity": unknown}})
    # A process recorded before a reboot ended with it.
    rebooted = {**identity, "boot_id": "an-earlier-boot"}
    assert control_store.verify_invocation({**item, "detail": {"stage": "review", "identity": rebooted}})


def _seed_barrier(state: Path, run: Path) -> None:
    with ControlStore.open(state) as store:
        store.fence(
            [
                {
                    "id": "a1",
                    "run": str(run),
                    "kind": "invocation",
                    "detail": {"stage": "coding", "identity": None},
                }
            ],
            "process 7 of coding invocation a1 still runs",
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
    assert "cleanup of 1 crashed call is not verified" in result.stderr
    assert work == []
    assert _host(isolated_host_state).barrier == control_store.HOST_CLEANUP
    assert not run_lock.execution_owner(isolated_host_state).held


def test_a_barrier_lifts_once_its_cleanup_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    _seed_barrier(isolated_host_state, tmp_path / "crashed-run")
    monkeypatch.setattr(control_store, "verify_invocation", lambda item: {"id": "a1"})
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
        assert store.items(unclean=True) == []


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


def _live_call(tmp_path: Path) -> tuple[Path, subprocess.Popen[bytes]]:
    """A run whose open review call recorded a child that still runs."""
    run = tmp_path / "crashed"
    run.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    ledger = Ledger.create(
        run / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "vanilla", "attempt": 1}],
        {"a": "running"},
    )
    try:
        ledger.invocation_started(ledger.intent(slot=0, stage="review", attempt_id="a"), child.pid)
    finally:
        ledger.close()
    return run, child


def _judged(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[Path]]:
    """`judge run` aimed at a run with nothing to do with the crash; returns argv and calls."""
    judged: list[Path] = []

    def judged_once(directory: Path, **kwargs: Any) -> int:
        judged.append(directory)
        return 0

    monkeypatch.setattr(judge, "run", judged_once)
    return ["judge", "run", "unrelated-run", "--judge", "codex:m:high"], judged


def _sweep(run: Path) -> None:
    ledger = Ledger.open(run / LEDGER_NAME)
    try:
        control_store.verify_crashed(ledger, run / LEDGER_NAME, ("review",))
    finally:
        ledger.close()


def _failing_fence(self: ControlStore, items: Any, detail: str) -> None:
    raise sqlite3.OperationalError("disk I/O error")


def test_a_failed_barrier_write_still_keeps_an_unrelated_run_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    argv, judged = _judged(monkeypatch)
    try:
        with monkeypatch.context() as broken:
            broken.setattr(ControlStore, "fence", _failing_fence)
            with control_store.execute(_owner(run)), pytest.raises(sqlite3.OperationalError):
                _sweep(run)
        # The marker written before the barrier fences the host for every later command.
        result = CliRunner().invoke(cli.app, argv)
        assert result.exit_code == 2, result.output
        assert f"still has processes {child.pid}" in result.stderr and judged == []
    finally:
        child.kill()
        child.wait(timeout=5)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert len(judged) == 1 and _host(isolated_host_state).barrier is None
    assert not (isolated_host_state / control_store.MARKER_NAME).exists()


def test_when_no_fence_can_be_written_the_host_stays_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)

    def unwritable(state: Path, items: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(control_store, "_write_marker", unwritable)
    monkeypatch.setattr(ControlStore, "fence", _failing_fence)
    try:
        with control_store.execute(_owner(run)):
            with pytest.raises(CleanupBlocked, match=r"process \d+ keeps the host") as caught:
                _sweep(run)
        found = re.search(r"process (\d+) keeps", str(caught.value))
        assert found is not None
        sleeper = int(found.group(1))
        try:
            snapshot = run_lock.execution_owner(isolated_host_state)
            assert snapshot.held and not snapshot.verified
            with pytest.raises(run_lock.ExecutionBusy):
                with run_lock.hold_execution(_owner(tmp_path / "unrelated")):
                    pass
        finally:
            os.kill(sleeper, signal.SIGKILL)
    finally:
        child.kill()
        child.wait(timeout=5)


def test_a_failed_container_stop_blocks_the_host_before_any_crashed_row_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    root = _killed_run(tmp_path, boundary="after-identity", at=1, tasks=1, treatments=0, repeats=1)
    [crashed] = _rows(root)
    here = str((root / "frozen/suite/0/task").resolve()) + "/environment"
    docker = _Docker([_container("main", "pluginbench-" + "a" * 32 + "__env", here)])

    def stuck(*arguments: str, **kwargs: Any) -> str:
        if arguments[0] == "stop":
            raise ValueError("docker stop failed: permission denied")
        return docker(*arguments, **kwargs)

    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    monkeypatch.setattr(trial_containers, "running", _REAL_RUNNING)
    monkeypatch.setattr(harbor_backend, "_docker", stuck)
    with control_store.execute(_owner(root)):
        with pytest.raises(CleanupBlocked, match="could not be stopped: docker stop failed"):
            study.resume_study(root)
    assert calls == [] and _rows(root) == [crashed]
    assert _host(isolated_host_state).barrier == control_store.HOST_CLEANUP
    argv, judged = _judged(monkeypatch)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 2 and "could not be stopped" in result.stderr and judged == []
    # Once Docker can stop it, the next command does, lifts the barrier and runs.
    monkeypatch.setattr(harbor_backend, "_docker", docker)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert docker.stopped == [["main"]] and len(judged) == 1
    assert _host(isolated_host_state).barrier is None


def test_an_unverifiable_call_blocks_until_the_operator_confirms_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    root = _crashed(tmp_path)  # Killed before its child was recorded.
    [crashed] = _rows(root)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    with control_store.execute(_owner(root)):
        with pytest.raises(CleanupBlocked, match=f"repair --confirm-stopped {root}"):
            study.resume_study(root)
    runner = CliRunner()
    refused = runner.invoke(cli.app, ["repair", str(root)])
    assert refused.exit_code == 2
    assert _host(isolated_host_state).barrier == control_store.HOST_CLEANUP
    result = runner.invoke(cli.app, ["repair", str(root), "--confirm-stopped"])
    assert result.exit_code == 0, result.output
    assert f"Confirmed stopped: coding {crashed['id']} of {root}" in result.stdout
    assert "The host is unblocked." in result.stdout
    with ControlStore.open(isolated_host_state) as store:
        [item] = store.items(unclean=False)
    assert item["proof"]["confirmed_by"] == "operator" and item["proof"]["at"]
    # The confirmation stands: the resume closes the crashed row and codes the slot again.
    with control_store.execute(_owner(root)):
        study.resume_study(root)
    assert _rows(root)[0]["state"] == "unresolved" and len(calls) == 1
