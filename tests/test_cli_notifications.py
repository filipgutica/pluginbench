"""Walk-away runs report when coding starts and when the run ends."""

from __future__ import annotations

import subprocess
import sys
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
    (tmp_path / "evaluation.yaml").touch()
    return CliRunner().invoke(
        cli.app,
        ["run", str(tmp_path / "evaluation.yaml"), "--yes", "--foreground", *extra],
    )


def _prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def prepare(config: object, output: Path, *, progress: Callable[[str], None]) -> Path:
        progress("Preparing native Harbor task a")
        (tmp_path / "run").mkdir()
        return tmp_path / "run"

    monkeypatch.setattr(cli, "prepare_evaluation", prepare)


def test_notifies_once_when_coding_starts_and_when_the_run_finishes(
    notices: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(root: Path, *, progress: Callable[[str], None], guard: object) -> Path:
        for message in ("Coding 1/2: a", "Coding 2/2: a"):
            progress(message)
        return root

    _prepared(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "resume_evaluation", run)
    result = _run(tmp_path, "--notify")
    assert result.exit_code == 0
    assert notices == [
        "Setup passed; 2 coding attempts started.",
        f"Run finished. Report: {tmp_path / 'run/report/report.html'}",
    ]


def test_notifies_when_preparation_fails_before_coding(
    notices: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prepare(config: object, output: Path, *, progress: Callable[[str], None]) -> Path:
        progress("Preparing native Harbor task a")
        raise ValueError("measurements are not ready")

    monkeypatch.setattr(cli, "prepare_evaluation", prepare)
    result = _run(tmp_path, "--notify")
    assert result.exit_code == 2
    assert notices == ["Run failed: measurements are not ready"]


def test_runs_without_the_flag_send_no_notices(
    notices: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(root: Path, *, progress: Callable[[str], None], guard: object) -> Path:
        progress("Coding 1/2: a")
        return root

    _prepared(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "resume_evaluation", run)
    assert _run(tmp_path).exit_code == 0
    assert notices == []


@pytest.mark.parametrize(
    "platform,command",
    [
        (
            "darwin",
            [
                "osascript",
                "-e",
                'display notification "Run failed: \\"quoted\\" reason" with title "PluginBench"',
            ],
        ),
        ("linux", ["notify-send", "PluginBench", 'Run failed: "quoted" reason']),
    ],
)
def test_a_failed_notice_never_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, platform: str, command: list[str]
) -> None:
    calls: list[list[str]] = []

    def unavailable(command: list[str], **kwargs: object) -> None:
        calls.append(command)
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(subprocess, "run", unavailable)
    monkeypatch.setattr(sys, "platform", platform)
    cli._notify('Run failed: "quoted" reason')
    assert calls == [command]
