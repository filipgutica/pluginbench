"""Public evaluation interface; obsolete separate measurement/review commands are gone."""

from __future__ import annotations

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

from pluginbench import claude, cli, codex, evaluation, harbor_backend, study, submissions
from pluginbench.remeasure import propose_remeasurement
from pluginbench.study_review import recover_review_citations
from native_support import (
    task_entry,
    execution as native_execution,
    runtime_identity as native_runtime_identity,
    synthetic_contract,
    qualify_synthetic_source,
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
    assert set(get_group(cli.app).commands) == {"run", "report", "tasks"}
    assert set(get_group(cli.tasks_app).commands) == {"pick"}
    assert CliRunner().invoke(cli.app, ["--version"]).stdout.strip() == "0.3.0"


def test_config_dry_run_includes_fresh_vanilla_and_every_package(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(cli, "run_evaluation", lambda *args: pytest.fail("dry run cannot execute"))
    result = CliRunner().invoke(cli.app, ["run", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["coding_calls"] == 9 and plan["review_calls"] == 0
    assert set(plan["treatments"]) == {"superpowers", "workflow"}
    assert "fresh vanilla" in plan["baseline"]


@pytest.mark.parametrize("jobs", [1, 2, 4])
def test_flags_match_single_package_config(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch, jobs: int
) -> None:
    expected = config.model_copy(deep=True)
    expected.treatments = {"workflow": expected.treatments["workflow"]}
    expected.jobs = jobs
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--harness",
            "codex",
            "--model",
            "gpt-6.1-sol",
            "--reasoning",
            "medium",
            "--guidance",
            str(config.treatments["workflow"].guidance[0]),
            "--guidance-name",
            "workflow",
            "--tasks",
            str(config.tasks.source),
            "--task",
            config.tasks.ids[0],
            "--measure",
            "tokens",
            "--measure",
            "lines",
            "--jobs",
            str(jobs),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == evaluation.plan_evaluation(expected)


@pytest.mark.parametrize("version", ["codex-cli 0.162.0-alpha.1", "codex-cli 0.162.0+build.4"])
def test_harbor_rejects_cli_versions_it_cannot_pin_exactly(
    config: evaluation.Evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    selected = config.model_copy(deep=True)
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
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
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
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
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
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
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
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
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.162.0"})
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


def test_config_and_overrides_fail_clearly(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app, ["run", "--config", str(tmp_path / "missing.yaml"), "--model", "x", "--dry-run"]
    )
    assert (
        result.exit_code == 2 and "not both" in result.stderr and "Traceback" not in result.stderr
    )


@pytest.mark.parametrize("cores,expected", [(None, 1), (1, 1), (2, 2), (18, 4)])
def test_default_jobs_are_resolved_from_cpu_capacity(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch, cores: int | None, expected: int
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: cores)
    raw = config.model_dump(mode="json")
    raw.pop("jobs")
    selected = evaluation.resolve_evaluation(raw, Path.cwd())
    plan = evaluation.plan_evaluation(selected)
    identity = plan["comparison_identity"]
    assert isinstance(identity, dict)
    assert selected.jobs == plan["jobs"] == identity["jobs"] == expected
    assert plan["coding_calls"] == evaluation.plan_evaluation(config)["coding_calls"]


def test_jobs_override_works_with_config_and_is_visible_in_dry_run(
    config: evaluation.Evaluation, tmp_path: Path
) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    result = CliRunner().invoke(cli.app, ["run", "--config", str(path), "--jobs", "2", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["jobs"] == plan["comparison_identity"]["jobs"] == 2
    assert plan["coding_calls"] == 9


@pytest.mark.parametrize(
    "unavailable,recover,citation_error,collection_unavailable,collection_integrity,no_profiles,readiness_required",
    [
        (False, False, False, False, False, False, False),
        (True, False, False, False, False, False, False),
        (False, True, False, False, False, False, False),
        (True, True, False, False, False, False, False),
        (False, False, True, False, False, False, False),
        (False, False, False, True, False, False, False),
        (False, False, False, False, True, False, False),
        (False, False, False, False, False, True, False),
        (False, False, False, True, False, False, True),
        (False, False, False, False, False, False, True),
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
) -> None:
    config.measurement_settings.require_ready = readiness_required
    config.jobs = 1 if recover else 4
    config.measurements += ["coverage", "review"]
    standards = tmp_path / "review-standards.md"
    standards.write_text("Report actionable correctness findings only.\n")
    config.reviewer = evaluation.Reviewer(
        harness="codex", model="gpt-6-astra", reasoning="high", guidance=[standards]
    )
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
            version="0.162.0",
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
                        "cli_version": "0.162.0" if separate_coverage_scope else "test-version"
                    },
                }
            ],
        )
        return result

    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.162.0"})
    monkeypatch.setattr(harbor_backend, "prepare_native_task", prepare)
    monkeypatch.setattr(harbor_backend, "run_native_command", native_coverage)
    monkeypatch.setattr(codex, "runtime_environment", lambda *args: ({}, ()))
    monkeypatch.setattr(codex, "invoke", invoke)
    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    root = tmp_path / "results"
    reward_decoder = study._binary_native_reward
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
        for extra in (
            ["--model", "other"],
            ["--config", "missing.yaml"],
            ["--output", "other"],
            ["--jobs", "2"],
        ):
            rejected = CliRunner().invoke(cli.app, ["run", "--resume", str(root), *extra])
            assert rejected.exit_code == 2 and "saved settings" in rejected.stderr
        dry = CliRunner().invoke(cli.app, ["run", "--resume", str(root), "--dry-run"])
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
        result = CliRunner().invoke(cli.app, ["run", "--resume", str(root), "--yes"])
        assert result.exit_code == 0, result.stderr
        assert "0 coding attempts +" in result.stdout
        failed = root / "evidence/observer-revisions/measurements/revision.json"
        assert json.loads(failed.read_text())["status"] == "failed"
    else:
        if collection_unavailable:
            monkeypatch.setattr(
                harbor_backend,
                "run_native_command",
                lambda **kwargs: {"exit_code": 3, "timed_out": False, "report_collected": False},
            )
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
        with monkeypatch.context() as changed:
            changed.setattr(
                evaluation,
                "_load_selection",
                lambda _: {**saved, "timeout_seconds": config.timeout_seconds + 1},
            )
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
        result = CliRunner().invoke(cli.app, ["run", "--resume", str(root), "--yes"])
        assert result.exit_code == 0, result.stderr
        assert "0 coding attempts + 0 review calls" in result.stdout
        assert (root / "report/results.json").read_bytes() == original
    if recover:
        saved = json.loads((root / "evaluation.json").read_text())
        saved["pending_measurement_revision"] = saved["measurement_revision"]
        saved["measurement_revision"] = None
        evaluation._save_selection(root, saved)
        result = CliRunner().invoke(cli.app, ["run", "--resume", str(root), "--yes"])
        assert result.exit_code == 0, result.stderr
        assert "0 coding attempts + 0 review calls" in result.stdout
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
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.162.0"})
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
