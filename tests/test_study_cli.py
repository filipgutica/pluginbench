"""Public evaluation interface; obsolete separate measurement/review commands are gone."""

from __future__ import annotations

import inspect
import json
import os
import shutil
from pathlib import Path
from threading import Lock
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner
from typer.main import get_group

from pluginbench import (
    claude,
    cli,
    codex,
    evaluation,
    harbor_backend,
    isolation,
    study,
    submissions,
)
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
    monkeypatch.setattr(harbor_backend, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    # Docker-backed checks pass here; tests that exercise a failing check replace one of these.
    monkeypatch.setattr(harbor_backend, "probe_coding_environment", passing_git_probe)
    monkeypatch.setattr(harbor_backend, "run_reference_agents", passing_reference_agents)
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
    assert CliRunner().invoke(cli.app, ["--version"]).stdout.strip() == "0.4.0"


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
        config.measurement_settings.coverage = None

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
                # A collector broken before coding blocks the check; only the required-readiness
                # case keeps it broken there. Otherwise collection fails after coding.
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
    }
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
    assert len(coverage) == len(findings) == 9
    if separate_coverage_scope:
        assert all(row["status"] == "measured" for row in coverage)
        source_lines = [
            row for row in data["observations"] if row["metric"] == "Source lines added"
        ]
        assert sorted(row["value"] for row in source_lines) == [1] + [2] * 8
    assert sum(row["value"] is None for row in coverage) == (
        9 if collection_unavailable or no_profiles else int(unavailable)
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


def _failing_reference_agents(oracle: dict[str, object], nop: dict[str, object]) -> Any:
    return lambda identity, destination: {"oracle": oracle, "nop": nop}


@pytest.mark.parametrize("command", ["check", "run"])
@pytest.mark.parametrize(
    "failure,expected",
    [
        ("codex 0.168.0", "Codex CLI 0.168.0 is not supported"),
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
    if failure == "codex 0.168.0":
        monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.168.0"})
    elif failure == "no code-mode host":
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
    monkeypatch.setattr(evaluation, "__file__", str(package / "evaluation.py"))
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
) -> Any:
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


def test_stopped_run_copied_to_a_new_path_resumes_there(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls, reviews = _stopped_after_two_attempts(config, tmp_path, monkeypatch)
    moved = tmp_path / "moved"
    shutil.copytree(root, moved, symlinks=True)
    shutil.rmtree(root)
    coded = len(calls)
    result = CliRunner().invoke(cli.app, ["run", str(moved), "--yes", "--foreground"])
    assert result.exit_code == 0, result.stderr + result.stdout
    assert len(calls) == coded + 7 and len(reviews) == 9
    assert json.loads((moved / "evidence/run.json").read_text())["status"] == "complete"
    assert not root.exists()
