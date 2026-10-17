from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pluginbench import processes


def _command(tmp_path: Path, *, wait: bool, ignore_term: bool = False) -> tuple[list[str], Path]:
    pid_file = tmp_path / "child.pid"
    child = (
        "import os, signal, time; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    leader = (
        "import subprocess, sys, time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"ready=Path({str(pid_file)!r})\n"
        "while not ready.exists(): time.sleep(0.01)\n"
        "print('captured stdout', flush=True)\n"
        "print('captured stderr', file=sys.stderr, flush=True)\n"
        "ready.with_suffix('.ready').write_text('ready')\n" + ("time.sleep(60)\n" if wait else "")
    )
    return [sys.executable, "-c", leader], pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Container PID 1 may leave dead orphaned descendants as zombies.
    status = Path(f"/proc/{pid}/stat")
    return not (status.exists() and status.read_text().split(") ", 1)[1].startswith("Z "))


def _assert_dead(pid: int) -> None:
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _alive(pid)


def test_cleanup_reaches_descendants_after_leader_exit_and_preserves_unrelated_group(
    tmp_path: Path,
) -> None:
    argv, pid_file = _command(tmp_path, wait=False, ignore_term=True)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        proc.communicate(timeout=5)
        child_pid = int(pid_file.read_text())
        assert proc.returncode == 0 and _alive(child_pid)
        processes.terminate_process_group(proc)
        _assert_dead(child_pid)
        assert unrelated.poll() is None
    finally:
        for command in (proc, unrelated):
            try:
                os.killpg(command.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            command.wait()


def test_run_command_captures_output_and_cleans_descendants_after_success(tmp_path: Path) -> None:
    argv, pid_file = _command(tmp_path, wait=False)
    result = processes.run_command(argv, cwd=tmp_path, env=dict(os.environ), timeout=5)
    assert result.args == argv and result.returncode == 0
    assert result.stdout == "captured stdout\n" and result.stderr == "captured stderr\n"
    _assert_dead(int(pid_file.read_text()))


@pytest.mark.parametrize(
    "cleanup_error",
    [None, PermissionError("cleanup blocked"), subprocess.TimeoutExpired("cleanup", 1)],
)
def test_timeout_preserves_partial_output_and_cleans_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: OSError | subprocess.TimeoutExpired | None,
) -> None:
    argv, pid_file = _command(tmp_path, wait=True)
    if cleanup_error is not None:
        terminate = processes.terminate_process_group

        def cleanup(proc: subprocess.Popen[str]) -> None:
            terminate(proc)
            raise cleanup_error

        monkeypatch.setattr(processes, "terminate_process_group", cleanup)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        processes.run_command(argv, cwd=tmp_path, env=dict(os.environ), timeout=1)
    assert failure.value.cmd == argv and failure.value.timeout == 1
    assert failure.value.stdout == b"captured stdout\n"
    assert failure.value.stderr == b"captured stderr\n"
    if cleanup_error is not None:
        assert any("Process-group cleanup failed" in note for note in failure.value.__notes__)
    _assert_dead(int(pid_file.read_text()))


@pytest.mark.parametrize("failure", [KeyboardInterrupt("interrupted"), OSError("read failed")])
def test_exception_cleans_group_and_drains_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    argv, pid_file = _command(tmp_path, wait=True)
    communicate = subprocess.Popen.communicate
    calls = 0
    drained: list[tuple[str, str]] = []

    def interrupt(
        self: subprocess.Popen[str], input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            deadline = time.monotonic() + 5
            ready = pid_file.with_suffix(".ready")
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            raise failure
        output = communicate(self, input, timeout)
        drained.append(output)
        return output

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupt)
    with pytest.raises(type(failure), match=str(failure)):
        processes.run_command(argv, cwd=tmp_path, env=dict(os.environ), timeout=5)
    assert calls == 2 and drained == [("captured stdout\n", "captured stderr\n")]
    _assert_dead(int(pid_file.read_text()))


def test_nonzero_status_is_returned_and_launch_errors_propagate(tmp_path: Path) -> None:
    result = processes.run_command(
        [sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5,
    )
    assert result.returncode == 7 and result.stdout == "failed\n"
    with pytest.raises(FileNotFoundError):
        processes.run_command([str(tmp_path / "missing")], cwd=tmp_path, env={}, timeout=5)


def test_escaped_descendant_cannot_block_timeout_output_drain(tmp_path: Path) -> None:
    pid_file = tmp_path / "escaped.pid"
    child = (
        "import os, sys, time; from pathlib import Path; os.setsid(); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "print('escaped stdout', flush=True); "
        "print('escaped stderr', file=sys.stderr, flush=True); time.sleep(60)"
    )
    leader = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            processes.run_command(
                [sys.executable, "-c", leader], cwd=tmp_path, env=dict(os.environ), timeout=0.5
            )
        assert time.monotonic() - started < 5
        assert failure.value.stdout == b"escaped stdout\n"
        assert failure.value.stderr == b"escaped stderr\n"
        assert any("Final output drain" in note for note in failure.value.__notes__)
        assert _alive(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            child_pid = int(pid_file.read_text())
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _assert_dead(child_pid)
