"""run.lock ownership and `stop`: only the recorded, still-matching process group is signalled.

Every signalled process here is a child group this test starts.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGINT], ids=["stop", "ctrl-c"])
def test_a_stop_between_the_settings_and_their_binding_leaves_them_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: signal.Signals
) -> None:
    from pluginbench import evaluation

    evaluation._save_selection(tmp_path, {"name": "before"})
    write = evaluation._write

    def stopped_after_the_settings(path: Path, value: object) -> None:
        write(path, value)
        if path.name == "evaluation.json":
            signal.raise_signal(stop)

    monkeypatch.setattr(evaluation, "_write", stopped_after_the_settings)
    with run_lock.hold(tmp_path):
        with pytest.raises(KeyboardInterrupt):
            evaluation._save_selection(tmp_path, {"name": "after"})

    assert evaluation._load_selection(tmp_path) == {"name": "after"}


def _stopped(signum: int, frame: object) -> None:
    raise KeyboardInterrupt(signal.Signals(signum).name)


@pytest.fixture
def stop_handlers() -> Iterator[None]:
    """SIGTERM and SIGINT raise KeyboardInterrupt naming the signal; pytest's return afterwards."""
    saved = {item: signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGINT)}
    for item in saved:
        signal.signal(item, _stopped)
    try:
        yield
    finally:
        for item, handler in saved.items():
            signal.signal(item, handler)


def test_an_ignored_ctrl_c_never_hides_a_later_stop(stop_handlers: None) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        with run_lock.deferred_stop():
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) is _stopped


def test_a_stop_while_handlers_are_restored_still_restores_ctrl_c(
    stop_handlers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    restore = signal.signal
    sent = False

    def stopped_once_the_stop_handler_is_back(item: int, handler: Any) -> Any:
        nonlocal sent
        previous = restore(item, handler)
        if item == signal.SIGTERM and handler is _stopped and not sent:
            sent = True
            signal.raise_signal(signal.SIGTERM)
        return previous

    monkeypatch.setattr(signal, "signal", stopped_once_the_stop_handler_is_back)
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        with run_lock.deferred_stop():
            pass
    assert sent
    assert signal.getsignal(signal.SIGINT) is _stopped
    assert signal.getsignal(signal.SIGTERM) is _stopped


def test_nested_blocks_deliver_a_stop_when_the_outer_block_ends(stop_handlers: None) -> None:
    finished = []
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        with run_lock.deferred_stop():
            with run_lock.deferred_stop():
                signal.raise_signal(signal.SIGTERM)
            finished.append("inner")
    assert finished == ["inner"]
    assert signal.getsignal(signal.SIGTERM) is _stopped
    assert signal.getsignal(signal.SIGINT) is _stopped


def test_an_error_leaving_the_block_still_restores_handlers_and_delivers_the_stop(
    stop_handlers: None,
) -> None:
    with pytest.raises(KeyboardInterrupt, match="SIGINT") as raised:
        with run_lock.deferred_stop():
            signal.raise_signal(signal.SIGINT)
            raise OSError("disk full")
    assert isinstance(raised.value.__context__, OSError)
    assert signal.getsignal(signal.SIGTERM) is _stopped
    assert signal.getsignal(signal.SIGINT) is _stopped
