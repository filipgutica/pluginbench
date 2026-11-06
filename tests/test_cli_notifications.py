"""Walk-away runs report when coding starts and when the run ends."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pluginbench import cli


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(cli, "_notify", sent.append)
    monkeypatch.setattr(cli, "load_evaluation", lambda path: object())
    monkeypatch.setattr(
        cli, "plan_evaluation", lambda config: {"coding_calls": 2, "review_calls": 0}
    )
    return sent


def _run(tmp_path: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        cli.app, ["run", "--config", str(tmp_path / "evaluation.yaml"), "--yes", *extra]
    )


def test_notifies_once_when_coding_starts_and_when_the_run_finishes(
    notices: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(config: object, output: Path, *, progress: Callable[[str], None]) -> Path:
        for message in ("Preparing native Harbor task a", "Coding 1/2: a", "Coding 2/2: a"):
            progress(message)
        return tmp_path / "run"

    monkeypatch.setattr(cli, "run_evaluation", run)
    result = _run(tmp_path, "--notify")
    assert result.exit_code == 0
    assert notices == [
        "Setup passed; 2 coding attempts started.",
        f"Run finished. Report: {tmp_path / 'run/report/report.html'}",
    ]


def test_notifies_when_preparation_fails_before_coding(
    notices: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(config: object, output: Path, *, progress: Callable[[str], None]) -> Path:
        progress("Preparing native Harbor task a")
        raise ValueError("measurements are not ready")

    monkeypatch.setattr(cli, "run_evaluation", run)
    result = _run(tmp_path, "--notify")
    assert result.exit_code == 2
    assert notices == ["Run failed: measurements are not ready"]


def test_runs_without_the_flag_send_no_notices(
    notices: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(config: object, output: Path, *, progress: Callable[[str], None]) -> Path:
        progress("Coding 1/2: a")
        return tmp_path / "run"

    monkeypatch.setattr(cli, "run_evaluation", run)
    assert _run(tmp_path).exit_code == 0
    assert notices == []


def test_a_failed_notice_never_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def unavailable(command: list[str], **kwargs: object) -> None:
        calls.append(command)
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(subprocess, "run", unavailable)
    cli._notify('Run failed: "quoted" reason')
    assert calls == [
        [
            "osascript",
            "-e",
            'display notification "Run failed: \\"quoted\\" reason" with title "PluginBench"',
        ]
    ]
