"""The handler table: the five operations a scope may run, and the arguments each accepts."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

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
from pluginbench.measurement_profiles import CoverageSettings, MeasurementSettings


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

    def check(config: evaluation.Evaluation, output: Path, **kwargs: Any) -> dict[str, object]:
        calls.append(("check", config, output))
        return {"report": True}

    def prepare(config: evaluation.Evaluation, output: Path, **kwargs: Any) -> Path:
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


def _every_path_evaluation(tmp_path: Path) -> dict[str, Any]:
    """A resolved evaluation that sets every nested host path, dumped as a caller sends it."""
    for name in ("guidance.md", "review.md", "skill.md"):
        (tmp_path / name).write_text("Text.\n")
    (tmp_path / "fallow.json").write_text("{}\n")
    (tmp_path / "tasks").mkdir()
    raw = {
        "runner": {"harness": "codex", "model": "gpt-6-luna", "reasoning": "low"},
        "guidance": {"candidate": ["guidance.md"]},
        "tasks": {"source": "tasks", "ids": ["one"]},
        "measurements": ["tokens", "lines", "review"],
        "reviewer": {
            "harness": "codex", "model": "gpt-6-luna", "reasoning": "low",
            "guidance": ["review.md"],
        },
        "measurement_settings": {"fallow_config": "fallow.json"},
        "repeats": 1,
    }
    dumped: dict[str, Any] = evaluation.resolve_evaluation(raw, tmp_path).model_dump(mode="json")
    return dumped


@pytest.mark.parametrize(
    "field",
    ["treatment guidance", "treatment skills", "task source", "reviewer guidance",
     "fallow config"],
)
def test_a_relative_path_nested_in_the_evaluation_is_refused(tmp_path: Path, field: str) -> None:
    config = _every_path_evaluation(tmp_path)
    arguments = {"mode": "check", "config": config, "output": str(tmp_path / "runs")}
    operations.Preflight.model_validate(arguments)  # Resolved, so every path is absolute.
    if field == "treatment guidance":
        config["treatments"]["candidate"]["guidance"] = ["guidance.md"]
    elif field == "treatment skills":
        config["treatments"]["candidate"]["skills"] = ["skills/one"]
    elif field == "task source":
        config["tasks"]["source"] = "tasks"
    elif field == "reviewer guidance":
        config["reviewer"]["guidance"] = ["review.md"]
    else:
        config["measurement_settings"]["fallow_config"] = "fallow.json"
    with pytest.raises(ValidationError, match="is not an absolute path"):
        operations.Preflight.model_validate(arguments)


# What each handler runs, and the callers that may still call it directly: only code that
# already runs inside an operation, or a helper with no production caller.
_ROUTED = {
    "invoke_native": set(),
    "regrade_native": set(),
    "invoke": set(),
    # Only when already inside an operation, whose scope and budget it runs under.
    "_run_collector": {("collectors", "_contained")},
    "check_evaluation": set(),
    # Inside the preflight operation's own check.
    "run_reference_agents": {("evaluation", "_reference_agents")},
    # `run_evaluation` is a test helper that no production code calls.
    "prepare_evaluation": {("evaluation", "run_evaluation")},
}


def test_every_handled_implementation_is_called_only_through_its_operation() -> None:
    """A model call, trial or container started outside `operations.run` would carry no scope
    and no label, so recovery could not find it."""
    package = Path(operations.__file__).parent
    direct: list[tuple[str, str, str]] = []
    for path in sorted(package.glob("*.py")):
        if path.stem == "operations":
            continue
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in _ROUTED and (path.stem, function.name) not in _ROUTED[name]:
                    direct.append((path.stem, function.name, name))
    # `codex.invoke` aside, `invoke` names no other function PluginBench calls.
    assert sorted(set(direct)) == []


def test_the_image_budgets_match_harbor_backends_pull_and_build_timeouts() -> None:
    """`operations` restates them so the hashed launcher file stays unchanged; a change to
    either timeout there must change the preflight budget too."""
    timeouts: dict[str, object] = {}
    for node in ast.walk(ast.parse(Path(harbor_backend.__file__).read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_docker"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ("pull", "build")
        ):
            [timeout] = [item.value for item in node.keywords if item.arg == "timeout"]
            timeouts[str(node.args[0].value)] = ast.literal_eval(timeout)
    assert timeouts == {"pull": operations.IMAGE_PULL, "build": operations.IMAGE_BUILD}


def test_each_operation_budget_outlasts_its_implementations_own_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scope's deadline kills a hung operation; it must never cut short a call whose own
    timeout would end it first, and a preflight's is summed over its tasks, not divided."""

    def worker(*, task_dir: Path, agent_seconds: int = 0, prepare: bool = False) -> int:
        return (5000 if prepare else 1000) + agent_seconds

    monkeypatch.setattr(harbor_backend, "_native_worker_budget", worker)
    task = tmp_path / "tasks/one"
    assert operations.coding_budget(task, 900) > worker(task_dir=task, agent_seconds=900)
    assert operations.grading_budget(task) > worker(task_dir=task)
    assert operations.review_budget(600) > 600
    assert operations.collect_budget(_collector(tmp_path)) > _collector(tmp_path).timeout_seconds
    mutation = tmp_path / "mutation.json"
    mutation.write_text(json.dumps({"coverage": {"timeout_seconds": 100}}))
    # Every run a mutation may make, each at its longest mutant timeout.
    assert operations.collect_budget(Collector(kind="mutation", config=mutation)) > 55 * (3 * 100 + 60)
    tasks = SimpleNamespace(resolve=lambda: tmp_path / "tasks", task_ids=["one", "two"])
    for name in tasks.task_ids:
        (tmp_path / "tasks" / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / "tasks" / name / "task.toml").write_text('[metadata]\nlanguage = "python"\n')
    coverage = CoverageSettings(
        command=["pytest"], report_path="coverage.json", tool_version="7",
        format="coverage-py-json", timeout_seconds=7200,
    )
    config = SimpleNamespace(
        tasks=tasks,
        measurements=["coverage", "cyclomatic"],
        measurement_settings=MeasurementSettings(coverage=coverage),
    )
    # Each task: preparation, nop and oracle, then both readiness repeats of its coverage and
    # analysis commands, each at the longest timeout it may have (#134's review: 11,400 s was
    # given where two 7,200 s coverage runs alone need 14,400 s).
    one = worker(task_dir=task, prepare=True) + operations.requalify_budget(task)
    readiness = 2 * (7200 + 7200)
    images = 3 * (600 + 1800)  # Base, grader and bundled grader, each pulled or built.
    assert operations.preflight_budget(cast(Any, config)) > 2 * (one + readiness + images)
