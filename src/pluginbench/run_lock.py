"""Own one run directory: a lock naming its process group, and a stop that signals only that group.

This lives outside processes.py on purpose: processes.py is part of the vanilla launcher hash,
so editing it would change every vanilla cache key.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

LOCK_NAME = "run.lock"
_PROBE_GRACE = 0.5


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
def hold(root: Path) -> Iterator[RunLock]:
    """Own `root` while the block runs; SIGTERM becomes KeyboardInterrupt in this process.

    The flock is released when the process exits, even by SIGKILL, so a lock that can be
    acquired always belongs to a dead owner. The file stays behind and keeps the last root.
    """
    root = root.resolve()
    fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # `status` probes ownership with a brief shared flock; wait that out, not a real owner.
        deadline = time.monotonic() + _PROBE_GRACE
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() > deadline:
                    raise ValueError(
                        f"run is already active: {root}; stop it with `pluginbench stop {root}`"
                    ) from exc
                time.sleep(0.02)
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
            yield RunLock(root, fd)
        finally:
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
