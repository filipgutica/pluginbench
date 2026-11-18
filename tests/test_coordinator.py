"""The coordinator singleton, background launches, adoption and recovery.

Scopes are fake (`fake_systemd`) except in the tests marked `systemd`. Workers are stand-ins
that take the host execution lock for their launch and record each time their body runs; none
starts a container or a model.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import pluginbench
from fake_systemd import FakeSystemd, members
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import SLEEP, in_scope, launches, ticket, until
from pluginbench import control_store, coordinator, invocation_runtime, run_lock
from pluginbench.control_store import ControlStore, LaunchFenced

pytestmark = pytest.mark.real_operations

_TESTS = Path(__file__).parent
_SOURCE = Path(pluginbench.__file__).parents[1]

# Stands in for `pluginbench run RUN --launch ID`: owns the host for its launch, records that its
# body ran, and holds on until `release` exists.
_WORKER = """
import os, signal, sys, time
from pathlib import Path
from pluginbench import control_store, run_lock
state, root, launch_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if os.environ.get("PB_TEST_VERSION"):
    run_lock.execution_version = lambda: os.environ["PB_TEST_VERSION"]
if os.environ.get("PB_TEST_DIE_BEFORE_CLAIM"):
    control_store.ControlStore.claim = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
owner = run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root), launch_id=launch_id)
with control_store.execute(owner, state=state):
    with (root / "bodies").open("a") as stream:
        stream.write(f"{os.getpid()}\\n")
    while not (root / "release").exists():
        time.sleep(0.02)
"""

# A process that starts or launches through the coordinator module, with fake cgroups.
_CLIENT = """
import json, os, signal, sys, time
from pathlib import Path
from fake_systemd import fake_cgroups
from pluginbench import control_store, coordinator, invocation_runtime
state, fake, go, action = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
for name, value in fake_cgroups(fake).items():
    setattr(invocation_runtime, name, value)
invocation_runtime.GATE_SECONDS = 2.0
while not go.exists():
    time.sleep(0.005)
if action == "start":
    print(json.dumps(coordinator.ensure_started(state)), flush=True)
    sys.exit(0)
root, worker, point = Path(sys.argv[5]), sys.argv[6], action
coordinator._worker_argv = lambda request, launch_id: [
    sys.executable, "-c", worker, str(state), str(request.target), launch_id
]
def die(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
start = invocation_runtime.start_gated
if point == "before_spawn":
    invocation_runtime.start_gated = die
elif point == "before_bootstrap_record":
    control_store.ControlStore.record_bootstrap = die
elif point in ("after_permit", "before_claim"):
    def permitted(*args, **kwargs):
        start(*args, **kwargs)
        die()
    invocation_runtime.start_gated = permitted
coordinator.launch(coordinator.LaunchRequest("run", root), state=state)
"""


def _env(**extra: str) -> dict[str, str]:
    path = os.pathsep.join((str(_TESTS), str(_SOURCE), os.environ.get("PYTHONPATH", "")))
    return {**os.environ, "PYTHONPATH": path, **extra}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"


def _bodies(root: Path) -> list[str]:
    path = root / "bodies"
    return path.read_text().split() if path.exists() else []


@contextmanager
def _coordinator(state: Path) -> Iterator[dict[str, Any]]:
    found = coordinator.ensure_started(state)
    try:
        yield found
    finally:
        coordinator.stop(state)


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, isolated_host_state: Path) -> None:
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [
            sys.executable, "-c", _WORKER, str(isolated_host_state), str(request.target), launch_id
        ],
    )
    monkeypatch.setenv("PYTHONPATH", _env()["PYTHONPATH"])


# The singleton and its detached start.


def test_two_starters_at_once_share_one_coordinator_that_outlives_both(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    starters = [
        subprocess.Popen(
            [sys.executable, "-c", _CLIENT, str(isolated_host_state), str(fake_systemd.root),
             str(go), "start"],
            stdout=subprocess.PIPE, text=True, env=_env(),
        )
        for _ in range(2)
    ]
    go.touch()
    outputs = [starter.communicate(timeout=60)[0] for starter in starters]
    try:
        assert [starter.returncode for starter in starters] == [0, 0], outputs
        first, second = (json.loads(output) for output in outputs)
        assert first == second
        # Both starters have exited, as a closed terminal's would; the coordinator runs on.
        assert _alive(first["pid"])
        assert coordinator.identity(isolated_host_state) == first
        coordinators = [unit for unit in fake_systemd.units() if unit.startswith("pb-coord-")]
        assert coordinators == [f"pb-coord-{first['launch_id']}.scope"]
        assert first["pid"] in members(coordinators[0])
    finally:
        coordinator.stop(isolated_host_state)
    assert coordinator.identity(isolated_host_state) is None
    assert launches(isolated_host_state)[first["launch_id"]].status == "finished"


def test_a_stop_whose_signal_is_lost_still_ends_the_coordinator_and_no_worker(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    worker: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = coordinator.ensure_started(isolated_host_state)
    launched = coordinator.launch(coordinator.LaunchRequest("run", tmp_path), isolated_host_state)
    pid = int(launched.process["pid"]) if launched.process else 0
    monkeypatch.setattr(coordinator, "_signal", lambda found, signum: None)
    try:
        assert coordinator.stop(isolated_host_state) == found
        assert not _alive(found["pid"])
        # The worker runs in its own scope and keeps its launch.
        assert _alive(pid)
        assert launches(isolated_host_state)[launched.launch_id].status == "active"
    finally:
        (tmp_path / "release").touch()
    until(lambda: launches(isolated_host_state)[launched.launch_id].status == "finished")


def test_a_coordinator_that_misses_its_handshake_is_stopped_not_left(
    fake_systemd: FakeSystemd, isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(invocation_runtime, "GATE_SECONDS", 1.0)
    monkeypatch.setattr(
        coordinator,
        "_scope_command",
        lambda launcher, scope, argv: [launcher, "--user", "--scope", f"--unit={scope}", "--",
                                       SLEEP, "300"],
    )

    with pytest.raises(coordinator.CoordinatorUnavailable, match="did not start within 1"):
        coordinator.ensure_started(isolated_host_state)

    (launch,) = launches(isolated_host_state).values()
    assert launch.status == "finished"
    assert members(launch.scope) == []
    # The lock is free again: a next start can take it.
    fd = run_lock.named_flock(isolated_host_state / coordinator.COORDINATOR_LOCK, RuntimeError)
    os.close(fd)


# Launches killed part way, then settled by a coordinator.


@pytest.mark.parametrize(
    "point", ["before_spawn", "before_bootstrap_record", "after_permit", "before_claim"]
)
def test_a_launcher_killed_part_way_is_settled_by_the_coordinator_never_run_twice(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path, point: str
) -> None:
    go = tmp_path / "go"
    go.touch()
    run = tmp_path / "run"
    run.mkdir()
    env = _env(**({"PB_TEST_DIE_BEFORE_CLAIM": "1"} if point == "before_claim" else {}))
    launcher = subprocess.run(
        [sys.executable, "-c", _CLIENT, str(isolated_host_state), str(fake_systemd.root),
         str(go), point, str(run), _WORKER],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert launcher.returncode == -signal.SIGKILL, launcher.stderr
    (launch,) = launches(isolated_host_state).values()

    with _coordinator(isolated_host_state):
        if point == "after_permit":
            # The worker claimed its launch; the coordinator adopts it and starts nothing.
            until(lambda: (coordinator.identity(isolated_host_state) or {}).get("adopted")
                   == launch.launch_id)
            assert launches(isolated_host_state)[launch.launch_id].status == "active"
            (run / "release").touch()
        until(lambda: launches(isolated_host_state)[launch.launch_id].status == "finished")

    assert len(_bodies(run)) == (1 if point == "after_permit" else 0)
    assert members(launch.scope) == []
    assert run_lock.execution_owner(isolated_host_state).held is False


def test_a_restarted_coordinator_adopts_a_running_worker_and_recovers_it_once_killed(
    fake_systemd: FakeSystemd, isolated_host_state: Path, worker: None, tmp_path: Path
) -> None:
    with _coordinator(isolated_host_state) as first:
        launched = coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
        until(lambda: (coordinator.identity(isolated_host_state) or {}).get("adopted")
               == launched.launch_id)
    with _coordinator(isolated_host_state) as restarted:
        assert restarted["launch_id"] != first["launch_id"]
        until(lambda: (coordinator.identity(isolated_host_state) or {}).get("adopted")
               == launched.launch_id)
        assert len(_bodies(tmp_path)) == 1
        assert launched.process is not None
        os.kill(int(launched.process["pid"]), signal.SIGKILL)
        until(lambda: launches(isolated_host_state)[launched.launch_id].status == "finished")
        until(lambda: (run_lock.execution_owner(isolated_host_state).record or {}).get("state")
               == "released")
    assert len(_bodies(tmp_path)) == 1


def test_a_worker_of_another_version_never_claims_its_launch(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    worker: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PB_TEST_VERSION", "0.0.0+other")
    with pytest.raises(LaunchFenced, match="exited with code"):
        coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
    assert _bodies(tmp_path) == []
    assert [launch.status for launch in launches(isolated_host_state).values()] == ["finished"]


# The launcher's handshake.

# A worker whose recovery of the last owner outlasts the startup gate: it holds the host, and
# claims its launch only once the launch's gate has passed (the monotonic clock is shared).
_SLOW_RECOVERY = _WORKER.replace(
    "owner = run_lock.ExecutionOwner(",
    "import time as _t\n_settle = control_store._settle_launches\n"
    "def _slow(store, own):\n"
    "    while _t.monotonic() < store.launch(own).expires + 1:\n"
    "        _t.sleep(0.05)\n"
    "    _settle(store, own)\n"
    "control_store._settle_launches = _slow\n"
    "owner = run_lock.ExecutionOwner(",
)


def test_a_worker_still_recovering_after_the_gate_is_awaited_not_killed(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invocation_runtime, "GATE_SECONDS", 5.0)
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [
            sys.executable, "-c", _SLOW_RECOVERY, str(isolated_host_state), str(request.target),
            launch_id,
        ],
    )
    monkeypatch.setenv("PYTHONPATH", _env()["PYTHONPATH"])
    try:
        launched = coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
        assert launched.status == "active"
        until(lambda: len(_bodies(tmp_path)) == 1)
    finally:
        (tmp_path / "release").touch()
    until(lambda: launches(isolated_host_state)[launched.launch_id].status == "finished")
    assert (run_lock.execution_owner(isolated_host_state).record or {}).get("state") == "released"


# A worker that takes the host only once `go` exists, recovers until `landed` exists, and
# records whether its launch was fenced.
_LATE_TAKER = """
import sys, time
from pathlib import Path
from pluginbench import control_store, run_lock
state, root, launch_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
settle = control_store._settle_launches
def slow(store, own):
    while not (root / "landed").exists():
        time.sleep(0.01)
    settle(store, own)
control_store._settle_launches = slow
while not (root / "go").exists():
    time.sleep(0.01)
owner = run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root), launch_id=launch_id)
try:
    with control_store.execute(owner, state=state):
        (root / "bodies").write_text("ran\\n")
except control_store.LaunchFenced:
    (root / "outcome").write_text("fenced")
"""


def test_a_worker_that_takes_the_host_as_its_gate_passes_exits_by_itself(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invocation_runtime, "GATE_SECONDS", 1.0)
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [
            sys.executable, "-c", _LATE_TAKER, str(isolated_host_state), str(request.target),
            launch_id,
        ],
    )
    monkeypatch.setenv("PYTHONPATH", _env()["PYTHONPATH"])
    fence = ControlStore.fence

    def taken_first(store: ControlStore, launch_id: str) -> bool:
        # The gate has passed and the launcher missed the worker; the worker takes the host
        # now, and is still recovering when the launcher's fence lands.
        (tmp_path / "go").touch()
        until(lambda: (run_lock.execution_owner(isolated_host_state).record or {}).get(
            "launch_id") == launch_id)
        try:
            return fence(store, launch_id)
        finally:
            (tmp_path / "landed").touch()

    monkeypatch.setattr(ControlStore, "fence", taken_first)
    with pytest.raises(LaunchFenced, match="took the host just as its 1-second startup gate"):
        coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
    # It was not stopped holding the host: it failed its claim and exited by itself, cleanly.
    until(lambda: (tmp_path / "outcome").exists())
    assert (tmp_path / "outcome").read_text() == "fenced"
    assert _bodies(tmp_path) == []
    until(lambda: (run_lock.execution_owner(isolated_host_state).record or {}).get("state")
          == "released")


def test_abandoning_a_launch_its_worker_claimed_first_leaves_it_running(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store)
    worker = in_scope(launch.scope)
    try:
        with ControlStore.open(isolated_host_state) as store:
            assert store.claim(launch.launch_id, {"pid": worker.pid})
        started = coordinator._abandon(isolated_host_state, launch.launch_id)
        assert started is not None and started.status == "active"
        assert members(launch.scope)
    finally:
        worker.kill()
        worker.wait()


def test_a_launch_being_abandoned_can_no_longer_be_claimed(
    fake_systemd: FakeSystemd, isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store)
    worker = in_scope(launch.scope)
    claimed: list[bool] = []
    verify = invocation_runtime.verify_scope

    def claim_first(operation: Any, **kwargs: Any) -> Any:
        # The worker reaches its claim while its scope is being stopped.
        with ControlStore.open(isolated_host_state) as store:
            claimed.append(store.claim(launch.launch_id, {"pid": worker.pid}))
        return verify(operation, **kwargs)

    monkeypatch.setattr(invocation_runtime, "verify_scope", claim_first)
    try:
        assert coordinator._abandon(isolated_host_state, launch.launch_id) is None
        assert claimed == [False]
        assert members(launch.scope) == []
        assert launches(isolated_host_state)[launch.launch_id].status == "finished"
    finally:
        worker.kill()
        worker.wait()


def test_a_launch_whose_start_fails_is_ended_not_left_launching(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    worker: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("systemd-run could not be started")

    monkeypatch.setattr(invocation_runtime, "start_gated", broken)
    with pytest.raises(OSError, match="systemd-run"):
        coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
    assert [launch.status for launch in launches(isolated_host_state).values()] == ["finished"]


def test_a_worker_that_claims_and_finishes_before_the_launcher_looks_counts_as_started(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    worker: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "release").touch()  # Its body ends at once.
    start = invocation_runtime.start_gated

    def descheduled(*args: Any, **kwargs: Any) -> Any:
        proc = start(*args, **kwargs)
        until(lambda: [launch.status for launch in launches(isolated_host_state).values()]
              == ["finished"])
        return proc

    monkeypatch.setattr(invocation_runtime, "start_gated", descheduled)
    launched = coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
    assert launched.status == "finished" and launched.process is not None
    assert len(_bodies(tmp_path)) == 1


def test_a_launch_fenced_before_its_late_systemd_run_never_starts_its_worker(
    fake_systemd: FakeSystemd,
    isolated_host_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "ran"
    monkeypatch.setattr(
        coordinator,
        "_worker_argv",
        lambda request, launch_id: [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
    )
    start = invocation_runtime.start_gated

    def late(*args: Any, **kwargs: Any) -> Any:
        # A command takes the host before this launcher's systemd-run runs.
        with ControlStore.open(isolated_host_state) as store:
            (pending,) = store.launches("launching")
            assert store.fence(pending.launch_id)
        return start(*args, **kwargs)

    monkeypatch.setattr(invocation_runtime, "start_gated", late)
    with pytest.raises(LaunchFenced, match="fenced before it started"):
        coordinator.launch(coordinator.LaunchRequest("run", tmp_path))
    assert not marker.exists()
    (launch,) = launches(isolated_host_state).values()
    assert launch.status == "finished" and launch.bootstrap is None
    assert members(launch.scope) == []


# Adoption of a worker of another version.


def test_a_live_worker_of_another_version_is_neither_adopted_nor_signalled(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path
) -> None:
    other = "0.0.0+other"
    launch_id = uuid4().hex
    launch = coordinator._ticket(
        launch_id, "run", "app", tmp_path, f"pb-worker-{launch_id}.scope"
    )
    launch = dataclasses.replace(launch, version=other)
    with ControlStore.open(isolated_host_state) as store:
        store.create_launch(launch)
    # Started by a coordinator of that version; it claims and holds the host.
    older = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(isolated_host_state), str(tmp_path),
         launch.launch_id],
        env=_env(PB_TEST_VERSION=other),
    )
    try:
        until(lambda: len(_bodies(tmp_path)) == 1)
        with _coordinator(isolated_host_state):
            time.sleep(8 * coordinator.POLL)
            assert (coordinator.identity(isolated_host_state) or {}).get("adopted") is None
            assert older.poll() is None
            assert launches(isolated_host_state)[launch.launch_id].status == "active"
    finally:
        (tmp_path / "release").touch()
        older.wait(timeout=30)
    assert older.returncode == 0


# The coordinator process itself.


@pytest.fixture
def served(isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Run `serve` without its loop; records whether its lock descriptor was inheritable."""
    isolated_host_state.mkdir(parents=True, exist_ok=True)
    looped: list[bool] = []

    def run(self: Any, stopping: Any) -> None:
        looped.append(os.get_inheritable(self.record["lock_fd"]))

    monkeypatch.setattr(coordinator._Coordinator, "run", run)
    return looped


def _coordinator_ticket(state: Path) -> str:
    launch_id = uuid4().hex
    with ControlStore.open(state) as store:
        coordinator._new_launch(
            store, coordinator._ticket(launch_id, "coordinator", "app", state, scope="")
        )
    return launch_id


def test_serve_exits_unless_it_holds_the_coordinator_lock(
    isolated_host_state: Path, served: list[bool]
) -> None:
    launch_id = _coordinator_ticket(isolated_host_state)
    fd = os.open(isolated_host_state / coordinator.COORDINATOR_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert coordinator.serve(isolated_host_state, launch_id, fd) == 1
    finally:
        os.close(fd)
    assert served == []
    assert launches(isolated_host_state)[launch_id].status == "launching"
    assert coordinator.identity(isolated_host_state) is None


def test_serve_exits_when_its_launch_is_no_longer_current(
    isolated_host_state: Path, served: list[bool]
) -> None:
    launch_id = _coordinator_ticket(isolated_host_state)
    with ControlStore.open(isolated_host_state) as store:
        assert store.fence(launch_id)
    fd = run_lock.named_flock(isolated_host_state / coordinator.COORDINATOR_LOCK, RuntimeError)
    try:
        assert coordinator.serve(isolated_host_state, launch_id, fd) == 75
    finally:
        os.close(fd)
    assert served == []
    assert not (isolated_host_state / coordinator.COORDINATOR_OWNER).exists()


def test_serve_never_hands_its_lock_descriptor_on(
    isolated_host_state: Path, served: list[bool]
) -> None:
    launch_id = _coordinator_ticket(isolated_host_state)
    fd = run_lock.named_flock(isolated_host_state / coordinator.COORDINATOR_LOCK, RuntimeError)
    os.set_inheritable(fd, True)  # As passed through pass_fds.
    try:
        assert coordinator.serve(isolated_host_state, launch_id, fd) == 0
    finally:
        os.close(fd)
    assert served == [False]


def test_a_failing_check_is_logged_and_retried_and_never_ends_the_coordinator(
    isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checks: list[int] = []

    def tick(self: Any) -> None:
        checks.append(1)
        if len(checks) == 1:
            raise sqlite3.OperationalError("database is locked")
        with ControlStore.open(self.state) as store:
            store.request_stop(self.launch_id)

    monkeypatch.setattr(coordinator._Coordinator, "tick", tick)
    assert coordinator.run_foreground(isolated_host_state) == 0
    assert len(checks) == 2
    assert "database is locked" in capsys.readouterr().out


def _recovering(
    state: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> tuple[Any, list[str], list[dict[str, object]]]:
    """A coordinator facing a free host with an unreleased owner record, whose every recovery
    fails with `failure`. Returns it, the recoveries tried, and the owner records seen."""
    state.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = [{"state": "active", "pid": 1}]
    monkeypatch.setattr(
        run_lock,
        "execution_owner",
        lambda state=None: run_lock.LockSnapshot(
            Path("/unused"), held=False, record=records[-1], verified=False
        ),
    )
    tried: list[str] = []

    @contextmanager
    def refused(owner: run_lock.ExecutionOwner, *, state: Path | None = None) -> Iterator[None]:
        tried.append(owner.kind)
        raise failure
        yield

    monkeypatch.setattr(control_store, "execute", refused)
    found = coordinator._Coordinator(state, uuid4().hex, {"adopted": None})
    monkeypatch.setattr(found.watch, "poll", lambda: None)
    return found, tried, records


def test_a_recovery_that_cannot_finish_is_retried_ever_more_slowly(
    isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = control_store.CleanupBlocked("a container is still running")
    found, tried, _ = _recovering(isolated_host_state, monkeypatch, blocked)
    waits = []
    for _ in range(3):
        found.retry_at = 0.0
        found.tick()
        waits.append(round(found.retry_at - time.monotonic()))
    assert waits == [30, 60, 120]
    assert len(tried) == 3


def test_a_recovery_only_the_operator_can_clear_is_not_retried_until_they_act(
    isolated_host_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = control_store.CleanupUnknown("no verifiable process record")
    found, tried, records = _recovering(isolated_host_state, monkeypatch, unknown)
    for _ in range(3):
        found.retry_at = 0.0
        found.tick()
    assert tried == ["recovery"]
    records.append({"state": "released", "pending": [{"root": "/run"}]})  # The operator acted.
    found.tick()
    assert tried == ["recovery", "recovery"]


# Real systemd: the coordinator's own scope, and the descriptor it inherits.


@pytest.mark.systemd
def test_a_real_coordinator_keeps_its_lock_through_its_scope_and_outlives_its_terminal(
    user_manager: None, isolated_host_state: Path, tmp_path: Path
) -> None:
    terminal = f"pb-test-terminal-{uuid4().hex}.scope"
    starter = subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--collect", "--quiet", f"--unit={terminal}", "--",
         sys.executable, "-c",
         "import json, sys, time; from pathlib import Path; "
         "from pluginbench import coordinator; "
         "print(json.dumps(coordinator.ensure_started(Path(sys.argv[1]))), flush=True); "
         "time.sleep(300)",
         str(isolated_host_state)],
        stdout=subprocess.PIPE, text=True, env=_env(),
    )
    try:
        assert starter.stdout is not None
        found = json.loads(starter.stdout.readline())
        scope = f"pb-coord-{found['launch_id']}.scope"
        # Closing the terminal ends everything in its scope, but not the coordinator.
        subprocess.run(["systemctl", "--user", "stop", terminal], check=True, timeout=30)
        starter.wait(timeout=30)
        assert _alive(found["pid"])
        assert (invocation_runtime.cgroup_of(found["pid"]) or "").endswith(f"/{scope}")
        # Its inherited descriptor still holds the lock, on the same open file.
        assert coordinator.identity(isolated_host_state) == found
    finally:
        with suppress(Exception):
            coordinator.stop(isolated_host_state)
        subprocess.run(["systemctl", "--user", "stop", terminal], check=False, timeout=30)
        if starter.poll() is None:
            starter.kill()
            starter.wait()
    assert coordinator.identity(isolated_host_state) is None
