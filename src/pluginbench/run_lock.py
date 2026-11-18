"""Own one run directory: a lock naming its process group, and a stop that signals only that group.

Above every run lock sits one host execution lock per account: whatever run it targets, only one
PluginBench process executes containers or model calls at a time. Take it before a run lock.

This lives outside processes.py on purpose: processes.py is part of the vanilla launcher hash,
so editing it would change every vanilla cache key.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

from pluginbench import __version__

LOCK_NAME = "run.lock"
_PROBE_GRACE = 0.5
EXECUTION_LOCK = "execution.lock"
EXECUTION_OWNER = "execution-owner.json"
EXECUTION_KINDS = ("run", "review", "measure", "preflight", "judge", "recovery")
# The flock authorizes work. The owner record is also the fence recovery reads: until its owner
# records `released`, a dead owner may have left something running at its root.
OWNER_STATES = ("acquiring", "active", "reconciling", "blocked", "released")


class RunMoved(ValueError):
    """The run directory is no longer at the path its lock recorded."""


def process_start(pid: int) -> str:
    """The kernel's start time for `pid`, so a reused PID never matches a recorded owner."""
    if Path("/proc/self/stat").is_file():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError as exc:
            raise ProcessLookupError(pid) from exc
        # Field 22 is starttime. Split after the last ")" because the command name may hold
        # spaces or parentheses; the first field after it is field 3.
        return stat.rsplit(")", 1)[1].split()[19]
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ProcessLookupError(pid)
    return result.stdout.strip()


@dataclass(frozen=True)
class RunLock:
    root: Path
    fd: int

    def assert_in_place(self) -> None:
        """Raise RunMoved when the root path no longer leads to the lock this process holds."""
        held = os.fstat(self.fd)
        try:
            current = os.stat(self.root / LOCK_NAME)
        except FileNotFoundError:
            current = None
        if current is None or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise RunMoved(f"run directory moved away from {self.root}; no further work starts")


# Roots whose lock this process holds now. The owner record can lag a handover, so it cannot
# say whether this process still holds a lock.
_held: set[Path] = set()


def held_here(root: Path) -> bool:
    """Whether this process holds root/run.lock right now."""
    return root.resolve() in _held


def _owner(fd: int) -> dict[str, object] | None:
    text = os.pread(fd, 1 << 16, 0).decode()
    if not text.strip():
        return None
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("run.lock must hold a JSON object")
    return value


def _interrupt(signum: int, frame: FrameType | None) -> None:
    # Reuse the interrupt path: no new attempts start and saved work is checkpointed.
    raise KeyboardInterrupt("stopped by SIGTERM")


@contextmanager
def deferred_stop() -> Iterator[None]:
    """Hold SIGTERM and SIGINT until the block ends, then deliver them in arrival order.

    For writes that must land together: `stop` and Ctrl-C otherwise interrupt between any two
    bytecodes. Keep the block to those writes. Only the main thread runs signal handlers, so
    elsewhere nothing can interrupt the block and it runs as is.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[int] = []

    def defer(signum: int, frame: FrameType | None) -> None:
        pending.append(signum)

    # An ignored signal stays ignored. A handler installed outside Python cannot be put back.
    previous = {
        item: handler
        for item in (signal.SIGTERM, signal.SIGINT)
        if (handler := signal.getsignal(item)) not in (None, signal.SIG_IGN)
    }
    try:
        for item in previous:
            signal.signal(item, defer)
        yield
    finally:
        failure: BaseException | None = None
        while True:
            try:
                for item, handler in previous.items():
                    signal.signal(item, handler)
                break
            except BaseException as exc:
                # A signal that arrives now runs a handler already restored, which may raise
                # anywhere in the loop. Keep the first error and restore every handler again.
                failure = failure or exc
        if failure is not None:
            raise failure
        # In arrival order: a handler that returns must not swallow a later stop.
        for signum in pending:
            signal.raise_signal(signum)


def named_flock(path: Path, busy: Callable[[], Exception]) -> int:
    """Open `path` and take its exclusive flock; return the descriptor, which the caller closes.

    Never unlink or replace `path`: its inode is the lock. The descriptor is not inherited by
    children unless a caller passes it on deliberately.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _flock(fd, busy)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _flock(fd: int, busy: Callable[[], Exception]) -> None:
    """Take an exclusive flock on `fd`; raise `busy()` while another owner keeps it.

    `status` probes ownership with a brief shared flock; wait that out, not a real owner.
    """
    deadline = time.monotonic() + _PROBE_GRACE
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() > deadline:
                raise busy() from exc
            time.sleep(0.02)


@contextmanager
def hold(root: Path) -> Iterator[RunLock]:
    """Own `root` while the block runs; SIGTERM becomes KeyboardInterrupt in this process.

    The flock is released when the process exits, even by SIGKILL, so a lock that can be
    acquired always belongs to a dead owner. The file stays behind and keeps the last root.
    """
    root = root.resolve()
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _flock(
            fd,
            lambda: ValueError(
                f"run is already active: {root}; stop it with `pluginbench stop {root}`"
            ),
        )
        previous = _owner(fd)
        if previous is not None and previous.get("root") != str(root):
            raise ValueError(
                f"run was at {previous.get('root')} and is now at {root}; if you moved a stopped "
                f"run on purpose, delete {root / LOCK_NAME} and run it again"
            )
        owner = {
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "process_start": process_start(os.getpid()),
            "started_at": datetime.now(UTC).isoformat(),
            "root": str(root),
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(owner, indent=2) + "\n").encode(), 0)
        os.fsync(fd)
        previous_handler = signal.signal(signal.SIGTERM, _interrupt)
        try:
            _held.add(root)
            yield RunLock(root, fd)
        finally:
            _held.discard(root)
            signal.signal(signal.SIGTERM, previous_handler)
    finally:
        os.close(fd)


def owner(root: Path) -> dict[str, object] | None:
    """The recorded owner while a live process holds root/run.lock; otherwise None."""
    path = root.resolve() / LOCK_NAME
    if not path.is_file():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            # Held: the owner may not have written its record yet.
            return _owner(fd) or {}
        return None
    finally:
        os.close(fd)


def stop(root: Path) -> dict[str, object] | None:
    """SIGTERM the process group recorded in root/run.lock; None when no live run owns it.

    Nothing is signalled unless the lock is held and its recorded process still has the
    recorded start time and group. This command's own group is never signalled.
    """
    if not (root.resolve() / LOCK_NAME).is_file():
        raise ValueError(f"no run lock in {root}; it never started here")
    current = owner(root)
    if current is None:
        return None
    if not current:
        raise ValueError("run.lock is held but names no owner yet; try again")
    pid, pgid = current.get("pid"), current.get("pgid")
    if type(pid) is not int or type(pgid) is not int:
        raise ValueError("run.lock owner must record integer pid and pgid")
    try:
        matches = process_start(pid) == current.get("process_start") and os.getpgid(pid) == pgid
    except ProcessLookupError:
        matches = False
    if not matches:
        raise ValueError(
            f"run.lock owner {pid} no longer matches its recorded process; nothing signalled"
        )
    if pgid == os.getpgrp():
        raise ValueError("refusing to signal this command's own process group")
    os.killpg(pgid, signal.SIGTERM)
    return current


# The host execution lock. One per account, whatever checkout, run directory or HOME a command
# uses: see `state_root`.


class ExecutionBusy(ValueError):
    """Another process holds the host execution lock; nothing started."""


class ExecutionLost(RuntimeError):
    """This process no longer holds the host execution lock it took."""


def state_root() -> Path:
    """This account's PluginBench state directory, which holds the one host execution lock.

    Read from the password database, not $HOME, so a private invocation HOME cannot make a second
    lock. Tests replace this function; production has no switch for another lock namespace.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/pluginbench"


def boot_id() -> str:
    """This boot's ID: a PID and its start ticks can repeat after a reboot, but not this."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except FileNotFoundError:
        return ""


def pid_namespace() -> str:
    """This process's PID namespace. A PID recorded in another namespace names another process
    here, or none."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


@cache
def execution_version() -> str:
    """The package version and a hash of its code. A changed checkout keeps the version number,
    so the hash tells its owners apart."""
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return f"{__version__}+{digest.hexdigest()[:12]}"


@dataclass(frozen=True)
class ExecutionOwner:
    """Why a process takes the host execution lock.

    `eval` is the requested evaluation: a config, or the run it resumes. `root` is where its
    evidence and ledger live. `launch_id` names an app launch; a CLI command has none.
    """

    kind: str
    eval: str
    root: str
    launch_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in EXECUTION_KINDS:
            raise ValueError(f"unknown execution kind {self.kind}")


@dataclass(frozen=True)
class LockSnapshot:
    """What an observer sees of the host execution lock.

    `record` is the last published owner record and may be stale or absent. `verified` means
    the lock is held and the recorded process, on this boot and with its start time, holds it
    through its recorded descriptor now.
    """

    path: Path
    held: bool
    record: dict[str, object] | None
    verified: bool

    def busy(self) -> ExecutionBusy:
        """The refusal a command gets while this snapshot's holder keeps the lock."""
        record = self.record or {}
        kind, root, pid = record.get("kind"), record.get("root"), record.get("pid")
        if self.verified:
            stop = (
                f"stop it with `pluginbench stop {root}`"
                if kind in ("run", "review", "measure")
                else f"stop process {pid}"
            )
            return ExecutionBusy(
                f"PluginBench is already executing on this host: {kind} for {root} "
                f"(process {pid}). Wait for it, or {stop}"
            )
        last = f"; its last record names {kind} for {root} (process {pid})" if record else ""
        return ExecutionBusy(
            f"the host execution lock {self.path} is held by a process PluginBench cannot "
            f"verify{last}, so nothing starts. Find the holder with `lslocks | grep {self.path}` "
            "and `ps`; do not delete the lock file"
        )


def _read_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The owner record at `path`, or None and why it cannot be trusted. A missing record has
    no reason; an unreadable or invalid one is unknown, never an empty record."""
    value, problem = _load_record(path)
    if value is _MISSING:
        return None, None
    problem = problem or _record_problem(value)
    return (None, problem) if problem else (value, None)


# What _load_record returns for a record that does not exist. JSON `null` is a parsed value,
# which validation refuses like any other record that is not an object.
_MISSING = object()


def _load_record(path: Path) -> tuple[Any, str | None]:
    """The parsed JSON at `path`, or None and why it cannot be read; _MISSING if absent."""
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return _MISSING, None
    except (OSError, ValueError) as exc:
        return None, f"it cannot be read: {exc}"


def _record_problem(value: object) -> str | None:
    """Why `value` is not an owner record whose release and obligations can be trusted; None
    when it is."""
    return "; ".join(_obligations(value)[1]) or None


def _obligations(value: object) -> tuple[list[dict[str, Any]], list[str]]:
    """The obligations an owner record carries, each that validates on its own, and every
    reason any part of it cannot be trusted.

    The owner itself counts unless the whole record validates and says `released`. A record
    from before releases were recorded (schema 1) always counts as unreleased.
    """
    if not isinstance(value, dict):
        return [], ["it is not a JSON object"]
    problems: list[str] = []
    schema = value.get("schema")
    if type(schema) is not int or schema not in (1, 2):
        problems.append(f"its schema {schema!r} is not 1 or 2")
    elif schema == 2 and value.get("state") not in OWNER_STATES:
        problems.append(f"its state {value.get('state')!r} is not one of {', '.join(OWNER_STATES)}")
    kept: list[dict[str, Any]] = []
    pending = value.get("pending", [] if schema == 1 else None)
    if not isinstance(pending, list):
        problems.append(f"its pending obligations are {type(pending).__name__}, not a list")
    else:
        for index, item in enumerate(pending):
            if problem := _obligation_problem(item):
                problems.append(f"pending obligation {index} {problem}")
            else:
                kept.append(item)
    owner = {key: item for key, item in value.items() if key != "pending"}
    if problem := _obligation_problem(owner):
        problems.append(f"its owner {problem}")
    elif problems or schema == 1 or value.get("state") != "released":
        kept.append(owner)
    return kept, problems


def _obligation_problem(value: object) -> str | None:
    """Why `value` cannot be checked as an owner that may have left work running. Its pid and
    acquired_at are its identity, which a confirmation names; only the fence for an unreadable
    record (kind `unknown`) has no pid."""
    if not isinstance(value, dict):
        return "is not an object"
    for key, kind, required in (
        ("root", str, True),
        ("kind", str, True),
        ("pid", int, value.get("kind") != "unknown"),
        ("acquired_at", str, True),
        ("runtimes", list, False),
        ("leftovers", list, False),
        ("operations", list, False),
    ):
        item = value.get(key)
        if item is None and not required:
            continue
        if type(item) is not kind or (kind is str and not item):
            return f"has {key} {item!r}, not a{'n' if kind is int else ''} {kind.__name__}"
    if not all(isinstance(item, dict) for item in value.get("runtimes") or []):
        return "has a runtime that is not an object"
    if not all(
        isinstance(item, dict) and isinstance(item.get("reason"), str)
        for item in value.get("leftovers") or []
    ):
        return "has a failed cleanup without a reason"
    for item in value.get("operations") or []:
        if problem := _operation_problem(item):
            return f"has an operation that {problem}"
    return None


def is_invocation_id(value: object) -> bool:
    """Whether `value` is an invocation ID: 32 lowercase hex digits, valid in a systemd unit name
    and a Docker label value."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _operation_problem(value: object) -> str | None:
    """Why `value` is not an operation `invocation_runtime` recorded, whose scope and labelled
    containers recovery can stop; None when it is."""
    if not isinstance(value, dict):
        return "is not an object"
    invocation = value.get("id")
    if not is_invocation_id(invocation):
        return f"has id {invocation!r}, not 32 hex digits"
    if value.get("scope") != f"pb-inv-{invocation}.scope":
        return f"names scope {value.get('scope')!r}, not its own"
    if value.get("label") != f"pluginbench.invocation={invocation}":
        return f"names label {value.get('label')!r}, not its own"
    if not isinstance(value.get("docker"), dict | None) or not isinstance(value.get("dir"), str):
        return "has no valid Docker runtime or directory"
    for key in ("bootstrap", "payload"):
        identity = value.get(key)
        if identity is None:
            continue
        if not (
            isinstance(identity, dict)
            and type(identity.get("pid")) is int
            and isinstance(identity.get("start_time"), str | None)
            and isinstance(identity.get("boot_id"), str)
            and isinstance(identity.get("pid_namespace"), str)
        ):
            return f"has an invalid {key} identity"
    return None


def _holds(record: dict[str, object], lock: os.stat_result) -> bool:
    """Whether the recorded process holds the exclusive flock on `lock` through its recorded
    descriptor. A live PID alone is not enough: it may be reused, or have handed the lock on."""
    pid, fd = record.get("pid"), record.get("lock_fd")
    if type(pid) is not int or type(fd) is not int or record.get("boot_id") != boot_id():
        return False
    try:
        if process_start(pid) != record.get("start_time"):
            return False
        info = Path(f"/proc/{pid}/fdinfo/{fd}").read_text()
    except (OSError, ProcessLookupError):
        return False
    inode = f"{os.major(lock.st_dev):02x}:{os.minor(lock.st_dev):02x}:{lock.st_ino}"
    # Each line names one lock this open file holds, as
    # "lock:\t1: FLOCK  ADVISORY  WRITE <pid> <major>:<minor>:<inode> 0 EOF".
    return any(
        fields[2:5] == ["FLOCK", "ADVISORY", "WRITE"] and fields[6] == inode
        for line in info.splitlines()
        if (fields := line.split())[:1] == ["lock:"] and len(fields) > 6
    )


def execution_owner(state: Path | None = None) -> LockSnapshot:
    """Observe the host execution lock without taking it."""
    state = state or state_root()
    path = state / EXECUTION_LOCK
    record, _ = _read_record(state / EXECUTION_OWNER)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return LockSnapshot(path, held=False, record=record, verified=False)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
        lock = os.fstat(fd)
    finally:
        os.close(fd)
    verified = held and record is not None and _holds(record, lock)
    return LockSnapshot(path, held=held, record=record, verified=verified)


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    """Replace `path` with `value` so a reader sees the old record or the new, never a mix.

    Each write has its own temporary file, so writes that overlap never share one.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(value, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass
class ExecutionLock:
    """The host execution lock this process holds, and the owner record it published."""

    state: Path
    fd: int
    record: dict[str, Any]
    # Set once this owner records work it must clean up, or knows of cleanup it could not
    # verify. Until a clean exit clears it, its release stays unrecorded, so the next owner
    # recovers its root.
    dirty: bool = False
    # Set once a cleanup of something this owner started failed. Unlike `dirty`, a clean exit
    # never clears it, and the record names each failure (`note_leftover`).
    leftover: bool = False
    # Worker threads update the record too; each change reads and writes it under this lock.
    _updating: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def assert_owned(self) -> None:
        """Raise ExecutionLost unless the lock path still leads to the inode this process holds
        and the published record still names this process."""
        held = os.fstat(self.fd)
        try:
            current = os.stat(self.state / EXECUTION_LOCK)
        except FileNotFoundError:
            current = None
        if current is None or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise ExecutionLost(f"the host execution lock in {self.state} was replaced")
        # Not while another thread's update has renamed its record into place but not yet
        # adopted it (`_update`): the file would differ from `self.record` for that moment.
        with self._updating:
            replaced = _read_record(self.state / EXECUTION_OWNER)[0] != self.record
        if replaced:
            raise ExecutionLost(f"another owner record replaced this process's in {self.state}")

    def publish(self, state: str) -> None:
        """Atomically replace the owner record with this one in `state`."""
        if state not in OWNER_STATES:
            raise ValueError(f"unknown owner state {state}")
        self._update(lambda record: {**record, "state": state})

    def settle(self, pending: list[dict[str, Any]]) -> None:
        """Replace the obligations this owner carries; the only write recovery makes."""
        self._update(lambda record: {**record, "pending": pending})

    def note_runtime(self, runtime: dict[str, object]) -> None:
        """Record a Docker runtime before any container starts under it. If the record cannot be
        written, this raises and nothing may start."""
        docker = {key: runtime[key] for key in ("docker_cli", "docker_engine") if key in runtime}
        self.dirty = True
        self._update(
            lambda record: record
            if docker in record["runtimes"]
            else {**record, "runtimes": [*record["runtimes"], docker]}
        )

    def note_leftover(self, reason: str) -> None:
        """Keep this owner unreleased, and record why a cleanup failed so the next owner's error
        can say so. The record is best-effort: an unreleased owner needs repair anyway."""
        self.leftover = True
        failure = {"reason": reason, "at": datetime.now(UTC).isoformat()}
        try:
            self._update(
                lambda record: {**record, "leftovers": [*record.get("leftovers", []), failure]}
            )
        except OSError:
            pass  # The in-memory flag alone keeps the release unrecorded.

    def note_operation(self, operation: dict[str, Any]) -> None:
        """Record an operation's scope, label and runtime before anything of it spawns. While it
        stays recorded, this owner's release stays unrecorded. If the record cannot be written,
        this raises and nothing may spawn."""
        if problem := _operation_problem(operation):
            raise ValueError(f"operation {problem}")
        self._update(
            lambda record: {**record, "operations": [*record.get("operations", []), operation]}
        )

    def record_operation(self, invocation: str) -> dict[str, Any]:
        """The recorded operation `invocation`, as last written."""
        return next(item for item in self.record["operations"] if item["id"] == invocation)

    def update_operation(self, invocation: str, **fields: object) -> None:
        """Add what is known of a recorded operation, such as a process identity."""
        self._update(
            lambda record: {
                **record,
                "operations": [
                    {**item, **fields} if item["id"] == invocation else item
                    for item in record["operations"]
                ],
            }
        )

    def drop_operation(self, invocation: str) -> None:
        """Forget an operation once its cleanup is verified."""
        self._update(
            lambda record: {
                **record,
                "operations": [item for item in record["operations"] if item["id"] != invocation],
            }
        )

    def _update(self, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        # Adopt the record only once it is on disk, so a failed write never reaches it later.
        with self._updating:
            record = change(self.record)
            _write_atomic(self.state / EXECUTION_OWNER, record)
            self.record = record


# The lock this process holds now, so recovery deep in a run can require it.
_execution: ExecutionLock | None = None


def held_execution() -> ExecutionLock | None:
    """The host execution lock this process holds right now, if any."""
    return _execution


def note_work() -> None:
    """Mark that the owner is about to start something it must clean up, such as a recorded
    call. Without the host lock there is nothing to mark."""
    if _execution is not None:
        _execution.dirty = True


@contextmanager
def external_work() -> Iterator[None]:
    """Hold the owner unclean while the block starts external work, such as image builds and
    probe containers. A block left by an exception may leave that work behind; one that returns
    has finished it, so the owner is then as clean as before."""
    lock = _execution
    if lock is None:
        yield
        return
    before, lock.dirty = lock.dirty, True
    yield
    lock.dirty = before


def started_at(pid: int) -> str | None:
    """The start time of `pid`, or None once it has exited, as `child_identity` records it."""
    try:
        return process_start(pid)
    except ProcessLookupError:
        return None


def note_leftover(reason: str) -> None:
    """Record that cleanup of something the owner started failed, as `reason` says, so its
    release stays unrecorded and the next owner cannot clear it by any check. Without the host
    lock there is nothing to record."""
    if _execution is not None:
        _execution.note_leftover(reason)


def note_runtime(runtime: dict[str, object]) -> None:
    """Record the Docker runtime containers are about to start under; without the host lock
    there is no owner record, and nothing to record."""
    if _execution is not None:
        _execution.note_runtime(runtime)


def _carried(state: Path) -> list[dict[str, Any]]:
    """The obligations the last owner left: those it carried, and itself unless it recorded a
    clean release (see _obligations). Whatever part of the record cannot be read or fails
    validation adds one more obligation, a fence whose root is the state directory. Every
    obligation that validates on its own is kept with its own root and checks, so repairing
    the fence never clears them."""
    value, problem = _load_record(state / EXECUTION_OWNER)
    if value is _MISSING:
        return []
    kept, problems = ([], [problem]) if problem else _obligations(value)
    if problems:
        # Named by when it was found, so confirming it never covers a later unreadable record.
        found = datetime.now(UTC).isoformat()
        kept.append(
            {"kind": "unknown", "root": str(state), "pid": None, "acquired_at": found,
             "problem": "; ".join(problems)}
        )
    return kept


@contextmanager
def hold_execution(owner: ExecutionOwner, *, state: Path | None = None) -> Iterator[ExecutionLock]:
    """Own this account's host execution lock while the block runs, or raise ExecutionBusy.

    The lock file is never unlinked or replaced: its inode is the lock. The flock ends with the
    process, even on SIGKILL, and is never inherited by a child. The owner record is published
    before the block starts any work, and carries every obligation the last owner left, so that
    write either fences them again or fails with nothing started. Leaving the block records a
    clean release, unless the owner knows of unverified cleanup.
    """
    global _execution
    if _execution is not None:
        raise RuntimeError("this process already holds the host execution lock")
    state = state or state_root()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = named_flock(state / EXECUTION_LOCK, lambda: execution_owner(state).busy())
    try:
        lock = ExecutionLock(
            state,
            fd,
            {
                "schema": 2,
                "pid": os.getpid(),
                "start_time": process_start(os.getpid()),
                "boot_id": boot_id(),
                "lock_fd": fd,
                "kind": owner.kind,
                "eval": owner.eval,
                "root": owner.root,
                "launch_id": owner.launch_id,
                "version": execution_version(),
                "state": "acquiring",
                "acquired_at": datetime.now(UTC).isoformat(),
                "runtimes": [],
                "leftovers": [],
                "operations": [],
                "pending": _carried(state),
            },
        )
        lock.publish("acquiring")
        _execution = lock
        try:
            yield lock
        finally:
            _execution = None
            # An operation still recorded was never proven cleaned up, whatever the caller did
            # with the error that said so.
            if not lock.dirty and not lock.leftover and not lock.record["operations"]:
                try:
                    lock.publish("released")
                except OSError:
                    # The record stays unreleased, so the next owner recovers this root: safe,
                    # and an exception already leaving the block is not hidden.
                    pass
    finally:
        os.close(fd)
