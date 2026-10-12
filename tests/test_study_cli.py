"""Public evaluation interface; obsolete separate measurement/review commands are gone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Lock
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from pluginbench import claude, cli, codex, collectors, evaluation, harbor_backend
from pluginbench.remeasure import propose_remeasurement
from pluginbench.study_review import recover_review_citations


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> evaluation.Evaluation:
    suite = tmp_path / "deep-swe"
    task = suite / "tasks" / evaluation.SUPPORTED_TASKS[0]
    for name in ("environment", "tests", "solution"):
        (task / name).mkdir(parents=True)
    (task / "task.toml").write_text(
        "version='1.0'\n[agent]\ntimeout_sec=10800.0\n"
        "[environment]\ncpus=2\nmemory_mb=8192\nbuild_timeout_sec=1800.0\n"
    )
    (task / "instruction.md").write_text("Implement the task.\n")
    (tmp_path / "a.md").write_text("Use existing helpers.\n")
    (tmp_path / "b.md").write_text("Keep state minimal.\n")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **kw: evaluation.SUITE_SHA if "rev-parse" in argv else "",
    )
    return evaluation.resolve_evaluation(
        {
            "backend": "local",
            "runner": {"harness": "codex", "model": "gpt-6.1-sol", "reasoning": "medium"},
            "guidance": {"workflow": ["a.md"], "superpowers": ["b.md"]},
            "tasks": {"source": "deep-swe/tasks", "ids": [evaluation.SUPPORTED_TASKS[0]]},
            "measurements": ["tokens", "lines"],
        },
        tmp_path,
    )


def test_only_run_and_report_are_public() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout and "report" in result.stdout
    assert all(name not in result.stdout for name in ("legacy", "Promptfoo", "measure ", "review "))
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
    assert plan["guidance"] == ["superpowers", "workflow"] or plan["guidance"] == [
        "workflow",
        "superpowers",
    ]
    assert "fresh vanilla" in plan["baseline"]


@pytest.mark.parametrize("jobs", [1, 2, 4])
def test_flags_match_single_package_config(
    config: evaluation.Evaluation, monkeypatch: pytest.MonkeyPatch, jobs: int
) -> None:
    expected = config.model_copy(deep=True)
    expected.guidance = {"workflow": expected.guidance["workflow"]}
    expected.jobs = jobs
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--backend",
            "local",
            "--harness",
            "codex",
            "--model",
            "gpt-6.1-sol",
            "--reasoning",
            "medium",
            "--guidance",
            str(config.guidance["workflow"][0]),
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
    selected = config.model_copy(update={"backend": "harbor"})
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: {"offline": True})
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
    selected = config.model_copy(update={"backend": "harbor"})
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
    selected = config.model_copy(update={"backend": "harbor", **override})
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
    selected = config.model_copy(update={"backend": "harbor"})
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
    selected = config.model_copy(deep=True, update={"backend": "harbor"})
    task = selected.tasks.source / evaluation.SUPPORTED_TASKS[1]
    for name in ("environment", "tests", "solution"):
        (task / name).mkdir(parents=True)
    (task / "instruction.md").write_text("Implement another task.\n")
    (task / "task.toml").write_text(
        "[agent]\ntimeout_sec=10800\n[environment]\ncpus=2\nmemory_mb=4096\nbuild_timeout_sec=1800\n"
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
    selected = config.model_copy(update={"backend": "harbor"})
    definition = selected.tasks.source / selected.tasks.ids[0] / "task.toml"
    definition.write_text(definition.read_text() + "docker_image='fixture:fixed'\n")
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {})
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli 0.162.0"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args, **kwargs: ({}, ()))
    monkeypatch.setattr(harbor_backend, "resolve_image", lambda image: {"image": image})
    monkeypatch.setattr(
        harbor_backend, "prepare_toolchain", lambda *args, **kwargs: {"fixed": True}
    )
    monkeypatch.setattr(codex, "invoke", lambda **kwargs: pytest.fail("no coding call"))
    monkeypatch.setattr(harbor_backend, "invoke", lambda **kwargs: pytest.fail("no native call"))

    def prepare(argv: list[str], **kwargs: Any) -> None:
        assert str(evaluation.ADAPTER / "prepare.py") in argv
        assert "--prepared-dependencies" in argv
        destination = Path(argv[argv.index("--output") + 1])
        (destination / "fixture").mkdir(parents=True)
        (destination / "fixture/source.ts").write_text("export const value = 1;\n")
        (destination / "native-environment.json").write_text("{}")
        (destination / "tasks.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tasks": [
                        {
                            "id": selected.tasks.ids[0],
                            "fixture": "fixture",
                            "prompt": "Implement the change.",
                            "verifier": {"command": [sys.executable], "script": "verify.py"},
                        }
                    ],
                }
            )
        )

    def reject(**kwargs: Any) -> None:
        assert kwargs["fixture"].is_dir()
        assert kwargs["cpus"] == 2 and kwargs["memory_mb"] == 8192
        raise ValueError("native visible checks failed")

    monkeypatch.setattr(subprocess, "run", prepare)
    monkeypatch.setattr(harbor_backend, "prepare_task_image", reject)
    root = tmp_path / "evaluation"
    with pytest.raises(ValueError, match="native visible checks failed"):
        evaluation.run_evaluation(selected, root)
    assert not (root / "evidence/attempts").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"measurements": ["nope"]},
        {"measurements": ["review"]},
        {"repeats": 0},
        {"tasks": {"source": ".", "ids": ["unsupported-task"]}},
        {"guidance": {"vanilla": ["a.md"]}},
        *({"jobs": jobs} for jobs in (0, 17, True, False, 1.5, "2", None)),
    ],
)
def test_invalid_contract_rejected_before_calls(
    config: evaluation.Evaluation, mutation: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        evaluation.resolve_evaluation(
            {**config.model_dump(mode="json"), **mutation}, config.tasks.source.parent.parent
        )


def test_fixed_reviewer_separate_from_candidate_guidance(config: evaluation.Evaluation) -> None:
    raw = config.model_dump(mode="json")
    raw["measurements"].append("review")
    raw["reviewer"] = {
        "harness": "claude",
        "model": "claude-sonnet-5-5",
        "reasoning": "high",
        "guidance": [str(config.guidance["workflow"][0])],
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
    changed.guidance = {"other": config.guidance["workflow"]}
    assert evaluation.plan_evaluation(changed)["comparison_identity"] == original
    changed.jobs = 1 if config.jobs != 1 else 2
    assert evaluation.plan_evaluation(changed)["comparison_identity"] != original
    changed.jobs = config.jobs
    changed.runner.reasoning = "high"
    assert evaluation.plan_evaluation(changed)["comparison_identity"] != original
    (config.tasks.source / config.tasks.ids[0] / "instruction.md").write_text("Changed task")
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
    "unavailable,recover,citation_error,collection_unavailable,collection_integrity",
    [
        (False, False, False, False, False),
        (True, False, False, False, False),
        (False, True, False, False, False),
        (True, True, False, False, False),
        (False, False, True, False, False),
        (False, False, False, True, False),
        (False, False, False, False, True),
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
) -> None:
    config.jobs = 1 if recover else 4
    config.measurements += ["coverage", "review"]
    standards = tmp_path / "review-standards.md"
    standards.write_text("Report actionable correctness findings only.\n")
    config.reviewer = evaluation.Reviewer(
        harness="codex", model="gpt-6-astra", reasoning="high", guidance=[standards]
    )
    real_run = subprocess.run

    def prepare(argv: list[str], **kwargs: Any) -> Any:
        if str(evaluation.ADAPTER / "calibrate.py") in argv:
            return None
        if str(evaluation.ADAPTER / "prepare.py") not in argv:
            return real_run(argv, **kwargs)
        destination = Path(argv[argv.index("--output") + 1])
        fixture = destination / "fixture"
        (fixture / "src").mkdir(parents=True)
        (fixture / "src/answer.py").write_text("answer = 0\n")
        (destination / "verify.py").write_text(
            "from pathlib import Path\nimport sys\nsys.exit(0 if Path('src/answer.py').read_text() == 'answer = 1\\n' else 1)\n"
        )
        (destination / "tasks.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tasks": [
                        {
                            "id": config.tasks.ids[0],
                            "fixture": "fixture",
                            "prompt": "Set answer to one.",
                            "verifier": {
                                "command": [sys.executable],
                                "script": "verify.py",
                            },
                        }
                    ],
                }
            )
        )
        collector = destination / "coverage.py"
        collector.write_text(
            "import json,pathlib,sys\n"
            "report={'files_measured':['src/answer.py'], 'branches':{'total':['src/answer.py:1:0'],'covered':['src/answer.py:1:0']}, 'functions':{'total':['src/answer.py:1:0'],'covered':['src/answer.py:1:0']}}\n"
            "pathlib.Path(sys.argv[2]).write_text(json.dumps(report))\n"
        )
        profile = destination / "coverage.json"
        profile.write_text('{"test_scope":["tests/**"]}')
        (destination / "measurements.json").write_text(
            json.dumps(
                {
                    config.tasks.ids[0]: [
                        {
                            "kind": "coverage",
                            "config": str(profile),
                            "scope": ["src/**"],
                            "command": [sys.executable, str(collector), "{snapshot}", "{output}"],
                            "tool_version": "fixture-1",
                            "failure_protocol": "coverage-exit-v1",
                        }
                    ]
                }
            )
        )
        (destination / "provenance.json").write_text(
            json.dumps(
                {
                    "source_scope": '["src/**"]',
                    "test_scope": '["tests/**"]',
                    "measurement_profile_version": "test-v1",
                }
            )
        )
        return None

    calls = []
    judge_calls = []
    call_lock = Lock()

    def invoke(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("read_only"):
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
        return {
            "successful_native_completion": True,
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
            "usage": {"input_tokens": 100 + call_number, "output_tokens": 20},
            "startup": [{"type": "session_meta", "payload": {"cli_version": "test-version"}}],
        }

    monkeypatch.setattr(subprocess, "run", prepare)
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "test-version"})
    monkeypatch.setattr(codex, "runtime_environment", lambda *args: ({}, ()))
    monkeypatch.setattr(codex, "invoke", invoke)
    root = tmp_path / "results"
    if collection_integrity:
        monkeypatch.setattr(
            collectors,
            "_execute",
            lambda argv, cwd, timeout, *, output=None, config=None: subprocess.CompletedProcess(
                argv, 2, stdout="", stderr="inner integrity rejected"
            ),
        )
        with pytest.raises(ValueError, match="trusted coverage adapter rejected evidence"):
            evaluation.run_evaluation(config, root)
        assert len(calls) == 9 and not judge_calls
        state = json.loads(
            (root / "evidence/observer-revisions/measurements/revision.json").read_text()
        )
        assert state["status"] == "failed"
        assert not (root / "report").exists()
        assert not (root / "evidence/review-revisions").exists()
        return
    if recover:

        def strict_initial(
            evidence: Path,
            *,
            profiles: dict[str, list[collectors.Collector]],
            revision: str,
            progress: Any = None,
            allow_partial: bool = False,
            jobs: int = 1,
        ) -> Path:
            return propose_remeasurement(
                evidence, profiles=profiles, revision=revision, progress=progress, jobs=jobs
            )

        monkeypatch.setattr(evaluation, "propose_remeasurement", strict_initial)
        execute = collectors._execute
        monkeypatch.setattr(
            collectors,
            "_execute",
            lambda argv, cwd, timeout, *, output=None, config=None: subprocess.CompletedProcess(
                argv, 3, stdout="", stderr="observer unavailable"
            ),
        )
        with pytest.raises(ValueError, match="partial observer collection"):
            evaluation.run_evaluation(config, root)
        assert len(calls) == 9 and not judge_calls
        legacy = evaluation._load_selection(root)
        legacy.pop("jobs")
        evaluation._save_selection(root, legacy)
        assert evaluation.plan_resume(root)["jobs"] == 1
        monkeypatch.setattr(collectors, "_execute", execute)
        frozen = next((root / "evidence/observer-revisions/measurements/profiles").rglob("*.py"))
        original_profile = frozen.read_bytes()
        frozen.write_bytes(original_profile + b"\n# edited\n")
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
        for files in config.guidance.values():
            for file in files:
                file.unlink()
        standards.unlink()
        result = CliRunner().invoke(cli.app, ["run", "--resume", str(root), "--yes"])
        assert result.exit_code == 0, result.stderr
        assert "0 coding calls +" in result.stdout
        failed = root / "evidence/observer-revisions/measurements/revision.json"
        assert json.loads(failed.read_text())["status"] == "failed"
    else:
        if collection_unavailable:
            monkeypatch.setattr(
                collectors,
                "_execute",
                lambda argv, cwd, timeout, *, output=None, config=None: subprocess.CompletedProcess(
                    argv, 3, stdout="", stderr="coverage unavailable"
                ),
            )
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
    data = json.loads((root / "report/results.json").read_text())
    assert data["jobs"] == data["comparison_identity"]["jobs"] == config.jobs
    assert json.loads((root / "evidence/run.json").read_text())["jobs"] == config.jobs
    saved_selection = evaluation._load_selection(root)
    measurement = (
        root / "evidence/observer-revisions" / str(saved_selection["measurement_revision"])
    )
    review = root / "evidence/review-revisions" / str(saved_selection["review_revision"])
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
    assert sum(row["value"] is None for row in coverage) == (
        9 if collection_unavailable else int(unavailable)
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
        assert "0 coding calls + 0 review calls" in result.stdout
        assert (root / "report/results.json").read_bytes() == original
    if recover:
        saved = json.loads((root / "evaluation.json").read_text())
        saved["pending_measurement_revision"] = saved["measurement_revision"]
        saved["measurement_revision"] = None
        evaluation._save_selection(root, saved)
        result = CliRunner().invoke(cli.app, ["run", "--resume", str(root), "--yes"])
        assert result.exit_code == 0, result.stderr
        assert "0 coding calls + 0 review calls" in result.stdout
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
    monkeypatch.setattr(codex, "preflight", lambda: {})
    monkeypatch.setattr(claude, "preflight", lambda: {})

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
