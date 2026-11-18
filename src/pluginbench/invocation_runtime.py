"""Run one operation in its own systemd scope behind two startup gates, then prove it left nothing.

`run_operation` needs the host execution lock and goes through these steps:
1. The owner record names the operation's scope, container label and Docker runtime before
   anything spawns, so a dead owner's successor can find what it may have left.
2. A bootstrap starts in a new session and waits for one permit byte on a pipe only this process
   can write. Its identity is recorded before the permit is sent. Without the permit, because
   this process died, it exits and runs nothing.
3. With the permit it execs `systemd-run --scope`, which runs this module's `execute` entry
   point in the operation's scope.
4. The entry point connects to this process's socket and says who it is. This process checks
   the peer's credentials and that it runs in the scope, records it, checks it still owns the
   host, and calls `started`. Only then does it send the second permit, and the handler runs.
5. However the operation ends, `verify_cleanup` stops the scope and removes the containers that
   carry its label, and both must be proven gone before the record drops the operation. A
   descendant that left its process group with `setsid` is still in the scope.

A handler past its deadline gets SIGTERM, and SIGKILL KILL_GRACE seconds later; the scope's own
RuntimeMaxSec is the same bound, should this process die first.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pluginbench import run_lock

# The operations a scope may run, as "module:function". Each takes the request's JSON arguments
# and returns a JSON value; `operations` holds them and their argument models. Nothing else runs
# in a scope: an unknown name is refused before anything spawns, and again by the entry point.
HANDLERS: dict[str, str] = {
    name: f"pluginbench.operations:{name}"
    for name in ("coding", "grading", "review", "collect", "preflight")
}

# Each startup gate's bound: the bootstrap's wait for its permit, and the entry point's
# connection and wait for the second permit.
GATE_SECONDS = 30.0
# Between SIGTERM at the operation's deadline and SIGKILL.
KILL_GRACE = 60.0
# Cleanup bounds: one query or control command, one container removal, the scope phase, the
# container phase, and the whole pass. Each command is capped by what its phase has left.
QUERY_SECONDS = 5.0
REMOVE_SECONDS = 20.0
SCOPE_SECONDS = 30.0
CONTAINER_SECONDS = 60.0
CLEANUP_SECONDS = 120.0
# A scope still loaded this long after its stop is killed with SIGKILL.
STOP_ESCALATION = 10.0
UNIT_POLL = 0.25
MINIMUM_SYSTEMD = 255
CGROUP_ROOT = Path("/sys/fs/cgroup")
LABEL_KEY = "pluginbench.invocation"

# The invocation whose handler this process runs, and the Docker runtime it recorded (None for
# an operation that starts no container); set once by the entry point, in the scope.
_current: str | None = None
_runtime: dict[str, object] | None = None
# The entry point's connection to its owner, for `report`, and the lock its senders share.
_channel: tuple[socket.socket, threading.Lock] | None = None
_CREATES = (("run",), ("create",), ("container", "run"), ("container", "create"))
# Handler exceptions the owner raises again as themselves, so callers keep handling them by
# type, beside builtin ones. Any other becomes OperationFailed.
_KNOWN_ERRORS = {
    "pluginbench.collectors.CollectorUnavailable": "pluginbench.collectors:CollectorUnavailable",
    "pluginbench.isolation.RuntimeDriftError": "pluginbench.isolation:RuntimeDriftError",
}

_MESSAGE_LIMIT = 4096
_PEER = struct.Struct("3i")
# Stdlib only, under -I: nothing on the path can run before the permit arrives.
_BOOTSTRAP = """
import os, select, sys
gate = int(sys.argv[1])
ready = select.select([gate], [], [], float(sys.argv[2]))[0]
if not ready or os.read(gate, 1) != b"1":
    sys.exit(75)
os.close(gate)
os.execv(sys.argv[3], sys.argv[3:])
"""


class HostUnavailable(ValueError):
    """This host cannot contain an operation: no reachable systemd user manager or cgroup v2."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"{reason}. PluginBench runs each model call and container in a systemd user scope, "
            "so nothing started. Restore the user manager (`systemctl --user "
            "is-system-running` must print running or degraded) on a host with cgroup v2 and "
            "systemd 255 or later, then run the command again"
        )
        self.reason = reason


class OperationFailed(ValueError):
    """The operation ended without a result: its handler raised an exception no caller handles
    by type, or it never got past a gate. Callers treat it as an infrastructure error.

    `type` names the handler's exception, or is empty for a launch failure. Either way its
    bootstrap had started, so the call counts as spent (`launched`, read by `ledger.outcome`)."""

    launched = True

    def __init__(self, message: str, *, type: str = "") -> None:
        super().__init__(f"{type}: {message}" if type else message)
        self.type = type
        self.reason = message


class OperationDeadlineExceeded(TimeoutError):
    """The operation did not return within its timeout, so its scope was killed."""


@dataclass(frozen=True)
class InvocationSpec:
    """One operation's containment: its scope and container label derive from `id`, a 32-digit
    hex ID such as a ledger invocation's. `docker` is the frozen runtime its containers start
    under, or None for an operation that starts none."""

    id: str
    docker: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not run_lock.is_invocation_id(self.id):
            raise ValueError(f"invocation ID {self.id!r} is not 32 hex digits")

    @property
    def scope(self) -> str:
        return f"pb-inv-{self.id}.scope"

    @property
    def label(self) -> str:
        return f"{LABEL_KEY}={self.id}"


@dataclass(frozen=True)
class OperationRequest:
    """An allowlisted operation, its JSON arguments, and its whole budget in seconds."""

    operation: str
    arguments: dict[str, Any]
    timeout: float
    spec: InvocationSpec

    def __post_init__(self) -> None:
        if self.operation not in HANDLERS:
            raise ValueError(f"unknown operation {self.operation!r}")
        if not (0 < self.timeout < math.inf):
            raise ValueError(f"operation timeout must be finite and positive, not {self.timeout}")


@dataclass(frozen=True)
class CleanupProof:
    """What `verify_cleanup` observed: the scope gone, and no container left with its label."""

    id: str
    scope: dict[str, Any]
    containers: dict[str, Any]
    at: str


def _state() -> tuple[str | None, dict[str, object] | None]:
    """The invocation this process runs as and the Docker runtime it recorded."""
    return _current, _runtime


def current() -> str | None:
    """The invocation ID of the operation this process runs as, or None outside one."""
    return _state()[0]


def report(text: str) -> None:
    """Send one progress line to the operation's owner, which passes it to its caller; outside
    an operation, nothing. A line too long for one message is shortened. Progress is only
    shown, so a send that fails is dropped: an owner that is gone stops this scope anyway."""
    if _channel is None:
        return
    connection, sending = _channel
    while len(message := json.dumps({"progress": text}).encode() + b"\n") > _MESSAGE_LIMIT:
        text = text[: len(text) // 2]
    with sending:
        try:
            connection.sendall(message)
        except OSError:
            pass


def creates(arguments: tuple[str, ...]) -> bool:
    """Whether Docker `arguments` create a container, or build an image: a builder other than
    the engine's own starts BuildKit in a container."""
    return arguments[:1] == ("build",) or any(
        arguments[: len(create)] == create for create in _CREATES
    )


def container_invocation(runtime: dict[str, object] | None) -> str:
    """The invocation whose label a container about to start under `runtime` carries.

    Containers start only inside an operation that recorded the same Docker daemon, so
    recovery knows where to find them by that label. Anything else is refused.
    """
    invocation, recorded = _state()
    if invocation is None:
        raise ValueError(
            "PluginBench creates a container or builds an image only inside an operation, "
            "whose label lets recovery find it"
        )
    ours, theirs = (recorded or {}).get("docker_engine"), (runtime or {}).get("docker_engine")
    if not (
        isinstance(ours, dict)
        and isinstance(theirs, dict)
        and (theirs.get("id"), theirs.get("endpoint")) == (ours.get("id"), ours.get("endpoint"))
    ):
        raise ValueError(
            f"operation {invocation} recorded no Docker daemon, or another one, so it starts no "
            "container on this one"
        )
    return invocation


def with_label(arguments: tuple[str, ...], invocation: str) -> tuple[str, ...]:
    """Docker `arguments` with the invocation's label right after the subcommand, when they
    create a container; otherwise unchanged.

    Docker keeps the last of repeated labels and accepts many spellings of the option, so no
    argument may name the invocation label at all, and no label file is taken.
    """
    if any(LABEL_KEY in item or item.startswith("--label-file") for item in arguments):
        raise ValueError(
            f"inside an operation, only PluginBench sets the {LABEL_KEY} label, and no label "
            "file is read"
        )
    for create in _CREATES:
        if arguments[: len(create)] == create:
            return (*create, "--label", f"{LABEL_KEY}={invocation}", *arguments[len(create):])
    return arguments


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
    )


def require_user_manager() -> str:
    """Return `systemd-run`'s path once this host can run scopes; raise HostUnavailable."""
    launcher, control = shutil.which("systemd-run"), shutil.which("systemctl")
    if launcher is None or control is None:
        raise HostUnavailable("systemd-run or systemctl is not installed")
    try:
        version = _run([launcher, "--version"], QUERY_SECONDS).stdout.split()
        manager = _run([control, "--user", "is-system-running"], QUERY_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostUnavailable(f"systemd could not be queried ({exc})") from exc
    if len(version) < 2 or not version[1].isdigit() or int(version[1]) < MINIMUM_SYSTEMD:
        raise HostUnavailable(f"systemd {MINIMUM_SYSTEMD} or later is required")
    # A degraded manager is reachable, though is-system-running then exits nonzero.
    if manager.stdout.strip() not in ("running", "degraded"):
        detail = (manager.stdout or manager.stderr).strip() or f"exit {manager.returncode}"
        raise HostUnavailable(f"the systemd user manager is not reachable ({detail})")
    if not (CGROUP_ROOT / "cgroup.controllers").is_file():
        raise HostUnavailable("cgroup v2 is not mounted at /sys/fs/cgroup")
    return launcher


def _scope_command(launcher: str, request: OperationRequest, path: Path) -> list[str]:
    return [
        launcher, "--user", "--scope", "--collect", "--quiet", "--no-ask-password",
        f"--unit={request.spec.scope}",
        "--expand-environment=no",
        "--property=KillMode=control-group",
        "--property=KillSignal=SIGKILL",
        "--property=SendSIGKILL=yes",
        f"--property=RuntimeMaxSec={math.ceil(request.timeout + KILL_GRACE)}s",
        "--property=TimeoutStopSec=10s",
        "--", sys.executable, "-m", "pluginbench.invocation_runtime", "execute", str(path),
    ]


def _write_private(path: Path, value: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)


def _gate_path(directory: Path) -> tuple[int, str]:
    """A short path to directory/gate.sock, whatever the directory's length: AF_UNIX paths are
    limited to 108 bytes. The caller closes the returned descriptor."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    return fd, f"/proc/self/fd/{fd}/gate.sock"


def _send(connection: socket.socket, value: dict[str, Any]) -> None:
    connection.sendall(json.dumps(value).encode() + b"\n")


def _receive(connection: socket.socket) -> object:
    """One newline-ended JSON message of at most _MESSAGE_LIMIT bytes; None on EOF or excess."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = connection.recv(_MESSAGE_LIMIT - len(data))
        if not chunk or len(data) + len(chunk) >= _MESSAGE_LIMIT:
            return None
        data += chunk
    try:
        return json.loads(data)
    except ValueError:
        return None


def _cgroup(pid: int) -> str | None:
    """The cgroup v2 path of `pid`, or None once it has exited."""
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return None
    return next((line[3:] for line in lines if line.startswith("0::")), None)


def _populated(cgroup: str) -> bool:
    """Whether any process remains in `cgroup`, descendants included. A removed cgroup has none."""
    try:
        events = (CGROUP_ROOT / cgroup.lstrip("/") / "cgroup.events").read_text()
    except FileNotFoundError:
        return False
    return "populated 0" not in events.splitlines()


def _identity(pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "start_time": run_lock.started_at(pid),
        "boot_id": run_lock.boot_id(),
        "pid_namespace": run_lock.pid_namespace(),
    }


def _lock() -> run_lock.ExecutionLock:
    lock = run_lock.held_execution()
    if lock is None:
        raise RuntimeError("an operation needs the host execution lock")
    lock.assert_owned()
    return lock


def run_operation(
    request: OperationRequest,
    *,
    started: Callable[[int], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Any:
    """Run `request` in its scope and return its handler's value, once its cleanup is verified.

    `started` gets the entry point's PID after the scope is checked and before the handler may
    run; if it raises, nothing runs. `progress` gets each line the handler reports (`report`).
    The handler's own exception becomes OperationFailed; a handler past its deadline is killed
    and raises OperationDeadlineExceeded. Before anything spawns this raises only
    HostUnavailable, ValueError, or CleanupBlocked once an earlier cleanup of this owner was not
    verified. A cleanup that cannot be verified raises control_store.CleanupBlocked whatever the
    operation did, and keeps the owner unreleased. Any error raised once the scope's bootstrap
    started carries `launched`: the call is spent (`ledger.outcome`).
    """
    lock = _lock()
    if lock.leftover:
        from pluginbench.control_store import CleanupBlocked

        # A failed cleanup halts new admission for the whole owner, whichever slot hit it.
        raise CleanupBlocked(
            "an earlier operation's cleanup was not verified, so this command starts no more "
            "operations; the next command checks what it left first"
        )
    launcher = require_user_manager()
    spec = request.spec
    directory = lock.state / "invocations" / spec.id
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    directory.mkdir(mode=0o700)
    path = directory / "request.json"
    _write_private(
        path,
        {"id": spec.id, "operation": request.operation, "arguments": request.arguments,
         "docker": spec.docker},
    )
    lock.note_operation(
        {"id": spec.id, "scope": spec.scope, "label": spec.label, "docker": spec.docker,
         "dir": str(directory), "bootstrap": None, "payload": None}
    )
    launched = True  # Unless it fails before its bootstrap starts; `_launch` marks the rest.
    try:
        return _launch(
            lock, request, _scope_command(launcher, request, path), directory, started, progress
        )
    except BaseException as exc:
        launched = getattr(exc, "launched", False)
        raise
    finally:
        try:
            verify_cleanup(lock.record_operation(spec.id), lock=lock)
        except Exception as exc:
            from pluginbench.control_store import CleanupBlocked

            lock.note_leftover(
                f"cleanup of invocation {spec.id} was not verified: {exc}", operation=spec.id
            )
            # It replaces the operation's own outcome (chained as its context): while anything
            # of it may run, its slot stays taken and no more work starts.
            blocked = (
                exc
                if isinstance(exc, CleanupBlocked)
                else CleanupBlocked(f"cleanup of invocation {spec.id} was not verified: {exc}")
            )
            blocked.launched = launched
            if blocked is exc:
                raise
            raise blocked from exc


def _launch(
    lock: run_lock.ExecutionLock,
    request: OperationRequest,
    command: list[str],
    directory: Path,
    started: Callable[[int], None] | None,
    progress: Callable[[str], None] | None,
) -> Any:
    spec = request.spec
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        gate, gate_path = _gate_path(directory)
        try:
            listener.bind(gate_path)
        finally:
            os.close(gate)
        listener.listen(1)
        read_end, write_end = os.pipe()
        try:
            with (directory / "output.log").open("wb") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-c", _BOOTSTRAP, str(read_end), str(GATE_SECONDS),
                     *command],
                    pass_fds=(read_end,),
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except BaseException:
            os.close(write_end)
            raise
        finally:
            os.close(read_end)
        try:
            try:
                try:
                    lock.update_operation(spec.id, bootstrap=_identity(proc.pid))
                    os.write(write_end, b"1")
                finally:
                    os.close(write_end)
                with _admit(listener, lock, spec, started) as connection:
                    outcome = _wait(connection, spec, request.timeout, progress)
            finally:
                # The bootstrap, or the entry point it became, is this process's unreaped
                # child, so its PID cannot have been reused.
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if outcome == "deadline":
                raise OperationDeadlineExceeded(
                    f"operation {request.operation} did not return within "
                    f"{request.timeout:g} seconds"
                )
            return _result(directory, request.operation, proc.returncode)
        except BaseException as exc:
            # Its bootstrap started, so whatever ended it, the call is spent.
            setattr(exc, "launched", True)
            raise


def rebuild(result: dict[str, Any]) -> Exception:
    """The handler's exception from its saved `type` and `message`: a builtin one or one in
    _KNOWN_ERRORS as itself, a timeout with its command and limit, and any other as
    OperationFailed. Nothing is unpickled."""
    name, message = str(result.get("type")), str(result.get("message"))
    module, _, attribute = name.rpartition(".")
    source = _KNOWN_ERRORS.get(name) or (f"builtins:{attribute}" if module == "builtins" else "")
    try:
        if name == "subprocess.TimeoutExpired":
            return subprocess.TimeoutExpired(result["cmd"], float(result["timeout"]))
        if source:
            located, _, attribute = source.partition(":")
            error = getattr(importlib.import_module(located), attribute, None)
            if isinstance(error, type) and issubclass(error, Exception):
                return error(message)
    except (KeyError, TypeError, ValueError):
        pass  # Its saved fields do not rebuild it, so it is an unknown failure.
    return OperationFailed(message, type=name)


def _admit(
    listener: socket.socket,
    lock: run_lock.ExecutionLock,
    spec: InvocationSpec,
    started: Callable[[int], None] | None,
) -> socket.socket:
    """Accept the entry point, prove it runs in the scope, and send it the second permit."""
    listener.settimeout(GATE_SECONDS)
    try:
        connection, _ = listener.accept()
    except TimeoutError as exc:
        raise OperationFailed(
            f"the operation's scope did not start within {GATE_SECONDS:g} seconds"
        ) from exc
    try:
        connection.settimeout(GATE_SECONDS)
        pid, uid, _ = _PEER.unpack(
            connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER.size)
        )
        hello = _receive(connection)
        if uid != os.getuid() or hello != {"id": spec.id, "pid": pid}:
            raise OperationFailed(f"an unexpected process {pid} answered the startup gate")
        cgroup = _cgroup(pid)
        if cgroup is None or cgroup.rsplit("/", 1)[-1] != spec.scope:
            raise OperationFailed(f"process {pid} does not run in scope {spec.scope}")
        lock.update_operation(spec.id, payload={**_identity(pid), "cgroup": cgroup})
        lock.assert_owned()
        if started is not None:
            started(pid)
            # The lock can be lost while the callback waits; check again before the permit.
            lock.assert_owned()
        _send(connection, {"permit": spec.id})
    except BaseException:
        connection.close()
        raise
    return connection


def _wait(
    connection: socket.socket,
    spec: InvocationSpec,
    timeout: float,
    progress: Callable[[str], None] | None,
) -> str:
    """Pass on the handler's progress until the entry point exits, which closes its end; past
    `timeout`, stop the scope: SIGTERM, then SIGKILL after KILL_GRACE. Return "exited" or
    "deadline"."""
    deadline = time.monotonic() + timeout
    from pluginbench.control_store import CleanupUnknown

    follow = _Follow(connection, progress)
    if follow.closed_before(deadline):
        return "exited"
    # A kill that fails is left to the cleanup that follows, which proves the scope gone.
    try:
        _systemctl("kill", "--kill-whom=all", "--signal=SIGTERM", spec.scope, timeout=QUERY_SECONDS)
        if not follow.closed_before(time.monotonic() + KILL_GRACE):
            _systemctl(
                "kill", "--kill-whom=all", "--signal=SIGKILL", spec.scope, timeout=QUERY_SECONDS
            )
    except CleanupUnknown:
        pass
    return "deadline"


class _Follow:
    """The entry point's messages after its permit: one `{"progress": text}` JSON line each.

    Only its exit closes the connection. A line that is not such a message, or is longer than
    _MESSAGE_LIMIT, is dropped; it never reads as an exit."""

    def __init__(
        self, connection: socket.socket, progress: Callable[[str], None] | None
    ) -> None:
        self._connection = connection
        self._progress = progress
        self._pending = b""

    def closed_before(self, deadline: float) -> bool:
        """Whether the peer closes the connection before `deadline`."""
        while (left := deadline - time.monotonic()) > 0:
            self._connection.settimeout(min(left, 1.0))
            try:
                data = self._connection.recv(_MESSAGE_LIMIT)
            except TimeoutError:
                continue
            except OSError:
                return True
            if not data:
                return True
            self._take(data)
        return False

    def _take(self, data: bytes) -> None:
        *lines, self._pending = (self._pending + data).split(b"\n")
        if len(self._pending) > _MESSAGE_LIMIT:
            self._pending = b""  # Not a message; the next newline starts the next one.
        for line in lines:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            text = message.get("progress") if isinstance(message, dict) else None
            if isinstance(text, str) and len(line) <= _MESSAGE_LIMIT and self._progress:
                self._progress(text)


def _result(directory: Path, operation: str, status: int) -> Any:
    try:
        result = json.loads((directory / "result.json").read_text())
    except (OSError, ValueError) as exc:
        raise OperationFailed(
            f"operation {operation} ended without a result (exit status {status}): {exc}"
        ) from exc
    (directory / "result.json").unlink()
    if result.get("ok") is True:
        return result.get("value")
    error = rebuild(result)
    error.add_note(f"Operation {operation} raised it; see {directory / 'traceback.txt'}")
    raise error


def _systemctl(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run `systemctl --user`; raise CleanupUnknown when it cannot run at all."""
    from pluginbench.control_store import CleanupUnknown

    try:
        return _run(["systemctl", "--user", *arguments], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanupUnknown(f"systemctl {arguments[0]} failed: {exc}") from exc


def _left(deadline: float, cap: float, what: str) -> float:
    from pluginbench.control_store import CleanupUnknown

    left = deadline - time.monotonic()
    if left <= 0:
        raise CleanupUnknown(f"cleanup ran out of time before {what} was verified")
    return min(cap, left)


def verify_cleanup(
    operation: dict[str, Any], *, lock: run_lock.ExecutionLock
) -> CleanupProof:
    """Stop the operation's scope and remove the containers carrying its label, prove both gone,
    then drop it from the owner record. Needs the host execution lock.

    Raises control_store.CleanupBlocked when something survives, and its subclass
    CleanupUnknown when systemd or Docker cannot be checked; the operation then stays recorded.
    """
    deadline = time.monotonic() + CLEANUP_SECONDS
    scope = verify_scope(operation, deadline=deadline)
    containers = verify_labelled(operation, deadline=deadline)
    proof = CleanupProof(operation["id"], scope, containers, _now())
    directory = Path(operation["dir"])
    if directory.is_dir():
        (directory / "request.json").unlink(missing_ok=True)
        (directory / "cleanup.json").write_text(json.dumps(proof.__dict__, indent=2) + "\n")
    lock.drop_operation(operation["id"])
    return proof


def _stop_bootstrap(operation: dict[str, Any], deadline: float) -> str:
    """Kill the recorded bootstrap, or the entry point it became, if that process still runs."""
    from pluginbench.control_store import CleanupBlocked, CleanupUnknown

    identity = operation.get("bootstrap")
    if identity is None:
        # Its permit is sent only after its identity is recorded, so it exits without one.
        return "never permitted"
    if identity["boot_id"] != run_lock.boot_id():
        return "earlier boot"
    if identity["pid_namespace"] != run_lock.pid_namespace():
        raise CleanupUnknown(f"the bootstrap of invocation {operation['id']} ran in another "
                             "PID namespace")
    try:
        handle = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return "gone"
    except OSError as exc:
        raise CleanupUnknown(
            f"the bootstrap {identity['pid']} could not be checked: {exc}"
        ) from exc
    try:
        # The pidfd pins this process, so the start time read now is its own.
        if run_lock.started_at(identity["pid"]) != identity["start_time"]:
            return "gone"
        try:
            signal.pidfd_send_signal(handle, signal.SIGKILL)
        except ProcessLookupError:
            return "gone"
        import select

        if not select.select([handle], [], [], _left(deadline, QUERY_SECONDS, "the bootstrap"))[0]:
            raise CleanupBlocked(f"the bootstrap {identity['pid']} survived SIGKILL")
    finally:
        os.close(handle)
    return "killed"


def _unit(scope: str, deadline: float) -> dict[str, str]:
    from pluginbench.control_store import CleanupUnknown

    result = _systemctl(
        "show", "-p", "LoadState,ActiveState,ControlGroup", scope,
        timeout=_left(deadline, QUERY_SECONDS, scope),
    )
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or "LoadState" not in state:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise CleanupUnknown(f"systemd could not be asked about {scope} ({detail})")
    return state


def verify_scope(operation: dict[str, Any], *, deadline: float) -> dict[str, Any]:
    """Stop the operation's scope and prove it gone: the unit not found, and its recorded cgroup
    empty or removed. A scope still loaded STOP_ESCALATION seconds after its stop gets SIGKILL;
    one still loaded after SCOPE_SECONDS blocks. A bus that cannot be reached is unknown, never
    "not found"."""
    from pluginbench.control_store import CleanupBlocked

    scope = operation["scope"]
    bootstrap = _stop_bootstrap(operation, deadline)
    begun = time.monotonic()
    phase = min(deadline, begun + SCOPE_SECONDS)
    escalated = False
    # The phase bounds the wait for the scope to go; each command is bounded by the whole pass,
    # so a scope that stays is a survivor, not an unanswered query.
    _systemctl("--no-block", "stop", scope, timeout=_left(deadline, QUERY_SECONDS, scope))
    while (state := _unit(scope, deadline))["LoadState"] != "not-found":
        group = state.get("ControlGroup", "")
        if state.get("ActiveState") in ("inactive", "failed") and not (group and _populated(group)):
            _systemctl("reset-failed", scope, timeout=_left(deadline, QUERY_SECONDS, scope))
        elif not escalated and time.monotonic() - begun >= STOP_ESCALATION:
            _systemctl(
                "kill", "--kill-whom=all", "--signal=SIGKILL", scope,
                timeout=_left(deadline, QUERY_SECONDS, scope),
            )
            escalated = True
        if time.monotonic() >= phase:
            raise CleanupBlocked(
                f"scope {scope} still runs {SCOPE_SECONDS:g} seconds after its stop"
            )
        time.sleep(UNIT_POLL)
    recorded = (operation.get("payload") or {}).get("cgroup")
    if recorded and _populated(recorded):
        raise CleanupBlocked(f"the cgroup of scope {scope} still has processes")
    return {"unit": "not-found", "bootstrap": bootstrap, "escalated": escalated, "at": _now()}


def verify_labelled(operation: dict[str, Any], *, deadline: float) -> dict[str, Any]:
    """Remove every container carrying the operation's exact label from the frozen daemon, then
    require two empty inventories INVENTORY_INTERVAL apart. Only run once the scope is gone, so
    nothing of the operation can still create one. A daemon that cannot be checked, or is not
    the recorded one, is unknown."""
    from pluginbench import harbor_backend, isolation
    from pluginbench.control_store import INVENTORY_INTERVAL, CleanupBlocked, CleanupUnknown

    runtime = operation.get("docker")
    if runtime is None:
        return {"docker": "not used"}
    label, phase = operation["label"], min(deadline, time.monotonic() + CONTAINER_SECONDS)

    def docker(*arguments: str, cap: float = QUERY_SECONDS) -> str:
        timeout = math.ceil(_left(phase, cap, f"the containers labelled {label}"))
        try:
            if arguments[0] == "ps":
                harbor_backend._assert_daemon(runtime, timeout=timeout)
            return harbor_backend._docker(*arguments, expected_runtime=runtime, timeout=timeout)
        except (
            OSError, ValueError, subprocess.SubprocessError, isolation.RuntimeDriftError
        ) as exc:
            raise CleanupUnknown(f"Docker could not be checked for {label}: {exc}") from exc

    def inventory() -> list[str]:
        return docker(
            "ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={label}"
        ).split()

    removed: list[str] = []
    if found := inventory():
        labels = docker(
            "inspect", "--format", f'{{{{index .Config.Labels "{LABEL_KEY}"}}}}', *found
        ).splitlines()
        removed = [
            item for item, value in zip(found, labels, strict=False) if value == operation["id"]
        ]
        if removed:
            try:
                docker("rm", "--force", "--volumes", *removed, cap=REMOVE_SECONDS)
            except CleanupUnknown:
                pass  # One may have gone meanwhile; the inventories below decide.
    for _ in range(2):
        time.sleep(INVENTORY_INTERVAL)
        if left := inventory():
            raise CleanupBlocked(f"containers labelled {label} remain: {', '.join(left)}")
    return {"removed": removed, "inventories": 2, "at": _now()}


def handler(operation: str) -> Callable[[dict[str, Any]], Any]:
    """The function HANDLERS names for `operation`."""
    module, name = HANDLERS[operation].split(":")
    function: Callable[[dict[str, Any]], Any] = getattr(importlib.import_module(module), name)
    return function


def failure(error: Exception) -> dict[str, Any]:
    """A handler's exception as its saved result, which the owner raises again (`rebuild`)."""
    kind = type(error)
    result = {"ok": False, "type": f"{kind.__module__}.{kind.__qualname__}", "message": str(error)}
    if isinstance(error, subprocess.TimeoutExpired):
        command = error.cmd
        result.update(
            cmd=[str(item) for item in command] if isinstance(command, (list, tuple)) else str(command),
            timeout=error.timeout,
        )
    return result


def _execute(path: Path) -> int:
    """The entry point inside the scope: pass the second gate, run the handler, save its result.

    It keeps the connection open while the handler runs, so its exit closes it."""
    request = json.loads(path.read_text())
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with connection:
        connection.settimeout(GATE_SECONDS)
        gate, gate_path = _gate_path(path.parent)
        try:
            connection.connect(gate_path)
        except OSError:
            return 75  # Nobody admits it: its owner is gone.
        finally:
            os.close(gate)
        _send(connection, {"id": request["id"], "pid": os.getpid()})
        if _receive(connection) != {"permit": request["id"]}:
            return 75
        connection.settimeout(None)
        global _current, _runtime, _channel
        _current, _runtime = request["id"], request["docker"]
        _channel = (connection, threading.Lock())
        try:
            value = handler(request["operation"])(request["arguments"])
            result: dict[str, Any] = {"ok": True, "value": value}
        except Exception as exc:
            result = failure(exc)
            (path.parent / "traceback.txt").write_text(traceback.format_exc())
        _write_private(path.parent / "result.json", result)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] != ["execute"] or len(sys.argv) != 3:
        raise SystemExit("usage: python -m pluginbench.invocation_runtime execute REQUEST")
    # Run the imported module, not this __main__ copy, so there is one HANDLERS table.
    from pluginbench import invocation_runtime

    raise SystemExit(invocation_runtime._execute(Path(sys.argv[2])))
