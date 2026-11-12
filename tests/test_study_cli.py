"""Public evaluation interface; obsolete separate measurement/review commands are gone."""

from __future__ import annotations

import fcntl
import inspect
import json
import hashlib
import os
import signal
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner
from typer.main import get_group

import pluginbench
from pluginbench import (
    claude,
    cli,
    codex,
    components,
    evaluation,
    harbor_backend,
    isolation,
    parallel,
    remeasure,
    run_lock,
    study,
    submissions,
    trial_containers,
)
from pluginbench.compare import Gate, Gates, check_gate, load_evidence
from pluginbench.harbor_backend import assert_runtime as checked_runtime
from pluginbench.ledger import Ledger
from pluginbench.remeasure import propose_remeasurement
from pluginbench.study_review import recover_review_citations
from native_support import (
    task_entry,
    execution as native_execution,
    runtime_identity as native_runtime_identity,
    synthetic_contract,
    qualify_synthetic_source,
    passing_git_probe,
    passing_reference_agents,
    reference_agent,
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> evaluation.Evaluation:
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)
    suite = tmp_path / "deep-swe"
    task = suite / "tasks" / "arbitrary-native-task"
    for name in ("environment", "tests", "solution"):
        (task / name).mkdir(parents=True)
    (task / "task.toml").write_text(
        "version='1.0'\n[agent]\ntimeout_sec=10800.0\n"
        "[environment]\ncpus=2\nmemory_mb=8192\nbuild_timeout_sec=1800.0\n"
        "docker_image='fixture:pinned'\n[verifier]\nmode='separate'\n[verifier.environment]\ndocker_image='grader:pinned'\ncpus=2\nmemory_mb=8192\nbuild_timeout_sec=1800\n"
    )
    (task / "instruction.md").write_text("Implement the task.\n")
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "tests/test.patch").write_text("")
    (tmp_path / "a.md").write_text("Use existing helpers.\n")
    (tmp_path / "b.md").write_text("Keep state minimal.\n")
    monkeypatch.setattr(
        harbor_backend,
        "_engine",
        lambda: ({"NCPU": 16, "MemTotal": 64 * 1024**3}, "unix:///fixture"),
    )
    monkeypatch.setattr(
        harbor_backend,
        "runtime_identity",
        lambda **kwargs: {
            **native_runtime_identity(),
            "resources": {
                "cpus": kwargs.get("cpus", 2),
                "memory_mb": kwargs.get("memory_mb", 8192),
            },
        },
    )
    monkeypatch.setattr(harbor_backend, "assert_runtime", lambda expected, **kwargs: expected)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    # Docker-backed checks pass here; tests that exercise a failing check replace one of these.
    monkeypatch.setattr(harbor_backend, "probe_coding_environment", passing_git_probe)
    monkeypatch.setattr(harbor_backend, "run_reference_agents", passing_reference_agents)
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])
    monkeypatch.setattr(
        isolation, "codex_code_mode_host", lambda: Path("/fixture/codex-code-mode-host")
    )
    return evaluation.resolve_evaluation(
        {
            "runner": {"harness": "codex", "model": "gpt-6.1-sol", "reasoning": "medium"},
            "guidance": {"workflow": ["a.md"], "superpowers": ["b.md"]},
            "tasks": {"source": "deep-swe/tasks", "ids": ["arbitrary-native-task"]},
            "measurements": ["tokens", "lines"],
        },
        tmp_path,
    )


def test_public_commands_cover_evaluations_reports_and_task_selection() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert set(get_group(cli.app).commands) == {
        "init",
        "check",
        "run",
        "status",
        "stop",
        "compare",
        "report",
        "review",
        "tasks",
        "judge",
    }
    assert set(get_group(cli.tasks_app).commands) == {"pick"}
    assert set(get_group(cli.judge_app).commands) == {"prepare", "run", "tally"}
    assert CliRunner().invoke(cli.app, ["--version"]).stdout.strip() == pluginbench.__version__


def test_config_dry_run_includes_fresh_vanilla_and_every_package(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(
        cli, "prepare_evaluation", lambda *args, **kwargs: pytest.fail("dry run cannot execute")
    )
    result = CliRunner().invoke(cli.app, ["run", str(path), "--dry-run", "--json"])
    assert result.exit_code == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["coding_calls"] == 9 and plan["review_calls"] == 0
    assert set(plan["treatments"]) == {"superpowers", "workflow"}
    assert "fresh vanilla" in plan["vanilla"]
    summary = CliRunner().invoke(cli.app, ["run", str(path), "--dry-run"])
    assert summary.exit_code == 0, summary.stderr
    assert "Arms: vanilla, " in summary.stdout
    assert "Coding attempts: 9   Review calls: 0" in summary.stdout
    assert not summary.stdout.lstrip().startswith("{")


def test_run_without_a_terminal_says_to_pass_yes(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(
        cli, "prepare_evaluation", lambda *args, **kwargs: pytest.fail("unconfirmed run started")
    )
    # CliRunner's stdin is not a terminal, like a script or an agent shell.
    result = CliRunner().invoke(cli.app, ["run", str(path)], input="")
    assert result.exit_code == 1
    assert "Pass --yes to start without confirmation." in result.stderr
    assert "Aborted" not in result.output


def test_vanilla_only_config_plans_two_attempts_without_treatments(
    config: evaluation.Evaluation, tmp_path: Path
) -> None:
    raw = config.model_dump(mode="json")
    raw.update(treatments={}, repeats=2)
    path = tmp_path / "vanilla.yaml"
    path.write_text(yaml.safe_dump(raw))

    result = CliRunner().invoke(cli.app, ["run", str(path), "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["treatments"] == {}
    assert plan["coding_calls"] == 2
    assert plan["review_calls"] == 0
    # Execution reloads the evaluation's frozen settings through StudyRecipe.
    recipe = study.StudyRecipe(
        schema_version=2,
        name="vanilla screen",
        tasks=tmp_path / "tasks.json",
        treatments={},
        repeats=2,
    )
    assert recipe.treatments == {}


def test_init_writes_a_starter_config_that_plans_once_its_paths_are_filled(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.stderr
    assert "pluginbench check evaluation.yaml" in result.stdout
    starter = tmp_path / "evaluation.yaml"
    written = starter.read_text()
    again = CliRunner().invoke(cli.app, ["init"])
    assert again.exit_code == 2 and "already exists" in again.stderr
    assert starter.read_text() == written
    unfilled = CliRunner().invoke(cli.app, ["run", str(starter), "--dry-run"])
    assert unfilled.exit_code == 2 and "/path/to/workflow-plugin" in unfilled.stderr
    # Fill the /path/to placeholders and the remote task source with this fixture's local ones.
    raw = yaml.safe_load(written)
    raw["treatments"] = {"workflow": {"guidance": ["a.md"]}}
    raw["tasks"] = {"source": "deep-swe/tasks", "ids": ["arbitrary-native-task"]}
    raw["reviewer"]["guidance"] = ["b.md"]
    starter.write_text(yaml.safe_dump(raw))
    planned = CliRunner().invoke(cli.app, ["run", str(starter), "--dry-run", "--json"])
    assert planned.exit_code == 0, planned.stderr
    assert json.loads(planned.stdout)["coding_calls"] == 2 * raw["repeats"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--config", "eval.yaml"],
        ["eval.yaml", "--model", "gpt-6.1-sol"],
        ["eval.yaml", "--guidance", "a.md"],
        ["eval.yaml", "--jobs", "2"],
        ["--resume", "runs/x"],
    ],
)
def test_settings_come_only_from_the_positional_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    # One way to configure a run: the config file, or a run directory's saved settings.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "eval.yaml").write_text("name: x\n")
    result = CliRunner().invoke(cli.app, ["run", *arguments, "--dry-run"])
    assert result.exit_code == 2 and "No such option" in result.output


def test_json_without_dry_run_says_what_to_add(tmp_path: Path) -> None:
    (tmp_path / "eval.yaml").write_text("name: x\n")
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path / "eval.yaml"), "--json"])
    assert result.exit_code == 2 and "add --dry-run" in result.stderr


def test_a_missing_target_says_what_to_give(tmp_path: Path) -> None:
    missing = CliRunner().invoke(cli.app, ["run", str(tmp_path / "missing.yaml"), "--dry-run"])
    assert missing.exit_code == 2 and "does not exist" in missing.stderr
    not_a_run = CliRunner().invoke(cli.app, ["run", str(tmp_path), "--dry-run"])
    assert not_a_run.exit_code == 2 and "not a run directory" in not_a_run.stderr
    assert "Traceback" not in missing.stderr + not_a_run.stderr


@pytest.mark.parametrize("version", ["codex-cli 0.162.0-alpha.1", "codex-cli 0.162.0+build.4"])
def test_harbor_rejects_cli_versions_it_cannot_pin_exactly(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    selected = config.model_copy(deep=True)
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 16)
    monkeypatch.setattr(
        harbor_backend,
        "runtime_identity",
        lambda **kwargs: {
            **native_runtime_identity(),
            "resources": {
                "cpus": kwargs.get("cpus", 2),
                "memory_mb": kwargs.get("memory_mb", 8192),
            },
        },
    )
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": version})
    monkeypatch.setattr(codex, "invoke", lambda **kwargs: pytest.fail("cannot launch"))
    root = tmp_path / "results"
    with pytest.raises(ValueError, match="cannot pin codex CLI version"):
        evaluation.run_evaluation(selected, root)
    assert not root.exists()


def test_harbor_plan_uses_task_resources_and_labels_the_shorter_agent_budget(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = config.model_copy(deep=True)
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 16)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: kwargs)
    plan = evaluation.plan_evaluation(selected)
    assert plan["container_limits"] == {"cpus": 2, "memory_mb": 8192}
    comparison = plan["comparison_identity"]
    assert isinstance(comparison, dict)
    assert comparison["isolation"] == plan["container_limits"]
    assert comparison["task_limits"][selected.tasks.ids[0]] == {
        "declared": {
            "cpus": 2,
            "memory_mb": 8192,
            "agent_timeout_seconds": 10800,
            "build_timeout_seconds": 1800,
        },
        "effective": {
            "cpus": 2,
            "memory_mb": 8192,
            "agent_timeout_seconds": 1800,
            "build_timeout_seconds": 1800,
        },
        "adapted_agent_budget": True,
    }


@pytest.mark.parametrize("override", [{"container_cpus": 1}, {"container_memory_mb": 2048}])
def test_task_resource_reduction_stops_before_setup_or_model_calls(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, int],
) -> None:
    selected = config.model_copy(update=override)
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 16)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: {})
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    monkeypatch.setattr(codex, "preflight", lambda: pytest.fail("must reject before harness setup"))
    root = tmp_path / "results"
    with pytest.raises(ValueError, match="below.*task"):
        evaluation.run_evaluation(selected, root)
    assert not root.exists()


def test_task_resources_that_do_not_fit_docker_stop_before_setup(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = config.model_copy(deep=True)
    monkeypatch.setattr(
        harbor_backend, "_engine", lambda: ({"NCPU": 2, "MemTotal": 6 * 1024**3}, "unix:///docker")
    )
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: {})
    monkeypatch.setattr(
        harbor_backend, "preflight", lambda **kwargs: pytest.fail("must reject in planning")
    )
    with pytest.raises(ValueError, match="does not fit"):
        evaluation.run_evaluation(selected, tmp_path / "results")
    assert not (tmp_path / "results").exists()


def test_mixed_task_resources_require_an_explicit_common_budget(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = config.model_copy(deep=True)
    task = selected.tasks.resolve() / "another-native-task"
    for name in ("environment", "tests", "solution"):
        (task / name).mkdir(parents=True)
    (task / "instruction.md").write_text("Implement another task.\n")
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "task.toml").write_text(
        "[agent]\ntimeout_sec=10800\n[environment]\ncpus=2\nmemory_mb=4096\nbuild_timeout_sec=1800\ndocker_image='fixture:pinned'\n[verifier]\nmode='separate'\n[verifier.environment]\ndocker_image='grader:pinned'\ncpus=2\nmemory_mb=4096\nbuild_timeout_sec=1800\n"
    )
    selected.tasks.ids.append(task.name)
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 16)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: kwargs)
    with pytest.raises(ValueError, match="common container_memory_mb"):
        evaluation.plan_evaluation(selected)
    selected.container_memory_mb = 8192
    assert evaluation.plan_evaluation(selected)["container_limits"] == {
        "cpus": 2,
        "memory_mb": 8192,
    }


def test_failed_native_preparation_stops_before_coding_or_grading(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = config.model_copy(deep=True)
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 16)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(codex, "invoke", lambda **kwargs: pytest.fail("no coding call"))
    monkeypatch.setattr(
        harbor_backend, "invoke_native", lambda **kwargs: pytest.fail("no native call")
    )

    def reject(**kwargs: Any) -> None:
        assert kwargs["task_dir"].is_dir()
        assert kwargs["limits"]["cpus"] == 2 and kwargs["limits"]["memory_mb"] == 8192
        raise ValueError("native preparation failed")

    monkeypatch.setattr(harbor_backend, "prepare_native_task", reject)
    root = tmp_path / "evaluation"
    with pytest.raises(ValueError, match="native preparation failed"):
        evaluation.run_evaluation(selected, root)
    assert not (root / "evidence/attempts").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"measurements": ["nope"]},
        {"measurements": ["review"]},
        {"repeats": 0},
        {"guidance": {"vanilla": ["a.md"]}},
        *({"jobs": jobs} for jobs in (0, 17, True, False, 1.5, "2", None)),
    ],
)
def test_invalid_contract_rejected_before_calls(
    config: evaluation.Evaluation, mutation: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        evaluation.resolve_evaluation(
            {**config.model_dump(mode="json"), **mutation}, config.tasks.resolve().parent.parent
        )


@pytest.mark.parametrize(
    "mutation,remedy",
    [
        ({"measurements": ["tokens", "test_overlap"]}, "Remove test_overlap from measurements"),
        (
            {
                "measurement_settings": {
                    "test_contribution": {"pytest_version": "8.4.2", "coverage_version": "7.10.7"}
                }
            },
            "Omit test_contribution or set it to null",
        ),
    ],
)
def test_removed_leave_one_out_overlap_is_rejected_with_its_replacement(
    config: evaluation.Evaluation, mutation: dict[str, Any], remedy: str
) -> None:
    with pytest.raises(ValueError) as error:
        evaluation.resolve_evaluation(
            {**config.model_dump(mode="json"), **mutation}, config.tasks.resolve().parent.parent
        )
    message = str(error.value)
    assert "leave-one-out test-overlap measure" in message
    assert "Redundant added tests, measured from per-test coverage, replace it" in message
    assert remedy in message


def test_saved_evaluation_with_null_test_contribution_still_loads(
    config: evaluation.Evaluation,
) -> None:
    # The exact settings recorded by saved evaluations, including comparison identities.
    saved: dict[str, object] = {
        "source_scope": None,
        "test_scope": None,
        "fallow_config": None,
        "coverage": None,
        "coverage_profiles": {},
        "test_contribution": None,
        "require_ready": True,
    }
    selection = {**config.model_dump(mode="json"), "measurement_settings": saved}
    loaded = evaluation._saved_evaluation(selection)
    assert loaded.measurement_settings.model_dump(mode="json") == saved


def test_fixed_reviewer_separate_from_candidate_guidance(config: evaluation.Evaluation) -> None:
    raw = config.model_dump(mode="json")
    raw["measurements"].append("review")
    raw["reviewer"] = {
        "harness": "claude",
        "model": "claude-sonnet-5-5",
        "reasoning": "high",
        "guidance": [str(config.treatments["workflow"].guidance[0])],
    }
    selected = evaluation.resolve_evaluation(raw, Path.cwd())
    plan = evaluation.plan_evaluation(selected)
    assert plan["review_calls"] == plan["coding_calls"] == 9
    identity = plan["comparison_identity"]
    assert isinstance(identity, dict) and identity["review_criteria_sha256"]
    assert selected.reviewer and selected.reviewer.harness == "claude"


@pytest.mark.parametrize("timeout", [0, -1, True, False, 1.5, "600"])
def test_invalid_review_timeout_rejected_before_calls(
    config: evaluation.Evaluation, timeout: object
) -> None:
    raw = config.model_dump(mode="json")
    raw["measurements"].append("review")
    raw["reviewer"] = {
        "harness": "codex",
        "model": "gpt-6-astra",
        "reasoning": "high",
        "timeout_seconds": timeout,
    }
    with pytest.raises(ValueError, match="timeout_seconds"):
        evaluation.resolve_evaluation(raw, Path.cwd())


def test_comparison_identity_ignores_guidance_label_but_tracks_task_and_runtime(
    config: evaluation.Evaluation,
) -> None:
    original = evaluation.plan_evaluation(config)["comparison_identity"]
    changed = config.model_copy(deep=True)
    changed.treatments = {"other": config.treatments["workflow"]}
    assert evaluation.plan_evaluation(changed)["comparison_identity"] == original
    changed.jobs = 1 if config.jobs != 1 else 2
    assert evaluation.plan_evaluation(changed)["comparison_identity"] != original
    changed.jobs = config.jobs
    changed.capabilities.subagents = not config.capabilities.subagents
    assert evaluation.plan_evaluation(changed)["comparison_identity"] != original
    changed.capabilities = config.capabilities.model_copy()
    changed.runner.reasoning = "high"
    assert evaluation.plan_evaluation(changed)["comparison_identity"] != original
    (config.tasks.resolve() / config.tasks.ids[0] / "instruction.md").write_text("Changed task")
    assert evaluation.plan_evaluation(config)["comparison_identity"] != original


@pytest.mark.parametrize("cores", [None, 1, 18])
def test_default_jobs_is_two_on_every_machine_and_at_most_four(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch, cores: int | None
) -> None:
    # jobs is part of the comparison identity, so its default must not depend on the machine.
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    raw = config.model_dump(mode="json")
    raw.pop("jobs")
    selected = evaluation.resolve_evaluation(raw, Path.cwd())
    plan = evaluation.plan_evaluation(selected)
    identity = plan["comparison_identity"]
    assert isinstance(identity, dict)
    assert selected.jobs == plan["jobs"] == identity["jobs"] == 2
    with pytest.raises(ValueError, match="less than or equal to 4"):
        evaluation.resolve_evaluation({**raw, "jobs": 5}, Path.cwd())


@pytest.mark.parametrize(
    "unavailable,recover,citation_error,collection_unavailable,collection_integrity,"
    "no_profiles,readiness_required,review_timeout",
    [
        (False, False, False, False, False, False, False, None),
        (True, False, False, False, False, False, False, None),
        (False, True, False, False, False, False, False, None),
        (True, True, False, False, False, False, False, None),
        (False, False, True, False, False, False, False, None),
        (False, False, False, True, False, False, False, None),
        (False, False, False, False, True, False, False, None),
        (False, False, False, False, False, True, False, None),
        (False, False, False, True, False, False, True, None),
        (False, False, False, False, False, False, True, None),
        (False, False, False, False, False, False, False, 600),
    ],
)
def test_integrated_evaluation_and_report_regeneration(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: bool,
    recover: bool,
    citation_error: bool,
    collection_unavailable: bool,
    collection_integrity: bool,
    no_profiles: bool,
    readiness_required: bool,
    review_timeout: int | None,
) -> None:
    config.measurement_settings.require_ready = readiness_required
    config.jobs = 1 if recover else 4
    config.measurements += ["coverage", "review"]
    standards = tmp_path / "review-standards.md"
    standards.write_text("Report actionable correctness findings only.\n")
    config.reviewer = evaluation.Reviewer(
        harness="codex", model="gpt-6-astra", reasoning="high", guidance=[standards]
    )
    if review_timeout is not None:
        # Validate the explicit setting through the same config boundary as real runs.
        raw = config.model_dump(mode="json")
        raw["reviewer"]["timeout_seconds"] = review_timeout
        config = evaluation.resolve_evaluation(raw, Path.cwd())
    separate_coverage_scope = not any(
        (
            unavailable,
            recover,
            citation_error,
            collection_unavailable,
            collection_integrity,
            no_profiles,
            readiness_required,
        )
    )
    config.measurement_settings.source_scope = ["*"] if separate_coverage_scope else ["src/**"]
    config.measurement_settings.test_scope = ["tests/**"]
    from pluginbench.measurement_profiles import CoverageSettings

    config.measurement_settings.coverage = CoverageSettings(
        command=["python3", "-m", "coverage"],
        report_path="coverage/lcov.info",
        tool_version="fixture-1",
        source_scope=["src/**"],
    )
    if no_profiles:
        # A selected measure that no task can collect now stops the check, so select none.
        config.measurements.remove("coverage")

    def prepare(**kwargs: Any) -> dict[str, object]:
        destination = kwargs["destination"]
        destination.mkdir(parents=True)
        entry = task_entry(destination, kwargs["task_dir"].name, version=kwargs["version"])
        provenance = entry["provenance"]
        assert isinstance(provenance, dict)
        identity: dict[str, object] = json.loads(provenance["native_task_identity"])
        native_task = Path(str(identity["task_dir"]))
        shutil.rmtree(native_task)
        shutil.copytree(kwargs["task_dir"], native_task)
        identity.update(
            task_assets_sha256=harbor_backend.native_task_assets(native_task),
            original_task_assets_sha256=harbor_backend.native_task_assets(kwargs["task_dir"]),
            resources=kwargs["limits"],
            isolation=harbor_backend.runtime_identity(
                cpus=kwargs["limits"]["cpus"], memory_mb=kwargs["limits"]["memory_mb"]
            ),
        )
        qualify_synthetic_source(identity)
        if readiness_required and not collection_unavailable:
            identity["source_qualification"] = {
                "status": "unknown",
                "reason": "grader baseline differs from coding fixture",
            }
        return identity

    def native_coverage(**kwargs: Any) -> dict[str, object]:
        destination = kwargs["destination"]
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.raw").write_text(
            "SF:src/answer.py\nFN:1,answer\nFNDA:1,answer\nFNF:1\nFNH:1\nBRDA:1,0,0,1\nBRF:1\nBRH:1\nend_of_record\n"
        )
        return {"exit_code": 0, "timed_out": False, "report_collected": True}

    calls = []
    judge_calls = []
    call_lock = Lock()

    def invoke(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("output_schema") is not None:
            judge_calls.append(kwargs)
            packet = json.loads((kwargs["area"] / "workspace/packet.json").read_text())
            issues = []
            if citation_error:
                source = (kwargs["area"] / "workspace/after/src/answer.py").read_text().strip()
                issues = [
                    {
                        "issue_id": "I1",
                        "category": "correctness",
                        "severity": "P2",
                        "description": "Synthetic source-bound judgment",
                        "rationale": "Fixture only",
                        "path": "src/answer.py",
                        "line": 1,
                        "evidence": "\t" + source,
                        "smallest_fix": "Fixture only",
                    }
                ]
            return {
                "successful_native_completion": True,
                "actual_primary_models": ["gpt-6-astra"],
                "runtime_effort_evidence": ["high"],
                "command_items": [],
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "startup": [{"type": "session_meta", "payload": {"cli_version": "judge-version"}}],
                "final": json.dumps(
                    {
                        "packet_id": packet["packet_id"],
                        "status": "reviewed",
                        "issues": issues,
                        "limitations": ["Fixture only"],
                        "outside_packet_access": False,
                    }
                ),
            }
        with call_lock:
            calls.append(kwargs)
            call_number = len(calls)
        assert (kwargs["area"] / "workspace/src/answer.py").read_text() == "answer = 0\n"
        if call_number != 1:
            (kwargs["area"] / "workspace/src/answer.py").write_text("answer = 1\n")
        elif unavailable:
            (kwargs["area"] / "workspace/src/alias.py").symlink_to("answer.py")
        if separate_coverage_scope:
            (kwargs["area"] / "workspace/outside.py").write_text("outside = 1\n")
        result = native_execution(
            kwargs["area"],
            kwargs["protocol"]["native_task_identity"],
            reward=0 if call_number == 1 else 1,
            timeout=config.timeout_seconds,
            version="0.167.0",
        )
        if separate_coverage_scope:
            verifier = result["native_verifier_result"]
            assert isinstance(verifier, dict)
            rewards = verifier["rewards"]
            assert isinstance(rewards, dict)
            rewards.update(f2p=1.0, p2p=1.0)
        result.update(
            usage={"input_tokens": 100 + call_number, "output_tokens": 20},
            startup=[
                {
                    "type": "session_meta",
                    "payload": {
                        "cli_version": "0.167.0" if separate_coverage_scope else "test-version"
                    },
                }
            ],
        )
        return result

    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(harbor_backend, "prepare_native_task", prepare)
    monkeypatch.setattr(harbor_backend, "run_native_command", native_coverage)
    monkeypatch.setattr(codex, "runtime_environment", lambda *args: ({}, ()))
    monkeypatch.setattr(codex, "invoke", invoke)
    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    root = tmp_path / "results"
    reward_decoder = harbor_backend.binary_native_reward
    if separate_coverage_scope:
        monkeypatch.setattr(study, "_binary_native_reward", lambda value: None)

    def _invalid_lcov(destination: Path) -> dict[str, object]:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.raw").write_text("SF:../escape.py\nend_of_record\n")
        return {"exit_code": 0, "timed_out": False, "report_collected": True}

    if collection_integrity:
        monkeypatch.setattr(
            harbor_backend,
            "run_native_command",
            lambda **kwargs: _invalid_lcov(kwargs["destination"]),
        )
        with pytest.raises(ValueError, match="Coverage"):
            evaluation.run_evaluation(config, root)
        assert not calls and not judge_calls
        assert not (root / "evidence/run.json").exists()
        assert not (root / "report").exists()
        assert not (root / "evidence/review-revisions").exists()
        return
    if recover:
        execute = harbor_backend.run_native_command

        def interrupted_initial(*args: Any, **kwargs: Any) -> Any:
            if "measurement-readiness" in str(kwargs.get("destination")):
                return execute(*args, **kwargs)
            raise KeyboardInterrupt("interrupted measurement job")

        monkeypatch.setattr(harbor_backend, "run_native_command", interrupted_initial)
        with pytest.raises(KeyboardInterrupt):
            evaluation.run_evaluation(config, root)
        assert len(calls) == 9 and not judge_calls
        if unavailable:
            # Simulate a historical file-only selection at the persisted resume boundary.
            saved = evaluation._load_selection(root)
            treatments = saved.pop("treatments")
            assert isinstance(treatments, dict)
            saved["guidance"] = {name: item["guidance"] for name, item in treatments.items()}
            saved.pop("capabilities")
            original_comparison = saved["comparison_identity"]
            evaluation._save_selection(root, saved)
        assert evaluation.plan_resume(root)["jobs"] == config.jobs
        if unavailable:
            assert evaluation._load_selection(root)["comparison_identity"] == original_comparison
        monkeypatch.setattr(harbor_backend, "run_native_command", execute)
        for directory in ("diagnostic-source", "snapshot"):
            source = next((root / "evidence/attempts").glob(f"*/handoff/{directory}/src/answer.py"))
            mode = source.stat().st_mode
            source.chmod(0o755)
            with pytest.raises(
                ValueError, match="metadata changed|canonical graded source changed"
            ):
                evaluation.plan_resume(root)
            source.chmod(mode)
            assert len(calls) == 9 and not judge_calls
        frozen = next(
            (root / "evidence/observer-revisions/measurements/profiles").rglob("config.json")
        )
        original_profile = frozen.read_bytes()
        frozen.write_bytes(original_profile + b"\n ")
        with pytest.raises(ValueError, match="frozen observer profiles changed"):
            evaluation.plan_resume(root)
        frozen.write_bytes(original_profile)
        criteria = root / "inputs/review-0.md"
        original_criteria = criteria.read_bytes()
        criteria.write_text("Use different criteria.\n")
        with pytest.raises(ValueError, match="frozen review criteria changed"):
            evaluation.plan_resume(root)
        criteria.write_bytes(original_criteria)
        existing_review = root / "evidence/review-revisions/review"
        existing_review.mkdir(parents=True)
        with pytest.raises(ValueError, match="model calls cannot be replayed"):
            evaluation.plan_resume(root)
        existing_review.rmdir()
        rejected = CliRunner().invoke(cli.app, ["run", str(root), "--output", "other"])
        assert rejected.exit_code == 2 and "resumes in place" in rejected.stderr
        dry = CliRunner().invoke(cli.app, ["run", str(root), "--dry-run", "--json"])
        assert dry.exit_code == 0, dry.stderr
        assert json.loads(dry.stdout)["coding_calls"] == 0
        assert not (root / "report").exists()
        for interruption in ("absent", "empty", "failed"):

            def interrupted(evidence: Path, *, revision: str, **kwargs: Any) -> None:
                if interruption != "absent":
                    directory = evidence / "observer-revisions" / revision
                    directory.mkdir()
                if interruption == "failed":
                    (directory / "revision.json").write_text(
                        json.dumps({"status": "failed", "error": "profile freeze interrupted"})
                    )
                raise KeyboardInterrupt

            monkeypatch.setattr(evaluation, "propose_remeasurement", interrupted)
            with pytest.raises(KeyboardInterrupt):
                evaluation.resume_evaluation(root)
            assert evaluation.plan_resume(root)["coding_calls"] == 0
            assert len(calls) == 9 and not judge_calls
        monkeypatch.setattr(evaluation, "propose_remeasurement", propose_remeasurement)
        for treatment in config.treatments.values():
            files = treatment.guidance
            for file in files:
                file.unlink()
        standards.unlink()

        def missing_host() -> Path:
            raise ValueError("Codex reviews need an executable codex-code-mode-host")

        # Resumed reviews get the same sandbox probe before their first call.
        with monkeypatch.context() as patched:
            patched.setattr(isolation, "codex_code_mode_host", missing_host)
            blocked = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
        assert blocked.exit_code == 2 and "codex-code-mode-host" in blocked.stderr
        assert len(calls) == 9 and not judge_calls
        result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
        assert result.exit_code == 0, result.stderr
        assert "Coding attempts: 0   Review calls:" in result.stdout
        failed = root / "evidence/observer-revisions/measurements/revision.json"
        assert json.loads(failed.read_text())["status"] == "failed"
    else:
        if collection_unavailable:
            prepared_collection = harbor_backend.run_native_command

            def failed_collection(**kwargs: Any) -> dict[str, object]:
                # Coverage broken on every task before coding blocks the check, so only the
                # required-readiness case keeps it broken there. Otherwise collection fails
                # after coding.
                if not readiness_required and "measurement-readiness" in str(
                    kwargs.get("destination")
                ):
                    return prepared_collection(**kwargs)
                return {"exit_code": 3, "timed_out": False, "report_collected": False}

            monkeypatch.setattr(harbor_backend, "run_native_command", failed_collection)
        if readiness_required:
            with pytest.raises(ValueError, match="no coding calls"):
                evaluation.run_evaluation(config, root)
            assert not calls and not judge_calls
            readiness = json.loads((root / "measurement-readiness.json").read_text())
            if collection_unavailable:
                assert all(row["coverage"]["readiness"] == "failed" for row in readiness.values())
            else:
                assert all(
                    row["submission_source"]["reason"]
                    == "grader baseline differs from coding fixture"
                    for row in readiness.values()
                )
            return
        if citation_error:
            recover_citations = recover_review_citations

            def interrupt_recovery(*args: object) -> str:
                raise KeyboardInterrupt

            monkeypatch.setattr(evaluation, "recover_review_citations", interrupt_recovery)
            with pytest.raises(KeyboardInterrupt):
                evaluation.run_evaluation(config, root)
            assert json.loads((root / "evaluation.json").read_text())["review_revision"] == "review"
            monkeypatch.setattr(evaluation, "recover_review_citations", recover_citations)
            evaluation.resume_evaluation(root)
        else:
            evaluation.run_evaluation(config, root)
    # check previews exactly the cache keys the run then decides with.
    checked = json.loads((root / "check.json").read_text())
    planned = json.loads((root / "evidence/plan.json").read_text())
    assert {
        (row["task_id"], row["repeat"]): row["cache_key"] for row in checked["vanilla_cache"]
    } == {
        (entry["task_id"], entry["attempt"]): planned["baseline_cache"][entry["id"]]["cache_key"]
        for entry in planned["entries"]
        if entry["arm"] == "baseline"
    }
    if separate_coverage_scope:
        original_receipts = {
            path: path.read_bytes()
            for pattern in ("attempts/*/*/correctness.json", "attempts/*/*/verifier-result.json")
            for path in (root / "evidence").glob(pattern)
        }
        model_calls = len(calls), len(judge_calls)
        monkeypatch.setattr(study, "_binary_native_reward", reward_decoder)
        evaluation.regenerate_report(root)
        # A saved run opens through any spelling of its path, as a new one does.
        evaluation.regenerate_report(root / ".." / root.name)
        assert (len(calls), len(judge_calls)) == model_calls
        assert all(path.read_bytes() == data for path, data in original_receipts.items())
    data = json.loads((root / "report/results.json").read_text())
    assert data["jobs"] == data["comparison_identity"]["jobs"] == config.jobs
    assert json.loads((root / "evidence/run.json").read_text())["jobs"] == config.jobs
    saved_selection = evaluation._load_selection(root)
    measurement = (
        root / "evidence/observer-revisions" / str(saved_selection["measurement_revision"])
    )
    review = root / "evidence/review-revisions" / str(saved_selection["review_revision"])
    if no_profiles:
        assert saved_selection["measurement_required"] is False
        assert not (root / "evidence/observer-revisions").exists()
        assert evaluation.plan_resume(root)["collect_measurements"] is False
        counts = len(calls), len(judge_calls)
        evaluation.resume_evaluation(root)
        assert (len(calls), len(judge_calls)) == counts
    else:
        assert json.loads((measurement / "revision.json").read_text())["jobs"] == config.jobs
    assert json.loads((review / "protocol.json").read_text())["jobs"] == config.jobs
    assert len(calls) == data["assigned"] == 9
    assert {row["guidance"] for row in data["attempts"]} == {"baseline", "workflow", "superpowers"}
    assert sum(row["correctness"] == "fail" for row in data["attempts"]) == (
        0 if unavailable else 1
    )
    assert {row["family"] for row in data["observations"]} == {
        "tokens",
        "lines",
        "coverage",
        "review",
    } - ({"coverage"} if no_profiles else set())
    assert len(judge_calls) == (8 if unavailable else 9)
    expected_review_timeout = 1800 if review_timeout is None else 600
    assert all(call["timeout"] == expected_review_timeout for call in judge_calls)
    assert all(call["timeout"] == 1800 for call in calls)
    assert (
        json.loads((review / "protocol.json").read_text())["timeout_seconds"]
        == expected_review_timeout
    )
    coverage = [row for row in data["observations"] if row["metric"] == "Branch coverage %"]
    findings = [row for row in data["observations"] if row["metric"] == "Total review findings"]
    assert len(findings) == 9 and len(coverage) == (0 if no_profiles else 9)
    if separate_coverage_scope:
        assert all(row["status"] == "measured" for row in coverage)
        source_lines = [
            row for row in data["observations"] if row["metric"] == "Source lines added"
        ]
        assert sorted(row["value"] for row in source_lines) == [1] + [2] * 8
    assert sum(row["value"] is None for row in coverage) == (
        9 if collection_unavailable else 0 if no_profiles else int(unavailable)
    )
    assert sum(row["value"] is None for row in findings) == int(unavailable)
    assert data["comparison_identity"]["observed_reviewer_profiles"]
    if collection_unavailable:
        assert data["measurement_collection"]["status"] == "partial"
        assert data["measurement_collection"]["unavailable_families"] == 18
        assert all(not row["complete"] for row in data["measurement_completeness"])
        assert all(row["value"] is not None for row in findings)
        assert len(calls) == 9 and len(judge_calls) == 9
        assert evaluation.plan_resume(root)["coding_calls"] == 0
        assert evaluation.plan_resume(root)["review_calls"] == 0
    if citation_error:
        assert data["independent_review"]["status"] == "complete"
        assert all(row["value"] == 1 for row in findings)
        original_review = root / "evidence/review-revisions/review/review-state.json"
        assert json.loads(original_review.read_text())["status"] == "incomplete"
        assert (
            json.loads((root / "evaluation.json").read_text())["review_revision"]
            == "review-citations"
        )
    if not any((unavailable, recover, collection_unavailable)):
        saved = evaluation._load_selection(root)
        protected = {
            path: path.read_bytes()
            for path in (
                root / "evaluation.json",
                root / "evaluation-binding.json",
                root / "report/results.json",
                root / "report/results.csv",
                root / "report/report.html",
            )
        }
        native_counts = (len(calls), len(judge_calls))
        revision = "review" if citation_error else str(saved["review_revision"])
        projected_path = evaluation.regenerate_report(root, review_revision=revision)
        assert (
            projected_path
            == root / f"evidence/review-revisions/{revision}/evaluation-report/results.json"
        )
        projected = json.loads(projected_path.read_text())
        if citation_error:
            assert all(
                row["value"] is None
                for row in projected["observations"]
                if row["metric"] == "Total review findings"
            )
        else:
            assert projected["observations"] == data["observations"]
        assert projected["review_projection"]["native_invocations_in_these_revisions"] == 9
        assert (len(calls), len(judge_calls)) == native_counts
        assert all(path.read_bytes() == content for path, content in protected.items())
        changed_settings = {**saved}
        if review_timeout is None:
            changed_settings["timeout_seconds"] = config.timeout_seconds + 1
        else:
            saved_reviewer = saved["reviewer"]
            assert isinstance(saved_reviewer, dict)
            changed_settings["reviewer"] = {
                **saved_reviewer,
                "timeout_seconds": expected_review_timeout + 1,
            }
        with monkeypatch.context() as changed:
            changed.setattr(evaluation, "_load_selection", lambda _: changed_settings)
            with pytest.raises(ValueError, match="fixed comparison settings"):
                evaluation.regenerate_report(root, review_revision=revision)
        assert all(path.read_bytes() == content for path, content in protected.items())
    original = (root / "report/results.json").read_bytes()
    monkeypatch.setattr(
        codex, "invoke", lambda **kwargs: pytest.fail("report cannot invoke models")
    )
    assert evaluation.regenerate_report(root).read_bytes() == original
    if collection_unavailable:
        evaluation.resume_evaluation(root)
        assert len(calls) == len(judge_calls) == 9
        assert (root / "report/results.json").read_bytes() == original
    if citation_error:
        result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
        assert result.exit_code == 0, result.stderr
        assert "Coding attempts: 0   Review calls: 0" in result.stdout
        assert (root / "report/results.json").read_bytes() == original
    if recover:
        saved = json.loads((root / "evaluation.json").read_text())
        saved["pending_measurement_revision"] = saved["measurement_revision"]
        saved["measurement_revision"] = None
        evaluation._save_selection(root, saved)
        result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
        assert result.exit_code == 0, result.stderr
        assert "Coding attempts: 0   Review calls: 0" in result.stdout
        assert (root / "report/results.json").read_bytes() == original
    selection = root / "evaluation.json"
    selection.write_text(selection.read_text().replace('"repeats": 3', '"repeats": 4'))
    with pytest.raises(ValueError, match="settings changed"):
        evaluation.regenerate_report(root)


def test_review_credentials_checked_before_coding(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = config.model_dump(mode="json")
    raw["measurements"].append("review")
    raw["reviewer"] = {"harness": "claude", "model": "claude-sonnet-5-5", "reasoning": "high"}
    selected = evaluation.resolve_evaluation(raw, Path.cwd())
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(claude, "preflight", lambda: {"harness_version": "2.1.0"})

    def credentials(private: Path, harness: str) -> tuple[dict[str, str], tuple[str, ...]]:
        if harness == "claude":
            raise ValueError("conflicting Claude credentials")
        return {}, ()

    monkeypatch.setattr(codex, "runtime_environment", credentials)
    monkeypatch.setattr(
        codex, "invoke", lambda **kw: pytest.fail("cannot spend before credential checks")
    )
    with pytest.raises(ValueError, match="conflicting Claude credentials"):
        evaluation.run_evaluation(selected, tmp_path / "never-started")
    assert not (tmp_path / "never-started").exists()


@pytest.mark.parametrize(
    "model", ["sonnet", "opus", "fable", "sonnet[1m]", "claude-sonnet-5-5[1m]"]
)
def test_claude_aliases_rejected_before_calls(model: str) -> None:
    with pytest.raises(ValueError, match="exact claude-"):
        evaluation.Runner(harness="claude", model=model, reasoning="high")


REAL_CODE_MODE_HOST = isolation.codex_code_mode_host
LCOV = "SF:src/answer.py\nDA:1,1\nLF:1\nLH:1\nFN:1,answer\nFNDA:1,answer\nFNF:1\nFNH:1\nend_of_record\n"


def _prepare_fixture_task(**kwargs: Any) -> dict[str, object]:
    """Offline preparation: the frozen task is a copy of the real config's task."""
    destination = kwargs["destination"]
    destination.mkdir(parents=True)
    entry = task_entry(destination, kwargs["task_dir"].name, version=kwargs["version"])
    provenance = entry["provenance"]
    assert isinstance(provenance, dict)
    identity: dict[str, object] = json.loads(provenance["native_task_identity"])
    native_task = Path(str(identity["task_dir"]))
    shutil.rmtree(native_task)
    shutil.copytree(kwargs["task_dir"], native_task)
    identity.update(
        task_assets_sha256=harbor_backend.native_task_assets(native_task),
        original_task_assets_sha256=harbor_backend.native_task_assets(kwargs["task_dir"]),
        resources=kwargs["limits"],
        isolation=harbor_backend.runtime_identity(
            cpus=kwargs["limits"]["cpus"], memory_mb=kwargs["limits"]["memory_mb"]
        ),
    )
    qualify_synthetic_source(identity)
    return identity


def _lcov(**kwargs: Any) -> dict[str, object]:
    kwargs["destination"].mkdir(parents=True, exist_ok=True)
    (kwargs["destination"] / "report.raw").write_text(LCOV)
    return {"exit_code": 0, "timed_out": False, "report_collected": True}


@pytest.fixture
def checkable(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """One coverage task, one task with no coverage tool, a Codex reviewer; every check passes."""
    covered = config.tasks.resolve() / "covered-native-task"
    shutil.copytree(config.tasks.resolve() / config.tasks.ids[0], covered)
    toml = (covered / "task.toml").read_text()
    (covered / "task.toml").write_text(
        toml.replace(
            "[agent]", "[metadata]\nrepository_url='https://example.invalid/covered'\n[agent]"
        )
    )
    standards = tmp_path / "review-standards.md"
    standards.write_text("Report actionable correctness findings only.\n")
    raw = config.model_dump(mode="json")
    raw["tasks"]["ids"].append(covered.name)
    raw["measurements"] += ["coverage", "review"]
    raw["measurement_settings"]["coverage_profiles"] = {
        "https://example.invalid/covered": {
            "command": ["python3", "-m", "coverage"],
            "report_path": "coverage/lcov.info",
            "tool_version": "fixture-1",
        }
    }
    raw["reviewer"] = {
        "harness": "codex",
        "model": "gpt-6-astra",
        "reasoning": "high",
        "guidance": [str(standards)],
    }
    calls: list[object] = []
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(harbor_backend, "prepare_native_task", _prepare_fixture_task)
    monkeypatch.setattr(harbor_backend, "run_native_command", _lcov)
    monkeypatch.setattr(codex, "invoke", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(harbor_backend, "invoke_native", lambda **kwargs: calls.append(kwargs))
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(raw))
    return {"path": path, "calls": calls, "output": tmp_path / "checked"}


def test_check_passes_tasks_with_and_without_a_coverage_tool_in_one_config(
    checkable: dict[str, Any],
) -> None:
    # The pilot had to split every phase into two evaluations because of blanket require_ready.
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )
    assert result.exit_code == 0, result.stderr
    report = json.loads((checkable["output"] / "check.json").read_text())
    assert report["status"] == "ready" and report["model_calls"] == 0
    matrix = report["matrix"]
    assert matrix["covered-native-task"]["coverage"]["state"] == "ready"
    assert matrix["arbitrary-native-task"]["coverage"]["state"] == "not_configured"
    assert {
        row["state"] for task in matrix.values() for name, row in task.items() if name != "coverage"
    } == {"ready"}
    assert matrix["covered-native-task"]["coding_environment"]["image_source"] == "prebuilt"
    header = result.stdout.splitlines()[-4]
    assert header.split()[:2] == ["task", "coverage"] and "oracle" in header
    assert not checkable["calls"]
    assert not list(checkable["output"].rglob("invocation.json"))
    assert not (checkable["output"] / "evidence").exists()


def test_check_accepts_an_unverified_codex_version_and_warns_once(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.168.0"})
    with pytest.warns(UserWarning, match="Codex CLI 0.168.0 has not been checked") as warnings:
        result = CliRunner().invoke(
            cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
        )
        codex.admit_version("0.168.0")
        codex.admit_version("0.168.0")
    assert result.exit_code == 0, result.stderr
    assert len(warnings) == 1
    message = str(warnings[0].message)
    assert "0.162.1, 0.167.0, 0.167.1" in message
    assert "token usage is verified per attempt" in message
    report = json.loads((checkable["output"] / "check.json").read_text())
    assert report["status"] == "ready" and report["model_calls"] == 0
    assert report["harness_versions"]["codex"] == "0.168.0"
    assert checkable["calls"] == []


@pytest.mark.parametrize("cli_version", ["0.167.0", None])
def test_prepare_pins_the_selected_coding_cli_and_records_its_source(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch, cli_version: str | None
) -> None:
    raw = yaml.safe_load(checkable["path"].read_text())
    if cli_version is not None:
        raw["runner"]["cli_version"] = cli_version
    else:
        raw["runner"].pop("cli_version", None)
    checkable["path"].write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.1"})
    admitted: list[str] = []
    monkeypatch.setattr(codex, "admit_version", admitted.append)
    config = evaluation.load_evaluation(checkable["path"])
    root = evaluation.prepare_evaluation(config, checkable["output"])
    pinned = cli_version or "0.167.1"
    selection = evaluation._load_selection(root)
    comparison = selection["comparison_identity"]
    assert isinstance(comparison, dict)
    runner_identity = comparison["runner"]
    assert isinstance(runner_identity, dict)
    recipe = json.loads((root / "inputs/study.json").read_text())
    assert recipe["harness_version"] == pinned
    assert selection["cli_version_source"] == ("runner.cli_version" if cli_version else "host")
    assert comparison["harness_version"] == pinned
    assert "cli_version" not in runner_identity
    assert "cli_version_source" not in comparison
    identity = json.loads((root / "inputs/vanilla-identity.json").read_text())
    assert identity["harness_version"] == pinned
    for task in identity["task_environment"].values():
        assert task["toolchain_identity"]["harness_version"] == pinned
    assert admitted == [pinned]
    # The pin's provenance is not another coding identity input.
    config.runner.cli_version = pinned
    planned = evaluation.plan_evaluation(config)["comparison_identity"]
    assert isinstance(planned, dict)
    assert planned["runner"] == runner_identity
    assert not checkable["calls"]


@pytest.mark.parametrize("cli_version", ["latest", "^0.167.0", "0.167", "0.167.0\n", "0.167.0;id", 167])
def test_invalid_coding_cli_version_is_rejected_at_config_load(
    config: evaluation.Evaluation, tmp_path: Path, cli_version: object
) -> None:
    raw = config.model_dump(mode="json")
    raw["runner"]["cli_version"] = cli_version
    path = tmp_path / "invalid-version.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="cli_version"):
        evaluation.load_evaluation(path)


@pytest.mark.parametrize("cli_version", ["0.167.0-beta.1", "0.167.0+build.2"])
def test_coding_cli_version_accepts_the_exact_toolchain_version_pattern(cli_version: str) -> None:
    runner = evaluation.Runner(
        harness="codex", model="gpt-6.1-sol", reasoning="medium", cli_version=cli_version
    )
    assert runner.cli_version == cli_version
    assert cli_version in harbor_backend._cli_guard(runner.harness, runner.cli_version)


def test_required_measure_that_is_not_configured_blocks_the_check(
    checkable: dict[str, Any],
) -> None:
    raw = yaml.safe_load(checkable["path"].read_text())
    raw["measurement_settings"]["require_ready"] = True
    checkable["path"].write_text(yaml.safe_dump(raw))
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )
    assert result.exit_code == 2
    assert "arbitrary-native-task: coverage is not_configured" in result.stderr
    assert not checkable["calls"]


def test_check_reports_a_runtime_refusal_like_other_refusals(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # check reaches the saved-runtime checks through its native measurement probes.
    def refused(*args: object, **kwargs: object) -> object:
        raise isolation.RuntimeDriftError("Docker capacity no longer fits this run's measurement")

    monkeypatch.setattr(cli, "check_evaluation", refused)
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )
    assert result.exit_code == 2
    assert "Error: Docker capacity no longer fits" in result.stderr


def test_check_fails_when_a_run_could_not_load_the_saved_profiles(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # check passed a coverage profile that the background run then failed to load.
    def unloadable(path: Path) -> object:
        raise OSError(36, "File name too long", str(path))

    monkeypatch.setattr(evaluation, "load_profiles", unloadable)
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )
    assert result.exit_code == 2
    assert "File name too long" in result.stderr
    assert not checkable["calls"]


def test_run_measures_a_task_that_has_no_collector_beside_a_covered_task(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # check passed this config, then measurement refused it: the task without coverage had
    # no collectors, so the saved profiles left it out.
    raw = yaml.safe_load(checkable["path"].read_text())
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=raw["timeout_seconds"], interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    root = checkable["output"]

    result = CliRunner().invoke(
        cli.app,
        ["run", str(checkable["path"]), "--output", str(root), "--yes", "--foreground"],
    )

    assert result.exit_code == 0, result.stderr + result.stdout
    data = json.loads((root / "report/results.json").read_text())
    coverage = [row for row in data["observations"] if row["metric"] == "Line coverage %"]
    assert {row["task"] for row in coverage if row["status"] == "measured"} == {
        "covered-native-task"
    }
    matrix = {(row["task"], row["metric"]): row["cells"] for row in data["completeness_matrix"]}
    assert {
        cell["state"] for cell in matrix["arbitrary-native-task", "Line coverage %"].values()
    } == {"not_configured"}
    assert {
        row["task"]
        for row in data["observations"]
        if row["metric"] == "Source lines added" and row["status"] == "measured"
    } == {"covered-native-task", "arbitrary-native-task"}


def _coverage_broken_on_one_task(
    checkable: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    require_ready: bool = False,
    after_coding: str = "valid",
) -> None:
    """Both tasks configure coverage; only arbitrary-native-task's probe fails before coding.

    After coding, that task's collector returns a valid report, fails as unavailable, or
    returns evidence that fails an integrity check.
    """
    raw = yaml.safe_load(checkable["path"].read_text())
    settings = raw["measurement_settings"]
    settings["coverage"] = settings["coverage_profiles"]["https://example.invalid/covered"]
    settings["require_ready"] = require_ready
    checkable["path"].write_text(yaml.safe_dump(raw))

    def collect(**kwargs: Any) -> dict[str, object]:
        if "arbitrary-native-task" in str(kwargs["task_identity"]):
            if "measurement-readiness" in str(kwargs["destination"]):
                return {"exit_code": 3, "timed_out": False, "report_collected": False}
            if after_coding == "unavailable":
                return {"exit_code": 3, "timed_out": False, "report_collected": False}
            if after_coding == "integrity":
                kwargs["destination"].mkdir(parents=True, exist_ok=True)
                (kwargs["destination"] / "report.raw").write_text("SF:../escape.py\nend_of_record\n")
                return {"exit_code": 0, "timed_out": False, "report_collected": True}
        return _lcov(**kwargs)

    monkeypatch.setattr(harbor_backend, "run_native_command", collect)


@pytest.mark.parametrize("require_ready", [False, True])
def test_check_records_a_measure_broken_on_one_task_as_not_measured(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch, require_ready: bool
) -> None:
    _coverage_broken_on_one_task(checkable, monkeypatch, require_ready=require_ready)

    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )

    report = json.loads((checkable["output"] / "check.json").read_text())
    row = report["matrix"]["arbitrary-native-task"]["coverage"]
    assert row["state"] == "broken" and row["reason"]
    assert report["matrix"]["covered-native-task"]["coverage"]["state"] == "ready"
    if require_ready:
        assert result.exit_code == 2
        assert f"arbitrary-native-task: coverage is broken: {row['reason']}" in result.stderr
        # The check blocks, so it saves no list of skipped pairs and warns of none.
        assert report["not_measured"] == []
        assert "is not measured for this task" not in result.stdout + result.stderr
    else:
        assert result.exit_code == 0, result.stderr
        warning = (
            "arbitrary-native-task: coverage is broken, so it is not measured for this task: "
            + row["reason"]
        )
        assert report["status"] == "ready" and warning in report["warnings"]
        assert "Warning: " + warning in result.stdout
    assert not checkable["calls"]


@pytest.mark.parametrize("require_ready", [False, True])
def test_check_refuses_a_measure_that_no_task_can_measure(
    checkable: dict[str, Any], require_ready: bool
) -> None:
    raw = yaml.safe_load(checkable["path"].read_text())
    raw["measurement_settings"]["coverage_profiles"] = {}
    raw["measurement_settings"]["require_ready"] = require_ready
    checkable["path"].write_text(yaml.safe_dump(raw))

    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )

    assert result.exit_code == 2
    assert "coverage is ready on no task, so the run would measure nothing for it" in result.stderr
    assert not checkable["calls"]


def _run_checkable(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any, list[str], list[str]]:
    """Run the checkable config with stubbed coding and reviews: run root, result, calls."""
    raw = yaml.safe_load(checkable["path"].read_text())
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=raw["timeout_seconds"], interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    root = checkable["output"]

    result = CliRunner().invoke(
        cli.app,
        ["run", str(checkable["path"]), "--output", str(root), "--yes", "--foreground"],
    )
    return root, result, calls, reviews


def test_run_reports_a_measure_broken_on_one_task_as_not_measured(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _coverage_broken_on_one_task(checkable, monkeypatch)
    root, result, _, _ = _run_checkable(checkable, monkeypatch)

    assert result.exit_code == 0, result.stderr + result.stdout
    reason = json.loads((root / "check.json").read_text())["matrix"]["arbitrary-native-task"][
        "coverage"
    ]["reason"]
    data = json.loads((root / "report/results.json").read_text())
    coverage = [row for row in data["observations"] if row["metric"] == "Line coverage %"]
    # The collector still ran after coding on the broken task; its values are not reported.
    assert {
        (row["task"], row["status"], row["value"] is None) for row in coverage
    } == {
        ("covered-native-task", "measured", False),
        ("arbitrary-native-task", "unavailable", True),
    }
    assert {row["reason"] for row in coverage if row["value"] is None} == {reason}
    # The broken pair takes no other measure of that task with it.
    assert {
        row["status"]
        for row in data["observations"]
        if (row["task"], row["metric"]) == ("arbitrary-native-task", "Source lines added")
    } == {"measured"}
    assert not [
        row
        for row in data["medians"] + data["raw_medians"]
        if (row["task"], row["metric"]) == ("arbitrary-native-task", "Line coverage %")
    ]
    # The marked pair is incomplete, though its collector counted a full population after coding.
    complete = {
        (row["task"], row["counts"]["Coverage covered/total counts"])
        for row in data["measurement_completeness"]
    }
    assert complete == {("arbitrary-native-task", False), ("covered-native-task", True)}
    assert not any(
        row["complete"]
        for row in data["measurement_completeness"]
        if row["task"] == "arbitrary-native-task"
    )
    matrix = {(row["task"], row["metric"]): row["cells"] for row in data["completeness_matrix"]}
    cells = matrix["arbitrary-native-task", "Line coverage %"].values()
    assert {cell["state"] for cell in cells} == {"unknown"}
    assert all(cell["measured"] == 0 and cell["reasons"] == [reason] for cell in cells)
    assert {
        cell["state"] for cell in matrix["covered-native-task", "Line coverage %"].values()
    } == {"measured"}

    status = CliRunner().invoke(cli.app, ["status", str(root)])
    assert status.exit_code == 0, status.stderr
    assert f"Warning: arbitrary-native-task: coverage not measured: {reason}" in status.stdout
    shown = json.loads(CliRunner().invoke(cli.app, ["status", str(root), "--json"]).stdout)
    assert shown["measures"]["not_measured"] == [
        {"task": "arbitrary-native-task", "measure": "coverage", "reason": reason}
    ]

    # The saved list is the only source: without it, the collected values come back.
    selection = evaluation._load_selection(root)
    assert selection["not_measured"] == shown["measures"]["not_measured"]
    del selection["not_measured"]
    evaluation._save_selection(root, selection)
    evaluation.regenerate_report(root)
    unmarked = json.loads((root / "report/results.json").read_text())
    assert {
        row["status"]
        for row in unmarked["observations"]
        if (row["task"], row["metric"]) == ("arbitrary-native-task", "Line coverage %")
    } == {"measured"}
    assert "Warning:" not in CliRunner().invoke(cli.app, ["status", str(root)]).stdout


def test_a_gate_on_a_listed_pair_reads_the_report_as_undetermined(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _coverage_broken_on_one_task(checkable, monkeypatch)
    root, result, _, _ = _run_checkable(checkable, monkeypatch)
    assert result.exit_code == 0, result.stderr
    evidence = load_evidence([str(root)])
    candidate = sorted({row.arm for row in evidence.attempts} - {"vanilla"})[0]
    gate = Gate(
        name="coverage",
        metrics=["Line coverage %"],
        statistic="mean_of_task_medians",
        against="vanilla",
        direction="higher",
        tolerance=10,
    )
    gates = Gates(count_overlap_passes=True, gates=[gate])

    split = check_gate(
        evidence, gates, gate, candidate, ["arbitrary-native-task", "covered-native-task"]
    )
    covered = check_gate(evidence, gates, gate, candidate, ["covered-native-task"])

    assert split.result == "undetermined" and split.detail.startswith("missing Line coverage %")
    # A split without the listed task decides.
    assert covered.result in {"pass", "fail"} and covered.detail == "1 of 1 tasks"


@pytest.mark.parametrize("failure", ["unavailable", "integrity"])
def test_a_failing_collector_on_a_not_measured_pair_after_coding(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # The pair is not measured, but one collector can serve several measures, so it still runs.
    _coverage_broken_on_one_task(checkable, monkeypatch, after_coding=failure)
    root, result, calls, reviews = _run_checkable(checkable, monkeypatch)

    revision = json.loads(
        (root / "evidence/observer-revisions/measurements/revision.json").read_text()
    )
    if failure == "unavailable":
        # An unavailable collector costs only its own pair: the run finishes and reports.
        assert result.exit_code == 0, result.stderr
        assert revision["status"] == "complete"
        data = json.loads((root / "report/results.json").read_text())
        assert data["measurement_collection"]["status"] == "partial"
        assert {row["task"] for row in data["measurement_collection"]["failures"]} == {
            "arbitrary-native-task"
        }
        assert {
            (row["task"], row["metric"], row["status"])
            for row in data["observations"]
            if row["metric"] in {"Line coverage %", "Source lines added"}
        } == {
            ("arbitrary-native-task", "Line coverage %", "unavailable"),
            ("arbitrary-native-task", "Source lines added", "measured"),
            ("covered-native-task", "Line coverage %", "measured"),
            ("covered-native-task", "Source lines added", "measured"),
        }
        assert reviews
    else:
        # Evidence that fails an integrity check stays fatal, even for a pair not measured: the
        # measurement revision fails, so no task gets measurements, a report, or reviews.
        assert result.exit_code == 2
        assert "Coverage source is absent from the frozen snapshot" in result.stderr
        assert revision["status"] == "failed"
        assert not (root / "report/results.json").exists()
        assert calls and not reviews


def _failing_reference_agents(oracle: dict[str, object], nop: dict[str, object]) -> Any:
    return lambda identity, destination: {"oracle": oracle, "nop": nop}


@pytest.mark.parametrize("command", ["check", "run"])
@pytest.mark.parametrize(
    "failure,expected",
    [
        ("no code-mode host", "codex-code-mode-host"),
        ("git commit fails", "coding_environment is broken: git commit failed"),
        (
            "oracle empty patch",
            "oracle is broken: reference solution produced an empty graded patch",
        ),
        ("oracle reward 0", "oracle is broken: verifier reward is False; expected 1"),
        ("nop reward 1", "nop is broken: verifier reward is True; expected 0"),
        ("broken collector", "coverage is broken"),
    ],
)
def test_each_predictable_failure_stops_before_any_coding_call(
    checkable: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    failure: str,
    expected: str,
) -> None:
    if failure == "no code-mode host":
        launcher = tmp_path / "launcher"
        launcher.mkdir()
        (launcher / "codex").write_text("#!/bin/sh\n")
        (launcher / "codex").chmod(0o755)
        monkeypatch.setenv("PATH", str(launcher) + os.pathsep + os.environ["PATH"])
        monkeypatch.setattr(isolation, "codex_code_mode_host", REAL_CODE_MODE_HOST)
    elif failure == "git commit fails":
        monkeypatch.setattr(
            harbor_backend,
            "probe_coding_environment",
            lambda identity: {
                "status": "failed",
                "reason": "git commit failed in the coding image (exit 128): Author identity unknown",
            },
        )
    elif failure.startswith("oracle"):
        oracle = reference_agent(reward=0.0, patch_bytes=0 if "empty" in failure else 120)
        monkeypatch.setattr(
            harbor_backend,
            "run_reference_agents",
            _failing_reference_agents(oracle, reference_agent(reward=0.0, patch_bytes=0)),
        )
    elif failure == "nop reward 1":
        monkeypatch.setattr(
            harbor_backend,
            "run_reference_agents",
            _failing_reference_agents(
                reference_agent(reward=1.0, patch_bytes=120),
                reference_agent(reward=1.0, patch_bytes=0),
            ),
        )
    else:
        monkeypatch.setattr(
            harbor_backend,
            "run_native_command",
            lambda **kwargs: {"exit_code": 3, "timed_out": False, "report_collected": False},
        )
    output = str(checkable["output"])
    arguments = (
        ["check", str(checkable["path"]), "--output", output]
        if command == "check"
        else ["run", str(checkable["path"]), "--output", output, "--yes"]
    )
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2, result.stdout
    assert expected in result.stderr and "Traceback" not in result.stderr
    assert not checkable["calls"]
    assert not list(checkable["output"].rglob("invocation.json"))
    assert not (checkable["output"] / "evidence").exists()


def test_check_warns_when_a_prebuilt_image_ignores_the_task_dockerfile(
    checkable: dict[str, Any],
) -> None:
    # A git-identity fix in this Dockerfile silently did nothing, and a whole pilot was lost.
    raw = yaml.safe_load(checkable["path"].read_text())
    task = Path(raw["tasks"]["source"]) / raw["tasks"]["ids"][0]
    (task / "environment/Dockerfile").write_text(
        "FROM fixture:pinned\nRUN git config --global user.name x\n"
    )
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )
    assert result.exit_code == 0, result.stderr
    assert "Warning: arbitrary-native-task: runs the prebuilt image fixture:pinned" in result.stdout
    report = json.loads((checkable["output"] / "check.json").read_text())
    assert report["warnings"] == [
        "arbitrary-native-task: runs the prebuilt image fixture:pinned; environment/Dockerfile "
        "is never built, so changes to it have no effect"
    ]
    assert report["coding_images"]["covered-native-task"]["ignored_dockerfile"] is None
    environment = report["matrix"]["arbitrary-native-task"]["coding_environment"]
    assert environment["image_source"] == "prebuilt"
    assert environment["ignored_dockerfile"] == "environment/Dockerfile"


def _build_with_fresh_image_ids(
    monkeypatch: pytest.MonkeyPatch, scratch: Path
) -> list[str]:
    """Keep the real preparation cache; fake only the build, which commits a new image ID."""
    base = {
        "image": "sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64,
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    monkeypatch.setattr(harbor_backend, "_native_image", lambda **kwargs: base)
    monkeypatch.setattr(
        harbor_backend, "_inspect_image", lambda reference, **kwargs: {"Id": reference}
    )
    built: list[str] = []

    def build(**kwargs: Any) -> dict[str, object]:
        image_id = "sha256:" + os.urandom(32).hex()  # docker commit never repeats an ID
        built.append(image_id)
        identity = _prepare_fixture_task(
            **{**kwargs, "destination": scratch / f"build-{len(built)}"}
        )
        destination = kwargs["destination"]
        shutil.copytree(Path(str(identity["task_dir"])), destination / "task")
        shutil.copytree(Path(str(identity["fixture_dir"])), destination / "fixture")
        image = {**base, "image": image_id, "image_id": image_id}
        toolchain = identity["toolchain_identity"]
        assert isinstance(toolchain, dict)
        identity.update(
            task_dir=str(destination / "task"),
            fixture_dir=str(destination / "fixture"),
            image_identity=image,
            toolchain_identity={**toolchain, "image_id": image_id, "base_image_identity": base},
            grader_image_identity=base,
        )
        qualify_synthetic_source(identity)
        (destination / "artifacts").mkdir()
        (destination / "artifacts/native-preparation.json").write_text(json.dumps(identity))
        return identity

    monkeypatch.setattr(harbor_backend, "_prepare_native_task", build)
    return built


def test_new_output_folder_keeps_comparison_identity_and_cache_decisions(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_cache: Path,
) -> None:
    # Caches beside the output folder missed every vanilla attempt once the folder moved.
    built = _build_with_fresh_image_ids(monkeypatch, tmp_path / "scratch")
    qualified: list[Path] = []

    def reference_agents(identity: dict[str, object], destination: Path) -> dict[str, object]:
        qualified.append(destination)
        return passing_reference_agents(identity, destination)

    monkeypatch.setattr(harbor_backend, "run_reference_agents", reference_agents)
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    first = evaluation._check(config, tmp_path / "a/run", None)
    second = evaluation._check(config, tmp_path / "b/run", None)
    assert len(built) == 1 and len(qualified) == 1
    assert second.caches.vanilla == isolated_cache / "pluginbench/vanilla"
    for name in ("comparison", "task_environment", "capabilities", "vanilla_identity"):
        assert getattr(first, name) == getattr(second, name), name
    assert first.report["vanilla_cache"] == second.report["vanilla_cache"]
    rows = second.report["vanilla_cache"]
    assert isinstance(rows, list) and len(rows) == config.repeats
    assert {row["status"] for row in rows} == {"miss"}
    # Once a cohort is published under that key, a third folder sees every attempt as a hit.
    for row in rows:
        (second.caches.vanilla / row["cache_key"] / str(row["repeat"])).mkdir(parents=True)
    third = evaluation.check_evaluation(config, tmp_path / "c/run")
    third_rows = third["vanilla_cache"]
    assert isinstance(third_rows, list)
    assert [row["status"] for row in third_rows] == ["hit"] * config.repeats
    assert len(built) == 1 and len(qualified) == 1


@pytest.mark.parametrize("layout", ["linked-root", "linked-vanilla", "output-in-cache"])
def test_cache_location_is_resolved_and_checked_before_preparation(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_cache: Path,
    layout: str,
) -> None:
    real = tmp_path / "real-cache"
    (real / "pluginbench").mkdir(parents=True)
    output = tmp_path / "results"
    if layout == "linked-root":
        (tmp_path / "linked-cache").symlink_to(real)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "linked-cache"))
        caches = evaluation._caches(output)
        assert caches.vanilla == real / "pluginbench/vanilla"
        assert not any(path.is_symlink() for path in (caches.vanilla, *caches.vanilla.parents))
        return
    monkeypatch.setenv("XDG_CACHE_HOME", str(real))
    if layout == "linked-vanilla":
        (tmp_path / "elsewhere").mkdir()
        (real / "pluginbench/vanilla").symlink_to(tmp_path / "elsewhere")
        expected = "must not be symlinks"
    else:
        output = real / "pluginbench/runs/results"
        expected = "must not overlap"
    monkeypatch.setattr(
        harbor_backend,
        "prepare_native_task",
        lambda **kwargs: pytest.fail("the cache is checked before any preparation"),
    )
    with pytest.raises(ValueError, match=expected):
        evaluation.check_evaluation(config, output)
    assert not output.exists()


@pytest.mark.parametrize("spelling", ["parent", "symlink"])
def test_output_reached_through_a_parent_or_a_symlink_is_prepared(
    spelling: str,
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_cache: Path,
) -> None:
    # Prepared task paths are resolved, so an unresolved output root was never their parent.
    _build_with_fresh_image_ids(monkeypatch, tmp_path / "scratch")
    monkeypatch.setattr(harbor_backend, "run_reference_agents", passing_reference_agents)
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    output = {
        "parent": tmp_path / "real/sub/../run",
        "symlink": tmp_path / "link/run",
    }[spelling]

    checked = evaluation._check(config, output, None)

    assert checked.root == tmp_path / "real/run"
    assert (checked.root / "inputs").is_dir()


def test_jobs_above_docker_capacity_fail_instead_of_being_lowered(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 2)
    with pytest.raises(ValueError, match="jobs=3 does not fit.*set jobs: 2 or fewer"):
        evaluation.plan_evaluation(config.model_copy(update={"jobs": 3}))
    plan = evaluation.plan_evaluation(config.model_copy(update={"jobs": 2}))
    comparison_identity = plan["comparison_identity"]
    assert isinstance(comparison_identity, dict)
    assert plan["jobs"] == comparison_identity["jobs"] == 2


def test_reference_agents_reuse_only_a_pass_for_the_same_preparation_and_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken: dict[str, object] = {
        "oracle": reference_agent(reward=0.0, patch_bytes=0),
        "nop": reference_agent(reward=0.0, patch_bytes=0),
    }
    passing = passing_reference_agents({}, tmp_path)
    results = iter([broken, passing, passing])
    runs: list[Path] = []

    def run(identity: dict[str, object], destination: Path) -> dict[str, object]:
        runs.append(destination)
        return next(results)

    monkeypatch.setattr(harbor_backend, "run_reference_agents", run)
    identity: dict[str, object] = {
        "preparation_key": "6" * 64,
        "image_identity": {"image_id": "sha256:" + "1" * 64},
        "grader_image_identity": {"image_id": "sha256:" + "2" * 64},
    }
    cache = tmp_path / "reference-agents"
    states = [
        evaluation._reference_agents(identity, tmp_path / str(index), cache)["oracle"]["state"]
        for index in range(3)
    ]
    assert states == ["broken", "ready", "ready"] and len(runs) == 2
    rebuilt = {**identity, "image_identity": {"image_id": "sha256:" + "3" * 64}}
    rows = evaluation._reference_agents(rebuilt, tmp_path / "3", cache)
    assert rows["oracle"]["state"] == "ready" and len(runs) == 3
    # A pass without a supplied git identity does not qualify a task that is given one.
    supplied = {**rebuilt, "supplied_git_identity": dict(harbor_backend.SUPPLIED_GIT_IDENTITY)}
    results = iter([passing])
    for index in (4, 5):
        rows = evaluation._reference_agents(supplied, tmp_path / str(index), cache)
        assert rows["oracle"]["state"] == "ready"
    assert len(runs) == 4


def test_check_records_a_supplied_git_identity_only_for_the_task_that_needed_it(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def keys(output: Path) -> dict[str, set[str]]:
        rows = json.loads((output / "check.json").read_text())["vanilla_cache"]
        return {
            task: {row["cache_key"] for row in rows if row["task_id"] == task}
            for task in ("arbitrary-native-task", "covered-native-task")
        }

    def prepared(output: Path) -> dict[str, dict[str, object]]:
        catalog = json.loads((output / "inputs/tasks.json").read_text())
        return {
            task["id"]: json.loads(task["provenance"]["native_task_identity"])
            for task in catalog["tasks"]
        }

    own = checkable["output"]
    result = CliRunner().invoke(cli.app, ["check", str(checkable["path"]), "--output", str(own)])
    assert result.exit_code == 0, result.stderr
    assert not any("supplied_git_identity" in value for value in prepared(own).values())
    supplied = dict(harbor_backend.SUPPLIED_GIT_IDENTITY)

    def probe(identity: dict[str, object]) -> dict[str, object]:
        if "arbitrary-native-task" not in str(identity["task_dir"]):
            return passing_git_probe(identity)
        return {**passing_git_probe(identity), "supplied_git_identity": supplied}

    referenced: dict[str, object] = {}

    def reference_agents(identity: dict[str, object], destination: Path) -> dict[str, object]:
        referenced[destination.name] = identity.get("supplied_git_identity")
        return passing_reference_agents(identity, destination)

    monkeypatch.setattr(harbor_backend, "probe_coding_environment", probe)
    monkeypatch.setattr(harbor_backend, "run_reference_agents", reference_agents)
    output = Path(str(own) + "-supplied")
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(output)]
    )
    assert result.exit_code == 0, result.stderr
    note = (
        "the task sets no git identity, so PluginBench supplied "
        "PluginBench Agent <agent@pluginbench.invalid> to its agents"
    )
    assert f"arbitrary-native-task: coding_environment ready — {note}" in result.stdout
    assert "covered-native-task: coding_environment ready" not in result.stdout
    matrix = json.loads((output / "check.json").read_text())["matrix"]
    assert matrix["arbitrary-native-task"]["coding_environment"]["note"] == note
    assert "note" not in matrix["covered-native-task"]["coding_environment"]
    # Saved where a run and its resume read the prepared identity, and in the receipt.
    identities = prepared(output)
    assert identities["arbitrary-native-task"]["supplied_git_identity"] == supplied
    receipt = output / "inputs/arbitrary-native-task/artifacts/native-preparation.json"
    assert json.loads(receipt.read_text())["supplied_git_identity"] == supplied
    assert referenced["arbitrary-native-task"] == supplied
    assert referenced.get("covered-native-task") is None
    # The task with its own identity keeps today's cache key; the other gets a new one.
    assert "supplied_git_identity" not in identities["covered-native-task"]
    assert keys(output)["covered-native-task"] == keys(own)["covered-native-task"]
    assert keys(output)["arbitrary-native-task"].isdisjoint(keys(own)["arbitrary-native-task"])


def test_check_prints_a_hit_or_miss_for_every_vanilla_attempt(
    checkable: dict[str, Any], isolated_cache: Path
) -> None:
    result = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"])]
    )
    assert result.exit_code == 0, result.stderr
    report = json.loads((checkable["output"] / "check.json").read_text())
    rows = report["vanilla_cache"]
    assert len(rows) == 2 * 3 and {row["status"] for row in rows} == {"miss"}
    assert "Vanilla cache: 0 of 6 attempts hit" in result.stdout
    covered = next(row for row in rows if row["task_id"] == "covered-native-task")
    # Each repeat is its own cache entry, so a stopped run's finished repeats are hits.
    for repeat in (1, 2):
        (isolated_cache / "pluginbench/vanilla" / covered["cache_key"] / str(repeat)).mkdir(
            parents=True
        )
    again = CliRunner().invoke(
        cli.app, ["check", str(checkable["path"]), "--output", str(checkable["output"]) + "-2"]
    )
    assert again.exit_code == 0, again.stderr
    assert "Vanilla cache: 2 of 6 attempts hit" in again.stdout
    for repeat in (1, 2, 3):
        status = "miss" if repeat == 3 else f"hit ({covered['cache_key'][:12]})"
        assert f"covered-native-task repeat {repeat}: {status}" in again.stdout
        assert f"arbitrary-native-task repeat {repeat}: miss" in again.stdout


def test_parser_only_change_keeps_the_vanilla_cache_identity(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every edit to codex.py once missed every cached vanilla attempt, parser fixes included.
    package = tmp_path / "pluginbench"
    shutil.copytree(Path(evaluation.__file__).parent, package)
    monkeypatch.setattr(components, "__file__", str(package / "components.py"))
    comparison = evaluation.plan_evaluation(config)["comparison_identity"]
    assert isinstance(comparison, dict)

    def identity() -> dict[str, object]:
        return evaluation._vanilla_identity(config, comparison, "0.167.0", {})

    original = identity()
    parser = package / Path(str(inspect.getsourcefile(codex.parse_events))).name
    parser.write_text(parser.read_text() + "\n# a parser-only change\n")
    assert identity() == original
    launcher = package / "codex.py"
    launcher.write_text(launcher.read_text() + "\n# a launch change\n")
    assert identity() != original


def _coding_and_review_backend(
    calls: list[str], reviews: list[str], *, timeout: int, interrupt_at: int | None
) -> Callable[..., dict[str, Any]]:
    usage = {"input_tokens": 10, "output_tokens": 5}

    def invoke(**kwargs: Any) -> dict[str, Any]:
        area = kwargs["area"]
        if kwargs.get("output_schema") is not None:
            packet = json.loads((area / "workspace/packet.json").read_text())
            reviews.append(packet["packet_id"])
            return {
                "successful_native_completion": True,
                "actual_primary_models": ["gpt-6-astra"],
                "runtime_effort_evidence": ["high"],
                "command_items": [],
                "usage": {"input_tokens": 50, "output_tokens": 10},
                "startup": [{"type": "session_meta", "payload": {"cli_version": "judge-version"}}],
                "final": json.dumps(
                    {
                        "packet_id": packet["packet_id"],
                        "status": "reviewed",
                        "issues": [],
                        "limitations": ["Fixture only"],
                        "outside_packet_access": False,
                    }
                ),
            }
        calls.append(kwargs["entry"]["id"])
        if len(calls) == interrupt_at:
            # As `stop` does: SIGTERM becomes KeyboardInterrupt after some usage was saved.
            (area / "artifacts").mkdir()
            (area / "artifacts/execution.json").write_text(json.dumps({"usage": usage}))
            raise KeyboardInterrupt("stopped by SIGTERM")
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        result = native_execution(
            area,
            kwargs["protocol"]["native_task_identity"],
            reward=1,
            timeout=timeout,
            version="0.167.0",
        )
        result["startup"] = [{"type": "session_meta", "payload": {"cli_version": "0.167.0"}}]
        return result

    return invoke


def _measured_and_reviewed(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> evaluation.Evaluation:
    """One task, coverage and a Codex reviewer, one job, with offline preparation."""
    from pluginbench.measurement_profiles import CoverageSettings

    standards = tmp_path / "review-standards.md"
    standards.write_text("Report actionable correctness findings only.\n")
    config.jobs = 1
    config.measurements += ["coverage", "review"]
    config.measurement_settings.coverage = CoverageSettings(
        command=["python3", "-m", "coverage"],
        report_path="coverage/lcov.info",
        tool_version="fixture-1",
    )
    config.reviewer = evaluation.Reviewer(
        harness="codex", model="gpt-6-astra", reasoning="high", guidance=[standards]
    )
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(harbor_backend, "prepare_native_task", _prepare_fixture_task)
    monkeypatch.setattr(harbor_backend, "run_native_command", _lcov)
    return config


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGINT], ids=["stop", "ctrl-c"])
def test_single_job_stop_keeps_the_active_attempt_and_resume_never_recodes_it(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: signal.Signals,
) -> None:
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    cancelled = Event()

    def invoke(**kwargs: Any) -> dict[str, Any]:
        # Deliver a real signal during coding, then keep the call active until admission stops.
        os.kill(os.getpid(), stop)
        assert cancelled.wait(timeout=5)
        return backend(**kwargs)

    monkeypatch.setattr(codex, "invoke", backend)
    handlers = {item: signal.getsignal(item) for item in (signal.SIGTERM, signal.SIGINT)}
    root = tmp_path / "run"
    with monkeypatch.context() as stopping:
        stopping.setattr(parallel, "Event", lambda: cancelled)
        stopping.setattr(harbor_backend, "invoke_native", invoke)
        with pytest.raises(KeyboardInterrupt):
            evaluation.run_evaluation(config, root)

    assert {item: signal.getsignal(item) for item in handlers} == handlers
    stopped = json.loads(CliRunner().invoke(cli.app, ["status", str(root), "--json"]).stdout)
    assert stopped["phase"] == "stopped" and stopped["states_source"] == "ledger"
    assert stopped["states"] == {"complete": 1, "pending": 8}
    assert len(calls) == 1 and reviews == []
    attempt_id = calls[0]
    receipt = root / "evidence/records" / f"{attempt_id}.json"
    recorded = receipt.read_bytes()

    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    resumed = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    assert resumed.exit_code == 0, resumed.stderr + resumed.stdout
    done = json.loads(CliRunner().invoke(cli.app, ["status", str(root), "--json"]).stdout)
    assert done["phase"] == "complete" and done["states"] == {"complete": 9}
    assert done["discarded"] == []
    assert len(calls) == len(set(calls)) == 9 and calls.count(attempt_id) == 1
    assert receipt.read_bytes() == recorded


def test_status_follows_a_stopped_run_through_its_resume(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=3
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        evaluation.run_evaluation(config, root)

    stopped = json.loads(CliRunner().invoke(cli.app, ["status", str(root), "--json"]).stdout)
    assert stopped["phase"] == "stopped" and stopped["states_source"] == "ledger"
    assert stopped["states"] == {"complete": 2, "interrupted": 1, "pending": 6}
    assert stopped["tokens"]["lower_bound"] is True

    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    assert CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"]).exit_code == 0

    done = json.loads(CliRunner().invoke(cli.app, ["status", str(root), "--json"]).stdout)
    assert done["phase"] == "complete" and done["states"] == {"complete": 9}
    assert done["outcomes"] == {"pass": 9}
    assert len(done["discarded"]) == 1
    assert done["discarded"][0]["replaced_by"] in {row["id"] for row in done["attempts"]}
    assert done["discarded"][0]["tokens"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert set(done["arms"]) == {"vanilla", "superpowers", "workflow"}
    assert [(row["revision"], row["selected"]) for row in done["measures"]["review_revisions"]] == [
        ("review", True)
    ]
    assert done["report"] == str(root / "report/report.html")
    assert all(row["measures"] for row in done["attempts"])


def test_review_adds_a_selected_revision_with_the_frozen_reviewer(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    root = tmp_path / "run"
    evaluation.run_evaluation(config, root)
    first = (root / "evidence/review-revisions/review/review-state.json").read_bytes()
    report = (root / "report/results.json").stat().st_mtime_ns
    coded, reviewed = len(calls), len(reviews)

    def missing_host() -> Path:
        raise ValueError("Codex reviews need an executable codex-code-mode-host")

    with monkeypatch.context() as patched:
        patched.setattr(isolation, "codex_code_mode_host", missing_host)
        blocked = CliRunner().invoke(cli.app, ["review", str(root), "--revision", "r2", "--yes"])
    assert blocked.exit_code == 2 and "codex-code-mode-host" in blocked.stderr
    assert len(reviews) == reviewed

    result = CliRunner().invoke(cli.app, ["review", str(root), "--revision", "r2", "--yes"])

    assert result.exit_code == 0, result.stderr + result.stdout
    assert "9 review calls as review revision r2." in result.stdout
    assert len(reviews) == reviewed + 9 and len(calls) == coded
    assert evaluation._load_selection(root)["review_revision"] == "r2"
    assert (root / "evidence/review-revisions/review/review-state.json").read_bytes() == first
    assert (root / "report/results.json").stat().st_mtime_ns != report
    status = json.loads(CliRunner().invoke(cli.app, ["status", str(root), "--json"]).stdout)
    assert [(row["revision"], row["selected"]) for row in status["measures"]["review_revisions"]] == [
        ("r2", True),
        ("review", False),
    ]
    assert status["last_event"]["event"] == "finished"

    again = CliRunner().invoke(cli.app, ["review", str(root), "--revision", "r2", "--yes"])
    assert again.exit_code == 2 and "choose a new --revision name" in again.stderr
    assert len(reviews) == reviewed + 9
    saved = evaluation._load_selection(root)
    measurements = saved["measurements"]
    assert isinstance(measurements, list)
    for change, message in (
        (
            {"reviewer": None, "measurements": [m for m in measurements if m != "review"]},
            "has no reviewer",
        ),
        ({"review_revision": None}, "has not finished its first review"),
    ):
        evaluation._save_selection(root, {**saved, **change})
        refused = CliRunner().invoke(cli.app, ["review", str(root), "--revision", "r3", "--yes"])
        assert refused.exit_code == 2 and message in refused.stderr
    assert len(reviews) == reviewed + 9


def test_run_on_a_stopped_run_codes_only_unfinished_slots_then_measures_and_reviews(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=3
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        evaluation.run_evaluation(config, root)
    stopped = json.loads((root / "evidence/run.json").read_text())
    states = stopped["attempt_states"]
    finished = {key for key, state in states.items() if state == "complete"}
    assert stopped["status"] == "interrupted" and len(states) == 9
    assert len(finished) == 2 and states[calls[2]] == "interrupted"
    assert evaluation.plan_resume(root)["coding_calls"] == 7
    interrupted = calls[2]
    calls.clear()
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)

    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])

    assert result.exit_code == 0, result.stderr + result.stdout
    assert "Coding attempts: 7   Review calls: 9" in result.stdout
    run = json.loads((root / "evidence/run.json").read_text())
    assert run["status"] == "complete"
    assert set(run["attempt_states"].values()) == {"complete"} and len(run["attempt_states"]) == 9
    assert len(calls) == 7 and not set(calls) & (finished | {interrupted})
    discarded = run["discarded_attempts"][interrupted]
    assert discarded["replaced_by"] in calls
    assert discarded["stages"]["handoff"]["work"]["tokens"] == {
        "input_tokens": 10,
        "output_tokens": 5,
    }
    # Measurement and review run on the replaced attempt identities.
    selection = evaluation._load_selection(root)
    assert selection["measurement_revision"] == "measurements"
    assert selection["review_revision"] == "review"
    assert len(reviews) == 9
    assert (root / "report/report.html").is_file()
    events = [json.loads(line)["event"] for line in (root / "events.jsonl").read_text().splitlines()]
    assert events[0] == "started" and events[-1] == "finished"


def test_run_interrupted_after_its_last_attempt_finishes_measurement_and_reviews(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    checkpoint = study._checkpoint

    def stopped_at_the_end(*args: Any, status: str, **kwargs: Any) -> None:
        if status == "complete":
            raise KeyboardInterrupt("stopped by SIGTERM")
        checkpoint(*args, status=status, **kwargs)

    root = tmp_path / "run"
    with monkeypatch.context() as patched:
        patched.setattr(study, "_checkpoint", stopped_at_the_end)
        with pytest.raises(KeyboardInterrupt):
            evaluation.run_evaluation(config, root)
    run = json.loads((root / "evidence/run.json").read_text())
    assert run["status"] == "interrupted" and set(run["attempt_states"].values()) == {"complete"}
    assert evaluation.plan_resume(root)["coding_calls"] == 0
    coded = len(calls)

    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])

    assert result.exit_code == 0, result.stderr + result.stdout
    assert len(calls) == coded and len(reviews) == 9
    assert json.loads((root / "evidence/run.json").read_text())["status"] == "complete"
    assert evaluation._load_selection(root)["review_revision"] == "review"


class _Killed(BaseException):
    """Stands in for SIGKILL: once raised, no handler writes anything."""


def _recorded_calls(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[evaluation.Evaluation, list[str], list[str]]:
    """The measured and reviewed config, with fake coding and review calls recorded."""
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    return config, calls, reviews


def _killed_while_measuring(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write: int,
    torn: bool,
) -> tuple[Path, list[str], list[str]]:
    """Code and start measuring, then die at the given revision.json write, as SIGKILL would."""
    config, calls, reviews = _recorded_calls(config, tmp_path, monkeypatch)
    write_text = Path.write_text
    writes = 0
    dead = False

    def killed_at_that_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        # The first revision.json write saves the frozen profiles; the second records progress.
        nonlocal writes, dead
        if dead:
            raise _Killed
        if path.name.startswith("revision.json"):
            writes += 1
            if writes == write:
                dead = True
                if torn:
                    write_text(path, data[: len(data) // 2], *args, **kwargs)
                raise _Killed
        return write_text(path, data, *args, **kwargs)

    root = tmp_path / "run"
    with monkeypatch.context() as patched:
        patched.setattr(Path, "write_text", killed_at_that_write)
        with pytest.raises(_Killed):
            evaluation.run_evaluation(config, root)
    return root, calls, reviews


@contextmanager
def _held_by_another_process(root: Path, record: dict[str, object]) -> Iterator[None]:
    """Hold run.lock as another process that has not replaced the owner record yet."""
    fd = os.open(root / run_lock.LOCK_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        saved = os.pread(fd, 1 << 16, 0)
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode(), 0)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            os.pwrite(fd, saved, 0)
    finally:
        os.close(fd)


def _assert_only_a_free_run_is_recovered(root: Path) -> None:
    # No record yet, or one that still names this process, which held the lock before.
    records: tuple[dict[str, object], ...] = ({}, {"pid": os.getpid()})
    for record in records:
        with _held_by_another_process(root, record):
            with pytest.raises(ValueError, match="another process may own this run"):
                evaluation.plan_resume(root)
            with pytest.raises(ValueError, match="run is already active"):
                evaluation.resume_evaluation(root)
    assert evaluation.plan_resume(root)["collect_measurements"] is True


@pytest.mark.parametrize("torn", [False, True], ids=["before-a-write", "during-a-write"])
def test_run_resumes_a_measurement_that_a_kill_left_collecting(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torn: bool
) -> None:
    root, calls, reviews = _killed_while_measuring(
        config, tmp_path, monkeypatch, write=2, torn=torn
    )
    killed = root / "evidence/observer-revisions/measurements/revision.json"
    assert json.loads(killed.read_text())["status"] == "collecting"
    _assert_only_a_free_run_is_recovered(root)
    assert json.loads(killed.read_text())["status"] == "collecting"
    coded = len(calls)

    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])

    assert result.exit_code == 0, result.stderr + result.stdout
    assert (
        "Measurement revision measurements was left collecting by a process that ended, so it "
        "cannot continue. Its files stay; measurement starts again as measurements-2"
    ) in " ".join((result.stderr + result.stdout).split())
    assert len(calls) == coded and len(reviews) == 9
    state = json.loads(killed.read_text())
    assert state["status"] == "failed" and "ended while collecting" in state["error"]
    assert (killed.parent / "profiles").is_dir()
    selection = evaluation._load_selection(root)
    assert selection["measurement_revision"] == "measurements-2"
    assert selection["measurement_history"] == [study._reference(root / "evidence", killed)]
    assert (root / "report/report.html").is_file()
    status = CliRunner().invoke(cli.app, ["status", str(root), "--json"])
    assert status.exit_code == 0, status.stderr
    assert json.loads(status.stdout)["measures"]["measurement_revisions"] == [
        {"revision": "measurements", "status": "failed", "selected": False},
        {"revision": "measurements-2", "status": "complete", "selected": True},
    ]


@pytest.mark.parametrize("ended", ["killed", "stopped"])
def test_run_measures_again_when_measurement_ended_before_saving_its_profiles(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ended: str
) -> None:
    abandoned = tmp_path / "run/evidence/observer-revisions/measurements"
    if ended == "killed":
        # Killed before the first revision.json write: the directory has no state file.
        root, calls, reviews = _killed_while_measuring(
            config, tmp_path, monkeypatch, write=1, torn=False
        )
        assert abandoned.is_dir() and not (abandoned / "revision.json").exists()
    else:
        # Stopped while freezing collectors: its state says failed, with no frozen profiles.
        config, calls, reviews = _recorded_calls(config, tmp_path, monkeypatch)
        root = tmp_path / "run"

        def stopped(*args: Any, **kwargs: Any) -> None:
            raise KeyboardInterrupt("stopped by SIGTERM")

        with monkeypatch.context() as patched:
            patched.setattr(remeasure, "freeze_collectors", stopped)
            with pytest.raises(KeyboardInterrupt):
                evaluation.run_evaluation(config, root)
        state = json.loads((abandoned / "revision.json").read_text())
        assert state["status"] == "failed" and "frozen_profiles" not in state
    kept = study._inventory(abandoned)
    _assert_only_a_free_run_is_recovered(root)
    coded = len(calls)

    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])

    assert result.exit_code == 0, result.stderr + result.stdout
    assert (
        "Measurement revision measurements ended before it saved its frozen profiles, so it "
        "cannot continue. Its files stay; measurement starts again as measurements-2"
    ) in " ".join((result.stderr + result.stdout).split())
    assert len(calls) == coded and len(reviews) == 9
    assert study._inventory(abandoned) == kept
    selection = evaluation._load_selection(root)
    assert selection["measurement_revision"] == "measurements-2"
    assert selection["measurement_history"] == []
    assert (root / "report/report.html").is_file()
    # status lists revisions that saved a state; a directory killed before its first has none.
    revisions = [{"revision": "measurements-2", "status": "complete", "selected": True}]
    if ended == "stopped":
        revisions.insert(0, {"revision": "measurements", "status": "failed", "selected": False})
    status = CliRunner().invoke(cli.app, ["status", str(root), "--json"])
    assert status.exit_code == 0, status.stderr
    assert json.loads(status.stdout)["measures"]["measurement_revisions"] == revisions


def _stopped_after_two_attempts(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[str], list[str]]:
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    for interrupt_at in (3, None):
        backend = _coding_and_review_backend(
            calls, reviews, timeout=config.timeout_seconds, interrupt_at=interrupt_at
        )
        monkeypatch.setattr(codex, "invoke", backend)
        monkeypatch.setattr(harbor_backend, "invoke_native", backend)
        if interrupt_at:
            with pytest.raises(KeyboardInterrupt):
                evaluation.run_evaluation(config, tmp_path / "run")
    return tmp_path / "run", calls, reviews


def test_resume_refuses_before_coding_what_would_fail_after_it(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls, _ = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    coded = len(calls)
    profiles = root / "inputs/profiles.json"
    saved = profiles.read_bytes()
    profiles.unlink()  # As in runs prepared before profiles were saved, like the pilot.
    old = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    assert old.exit_code == 2 and "predates saved measurement profiles" in old.stderr
    profiles.write_bytes(saved)

    def missing_host() -> Path:
        raise ValueError("Codex reviews need an executable codex-code-mode-host")

    monkeypatch.setattr(isolation, "codex_code_mode_host", missing_host)
    blocked = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    assert blocked.exit_code == 2 and "codex-code-mode-host" in blocked.stderr
    assert len(calls) == coded


def test_resume_after_host_cli_update_keeps_the_pinned_container_version(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls, reviews = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    coded = len(calls)
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.1"})
    admitted: list[str] = []
    monkeypatch.setattr(codex, "admit_version", admitted.append)
    backend = harbor_backend.invoke_native

    def invoke(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["protocol"]["harness_version"] == "0.167.0"
        return backend(**kwargs)

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert len(calls) == coded + 7 and len(reviews) == 9
    assert admitted == ["0.167.0"]
    assert json.loads((root / "evidence/run.json").read_text())["status"] == "complete"
    assert json.loads((root / "inputs/study.json").read_text())["harness_version"] == "0.167.0"


def test_stopped_run_copied_to_a_new_path_resumes_there(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls, reviews = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    moved = tmp_path / "moved"
    shutil.copytree(root, moved, symlinks=True)
    shutil.rmtree(root)
    coded = len(calls)
    # The copied run.lock still names the old path; deleting it is the documented way on.
    refused = CliRunner().invoke(cli.app, ["run", str(moved), "--yes", "--foreground"])
    assert refused.exit_code == 2 and "delete" in refused.stderr
    (moved / "run.lock").unlink()
    result = CliRunner().invoke(cli.app, ["run", str(moved), "--yes", "--foreground"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert len(calls) == coded + 7 and len(reviews) == 9
    assert json.loads((moved / "evidence/run.json").read_text())["status"] == "complete"
    assert not root.exists()


# The launcher hashes a Codex run saved at 942f12c.
_SAVED_CODEX_LAUNCHER = {
    name: next(iter(components._SAVED_HASHES[name]))
    for name in components.FILES["launcher"]
    if name != "claude.py"
}


@pytest.mark.parametrize(
    ("saved", "message"),
    [
        ({"launcher_version": 2}, "launcher version 2, and this build's launcher is version 1"),
        (
            {"launcher_sha256": {**_SAVED_CODEX_LAUNCHER, "codex.py": "f" * 64}},
            "codex.py has sha256 ffffffffffff",
        ),
        # A partial set is not a declaration of the launcher that coded the run.
        (
            {"launcher_sha256": {"harbor_backend.py": _SAVED_CODEX_LAUNCHER["harbor_backend.py"]}},
            "hashes harbor_backend.py, not the files of a codex launcher",
        ),
    ],
)
def test_resume_refuses_to_code_with_a_different_launcher(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved: dict[str, object],
    message: str,
) -> None:
    # One comparison must never hold attempts from two launchers, and cached vanilla slots
    # must not be coded and paid for again.
    root, calls, _ = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    coded = len(calls)
    path = root / "inputs/vanilla-identity.json"
    identity = json.loads(path.read_text())
    identity.pop("launcher_version", None)
    path.write_text(json.dumps({**identity, **saved}))
    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    assert result.exit_code == 2
    assert message in result.stderr and "start a new run" in result.stderr
    assert len(calls) == coded


def test_resume_without_a_vanilla_identity_refuses_a_changed_task_runtime(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Runs prepared before vanilla identities were saved still bind the runtime each task
    # was prepared on: new attempts must not join the comparison across an exact change.
    root, calls, _ = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    coded = len(calls)
    (root / "inputs/vanilla-identity.json").unlink()
    monkeypatch.setattr(harbor_backend, "assert_runtime", checked_runtime)
    moved = harbor_backend.runtime_identity

    def other_platform(**kwargs: Any) -> dict[str, Any]:
        runtime = json.loads(json.dumps(moved(**kwargs)))
        runtime["docker_engine"]["platform"] = "linux/riscv64"
        return dict(runtime)

    monkeypatch.setattr(harbor_backend, "runtime_identity", other_platform)
    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    # A refusal, not a traceback: the message and exit code 2, like other refusals.
    assert result.exit_code == 2, result.stderr + result.stdout
    assert 'docker_engine.platform was "linux/arm64", is now "linux/riscv64"' in result.stderr
    assert "Restore that runtime, or start a new run" in result.stderr
    assert len(calls) == coded


@pytest.mark.parametrize("boundary", ["after-record", "after-ledger"])
def test_a_run_killed_after_its_last_attempt_finished_resumes_with_a_newer_launcher(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    # Killed once the final attempt's record receipt (and then its ledger row) is durable,
    # before run.json says so: run.json still shows coding left, but reconciliation does not.
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    evidence = tmp_path / "run/evidence"
    killed = False

    def all_recorded() -> bool:
        entries = json.loads((evidence / "plan.json").read_text())["entries"]
        return all((evidence / "records" / f"{entry['id']}.json").is_file() for entry in entries)

    def kill() -> None:
        nonlocal killed
        killed = True
        raise KeyboardInterrupt("killed")

    write, complete, checkpoint = study._write, Ledger.complete, study._checkpoint

    def writing(path: Path, value: object) -> None:
        write(path, value)
        if boundary == "after-record" and path.parent.name == "records" and all_recorded():
            kill()

    def completing(self: Ledger, *args: Any, **kwargs: Any) -> None:
        complete(self, *args, **kwargs)
        if boundary == "after-ledger" and all_recorded():
            kill()

    def checkpointing(*args: Any, **kwargs: Any) -> None:
        if not killed:  # A killed process writes nothing more.
            checkpoint(*args, **kwargs)

    with monkeypatch.context() as stopped:
        stopped.setattr(study, "_write", writing)
        stopped.setattr(Ledger, "complete", completing)
        stopped.setattr(study, "_checkpoint", checkpointing)
        # A killed process cannot report on the checkpoint it never finished writing.
        stopped.setattr(study, "report_study", lambda *args, **kwargs: None)
        with pytest.raises(KeyboardInterrupt):
            evaluation.run_evaluation(config, tmp_path / "run")
    states = json.loads((evidence / "run.json").read_text())["attempt_states"]
    assert sum(state != "complete" for state in states.values()) == 1
    coded = len(calls)
    monkeypatch.setattr(harbor_backend, "assert_runtime", checked_runtime)
    monkeypatch.setitem(components.VERSIONS, "launcher", components.VERSIONS["launcher"] + 1)
    result = CliRunner().invoke(cli.app, ["run", str(tmp_path / "run"), "--yes", "--foreground"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert len(calls) == coded
    assert json.loads((evidence / "run.json").read_text())["status"] == "complete"


def test_a_run_stopped_after_its_last_attempt_finishes_with_a_newer_launcher(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing is left to code, so a launcher change must not strand the finished paid work.
    config = _measured_and_reviewed(config, tmp_path, monkeypatch)
    calls: list[str] = []
    reviews: list[str] = []
    backend = _coding_and_review_backend(
        calls, reviews, timeout=config.timeout_seconds, interrupt_at=None
    )
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    checkpoint = study._checkpoint

    def killed_at_completion(*args: Any, status: str, **kwargs: Any) -> None:
        if status == "complete":
            raise KeyboardInterrupt("stopped before the completion checkpoint")
        checkpoint(*args, status=status, **kwargs)

    with monkeypatch.context() as stopped:
        stopped.setattr(study, "_checkpoint", killed_at_completion)
        with pytest.raises(KeyboardInterrupt):
            evaluation.run_evaluation(config, tmp_path / "run")
    root = tmp_path / "run"
    assert json.loads((root / "evidence/run.json").read_text())["status"] == "interrupted"
    coded = len(calls)
    monkeypatch.setattr(harbor_backend, "assert_runtime", checked_runtime)
    monkeypatch.setitem(components.VERSIONS, "launcher", components.VERSIONS["launcher"] + 1)
    result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert len(calls) == coded
    assert json.loads((root / "evidence/run.json").read_text())["status"] == "complete"


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _exported(commit: str, tmp_path: Path) -> Path:
    """An earlier build's src and tests, from this clone's history; skips when it is absent."""
    repository = Path(__file__).resolve().parents[1]
    present = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
    )
    if present.returncode:
        pytest.skip(f"{commit} is not in this clone's history")
    export = tmp_path / commit
    export.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", commit, "src", "tests"],
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive.stdout, check=True)
    return export


# Runs an earlier build's own `run <run-dir>` command. Docker and coding are stubbed out, so
# reaching coding ends the process with a line instead of starting an agent.
_OLD_RESUME = """
import json, sys
from pathlib import Path
export, root = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(export / "src"), str(export / "tests")]
import native_support, pluginbench
from pluginbench import evaluation, harbor_backend, study, submissions
assert Path(pluginbench.__file__).is_relative_to(export), pluginbench.__file__
def refuse_docker(*args, **kwargs):
    raise SystemExit("the earlier build ran a Docker command")
def coding(*args, **kwargs):
    raise SystemExit("the earlier build started coding")
harbor_backend._docker = refuse_docker
study._run_attempts = coding
submissions.reviewed_contract = native_support.synthetic_contract
version = json.loads((root / "inputs/study.json").read_text())["harness_version"]
evaluation._harness_version = lambda harness: version
evaluation._assert_resumable = lambda root, selection: None
from typer.testing import CliRunner
from pluginbench import cli
result = CliRunner().invoke(cli.app, ["run", str(root), "--yes", "--foreground"])
print("exit:", result.exit_code, repr(result.exception))
print(result.stderr)
"""


# 942f12c cannot read launcher versions. febdf03 can, but cannot supply a git identity.
@pytest.mark.parametrize("build", ["942f12c", "febdf03"])
@pytest.mark.parametrize("started", [True, False])
def test_an_earlier_build_refuses_a_run_it_would_code_differently(
    config: evaluation.Evaluation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started: bool,
    build: str,
) -> None:
    # The owner runs earlier builds beside this one. Each must refuse a run it would code in
    # another way, instead of spending paid attempts on it.
    export = _exported(build, tmp_path)
    if build == "febdf03":
        # One task has no git identity of its own, so this build supplies one.
        monkeypatch.setattr(
            harbor_backend,
            "probe_coding_environment",
            lambda identity: {
                **passing_git_probe(identity),
                "supplied_git_identity": dict(harbor_backend.SUPPLIED_GIT_IDENTITY),
            },
        )
    if started:
        root, _, _ = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    else:
        prepared = _measured_and_reviewed(config, tmp_path, monkeypatch)
        root = evaluation.prepare_evaluation(prepared, tmp_path / "prepared")
    # Only a run that needs the supplied identity gets the newer schema.
    saved = json.loads((root / "inputs/study.json").read_text())["schema_version"]
    assert saved == (4 if build == "febdf03" else 3)
    before = _tree(root)
    result = subprocess.run(
        [sys.executable, "-I", "-c", _OLD_RESUME, str(export), str(root)],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "exit: 2" in result.stdout and "schema_version" in result.stdout, result.stdout
    assert "started coding" not in result.stdout
    after = _tree(root)
    # Its CLI takes the run lock and records the attempt in events.jsonl before it reads the
    # study; it writes nothing else, and starts no coding.
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    assert changed <= {"run.lock", "events.jsonl"}, changed


# Runs an earlier build's own `report <run-dir>` command, which reads saved evidence only.
_OLD_REPORT = """
import sys
from pathlib import Path
export, root = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(export / "src"), str(export / "tests")]
import native_support, pluginbench
from pluginbench import harbor_backend, submissions
assert Path(pluginbench.__file__).is_relative_to(export), pluginbench.__file__
def refuse_docker(*args, **kwargs):
    raise SystemExit("the earlier build ran a Docker command")
harbor_backend._docker = refuse_docker
submissions.reviewed_contract = native_support.synthetic_contract
from typer.testing import CliRunner
from pluginbench import cli
result = CliRunner().invoke(cli.app, ["report", str(root)])
print("exit:", result.exit_code, result.stderr)
"""

# The build before the not-measured list: its report would publish a listed pair's values.
_BEFORE_LIST = "febdf03"


def _old_report(export: Path, root: Path, tmp_path: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _OLD_REPORT, str(export), str(root)],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_an_earlier_build_refuses_the_report_of_a_run_with_listed_pairs(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    export = _exported(_BEFORE_LIST, tmp_path)
    _coverage_broken_on_one_task(checkable, monkeypatch)
    root, result, _, _ = _run_checkable(checkable, monkeypatch)
    assert result.exit_code == 0, result.stderr
    path = root / "evidence/observer-revisions/measurements/revision.json"
    revision = json.loads(path.read_text())
    assert revision["schema_version"] == 3

    refused = _old_report(export, root, tmp_path)

    assert refused.startswith("exit: 2") and "unsupported measurement revision protocol" in refused
    # Control: without the barrier the earlier build reports, and publishes the hidden values.
    path.write_text(json.dumps({**revision, "schema_version": 2}, indent=2) + "\n")
    assert _old_report(export, root, tmp_path).startswith("exit: 0")
    data = json.loads((root / "report/results.json").read_text())
    assert "measured" in {
        row["status"]
        for row in data["observations"]
        if (row["task"], row["metric"]) == ("arbitrary-native-task", "Line coverage %")
    }


def test_an_earlier_build_writes_the_same_report_for_a_run_without_listed_pairs(
    checkable: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    export = _exported(_BEFORE_LIST, tmp_path)
    root, result, _, _ = _run_checkable(checkable, monkeypatch)
    assert result.exit_code == 0, result.stderr
    assert evaluation._load_selection(root)["not_measured"] == []
    revision = root / "evidence/observer-revisions/measurements/revision.json"
    assert json.loads(revision.read_text())["schema_version"] == 2
    names = ("results.json", "results.csv", "report.html")
    ours = {name: (root / "report" / name).read_bytes() for name in names}

    assert _old_report(export, root, tmp_path).startswith("exit: 0")

    assert {name: (root / "report" / name).read_bytes() for name in names} == ours


def test_report_on_a_run_stopped_during_coding_covers_the_finished_attempts_and_says_so(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped, _, _ = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    root = stopped.rename(tmp_path / "my run")  # The printed command must survive a space.
    result = CliRunner().invoke(cli.app, ["report", str(root)])
    assert result.exit_code == 0, result.stderr
    assert "This run is not finished: 2 of 9 attempts are complete" in result.stderr
    assert f"Finish it with: pluginbench run '{root}'" in result.stderr
    data = json.loads((root / "report/results.json").read_text())
    assert data["unfinished_run"] == {"complete": 2, "assigned": 9}
    assert data["limitations"][0].startswith("This run is not finished: 2 of 9 attempts")
    graded = [row for row in data["attempts"] if row["correctness"] != "unknown"]
    assert len(data["attempts"]) == 9 and len(graded) == 2


def test_judge_prepare_reads_saved_snapshots_when_a_run_has_no_review_packets(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pluginbench import judge

    config.jobs = 1
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.167.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(harbor_backend, "prepare_native_task", _prepare_fixture_task)
    backend = _coding_and_review_backend([], [], timeout=config.timeout_seconds, interrupt_at=None)
    monkeypatch.setattr(codex, "invoke", backend)
    monkeypatch.setattr(harbor_backend, "invoke_native", backend)
    root = tmp_path / "run"
    evaluation.run_evaluation(config, root)  # No reviewer, so no review packets.
    assert not (root / "evidence/review-revisions").exists()
    criteria = tmp_path / "standard.md"
    criteria.write_text("1. Correct behavior.\n")

    def prepare(output: Path) -> Path:
        return judge.prepare(
            arms=[f"{root}=vanilla", f"{root}=workflow"],
            tasks=["arbitrary-native-task"],
            exclude=[],
            criteria=criteria,
            seed=7,
            output=output,
        )

    folder = prepare(tmp_path / "judging") / "task-1"
    # The same folder a run with review packets gives: instruction, source, and blinded diffs.
    assert (folder / "instruction.md").read_text() == "Implement the task.\n"
    assert (folder / "source/src/answer.py").read_text() == "answer = 0\n"
    diffs = sorted(path.name for path in folder.glob("pair-*.diff"))
    assert diffs == [f"pair-{number}-{side}.diff" for number in (1, 2, 3) for side in "AB"]
    diff = (folder / "pair-1-A.diff").read_text()
    assert "diff --git a/src/answer.py b/src/answer.py" in diff and "+answer = 1" in diff
    assert "before/" not in diff and "after/" not in diff
    # Only the starting source is kept beside the diff; the submission's files are not.
    built = sorted(path.name for path in (tmp_path / "judging/submissions").iterdir())
    assert built == ["1", "2", "3", "4", "5", "6"]  # Numbered: no arm or attempt in a path.
    assert not list((tmp_path / "judging/submissions").glob("*/after"))
    key = json.loads((tmp_path / "judging" / judge.KEY).read_text())
    assert {side["arm"] for side in key["tasks"]["task-1"]["pairs"]["1"].values()} == {
        "vanilla",
        "workflow",
    }
    # A report that names another submission than the saved snapshot is refused.
    results = root / "report/results.json"
    saved = results.read_text()
    report = json.loads(saved)
    report["attempts"][0]["artifacts"]["snapshot_digest"] = "sha256:" + "0" * 64
    results.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="saved snapshot is not the reported submission"):
        prepare(tmp_path / "judging-1")
    results.write_text(saved)
    # So is a task instruction that changed after the run.
    catalog = root / "evidence/frozen/tasks.json"
    frozen = catalog.read_text()
    catalog.write_text(frozen.replace("Implement the task.", "Do something else."))
    with pytest.raises(ValueError, match="frozen study inputs changed"):
        prepare(tmp_path / "judging-0")
    catalog.write_text(frozen)
    # A saved snapshot that changed after the run is refused, as a changed packet is.
    answer = next((root / "evidence").glob("attempts/*/handoff/snapshot/src/answer.py"))
    answer.write_text("answer = 2\n")
    with pytest.raises(ValueError, match="snapshot digest mismatch"):
        prepare(tmp_path / "judging-2")


def test_judging_a_run_stopped_during_coding_leaves_out_attempts_without_a_submission(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pluginbench import judge

    root, _, _ = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    evaluation.regenerate_report(root)
    task = "arbitrary-native-task"
    # Both finished attempts are vanilla; the run stopped before any review packet was built.
    vanilla, missing = judge.load_submissions(root, "vanilla", [task])
    assert len(vanilla) == 2 and len(missing) == 1
    workflow, missing = judge.load_submissions(root, "workflow", [task])
    assert workflow == {} and sorted(missing) == [(task, 1), (task, 2), (task, 3)]
