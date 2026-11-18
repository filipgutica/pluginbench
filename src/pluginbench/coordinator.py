"""The coordinator: one per account, outside any terminal, watching this host's execution.

`ensure_started` takes `coordinator.lock` without waiting, records a launch for the coordinator,
and starts it behind a startup gate in its own systemd scope, `pb-coord-<launch>.scope`. The
lock's descriptor is handed to it, so the same open file keeps the lock with no unlock between.
The coordinator checks that it holds the lock through that descriptor, claims its launch, and
publishes its identity; the starter returns once that identity is verified and runs in the scope.
A second starter waits for that identity and returns it. The scope outlives the terminal and
the process that started it. A coordinator that misses its handshake is stopped, never left.

The coordinator runs no model and starts no container. Each POLL seconds it:
- watches the host execution lock for a holder it cannot verify (`control_store.OwnerWatch`);
- adopts the worker that owns the host for a current launch of this version: it records that
  launch as the one it monitors, and never starts it again;
- when the lock is free and something may be left (a worker launch that ended without
  finishing, one not claimed by its deadline, or an owner record never released) takes the lock
  as `recovery`, which verifies all of it before anything else may start (`control_store.execute`).

`stop` persists the request on the coordinator's own launch, then signals only its verified
process. Workers keep running in their own scopes, and the next coordinator adopts them.

`launch` starts a worker the same way, in `pb-worker-<launch>.scope`; the worker takes the host
execution lock itself and claims its launch only then (`control_store.execute`).
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from pluginbench import control_store, invocation_runtime, run_lock
from pluginbench.control_store import ControlStore, Launch, LaunchFenced

COORDINATOR_LOCK = "coordinator.lock"
COORDINATOR_OWNER = "coordinator-owner.json"
COORDINATOR_LOG = "coordinator.log"
# Seconds between the coordinator's checks.
POLL = 0.25
# Seconds before a recovery that could not finish is tried again; doubled after each failure,
# up to RECOVERY_BACKOFF_LIMIT, so a blocked host is not held from its operator.
RECOVERY_RETRY = 30.0
RECOVERY_BACKOFF_LIMIT = 960.0
# Seconds `stop` waits for the coordinator to exit.
STOP_WAIT = 10.0
# The execution kinds a worker can be launched for.
WORKER_KINDS = ("run",)


class CoordinatorUnavailable(ValueError):
    """The coordinator could not be started, or cannot be identified."""


class _Running(Exception):
    """Another process holds the coordinator lock."""


@dataclass(frozen=True)
class LaunchRequest:
    """A worker to start in the background: an execution `kind` for `target`."""

    kind: str
    target: Path
    initiator: str = "cli"

    def __post_init__(self) -> None:
        if self.kind not in WORKER_KINDS:
            raise ValueError(f"no worker is launched for {self.kind}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def identity(state: Path | None = None) -> dict[str, Any] | None:
    """The running coordinator's published identity, once verified: its process, on this boot
    and with its start time, holds the coordinator lock through its recorded descriptor."""
    state = state or run_lock.state_root()
    try:
        record = json.loads((state / COORDINATOR_OWNER).read_text())
        lock = os.stat(state / COORDINATOR_LOCK)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not run_lock.holds(record, lock):
        return None
    return record


def _scope_command(launcher: str, scope: str, argv: list[str]) -> list[str]:
    return [
        launcher, "--user", "--scope", "--collect", "--quiet", "--no-ask-password",
        f"--unit={scope}", "--expand-environment=no", "--", *argv,
    ]


def _abandon(state: Path, launch_id: str, *, stop_claimed: bool = False) -> Launch | None:
    """Fence a launch that missed its handshake, then stop its bootstrap and scope and prove
    them gone. Raises control_store.CleanupBlocked when they cannot be proven gone.

    A launch its process claimed before the fence started after all: it is returned, and left
    running, unless `stop_claimed`."""
    with ControlStore.open(state) as store:
        fenced = store.fence(launch_id)
        launch = store.launch(launch_id)
    if launch is None:
        return None
    if not fenced and launch.process is not None and not stop_claimed:
        return launch
    invocation_runtime.verify_scope(
        launch.operation(), deadline=time.monotonic() + invocation_runtime.CLEANUP_SECONDS
    )
    with ControlStore.open(state) as store:
        store.finish(launch_id)
    return None


def _new_launch(store: ControlStore, launch: Launch) -> None:
    if launch.kind == "coordinator":
        # Only the holder of the coordinator lock gets here, so any other coordinator is gone.
        for old in store.launches("launching", "active", "fenced"):
            if old.kind == "coordinator":
                store.finish(old.launch_id)
    store.create_launch(launch)


def ensure_started(state: Path | None = None) -> dict[str, Any]:
    """Return the coordinator's identity, starting it in the background if none runs."""
    state = state or run_lock.state_root()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = run_lock.named_flock(state / COORDINATOR_LOCK, _Running)
    except _Running:
        return _await(state)
    try:
        return _start(state, fd)
    finally:
        # The coordinator holds the same open file now; closing this copy unlocks nothing.
        os.close(fd)


def _await(state: Path) -> dict[str, Any]:
    """The identity of a coordinator another starter is starting, or already runs."""
    deadline = time.monotonic() + invocation_runtime.GATE_SECONDS
    while (found := identity(state)) is None:
        if time.monotonic() > deadline:
            lock = state / COORDINATOR_LOCK
            raise CoordinatorUnavailable(
                f"{lock} is held by a process that published no coordinator identity within "
                f"{invocation_runtime.GATE_SECONDS:g} seconds. Find it with `lslocks | grep "
                f"{lock}` and `ps`; do not delete the lock file"
            )
        time.sleep(0.05)
    return found


def _start(state: Path, fd: int) -> dict[str, Any]:
    launcher = invocation_runtime.require_user_manager()
    launch_id = uuid4().hex
    scope = f"pb-coord-{launch_id}.scope"
    argv = [sys.executable, "-m", "pluginbench.coordinator", "serve", str(state), launch_id,
            str(fd)]
    with ControlStore.open(state) as store:
        _new_launch(store, _ticket(launch_id, "coordinator", "app", state, scope))
        proc = invocation_runtime.start_gated(
            _scope_command(launcher, scope, argv),
            output=state / COORDINATOR_LOG,
            admit=partial(store.record_bootstrap, launch_id),
            cwd=state,
            pass_fds=(fd,),
        )
    deadline = time.monotonic() + invocation_runtime.GATE_SECONDS
    failure = "its launch was ended before it started"
    while proc is not None:
        found = identity(state)
        if found is not None and found.get("launch_id") == launch_id:
            cgroup = invocation_runtime.cgroup_of(int(found["pid"])) or ""
            if cgroup.rsplit("/", 1)[-1] == scope:
                return found
            failure = f"process {found['pid']} does not run in scope {scope}"
            break
        if (code := proc.poll()) is not None:
            failure = f"it exited with code {code}"
            break
        if time.monotonic() > deadline:
            failure = f"it did not start within {invocation_runtime.GATE_SECONDS:g} seconds"
            break
        time.sleep(0.05)
    with ControlStore.open(state) as store:
        store.request_stop(launch_id)
    _abandon(state, launch_id, stop_claimed=True)
    raise CoordinatorUnavailable(
        f"the coordinator did not start: {failure}, so it was stopped. See "
        f"{state / COORDINATOR_LOG}"
    )


def _ticket(launch_id: str, kind: str, initiator: str, target: Path, scope: str) -> Launch:
    return Launch(
        launch_id=launch_id,
        kind=kind,
        initiator=initiator,
        target=str(target),
        version=run_lock.execution_version(),
        scope=scope,
        boot_id=run_lock.boot_id(),
        expires=time.monotonic() + invocation_runtime.GATE_SECONDS,
    )


def run_foreground(state: Path | None = None) -> int:
    """Run the coordinator in this process, under the same singleton, until stopped."""
    state = state or run_lock.state_root()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)

    def running() -> CoordinatorUnavailable:
        found = identity(state)
        where = f" (process {found['pid']})" if found else ""
        return CoordinatorUnavailable(f"a coordinator already runs on this host{where}")

    fd = run_lock.named_flock(state / COORDINATOR_LOCK, running)
    try:
        launch_id = uuid4().hex
        with ControlStore.open(state) as store:
            _new_launch(store, _ticket(launch_id, "coordinator", "app", state, scope=""))
        return serve(state, launch_id, fd)
    finally:
        os.close(fd)


def serve(state: Path, launch_id: str, lock_fd: int) -> int:
    """Be the coordinator for `launch_id`, holding the coordinator lock through `lock_fd`."""
    pid = os.getpid()
    record: dict[str, Any] = {
        "pid": pid,
        "start_time": run_lock.process_start(pid),
        "boot_id": run_lock.boot_id(),
        "lock_fd": lock_fd,
        "launch_id": launch_id,
        "version": run_lock.execution_version(),
        "started_at": _now(),
        "adopted": None,
    }
    if not run_lock.holds(record, os.stat(state / COORDINATOR_LOCK)):
        print("the coordinator lock was not handed to this process; exiting", flush=True)
        return 1
    os.set_inheritable(lock_fd, False)  # Never handed on to a worker.
    with ControlStore.open(state) as store:
        launch = store.launch(launch_id)
        process = control_store.launched_process(launch.scope if launch else "")
        if not store.claim(launch_id, process):
            print(f"launch {launch_id} is no longer current; exiting", flush=True)
            return 75
    run_lock.write_atomic(state / COORDINATOR_OWNER, record)
    _log(f"coordinator {launch_id} started, process {pid}")
    stopping = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: stopping.set())
    try:
        _Coordinator(state, launch_id, record).run(stopping)
    finally:
        signal.signal(signal.SIGTERM, previous)
        with ControlStore.open(state) as store:
            store.finish(launch_id)
        _log(f"coordinator {launch_id} stopped")
    return 0


def _log(message: str) -> None:
    print(f"{_now()} {message}", flush=True)


class _Coordinator:
    def __init__(self, state: Path, launch_id: str, record: dict[str, Any]) -> None:
        self.state = state
        self.launch_id = launch_id
        self.record = record
        self.watch = control_store.OwnerWatch(state)
        self.retry_at = 0.0
        self.backoff = RECOVERY_RETRY
        # The owner record a recovery found only an operator can clear; set until it changes.
        self.awaiting_repair: str | None = None

    def run(self, stopping: threading.Event) -> None:
        """Check every POLL seconds until stopped. A check that fails is logged and retried,
        waiting twice as long each time up to RECOVERY_RETRY: nothing restarts a coordinator
        that exits, so it never exits on an error."""
        delay = POLL
        while not stopping.is_set():
            try:
                if self.stop_requested():
                    return
                self.tick()
                delay = POLL
            except Exception as exc:
                delay = min(delay * 2, RECOVERY_RETRY)
                _log(f"a check failed, retrying in {delay:g}s: {type(exc).__name__}: {exc}")
            stopping.wait(delay)

    def stop_requested(self) -> bool:
        with ControlStore.open(self.state) as store:
            launch = store.launch(self.launch_id)
        return launch is None or launch.desired == "stop" or launch.status != "active"

    def tick(self) -> None:
        self.watch.poll()
        snapshot = run_lock.execution_owner(self.state)
        with ControlStore.open(self.state) as store:
            workers = [
                launch
                for launch in store.launches("launching", "active", "fenced")
                if launch.kind != "coordinator"
            ]
        if snapshot.verified:
            self._adopt(snapshot.record or {}, workers)
        elif not snapshot.held and time.monotonic() >= self.retry_at:
            if self.awaiting_repair is not None and self.awaiting_repair == _fingerprint(
                snapshot.record
            ):
                return  # Only an operator clears it; a new owner record means one acted.
            due = [launch for launch in workers if launch.status != "launching" or launch.expired]
            if due or _unreleased(snapshot.record):
                self._recover()

    def _adopt(self, owner: dict[str, object], workers: list[Launch]) -> None:
        """Monitor the worker that owns the host for an active launch of this version. One of
        another version is only observed: never adopted, signalled or replaced."""
        launch_id = owner.get("launch_id")
        if launch_id is None or launch_id == self.record["adopted"]:
            return
        current = next(
            (launch for launch in workers
             if launch.launch_id == launch_id and launch.status == "active"),
            None,
        )
        if current is None or owner.get("version") != run_lock.execution_version():
            return
        self.record = {**self.record, "adopted": launch_id}
        run_lock.write_atomic(self.state / COORDINATOR_OWNER, self.record)
        _log(f"adopted launch {launch_id}: {current.kind} for {current.target}")

    def _recover(self) -> None:
        owner = run_lock.ExecutionOwner(
            kind="recovery", eval=str(self.state), root=str(self.state)
        )
        try:
            with control_store.execute(owner, state=self.state):
                pass
        except control_store.CleanupUnknown as exc:
            # Holding the lock to retry would only refuse the operator's own repair.
            self.awaiting_repair = _fingerprint(run_lock.execution_owner(self.state).record)
            _log(f"recovery needs the operator, so it is not retried until they act: {exc}")
            return
        except (control_store.CleanupBlocked, run_lock.ExecutionBusy, OSError,
                sqlite3.Error) as exc:
            self.retry_at = time.monotonic() + self.backoff
            _log(f"recovery could not finish, retrying in {self.backoff:g}s: {exc}")
            self.backoff = min(self.backoff * 2, RECOVERY_BACKOFF_LIMIT)
            return
        self.backoff = RECOVERY_RETRY
        self.awaiting_repair = None
        _log("recovered: every worker launch is settled and the last owner verified")


def _fingerprint(record: dict[str, object] | None) -> str:
    return json.dumps(record, sort_keys=True)


def _unreleased(record: dict[str, object] | None) -> bool:
    return record is not None and (record.get("state") != "released" or bool(record.get("pending")))


def stop(state: Path | None = None) -> dict[str, Any] | None:
    """Stop the coordinator: persist the request, then SIGTERM only its verified process, and
    wait for it to exit. Workers keep running. Returns the identity stopped, or None."""
    state = state or run_lock.state_root()
    found = identity(state)
    if found is None:
        return None
    with ControlStore.open(state) as store:
        store.request_stop(str(found["launch_id"]))
    _signal(found, signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT
    while identity(state) is not None:
        if time.monotonic() > deadline:
            raise CoordinatorUnavailable(
                f"the coordinator (process {found['pid']}) did not exit within {STOP_WAIT:g}s"
            )
        time.sleep(0.05)
    return found


def _signal(found: dict[str, Any], signum: signal.Signals) -> None:
    """Send `signum` to the identified process, only while its start time still matches."""
    pid = int(found["pid"])
    try:
        handle = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        # The pidfd pins the process, so the start time read now is its own.
        if run_lock.started_at(pid) == found["start_time"]:
            signal.pidfd_send_signal(handle, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(handle)


def status(state: Path | None = None) -> dict[str, Any]:
    """What an observer sees of the coordinator and the host execution lock. It changes no
    record; opening the control database writes only to bring an older one up to date."""
    state = state or run_lock.state_root()
    snapshot = run_lock.execution_owner(state)
    reconciling, launches = None, []
    if (state / control_store.CONTROL_NAME).exists():
        with ControlStore.open(state) as store:
            reconciling = store.reconciling()
            launches = [asdict(launch) for launch in store.launches("launching", "active", "fenced")]
    return {
        "coordinator": identity(state),
        "execution": {"held": snapshot.held, "verified": snapshot.verified,
                      "owner": snapshot.record},
        "reconciling": reconciling,
        "launches": launches,
    }


def _worker_argv(request: LaunchRequest, launch_id: str) -> list[str]:
    return [sys.executable, "-m", "pluginbench", request.kind, str(request.target.resolve()),
            "--yes", "--foreground", "--launch", launch_id]


def _holds_host(state: Path, launch_id: str) -> bool:
    """Whether a verified owner of the host execution lock names `launch_id`: its worker passed
    the handshake and claims the launch once its recovery ends."""
    snapshot = run_lock.execution_owner(state)
    return snapshot.verified and (snapshot.record or {}).get("launch_id") == launch_id


# How long a worker that holds the host may take to claim its launch: its recovery of the last
# owner's obligations, then the settling of other launches.
CLAIM_SECONDS = control_store.RECOVERY_SECONDS + invocation_runtime.CLEANUP_SECONDS


def launch(request: LaunchRequest, state: Path | None = None) -> Launch:
    """Start a worker for `request` in its own scope; return its launch once it claimed it.

    The worker takes the host execution lock itself, so a command that takes it first wins:
    the launch is then fenced, the worker exits without work, and this raises LaunchFenced.
    The handshake is the worker holding the host for this launch within the startup gate; one
    that misses it is stopped, never left. A worker that holds the host claims only once it has
    recovered the last owner, so it gets CLAIM_SECONDS more; one that still has not claimed is
    fenced, never stopped while it holds the lock, and exits without work once it checks.
    """
    state = state or run_lock.state_root()
    launcher = invocation_runtime.require_user_manager()
    target = request.target.resolve()
    launch_id = uuid4().hex
    scope = f"pb-worker-{launch_id}.scope"
    ticket = _ticket(launch_id, request.kind, request.initiator, target, scope)
    with ControlStore.open(state) as store:
        try:
            store.create_launch(ticket)
        except sqlite3.IntegrityError as exc:
            raise run_lock.ExecutionBusy("the app is already starting a worker") from exc
        try:
            proc = invocation_runtime.start_gated(
                _scope_command(launcher, scope, _worker_argv(request, launch_id)),
                output=target / "run.log",
                admit=partial(store.record_bootstrap, launch_id),
            )
        except BaseException:
            _abandon(state, launch_id)
            raise
        failure = "it was fenced before it started: another command took the host first"
        holding_until: float | None = None
        while proc is not None:
            current = store.launch(launch_id)
            assert current is not None
            if current.process is not None:
                return current  # Claimed: running, or already finished.
            if current.status != "launching":
                failure = "another command took the host execution lock first"
                break
            if (code := proc.poll()) is not None:
                failure = f"it exited with code {code} before it started; see {target / 'run.log'}"
                break
            if holding_until is None and _holds_host(state, launch_id):
                holding_until = time.monotonic() + CLAIM_SECONDS
            if holding_until is None and current.expired:
                if _holds_host(state, launch_id):
                    # It took the host just as its gate passed: await its claim, never stop it.
                    holding_until = time.monotonic() + CLAIM_SECONDS
                    continue
                failure = (
                    f"it did not start within {invocation_runtime.GATE_SECONDS:g} seconds; see "
                    f"{target / 'run.log'}"
                )
                break
            if holding_until is not None and time.monotonic() > holding_until:
                if store.fence(launch_id):
                    raise LaunchFenced(
                        f"the background {request.kind} for {target} held the host but did not "
                        f"claim its launch within {CLAIM_SECONDS:g} seconds, so its launch was "
                        "fenced: it exits without work once it checks"
                    )
                continue  # It claimed meanwhile, or another command fenced it.
            time.sleep(0.05)
    started = _abandon(state, launch_id)
    if started is not None:
        return started
    raise LaunchFenced(f"the background {request.kind} for {target} did not start: {failure}")


def main(argv: list[str]) -> int:
    """The coordinator's own entry point in its scope: `serve STATE LAUNCH_ID LOCK_FD`."""
    if len(argv) != 4 or argv[0] != "serve":
        print("usage: python -m pluginbench.coordinator serve STATE LAUNCH_ID LOCK_FD",
              file=sys.stderr)
        return 2
    return serve(Path(argv[1]), argv[2], int(argv[3]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
