from pathlib import Path

import pytest

from conftest import load_evaluation_config, write_swebench_config

from pluginbench.config import load_config
from pluginbench.datasets import load_tasks
from pluginbench.experiment import (
    _run_verifier,
    _task_results,
    build_dry_run,
    compatibility_payload,
)
from pluginbench.promptfoo import PromptfooTrialResult, Trial
from pluginbench.results import Usage
from pluginbench.skills import inspect_skill_bundle


def test_dry_run_describes_paired_promptfoo_work(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    bundle = inspect_skill_bundle(config.skill.path)
    tasks = load_tasks(config.dataset)

    plan = build_dry_run(config, bundle, tasks, Path("/tools/promptfoo"), "0.122.2")

    assert plan["engine"] == "promptfoo"
    assert plan["estimated_trial_count"] == 4
    assert plan["treatments"] == ["baseline", "treatment"]
    assert plan["tasks"] == ["task-a", "task-b"]
    assert plan["commands"][0][0] == "/tools/promptfoo"
    assert plan["possible_cost_exposure_usd"] is None


def test_swebench_dry_run_shows_official_grader_command(tmp_path: Path) -> None:
    config = load_config(write_swebench_config(tmp_path / "eval.yaml"))
    bundle = inspect_skill_bundle(config.skill.path)
    tasks = load_tasks(config.dataset)

    plan = build_dry_run(config, bundle, tasks, Path("/tools/promptfoo"), "0.122.2")

    assert plan["estimated_trial_count"] == 2
    assert plan["commands"][0][0:2] == ["docker", "run"]
    assert plan["runtime_container_image"] == "pluginbench-runtime:0.2.0"
    assert all("verifiers" not in argument for argument in plan["commands"][0])
    assert plan["benchmark_engine"] == "swebench==5.0.2"
    assert plan["benchmark_commands"][0][:3] == [
        str(tmp_path / "bin" / "swebench"),
        "eval",
        "<run>/baseline/verifiers/sympy__sympy-20590/attempt-01/dataset.json",
    ]


def test_container_dry_run_uses_one_command_per_attempt(tmp_path: Path) -> None:
    config = load_config(write_swebench_config(tmp_path / "eval.yaml"))
    config = config.model_copy(
        update={"execution": config.execution.model_copy(update={"attempts": 2})}
    )
    bundle = inspect_skill_bundle(config.skill.path)
    tasks = load_tasks(config.dataset)

    plan = build_dry_run(config, bundle, tasks, Path("/tools/promptfoo"), "0.122.2")

    assert plan["estimated_trial_count"] == 4
    assert len(plan["commands"]) == 4
    assert "attempt-01" in " ".join(plan["commands"][0])
    assert "attempt-02" not in " ".join(plan["commands"][0])
    assert "attempt-02" in " ".join(plan["commands"][1])
    assert "attempt-01" not in " ".join(plan["commands"][1])


def test_compatibility_payload_excludes_skill_but_includes_task_content(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    tasks = load_tasks(config.dataset)

    payload = compatibility_payload(config, tasks, promptfoo_version="0.122.2")

    assert "skill" not in payload
    assert payload["dataset"]["task_digests"]["task-a"].startswith("sha256:")
    assert payload["agent"]["model"] == "gpt-5.6-luna"
    assert payload["trial"]["batch_size"] == config.execution.batch_size


def test_compatibility_payload_changes_with_batch_size(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    tasks = load_tasks(config.dataset)
    changed = config.model_copy(
        update={"execution": config.execution.model_copy(update={"batch_size": 1})}
    )

    assert compatibility_payload(config, tasks, promptfoo_version="0.122.2") != (
        compatibility_payload(changed, tasks, promptfoo_version="0.122.2")
    )


def test_rejects_promptfoo_result_assigned_to_wrong_task(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    task = load_tasks(config.dataset)[0]
    trial = Trial(
        trial_id="task-a--attempt-01",
        task=task,
        attempt=1,
        workspace=tmp_path,
        codex_home=tmp_path / "codex-home",
        isolated_home=tmp_path / "home",
    )
    parsed = {
        trial.trial_id: PromptfooTrialResult(
            trial_id=trial.trial_id,
            task_id="task-b",
            provider_succeeded=True,
            duration_seconds=1,
            usage=Usage(),
            error=None,
            skill_calls=(),
        )
    }

    with pytest.raises(ValueError, match="task ID mismatch"):
        _task_results([trial], parsed)


def test_marks_result_past_agent_timeout_as_infrastructure_error(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    task = load_tasks(config.dataset)[0]
    trial = Trial(
        trial_id="task-a--attempt-01",
        task=task,
        attempt=1,
        workspace=tmp_path,
        codex_home=tmp_path / "codex-home",
        isolated_home=tmp_path / "home",
        agent_timeout_seconds=10,
    )
    parsed = {
        trial.trial_id: PromptfooTrialResult(
            trial_id=trial.trial_id,
            task_id=task.task_id,
            provider_succeeded=True,
            duration_seconds=11,
            usage=Usage(input_tokens=10, output_tokens=2),
            error=None,
            skill_calls=(),
        )
    }

    result = _task_results([trial], parsed)[task.task_id]

    assert result.score is None
    assert result.passed is False
    assert result.infrastructure_errors == ["promptfoo_reported_timeout_exceeded"]


def test_rejects_unexpected_promptfoo_result(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    task = load_tasks(config.dataset)[0]
    trial = Trial(
        trial_id="task-a--attempt-01",
        task=task,
        attempt=1,
        workspace=tmp_path,
        codex_home=tmp_path / "codex-home",
        isolated_home=tmp_path / "home",
    )
    parsed = {
        "unexpected": PromptfooTrialResult(
            trial_id="unexpected",
            task_id=task.task_id,
            provider_succeeded=True,
            duration_seconds=1,
            usage=Usage(),
            error=None,
            skill_calls=(),
        )
    }

    with pytest.raises(ValueError, match="unexpected trial IDs"):
        _task_results([trial], parsed)


def test_routes_swebench_trials_to_official_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write_swebench_config(tmp_path / "eval.yaml"))
    task = load_tasks(config.dataset)[0]
    trial = Trial(
        trial_id="sympy__sympy-20590--attempt-01",
        task=task,
        attempt=1,
        workspace=tmp_path / "workspace",
        codex_home=tmp_path / "codex-home",
        isolated_home=tmp_path / "home",
        verifier_dir=tmp_path / "verifier",
        model_name="gpt-5.6-luna",
    )
    captured: dict[str, object] = {}

    def fake_official(**kwargs: object) -> tuple[float, None]:
        captured.update(kwargs)
        return 1.0, None

    monkeypatch.setattr("pluginbench.experiment.run_official_evaluation", fake_official)

    score, _, error = _run_verifier(trial)

    assert score == 1.0
    assert error is None
    assert captured["instance_id"] == "sympy__sympy-20590"
    assert captured["model_name"] == "gpt-5.6-luna"
