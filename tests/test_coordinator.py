"""The coordinator singleton, background launches, adoption and recovery.

Scopes are fake (`fake_systemd`) except in the tests marked `systemd`. Workers are stand-ins
that take the host execution lock for their launch and record each time their body runs; none
starts a container or a model.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import pluginbench
from fake_systemd import FakeSystemd, members
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import SLEEP, launches, until
from pluginbench import coordinator, invocation_runtime, run_lock
from pluginbench.control_store import LaunchFenced

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
