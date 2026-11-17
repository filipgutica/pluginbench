"""Host recovery: the owner record fences every execution, and recovery clears it only on proof.

Nothing here starts a container, a systemd unit or a model. Docker is a fake; processes are real.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import pluginbench
import resume_support
from pluginbench import cli, control_store, harbor_backend, isolation, judge, run_lock, study
from pluginbench import trial_containers
from pluginbench.control_store import CleanupBlocked, CleanupUnknown, ControlStore, OwnerWatch
from pluginbench.ledger import LEDGER_NAME, Ledger, child_identity
from test_resume import _REAL_RUNNING, _Docker, _container, _killed_run

_SOURCE = str(Path(pluginbench.__file__).parents[1])


def _owner(root: Path) -> run_lock.ExecutionOwner:
    return run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root))


def _record(state: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((state / run_lock.EXECUTION_OWNER).read_text())
    return value


def _crashed_owner(
    state: Path, *, kind: str, root: Path, runtimes: list[Any], acquired_at: str = "earlier"
) -> None:
    """The record an owner leaves when it dies before releasing."""
    record = {
        "schema": 2, "pid": 7, "acquired_at": acquired_at, "kind": kind, "root": str(root),
        "state": "active", "runtimes": runtimes, "pending": [],
    }
    (state / run_lock.EXECUTION_OWNER).write_text(json.dumps(record))


def _still_running(check: Mapping[str, Any]) -> dict[str, Any]:
    raise CleanupBlocked(f"process 7 of {check['stage']} invocation still runs")


def _crashed(tmp_path: Path, boundary: str = "during-call") -> Path:
    """A one-slot run killed in its coding call, with no host lock: one open invocation."""
    return _killed_run(tmp_path, boundary=boundary, at=1, tasks=1, treatments=0, repeats=1)


def _rows(root: Path) -> list[dict[str, Any]]:
    ledger = Ledger.read(root / LEDGER_NAME)
    try:
        return ledger.invocations()
    finally:
        ledger.close()


def _admitted(state: Path, tmp_path: Path) -> bool:
    """Whether a command on an unrelated run may start."""
    try:
        with control_store.execute(_owner(tmp_path / "unrelated"), state=state):
            return True
    except CleanupBlocked:
        return False


def test_unverified_cleanup_keeps_crashed_rows_open_and_fences_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    root = _crashed(tmp_path)
    [crashed] = _rows(root)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    monkeypatch.setattr(control_store, "verify_invocation", _still_running)
    with pytest.raises(CleanupBlocked, match="process 7 of coding invocation still runs"):
        with control_store.execute(_owner(root)):
            study.resume_study(root)
    # No row closed and no new intent; the owner record stays unreleased, so it fences the host.
    assert calls == [] and _rows(root) == [crashed]
    assert _record(isolated_host_state)["state"] == "active"
    assert not _admitted(isolated_host_state, tmp_path)

    # Once cleanup verifies, it does so before saved output is reconciled, and only then is the
    # crashed row marked unresolved.
    trace: list[str] = []

    def verified(check: Mapping[str, Any]) -> dict[str, Any]:
        trace.append("verify")
        return {"id": check["id"]}

    reconcile, resolve = study._reconcile, Ledger.resolve_crashed

    def reconciled(*args: Any) -> Any:
        trace.append("reconcile")
        return reconcile(*args)

    def resolved(self: Ledger, stage: str, *, cleaned: frozenset[str]) -> int:
        trace.append(f"resolve {stage}")
        return resolve(self, stage, cleaned=cleaned)

    monkeypatch.setattr(control_store, "verify_invocation", verified)
    monkeypatch.setattr(study, "_reconcile", reconciled)
    monkeypatch.setattr(Ledger, "resolve_crashed", resolved)
    with control_store.execute(_owner(root)) as lock:
        # Taking the lock verified the obligation the failed resume left, and cleared it.
        assert trace == ["verify"] and lock.record["pending"] == []
        study.resume_study(root)
    assert trace[:5] == ["verify", "verify", "reconcile", "resolve coding", "resolve grading"]
    [resolved_row, *new] = _rows(root)
    assert resolved_row["id"] == crashed["id"] and resolved_row["state"] == "unresolved"
    assert len(calls) == 1 and len(new) == 1
    assert _record(isolated_host_state)["state"] == "released"


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


def _check(identity: dict[str, Any] | None, root: Path) -> dict[str, Any]:
    return {"id": "a1", "root": str(root), "kind": "invocation", "stage": "review",
            "identity": identity}


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
    check = _check(identity, tmp_path)
    try:
        with pytest.raises(CleanupBlocked, match=rf"{leader.pid} .* has processes {member}"):
            control_store.verify_invocation(check)
    finally:
        os.kill(member, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while True:
        try:
            assert control_store.verify_invocation(check)["checked"] == "process group"
            break
        except CleanupBlocked:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    # What cannot be checked is unknown: no record, or a PID from another namespace.
    for unknown in (None, {**identity, "pid_namespace": "pid:[1]"}):
        with pytest.raises(CleanupUnknown, match=f"repair --confirm-stopped {tmp_path}"):
            control_store.verify_invocation(_check(unknown, tmp_path))
    # A process recorded before a reboot ended with it.
    rebooted = {**identity, "boot_id": "an-earlier-boot"}
    assert control_store.verify_invocation(_check(rebooted, tmp_path))


def test_a_zombie_leader_is_not_counted_as_running(tmp_path: Path) -> None:
    leader = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
    identity = child_identity(leader.pid)
    # Wait for the exit but leave it unreaped: a zombie keeps its PID and start time.
    os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOWAIT)
    try:
        assert control_store.verify_invocation(_check(identity, tmp_path))
    finally:
        leader.wait(timeout=5)


def _entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: list[str]
) -> dict[str, list[str]]:
    """Each execution command aimed at a run other than the one that left the obligation."""
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
def test_an_unreleased_owner_blocks_every_execution_command_whatever_run_it_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path, command: str
) -> None:
    # A killed `judge run`: no ledger records its processes, so its cleanup is unknown.
    _crashed_owner(isolated_host_state, kind="judge", root=tmp_path / "judged", runtimes=[])
    work: list[str] = []
    argv = _entry_points(tmp_path, monkeypatch, work)[command]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 2, result.output
    assert "cleanup after a crash is not verified" in result.stderr
    assert f"repair --confirm-stopped {tmp_path / 'judged'}" in result.stderr
    assert work == []
    assert not run_lock.execution_owner(isolated_host_state).held
    assert _record(isolated_host_state)["pending"][0]["kind"] == "judge"


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


def test_containers_are_checked_only_after_the_processes_and_on_every_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    (run / "frozen/suite").mkdir(parents=True)
    _crashed_owner(isolated_host_state, kind="run", root=run, runtimes=[{"docker_engine": {}}])
    inventories: list[Path] = []

    def inventory(evidence: Path, **kwargs: Any) -> list[dict[str, str]]:
        inventories.append(evidence)
        return []

    monkeypatch.setattr(trial_containers, "running", inventory)
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, **kwargs: None)
    try:
        # A live producer could still create a container, so the containers are unknown too.
        with pytest.raises(CleanupBlocked, match="a container may still be created"):
            with control_store.execute(_owner(tmp_path / "unrelated")):
                pass
        assert inventories == []
        assert not _admitted(isolated_host_state, tmp_path)
    finally:
        child.kill()
        child.wait(timeout=5)
    # The retry checks the containers as well as the processes: the obligation lists both.
    assert _admitted(isolated_host_state, tmp_path)
    assert len(inventories) == 3 and _record(isolated_host_state)["pending"] == []


@pytest.mark.parametrize("unknown", ["daemon unreachable", "runtime unreadable"])
def test_containers_that_cannot_be_checked_during_a_resume_fence_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path, unknown: str
) -> None:
    root = _crashed(tmp_path, "after-identity")
    [crashed] = _rows(root)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def unreachable(runtime: Any, **kwargs: Any) -> None:
        raise ValueError("Cannot connect to the Docker daemon")

    def unreadable(frozen: Any) -> dict[str, object]:
        raise ValueError("no isolation recorded")

    with monkeypatch.context() as broken:
        if unknown == "daemon unreachable":
            broken.setattr(harbor_backend, "_assert_daemon", unreachable)
            reason = "Docker could not be checked"
        else:
            broken.setattr(study, "prepared_runtime", unreadable)
            reason = "the Docker runtime could not be found"
        with pytest.raises(CleanupBlocked, match=reason):
            with control_store.execute(_owner(root)):
                study.resume_study(root)
        assert calls == [] and _rows(root) == [crashed]
        # Its process is gone, but the run's containers are unknown: an unrelated run stays out.
        assert not _admitted(isolated_host_state, tmp_path)
    assert _admitted(isolated_host_state, tmp_path)


def test_without_a_writable_owner_record_nothing_starts_and_the_obligation_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    _crashed_owner(isolated_host_state, kind="judge", root=tmp_path / "judged", runtimes=[])
    before = (isolated_host_state / run_lock.EXECUTION_OWNER).read_bytes()

    def unwritable(path: Path, value: Any) -> None:
        raise OSError("read-only file system")

    started: list[str] = []
    with monkeypatch.context() as broken:
        broken.setattr(run_lock, "_write_atomic", unwritable)
        with pytest.raises(OSError, match="read-only"):
            with control_store.execute(_owner(tmp_path / "unrelated")):
                started.append("work")
    assert started == []
    assert (isolated_host_state / run_lock.EXECUTION_OWNER).read_bytes() == before
    assert not _admitted(isolated_host_state, tmp_path)


def test_a_command_left_by_an_exception_stays_unreleased_until_the_next_owner_verifies_it(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    try:
        with pytest.raises(KeyboardInterrupt):
            with control_store.execute(_owner(run)):
                ledger = Ledger.open(run / LEDGER_NAME)
                ledger.intent(slot=1, stage="review", attempt_id="a")  # About to start a call.
                ledger.close()
                raise KeyboardInterrupt  # Ctrl-C while its calls still run.
        assert _record(isolated_host_state)["state"] == "active"
        assert not _admitted(isolated_host_state, tmp_path)
        # A refusal adds nothing: the one obligation is still the interrupted run.
        [obligation] = _record(isolated_host_state)["pending"]
        assert obligation["root"] == str(run) and obligation["kind"] == "run"
    finally:
        child.kill()
        child.wait(timeout=5)
    # The child is gone, but the call recorded just before Ctrl-C has no process to check.
    assert not _admitted(isolated_host_state, tmp_path)
    result = CliRunner().invoke(cli.app, ["repair", str(run), "--confirm-stopped"])
    assert result.exit_code == 0 and "The host is unblocked." in result.stdout
    assert _admitted(isolated_host_state, tmp_path)
    assert _record(isolated_host_state)["pending"] == []


def test_an_error_before_any_recorded_work_releases_the_host(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    with pytest.raises(ValueError, match="unsupported measurements"):
        with control_store.execute(_owner(tmp_path / "measured")):
            raise ValueError("unsupported measurements: bogus")
    assert _record(isolated_host_state)["state"] == "released"
    assert _admitted(isolated_host_state, tmp_path)


def test_a_run_root_without_a_ledger_is_unknown(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    # Killed while preparing a new run: nothing records what preparation started.
    destination = tmp_path / "preparing"
    destination.mkdir()
    _crashed_owner(isolated_host_state, kind="run", root=destination, runtimes=[])
    with pytest.raises(CleanupBlocked, match=f"repair --confirm-stopped {destination}"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass


def test_confirming_an_unreadable_record_never_covers_a_later_one(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    owner_record = isolated_host_state / run_lock.EXECUTION_OWNER
    owner_record.write_text("{not json")
    assert not _admitted(isolated_host_state, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["repair", str(isolated_host_state), "--confirm-stopped"])
    assert result.exit_code == 0 and "The host is unblocked." in result.stdout
    assert _admitted(isolated_host_state, tmp_path)
    owner_record.write_text("{not json either")
    assert not _admitted(isolated_host_state, tmp_path)


def test_a_call_that_ended_while_its_process_lived_is_checked_after_its_owner_dies(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    # Its process-group cleanup failed, so the call ended failed_transient with its child alive,
    # and then the owner was killed.
    run, child = _live_call(tmp_path)
    acquired_at = datetime.now(UTC).isoformat()
    ledger = Ledger.open(run / LEDGER_NAME)
    [call] = ledger.invocations()
    ledger.end(call["id"], "failed_transient", "OSError: process-group cleanup failed")
    ledger.close()
    _crashed_owner(
        isolated_host_state, kind="review", root=run, runtimes=[], acquired_at=acquired_at
    )
    try:
        assert not _admitted(isolated_host_state, tmp_path)
    finally:
        child.kill()
        child.wait(timeout=5)
    assert _admitted(isolated_host_state, tmp_path)


def test_a_call_that_ended_without_its_process_record_is_unknown(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    # Its child ran, but the record of it failed, so the call ended with no process to check.
    run = tmp_path / "unrecorded"
    run.mkdir()
    ledger = Ledger.create(
        run / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "vanilla", "attempt": 1}],
        {"a": "running"},
    )
    call = ledger.intent(slot=0, stage="grading", attempt_id="a")
    ledger.end(call, "failed_transient", "OSError: the start record could not be written")
    ledger.close()
    _crashed_owner(isolated_host_state, kind="run", root=run, runtimes=[])
    with pytest.raises(CleanupBlocked, match=f"grading invocation {call} has no verifiable"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass


def test_calls_that_ended_before_the_dead_owner_started_are_not_its_obligation(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)  # The live child stands in for a reused process group.
    ledger = Ledger.open(run / LEDGER_NAME)
    [call] = ledger.invocations()
    ledger.end(call["id"], "finished")
    ledger.close()
    later = datetime.now(UTC).isoformat()
    _crashed_owner(isolated_host_state, kind="review", root=run, runtimes=[], acquired_at=later)
    try:
        assert _admitted(isolated_host_state, tmp_path)
    finally:
        child.kill()
        child.wait(timeout=5)


def test_each_recorded_runtime_is_checked_even_when_another_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    run = tmp_path / "two-runtimes"
    (run / "frozen/suite").mkdir(parents=True)
    Ledger.create(run / LEDGER_NAME, [], {}).close()
    runtimes = [{"docker_engine": {"id": "offline"}}, {"docker_engine": {"id": "live"}}]
    _crashed_owner(isolated_host_state, kind="run", root=run, runtimes=runtimes)
    daemons: list[str] = []

    def daemon(runtime: dict[str, Any], **kwargs: Any) -> None:
        daemons.append(runtime["docker_engine"]["id"])
        if runtime["docker_engine"]["id"] == "offline":
            raise ValueError("Cannot connect to the Docker daemon")

    survivor = [{"name": "survivor", "id": "s", "project": "p"}]
    monkeypatch.setattr(harbor_backend, "_assert_daemon", daemon)
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: survivor)
    monkeypatch.setattr(trial_containers, "stop_left_running", lambda evidence, **kwargs: None)
    result = CliRunner().invoke(cli.app, ["repair", str(run), "--confirm-stopped"])
    # The live daemon still runs a container, so nothing is confirmed, not even the offline one.
    assert result.exit_code == 2 and "survivor" in result.stderr, result.output
    assert "live" in daemons
    with ControlStore.open(isolated_host_state) as store:
        assert store.confirmations() == []
    assert not _admitted(isolated_host_state, tmp_path)


def test_a_recorded_runtime_is_checked_even_when_the_frozen_suite_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    run = tmp_path / "unfrozen"
    run.mkdir()
    Ledger.create(run / LEDGER_NAME, [], {}).close()
    _crashed_owner(
        isolated_host_state, kind="run", root=run, runtimes=[{"docker_engine": {"id": "d"}}]
    )
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, **kwargs: None)
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])
    with pytest.raises(CleanupBlocked, match="frozen suite .* is missing"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass


def test_recovery_shares_one_deadline_that_caps_every_docker_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, container = _stuck_run(tmp_path)
    timeouts: list[int] = []
    docker = _Docker([])

    def timed(*arguments: str, timeout: int = 60, **kwargs: Any) -> str:
        timeouts.append(timeout)
        return docker(*arguments, **kwargs)

    monkeypatch.setattr(trial_containers, "running", _REAL_RUNNING)
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, **kwargs: None)
    monkeypatch.setattr(harbor_backend, "_docker", timed)
    control_store.verify_containers(_containers(run), deadline=time.monotonic() + 5)
    assert timeouts and max(timeouts) <= 5
    with pytest.raises(CleanupUnknown, match="120 seconds"):
        control_store.verify_containers(_containers(run), deadline=time.monotonic() - 1)


def _stuck_run(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    run = tmp_path / "stuck"
    (run / "frozen/suite/0/task").mkdir(parents=True)
    container = _container(
        "main", "pluginbench-" + "a" * 32 + "__env", str(run / "frozen/suite/0/task/environment")
    )
    return run, container


def _containers(run: Path) -> dict[str, Any]:
    return {"id": "c1", "root": str(run), "kind": "containers", "evidence": str(run),
            "runtime": {"docker_engine": {}}}


def test_a_container_still_running_after_its_stop_or_created_late_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, container = _stuck_run(tmp_path)
    docker = _Docker([container])
    monkeypatch.setattr(trial_containers, "running", _REAL_RUNNING)
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, **kwargs: None)

    def ignored_stop(*arguments: str, **kwargs: Any) -> str:
        return "main" if arguments[0] == "stop" else docker(*arguments, **kwargs)

    monkeypatch.setattr(harbor_backend, "_docker", ignored_stop)
    with pytest.raises(CleanupBlocked, match="still run after the stop: "):
        control_store.verify_containers(_containers(run))
    # A sidecar created after the stop shows in the second inventory.
    container["running"] = True
    lists = 0
    sidecar = _container("sidecar", container["project"], container["working_dir"])

    def late_sidecar(*arguments: str, **kwargs: Any) -> str:
        nonlocal lists
        if arguments[0] == "ps":
            lists += 1
            if lists == 4:
                docker.containers.append(sidecar)
        return docker(*arguments, **kwargs)

    monkeypatch.setattr(harbor_backend, "_docker", late_sidecar)
    with pytest.raises(CleanupBlocked, match="still run after the stop: .*sidecar"):
        control_store.verify_containers(_containers(run))


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Docker daemon changed; cannot accept export or cleanup proof"),
        isolation.RuntimeDriftError("Cannot verify frozen Docker CLI"),
    ],
)
def test_a_replaced_docker_daemon_or_client_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    run, _ = _stuck_run(tmp_path)

    def replaced(runtime: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(harbor_backend, "_assert_daemon", replaced)
    with pytest.raises(CleanupUnknown, match=f"Docker could not be checked .*: {error}"):
        control_store.verify_containers(_containers(run))


@pytest.mark.parametrize("target", ["parent", "filesystem-root"])
def test_repair_takes_only_the_exact_root_an_obligation_names(
    tmp_path: Path, isolated_host_state: Path, target: str
) -> None:
    _crashed_owner(isolated_host_state, kind="judge", root=tmp_path / "runs/one", runtimes=[])
    path = tmp_path / "runs" if target == "parent" else Path("/")
    result = CliRunner().invoke(cli.app, ["repair", str(path), "--confirm-stopped"])
    assert result.exit_code == 2 and "no open obligation names" in result.stderr
    [obligation] = _record(isolated_host_state)["pending"]
    assert obligation["kind"] == "judge" and obligation["root"] == str(tmp_path / "runs/one")


def test_repair_never_confirms_a_known_survivor(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    _crashed_owner(isolated_host_state, kind="review", root=run, runtimes=[])
    try:
        result = CliRunner().invoke(cli.app, ["repair", str(run), "--confirm-stopped"])
        assert result.exit_code == 2 and f"has processes {child.pid}" in result.stderr
        with ControlStore.open(isolated_host_state) as store:
            assert store.confirmations() == []
        assert not _admitted(isolated_host_state, tmp_path)
    finally:
        child.kill()
        child.wait(timeout=5)


def test_an_unverifiable_call_blocks_until_the_operator_confirms_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    root = _crashed(tmp_path)  # Killed before its child was recorded.
    [crashed] = _rows(root)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    with pytest.raises(CleanupBlocked, match="no verifiable process record") as caught:
        with control_store.execute(_owner(root)):
            study.resume_study(root)
    # The printed hint works verbatim.
    hint = str(caught.value).split("`")[1].split()
    runner = CliRunner()
    assert runner.invoke(cli.app, [hint[1], hint[3]]).exit_code == 2  # No --confirm-stopped.
    result = runner.invoke(cli.app, hint[1:])
    assert result.exit_code == 0, result.output
    assert f"{crashed['id']} (coding invocation {crashed['id']} has no verifiable" in result.stdout
    assert "The host is unblocked." in result.stdout
    with ControlStore.open(isolated_host_state) as store:
        [confirmation] = store.confirmations()
    assert confirmation["check_id"] == crashed["id"] and confirmation["root"] == str(root)
    assert confirmation["confirmed_at"]
    # The confirmation covers that call, so the resume closes it.
    with control_store.execute(_owner(root)):
        study.resume_study(root)
    assert _rows(root)[0]["state"] == "unresolved" and len(calls) == 1


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


def _reconciling(state: Path) -> str | None:
    with ControlStore.open(state) as store:
        return store.reconciling()


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
        assert watch.poll() == "unknown" and _reconciling(state) is None
        now[0] = 130.0
        assert watch.poll() == "reconciling" and _reconciling(state) == "owner_unknown"
        assert holder.poll() is None
    finally:
        holder.kill()
        holder.wait(timeout=5)
    assert watch.poll() == "free" and _reconciling(state) is None
