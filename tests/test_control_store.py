"""Host recovery: the owner record fences every execution, and recovery clears it only on proof.

Nothing here starts a container, a systemd unit or a model. Docker is a fake; processes are real.
"""

from __future__ import annotations

import errno
import json
import os
import re
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
from fake_systemd import FakeSystemd
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from pluginbench import cli, control_store, harbor_backend, invocation_runtime, isolation, judge
from pluginbench import processes
from pluginbench import run_lock, study
from pluginbench import trial_containers
from pluginbench.control_store import CleanupBlocked, CleanupUnknown, ControlStore, OwnerWatch
from pluginbench.ledger import LEDGER_NAME, Ledger, child_identity, outcome
from test_resume import _REAL_RUNNING, _Docker, _container, _killed_run

_SOURCE = str(Path(pluginbench.__file__).parents[1])
# Every owner that dies unreleased says this until repair clears it.
_UNRELEASED = "ended without a clean release"


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


@pytest.fixture
def earlier_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owners write records as a build before P4b-3 did (schema 2), whose calls and containers
    did not all run as recorded operations, so each of their crashes needs repair."""
    monkeypatch.setattr(run_lock, "OWNER_SCHEMA", 2)


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
    # The failed resume's owner ran every call as an operation, so the next owner clears it by
    # itself once its checks pass, with no repair.
    assert _admitted(isolated_host_state, tmp_path)
    trace.clear()
    with control_store.execute(_owner(root)) as lock:
        assert lock.record["pending"] == []
        study.resume_study(root)
    assert trace[:4] == ["verify", "reconcile", "resolve coding", "resolve grading"]
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
        with pytest.raises(CleanupUnknown, match=f"app cleanup --confirm-stopped {tmp_path}"):
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


def test_a_reused_process_group_id_is_not_the_recorded_group(tmp_path: Path) -> None:
    # A new process now has the recorded leader's PID, which Linux frees only once no process
    # uses it as a process-group ID: the recorded group has ended, whatever runs as group N now.
    newcomer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        recorded = {**child_identity(newcomer.pid), "start_time": "1"}
        proof = control_store.verify_invocation(_check(recorded, tmp_path))
        assert proof["checked"] == "process group ended"
    finally:
        newcomer.kill()
        newcomer.wait(timeout=5)


def test_a_kill_hint_is_given_only_for_the_recorded_leader(tmp_path: Path) -> None:
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    assert leader.stdout is not None
    member = int(leader.stdout.readline())
    identity = child_identity(leader.pid)
    leader.wait(timeout=10)
    try:
        # The leader has exited, so the group's members cannot be matched to the record.
        with pytest.raises(CleanupBlocked) as caught:
            control_store.verify_invocation(_check(identity, tmp_path))
        assert f"has processes {member}" in str(caught.value)
        assert "kill" not in str(caught.value) and "belong to this call" in str(caught.value)
    finally:
        os.kill(member, signal.SIGKILL)


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
    for name in ("_preflight", "plan_resume", "plan_review", "plan_measure"):
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
    assert f"app cleanup --confirm-stopped {tmp_path / 'judged'}" in result.stderr
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


# Killed mid-write: the writer dies once its transaction has synced the rollback journal and
# changed the ledger file itself, as during a commit. A one-page cache makes the write spill to
# the file at once. The journal is then hot, and only a writable connection can roll it back.
_KILLED_MID_WRITE = """
import os, signal, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size = 1")
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE attempts SET state = 'complete'")
db.execute("CREATE TABLE spill (x)")
db.executemany("INSERT INTO spill VALUES (?)", [("x" * 1000,) for _ in range(200)])
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_recovery_reads_a_ledger_whose_writer_was_killed_mid_write(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    child.kill()
    child.wait(timeout=5)
    before = _rows(run)
    killed = subprocess.run(
        [sys.executable, "-c", _KILLED_MID_WRITE, str(run / LEDGER_NAME)], check=False
    )
    assert killed.returncode == -signal.SIGKILL
    assert (run / f"{LEDGER_NAME}-journal").is_file()
    _crashed_owner(isolated_host_state, kind="review", root=run, runtimes=[])

    with pytest.raises(CleanupBlocked, match=_UNRELEASED):
        with control_store.execute(_owner(tmp_path / "next")):
            pytest.fail("an unreleased owner was not verified")

    # The killed write is undone, as SQLite does on the next writable open; no row changed.
    assert _rows(run) == before
    assert not (run / f"{LEDGER_NAME}-journal").exists()


def test_repair_checks_containers_only_once_the_processes_are_gone(
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
        # A live producer could still create a container, so no container is checked yet, and
        # repair refuses: it never confirms a known survivor.
        with pytest.raises(CleanupBlocked, match="a container may still be created"):
            with control_store.execute(_owner(tmp_path / "unrelated")):
                pass
        result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
        assert result.exit_code == 2 and f"has processes {child.pid}" in result.stderr
        assert inventories == []
    finally:
        child.kill()
        child.wait(timeout=5)
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    assert result.exit_code == 0, result.output
    assert len(inventories) == 3 and _record(isolated_host_state)["pending"] == []
    assert _admitted(isolated_host_state, tmp_path)


@pytest.mark.usefixtures("earlier_build")
@pytest.mark.parametrize("unknown", ["daemon unreachable", "runtime unreadable"])
def test_containers_that_cannot_be_checked_during_a_resume_fence_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path, unknown: str
) -> None:
    """For a resume by an earlier build. A contained owner's containers all carry a recorded
    operation's label, so its own operations' checks prove them, never the run's inventory."""
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
    assert CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(root)]).exit_code == 0
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
        broken.setattr(run_lock, "write_atomic", unwritable)
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
    # The child is gone. The call recorded just before Ctrl-C has no process to check, but its
    # operation never made its runtime directory, so it never started: no repair is needed.
    assert _admitted(isolated_host_state, tmp_path)
    assert _record(isolated_host_state)["pending"] == []
    [_, intent] = _rows(run)
    proof = isolated_host_state / "invocations" / intent["id"] / "cleanup.json"
    assert json.loads(proof.read_text())["recovered"] == "its operation never started"


def test_an_owner_whose_call_cleanup_failed_stays_unreleased_after_a_normal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    run = tmp_path / "cleanup-failed"
    run.mkdir()
    ledger = Ledger.create(
        run / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "vanilla", "attempt": 1}],
        {"a": "running"},
    )
    children: list[int] = []

    def spawned(pid: int) -> None:
        children.append(pid)
        ledger.invocation_started(call, pid)

    def failed_cleanup(proc: Any) -> None:
        raise OSError("process-group cleanup failed")

    with monkeypatch.context() as broken:
        broken.setattr(processes, "terminate_process_group", failed_cleanup)
        with control_store.execute(_owner(run)):
            call = ledger.intent(slot=0, stage="review", attempt_id="a")
            with pytest.raises(subprocess.TimeoutExpired):
                processes.run_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=run, env=os.environ, timeout=0.05, on_spawn=spawned,
                )
            ledger.end(call, *outcome(subprocess.TimeoutExpired("x", 0.05), spawned=True))
    ledger.close()
    [child] = children
    try:
        # The owner returned normally, but its call's child may live: no clean release.
        assert _record(isolated_host_state)["state"] != "released"
        assert not _admitted(isolated_host_state, tmp_path)
    finally:
        os.killpg(child, signal.SIGKILL)
        os.waitpid(child, 0)
    # Its group is gone now, but only the operator can clear the recorded cleanup failure.
    assert not _admitted(isolated_host_state, tmp_path)
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    assert result.exit_code == 0 and "could not be stopped" in result.stdout, result.output
    assert _admitted(isolated_host_state, tmp_path)


def test_an_owner_whose_container_cleanup_failed_stays_unreleased_after_a_normal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    def unreachable(runtime: Any, **kwargs: Any) -> None:
        raise ValueError("Cannot connect to the Docker daemon")

    run = tmp_path / "trial"
    run.mkdir()
    Ledger.create(run / LEDGER_NAME, [], {}).close()  # Nothing else for the next owner to check.
    monkeypatch.setattr(harbor_backend, "_assert_daemon", unreachable)
    with control_store.execute(_owner(run)):
        with pytest.raises(ValueError, match="Cannot connect"):
            harbor_backend.cleanup(project="p", owner="o", expected_runtime={})
    assert _record(isolated_host_state)["state"] != "released"
    # The failure is on disk, so the next owner checks it however this one ended.
    with pytest.raises(CleanupBlocked, match="Harbor project p could not be cleaned up"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass


_RUNTIME = {
    "docker_engine": {"id": "fake", "endpoint": "unix:///fake"},
    "resources": {"cpus": 1, "memory_mb": 64},
}


def _measurement_docker(record: Path, *, on_start: Callable[[], None]) -> Callable[..., str]:
    """Fake Docker for a measurement: `create` saves the container to `record`, `start` marks it
    running, then calls `on_start`."""

    def docker(*args: str, **kwargs: Any) -> str:
        if args[0] == "create":
            record.write_text(json.dumps({"id": "m", "running": False}))
            return "m"
        if args[0] == "start":
            record.write_text(json.dumps({"id": "m", "running": True}))
            on_start()
            return ""
        raise AssertionError(args)

    return docker


def _measure(snapshot: Path, destination: Path) -> None:
    snapshot.mkdir()
    (snapshot / "index.ts").write_text("original\n")
    harbor_backend.run_native_command(
        snapshot=snapshot,
        task_identity={"isolation": _RUNTIME, "image_identity": {"image_id": "fake"}, "workdir": "/app"},
        destination=destination,
        command=["coverage"],
        report_path="coverage/lcov.info",
        timeout=10,
    )


@pytest.mark.usefixtures("earlier_build")
def test_a_failed_measurement_cleanup_leaves_a_normally_exiting_owner_unreleased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    daemon = {"up": True}
    disk = {"full": False}
    write = run_lock.write_atomic

    def assert_daemon(runtime: Any, **kwargs: Any) -> None:
        if not daemon["up"]:
            raise ValueError("Cannot connect to the Docker daemon")

    def daemon_lost() -> None:
        daemon["up"] = False
        disk["full"] = True
        raise ValueError("Cannot connect to the Docker daemon")

    def no_space(path: Path, value: Any) -> None:
        if disk["full"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, value)

    run = tmp_path / "measure"
    (run / "frozen/suite").mkdir(parents=True)
    Ledger.create(run / LEDGER_NAME, [], {}).close()
    monkeypatch.setattr(harbor_backend, "assert_runtime", lambda *args, **kwargs: _RUNTIME)
    monkeypatch.setattr(harbor_backend, "_assert_daemon", assert_daemon)
    monkeypatch.setattr(
        harbor_backend, "_docker", _measurement_docker(tmp_path / "container.json", on_start=daemon_lost)
    )
    monkeypatch.setattr(run_lock, "write_atomic", no_space)
    with control_store.execute(_owner(run)):
        run_lock.note_runtime(_RUNTIME)
        # The measurement fails, its cleanup fails, and the disk is full, so the reason is never
        # saved. The caller handles the error and the owner leaves its block normally.
        with pytest.raises(ValueError, match="Cannot connect"):
            _measure(tmp_path / "snapshot", tmp_path / "measurement")
        disk["full"] = False
    assert json.loads((tmp_path / "container.json").read_text())["running"]
    record = _record(isolated_host_state)
    assert record["state"] != "released" and record["leftovers"] == []
    # Docker answers again and lists nothing (no inventory finds a measurement container until
    # P4b labels it), yet the unreleased owner keeps the host blocked until repair.
    daemon["up"] = True
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])
    with pytest.raises(CleanupBlocked, match=_UNRELEASED):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    assert result.exit_code == 0, result.output
    assert _admitted(isolated_host_state, tmp_path)


# Owns the host lock for a run, records its runtime and one clean coding call, then is killed
# with a container still running: "measurement" kills itself when its measurement container
# starts; "regrade" kills itself after its regrade worker timed out, before the regrade's
# container cleanup. The fake container is saved to `container`.
_KILLED_WITH_CONTAINER = """
import json, os, signal, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from pluginbench import control_store, harbor_backend, invocation_runtime, processes, run_lock
from pluginbench.ledger import LEDGER_NAME, Ledger
run_lock.OWNER_SCHEMA = 2  # As a build before P4b-3.
state, run, mode, container = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
runtime = json.loads(sys.argv[5])
owner = run_lock.ExecutionOwner(kind="run", eval=str(run), root=str(run))
with control_store.execute(owner, state=state):
    run_lock.note_runtime(runtime)
    ledger = Ledger.open(run / LEDGER_NAME)
    call = ledger.intent(slot=0, stage="coding", attempt_id="a")
    processes.run_command(
        [sys.executable, "-c", ""], cwd=run, env=os.environ, timeout=10,
        on_spawn=lambda pid: ledger.invocation_started(call, pid),
    )
    ledger.end(call, "finished")
    if mode == "measurement":
        def docker(*args, **kwargs):
            if args[0] == "create":
                container.write_text(json.dumps({"id": "m", "running": False}))
                return "m"
            if args[0] == "start":
                container.write_text(json.dumps({"id": "m", "running": True}))
                os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError(args)
        snapshot = run.parent / "snapshot"
        snapshot.mkdir()
        (snapshot / "index.ts").write_text("original\\n")
        with patch.object(harbor_backend, "assert_runtime", lambda *a, **k: runtime), \\
                patch.object(harbor_backend, "_docker", docker):
            harbor_backend.run_native_command(
                snapshot=snapshot,
                task_identity={"isolation": runtime, "image_identity": {"image_id": "fake"}, "workdir": "/app"},
                destination=run.parent / "measurement", command=["coverage"],
                report_path="coverage/lcov.info", timeout=10,
            )
    else:
        grading = ledger.intent(slot=1, stage="grading", attempt_id="a")
        area = run.parent / "regrade"
        (area / "artifacts").mkdir(parents=True)
        code = (
            "import json, sys, time; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(json.dumps({'id': 'g', 'running': True})); "
            "Path(sys.argv[2]).write_text('{}'); time.sleep(30)"
        )
        def worker(argv, **kwargs):
            kwargs["timeout"] = 0.2
            return processes.run_command(
                [sys.executable, "-c", code, str(container), str(area / "artifacts/worker.json")], **kwargs
            )
        # The regrade runs as its grading operation, whose entry point this process stands in for.
        with patch.object(harbor_backend, "_private_docker_config", lambda **k: {}), \\
                patch.object(harbor_backend, "run_command", worker), \\
                patch.object(invocation_runtime, "_current", grading), \\
                patch.object(invocation_runtime, "_runtime", runtime):
            try:
                harbor_backend._native_process(
                    area=area, config={}, expected=runtime, harness="codex", timeout=5,
                    install_only=True, on_spawn=lambda pid: ledger.invocation_started(grading, pid),
                )
            except subprocess.TimeoutExpired:
                pass
            else:
                raise AssertionError("the regrade worker must time out")
        # The worker's group stopped and its output drained; the regrade's containers were not
        # cleaned up yet.
        os.kill(os.getpid(), signal.SIGKILL)
raise AssertionError("the owner must be killed with its container running")
"""


@pytest.mark.parametrize("mode", ["measurement", "regrade"])
@pytest.mark.usefixtures("earlier_build")
def test_an_earlier_builds_owner_killed_with_a_container_running_needs_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path, mode: str
) -> None:
    run = tmp_path / "run"
    (run / "frozen/suite").mkdir(parents=True)
    Ledger.create(
        run / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "coding", "attempt": 1}],
        {"a": "running"},
    ).close()
    container = tmp_path / "container.json"
    owner = subprocess.run(
        [sys.executable, "-c", _KILLED_WITH_CONTAINER, str(isolated_host_state), str(run), mode,
         str(container), json.dumps(_RUNTIME)],
        env={**os.environ, "PYTHONPATH": _SOURCE}, timeout=60,
    )
    assert owner.returncode == -signal.SIGKILL
    assert json.loads(container.read_text())["running"]
    # No inventory finds this unlabelled container, and every call the owner recorded ended
    # clean: nothing but the unreleased owner says it may still run.
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, **kwargs: None)
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])
    with pytest.raises(CleanupBlocked, match=_UNRELEASED):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    assert result.exit_code == 0 and _UNRELEASED in result.stdout, result.output
    assert _admitted(isolated_host_state, tmp_path)


class _FileDaemon:
    """A fake Docker daemon behind the frozen CLI whose containers live in a JSON file, so a
    killed owner and the next one share them. `start` calls `on_start`; with `removable`
    False, `rm` removes nothing, as when a container survives its removal."""

    def __init__(
        self, path: Path, *, on_start: Callable[[], None] = lambda: None, removable: bool = True
    ) -> None:
        self.path, self.on_start, self.removable = path, on_start, removable

    def load(self) -> dict[str, dict[str, Any]]:
        value: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )
        return value

    def labelled(self, label: str) -> list[str]:
        key, value = label.split("=", 1)
        return [item for item, row in self.load().items() if row["labels"].get(key) == value]

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        arguments = command[3:] if command[1:2] == ["--host"] else command[1:]
        containers, output = self.load(), ""
        if arguments[0] == "create":
            output = f"{len(containers):064x}"
            pairs = [arguments[index + 1] for index, item in enumerate(arguments) if item == "--label"]
            labels = dict(pair.split("=", 1) for pair in pairs)
            containers[output] = {"labels": labels, "running": False}
        elif arguments[0] == "start":
            containers[arguments[1]]["running"] = True
            self.path.write_text(json.dumps(containers))
            self.on_start()
        elif arguments[0] == "info":
            engine = _RUNTIME["docker_engine"]["id"]  # type: ignore[index]
            output = json.dumps(
                {"ID": engine, "ServerErrors": None, "ClientInfo": {"Version": "fake-cli"}}
            )
        elif arguments[0] == "ps":
            output = "\n".join(self.labelled(arguments[-1].removeprefix("label=")))
        elif arguments[0] == "inspect":
            key = invocation_runtime.LABEL_KEY
            output = "\n".join(containers[item]["labels"].get(key, "") for item in arguments[3:])
        elif arguments[0] == "rm" and self.removable:
            for item in arguments[3:]:
                containers.pop(item, None)
        self.path.write_text(json.dumps(containers))
        return subprocess.CompletedProcess(command, 0, output, "")


# Owns the host lock for a run, records an operation as `run_operation` does before anything of
# it spawns, then runs it in this process, which stands in for its scope's entry point.
# "measurement" kills itself when its measurement container starts; "regrade" kills itself after
# its regrade worker timed out, before the regrade's own container cleanup. The fake daemon's
# containers are saved to `daemon`.
_CONTAINED_KILLED = """
import json, os, signal, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from pluginbench import control_store, harbor_backend, invocation_runtime, processes, run_lock
from pluginbench.ledger import LEDGER_NAME, Ledger
from test_control_store import _FileDaemon
state, run, mode, daemon = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
runtime = json.loads(sys.argv[5])
owner = run_lock.ExecutionOwner(kind="run", eval=str(run), root=str(run))
with control_store.execute(owner, state=state) as lock:
    run_lock.note_runtime(runtime)
    ledger = Ledger.open(run / LEDGER_NAME)
    invocation = (
        ledger.intent(slot=0, stage="grading", attempt_id="a") if mode == "regrade" else uuid4().hex
    )
    directory = state / "invocations" / invocation
    directory.mkdir(parents=True)
    lock.note_operation({
        "id": invocation, "scope": f"pb-inv-{invocation}.scope",
        "label": f"pluginbench.invocation={invocation}", "docker": runtime,
        "dir": str(directory), "bootstrap": None, "payload": None,
    })
    if mode == "regrade":
        ledger.invocation_started(invocation, os.getpid())
    invocation_runtime._current, invocation_runtime._runtime = invocation, runtime
    die = lambda: os.kill(os.getpid(), signal.SIGKILL)
    fake = _FileDaemon(daemon, on_start=die)
    with patch.object(harbor_backend, "_frozen_executable", lambda *a: {"resolved_path": "docker"}):
        if mode == "measurement":
            snapshot = run.parent / "snapshot"
            snapshot.mkdir()
            (snapshot / "index.ts").write_text("original\\n")
            with patch.object(harbor_backend, "assert_runtime", lambda *a, **k: runtime), \\
                    patch.object(harbor_backend, "run_command", fake):
                harbor_backend.run_native_command(
                    snapshot=snapshot,
                    task_identity={"isolation": runtime, "image_identity": {"image_id": "fake"},
                                   "workdir": "/app"},
                    destination=run.parent / "measurement", command=["coverage"],
                    report_path="coverage/lcov.info", timeout=10,
                )
        area = run.parent / "regrade"
        (area / "artifacts").mkdir(parents=True)

        def worker(argv, **kwargs):
            # The Harbor worker's verifier container, labelled as `harbor_labels` labels it.
            config = json.loads(Path(argv[3]).read_text())
            fake(["docker", "create", "--label",
                  f"pluginbench.invocation={config['pluginbench_invocation']}", "grader"])
            kwargs["timeout"] = 0.2
            return processes.run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                                         **kwargs)

        with patch.object(harbor_backend, "_private_docker_config", lambda **k: {}), \\
                patch.object(harbor_backend, "run_command", worker):
            try:
                harbor_backend._native_process(
                    area=area, config={}, expected=runtime, harness="codex", timeout=5,
                    install_only=True,
                )
            except subprocess.TimeoutExpired:
                die()
raise AssertionError("the owner must be killed with its container left")
"""


@pytest.mark.parametrize("removed", [True, False], ids=["removed", "survives"])
@pytest.mark.parametrize("mode", ["measurement", "regrade"])
def test_an_owner_killed_with_a_labelled_container_recovers_only_once_it_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_host_state: Path,
    fake_systemd: FakeSystemd,
    mode: str,
    removed: bool,
) -> None:
    """The #130 Critic's two reproductions: a measurement container under `run`, then SIGKILL;
    a regrade timeout, then SIGKILL before its container cleanup. Neither container is in the
    frozen suite's inventory; each carries its operation's label, so recovery removes it and
    clears the owner by itself, and only once both inventories are empty."""
    run = tmp_path / "run"
    (run / "frozen/suite").mkdir(parents=True)
    _review_ledger(run).close()
    daemon = tmp_path / "daemon.json"
    tests = str(Path(__file__).parent)
    owner = subprocess.run(
        [sys.executable, "-c", _CONTAINED_KILLED, str(isolated_host_state), str(run), mode,
         str(daemon), json.dumps({**_RUNTIME, "docker_cli": {}})],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([_SOURCE, tests])}, timeout=60,
    )
    assert owner.returncode == -signal.SIGKILL
    [operation] = _record(isolated_host_state)["operations"]
    label = f"{invocation_runtime.LABEL_KEY}={operation['id']}"
    fake = _FileDaemon(daemon, removable=removed)
    assert len(fake.labelled(label)) == 1
    monkeypatch.setattr(harbor_backend, "run_command", fake)
    monkeypatch.setattr(processes, "run_command", fake)  # Recovery's current Docker CLI.
    binaries = tmp_path / "docker-bin"
    binaries.mkdir()
    (binaries / "docker").write_text("#!/bin/sh\nexit 1\n")
    (binaries / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        harbor_backend, "_frozen_executable", lambda identity, label: {"resolved_path": "docker"}
    )
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, **kwargs: None)
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])
    if not removed:
        for _ in range(2):  # Every later command is refused, not only the first.
            with pytest.raises(CleanupBlocked, match=f"containers labelled {label} remain"):
                with control_store.execute(_owner(tmp_path / "unrelated")):
                    pass
        assert fake.labelled(label) and _record(isolated_host_state)["pending"]
        return
    with control_store.execute(_owner(tmp_path / "unrelated")) as lock:
        assert lock.record["pending"] == []  # No repair: the checks alone cleared it.
    assert fake.load() == {}
    proof = json.loads(
        (isolated_host_state / "invocations" / operation["id"] / "cleanup.json").read_text()
    )
    assert proof["id"] == operation["id"]
    assert proof["docker_cli"] == {"path": str(binaries / "docker"), "version": "fake-cli"}


# Leads a call whose descendant leaves its process group with setsid() and keeps the call's
# output pipes open: the group stops, but the output drain times out.
_ESCAPING = """
import subprocess, sys, time
code = "import os, sys, time; os.setsid(); open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)"
subprocess.Popen([sys.executable, "-c", code, sys.argv[1]])
time.sleep(30)
"""


def _gone(pid: int) -> None:
    """Kill an orphan and wait until it no longer runs; init reaps it."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while control_store._leader_runs(pid, run_lock.started_at(pid)):
        assert time.monotonic() < deadline
        time.sleep(0.02)


def _wait_for(path: Path) -> int:
    deadline = time.monotonic() + 10
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return int(path.read_text())


def _review_ledger(run: Path) -> Ledger:
    return Ledger.create(
        run / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "vanilla", "attempt": 1}],
        {"a": "running"},
    )


@pytest.mark.usefixtures("earlier_build")
def test_a_failed_save_after_an_escaped_drain_still_needs_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    monkeypatch.setattr(processes, "_CLEANUP_TIMEOUT", 0.2)
    run = tmp_path / "review"
    run.mkdir()
    escaped = tmp_path / "escaped.pid"
    ledger = _review_ledger(run)

    def no_space(path: Path, value: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    try:
        with control_store.execute(_owner(run)):
            call = ledger.intent(slot=0, stage="review", attempt_id="a")
            with monkeypatch.context() as full:
                full.setattr(run_lock, "write_atomic", no_space)
                with pytest.raises((subprocess.TimeoutExpired, OSError)) as failed:
                    processes.run_command(
                        [sys.executable, "-c", _ESCAPING, str(escaped)],
                        cwd=run, env=os.environ, timeout=0.5,
                        on_spawn=lambda pid: ledger.invocation_started(call, pid),
                    )
            ledger.end(call, *outcome(failed.value, spawned=True))
        _wait_for(escaped)
        # Nothing about the failure reached the disk, but the owner was never released.
        assert _record(isolated_host_state)["state"] != "released"
        with pytest.raises(CleanupBlocked, match=_UNRELEASED):
            with control_store.execute(_owner(tmp_path / "unrelated")):
                pass
    finally:
        ledger.close()
        if escaped.exists() and escaped.read_text():
            _gone(int(escaped.read_text()))
    assert CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)]).exit_code == 0
    assert _admitted(isolated_host_state, tmp_path)


# Owns the host lock and makes one recorded call. With "mid-call", the call starts a descendant
# that escapes its group and the owner waits to be killed; with "between", the call ends clean
# and the owner kills itself before its next call.
_OWNER = """
import os, signal, subprocess, sys
from pathlib import Path
from pluginbench import control_store, processes, run_lock
from pluginbench.ledger import LEDGER_NAME, Ledger
run_lock.OWNER_SCHEMA = 2  # As a build before P4b-3.
state, run, mode, escaped = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
owner = run_lock.ExecutionOwner(kind="review", eval=str(run), root=str(run))
with control_store.execute(owner, state=state):
    ledger = Ledger.open(run / LEDGER_NAME)
    call = ledger.intent(slot=0, stage="review", attempt_id="a")
    escaping = "import os, sys, time; os.setsid(); open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)"
    code = (
        f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {escaping!r}, {escaped!r}]); time.sleep(30)"
        if mode == "mid-call" else ""
    )
    processes.run_command(
        [sys.executable, "-c", code], cwd=run, env=os.environ, timeout=60,
        on_spawn=lambda pid: ledger.invocation_started(call, pid),
    )
    ledger.end(call, "finished")
    os.kill(os.getpid(), signal.SIGKILL)
"""


def _killed_owner(tmp_path: Path, state: Path, mode: str) -> tuple[Path, Path, subprocess.Popen[bytes]]:
    run = tmp_path / "review"
    run.mkdir()
    _review_ledger(run).close()
    escaped = tmp_path / "escaped.pid"
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER, str(state), str(run), mode, str(escaped)],
        env={**os.environ, "PYTHONPATH": _SOURCE},
    )
    return run, escaped, owner


@pytest.mark.usefixtures("earlier_build")
def test_an_owner_killed_during_a_call_needs_repair_even_once_its_group_is_gone(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, escaped, owner = _killed_owner(tmp_path, isolated_host_state, "mid-call")
    leader = None
    try:
        _wait_for(escaped)
        owner.kill()  # Before it can save anything about the call.
        owner.wait(timeout=10)
        [row] = _rows(run)
        leader = row["identity"]["pid"]
        os.killpg(leader, signal.SIGKILL)  # The recorded group is now empty.
        deadline = time.monotonic() + 10
        while control_store._group_members(leader):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(CleanupBlocked, match=_UNRELEASED):
            with control_store.execute(_owner(tmp_path / "unrelated")):
                pass
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        if escaped.exists() and escaped.read_text():
            _gone(int(escaped.read_text()))
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    assert result.exit_code == 0 and _UNRELEASED in result.stdout
    assert _admitted(isolated_host_state, tmp_path)


@pytest.mark.usefixtures("earlier_build")
def test_an_owner_killed_between_calls_needs_repair_too(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, _, owner = _killed_owner(tmp_path, isolated_host_state, "between")
    assert owner.wait(timeout=30) == -signal.SIGKILL
    # Every call it made ended clean, but no check can list everything it may have started.
    with pytest.raises(CleanupBlocked, match=_UNRELEASED):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    assert CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)]).exit_code == 0
    assert _admitted(isolated_host_state, tmp_path)


def test_a_run_whose_calls_end_clean_is_released(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run = tmp_path / "review"
    run.mkdir()
    ledger = _review_ledger(run)
    with control_store.execute(_owner(run)):
        call = ledger.intent(slot=0, stage="review", attempt_id="a")
        processes.run_command(
            [sys.executable, "-c", ""], cwd=run, env=os.environ, timeout=30,
            on_spawn=lambda pid: ledger.invocation_started(call, pid),
        )
        ledger.end(call, "finished")
    ledger.close()
    assert _record(isolated_host_state)["state"] == "released"
    assert _admitted(isolated_host_state, tmp_path)


def test_an_escaped_process_holding_a_calls_output_blocks_until_repair_confirms_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    monkeypatch.setattr(processes, "_CLEANUP_TIMEOUT", 0.2)
    run = tmp_path / "review"
    run.mkdir()
    escaped = tmp_path / "escaped.pid"
    ledger = Ledger.create(
        run / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "vanilla", "attempt": 1}],
        {"a": "running"},
    )
    try:
        with control_store.execute(_owner(run)):
            call = ledger.intent(slot=0, stage="review", attempt_id="a")
            with pytest.raises(subprocess.TimeoutExpired) as timed_out:
                processes.run_command(
                    [sys.executable, "-c", _ESCAPING, str(escaped)],
                    cwd=run, env=os.environ, timeout=0.5,
                    on_spawn=lambda pid: ledger.invocation_started(call, pid),
                )
            assert any("Final output drain" in note for note in timed_out.value.__notes__)
            ledger.end(call, *outcome(timed_out.value, spawned=True))
        ledger.close()
        # Its recorded group is empty, but that proves nothing about the escaped process.
        with pytest.raises(CleanupBlocked, match="held open by an escaped process") as refused:
            with control_store.execute(_owner(tmp_path / "unrelated")):
                pass
        assert f"app cleanup --confirm-stopped {run}" in str(refused.value)
    finally:
        ledger.close()
        if escaped.exists():
            _gone(int(escaped.read_text()))
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    assert result.exit_code == 0 and "held open by an escaped process" in result.stdout
    assert _admitted(isolated_host_state, tmp_path)


# Takes the host lock for a run, records a failed cleanup, and is killed before it returns.
_KILLED_AFTER_LEFTOVER = """
import os, signal, sys
from pathlib import Path
from pluginbench import control_store, run_lock
state, run = Path(sys.argv[1]), sys.argv[2]
with control_store.execute(run_lock.ExecutionOwner(kind="review", eval=run, root=run), state=state):
    run_lock.note_leftover("output from an earlier call may still be held open by an escaped process")
    os.kill(os.getpid(), signal.SIGKILL)
"""


def test_a_failed_cleanup_survives_its_owner_being_killed(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run = tmp_path / "review"
    run.mkdir()
    Ledger.create(run / LEDGER_NAME, [], {}).close()
    killed = subprocess.run(
        [sys.executable, "-c", _KILLED_AFTER_LEFTOVER, str(isolated_host_state), str(run)],
        env={**os.environ, "PYTHONPATH": _SOURCE},
        check=False,
    )
    assert killed.returncode == -signal.SIGKILL
    with pytest.raises(CleanupBlocked, match="held open by an escaped process"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass




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
    with pytest.raises(CleanupBlocked, match=f"app cleanup --confirm-stopped {destination}"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass


def test_confirming_an_unreadable_record_never_covers_a_later_one(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    owner_record = isolated_host_state / run_lock.EXECUTION_OWNER
    owner_record.write_text("{not json")
    assert not _admitted(isolated_host_state, tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(isolated_host_state)])
    assert result.exit_code == 0 and "The host is unblocked." in result.stdout
    assert _admitted(isolated_host_state, tmp_path)
    owner_record.write_text("{not json either")
    assert not _admitted(isolated_host_state, tmp_path)


@pytest.mark.parametrize(
    "corrupt",
    ["pending null", "only released", "pending entry not an object", "pending pid a string", "truncated"],
)
def test_a_corrupt_owner_record_is_unknown_until_repair(
    tmp_path: Path, isolated_host_state: Path, corrupt: str
) -> None:
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    # A released owner that carries an earlier owner which never released.
    obligation = {
        "schema": 2, "pid": 7, "kind": "run", "root": str(earlier), "state": "active",
        "acquired_at": "earlier", "runtimes": [], "leftovers": [],
    }
    record: dict[str, Any] = {**obligation, "pid": 8, "state": "released", "pending": [obligation]}
    if corrupt == "pending null":
        record["pending"] = None
    elif corrupt == "only released":
        record = {"state": "released"}
    elif corrupt == "pending entry not an object":
        record["pending"] = [str(earlier)]
    elif corrupt == "pending pid a string":
        record["pending"] = [{**obligation, "pid": "7"}]
    text = json.dumps(record)
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(
        text[: len(text) // 2] if corrupt == "truncated" else text
    )
    # Nothing in it is trusted, not even its release: the state directory is the root to repair.
    state = re.escape(str(isolated_host_state))
    with pytest.raises(
        CleanupBlocked,
        match=rf"owner record in {state} is unreadable or invalid .*app cleanup --confirm-stopped {state}`",
    ):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(isolated_host_state)])
    assert result.exit_code == 0, result.output
    if corrupt not in ("only released", "truncated"):
        # The owner itself still validates, so it stays an obligation under its own root;
        # repairing the fence never clears it.
        assert "1 other run still wait" in result.stdout
        assert not _admitted(isolated_host_state, tmp_path)
        result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(earlier)])
        assert result.exit_code == 0, result.output
    assert "The host is unblocked." in result.stdout
    assert _admitted(isolated_host_state, tmp_path)


@pytest.mark.parametrize("record", ["null", "[]", '"x"', "0"])
def test_an_owner_record_that_is_not_an_object_is_unknown_until_repair(
    tmp_path: Path, isolated_host_state: Path, record: str
) -> None:
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(record)
    with pytest.raises(CleanupBlocked, match="it is not a JSON object"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(isolated_host_state)])
    assert result.exit_code == 0 and "The host is unblocked." in result.stdout, result.output
    assert _admitted(isolated_host_state, tmp_path)


def test_a_confirmation_covers_only_its_own_root_and_identity(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # Two unreleased owners with the same identity under different roots, as a hand edit
    # could leave them.
    same = {"schema": 2, "pid": 7, "acquired_at": "earlier", "kind": "measure", "state": "active"}
    path = isolated_host_state / run_lock.EXECUTION_OWNER
    path.write_text(json.dumps({
        **same, "pid": 8, "root": str(tmp_path / "released"), "state": "released",
        "pending": [{**same, "root": str(first)}, {**same, "root": str(second)}],
    }))
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(first)])
    assert result.exit_code == 0 and "1 other run still wait" in result.stdout, result.output
    # The first root's confirmation never covers the second.
    assert not _admitted(isolated_host_state, tmp_path)
    # Without its identity, an obligation is invalid: a new fence, never a shared identity.
    record = _record(isolated_host_state)
    [remaining] = [item for item in record["pending"] if item["root"] == str(second)]
    del remaining["pid"], remaining["acquired_at"]
    path.write_text(json.dumps(record))
    with pytest.raises(CleanupBlocked, match="pending obligation 0 has pid None, not an int"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    assert CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(second)]).exit_code == 2
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(isolated_host_state)])
    assert result.exit_code == 0, result.output
    # The release of the owner whose record held the bad entry is not trusted either, so it
    # waits for repair under its own root.
    assert "1 other run still wait" in result.stdout
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", record["root"]])
    assert result.exit_code == 0, result.output
    assert _admitted(isolated_host_state, tmp_path)


def test_a_malformed_entry_never_hides_a_valid_obligation_or_its_survivor(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    _crashed_owner(isolated_host_state, kind="review", root=run, runtimes=[])
    record = _record(isolated_host_state)
    record["pending"] = ["broken unrelated obligation"]
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(json.dumps(record))
    try:
        result = CliRunner().invoke(
            cli.app, ["app", "cleanup", "--confirm-stopped", str(isolated_host_state)]
        )
        # Repairing the fence leaves the valid owner, whose call's child still runs.
        assert result.exit_code == 0 and "1 other run still wait" in result.stdout, result.output
        with pytest.raises(CleanupBlocked, match=f"has processes {child.pid}"):
            with control_store.execute(_owner(tmp_path / "unrelated")):
                pass
        result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
        assert result.exit_code == 2 and f"has processes {child.pid}" in result.stderr
    finally:
        child.kill()
        child.wait(timeout=5)
    assert CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)]).exit_code == 0
    assert _admitted(isolated_host_state, tmp_path)


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
        # Repair refuses while the call's child lives: the check names it.
        result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
        assert result.exit_code == 2 and f"has processes {child.pid}" in result.stderr
    finally:
        child.kill()
        child.wait(timeout=5)
    assert CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)]).exit_code == 0
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
        # Repair does not count it as a survivor of this owner.
        result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
        assert result.exit_code == 0, result.output
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
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
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
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
    # Repair checks the recorded runtime, finds its containers cannot be listed, and says so.
    assert result.exit_code == 0 and "frozen suite" in result.stdout, result.output


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
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(path)])
    assert result.exit_code == 2 and "no open obligation names" in result.stderr
    [obligation] = _record(isolated_host_state)["pending"]
    assert obligation["kind"] == "judge" and obligation["root"] == str(tmp_path / "runs/one")


def test_repair_never_confirms_a_known_survivor(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    _crashed_owner(isolated_host_state, kind="review", root=run, runtimes=[])
    try:
        result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(run)])
        assert result.exit_code == 2 and f"has processes {child.pid}" in result.stderr
        assert "`app cleanup --confirm-stopped` confirms only what cannot be checked" in (
            result.stderr
        )
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
        [confirmation] = [
            item for item in store.confirmations() if item["check_id"] == crashed["id"]
        ]
    assert confirmation["root"] == str(root)
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


# `app cleanup` without --confirm-stopped, and --confirm-stopped narrowed to legacy items.


def test_plain_cleanup_refuses_while_a_call_of_a_dead_owner_still_runs(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    run, child = _live_call(tmp_path)
    _crashed_owner(isolated_host_state, kind="review", root=run, runtimes=[])
    try:
        result = CliRunner().invoke(cli.app, ["app", "cleanup"])
        assert result.exit_code == 2 and f"has processes {child.pid}" in result.stderr
        assert "the host is clean" not in result.stdout
        assert [item["root"] for item in _record(isolated_host_state)["pending"]] == [str(run)]
    finally:
        child.kill()
        child.wait(timeout=5)


_HOLDER = """
import sys, time
from pathlib import Path
from pluginbench import run_lock
state, root = Path(sys.argv[1]), sys.argv[2]
owner = run_lock.ExecutionOwner(kind="run", eval=root, root=root)
with run_lock.hold_execution(owner, state=state):
    (state / "ready").write_text("ready")
    time.sleep(30)
"""


def test_plain_cleanup_refuses_while_another_command_executes(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(isolated_host_state), str(tmp_path / "busy")],
        env={**os.environ, "PYTHONPATH": f"{_SOURCE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )
    try:
        while not (isolated_host_state / "ready").exists():
            assert holder.poll() is None
            time.sleep(0.02)
        result = CliRunner().invoke(cli.app, ["app", "cleanup"])
    finally:
        holder.kill()
        holder.wait(timeout=5)
    assert result.exit_code == 2
    assert f"already executing on this host: run for {tmp_path / 'busy'}" in result.stderr


def test_cleanup_hints_and_refusals_name_app_cleanup_not_repair(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    _crashed_owner(isolated_host_state, kind="judge", root=tmp_path / "one", runtimes=[])
    result = CliRunner().invoke(
        cli.app, ["app", "cleanup", "--confirm-stopped", str(tmp_path / "other")]
    )
    assert result.exit_code == 2
    assert "`pluginbench app cleanup --confirm-stopped` takes one of" in result.stderr
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text("{")
    with pytest.raises(CleanupBlocked) as refused:
        with control_store.execute(_owner(tmp_path / "next")):
            pass
    assert "`pluginbench app cleanup --confirm-stopped` takes this directory" in str(refused.value)
    assert "repair" not in str(refused.value).replace("app cleanup", "")


def test_cleanup_confirms_a_deleted_runs_legacy_obligation_by_its_root_or_its_id(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    gone = tmp_path / "deleted" / "run"  # Recorded, then deleted with its parent.
    _crashed_owner(isolated_host_state, kind="judge", root=gone, runtimes=[], acquired_at="a")
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(gone)])
    assert result.exit_code == 0, result.output
    assert _record(isolated_host_state)["pending"] == []

    _crashed_owner(isolated_host_state, kind="judge", root=gone, runtimes=[], acquired_at="b")
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", "7-b"])
    assert result.exit_code == 0, result.output
    assert "The host is unblocked." in result.stdout
    assert _record(isolated_host_state)["pending"] == []


def test_cleanup_never_confirms_an_unknown_the_runtime_recorded_itself(
    tmp_path: Path, isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A contained owner (schema 3) died with one recorded operation; systemd cannot answer.
    op = "0" * 32
    operation = {
        "id": op, "scope": f"pb-inv-{op}.scope", "label": f"pluginbench.invocation={op}",
        "docker": None, "dir": str(isolated_host_state / "invocations" / op),
    }
    record = {
        "schema": run_lock.CONTAINED_SCHEMA, "pid": 7, "acquired_at": "earlier", "kind": "judge",
        "root": str(tmp_path), "state": "active", "operations": [operation], "pending": [],
    }
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(json.dumps(record))

    def unknown(operation: Any, **kwargs: Any) -> None:
        raise CleanupUnknown("systemd could not be asked about scope pb-op-1.scope")

    monkeypatch.setattr(invocation_runtime, "verify_scope", unknown)
    monkeypatch.setattr(invocation_runtime, "verify_labelled", lambda operation, **kwargs: {})
    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(tmp_path)])
    assert result.exit_code == 2
    assert "restore the daemon and retry `pluginbench app cleanup`" in result.stderr
    assert "Only labelled containers whose recorded Docker endpoint another engine now answers" in (
        result.stderr
    )
    assert "Nothing was confirmed" in result.stderr
    with ControlStore.open(isolated_host_state) as store:
        assert store.confirmations() == []
    assert not _admitted(isolated_host_state, tmp_path)


def test_a_replaced_docker_daemon_is_refused_automatically_but_can_be_confirmed(
    tmp_path: Path, isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A contained owner died with one recorded operation whose containers ran on a daemon that
    # another engine has since replaced at the same endpoint.
    op = "1" * 32
    runtime = {"docker_engine": {"id": "recorded-engine", "endpoint": "unix:///recorded.sock"}}
    operation = {
        "id": op, "scope": f"pb-inv-{op}.scope", "label": f"pluginbench.invocation={op}",
        "docker": runtime, "dir": str(isolated_host_state / "invocations" / op),
    }
    record = {
        "schema": run_lock.CONTAINED_SCHEMA, "pid": 7, "acquired_at": "earlier", "kind": "judge",
        "root": str(tmp_path), "state": "active", "operations": [operation], "pending": [],
    }
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(json.dumps(record))
    monkeypatch.setattr(invocation_runtime, "verify_scope", lambda operation, **kwargs: {})
    binaries = tmp_path / "docker-bin"
    binaries.mkdir()
    (binaries / "docker").write_text("#!/bin/sh\nexit 1\n")
    (binaries / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")

    def another_engine(*arguments: str, **kwargs: Any) -> str:
        if arguments[0] == "info":
            info = {"ID": "replacement-engine", "ClientInfo": {"Version": "29.0.0"}}
            return json.dumps({**info, "ServerErrors": None})
        assert arguments[0] == "ps"  # It holds none of the operation's containers.
        return ""

    monkeypatch.setattr(invocation_runtime, "_recovery_docker", another_engine)
    plain = CliRunner().invoke(cli.app, ["app", "cleanup"])
    assert plain.exit_code == 2
    assert "another Docker engine, replacement-engine, answers at" in plain.stderr
    # The refusal names the way out.
    assert f"pluginbench app cleanup --confirm-stopped {tmp_path}" in plain.stderr
    assert not _admitted(isolated_host_state, tmp_path)

    result = CliRunner().invoke(cli.app, ["app", "cleanup", "--confirm-stopped", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Confirmed stopped at" in result.stdout and op in result.stdout
    with ControlStore.open(isolated_host_state) as store:
        [confirmation] = store.confirmations()
    assert confirmation["check_id"] == f"7-earlier:labelled:{op}"
    # It records which CLI found the other engine.
    assert f"checked with {binaries / 'docker'}, Docker CLI 29.0.0" in confirmation["reason"]
    assert _admitted(isolated_host_state, tmp_path)


@pytest.mark.parametrize(
    ("check", "legacy"),
    [
        ({"kind": "scope"}, True),  # Its owner predates the P4b runtime.
        ({"kind": "unrecorded", "contained": True}, True),  # A failed cleanup with no ticket.
        ({"kind": "invocation", "contained": True, "identity": {"pid": 1}}, True),  # No boot.
        ({"kind": "invocation", "contained": True, "identity": None}, True),
        ({"kind": "invocation", "contained": True, "identity": {"pid": 1, "boot_id": "b"}}, False),
        ({"kind": "scope", "contained": True}, False),
        ({"kind": "labelled", "contained": True}, False),
    ],
)
def test_only_a_check_nothing_recorded_can_prove_is_legacy(
    check: dict[str, Any], legacy: bool
) -> None:
    assert control_store._legacy(check) is legacy
