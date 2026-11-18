"""Launch rows and fake-scope processes for the launch and coordinator tests."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fake_systemd import members
from pluginbench import run_lock
from pluginbench.control_store import ControlStore, Launch

SLEEP = shutil.which("sleep") or "/bin/sleep"


def until(condition: Callable[[], object], within: float = 20.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def launches(state: Path) -> dict[str, Launch]:
    with ControlStore.open(state) as store:
        return {
            launch.launch_id: launch
            for status in ("launching", "active", "fenced", "finished")
            for launch in store.launches(status)
        }


def ticket(store: ControlStore, *, scope: str = "", status: str = "launching") -> Launch:
    launch_id = uuid4().hex
    launch = Launch(
        launch_id=launch_id,
        kind="run",
        initiator="app",
        target="/run",
        version=run_lock.execution_version(),
        scope=scope or f"pb-worker-{launch_id}.scope",
        boot_id=run_lock.boot_id(),
        expires=time.monotonic() + 60,
    )
    store.create_launch(launch)
    if status == "fenced":
        assert store.fence(launch_id)
    return launch


def in_scope(scope: str) -> subprocess.Popen[bytes]:
    """A process in the fake scope `scope`, as a permitted worker still waiting for the lock."""
    proc = subprocess.Popen(
        ["systemd-run", "--user", "--scope", f"--unit={scope}", "--", SLEEP, "300"],
        start_new_session=True,
    )
    until(lambda: bool(members(scope)))
    return proc
