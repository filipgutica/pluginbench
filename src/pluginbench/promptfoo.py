from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from pluginbench.config import PluginbenchConfig
from pluginbench.datasets import TaskSpec
from pluginbench.results import Usage
from pluginbench.review_models import ReviewEvaluationConfig, ReviewResponse


CODEX_SANDBOX_DOCKER_ARGS = (
    "--cap-add",
    "SYS_ADMIN",
    "--security-opt",
    "apparmor=unconfined",
    "--security-opt",
    "seccomp=unconfined",
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class PromptfooExecutionError(ValueError):
    def __init__(self, message: str, command: CommandResult) -> None:
        super().__init__(message)
        self.command = command


@dataclass(frozen=True)
class Trial:
    trial_id: str
    task: TaskSpec
    attempt: int
    workspace: Path
    codex_home: Path
    isolated_home: Path
    agent_timeout_seconds: float = 600
    verifier_dir: Path | None = None
    model_name: str = "pluginbench"
    verifier_timeout_seconds: int = 1800


@dataclass(frozen=True)
class ReviewTrial:
    trial_id: str
    candidate_id: str
    attempt: int
    prompt: str
    workspace: Path
    codex_home: Path
    isolated_home: Path


@dataclass(frozen=True)
class PromptfooTrialResult:
    trial_id: str
    task_id: str
    provider_succeeded: bool
    duration_seconds: float | None
    usage: Usage
    error: str | None
    skill_calls: tuple[str, ...]
    response_output: str | None = None


def build_eval_argv(
    executable: Path,
    config_path: Path,
    output_path: Path,
    *,
    concurrency: int,
) -> tuple[str, ...]:
    return (
        str(executable),
        "eval",
        "--config",
        str(config_path),
        "--output",
        str(output_path),
        "--no-cache",
        "--no-share",
        "--no-write",
        "--no-table",
        "--no-progress-bar",
        "--max-concurrency",
        str(concurrency),
    )


def _bind_mount(path: Path) -> str:
    absolute = path.absolute()
    if "," in str(absolute):
        raise ValueError(f"Docker bind path must not contain a comma: {absolute}")
    return f"type=bind,src={absolute},dst={absolute}"


def build_container_eval_argv(
    *,
    image: str,
    config_path: Path,
    output_path: Path,
    workspaces: tuple[Path, ...],
    codex_homes: tuple[Path, ...],
    isolated_homes: tuple[Path, ...],
    concurrency: int,
    timeout_seconds: float,
) -> tuple[str, ...]:
    paths = (config_path.parent, *workspaces, *codex_homes, *isolated_homes)
    mounts = tuple(argument for path in paths for argument in ("--mount", _bind_mount(path)))
    eval_argv = build_eval_argv(
        Path("/app/node_modules/.bin/promptfoo"),
        config_path.resolve(),
        output_path.resolve(),
        concurrency=concurrency,
    )[1:]
    return (
        "docker",
        "run",
        "--rm",
        "--init",
        *CODEX_SANDBOX_DOCKER_ARGS,
        "--workdir",
        str(config_path.parent.resolve()),
        *mounts,
        "--env",
        "PROMPTFOO_DISABLE_REMOTE_GENERATION=true",
        "--env",
        "PROMPTFOO_DISABLE_SHARING=true",
        "--env",
        "PROMPTFOO_DISABLE_TELEMETRY=true",
        "--env",
        "PROMPTFOO_DISABLE_UPDATE=true",
        "--env",
        f"PROMPTFOO_EVAL_TIMEOUT_MS={int(timeout_seconds * 1000)}",
        image,
        *eval_argv,
    )


def _trial_mounts(config_path: Path) -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
    try:
        loaded = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read Promptfoo config for container mounts: {exc}") from exc
    tests = loaded.get("tests") if isinstance(loaded, dict) else None
    if not isinstance(tests, list) or not tests:
        raise ValueError("Promptfoo config does not contain trial tests")
    variables: list[dict[str, Any]] = []
    for test in tests:
        item = test.get("vars") if isinstance(test, dict) else None
        if not isinstance(item, dict):
            raise ValueError("Promptfoo trial does not contain variables")
        variables.append(item)
    return (
        tuple(Path(item["workspace_dir"]) for item in variables),
        tuple(Path(item["codex_home"]) for item in variables),
        tuple(Path(item["isolated_home"]) for item in variables),
    )


def build_promptfoo_config(config: PluginbenchConfig, trials: list[Trial]) -> dict[str, Any]:
    provider_config: dict[str, Any] = {
        "model": config.agent.model,
        "model_reasoning_effort": config.agent.reasoning,
        "working_dir": "{{workspace_dir}}",
        "skip_git_repo_check": True,
        "sandbox_mode": config.agent.sandbox_mode,
        "approval_policy": config.agent.approval_policy,
        "network_access_enabled": config.agent.network_access_enabled,
        "web_search_enabled": config.agent.web_search_enabled,
        "enable_streaming": True,
        "inherit_process_env": False,
        "cli_env": {
            "CODEX_HOME": "{{codex_home}}",
            "HOME": "{{isolated_home}}",
        },
    }
    return {
        "description": f"pluginbench: {config.experiment.name}",
        "prompts": ["{{prompt}}"],
        "providers": [{"id": config.agent.provider, "config": provider_config}],
        "tests": [
            {
                "description": trial.trial_id,
                "vars": {
                    "trial_id": trial.trial_id,
                    "task_id": trial.task.task_id,
                    "attempt": str(trial.attempt),
                    "prompt": trial.task.prompt,
                    "workspace_dir": str(trial.workspace),
                    "codex_home": str(trial.codex_home),
                    "isolated_home": str(trial.isolated_home),
                },
                "metadata": {
                    "pluginbench_task_id": trial.task.task_id,
                    "pluginbench_attempt": trial.attempt,
                },
                "options": {"bustCache": True},
            }
            for trial in trials
        ],
    }


def _inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return schema

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"review output schema has an unresolved reference: {reference}")
            return inline({**target, **{key: item for key, item in value.items() if key != "$ref"}})
        return {key: inline(item) for key, item in value.items() if key != "$defs"}

    inlined = inline(schema)
    if not isinstance(inlined, dict):
        raise ValueError("review output schema must be an object")
    return inlined


def build_review_promptfoo_config(
    config: ReviewEvaluationConfig, trial: ReviewTrial
) -> dict[str, Any]:
    provider_config: dict[str, Any] = {
        "model": config.reviewer.model,
        "model_reasoning_effort": config.reviewer.reasoning,
        "working_dir": "{{workspace_dir}}",
        "skip_git_repo_check": True,
        "sandbox_mode": "read-only",
        "approval_policy": "never",
        "network_access_enabled": False,
        "web_search_enabled": False,
        "output_schema": _inline_local_schema_refs(ReviewResponse.model_json_schema()),
        "enable_streaming": True,
        "inherit_process_env": False,
        "cli_env": {
            "CODEX_HOME": "{{codex_home}}",
            "HOME": "{{isolated_home}}",
        },
    }
    return {
        "description": "pluginbench blinded review",
        "prompts": ["{{prompt}}"],
        "providers": [{"id": config.reviewer.provider, "config": provider_config}],
        "tests": [
            {
                "description": trial.trial_id,
                "vars": {
                    "trial_id": trial.trial_id,
                    "task_id": trial.candidate_id,
                    "candidate_id": trial.candidate_id,
                    "attempt": str(trial.attempt),
                    "prompt": trial.prompt,
                    "workspace_dir": str(trial.workspace),
                    "codex_home": str(trial.codex_home),
                    "isolated_home": str(trial.isolated_home),
                },
                "metadata": {
                    "pluginbench_candidate_id": trial.candidate_id,
                    "pluginbench_attempt": trial.attempt,
                },
                "options": {"bustCache": True},
            }
        ],
    }


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _skill_calls(response: dict[str, Any]) -> tuple[str, ...]:
    metadata = response.get("metadata")
    calls = metadata.get("skillCalls") if isinstance(metadata, dict) else None
    if not isinstance(calls, list):
        return ()
    names = [call.get("name") for call in calls if isinstance(call, dict)]
    return tuple(name for name in names if isinstance(name, str))


def _codex_sandbox_error(response: dict[str, Any]) -> str | None:
    messages = [response.get("output"), response.get("error")]
    detail = "\n".join(message for message in messages if isinstance(message, str))
    if "bwrap:" in detail and (
        "Operation not permitted" in detail
        or "No permissions to create a new namespace" in detail
        or "sandbox currently fails to start" in detail
    ):
        return "codex_sandbox_unavailable"
    return None


def parse_promptfoo_results(path: Path) -> dict[str, PromptfooTrialResult]:
    try:
        envelope = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Promptfoo results {path}: {exc}") from exc
    summary = envelope.get("results") if isinstance(envelope, dict) else None
    rows = summary.get("results") if isinstance(summary, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Promptfoo output does not contain results.results")
    parsed: dict[str, PromptfooTrialResult] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Promptfoo result row is not an object")
        variables = row.get("vars")
        if not isinstance(variables, dict):
            test_case = row.get("testCase")
            variables = test_case.get("vars") if isinstance(test_case, dict) else None
        trial_id = variables.get("trial_id") if isinstance(variables, dict) else None
        task_id = variables.get("task_id") if isinstance(variables, dict) else None
        if not isinstance(trial_id, str) or not isinstance(task_id, str):
            raise ValueError("Promptfoo result row lacks pluginbench trial variables")
        if trial_id in parsed:
            raise ValueError(f"Promptfoo output contains duplicate trial ID: {trial_id}")
        response = row.get("response")
        response = response if isinstance(response, dict) else {}
        token_usage = response.get("tokenUsage")
        token_usage = token_usage if isinstance(token_usage, dict) else {}
        latency_ms = _optional_float(row.get("latencyMs"))
        error = _codex_sandbox_error(response) or row.get("error") or response.get("error")
        metadata = response.get("metadata")
        if not error and isinstance(metadata, dict) and metadata.get("status") == "error":
            error = "Promptfoo provider returned error status"
        cost_value = row.get("cost") if "cost" in row else response.get("cost")
        output_value = response.get("output")
        response_output: str | None
        if isinstance(output_value, dict | list):
            response_output = json.dumps(output_value)
        else:
            response_output = output_value if isinstance(output_value, str) else None
        parsed[trial_id] = PromptfooTrialResult(
            trial_id=trial_id,
            task_id=task_id,
            provider_succeeded=bool(row.get("success")) and not error,
            duration_seconds=latency_ms / 1000 if latency_ms is not None else None,
            usage=Usage(
                input_tokens=_optional_int(token_usage.get("prompt")),
                cached_tokens=_optional_int(token_usage.get("cached")),
                output_tokens=_optional_int(token_usage.get("completion")),
                cost_usd=_optional_float(cost_value),
            ),
            error=str(error) if error else None,
            skill_calls=_skill_calls(response),
            response_output=response_output,
        )
    return parsed


def build_process_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    supplied = source if source is not None else os.environ
    allowed = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SYSTEMROOT",
        "WINDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    )
    environment = {key: supplied[key] for key in allowed if key in supplied}
    environment.update(
        {
            "PROMPTFOO_DISABLE_REMOTE_GENERATION": "true",
            "PROMPTFOO_DISABLE_SHARING": "true",
            "PROMPTFOO_DISABLE_TELEMETRY": "true",
            "PROMPTFOO_DISABLE_UPDATE": "true",
        }
    )
    return environment


class PromptfooRunner:
    def __init__(self, executable: Path | None = None) -> None:
        local = Path.cwd() / "node_modules" / ".bin" / "promptfoo"
        resolved = (
            executable
            or (local if local.is_file() else None)
            or (Path(found) if (found := shutil.which("promptfoo")) else None)
        )
        if resolved is None:
            raise ValueError(
                "promptfoo executable was not found; run npm install or pass --promptfoo-executable"
            )
        self.executable = resolved.expanduser().resolve()

    def validate_version(self, expected: str | None) -> str:
        try:
            result = subprocess.run(
                [str(self.executable), "--version"],
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=15,
                env=build_process_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"cannot run Promptfoo: {exc}") from exc
        output = f"{result.stdout}\n{result.stderr}".strip()
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
        if result.returncode != 0 or match is None:
            raise ValueError(f"cannot determine Promptfoo version: {output}")
        actual = match.group(1)
        if expected is not None and actual != expected:
            raise ValueError(f"Promptfoo version mismatch: expected {expected}, found {actual}")
        return actual

    def validate_codex_sdk_version(self, expected: str) -> str:
        promptfoo_package = next(
            (
                parent
                for parent in self.executable.parents
                if parent.name == "promptfoo" and parent.parent.name == "node_modules"
            ),
            None,
        )
        if promptfoo_package is None:
            raise ValueError("cannot locate Promptfoo's node_modules directory")
        package_path = promptfoo_package.parent / "@openai" / "codex-sdk" / "package.json"
        try:
            package = json.loads(package_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read the Codex SDK package version: {exc}") from exc
        actual = package.get("version") if isinstance(package, dict) else None
        if actual != expected:
            raise ValueError(f"Codex SDK version mismatch: expected {expected}, found {actual}")
        return expected

    def validate_container_image(
        self, image: str, *, promptfoo_version: str, codex_sdk_version: str
    ) -> str:
        commands = (
            ("docker", "image", "inspect", image, "--format", "{{.Id}}"),
            ("docker", "run", "--rm", image, "--version"),
            (
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "node",
                image,
                "-e",
                'console.log(require("/app/node_modules/@openai/codex-sdk/package.json").version)',
            ),
            (
                "docker",
                "run",
                "--rm",
                *CODEX_SANDBOX_DOCKER_ARGS,
                "--entrypoint",
                "sh",
                image,
                "-c",
                (
                    "sandbox=$(find /app/node_modules/@openai -path "
                    "'*/codex-resources/bwrap' -type f -print -quit); "
                    'test -n "$sandbox"; exec "$sandbox" --ro-bind / / '
                    "--proc /proc --dev /dev /bin/true"
                ),
            ),
        )
        outputs: list[str] = []
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    shell=False,
                    check=False,
                    timeout=30,
                    env=build_process_environment(),
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError(
                    f"Promptfoo container validation timed out after 30 seconds: {image}"
                ) from exc
            if result.returncode != 0:
                raise ValueError(
                    f"cannot validate Promptfoo container {image}: {result.stderr.strip()}"
                )
            outputs.append(result.stdout.strip())
        image_id, actual_promptfoo, actual_sdk, _ = outputs
        if actual_promptfoo != promptfoo_version:
            raise ValueError(
                f"container Promptfoo version mismatch: expected {promptfoo_version}, "
                f"found {actual_promptfoo}"
            )
        if actual_sdk != codex_sdk_version:
            raise ValueError(
                f"container Codex SDK version mismatch: expected {codex_sdk_version}, "
                f"found {actual_sdk}"
            )
        return image_id

    def execute(
        self,
        config_path: Path,
        output_path: Path,
        *,
        concurrency: int,
        timeout_seconds: float,
        container_image: str | None = None,
    ) -> CommandResult:
        if container_image is None:
            argv = build_eval_argv(
                self.executable,
                config_path,
                output_path,
                concurrency=concurrency,
            )
        else:
            workspaces, codex_homes, isolated_homes = _trial_mounts(config_path)
            argv = build_container_eval_argv(
                image=container_image,
                config_path=config_path,
                output_path=output_path,
                workspaces=workspaces,
                codex_homes=codex_homes,
                isolated_homes=isolated_homes,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
            )
        environment = build_process_environment()
        if container_image is None:
            environment["PROMPTFOO_EVAL_TIMEOUT_MS"] = str(int(timeout_seconds * 1000))
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                env=environment,
                timeout=timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            command = CommandResult(argv, -1, stdout, stderr)
            raise PromptfooExecutionError(
                f"Promptfoo exceeded the {timeout_seconds:g}s timeout", command
            ) from exc
        command = CommandResult(argv, result.returncode, result.stdout, result.stderr)
        if result.returncode != 0:
            raise PromptfooExecutionError(
                f"Promptfoo failed with exit code {result.returncode}: {result.stderr.strip()}",
                command,
            )
        if not output_path.is_file():
            raise PromptfooExecutionError(
                "Promptfoo completed without writing its JSON output", command
            )
        return command
