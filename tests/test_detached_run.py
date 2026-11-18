"""`run` returns once a background worker owns the host for its launch; the worker keeps running."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml
from typer.testing import CliRunner

import pluginbench
from fake_systemd import FakeSystemd
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import launches, ticket, until
from pluginbench import cli, control_store, coordinator, invocation_runtime, run_lock
from pluginbench.control_store import ControlStore
from pluginbench.isolation import RuntimeDriftError

_SOURCE = str(Path(pluginbench.__file__).parents[1])

# Stands in for `pluginbench run RUN --launch ID`: claims its launch, records the PATH it got,
# and owns the run until stopped, checking for a recorded stop before each job it would admit.
# It records what stopped it. PB_TEST_STOP_POLL slows its stop wake-up.
_WORKER = """
import os, sys, time
from pathlib import Path
from pluginbench import control_store, run_lock
control_store.STOP_POLL = float(os.environ.get("PB_TEST_STOP_POLL", control_store.STOP_POLL))
state, root, launch_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
(root / "worker-path").write_text(os.environ["PATH"])
owner = run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root), launch_id=launch_id)
try:
    with control_store.execute(owner, state=state), run_lock.hold(root):
        while not (root / "stop-at-once").exists():
            run_lock.check_stop()
            time.sleep(0.02)
except KeyboardInterrupt as stopped:
    (root / "stopped-by").write_text(type(stopped).__name__)
"""


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "plan_resume", lambda root: {"coding_calls": 3, "review_calls": 0})


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path) -> None:
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [
            sys.executable, "-c", _WORKER, str(isolated_host_state), str(request.target), launch_id
        ],
    )
    monkeypatch.setenv("PYTHONPATH", f"{_SOURCE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")


def _start_and_stop(root: Path, state: Path, *, signal: bool = True) -> tuple[str, str]:
    """Start the worker with `run`, then stop it with `stop`; return the start's output and the
    worker's cgroup. Without `signal`, the stop's SIGTERM is lost: only its record remains."""
    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes"])
    assert result.exit_code == 0, result.output
    owner = run_lock.owner(root)
    assert owner and owner["pid"] != os.getpid()
    cgroup = invocation_runtime.cgroup_of(int(str(owner["pid"]))) or ""
    until(lambda: (root / "worker-path").is_file())
    stopped = CliRunner().invoke(cli.app, ["stop", str(root)])
    assert stopped.exit_code == 0, stopped.output
    until(lambda: run_lock.owner(root) is None)
    until(lambda: {launch.status for launch in launches(state).values()} == {"finished"})
    return result.stdout, cgroup


@pytest.mark.real_operations
def test_run_returns_while_the_background_worker_continues_until_stopped(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path, planned: None,
    worker: None,
) -> None:
    output, cgroup = _start_and_stop(tmp_path, isolated_host_state)
    (launch,) = launches(isolated_host_state).values()
    assert f"in scope {launch.scope}" in output
    assert cgroup.endswith(f"/{launch.scope}")
    assert f"pluginbench stop {tmp_path}" in output
    assert f"pluginbench status {tmp_path} --watch" in output
    assert f"pluginbench resume {tmp_path}" in output
    assert launch.desired == "stop"


@pytest.mark.real_operations
def test_a_stop_whose_signal_is_lost_still_stops_the_background_worker(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path, planned: None,
    worker: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Neither `stop`'s SIGTERM nor the worker's own wake-up arrives: only the record can stop it.
    monkeypatch.setattr(run_lock, "stop", lambda root: None)
    monkeypatch.setenv("PB_TEST_STOP_POLL", "3600")
    _start_and_stop(tmp_path, isolated_host_state)
    (launch,) = launches(isolated_host_state).values()
    assert launch.desired == "stop"
    assert (tmp_path / "stopped-by").read_text() == "StopRequested"


@pytest.mark.systemd
def test_background_run_leaves_the_launcher_cgroup_and_keeps_its_environment(
    user_manager: None, isolated_host_state: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, planned: None, worker: None,
) -> None:
    # Ending the launcher's cgroup, as an agent session restart does, must not end the run.
    path = f"{tmp_path / 'launcher-bin'}{os.pathsep}{os.environ['PATH']}"
    monkeypatch.setenv("PATH", path)
    run = tmp_path / "run"
    run.mkdir()
    output, cgroup = _start_and_stop(run, isolated_host_state)
    (launch,) = launches(isolated_host_state).values()
    assert cgroup.endswith(f"/{launch.scope}")
    assert cgroup != invocation_runtime.cgroup_of(os.getpid())
    assert (run / "worker-path").read_text() == path


@pytest.mark.real_operations
def test_a_host_without_systemd_refuses_the_run_before_anything_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path, worker: None
) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(cli, "plan_resume", lambda root: pytest.fail("planned without systemd"))
    run = tmp_path / "run"
    run.mkdir()
    result = CliRunner().invoke(cli.app, ["run", str(run), "--yes"])
    assert result.exit_code == 2
    assert "systemd-run or systemctl is not installed" in result.stderr
    assert not (run / "worker-path").exists()
    assert not (isolated_host_state / "control.sqlite").exists() or not launches(
        isolated_host_state
    )


@pytest.mark.real_operations
def test_a_host_without_systemd_refuses_a_config_before_it_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path
) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "eval.yaml").write_text("{}")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(cli, "load_evaluation", lambda path: pytest.fail("loaded without systemd"))
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path / "eval.yaml"), "--yes"])
    assert result.exit_code == 2
    assert "systemd-run or systemctl is not installed" in result.stderr


@pytest.mark.real_operations
def test_a_command_that_takes_the_host_as_run_releases_it_fences_the_background_run(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, planned: None, worker: None,
) -> None:
    detach = cli._detach

    def another_command_first(*args: Any, **kwargs: Any) -> None:
        other = run_lock.ExecutionOwner(kind="review", eval="/other", root="/other")
        with control_store.execute(other):
            pass
        detach(*args, **kwargs)

    monkeypatch.setattr(cli, "_detach", another_command_first)
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes"])
    assert result.exit_code == 2
    assert "another command took the host first" in result.stderr
    assert not (tmp_path / "worker-path").exists()
    assert [launch.status for launch in launches(isolated_host_state).values()] == ["finished"]


@pytest.mark.real_operations
def test_background_run_that_exits_before_it_starts_fails_the_command(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, planned: None,
) -> None:
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [sys.executable, "-c", "raise SystemExit(5)"],
    )
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes"])
    assert result.exit_code == 2
    assert "exited with code 5 before it started" in result.stderr
    assert [launch.status for launch in launches(isolated_host_state).values()] == ["finished"]


@pytest.mark.real_operations
@pytest.mark.parametrize("phase", ["failed", "complete"])
def test_a_background_run_that_ends_before_run_sees_it_is_reported_by_how_it_ended(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, planned: None, worker: None, phase: str,
) -> None:
    # The worker takes run.lock, then ends at once.
    (tmp_path / "stop-at-once").touch()
    monkeypatch.setattr(cli, "run_status", lambda root: {"phase": phase})
    start = invocation_runtime.start_gated

    def descheduled(*args: object, **kwargs: object) -> object:
        proc = start(*args, **kwargs)  # type: ignore[arg-type]
        until(lambda: [launch.status for launch in launches(isolated_host_state).values()]
              == ["finished"])
        return proc

    monkeypatch.setattr(invocation_runtime, "start_gated", descheduled)
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes"])
    assert (tmp_path / "worker-path").exists()
    if phase == "complete":
        assert result.exit_code == 0, result.output
        assert f"The background run for {tmp_path} already finished." in result.stdout
    else:
        assert result.exit_code == 2
        assert f"the background run for {tmp_path} has already ended (failed)" in result.stderr
    assert "before it took" not in result.output


def test_foreground_run_owns_the_lock_and_writes_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: None
) -> None:
    owners = []

    def plan(root: Path) -> dict[str, object]:
        # The run is owned before planning, so a background start cannot race its parent.
        owners.append(run_lock.owner(root))
        return {"coding_calls": 3, "review_calls": 0}

    def work(root: Path, *, progress: Callable[[str], None], guard: Callable[[], None]) -> Path:
        owners.append(run_lock.owner(root))
        guard()
        progress("Coding 1/3: task · baseline · repeat 1")
        return root

    monkeypatch.setattr(cli, "plan_resume", plan)
    monkeypatch.setattr(cli, "resume_evaluation", work)
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes", "--foreground"])
    assert result.exit_code == 0, result.output
    assert len(owners) == 2 and all(owner and owner["pid"] == os.getpid() for owner in owners)
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["started", "progress", "finished"]
    assert events[1]["message"] == "Coding 1/3: task · baseline · repeat 1"
    assert run_lock.owner(tmp_path) is None


def test_the_background_command_runs_this_package() -> None:
    request = coordinator.LaunchRequest("run", Path("/run"), notify=True)
    argv = coordinator._worker_argv(request, "launch-1")
    assert argv[:4] == [sys.executable, "-m", "pluginbench", "run"]
    assert argv[4:] == ["/run", "--yes", "--foreground", "--notify", "--launch", "launch-1"]
    version = subprocess.run(
        [sys.executable, "-m", "pluginbench", "--version"],
        env={**os.environ, "PYTHONPATH": _SOURCE},
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == pluginbench.__version__


@pytest.mark.parametrize(
    "failure,unexpected,exit_code",
    [
        (RuntimeError("worker broke"), True, 1),
        (json.JSONDecodeError("empty receipt", "", 0), True, 2),
        (ValueError("invalid evaluation"), False, 2),
        (OSError("missing input"), False, 2),
        (yaml.YAMLError("invalid YAML"), False, 2),
        (subprocess.SubprocessError("command failed"), False, 2),
        (RuntimeDriftError("runtime changed"), False, 2),
        (KeyboardInterrupt("stop"), False, 130),
    ],
)
def test_worker_failure_logs_unexpected_tracebacks_and_keeps_terminal_short(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planned: None,
    failure: BaseException,
    unexpected: bool,
    exit_code: int,
) -> None:
    def broken_worker(
        root: Path, *, progress: Callable[[str], None], guard: Callable[[], None]
    ) -> Path:
        raise failure

    monkeypatch.setattr(cli, "resume_evaluation", broken_worker)
    # The detached worker uses the same foreground command, with output redirected.
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes", "--foreground"])
    assert result.exit_code == exit_code
    assert "Traceback" not in result.output
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    ended = events[-1]
    assert ended["event"] == ("stopped" if isinstance(failure, KeyboardInterrupt) else "failed")
    if not isinstance(failure, KeyboardInterrupt):
        assert ended["error"] == f"{type(failure).__name__}: {failure}"
        assert f"Error: {failure}" in result.stderr
    if unexpected:
        assert "Traceback (most recent call last):" in ended["traceback"]
        assert "broken_worker" in ended["traceback"]
        assert type(failure).__name__ in ended["traceback"]
        assert str(failure) not in ended["traceback"]
    else:
        assert "traceback" not in ended
    assert run_lock.owner(tmp_path) is None


@pytest.mark.parametrize("explicit_cause", [True, False])
def test_worker_tracebacks_keep_chain_locations_without_messages_or_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: None, explicit_cause: bool
) -> None:
    credential = "synthetic-credential-for-traceback-regression"

    def failed_operation() -> NoReturn:
        raise ValueError(credential)

    def broken_worker(
        root: Path, *, progress: Callable[[str], None], guard: Callable[[], None]
    ) -> Path:
        try:
            failed_operation()
        except ValueError as cause:
            cause.add_note(credential)
            failure = RuntimeError("worker failed")
            failure.add_note(credential)
            if explicit_cause:
                raise failure from cause
            raise failure

    monkeypatch.setattr(cli, "resume_evaluation", broken_worker)
    CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes", "--foreground"])
    log = (tmp_path / "events.jsonl").read_text()
    assert credential not in log
    ended = json.loads(log.splitlines()[-1])
    assert ended["event"] == "failed"
    assert ended["error"] == "RuntimeError: worker failed"
    frames = ended["traceback"]
    for name in ("RuntimeError", "ValueError", "broken_worker", "failed_operation", "_work"):
        assert name in frames
    assert str(Path(__file__)) in frames
    assert "line " in frames
    assert "worker failed" not in frames
    assert "raise failure" not in frames


@pytest.mark.parametrize("fenced", [False, True])
def test_a_worker_run_claims_its_launch_and_a_fenced_one_starts_nothing(
    isolated_host_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    planned: None, fenced: bool,
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, status="fenced" if fenced else "launching")
    statuses: list[str] = []

    def work(root: Path, *, progress: Callable[[str], None], guard: Callable[[], None]) -> Path:
        statuses.append(launches(isolated_host_state)[launch.launch_id].status)
        return root

    monkeypatch.setattr(cli, "resume_evaluation", work)
    result = CliRunner().invoke(
        cli.app, ["run", str(tmp_path), "--yes", "--foreground", "--launch", launch.launch_id]
    )
    if fenced:
        assert result.exit_code == 2
        assert "is fenced" in result.stderr
        assert statuses == []
    else:
        assert result.exit_code == 0, result.output
        assert statuses == ["active"]
    expected = "fenced" if fenced else "finished"
    assert launches(isolated_host_state)[launch.launch_id].status == expected
