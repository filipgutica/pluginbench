"""The launch protocol: whose launch may claim the host, and how a persisted stop reaches it.

Scopes are fake (`fake_systemd`). No test starts a container or a model.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from fake_systemd import FakeSystemd, members
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import SLEEP, in_scope, launches, ticket, until
import pluginbench
from pluginbench import cli, control_store, invocation_runtime, parallel, run_lock
from pluginbench.control_store import ControlStore, LaunchFenced
from typer.testing import CliRunner

pytestmark = pytest.mark.real_operations



def test_a_stale_worker_exits_before_it_touches_another_launch(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        stale = ticket(store, status="fenced")
        current = ticket(store)
    waiting = in_scope(current.scope)
    try:
        owner = run_lock.ExecutionOwner("run", "/run", "/run", launch_id=stale.launch_id)
        with pytest.raises(LaunchFenced, match="is fenced"):
            with control_store.execute(owner):
                pytest.fail("a stale launch ran")
        assert launches(isolated_host_state)[current.launch_id].status == "launching"
        assert members(current.scope)
    finally:
        waiting.kill()
        waiting.wait()


def test_a_command_that_takes_the_host_first_fences_a_launching_worker_and_stops_it(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        pending = ticket(store)
    waiting = in_scope(pending.scope)
    try:
        with control_store.execute(run_lock.ExecutionOwner("review", "/other", "/other")):
            assert launches(isolated_host_state)[pending.launch_id].status == "finished"
            assert members(pending.scope) == []
        owner = run_lock.ExecutionOwner("run", "/run", "/run", launch_id=pending.launch_id)
        with pytest.raises(LaunchFenced):
            with control_store.execute(owner):
                pytest.fail("a fenced launch ran")
    finally:
        waiting.kill()
        waiting.wait()


def test_a_fenced_launch_is_never_permitted_to_start(
    isolated_host_state: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, status="fenced")
        proc = invocation_runtime.start_gated(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            output=tmp_path / "log",
            admit=partial(store.record_bootstrap, launch.launch_id),
        )
    assert proc is None
    assert not marker.exists()
    assert launches(isolated_host_state)[launch.launch_id].bootstrap is None


def _owner(path: Path, launch_id: str) -> run_lock.ExecutionOwner:
    return run_lock.ExecutionOwner("run", str(path), str(path), launch_id=launch_id)


def test_a_bootstrap_recorded_after_the_list_is_stopped_by_the_settle(
    fake_systemd: FakeSystemd, isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        pending = ticket(store)
    # A permitted bootstrap whose systemd-run has not made the scope yet: in no scope.
    bootstrap = subprocess.Popen([SLEEP, "300"], start_new_session=True)
    fence = ControlStore.fence

    def recorded_first(self: ControlStore, launch_id: str) -> bool:
        # Its launcher records the bootstrap after the owner listed the launches.
        assert self.record_bootstrap(launch_id, invocation_runtime.process_identity(bootstrap.pid))
        return fence(self, launch_id)

    monkeypatch.setattr(ControlStore, "fence", recorded_first)
    try:
        with control_store.execute(run_lock.ExecutionOwner("review", "/other", "/other")):
            assert launches(isolated_host_state)[pending.launch_id].status == "finished"
            assert bootstrap.wait(timeout=10) == -signal.SIGKILL
    finally:
        if bootstrap.poll() is None:
            bootstrap.kill()
            bootstrap.wait()


@pytest.mark.parametrize("why", ["fenced", "stop requested", "another version", "claimed"])
def test_only_a_launching_wanted_launch_of_this_version_can_be_claimed(
    isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, status="fenced" if why == "fenced" else "launching")
        if why == "stop requested":
            assert store.request_stop(launch.launch_id)
        if why == "claimed":
            assert store.claim(launch.launch_id, {"pid": 1})
        if why == "another version":
            monkeypatch.setattr(run_lock, "execution_version", lambda: "0.0.0+other")
        assert store.claim(launch.launch_id, {"pid": 2}) is False
        assert store.launch(launch.launch_id).process != {"pid": 2}  # type: ignore[union-attr]


def test_a_launch_fenced_while_its_owner_settles_is_never_claimed(
    isolated_host_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store)
    settle = control_store._settle_launches

    def fenced_meanwhile(store: ControlStore, own: str | None) -> None:
        settle(store, own)
        assert store.fence(launch.launch_id)  # As by a launcher that gave up on it.

    monkeypatch.setattr(control_store, "_settle_launches", fenced_meanwhile)
    with pytest.raises(LaunchFenced, match="is fenced"):
        with control_store.execute(_owner(tmp_path, launch.launch_id)):
            pytest.fail("a fenced launch ran")
    assert launches(isolated_host_state)[launch.launch_id].status == "fenced"


def test_a_stop_whose_signal_is_lost_admits_no_further_job_and_admitted_ones_finish(
    isolated_host_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wake-up never comes: only the recorded stop can stop the run. These jobs commit no
    # intent, as a measurement phase's do not.
    monkeypatch.setattr(control_store, "STOP_POLL", 3600.0)
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, scope="pb-worker-test.scope")
    both = threading.Barrier(2, timeout=10)
    admitted: list[int] = []
    finished: list[int] = []

    def job(slot: int, cancelled: threading.Event, emit: Any) -> None:
        admitted.append(slot)
        if slot < 2:
            both.wait()  # Two jobs run at once.
            if slot == 0:
                with ControlStore.open(isolated_host_state) as store:
                    store.request_stop(launch.launch_id)
            both.wait()
        finished.append(slot)

    with pytest.raises(run_lock.StopRequested):
        with control_store.execute(_owner(tmp_path, launch.launch_id)):
            parallel.run_jobs(range(6), jobs=2, operation=job)
    assert sorted(admitted) == sorted(finished) == [0, 1]
    assert launches(isolated_host_state)[launch.launch_id].status == "finished"


def test_after_a_stop_an_admitted_job_s_operations_still_run(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As after `stop`'s signal: an attempt past admission finishes, such as its measurement
    # after coding, so its paid call is kept and never made again on resume.
    monkeypatch.setattr(control_store, "STOP_POLL", 3600.0)
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, scope="pb-worker-test.scope")
    marker = tmp_path / "ran"
    request = invocation_runtime.OperationRequest(
        operation="echo",
        arguments={"value": 1, "marker": str(marker)},
        timeout=30.0,
        spec=invocation_runtime.InvocationSpec(uuid4().hex, None),
    )
    with control_store.execute(_owner(tmp_path, launch.launch_id)):
        with ControlStore.open(isolated_host_state) as store:
            store.request_stop(launch.launch_id)
        assert invocation_runtime.run_operation(request) == 1
    assert marker.exists()


_UNGUARDED = """
import sys, time
from pathlib import Path
from pluginbench import control_store, run_lock
state, root, launch_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
owner = run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root), launch_id=launch_id)
try:
    with control_store.execute(owner, state=state):
        (root / "ready").touch()
        time.sleep(60)  # No run_lock.hold here, so nothing else turns SIGTERM into an interrupt.
except KeyboardInterrupt:
    print("interrupted")
"""


def test_the_stop_wake_up_interrupts_an_owner_outside_a_run_lock_rather_than_killing_it(
    isolated_host_state: Path, tmp_path: Path
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, scope="pb-worker-test.scope")
    source = str(Path(pluginbench.__file__).parents[1])
    worker = subprocess.Popen(
        [sys.executable, "-c", _UNGUARDED, str(isolated_host_state), str(tmp_path),
         launch.launch_id],
        stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": f"{source}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )
    until(lambda: (tmp_path / "ready").exists())
    with ControlStore.open(isolated_host_state) as store:
        store.request_stop(launch.launch_id)
    output, _ = worker.communicate(timeout=30)
    assert worker.returncode == 0 and "interrupted" in output
    assert launches(isolated_host_state)[launch.launch_id].status == "finished"


def test_a_failed_permit_after_the_bootstrap_started_counts_as_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def admit(identity: dict[str, object]) -> bool:
        def broken(fd: int, data: bytes) -> int:
            raise OSError("the permit could not be written")

        monkeypatch.setattr(os, "write", broken)
        return True

    with pytest.raises(OSError, match="permit") as raised:
        invocation_runtime.start_gated(
            [sys.executable, "-c", "pass"], output=tmp_path / "log", admit=admit
        )
    assert getattr(raised.value, "launched", False) is True


@pytest.mark.parametrize(
    "arguments",
    [["{config}", "--yes", "--foreground"], ["{run}", "--yes"], ["{run}", "--yes", "--dry-run",
                                                                  "--foreground"]],
)
def test_launch_is_refused_unless_a_run_directory_runs_in_the_foreground(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    (tmp_path / "eval.yaml").write_text("{}")
    monkeypatch.setattr(cli, "plan_resume", lambda root: pytest.fail("planned"))
    monkeypatch.setattr(cli, "load_evaluation", lambda path: pytest.fail("loaded"))
    argv = [part.format(config=tmp_path / "eval.yaml", run=tmp_path) for part in arguments]
    result = CliRunner().invoke(cli.app, ["run", *argv, "--launch", "launch-1"])
    assert result.exit_code == 2
    assert "--launch names a background worker's launch" in result.stderr


def test_opening_a_current_control_store_writes_nothing(isolated_host_state: Path) -> None:
    with ControlStore.open(isolated_host_state):
        pass  # Created at the current version.
    observer = sqlite3.connect(isolated_host_state / control_store.CONTROL_NAME)
    try:
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        with ControlStore.open(isolated_host_state) as store:
            store.launches("active")
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before
    finally:
        observer.close()
