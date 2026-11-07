"""`run` returns once a background process owns the run; that process keeps running."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

import pluginbench
from pluginbench import cli, run_lock

_SOURCE = str(Path(pluginbench.__file__).parents[1])

# Stands in for the background run: owns the lock until `stop` interrupts it.
_WORKER = """
import sys, time
from pathlib import Path
from pluginbench import run_lock
root = Path(sys.argv[1])
with run_lock.hold(root):
    while True:
        time.sleep(0.02)
"""


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "plan_resume", lambda root: {"coding_calls": 3, "review_calls": 0})
    monkeypatch.setenv("PYTHONPATH", _SOURCE)


def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_run_returns_while_the_background_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: None
) -> None:
    monkeypatch.setattr(
        cli, "_worker_argv", lambda root, *, notify: [sys.executable, "-c", _WORKER, str(root)]
    )
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes"])
    owner = run_lock.owner(tmp_path)
    try:
        assert result.exit_code == 0, result.output
        assert f"pluginbench stop {tmp_path.absolute()}" in result.stdout
        assert owner and owner["pid"] == owner["pgid"] != os.getpgrp()
        time.sleep(0.2)
        assert run_lock.owner(tmp_path) == owner
    finally:
        if owner:
            run_lock.stop(tmp_path)
    _until(lambda: run_lock.owner(tmp_path) is None)
    assert "KeyboardInterrupt: stopped by SIGTERM" in (tmp_path / "run.log").read_text()


def test_background_run_that_exits_before_it_starts_fails_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: None
) -> None:
    monkeypatch.setattr(
        cli, "_worker_argv", lambda root, *, notify: [sys.executable, "-c", "raise SystemExit(5)"]
    )
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes"])
    assert result.exit_code == 2
    assert "exited with code 5 before it started" in result.stderr


def test_foreground_run_owns_the_lock_and_writes_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned: None
) -> None:
    owners = []

    def work(root: Path, *, progress: Callable[[str], None], guard: Callable[[], None]) -> Path:
        owners.append(run_lock.owner(root))
        guard()
        progress("Coding 1/3: task · baseline · repeat 1")
        return root

    monkeypatch.setattr(cli, "resume_evaluation", work)
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--yes", "--foreground"])
    assert result.exit_code == 0, result.output
    assert owners[0] is not None and owners[0]["pid"] == os.getpid()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["started", "progress", "finished"]
    assert events[1]["message"] == "Coding 1/3: task · baseline · repeat 1"
    assert run_lock.owner(tmp_path) is None


def test_the_background_command_runs_this_package() -> None:
    argv = cli._worker_argv(Path("/run"), notify=False)
    assert argv[:4] == [sys.executable, "-m", "pluginbench", "run"]
    assert argv[4:] == ["/run", "--yes", "--foreground"]
    version = subprocess.run(
        [sys.executable, "-m", "pluginbench", "--version"],
        env={**os.environ, "PYTHONPATH": _SOURCE},
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == pluginbench.__version__
