"""Run commands and clean up only the POSIX process groups they own."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pluginbench import run_lock

_CLEANUP_TIMEOUT = 1.0


def _signal_group(pid: int, sig: int) -> bool:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        if sig != 0:
            raise
        # macOS can report EPERM while an owned, terminated group is disappearing.
    return True


def terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Reap a command started with start_new_session=True and stop its group.

    The group can outlive its leader. Descendants that create a new session or
    process group escape this cleanup and require a separate containment boundary.
    """
    if _signal_group(proc.pid, signal.SIGTERM):
        deadline = time.monotonic() + _CLEANUP_TIMEOUT
        while time.monotonic() < deadline:
            proc.poll()
            if not _signal_group(proc.pid, 0):
                break
            time.sleep(0.01)
        else:
            _signal_group(proc.pid, signal.SIGKILL)
    proc.wait(timeout=_CLEANUP_TIMEOUT)


def run_command(
    argv: Sequence[str | Path],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float | None,
    on_spawn: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture UTF-8 output and clean the command's group, including on failure.

    TimeoutExpired retains byte output. The final drain is bounded because an
    escaped descendant can keep pipes open after the owned group has stopped.
    `on_spawn` gets the child's PID, which is also its group ID, once it runs; if
    it raises, the group is stopped like on any other failure.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    failure: BaseException | None = None
    try:
        try:
            if on_spawn is not None:
                on_spawn(proc.pid)
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                terminate_process_group(proc)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                run_lock.note_leftover(
                    f"the process group of an earlier call could not be stopped ({cleanup_error}), "
                    "so it may still run"
                )
                if failure is None:
                    raise
                failure.add_note(f"Process-group cleanup failed: {cleanup_error}")
    except BaseException as error:
        try:
            stdout, stderr = proc.communicate(timeout=_CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            run_lock.note_leftover(
                "output from an earlier call may still be held open by an escaped process"
            )
            error.add_note(
                f"Final output drain exceeded {_CLEANUP_TIMEOUT:g} seconds; "
                "a descendant may hold pipes open."
            )
        except OSError as drain_error:
            run_lock.note_leftover(
                f"output from an earlier call could not be drained ({drain_error}), so an "
                "escaped process may still hold it"
            )
            error.add_note(f"Could not drain captured process output: {drain_error}")
        else:
            if isinstance(error, subprocess.TimeoutExpired):
                error.output = stdout.encode("utf-8")
                error.stderr = stderr.encode("utf-8")
        finally:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
