"""Every execution command against a background worker launched at the same moment.

The worker and the command target different roots. The host execution lock decides: the loser
either fails busy (a command) or never claims its launch (a worker), so the two never execute at
once. Scopes are fake; no test starts a container or a model.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import pluginbench
from fake_systemd import FakeSystemd
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import launches, until
from pluginbench import cli, coordinator, judge, run_lock
from pluginbench.control_store import LaunchFenced

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


@pytest.mark.parametrize("order", ["at once", "worker first"])
@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_a_command_and_a_background_worker_never_execute_at_once(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    order: str,
) -> None:
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
    entered: list[tuple[float, float]] = []

    def execute(*args: object, **kwargs: object) -> None:
        start = time.time()
        time.sleep(0.3)  # Long enough for a worker that overlapped to show it.
        entered.append((start, time.time()))
        raise typer.Exit(0)

    module, name = _COMMANDS[command][1]
    monkeypatch.setattr(module, name, execute)
    argv = [part.format(target=target) for part in _COMMANDS[command][0]]
    both = threading.Barrier(2, timeout=10)
    outcome: list[object] = []

    def dispatch() -> None:
        both.wait()
        try:
            outcome.append(coordinator.launch(coordinator.LaunchRequest("run", worker_root)))
        except LaunchFenced as exc:
            outcome.append(exc)

    thread = threading.Thread(target=dispatch)
    thread.start()
    both.wait()
    if order == "worker first":
        thread.join(timeout=60)  # The worker owns the host before the command starts.
    result = CliRunner().invoke(cli.app, argv)
    thread.join(timeout=60)
    if order == "worker first":
        assert result.exit_code == 2, result.output
    try:
        worker_started = (
            float((worker_root / "entered").read_text())
            if (worker_root / "entered").exists() else None
        )
        if result.exit_code == 2:
            # The worker won; the command started nothing.
            assert entered == []
            assert "already executing" in result.stderr or "cannot verify" in result.stderr
            until(lambda: (worker_root / "entered").exists())
        else:
            assert result.exit_code == 0, result.output
            assert len(entered) == 1
            if isinstance(outcome[0], LaunchFenced):
                assert worker_started is None  # The command fenced it before it claimed.
            else:
                # It took the host only once the command released it.
                until(lambda: (worker_root / "entered").exists())
                started = float((worker_root / "entered").read_text())
                assert started >= entered[0][1]
    finally:
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
