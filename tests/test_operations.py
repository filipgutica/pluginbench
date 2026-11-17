"""The handler table: the five operations a scope may run, and the arguments each accepts."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from pluginbench import (
    codex,
    collectors,
    evaluation,
    harbor_backend,
    invocation_runtime,
    operations,
)
from pluginbench.collectors import Collector


def _evaluation(tmp_path: Path) -> evaluation.Evaluation:
    (tmp_path / "guidance.md").write_text("Reuse the owning implementation.\n")
    raw = {
        "runner": {"harness": "codex", "model": "gpt-6-luna", "reasoning": "low"},
        "guidance": {"candidate": ["guidance.md"]},
        "tasks": {"dataset": "example/tasks", "version": "1", "ids": ["example/one"]},
        "measurements": ["tokens", "lines"],
        "repeats": 1,
    }
    return evaluation.resolve_evaluation(yaml.safe_load(yaml.safe_dump(raw)), tmp_path)


def _collector(tmp_path: Path) -> Collector:
    config = tmp_path / "coverage.yaml"
    config.write_text("{}\n")
    return Collector(kind="coverage", config=config, command=["true"], tool_version="test")


_MODELS: dict[str, type[BaseModel]] = {
    "coding": operations.Coding,
    "grading": operations.Grading,
    "review": operations.Review,
    "collect": operations.Collect,
}


def _cases(tmp_path: Path) -> dict[str, tuple[Any, str, dict[str, Any]]]:
    """Per operation: the patched implementation's owner and name, and valid arguments."""
    return {
        "coding": (
            harbor_backend, "invoke_native",
            {"area": tmp_path / "area", "task_dir": tmp_path / "task", "entry": {"host": "codex"},
             "protocol": {"isolation": {}}, "prompt": "", "timeout": 60},
        ),
        "grading": (
            harbor_backend, "regrade_native",
            {"artifacts": tmp_path / "artifacts", "task_dir": tmp_path / "task",
             "identity": {"task_assets_sha256": "x"}, "directory": tmp_path / "replay",
             "artifact_hashes": {"a": "b"}},
        ),
        "review": (
            codex, "invoke",
            {"area": tmp_path / "area", "entry": {"host": "claude"}, "protocol": {}, "prompt": "p",
             "timeout": 600, "output_schema": tmp_path / "schema.json", "artifacts": None},
        ),
        "collect": (
            collectors, "_run_collector",
            {"root": tmp_path / "source", "collector": _collector(tmp_path),
             "destination": tmp_path / "coverage", "baseline": None},
        ),
    }


def test_the_handler_table_names_the_five_operations_and_each_resolves() -> None:
    assert sorted(invocation_runtime.HANDLERS) == [
        "coding", "collect", "grading", "preflight", "review",
    ]
    for target in invocation_runtime.HANDLERS.values():
        module, name = target.split(":")
        assert callable(getattr(importlib.import_module(module), name))


@pytest.mark.parametrize("operation", ["coding", "grading", "review", "collect"])
def test_each_handler_passes_its_validated_arguments_to_its_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    owner, name, arguments = _cases(tmp_path)[operation]
    seen: dict[str, Any] = {}

    def implementation(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ({"report": 1}, "1.0", "tool") if operation == "collect" else {"done": True}

    monkeypatch.setattr(owner, name, implementation)
    # As a caller sends it: the model dumped to JSON.
    value = getattr(operations, operation)(_MODELS[operation](**arguments).model_dump(mode="json"))
    assert seen == arguments
    assert value == ([{"report": 1}, "1.0", "tool"] if operation == "collect" else {"done": True})


@pytest.mark.parametrize("mode", ["check", "prepare"])
def test_preflight_runs_the_check_or_the_preparation_on_the_same_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["check", "prepare"]
) -> None:
    config = _evaluation(tmp_path)
    calls: list[tuple[str, evaluation.Evaluation, Path]] = []

    def check(config: evaluation.Evaluation, output: Path) -> dict[str, object]:
        calls.append(("check", config, output))
        return {"report": True}

    def prepare(config: evaluation.Evaluation, output: Path) -> Path:
        calls.append(("prepare", config, output))
        return output / "run"

    monkeypatch.setattr(evaluation, "check_evaluation", check)
    monkeypatch.setattr(evaluation, "prepare_evaluation", prepare)
    arguments = operations.Preflight(mode=mode, config=config, output=tmp_path / "runs")
    value = operations.preflight(arguments.model_dump(mode="json"))
    # The evaluation survives JSON unchanged, its resolved guidance path included.
    assert calls == [(mode, config, tmp_path / "runs")]
    assert calls[0][1].treatments["candidate"].guidance == [tmp_path / "guidance.md"]
    assert value == ({"report": True} if mode == "check" else str(tmp_path / "runs/run"))


@pytest.mark.parametrize(
    ("operation", "field"),
    [("coding", "area"), ("grading", "directory"), ("review", "output_schema"),
     ("collect", "root"), ("collect", "collector")],
)
def test_a_relative_path_is_refused_since_the_handler_runs_elsewhere(
    tmp_path: Path, operation: str, field: str
) -> None:
    _, _, arguments = _cases(tmp_path)[operation]
    if field == "collector":
        relative = arguments["collector"].model_copy(update={"config": Path("coverage.yaml")})
        arguments = {**arguments, "collector": relative.model_dump(mode="json")}
    else:
        arguments = {**arguments, field: "relative/path"}
    with pytest.raises(ValidationError, match="is not an absolute path"):
        _MODELS[operation].model_validate(arguments)


def test_an_unknown_argument_is_refused(tmp_path: Path) -> None:
    _, _, arguments = _cases(tmp_path)["coding"]
    with pytest.raises(ValidationError, match="extra"):
        operations.Coding.model_validate({**arguments, "on_spawn": "anything"})
