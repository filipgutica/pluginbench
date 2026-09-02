import json
from pathlib import Path

import pytest

from conftest import load_evaluation_config

from pluginbench.datasets import load_tasks
from pluginbench.promptfoo import (
    _bind_mount,
    CommandResult,
    PromptfooExecutionError,
    PromptfooRunner,
    ReviewTrial,
    Trial,
    build_container_eval_argv,
    build_eval_argv,
    build_process_environment,
    build_promptfoo_config,
    build_review_promptfoo_config,
    parse_promptfoo_results,
)
from pluginbench.review_models import load_review_config


def test_bind_mount_preserves_macos_var_path() -> None:
    assert _bind_mount(Path("/var/folders/pluginbench")) == (
        "type=bind,src=/var/folders/pluginbench,dst=/var/folders/pluginbench"
    )


def test_promptfoo_runner_finds_repository_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "node_modules" / ".bin" / "promptfoo"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env node\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pluginbench.promptfoo.shutil.which", lambda _: None)

    assert PromptfooRunner().executable == executable


def test_promptfoo_process_uses_only_the_copied_codex_login() -> None:
    environment = build_process_environment(
        {
            "PATH": "/tools",
            "OPENAI_API_KEY": "api-secret",
            "CODEX_API_KEY": "codex-secret",
            "CODEX_ACCESS_TOKEN": "access-secret",
            "GH_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "SENTINEL_SECRET": "sentinel-secret",
        }
    )

    assert environment["PATH"] == "/tools"
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "CODEX_ACCESS_TOKEN" not in environment
    assert "GH_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "SENTINEL_SECRET" not in environment
    assert environment["PROMPTFOO_DISABLE_SHARING"] == "true"


def test_failed_promptfoo_process_preserves_the_command_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "promptfoo"
    executable.write_text("")
    runner = PromptfooRunner(executable)
    monkeypatch.setattr(
        "pluginbench.promptfoo.subprocess.run",
        lambda *args, **kwargs: __import__("subprocess").CompletedProcess(
            args=[str(executable)], returncode=7, stdout="diagnostic out", stderr="diagnostic err"
        ),
    )

    with pytest.raises(PromptfooExecutionError) as captured:
        runner.execute(
            tmp_path / "config.yaml",
            tmp_path / "output.json",
            concurrency=1,
            timeout_seconds=10,
        )

    assert captured.value.command == CommandResult(
        argv=captured.value.command.argv,
        returncode=7,
        stdout="diagnostic out",
        stderr="diagnostic err",
    )


def test_container_validation_reports_a_controlled_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "promptfoo"
    executable.write_text("")
    runner = PromptfooRunner(executable)
    monkeypatch.setattr(
        "pluginbench.promptfoo.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            __import__("subprocess").TimeoutExpired(cmd=["docker"], timeout=30)
        ),
    )

    with pytest.raises(
        ValueError,
        match="Promptfoo container validation timed out after 30 seconds: runtime:latest",
    ):
        runner.validate_container_image(
            "runtime:latest",
            promptfoo_version="0.122.2",
            codex_sdk_version="0.151.0",
        )


def test_builds_shell_safe_promptfoo_command() -> None:
    argv = build_eval_argv(
        Path("/tools/promptfoo"),
        Path("/tmp/config with spaces.yaml"),
        Path("/tmp/results.json"),
        concurrency=3,
    )

    assert argv == (
        "/tools/promptfoo",
        "eval",
        "--config",
        "/tmp/config with spaces.yaml",
        "--output",
        "/tmp/results.json",
        "--no-cache",
        "--no-share",
        "--no-write",
        "--no-table",
        "--no-progress-bar",
        "--max-concurrency",
        "3",
    )


def test_container_eval_mounts_only_the_trial_inputs(tmp_path: Path) -> None:
    config_path = tmp_path / "batch" / "promptfooconfig.yaml"
    output_path = config_path.parent / "results.json"
    workspace = tmp_path / "run" / "baseline" / "workspace"
    codex_home = tmp_path / "auth" / "codex-home"
    isolated_home = tmp_path / "auth" / "home"
    for path in (config_path.parent, workspace, codex_home, isolated_home):
        path.mkdir(parents=True, exist_ok=True)

    argv = build_container_eval_argv(
        image="pluginbench-runtime:0.2.0",
        config_path=config_path,
        output_path=output_path,
        workspaces=(workspace,),
        codex_homes=(codex_home,),
        isolated_homes=(isolated_home,),
        concurrency=1,
        timeout_seconds=600,
    )

    command = " ".join(argv)
    assert argv[:3] == ("docker", "run", "--rm")
    assert "SYS_ADMIN" in argv
    assert "apparmor=unconfined" in argv
    assert "seccomp=unconfined" in argv
    assert str(workspace) in command
    assert str(codex_home) in command
    assert str(isolated_home) in command
    mounts = {argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"}
    assert mounts == {
        _bind_mount(config_path.parent),
        _bind_mount(workspace),
        _bind_mount(codex_home),
        _bind_mount(isolated_home),
    }


def test_promptfoo_config_uses_row_specific_isolation(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    task = load_tasks(config.dataset)[0]
    trial = Trial(
        trial_id="task-a--1",
        task=task,
        attempt=1,
        workspace=tmp_path / "workspace",
        codex_home=tmp_path / "codex-home",
        isolated_home=tmp_path / "home",
    )

    generated = build_promptfoo_config(config, [trial])

    provider = generated["providers"][0]
    assert provider["id"] == "openai:codex-sdk"
    assert provider["config"]["working_dir"] == "{{workspace_dir}}"
    assert provider["config"]["cli_env"] == {
        "CODEX_HOME": "{{codex_home}}",
        "HOME": "{{isolated_home}}",
    }
    assert generated["tests"][0]["vars"]["task_id"] == "task-a"


def test_review_promptfoo_config_is_read_only_structured_and_skill_free(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    config = load_review_config(root / "benchmarks" / "filip-stack-review" / "eval.yaml")
    trial = ReviewTrial(
        trial_id="candidate-abc--attempt-01",
        candidate_id="candidate-abc",
        attempt=1,
        prompt="Review this patch.",
        workspace=tmp_path / "repository",
        codex_home=tmp_path / "codex-home",
        isolated_home=tmp_path / "home",
    )

    generated = build_review_promptfoo_config(config, trial)
    provider = generated["providers"][0]["config"]

    assert provider["sandbox_mode"] == "read-only"
    assert provider["network_access_enabled"] is False
    assert provider["web_search_enabled"] is False
    assert provider["output_schema"]["additionalProperties"] is False
    assert "$defs" not in provider["output_schema"]
    assert '"$ref"' not in json.dumps(provider["output_schema"])
    assert "cli_config" not in provider
    assert generated["tests"][0]["vars"]["candidate_id"] == "candidate-abc"
    assert ".agents" not in str(generated)


def test_parses_promptfoo_usage_and_keeps_missing_cost_explicit() -> None:
    output = Path(__file__).parent / "fixtures" / "promptfoo-results.json"

    parsed = parse_promptfoo_results(output)

    assert parsed["task-a--1"].usage.input_tokens == 12
    assert parsed["task-a--1"].usage.cached_tokens == 3
    assert parsed["task-a--1"].usage.output_tokens == 4
    assert parsed["task-a--1"].usage.cost_usd is None
    assert parsed["task-a--1"].duration_seconds == 0.5


def test_parses_reviewer_output_without_interpreting_it(tmp_path: Path) -> None:
    output = tmp_path / "promptfoo-results.json"
    response = '{"verdict":"accept","findings":[]}'
    output.write_text(
        json.dumps(
            {
                "results": {
                    "results": [
                        {
                            "success": True,
                            "vars": {
                                "trial_id": "candidate-abc--attempt-01",
                                "task_id": "candidate-abc",
                            },
                            "response": {"output": response},
                        }
                    ]
                }
            }
        )
    )

    parsed = parse_promptfoo_results(output)["candidate-abc--attempt-01"]

    assert parsed.response_output == response


def test_parses_codex_sandbox_start_failure_as_provider_error(tmp_path: Path) -> None:
    output = tmp_path / "promptfoo-results.json"
    output.write_text(
        json.dumps(
            {
                "results": {
                    "results": [
                        {
                            "success": True,
                            "vars": {"trial_id": "task-a--1", "task_id": "task-a"},
                            "response": {
                                "error": (
                                    "Could not edit because bwrap: No permissions to create "
                                    "a new namespace"
                                )
                            },
                        }
                    ]
                }
            }
        )
    )

    parsed = parse_promptfoo_results(output)["task-a--1"]

    assert parsed.provider_succeeded is False
    assert parsed.error == "codex_sandbox_unavailable"
