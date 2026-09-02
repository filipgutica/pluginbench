import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import FakePromptfooRunner, load_evaluation_config, write_swebench_config

from pluginbench.config import ConfigOverrides, load_config
from pluginbench.datasets import SWEbenchSpec, load_tasks
from pluginbench.experiment import _ensure_unique_task_slugs, _slug, run_evaluation
from pluginbench.promptfoo import CommandResult, PromptfooExecutionError
from pluginbench.results import load_arm
from pluginbench.skills import inspect_skill_bundle


def test_run_evaluation_writes_paired_artifacts_without_model_calls(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    bundle = inspect_skill_bundle(config.skill.path)
    runner = FakePromptfooRunner()
    run_dir = tmp_path / "first-run"

    result = run_evaluation(config, bundle, runner, run_dir=run_dir)

    assert result == run_dir
    assert len(runner.calls) == 2
    assert load_arm(run_dir / "baseline").tasks_passed == 0
    assert load_arm(run_dir / "treatment").tasks_passed == 2
    assert not (run_dir / "baseline" / "workspaces" / "task-a" / "attempt-01" / ".agents").exists()
    assert (
        run_dir
        / "treatment"
        / "workspaces"
        / "task-a"
        / "attempt-01"
        / ".agents"
        / "skills"
        / "review"
        / "SKILL.md"
    ).is_file()
    report = json.loads((run_dir / "report.json").read_text())
    assert report["comparison"]["treatment_wins"] == 2
    assert json.loads((run_dir / "manifest.json").read_text())["status"] == "complete"
    assert (run_dir / "inputs" / "task-catalog.yaml").is_file()
    assert (run_dir / "inputs" / "tasks" / "task-a" / "fixture" / "README.md").is_file()
    assert (run_dir / "inputs" / "tasks" / "task-a" / "verifier.py").read_text() == (
        tmp_path / "verify.py"
    ).read_text()
    task_input = json.loads((run_dir / "inputs" / "tasks" / "task-a" / "task.json").read_text())
    assert task_input["digest"] == load_arm(run_dir / "baseline").task_checksums["task-a"]
    assert list(tmp_path.glob(".pluginbench-auth-*")) == []


def test_failed_run_preserves_promptfoo_logs_and_removes_staged_auth(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    bundle = inspect_skill_bundle(config.skill.path)

    class FailingRunner(FakePromptfooRunner):
        def execute(
            self,
            config_path: Path,
            output_path: Path,
            *,
            concurrency: int,
            timeout_seconds: float,
            container_image: str | None = None,
        ) -> CommandResult:
            command = CommandResult(
                argv=(str(self.executable), "eval"),
                returncode=9,
                stdout="partial output\n",
                stderr="provider failed\n",
            )
            raise PromptfooExecutionError("Promptfoo failed", command)

    run_dir = tmp_path / "failed-run"
    with pytest.raises(PromptfooExecutionError):
        run_evaluation(config, bundle, FailingRunner(), run_dir=run_dir)

    batch = run_dir / "baseline/batches/0001"
    assert json.loads((batch / "command.json").read_text()) == ["/fake/promptfoo", "eval"]
    assert (batch / "stdout.log").read_text() == "partial output\n"
    assert (batch / "stderr.log").read_text() == "provider failed\n"
    assert list(tmp_path.glob(".pluginbench-auth-*")) == []


def test_local_run_executes_the_snapshotted_verifier(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    bundle = inspect_skill_bundle(config.skill.path)
    original_verifier = tmp_path / "verify.py"

    class MutatingRunner(FakePromptfooRunner):
        def execute(
            self,
            config_path: Path,
            output_path: Path,
            *,
            concurrency: int,
            timeout_seconds: float,
            container_image: str | None = None,
        ) -> CommandResult:
            result = super().execute(
                config_path,
                output_path,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                container_image=container_image,
            )
            original_verifier.write_text("raise SystemExit(0)\n")
            return result

    run_directory = tmp_path / "snapshotted-verifier"
    run_evaluation(config, bundle, MutatingRunner(), run_dir=run_directory)

    assert load_arm(run_directory / "baseline").tasks_passed == 0
    assert load_arm(run_directory / "treatment").tasks_passed == 2


def test_distinct_task_ids_cannot_share_an_artifact_slug(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    tasks = load_tasks(config.dataset)
    transformed = replace(tasks[0], task_id="task a")
    collision = replace(tasks[1], task_id=_slug("task a"))

    with pytest.raises(ValueError, match="same artifact path"):
        _ensure_unique_task_slugs([transformed, collision])


def test_multi_trial_batch_uses_a_batch_sized_command_timeout(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    config = config.model_copy(
        update={"execution": config.execution.model_copy(update={"concurrency": 1})}
    )
    runner = FakePromptfooRunner()

    run_evaluation(
        config,
        inspect_skill_bundle(config.skill.path),
        runner,
        run_dir=tmp_path / "serial-batch",
    )

    assert runner.timeouts == [config.execution.timeout_seconds * 2] * 2


def test_treatment_stages_the_recorded_skill_snapshot(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    bundle = inspect_skill_bundle(config.skill.path)
    skill_file = config.skill.path / "review" / "SKILL.md"
    original = skill_file.read_text()
    staged_content: list[str] = []

    class MutatingSkillRunner(FakePromptfooRunner):
        def execute(
            self,
            config_path: Path,
            output_path: Path,
            *,
            concurrency: int,
            timeout_seconds: float,
            container_image: str | None = None,
        ) -> CommandResult:
            loaded = yaml.safe_load(config_path.read_text())
            workspace = Path(loaded["tests"][0]["vars"]["workspace_dir"])
            staged = workspace / ".agents/skills/review/SKILL.md"
            if staged.is_file():
                staged_content.append(staged.read_text())
            result = super().execute(
                config_path,
                output_path,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                container_image=container_image,
            )
            skill_file.write_text(original + "Source changed during the run.\n")
            return result

    run_evaluation(config, bundle, MutatingSkillRunner(), run_dir=tmp_path / "skill-snapshot")

    assert staged_content == [original]


def test_reuse_runs_only_treatment(tmp_path: Path) -> None:
    initial = load_evaluation_config(tmp_path)
    bundle = inspect_skill_bundle(initial.skill.path)
    run_evaluation(initial, bundle, FakePromptfooRunner(), run_dir=tmp_path / "first")
    baseline_path = tmp_path / "first" / "baseline" / "arm.json"
    reused = load_evaluation_config(tmp_path, ConfigOverrides(baseline_result=baseline_path))
    runner = FakePromptfooRunner()

    run_evaluation(reused, bundle, runner, run_dir=tmp_path / "reused")

    assert len(runner.calls) == 1
    report = json.loads((tmp_path / "reused" / "report.json").read_text())
    assert report["comparison"]["treatment_wins"] == 2


def test_skip_baseline_omits_comparison(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path, ConfigOverrides(skip_baseline=True))
    bundle = inspect_skill_bundle(config.skill.path)

    run_evaluation(config, bundle, FakePromptfooRunner(), run_dir=tmp_path / "skip")

    report = json.loads((tmp_path / "skip" / "report.json").read_text())
    assert "comparison" not in report


def test_cost_limit_stops_before_the_next_paired_batch(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    config = config.model_copy(
        update={
            "execution": config.execution.model_copy(update={"batch_size": 1}),
            "limits": config.limits.model_copy(update={"max_total_cost_usd": 0.0015}),
        }
    )
    bundle = inspect_skill_bundle(config.skill.path)
    runner = FakePromptfooRunner()

    run_evaluation(config, bundle, runner, run_dir=tmp_path / "limited")

    assert len(runner.calls) == 2
    manifest = json.loads((tmp_path / "limited" / "manifest.json").read_text())
    assert manifest["status"] == "stopped_by_limit"
    assert load_arm(tmp_path / "limited" / "baseline").tasks_attempted == 1
    assert load_arm(tmp_path / "limited" / "treatment").tasks_attempted == 1


def test_swebench_run_uses_pinned_checkout_and_official_grader_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(write_swebench_config(tmp_path / "eval.yaml"))
    config = config.model_copy(
        update={
            "execution": config.execution.model_copy(update={"attempts": 2}),
        }
    )
    bundle = inspect_skill_bundle(config.skill.path)
    prepared: list[str] = []
    runner = FakePromptfooRunner()

    def fake_materialize(spec: SWEbenchSpec, destination: Path) -> None:
        prepared.append(spec.base_commit)
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("fixture\n")

    def fake_official(*, workspace: Path, **kwargs: Any) -> tuple[float, None]:
        return (1.0, None) if (workspace / ".agents" / "skills").is_dir() else (0.0, None)

    monkeypatch.setattr("pluginbench.experiment.materialize_repository", fake_materialize)
    monkeypatch.setattr("pluginbench.experiment.validate_harness", lambda spec: None)
    monkeypatch.setattr("pluginbench.experiment.prepare_image", lambda spec: None)
    monkeypatch.setattr("pluginbench.experiment.run_official_evaluation", fake_official)

    run_evaluation(config, bundle, runner, run_dir=tmp_path / "swe-run")

    assert prepared == ["cffd4e0f86fefd4802349a9f9b19ed70934ea354"]
    assert len(runner.calls) == 4
    assert runner.container_images == ["sha256:fake-pluginbench-runtime"] * 4
    assert all("trials" in config_path.parts for config_path, _, _ in runner.calls)
    assert all(
        len(yaml.safe_load(config_path.read_text())["tests"]) == 1
        for config_path, _, _ in runner.calls
    )
    report = json.loads((tmp_path / "swe-run" / "report.json").read_text())
    assert report["comparison"]["treatment_wins"] == 1
    assert (tmp_path / "swe-run" / "inputs" / "swebench-dataset.json").is_file()


def test_swebench_run_applies_starting_patch_to_both_arms_and_reports_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starting_patch = tmp_path / "candidate.patch"
    starting_patch.write_text(
        "diff --git a/seeded.py b/seeded.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/seeded.py\n"
        "@@ -0,0 +1 @@\n"
        "+BROKEN = True\n"
    )
    config = load_config(
        write_swebench_config(tmp_path / "eval.yaml", starting_patch=starting_patch)
    )
    config = config.model_copy(
        update={"execution": config.execution.model_copy(update={"attempts": 2})}
    )
    bundle = inspect_skill_bundle(config.skill.path)
    seeded_workspaces: list[Path] = []

    class StartingPatchRunner(FakePromptfooRunner):
        def execute(
            self,
            config_path: Path,
            output_path: Path,
            *,
            concurrency: int,
            timeout_seconds: float,
            container_image: str | None = None,
        ) -> CommandResult:
            loaded = yaml.safe_load(config_path.read_text())
            workspace = Path(loaded["tests"][0]["vars"]["workspace_dir"])
            assert (workspace / "seeded.py").read_text() == "BROKEN = True\n"
            staged = subprocess.run(
                ["git", "-C", str(workspace), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            )
            assert staged.stdout == "seeded.py\n"
            seeded_workspaces.append(workspace)
            return super().execute(
                config_path,
                output_path,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                container_image=container_image,
            )

    def fake_materialize(_spec: SWEbenchSpec, destination: Path) -> None:
        subprocess.run(["git", "init", str(destination)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(destination), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(["git", "-C", str(destination), "config", "user.name", "Test"], check=True)
        (destination / "README.md").write_text("fixture\n")
        subprocess.run(["git", "-C", str(destination), "add", "README.md"], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "-C",
                str(destination),
                "commit",
                "-m",
                "base",
            ],
            check=True,
        )

    def fake_official(*, workspace: Path, **kwargs: Any) -> tuple[float, None]:
        return (1.0, None) if (workspace / ".agents" / "skills").is_dir() else (0.0, None)

    monkeypatch.setattr("pluginbench.experiment.materialize_repository", fake_materialize)
    monkeypatch.setattr("pluginbench.experiment.validate_harness", lambda spec: None)
    monkeypatch.setattr("pluginbench.experiment.prepare_image", lambda spec: None)
    monkeypatch.setattr("pluginbench.experiment.run_official_evaluation", fake_official)

    run_dir = tmp_path / "repair-run"
    run_evaluation(config, bundle, StartingPatchRunner(), run_dir=run_dir)

    assert len(seeded_workspaces) == 4
    baseline = load_arm(run_dir / "baseline")
    treatment = load_arm(run_dir / "treatment")
    assert baseline.tasks["sympy__sympy-20590"].starting_patch is not None
    assert baseline.tasks["sympy__sympy-20590"].starting_patch.unchanged_failure_attempts == 2
    assert treatment.tasks["sympy__sympy-20590"].starting_patch is not None
    assert treatment.tasks["sympy__sympy-20590"].starting_patch.repaired_attempts == 2
    report = json.loads((run_dir / "report.json").read_text())
    assert report["baseline"]["starting_patch"]["repair_rate"] == 0
    assert report["treatment"]["starting_patch"]["repair_rate"] == 1
    assert report["comparison"]["repair_rate_lift_pp"] == 100
    task_input = json.loads((run_dir / "inputs/tasks/sympy__sympy-20590/task.json").read_text())
    assert task_input["starting_patch"]["expected_score"] == 0
    assert (run_dir / "inputs/tasks/sympy__sympy-20590/starting.patch").read_text() == (
        starting_patch.read_text()
    )
