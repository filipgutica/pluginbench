import json
import shutil
import subprocess
from importlib.metadata import distribution
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakePromptfooRunner, write_evaluation_config, write_swebench_config

from pluginbench import cli
from pluginbench.config import load_config
from pluginbench.experiment import run_evaluation
from pluginbench.skills import inspect_skill_bundle


def test_cli_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.2.0"


def test_installed_distribution_exposes_the_pluginbench_console_script() -> None:
    entry_points = {
        entry_point.name: entry_point
        for entry_point in distribution("pluginbench").entry_points
        if entry_point.group == "console_scripts"
    }

    assert entry_points["pluginbench"].value == "pluginbench.cli:app"
    assert entry_points["pluginbench"].load() is cli.app


def test_cli_dry_run_does_not_start_promptfoo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")
    fake = FakePromptfooRunner()
    monkeypatch.setattr(cli, "_runner", lambda executable: fake)

    result = CliRunner().invoke(cli.app, ["run", str(config_path), "--dry-run"])

    assert result.exit_code == 0
    assert '"engine": "promptfoo"' in result.stdout
    assert '"estimated_trial_count": 4' in result.stdout
    assert fake.calls == []


def test_cli_dry_run_accepts_an_alternate_skill_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")
    alternate = tmp_path / "alternate-skills"
    alternate.mkdir()
    (alternate / "SKILL.md").write_text(
        "---\nname: alternate\ndescription: Alternate workflow guidance.\n---\nUse it.\n"
    )
    fake = FakePromptfooRunner()
    monkeypatch.setattr(cli, "_runner", lambda executable: fake)

    result = CliRunner().invoke(
        cli.app,
        ["run", str(config_path), "--skill", str(alternate), "--dry-run"],
    )

    assert result.exit_code == 0
    assert '"names": [\n      "alternate"\n    ]' in result.stdout
    assert f'"source_path": "{alternate}"' in result.stdout
    assert fake.calls == []


def test_doctor_checks_swebench_and_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")
    fake = FakePromptfooRunner()
    checked: list[str] = []
    monkeypatch.setattr(cli, "_runner", lambda executable: fake)
    monkeypatch.setattr(cli, "_node_version", lambda: (True, "26.3.0"))
    monkeypatch.setattr(cli, "_docker_version", lambda: (True, "29.5.2 linux/arm64"))
    monkeypatch.setattr(cli, "validate_harness", lambda spec: checked.append(spec.harness_version))

    result = CliRunner().invoke(cli.app, ["doctor", str(config_path)])

    assert result.exit_code == 0
    assert "PASS  SWE-bench: 5.0.2" in result.stdout
    assert "PASS  Docker: 29.5.2 linux/arm64" in result.stdout
    assert checked == ["5.0.2"]


def test_node_check_rejects_a_failing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pluginbench.cli.shutil.which", lambda executable: "/usr/local/bin/node")
    monkeypatch.setattr(
        "pluginbench.cli.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["node", "--version"], returncode=1, stdout="v26.3.0\n", stderr="failed\n"
        ),
    )

    assert cli._node_version() == (False, "failed")


def test_doctor_reports_missing_configuration_without_a_traceback(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.app, ["doctor", str(tmp_path / "missing.yaml")])

    assert result.exit_code == 1
    assert "FAIL  Configuration:" in result.stdout
    assert "Traceback (most recent call last)" not in result.stdout
    assert "Traceback (most recent call last)" not in (result.stderr or "")
    assert result.exception is not None


def test_doctor_checks_an_explicit_promptfoo_executable_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_node_version", lambda: (True, "26.3.0"))

    result = CliRunner().invoke(
        cli.app,
        [
            "doctor",
            "--promptfoo-executable",
            str(tmp_path / "missing-promptfoo"),
        ],
    )

    assert result.exit_code == 1
    assert "FAIL  Promptfoo:" in result.stdout


def test_doctor_attributes_sdk_failure_without_duplicate_promptfoo_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")

    class FailingSdkRunner(FakePromptfooRunner):
        def validate_codex_sdk_version(self, expected: str) -> str:
            raise ValueError("SDK mismatch")

    monkeypatch.setattr(cli, "_runner", lambda executable: FailingSdkRunner())
    monkeypatch.setattr(cli, "_node_version", lambda: (True, "26.3.0"))

    result = CliRunner().invoke(cli.app, ["doctor", str(config_path)])

    assert result.exit_code == 1
    assert result.stdout.count("PASS  Promptfoo:") == 1
    assert "FAIL  Codex SDK: SDK mismatch" in result.stdout
    assert "FAIL  Promptfoo:" not in result.stdout


def test_doctor_does_not_report_configuration_pass_after_harness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")
    monkeypatch.setattr(cli, "_runner", lambda executable: FakePromptfooRunner())
    monkeypatch.setattr(cli, "_node_version", lambda: (True, "26.3.0"))
    monkeypatch.setattr(
        cli, "validate_harness", lambda spec: (_ for _ in ()).throw(ValueError("bad harness"))
    )

    result = CliRunner().invoke(cli.app, ["doctor", str(config_path)])

    assert result.exit_code == 1
    assert result.stdout.count("Configuration:") == 1
    assert "FAIL  Configuration: bad harness" in result.stdout
    assert "PASS  Configuration:" not in result.stdout


@pytest.mark.parametrize(
    ("relative_path", "wrong_arm", "message"),
    [
        ("treatment/arm.json", "baseline", "arm=treatment"),
        ("baseline/arm.json", "treatment", "arm=baseline"),
    ],
)
def test_report_rejects_mislabeled_arm_artifacts(
    tmp_path: Path, relative_path: str, wrong_arm: str, message: str
) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")
    config = load_config(config_path)
    run_directory = tmp_path / "run"
    run_evaluation(
        config,
        inspect_skill_bundle(config.skill.path),
        FakePromptfooRunner(),
        run_dir=run_directory,
    )
    arm_path = run_directory / relative_path
    payload = json.loads(arm_path.read_text())
    payload["arm"] = wrong_arm
    arm_path.write_text(json.dumps(payload))

    result = CliRunner().invoke(cli.app, ["report", str(run_directory)])

    assert result.exit_code == 2
    assert message in result.stderr


def test_review_dry_run_resolves_saved_candidates_without_model_calls(
    monkeypatch: pytest.MonkeyPatch, review_config_path: Path
) -> None:
    fake = FakePromptfooRunner()
    monkeypatch.setattr(cli, "_runner", lambda executable: fake)

    result = CliRunner().invoke(
        cli.app,
        [
            "review",
            "run",
            str(review_config_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert '"comparable_task_count": 7' in result.stdout
    assert '"workflow_candidate_count": 21' in result.stdout
    assert '"gold_candidate_count": 7' in result.stdout
    assert '"estimated_review_call_count": 56' in result.stdout
    assert '"implementation_agent_calls": 0' in result.stdout
    assert '"swe_bench_grading_calls": 0' in result.stdout
    assert fake.calls == []


def test_gold_calibration_dry_run_only_schedules_gold_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_config_path: Path,
    completed_review_run: Path,
) -> None:
    target_run = tmp_path / "review-run"
    shutil.copytree(completed_review_run, target_run)
    fake = FakePromptfooRunner()
    monkeypatch.setattr(cli, "_runner", lambda executable: fake)

    result = CliRunner().invoke(
        cli.app,
        [
            "review",
            "calibrate",
            str(target_run),
            str(review_config_path),
            "--dry-run",
            "--max-tokens",
            "15000000",
            "--max-cost-usd",
            "1.5",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert '"mode": "gold_patch_calibration"' in result.stdout
    assert '"workflow_candidate_count": 0' in result.stdout
    assert '"gold_candidate_count": 7' in result.stdout
    assert '"estimated_review_call_count": 14' in result.stdout
    assert '"maximum_token_exposure": 15000000' in result.stdout
    assert '"maximum_cost_exposure_usd": 1.5' in result.stdout
    assert fake.calls == []
