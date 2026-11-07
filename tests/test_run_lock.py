"""run.lock ownership and `stop`: only the recorded, still-matching process group is signalled.

Every signalled process here is a child group this test starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pluginbench
from pluginbench import run_lock

_SOURCE = str(Path(pluginbench.__file__).parents[1])

# Holds the lock, reports readiness, and records how it ended.
_HOLDER = """
import sys, time
from pathlib import Path
from pluginbench import run_lock
root = Path(sys.argv[1])
try:
    with run_lock.hold(root):
        (root / "ready").write_text("ready")
        time.sleep(30)
except KeyboardInterrupt as exc:
    (root / "ended").write_text(f"KeyboardInterrupt: {exc}")
    raise SystemExit(3)
"""


def _holder(root: Path) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(root)],
        env={**os.environ, "PYTHONPATH": _SOURCE},
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not (root / "ready").exists():
        assert child.poll() is None, "lock holder exited early"
        assert time.monotonic() < deadline, "lock holder never became ready"
        time.sleep(0.02)
    return child


def _cleanup(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


def test_stop_interrupts_only_the_recorded_child_group(tmp_path: Path) -> None:
    child = _holder(tmp_path)
    try:
        owner = json.loads((tmp_path / "run.lock").read_text())
        assert owner["pid"] == owner["pgid"] == child.pid
        assert owner["root"] == str(tmp_path.resolve())
        assert run_lock.stop(tmp_path) == owner
        assert child.wait(timeout=10) == 3
        assert (tmp_path / "ended").read_text() == "KeyboardInterrupt: stopped by SIGTERM"
    finally:
        _cleanup(child)
    # The owner is gone, so its flock is free and nothing more is signalled.
    assert run_lock.stop(tmp_path) is None


def test_stop_refuses_an_owner_whose_start_time_differs(tmp_path: Path) -> None:
    child = _holder(tmp_path)
    try:
        lock = tmp_path / "run.lock"
        owner = json.loads(lock.read_text())
        # A reused PID has another start time; flock is advisory, so the file can be edited.
        lock.write_text(json.dumps({**owner, "process_start": "0"}))
        with pytest.raises(ValueError, match="nothing signalled"):
            run_lock.stop(tmp_path)
        time.sleep(0.2)
        assert child.poll() is None and not (tmp_path / "ended").exists()
    finally:
        _cleanup(child)


# Owns a lock and asks `stop` to signal it. It leads its own session, so even a broken guard
# could only signal this child, never the test's group.
_SELF_STOP = """
import sys
from pathlib import Path
from pluginbench import run_lock
root = Path(sys.argv[1])
with run_lock.hold(root):
    try:
        run_lock.stop(root)
    except ValueError as exc:
        print(exc)
"""


def test_stop_never_signals_a_dead_owner_or_its_own_group(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="never started"):
        run_lock.stop(tmp_path)
    child = _holder(tmp_path)
    child.kill()
    child.wait(timeout=5)
    assert run_lock.stop(tmp_path) is None
    own = subprocess.run(
        [sys.executable, "-c", _SELF_STOP, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": _SOURCE},
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert own.returncode == 0, own.stderr
    assert "refusing to signal this command's own process group" in own.stdout


def test_second_owner_and_moved_root_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    with run_lock.hold(root) as lock:
        with pytest.raises(ValueError, match="already active"):
            with run_lock.hold(root):
                pass
        lock.assert_in_place()
        root.rename(tmp_path / "moved")
        with pytest.raises(run_lock.RunMoved):
            lock.assert_in_place()
    with pytest.raises(ValueError, match="delete .*run.lock"):
        with run_lock.hold(tmp_path / "moved"):
            pass


def test_a_brief_status_probe_does_not_refuse_a_run_starting_at_that_moment(
    tmp_path: Path,
) -> None:
    # `status` checks ownership with a short shared flock; a writer starting then must wait.
    import fcntl
    import threading

    (tmp_path / run_lock.LOCK_NAME).touch()
    probe = os.open(tmp_path / run_lock.LOCK_NAME, os.O_RDONLY)
    fcntl.flock(probe, fcntl.LOCK_SH)
    threading.Timer(0.1, os.close, args=(probe,)).start()

    with run_lock.hold(tmp_path):
        assert run_lock.owner(tmp_path) is not None
