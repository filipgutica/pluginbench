"""Run commands and clean up only the POSIX process groups they own."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

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
) -> subprocess.CompletedProcess[str]:
    """Capture UTF-8 output and clean the command's group, including on failure.

    TimeoutExpired retains byte output. The final drain is bounded because an
    escaped descendant can keep pipes open after the owned group has stopped.
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
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                terminate_process_group(proc)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"Process-group cleanup failed: {cleanup_error}")
    except BaseException as error:
        try:
            stdout, stderr = proc.communicate(timeout=_CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            error.add_note(
                f"Final output drain exceeded {_CLEANUP_TIMEOUT:g} seconds; "
                "a descendant may hold pipes open."
            )
        except OSError as drain_error:
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
