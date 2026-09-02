import json
import subprocess
from pathlib import Path

import pytest

from pluginbench.swebench import (
    build_clone_argv,
    build_eval_argv,
    capture_patch,
    parse_swebench_report,
    run_official_evaluation,
    validate_harness,
)

from conftest import write_swebench_config
from pluginbench.config import load_config
from pluginbench.datasets import load_tasks


def test_builds_safe_repository_checkout_commands() -> None:
    commands = build_clone_argv(
        repo="sympy/sympy",
        base_commit="cffd4e0f86fefd4802349a9f9b19ed70934ea354",
        destination=Path("/tmp/worktree"),
    )

    assert commands == (
        ("git", "init", "/tmp/worktree"),
        (
            "git",
            "-C",
            "/tmp/worktree",
            "remote",
            "add",
            "origin",
            "https://github.com/sympy/sympy.git",
        ),
        (
            "git",
            "-C",
            "/tmp/worktree",
            "fetch",
            "--depth=1",
            "origin",
            "cffd4e0f86fefd4802349a9f9b19ed70934ea354",
        ),
        ("git", "-C", "/tmp/worktree", "checkout", "--detach", "FETCH_HEAD"),
    )


def test_builds_safe_official_harness_command() -> None:
    argv = build_eval_argv(
        executable=Path("/tools/swebench"),
        dataset_path=Path("/run/dataset.json"),
        predictions_path=Path("/run/predictions.jsonl"),
        instance_id="sympy__sympy-20590",
        run_id="sympy__sympy-20590--attempt-01",
        timeout_seconds=900,
        report_dir=Path("/run"),
    )

    assert argv[:4] == ("/tools/swebench", "eval", "/run/dataset.json", "--predictions")
    assert "--gold" not in argv
    assert argv[-2:] == ("--report-dir", "/run")


def test_harness_validation_reports_a_controlled_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")
    spec = load_tasks(load_config(config_path).dataset)[0].swebench
    assert spec is not None
    spec.harness_executable.parent.mkdir(parents=True, exist_ok=True)
    spec.harness_executable.write_text("")
    spec.harness_executable.with_name("python").write_text("")
    monkeypatch.setattr(
        "pluginbench.swebench.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["python"], timeout=30)
        ),
    )

    with pytest.raises(ValueError, match="SWE-bench version check timed out after 30 seconds"):
        validate_harness(spec)


def test_parses_resolved_and_infrastructure_results(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "resolved_ids": ["task-pass"],
                "unresolved_ids": ["task-fail"],
                "error_ids": ["task-error"],
                "infra_failure_ids": ["task-infra"],
            }
        )
    )

    assert parse_swebench_report(report, "task-pass") == (1.0, None)
    assert parse_swebench_report(report, "task-fail") == (0.0, None)
    assert parse_swebench_report(report, "task-error") == (None, "swebench_error")
    assert parse_swebench_report(report, "task-infra") == (None, "swebench_infrastructure_error")


def test_patch_capture_uses_the_original_base_after_agent_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    source = tmp_path / "source.py"
    source.write_text("before\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_text("after\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-am", "agent change"], check=True)

    patch = capture_patch(tmp_path, base)

    assert "-before" in patch
    assert "+after" in patch


def test_failed_evaluator_process_is_an_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")
    spec = load_tasks(load_config(config_path).dataset)[0].swebench
    assert spec is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_dir = tmp_path / "artifacts"

    def fake_capture_patch(*args: object, **kwargs: object) -> str:
        return "patch\n"

    monkeypatch.setattr("pluginbench.swebench.capture_patch", fake_capture_patch)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        report = artifact_dir / "model.task-a--attempt-01.json"
        report.write_text(json.dumps({"resolved_ids": ["task-a"]}))
        return subprocess.CompletedProcess(
            args=["swebench"], returncode=1, stdout="", stderr="boom"
        )

    monkeypatch.setattr("pluginbench.swebench.subprocess.run", fake_run)

    result = run_official_evaluation(
        spec=spec,
        instance_id="task-a",
        workspace=workspace,
        artifact_dir=artifact_dir,
        run_id="task-a--attempt-01",
        model_name="model",
        timeout_seconds=30,
    )

    assert result == (None, "swebench_evaluator_failed")
