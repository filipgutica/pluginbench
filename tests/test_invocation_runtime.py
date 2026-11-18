"""Scoped operations: two startup gates, a deadline, and cleanup proven before the record clears.

The fake tier runs everywhere; `fake_systemd` stands in for the user manager and Docker is a fake.
Tests marked `systemd` use the real user manager and skip without one, unless
PLUGINBENCH_REQUIRE_SYSTEMD_TESTS=1 makes that a failure. No test starts a container or a model.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import pluginbench
from fake_systemd import HANDLERS, FakeSystemd, register_handlers
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from pluginbench import control_store, harbor_backend, invocation_runtime, run_lock
from pluginbench import ledger as invocation_ledger
from pluginbench.control_store import CleanupBlocked, CleanupUnknown
from pluginbench.invocation_runtime import (
    HostUnavailable,
    InvocationSpec,
    OperationDeadlineExceeded,
    OperationFailed,
    OperationRequest,
)

pytestmark = pytest.mark.real_operations

_SOURCE = str(Path(pluginbench.__file__).parents[1])
_RUNTIME: dict[str, object] = {"docker_engine": {"id": "daemon", "endpoint": "unix:///fake.sock"}}


def _owner(root: Path) -> run_lock.ExecutionOwner:
    return run_lock.ExecutionOwner(kind="run", eval=str(root), root=str(root))


def _request(
    operation: str, docker: dict[str, object] | None = None, **arguments: Any
) -> OperationRequest:
    return OperationRequest(
        operation=operation,
        arguments=arguments,
        timeout=float(arguments.pop("timeout", 30)),
        spec=InvocationSpec(uuid4().hex, docker),
    )


def _record(state: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((state / run_lock.EXECUTION_OWNER).read_text())
    return value


def _alive(pid: int) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return False
    return fields[0] != "Z"


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _pid(path: Path, within: float = 10.0) -> int:
    deadline = time.monotonic() + within
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)
    return int(path.read_text())


class _Docker:
    """A fake daemon holding containers by ID, each with its invocation label's value."""

    def __init__(self, containers: dict[str, str] | None = None) -> None:
        self.containers = dict(containers or {})
        self.calls: list[tuple[str, ...]] = []
        self.removes = True
        self.on_ps: Callable[[int], None] | None = None

    def __call__(self, *arguments: str, **_: Any) -> str:
        self.calls.append(arguments)
        if arguments[0] == "ps":
            if self.on_ps is not None:
                self.on_ps(sum(call[0] == "ps" for call in self.calls))
            wanted = arguments[-1].removeprefix("label=")
            return "".join(
                f"{key}\n"
                for key, value in self.containers.items()
                if f"{invocation_runtime.LABEL_KEY}={value}" == wanted
            )
        if arguments[0] == "inspect":
            return "".join(f"{self.containers[key]}\n" for key in arguments[3:])
        if arguments[0] == "rm" and self.removes:
            for key in arguments[3:]:
                self.containers.pop(key, None)
        return ""


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> _Docker:
    fake = _Docker()
    monkeypatch.setattr(harbor_backend, "_docker", fake)
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, timeout: None)
    return fake


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[run_lock.ExecutionLock]:
    with run_lock.hold_execution(_owner(tmp_path)) as lock:
        yield lock


def test_an_operation_runs_in_its_scope_and_returns_once_its_cleanup_is_proven(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, tmp_path: Path
) -> None:
    started: list[int] = []
    marker = tmp_path / "ran"
    request = _request("echo", value={"answer": 42}, marker=str(marker))

    value = invocation_runtime.run_operation(request, started=started.append)

    assert value == {"answer": 42}
    # The handler ran in the process the gate admitted, inside the operation's own scope.
    assert started == [int(marker.read_text())]
    assert request.spec.scope in (fake_systemd.root / "log").read_text()
    assert _record(owned.state)["operations"] == []
    directory = owned.state / "invocations" / request.spec.id
    assert json.loads((directory / "cleanup.json").read_text())["scope"]["unit"] == "not-found"
    assert not (directory / "request.json").exists()


def test_a_descendant_that_escaped_with_setsid_is_stopped_after_a_successful_operation(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, tmp_path: Path
) -> None:
    # processes.run_command stops only the process group, so such a descendant outlived it and
    # its owner recorded a clean release (#130, residual risk).
    pid_file = tmp_path / "escaped"

    assert invocation_runtime.run_operation(_request("escape", pid_file=str(pid_file))) == "done"

    assert _gone(_pid(pid_file), within=1)
    assert _record(owned.state)["operations"] == []


def test_containers_with_the_operation_label_are_removed_even_after_success(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, docker: _Docker
) -> None:
    request = _request("echo", _RUNTIME, value=1)
    docker.containers = {"mine": request.spec.id, "other": uuid4().hex}

    invocation_runtime.run_operation(request)

    assert docker.containers.keys() == {"other"}
    assert ("rm", "--force", "--volumes", "mine") in docker.calls


def test_the_entry_point_creates_containers_only_on_the_daemon_its_operation_recorded(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, docker: _Docker
) -> None:
    """Recovery finds an operation's containers on the daemon its owner recorded, so inside the
    scope that is the only daemon a container may start on, and none without one."""
    other = {"docker_engine": {"id": "other", "endpoint": "unix:///fake.sock"}}
    with_docker = _request("recorded", _RUNTIME, runtime=_RUNTIME)
    assert invocation_runtime.run_operation(with_docker) == with_docker.spec.id
    assert "another one" in invocation_runtime.run_operation(
        _request("recorded", _RUNTIME, runtime=other)
    )
    assert "recorded no Docker daemon" in invocation_runtime.run_operation(
        _request("recorded", runtime=_RUNTIME)
    )


def test_a_container_that_survives_removal_fences_the_host_until_a_later_check_removes_it(
    fake_systemd: FakeSystemd, docker: _Docker, tmp_path: Path, isolated_host_state: Path
) -> None:
    request = _request("echo", _RUNTIME, value=1)
    docker.containers = {"mine": request.spec.id}
    docker.removes = False

    with run_lock.hold_execution(_owner(tmp_path)) as lock:
        with pytest.raises(CleanupBlocked, match="remain: mine"):
            invocation_runtime.run_operation(request)
        # The caller swallows the error and its block returns.
        assert lock.leftover

    record = _record(isolated_host_state)
    assert record["state"] != "released"
    assert [item["id"] for item in record["operations"]] == [request.spec.id]
    assert [item["operation"] for item in record["leftovers"]] == [request.spec.id]
    # The next owner carries the operation forward and verifies it before anything starts.
    with pytest.raises(CleanupBlocked, match="remain: mine"):
        with control_store.execute(_owner(tmp_path / "unrelated")):
            pass
    # Once its container can be removed, that same check clears the fence, with no repair.
    docker.removes = True
    with control_store.execute(_owner(tmp_path / "unrelated")) as recovered:
        assert recovered.record["pending"] == []
    assert docker.containers == {}
    assert (isolated_host_state / "invocations" / request.spec.id / "cleanup.json").is_file()


def test_a_contained_owner_needs_only_its_operations_proof_not_its_runs_frozen_suite(
    fake_systemd: FakeSystemd, docker: _Docker, tmp_path: Path, isolated_host_state: Path
) -> None:
    """Every container a contained owner started carries a recorded operation's label, so its
    label inventory alone proves them gone, even with the run's frozen suite missing."""
    from pluginbench.ledger import LEDGER_NAME, Ledger

    run = tmp_path / "run"
    run.mkdir()
    Ledger.create(run / LEDGER_NAME, [], {}).close()
    with pytest.raises(RuntimeError, match="killed"):
        with run_lock.hold_execution(_owner(run)) as lock:
            lock.note_runtime(_RUNTIME)
            invocation_runtime.run_operation(_request("echo", _RUNTIME, value=1))
            raise RuntimeError("killed")  # Its owner never records a clean release.
    assert _record(isolated_host_state)["state"] != "released"

    with control_store.execute(_owner(tmp_path / "unrelated")) as lock:
        assert lock.record["pending"] == []


def test_an_interrupted_cleanup_keeps_the_owner_unreleased_though_the_interrupt_is_caught(
    fake_systemd: FakeSystemd,
    tmp_path: Path,
    isolated_host_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(operation: dict[str, Any], *, lock: run_lock.ExecutionLock) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(invocation_runtime, "verify_cleanup", interrupted)
    request = _request("echo", value=1)

    with run_lock.hold_execution(_owner(tmp_path)):
        with pytest.raises(KeyboardInterrupt):
            invocation_runtime.run_operation(request)

    # Nothing recorded a failed cleanup, yet the operation was never proven gone.
    record = _record(isolated_host_state)
    assert record["state"] != "released"
    assert [item["id"] for item in record["operations"]] == [request.spec.id]


def test_an_unverified_cleanup_replaces_the_handlers_retryable_error(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, docker: _Docker
) -> None:
    """A caller retries a ValueError and frees its slot; while a container of the operation
    remains, it gets CleanupBlocked, with the handler's error as its context."""
    request = _request("fail", _RUNTIME, message="boom")
    docker.containers = {"mine": request.spec.id}
    docker.removes = False

    with pytest.raises(CleanupBlocked, match="remain: mine") as caught:
        invocation_runtime.run_operation(request)

    assert isinstance(caught.value.__context__, ValueError)
    assert owned.leftover


def test_a_collect_inside_an_operation_runs_in_that_operations_scope(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, tmp_path: Path
) -> None:
    """A preflight's readiness probe collects inside the preflight's scope, whose fresh entry
    point holds no execution lock, so the collect cannot start an operation of its own."""
    config = tmp_path / "coverage.yaml"
    config.write_text("{}\n")
    request = _request(
        "nested_collect",
        config=str(config),
        root=str(tmp_path),
        destination=str(tmp_path / "collected"),
    )

    assert invocation_runtime.run_operation(request) == {"collected": True}


def test_the_handlers_progress_reaches_the_caller_and_a_long_line_never_ends_it(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock
) -> None:
    """A preflight's progress is what `check` and `run` print while they prepare."""
    long = "é" * 5000  # Longer than one message, even before JSON escapes it.
    lines: list[str] = []

    value = invocation_runtime.run_operation(
        _request("chatty", lines=["one", long, "two"]), progress=lines.append
    )

    assert value == 3
    assert lines[0] == "one" and lines[2] == "two"
    assert lines[1] and long.startswith(lines[1])


@pytest.mark.parametrize(
    ("kind", "raised", "text"),
    [
        ("ValueError", ValueError, "boom"),
        ("TimeoutExpired", subprocess.TimeoutExpired, "timed out after 7.0 seconds"),
        ("Unexpected", OperationFailed, "sitecustomize.Unexpected: boom"),
    ],
)
def test_a_handler_error_comes_back_as_its_callers_handle_it_after_cleanup(
    fake_systemd: FakeSystemd,
    owned: run_lock.ExecutionLock,
    kind: str,
    raised: type[Exception],
    text: str,
) -> None:
    """Callers handle a timeout, a ValueError or a collector's error by type, so these come
    back as themselves; any other is an infrastructure failure."""
    request = _request("fail", message="boom", kind=kind)

    with pytest.raises(raised, match=text) as caught:
        invocation_runtime.run_operation(request)

    assert type(caught.value) is raised
    if isinstance(caught.value, subprocess.TimeoutExpired):
        assert caught.value.cmd == ["worker", "boom"]
    assert (owned.state / "invocations" / request.spec.id / "traceback.txt").is_file()
    assert _record(owned.state)["operations"] == []


def test_an_operation_past_its_deadline_is_killed_with_its_escaped_children(
    fake_systemd: FakeSystemd,
    owned: run_lock.ExecutionLock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invocation_runtime, "KILL_GRACE", 0.5)
    pid_file = tmp_path / "escaped"
    begun = time.monotonic()

    with pytest.raises(OperationDeadlineExceeded, match="within 1 seconds"):
        # The handler ignores SIGTERM, so only SIGKILL after the grace ends it.
        invocation_runtime.run_operation(_request("hang", pid_file=str(pid_file), timeout=1))

    assert time.monotonic() - begun < 10
    assert _gone(_pid(pid_file), within=1)
    log = fake_systemd.log()
    assert any("--signal=SIGTERM" in line for line in log)
    assert any("--signal=SIGKILL" in line for line in log)
    assert _record(owned.state)["operations"] == []


def test_the_handler_never_runs_when_started_refuses_it(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"

    def refuse(pid: int) -> None:
        raise ValueError("ledger refused")

    with pytest.raises(ValueError, match="ledger refused"):
        invocation_runtime.run_operation(_request("echo", marker=str(marker)), started=refuse)

    time.sleep(0.5)
    assert not marker.exists()
    assert _record(owned.state)["operations"] == []


def test_the_handler_never_runs_when_the_lock_is_lost_during_started(
    fake_systemd: FakeSystemd, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    with run_lock.hold_execution(_owner(tmp_path)) as lock:

        def replace_lock(pid: int) -> None:
            path = lock.state / run_lock.EXECUTION_LOCK
            path.rename(path.with_suffix(".old"))
            path.touch()

        with pytest.raises(run_lock.ExecutionLost):
            invocation_runtime.run_operation(
                _request("echo", marker=str(marker)), started=replace_lock
            )

    time.sleep(0.5)
    assert not marker.exists()


def test_a_process_outside_the_scope_is_never_admitted(
    fake_systemd: FakeSystemd,
    owned: run_lock.ExecutionLock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invocation_runtime, "_cgroup", lambda pid: "/user.slice/elsewhere.scope")
    marker = tmp_path / "ran"

    with pytest.raises(OperationFailed, match="does not run in scope"):
        invocation_runtime.run_operation(_request("echo", marker=str(marker)))

    assert not marker.exists()


def test_a_scope_that_fails_to_start_fails_the_operation_within_the_gate(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(invocation_runtime, "GATE_SECONDS", 1.0)
    fake_systemd.flag("run-fails")

    with pytest.raises(OperationFailed, match="did not start within 1 seconds"):
        invocation_runtime.run_operation(_request("echo"))

    assert _record(owned.state)["operations"] == []


@pytest.mark.parametrize(
    "error", [TimeoutError("timed out"), run_lock.ExecutionLost("replaced")], ids=type
)
def test_an_error_after_the_scope_launched_leaves_the_call_spent(
    fake_systemd: FakeSystemd,
    owned: run_lock.ExecutionLock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """The gate's own failures, raised once the bootstrap started: no refund, whatever type."""

    def failing(*args: Any) -> Any:
        raise error

    monkeypatch.setattr(invocation_runtime, "_admit", failing)

    with pytest.raises(type(error)) as caught:
        invocation_runtime.run_operation(_request("echo"))

    assert invocation_ledger.outcome(caught.value, spawned=False)[0] == "failed_transient"


def test_a_launched_call_whose_cleanup_then_fails_stays_spent(
    fake_systemd: FakeSystemd,
    owned: run_lock.ExecutionLock,
    monkeypatch: pytest.MonkeyPatch,
    docker: _Docker,
) -> None:
    """Never admitted, so no spawn was reported, but its bootstrap ran: the cleanup failure
    that replaces the gate's error keeps the call spent, and unresolved."""
    request = _request("echo", _RUNTIME, value=1)
    docker.containers = {"mine": request.spec.id}
    docker.removes = False

    def stalled(*args: Any) -> Any:
        raise TimeoutError("timed out")

    monkeypatch.setattr(invocation_runtime, "_admit", stalled)

    with pytest.raises(CleanupBlocked, match="remain: mine") as caught:
        invocation_runtime.run_operation(request)

    assert invocation_ledger.outcome(caught.value, spawned=False)[0] == "unresolved"


def test_a_cleanup_that_fails_while_this_operation_launches_still_stops_its_handler(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Another slot's cleanup fails after `run_operation` checked, while this one starts its
    scope. The gate checks again before the second permit, so the handler never runs."""
    probe = invocation_runtime.require_user_manager

    def other_slot_fails_now() -> str:
        owned.note_leftover("cleanup of another invocation was not verified")
        return probe()

    monkeypatch.setattr(invocation_runtime, "require_user_manager", other_slot_fails_now)
    marker = tmp_path / "ran"

    with pytest.raises(CleanupBlocked, match="starts no more operations"):
        invocation_runtime.run_operation(_request("echo", value=1, marker=str(marker)))

    time.sleep(0.5)  # Long enough for a permitted handler to have written its marker.
    assert not marker.exists()


def test_an_owner_whose_cleanup_failed_launches_no_other_operation(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock, docker: _Docker, tmp_path: Path
) -> None:
    """A failed cleanup halts admission for the whole owner, so another slot's next
    operation, or an in-flight review's call, is refused before it launches."""
    failed = _request("echo", _RUNTIME, value=1)
    docker.containers = {"mine": failed.spec.id}
    docker.removes = False
    with pytest.raises(CleanupBlocked, match="remain: mine"):
        invocation_runtime.run_operation(failed)
    marker = tmp_path / "ran"

    with pytest.raises(CleanupBlocked, match="starts no more operations") as refused:
        invocation_runtime.run_operation(_request("echo", marker=str(marker)))

    assert not marker.exists()
    assert [item["id"] for item in _record(owned.state)["operations"]] == [failed.spec.id]
    # Nothing launched, so the refused call is given back to its slot.
    assert invocation_ledger.outcome(refused.value, spawned=False)[0] == "aborted_prespawn"


def test_lock_descriptors_never_reach_the_operation(
    fake_systemd: FakeSystemd, tmp_path: Path
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    with run_lock.hold_execution(_owner(run)) as lock, run_lock.hold(run):
        opened = invocation_runtime.run_operation(_request("descriptors"))

    assert str(lock.state / run_lock.EXECUTION_LOCK) not in opened
    assert str(run / run_lock.LOCK_NAME) not in opened


def test_an_unknown_operation_is_refused_before_anything_is_recorded(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock
) -> None:
    with pytest.raises(ValueError, match="unknown operation 'shell'"):
        _request("shell")

    assert _record(owned.state)["operations"] == []


def test_an_operation_needs_the_host_execution_lock(fake_systemd: FakeSystemd) -> None:
    with pytest.raises(RuntimeError, match="host execution lock"):
        invocation_runtime.run_operation(_request("echo"))


@pytest.mark.parametrize(
    ("flag", "content", "message"),
    [
        ("bus-down", "", "not reachable"),
        ("manager", "offline", "not reachable"),
        ("version", "systemd 254 (old)", "systemd 255 or later"),
    ],
    ids=["unreachable bus", "manager offline", "systemd too old"],
)
def test_a_host_without_a_usable_user_manager_starts_nothing(
    fake_systemd: FakeSystemd,
    owned: run_lock.ExecutionLock,
    flag: str,
    content: str,
    message: str,
) -> None:
    fake_systemd.flag(flag, content)

    with pytest.raises(HostUnavailable, match=message):
        invocation_runtime.run_operation(_request("echo"))

    assert _record(owned.state)["operations"] == []
    assert not (owned.state / "invocations").exists()


def test_a_degraded_but_reachable_manager_is_usable(
    fake_systemd: FakeSystemd, owned: run_lock.ExecutionLock
) -> None:
    fake_systemd.flag("manager", "degraded")

    assert invocation_runtime.run_operation(_request("echo", value=1)) == 1


def test_a_host_without_systemd_starts_nothing(
    fake_systemd: FakeSystemd, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(HostUnavailable, match="not installed"):
        invocation_runtime.require_user_manager()


def _operation(spec: InvocationSpec, **fields: Any) -> dict[str, Any]:
    return {
        "id": spec.id, "scope": spec.scope, "label": spec.label, "docker": spec.docker,
        "dir": "/nonexistent", "bootstrap": None, "payload": None, **fields,
    }


def _deadline() -> float:
    return time.monotonic() + invocation_runtime.CLEANUP_SECONDS


def test_an_unreachable_bus_is_unknown_never_a_missing_scope(fake_systemd: FakeSystemd) -> None:
    operation = _operation(InvocationSpec(uuid4().hex))
    assert invocation_runtime.verify_scope(operation, deadline=_deadline())["unit"] == "not-found"

    fake_systemd.flag("bus-down")

    with pytest.raises(CleanupUnknown, match="Failed to connect to bus"):
        invocation_runtime.verify_scope(operation, deadline=_deadline())


def test_a_scope_that_will_not_stop_is_escalated_then_blocks(
    fake_systemd: FakeSystemd, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(invocation_runtime, "STOP_ESCALATION", 0.2)
    monkeypatch.setattr(invocation_runtime, "SCOPE_SECONDS", 1.0)
    spec = InvocationSpec(uuid4().hex)
    (fake_systemd.root / "units" / spec.scope).write_text("")
    survivor = subprocess.Popen(["sleep", "60"], env={**os.environ, "PB_FAKE_SCOPE": spec.scope})
    calls: list[tuple[str, ...]] = []
    systemctl = invocation_runtime._systemctl

    def recorded(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return systemctl(*arguments, timeout=timeout)

    monkeypatch.setattr(invocation_runtime, "_systemctl", recorded)
    fake_systemd.flag("stuck")
    try:
        with pytest.raises(CleanupBlocked, match="still runs 1 seconds after its stop"):
            invocation_runtime.verify_scope(_operation(spec), deadline=_deadline())
        assert ("kill", "--kill-whom=all", "--signal=SIGKILL", spec.scope) in calls
    finally:
        survivor.kill()
        survivor.wait()


def test_a_recorded_bootstrap_is_killed_by_identity_before_the_scope_is_checked(
    fake_systemd: FakeSystemd,
) -> None:
    bootstrap = subprocess.Popen(["sleep", "60"])
    identity = invocation_runtime._identity(bootstrap.pid)
    try:
        proof = invocation_runtime.verify_scope(
            _operation(InvocationSpec(uuid4().hex), bootstrap=identity), deadline=_deadline()
        )
        assert proof["bootstrap"] == "killed"
        assert bootstrap.wait(timeout=5) == -signal.SIGKILL
    finally:
        bootstrap.kill()
        bootstrap.wait()


def test_a_reused_bootstrap_pid_is_never_signalled(fake_systemd: FakeSystemd) -> None:
    unrelated = subprocess.Popen(["sleep", "60"])
    try:
        identity = {**invocation_runtime._identity(unrelated.pid), "start_time": "1"}
        proof = invocation_runtime.verify_scope(
            _operation(InvocationSpec(uuid4().hex), bootstrap=identity), deadline=_deadline()
        )
        assert proof["bootstrap"] == "gone"
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait()


@pytest.mark.parametrize(
    ("break_docker", "error"),
    [
        (lambda fake, mp: mp.setattr(fake, "removes", False), CleanupBlocked),
        (
            lambda fake, mp: mp.setattr(harbor_backend, "_assert_daemon", _changed_daemon),
            CleanupUnknown,
        ),
        (lambda fake, mp: mp.setattr(harbor_backend, "_docker", _unreachable), CleanupUnknown),
        (lambda fake, mp: mp.setattr(fake, "on_ps", _late_create(fake)), CleanupBlocked),
    ],
    ids=["survives removal", "changed daemon", "docker fails", "created late"],
)
def test_labelled_containers_are_clean_only_after_two_empty_inventories_of_the_recorded_daemon(
    docker: _Docker,
    monkeypatch: pytest.MonkeyPatch,
    break_docker: Callable[[_Docker, pytest.MonkeyPatch], None],
    error: type[Exception],
) -> None:
    spec = InvocationSpec("a" * 32, _RUNTIME)
    docker.containers = {"mine": spec.id}
    proof = invocation_runtime.verify_labelled(_operation(spec), deadline=_deadline())
    assert proof["removed"] == ["mine"]

    docker.containers = {"mine": spec.id}
    docker.calls.clear()
    break_docker(docker, monkeypatch)

    with pytest.raises(error):
        invocation_runtime.verify_labelled(_operation(spec), deadline=_deadline())


def _changed_daemon(runtime: object, timeout: int) -> None:
    raise ValueError("Docker daemon changed; cannot accept export or cleanup proof")


def _unreachable(*arguments: str, **_: Any) -> str:
    raise ValueError("docker ps failed: Cannot connect to the Docker daemon")


def _late_create(fake: _Docker) -> Callable[[int], None]:
    def create(count: int) -> None:
        if count == 2:  # The first inventory after the removal.
            fake.containers["late"] = "a" * 32

    return create


def test_an_operation_without_docker_needs_no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harbor_backend, "_docker", _unreachable)

    operation = _operation(InvocationSpec("b" * 32))
    proof = invocation_runtime.verify_labelled(operation, deadline=_deadline())

    assert proof == {"docker": "not used"}


@pytest.mark.parametrize(
    "change",
    [
        {"id": "not-hex"},
        {"scope": "user.slice"},
        {"label": "pluginbench.invocation=other"},
        {"docker": "unix:///var/run/docker.sock"},
        {"bootstrap": {"pid": "7"}},
    ],
    ids=["id", "scope", "label", "docker", "bootstrap"],
)
def test_a_malformed_operation_becomes_the_repairable_fence(
    isolated_host_state: Path, tmp_path: Path, change: dict[str, Any]
) -> None:
    operation = {**_operation(InvocationSpec("c" * 32)), **change}
    record = {
        "schema": 2, "pid": 7, "acquired_at": "earlier", "kind": "run", "root": str(tmp_path),
        "state": "released", "operations": [operation], "pending": [],
    }
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(json.dumps(record))

    with run_lock.hold_execution(_owner(tmp_path)) as lock:
        pending = lock.record["pending"]

    assert [item["kind"] for item in pending] == ["unknown"]
    assert "has an operation that" in pending[0]["problem"]


_OWNER_SCRIPT = """
import os, signal, sys
from pathlib import Path
from pluginbench import invocation_runtime, run_lock

state, root, boundary, operation, pid_file, marker, invocation = sys.argv[1:8]
invocation_runtime.GATE_SECONDS = 5.0
if "FAKE_SYSTEMD" in os.environ:
    from fake_systemd import fake_cgroups

    for name, value in fake_cgroups(Path(os.environ["FAKE_SYSTEMD"])).items():
        setattr(invocation_runtime, name, value)


def die(*_):
    os.kill(os.getpid(), signal.SIGKILL)


update = run_lock.ExecutionLock.update_operation


def update_then_die(self, invocation, **fields):
    update(self, invocation, **fields)
    if boundary in fields:
        die()


run_lock.ExecutionLock.update_operation = update_then_die
owner = run_lock.ExecutionOwner(kind="run", eval=root, root=root)
with run_lock.hold_execution(owner, state=Path(state)):
    invocation_runtime.run_operation(
        invocation_runtime.OperationRequest(
            operation=operation,
            arguments={"pid_file": pid_file, "marker": marker},
            timeout=300,
            spec=invocation_runtime.InvocationSpec(invocation),
        ),
        started=die if boundary == "started" else None,
    )
"""


def _killed_owner(
    state: Path, root: Path, *, boundary: str, operation: str, tmp_path: Path
) -> tuple[str, subprocess.Popen[bytes]]:
    """An owner that SIGKILLs itself at `boundary`, or that the test kills during the call."""
    invocation = uuid4().hex
    tests = Path(__file__).parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (HANDLERS, tests, _SOURCE)))}
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER_SCRIPT, str(state), str(root), boundary, operation,
         str(tmp_path / "escaped"), str(tmp_path / "ran"), invocation],
        env=env,
    )
    return invocation, owner


def _recover(state: Path, tmp_path: Path, invocation: str) -> None:
    """Run the next execution, as any command does: the killed owner's checks alone clear it,
    and its operation's cleanup is proven on disk."""
    with control_store.execute(_owner(tmp_path / "unrelated"), state=state) as lock:
        assert lock.record["pending"] == []
    proof = json.loads((state / "invocations" / invocation / "cleanup.json").read_text())
    assert proof["id"] == invocation


@pytest.mark.parametrize("boundary", ["bootstrap", "started"])
def test_an_owner_killed_inside_a_startup_gate_never_runs_the_handler(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path, boundary: str
) -> None:
    invocation, owner = _killed_owner(
        isolated_host_state, tmp_path, boundary=boundary, operation="echo", tmp_path=tmp_path
    )
    assert owner.wait(timeout=30) == -signal.SIGKILL
    [recorded] = _record(isolated_host_state)["operations"]
    assert recorded["id"] == invocation

    _recover(isolated_host_state, tmp_path, invocation)

    assert fake_systemd.units() == []
    time.sleep(1)
    assert not (tmp_path / "ran").exists()


def test_an_owner_killed_during_a_call_has_its_scope_and_escaped_children_stopped_by_recovery(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path
) -> None:
    sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)
    invocation, owner = _killed_owner(
        isolated_host_state, tmp_path, boundary="none", operation="hang", tmp_path=tmp_path
    )
    try:
        escaped = _pid(tmp_path / "escaped")
        owner.kill()
        owner.wait()
        assert _alive(escaped)  # Its owner's death left it running.

        _recover(isolated_host_state, tmp_path, invocation)

        assert _gone(escaped)
        assert fake_systemd.units() == []
        assert sibling.poll() is None  # Not in the scope, so never signalled.
    finally:
        sibling.kill()
        sibling.wait()


# Real systemd: the scope is a real cgroup, so a setsid descendant cannot leave it.


@pytest.fixture
def user_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        invocation_runtime.require_user_manager()
    except HostUnavailable as exc:
        if os.environ.get("PLUGINBENCH_REQUIRE_SYSTEMD_TESTS") == "1":
            pytest.fail(f"a systemd user manager is required: {exc}")
        pytest.skip(f"no systemd user manager: {exc.reason}")
    monkeypatch.setenv("PYTHONPATH", f"{HANDLERS}{os.pathsep}{_SOURCE}")
    register_handlers(monkeypatch)


@pytest.mark.systemd
def test_a_real_scope_stops_a_descendant_that_outlives_a_successful_operation(
    user_manager: None, owned: run_lock.ExecutionLock, tmp_path: Path
) -> None:
    request = _request("escape", pid_file=str(tmp_path / "escaped"))

    assert invocation_runtime.run_operation(request) == "done"

    assert _gone(_pid(tmp_path / "escaped"))
    show = subprocess.run(
        ["systemctl", "--user", "show", "-p", "LoadState", request.spec.scope],
        capture_output=True, text=True, check=True,
    )
    assert show.stdout.strip() == "LoadState=not-found"


@pytest.mark.systemd
def test_a_real_scope_left_by_a_killed_owner_is_stopped_by_recovery(
    user_manager: None, isolated_host_state: Path, tmp_path: Path
) -> None:
    sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)
    invocation, owner = _killed_owner(
        isolated_host_state, tmp_path, boundary="none", operation="hang", tmp_path=tmp_path
    )
    try:
        escaped = _pid(tmp_path / "escaped")
        owner.kill()
        owner.wait()
        assert _alive(escaped)

        _recover(isolated_host_state, tmp_path, invocation)

        assert _gone(escaped)
        assert sibling.poll() is None
    finally:
        sibling.kill()
        sibling.wait()
        subprocess.run(
            ["systemctl", "--user", "stop", f"pb-inv-{invocation}.scope"],
            capture_output=True, check=False,
        )
