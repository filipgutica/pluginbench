"""Every execution command against a background worker launched at the same moment.

The worker and the command target different roots. The host execution lock decides: the loser
either fails busy (a command) or never claims its launch (a worker), so the two never execute at
once. Scopes are fake; no test starts a container or a model.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import typer
from typer.testing import CliRunner

import pluginbench
from fake_systemd import FakeSystemd
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import launches, until
from pluginbench import cli, control_store, coordinator, invocation_runtime, judge, run_lock
from pluginbench.control_store import ControlStore, LaunchFenced

pytestmark = pytest.mark.real_operations

_SOURCE = str(Path(pluginbench.__file__).parents[1])

# Claims its launch, records when its body started, and holds the host until `release` exists.
_WORKER = """
import sys, time
from pathlib import Path
from pluginbench import control_store, run_lock
state, root, launch_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
owner = run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root), launch_id=launch_id)
with control_store.execute(owner, state=state):
    (root / "entered").write_text(repr(time.time()))
    while not (root / "release").exists():
        time.sleep(0.02)
"""

# Each command, and the function it calls first once it owns the host.
_COMMANDS: dict[str, tuple[list[str], tuple[Any, str]]] = {
    "run": (["run", "{target}", "--yes"], (cli, "plan_resume")),
    "resume": (["resume", "{target}", "--yes"], (cli, "plan_resume")),
    "check": (["check", "{target}/eval.yaml", "--output", "{target}/check"],
              (cli, "load_evaluation")),
    "review": (["review", "{target}", "--revision", "review-2", "--yes"], (cli, "plan_review")),
    "measure": (["measure", "{target}", "--measurements", "cyclomatic", "--revision", "m-2",
                 "--yes"], (cli, "plan_measure")),
    "judge run": (["judge", "run", "{target}", "--judge", "codex:model:high"], (judge, "run")),
}


@pytest.mark.parametrize("order", ["command first", "command while launching", "worker first"])
@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_a_command_and_a_background_worker_never_execute_at_once(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    order: str,
) -> None:
    """Each interleaving is forced, so each side wins in turn: a command that holds the host
    while a worker launches; a command that takes the host after a launch is recorded but
    before its systemd-run; and a worker that holds the host when the command starts."""
    worker_root, target = tmp_path / "worker", tmp_path / "target"
    worker_root.mkdir()
    target.mkdir()
    (target / "eval.yaml").write_text("{}")
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [
            sys.executable, "-c", _WORKER, str(isolated_host_state), str(request.target), launch_id
        ],
    )
    monkeypatch.setenv("PYTHONPATH", f"{_SOURCE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    outcome: list[object] = []

    def dispatch() -> None:
        try:
            outcome.append(coordinator.launch(coordinator.LaunchRequest("run", worker_root)))
        except LaunchFenced as exc:
            outcome.append(exc)

    entered: list[bool] = []

    def execute(*args: object, **kwargs: object) -> None:
        entered.append((worker_root / "entered").exists())
        if order == "command first":
            dispatch()  # Launched, and given up on, while this command holds the host.
        raise typer.Exit(0)

    module, name = _COMMANDS[command][1]
    monkeypatch.setattr(module, name, execute)
    argv = [part.format(target=target) for part in _COMMANDS[command][0]]
    recorded, commanded = threading.Event(), threading.Event()
    start = invocation_runtime.start_gated

    def late(*args: Any, **kwargs: Any) -> Any:
        recorded.set()  # The launch is recorded; its systemd-run has not run yet.
        assert commanded.wait(timeout=60)
        return start(*args, **kwargs)

    thread = threading.Thread(target=dispatch)
    if order == "command while launching":
        monkeypatch.setattr(invocation_runtime, "start_gated", late)
        thread.start()
        assert recorded.wait(timeout=60)
    elif order == "worker first":
        thread.start()
        thread.join(timeout=60)  # The worker owns the host before the command starts.
    try:
        result = CliRunner().invoke(cli.app, argv)
        commanded.set()
        if thread.is_alive() or order == "command while launching":
            thread.join(timeout=60)
        if order == "worker first":
            # The worker won; the command started nothing.
            assert result.exit_code == 2, result.output
            assert entered == []
            assert "already executing" in result.stderr
            assert (worker_root / "entered").exists()
        else:
            # The command won; the worker was fenced, or failed busy, and never ran.
            assert result.exit_code == 0, result.output
            assert entered == [False]
            assert isinstance(outcome[0], LaunchFenced), outcome
            assert not (worker_root / "entered").exists()
    finally:
        commanded.set()
        (worker_root / "release").touch()
    until(lambda: {launch.status for launch in launches(isolated_host_state).values()}
          == {"finished"})


def test_app_starts_one_coordinator_shows_it_and_stops_it(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    runner = CliRunner()
    first, second = runner.invoke(cli.app, ["app"]), runner.invoke(cli.app, ["app"])
    try:
        assert first.exit_code == second.exit_code == 0, first.output + second.output
        # The second `app` names the coordinator the first one started.
        assert first.stdout.splitlines()[0] == second.stdout.splitlines()[0]
        status = runner.invoke(cli.app, ["app", "status", "--json"])
        assert status.exit_code == 0, status.output
        found = json.loads(status.stdout)["coordinator"]
        assert f"process {found['pid']}" in first.stdout
        assert "Execution: idle" in runner.invoke(cli.app, ["app", "status"]).stdout
    finally:
        stopped = runner.invoke(cli.app, ["app", "stop"])
    assert stopped.exit_code == 0, stopped.output
    assert "Workers keep running" in stopped.stdout
    assert "not running" in runner.invoke(cli.app, ["app", "status"]).stdout


def test_app_cleanup_verifies_an_owner_that_never_released_and_clears_the_host(
    isolated_host_state: Path, tmp_path: Path
) -> None:
    owner = run_lock.ExecutionOwner(kind="run", eval=str(tmp_path), root=str(tmp_path))
    with run_lock.hold_execution(owner) as lock:
        lock.dirty = True  # As a crash leaves it: never released.
    assert (run_lock.execution_owner(isolated_host_state).record or {}).get("state") != "released"

    result = CliRunner().invoke(cli.app, ["app", "cleanup"])

    assert result.exit_code == 0, result.output
    assert "Every check passed; the host is clean." in result.stdout
    record = run_lock.execution_owner(isolated_host_state).record
    assert record and record["kind"] == "recovery" and record["state"] == "released"
    assert record["pending"] == []


# `stop`: what was recorded, and what was signalled.


def _launch_for(state: Path, root: Path, *, claimed_by: int | None = None) -> str:
    launch_id = uuid4().hex
    with ControlStore.open(state) as store:
        store.create_launch(
            coordinator._ticket(launch_id, "run", "cli", root.resolve(),
                                f"pb-worker-{launch_id}.scope")
        )
        if claimed_by is not None:
            assert store.claim(launch_id, {"pid": claimed_by})
    return launch_id


def test_stop_of_a_launch_that_has_no_run_lock_yet_reports_its_recorded_stop(
    isolated_host_state: Path, tmp_path: Path
) -> None:
    launch_id = _launch_for(isolated_host_state, tmp_path)
    result = CliRunner().invoke(cli.app, ["stop", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"Stop recorded for {tmp_path}" in result.stdout
    assert "Nothing signalled: no run lock" in result.stdout
    assert launches(isolated_host_state)[launch_id].desired == "stop"


def test_stop_of_a_launch_whose_worker_died_says_no_live_process_holds_the_run(
    isolated_host_state: Path, tmp_path: Path
) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _launch_for(isolated_host_state, tmp_path, claimed_by=dead.pid)
    (tmp_path / run_lock.LOCK_NAME).write_text("")  # Left by the worker; nothing holds it.
    result = CliRunner().invoke(cli.app, ["stop", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"Stop recorded for {tmp_path}" in result.stdout
    assert f"No live process holds {tmp_path / run_lock.LOCK_NAME}; nothing signalled." in (
        result.stdout
    )


@pytest.fixture
def broken_control(isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A control database that exists but cannot be used, as when it is corrupt or locked."""
    with ControlStore.open(isolated_host_state):
        pass

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(ControlStore, "__init__", broken)


def test_a_stop_that_cannot_be_recorded_still_signals_and_says_so(
    broken_control: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signalled: list[Path] = []

    def stop(root: Path) -> dict[str, object]:
        signalled.append(root)
        return {"pgid": 4242}

    monkeypatch.setattr(run_lock, "stop", stop)
    result = CliRunner().invoke(cli.app, ["stop", str(tmp_path)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "the stop could not be recorded (database disk image is malformed)" in result.stderr
    assert "Sent SIGTERM to process group 4242" in result.stdout
    assert signalled == [tmp_path]


@pytest.mark.parametrize(
    "argv", [["run", "{run}", "--yes"], ["app"], ["app", "stop"], ["app", "status"]]
)
def test_a_broken_control_database_is_a_clean_error(
    fake_systemd: FakeSystemd, broken_control: None, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> None:
    monkeypatch.setattr(cli, "plan_resume", lambda root: {"coding_calls": 1, "review_calls": 0})
    monkeypatch.setattr(coordinator, "identity", lambda state=None: {"launch_id": "c", "pid": 1})
    result = CliRunner().invoke(cli.app, [part.format(run=tmp_path) for part in argv])
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert "the host control database" in result.stderr
    assert "database disk image is malformed" in result.stderr


# `app status` and `app --foreground`.


def test_app_status_writes_nothing_and_never_upgrades_an_older_database(
    isolated_host_state: Path,
) -> None:
    path = isolated_host_state / control_store.CONTROL_NAME
    with ControlStore.open(isolated_host_state):
        pass
    path.chmod(0o644)
    observer = sqlite3.connect(path)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        assert CliRunner().invoke(cli.app, ["app", "status"]).exit_code == 0
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before
        assert path.stat().st_mode & 0o777 == 0o644

        observer.execute("PRAGMA user_version = 1")
        observer.commit()
        result = CliRunner().invoke(cli.app, ["app", "status"])
        assert result.exit_code == 2 and "has schema 1" in result.stderr
        assert observer.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        observer.close()


def test_app_in_the_foreground_is_the_one_coordinator_until_its_stop_is_recorded(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    seen: list[dict[str, Any]] = []

    def observe_then_stop() -> None:
        until(lambda: coordinator.identity(isolated_host_state) is not None)
        found = coordinator.identity(isolated_host_state)
        assert found is not None
        # Another `app` finds this one rather than starting a second.
        seen.extend([found, coordinator.ensure_started(isolated_host_state)])
        with ControlStore.open(isolated_host_state) as store:
            store.request_stop(str(found["launch_id"]))

    thread = threading.Thread(target=observe_then_stop)
    thread.start()
    result = CliRunner().invoke(cli.app, ["app", "--foreground"])
    thread.join(timeout=30)
    assert result.exit_code == 0, result.output
    assert seen[0]["pid"] == os.getpid() and seen[1] == seen[0]
    assert launches(isolated_host_state)[seen[0]["launch_id"]].status == "finished"
    assert coordinator.identity(isolated_host_state) is None
