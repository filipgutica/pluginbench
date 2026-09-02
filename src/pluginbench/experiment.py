from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from pluginbench.config import PluginbenchConfig
from pluginbench.datasets import TaskSpec, load_tasks
from pluginbench.promptfoo import (
    CODEX_SANDBOX_DOCKER_ARGS,
    CommandResult,
    PromptfooExecutionError,
    PromptfooRunner,
    PromptfooTrialResult,
    Trial,
    build_eval_argv,
    build_promptfoo_config,
    parse_promptfoo_results,
)
from pluginbench.reporting import build_report, write_reports
from pluginbench.results import (
    ArmName,
    ArmResult,
    TaskResult,
    Usage,
    canonical_fingerprint,
    fingerprint_differences,
    load_arm,
    save_arm,
)
from pluginbench.skills import SkillBundle, inspect_skill_bundle, stage_skill_bundle
from pluginbench.swebench import (
    build_eval_argv as build_swebench_eval_argv,
    materialize_repository,
    prepare_image,
    run_official_evaluation,
    validate_harness,
)


class Runner(Protocol):
    executable: Path

    def validate_version(self, expected: str) -> str: ...

    def validate_codex_sdk_version(self, expected: str) -> str: ...

    def validate_container_image(
        self, image: str, *, promptfoo_version: str, codex_sdk_version: str
    ) -> str: ...

    def execute(
        self,
        config_path: Path,
        output_path: Path,
        *,
        concurrency: int,
        timeout_seconds: float,
        container_image: str | None = None,
    ) -> CommandResult: ...


def _sum_complete_int(values: list[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _sum_complete_float(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _new_arms(config: PluginbenchConfig) -> list[ArmName]:
    return ["baseline", "treatment"] if config.execution.baseline.mode == "run" else ["treatment"]


def _task_batches(config: PluginbenchConfig, tasks: list[TaskSpec]) -> list[list[TaskSpec]]:
    size = config.execution.batch_size
    return [tasks[index : index + size] for index in range(0, len(tasks), size)]


def _dry_run_promptfoo_argv(
    *,
    config: PluginbenchConfig,
    promptfoo_executable: Path,
    arm: ArmName,
    batch_number: int,
    tasks: list[TaskSpec],
    attempts: tuple[int, ...] | None = None,
) -> list[str]:
    batch = f"<run>/{arm}/batches/{batch_number:04d}"
    config_path = f"{batch}/promptfooconfig.yaml"
    output_path = f"{batch}/promptfoo-results.json"
    if config.tooling.container_image is None:
        return list(
            build_eval_argv(
                promptfoo_executable,
                Path(config_path),
                Path(output_path),
                concurrency=config.execution.concurrency,
            )
        )
    selected_attempts = attempts or tuple(range(1, config.execution.attempts + 1))
    trial_roots = [
        (
            f"<run>/{arm}/workspaces/{_slug(task.task_id)}/attempt-{attempt:02d}",
            f"<temporary-auth>/{arm}/batch-{batch_number:04d}/"
            f"{_slug(task.task_id)}--attempt-{attempt:02d}",
        )
        for task in tasks
        for attempt in selected_attempts
    ]
    mounted_paths = [batch]
    mounted_paths.extend(workspace for workspace, _ in trial_roots)
    mounted_paths.extend(f"{auth}/codex-home" for _, auth in trial_roots)
    mounted_paths.extend(f"{auth}/home" for _, auth in trial_roots)
    mounts = [
        argument
        for path in mounted_paths
        for argument in ("--mount", f"type=bind,src={path},dst={path}")
    ]
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        *CODEX_SANDBOX_DOCKER_ARGS,
        "--workdir",
        batch,
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
        f"PROMPTFOO_EVAL_TIMEOUT_MS={int(config.execution.timeout_seconds * 1000)}",
        config.tooling.container_image,
        "eval",
        "--config",
        config_path,
        "--output",
        output_path,
        "--no-cache",
        "--no-share",
        "--no-write",
        "--no-table",
        "--no-progress-bar",
        "--max-concurrency",
        str(config.execution.concurrency),
    ]


def compatibility_payload(
    config: PluginbenchConfig,
    tasks: list[TaskSpec],
    *,
    promptfoo_version: str,
    runtime_image_id: str | None = None,
) -> dict[str, Any]:
    agent = config.agent.model_dump(mode="json")
    agent.pop("auth_file", None)
    harness = config.dataset.harness
    return {
        "schema_version": 1,
        "engine": {
            "name": "promptfoo",
            "version": promptfoo_version,
        },
        "runtime": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "container_image": config.tooling.container_image,
            "container_image_id": runtime_image_id,
            "container_sandbox": "codex-bwrap",
            "docker_security_options": list(CODEX_SANDBOX_DOCKER_ARGS),
        },
        "dataset": {
            "adapter": config.dataset.adapter,
            "name": config.dataset.name,
            "version": config.dataset.version,
            "task_ids": config.dataset.task_ids,
            "task_digests": {task.task_id: task.digest for task in tasks},
            "harness": (
                {
                    "version": harness.version,
                    "image_platform": harness.image_platform,
                }
                if harness is not None
                else None
            ),
        },
        "agent": agent,
        "trial": {
            "attempts": config.execution.attempts,
            "concurrency": config.execution.concurrency,
            "batch_size": config.execution.batch_size,
            "timeout_seconds": config.execution.timeout_seconds,
        },
    }


def validate_baseline_compatibility(
    baseline: ArmResult, *, expected_payload: dict[str, Any]
) -> None:
    if baseline.arm != "baseline":
        raise ValueError("baseline reuse requires a baseline arm")
    expected_fingerprint = canonical_fingerprint(expected_payload)
    if (
        baseline.compatibility_fingerprint != expected_fingerprint
        or baseline.compatibility_payload != expected_payload
    ):
        fields = fingerprint_differences(expected_payload, baseline.compatibility_payload)
        changed = ", ".join(fields) if fields else "compatibility_fingerprint"
        raise ValueError(f"incompatible baseline; changed compatibility fields: {changed}")
    dataset = expected_payload["dataset"]
    expected_tasks = set(dataset["task_ids"])
    if baseline.tasks.keys() != expected_tasks:
        raise ValueError("incompatible baseline; baseline task set is incomplete or different")
    if baseline.task_checksums != dataset["task_digests"]:
        raise ValueError("incompatible baseline; task content digests differ")


def build_dry_run(
    config: PluginbenchConfig,
    bundle: SkillBundle,
    tasks: list[TaskSpec],
    promptfoo_executable: Path,
    promptfoo_version: str,
    runtime_image_id: str | None = None,
) -> dict[str, Any]:
    _ensure_unique_task_slugs(tasks)
    arms = _new_arms(config)
    trial_count = len(tasks) * config.execution.attempts * len(arms)
    possible_cost = (
        trial_count * config.estimates.cost_per_trial_usd
        if config.estimates.cost_per_trial_usd is not None
        else None
    )
    possible_tokens = (
        trial_count * config.estimates.tokens_per_trial
        if config.estimates.tokens_per_trial is not None
        else None
    )
    if config.tooling.container_image is None:
        commands = [
            _dry_run_promptfoo_argv(
                config=config,
                promptfoo_executable=promptfoo_executable,
                arm=arm,
                batch_number=batch_number,
                tasks=batch,
            )
            for batch_number, batch in enumerate(_task_batches(config, tasks), start=1)
            for arm in arms
        ]
    else:
        commands = [
            _dry_run_promptfoo_argv(
                config=config,
                promptfoo_executable=promptfoo_executable,
                arm=arm,
                batch_number=batch_number,
                tasks=[task],
                attempts=(attempt,),
            )
            for batch_number, batch in enumerate(_task_batches(config, tasks), start=1)
            for arm in arms
            for task in batch
            for attempt in range(1, config.execution.attempts + 1)
        ]
    benchmark_commands = [
        list(
            build_swebench_eval_argv(
                executable=task.swebench.harness_executable,
                dataset_path=Path(
                    f"<run>/{arm}/verifiers/{_slug(task.task_id)}/attempt-{attempt:02d}/dataset.json"
                ),
                predictions_path=Path(
                    f"<run>/{arm}/verifiers/{_slug(task.task_id)}/attempt-{attempt:02d}/predictions.jsonl"
                ),
                instance_id=task.task_id,
                run_id=f"{_slug(task.task_id)}--attempt-{attempt:02d}",
                timeout_seconds=int(config.execution.timeout_seconds),
                report_dir=Path(
                    f"<run>/{arm}/verifiers/{_slug(task.task_id)}/attempt-{attempt:02d}"
                ),
            )
        )
        for arm in arms
        for task in tasks
        if task.swebench is not None
        for attempt in range(1, config.execution.attempts + 1)
    ]
    benchmark_versions = {
        task.swebench.harness_version for task in tasks if task.swebench is not None
    }
    return {
        "engine": "promptfoo",
        "promptfoo_version": promptfoo_version,
        "runtime_container_image": config.tooling.container_image,
        "runtime_container_image_id": runtime_image_id,
        "tasks": [task.task_id for task in tasks],
        "treatments": arms,
        "baseline_mode": config.execution.baseline.mode,
        "reused_baseline": (
            str(config.execution.baseline.result)
            if config.execution.baseline.result is not None
            else None
        ),
        "skill": {
            "source_path": str(bundle.source_path),
            "names": list(bundle.skill_names),
            "digest": bundle.digest,
        },
        "estimated_trial_count": trial_count,
        "maximum_concurrent_trials": config.execution.concurrency,
        "possible_token_exposure": possible_tokens,
        "possible_cost_exposure_usd": possible_cost,
        "limits": config.limits.model_dump(mode="json"),
        "commands": commands,
        "benchmark_engine": (
            f"swebench=={next(iter(benchmark_versions))}" if benchmark_versions else None
        ),
        "benchmark_commands": benchmark_commands,
        "external_dataset_adapters": {
            "swe-bench": "supported through the pinned official CLI and Docker evaluator",
            "live-swe-bench": "planned; requires the official prepare/test harness",
        },
        "warning": (
            "Promptfoo does not enforce an aggregate token or cost stop for an active eval. "
            "pluginbench checks best-effort limits between bounded batches, so one active "
            "batch can exceed a limit."
        ),
    }


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "task"
    if cleaned == value:
        return cleaned
    suffix = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{cleaned}-{suffix}"


def _ensure_unique_task_slugs(tasks: list[TaskSpec]) -> None:
    by_slug: dict[str, str] = {}
    for task in tasks:
        slug = _slug(task.task_id)
        existing = by_slug.get(slug)
        if existing is not None and existing != task.task_id:
            raise ValueError(
                f"task IDs produce the same artifact path: {existing!r} and {task.task_id!r}"
            )
        by_slug[slug] = task.task_id


def _prepare_trial(
    *,
    run_dir: Path,
    auth_root: Path,
    auth_file: Path,
    arm: ArmName,
    task: TaskSpec,
    attempt: int,
    bundle: SkillBundle,
    source: Path,
    verifier_script: Path | None,
    model_name: str,
    agent_timeout_seconds: float,
    verifier_timeout_seconds: int,
) -> Trial:
    workspace = run_dir / arm / "workspaces" / _slug(task.task_id) / f"attempt-{attempt:02d}"
    shutil.copytree(source, workspace)
    if arm == "treatment":
        stage_skill_bundle(bundle, workspace)
    trial_key = f"{_slug(task.task_id)}--attempt-{attempt:02d}"
    codex_home = auth_root / trial_key / "codex-home"
    isolated_home = auth_root / trial_key / "home"
    codex_home.mkdir(parents=True)
    isolated_home.mkdir(parents=True)
    copied_auth = codex_home / "auth.json"
    shutil.copy2(auth_file, copied_auth)
    copied_auth.chmod(0o600)
    trial_task = task
    if task.verifier is not None:
        if verifier_script is None:
            raise ValueError(f"snapshotted verifier is missing for {task.task_id}")
        trial_task = replace(
            task,
            verifier=replace(
                task.verifier,
                command=(*task.verifier.command[:-1], str(verifier_script)),
                script=verifier_script,
            ),
        )
    return Trial(
        trial_id=trial_key,
        task=trial_task,
        attempt=attempt,
        workspace=workspace,
        codex_home=codex_home,
        isolated_home=isolated_home,
        agent_timeout_seconds=agent_timeout_seconds,
        verifier_dir=(run_dir / arm / "verifiers" / _slug(task.task_id) / f"attempt-{attempt:02d}"),
        model_name=model_name,
        verifier_timeout_seconds=verifier_timeout_seconds,
    )


def _verifier_environment() -> dict[str, str]:
    allowed = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _run_verifier(trial: Trial) -> tuple[float | None, float | None, str | None]:
    started = time.monotonic()
    if trial.task.swebench is not None:
        if trial.verifier_dir is None:
            return None, time.monotonic() - started, "swebench_verifier_directory_missing"
        try:
            score, error = run_official_evaluation(
                spec=trial.task.swebench,
                instance_id=trial.task.task_id,
                workspace=trial.workspace,
                artifact_dir=trial.verifier_dir,
                run_id=trial.trial_id,
                model_name=trial.model_name,
                timeout_seconds=trial.verifier_timeout_seconds,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return None, time.monotonic() - started, f"swebench_verifier_error:{type(exc).__name__}"
        return score, time.monotonic() - started, error
    verifier = trial.task.verifier
    if verifier is None:
        return None, time.monotonic() - started, "verifier_missing"
    try:
        result = subprocess.run(
            verifier.command,
            cwd=trial.workspace,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=verifier.timeout_seconds,
            env=_verifier_environment(),
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - started, "verifier_timeout"
    except OSError as exc:
        return None, time.monotonic() - started, f"verifier_error:{type(exc).__name__}"
    return (1.0 if result.returncode == 0 else 0.0), time.monotonic() - started, None


def _combine_duration(agent: float | None, verifier: float | None) -> float | None:
    if agent is None or verifier is None:
        return None
    return agent + verifier


def _task_results(
    trials: list[Trial], parsed: dict[str, PromptfooTrialResult]
) -> dict[str, TaskResult]:
    expected = {trial.trial_id: trial for trial in trials}
    unexpected = sorted(parsed.keys() - expected.keys())
    if unexpected:
        raise ValueError("Promptfoo output contains unexpected trial IDs: " + ", ".join(unexpected))
    mismatched = [
        trial.trial_id
        for trial in trials
        if (provider := parsed.get(trial.trial_id)) is not None
        and provider.task_id != trial.task.task_id
    ]
    if mismatched:
        raise ValueError("Promptfoo result task ID mismatch for trials: " + ", ".join(mismatched))

    attempts: dict[
        str, list[tuple[PromptfooTrialResult | None, float | None, float | None, str | None]]
    ] = defaultdict(list)
    for trial in trials:
        provider = parsed.get(trial.trial_id)
        if provider is None:
            attempts[trial.task.task_id].append((None, None, None, "missing_promptfoo_result"))
            continue
        if (
            provider.duration_seconds is not None
            and provider.duration_seconds > trial.agent_timeout_seconds
        ):
            attempts[trial.task.task_id].append(
                (provider, None, None, "promptfoo_reported_timeout_exceeded")
            )
            continue
        if not provider.provider_succeeded:
            suffix = f":{provider.error}" if provider.error is not None else ""
            attempts[trial.task.task_id].append(
                (provider, None, None, f"promptfoo_provider_error{suffix}")
            )
            continue
        score, verifier_duration, verifier_error = _run_verifier(trial)
        attempts[trial.task.task_id].append((provider, score, verifier_duration, verifier_error))

    results: dict[str, TaskResult] = {}
    for task_id, rows in attempts.items():
        providers = [provider for provider, _, _, _ in rows if provider is not None]
        usages = [provider.usage if provider is not None else Usage() for provider, _, _, _ in rows]
        scores = [score for _, score, _, _ in rows if score is not None]
        errors = [error for _, _, _, error in rows if error is not None]
        durations = [
            _combine_duration(provider.duration_seconds, verifier_duration)
            if provider is not None
            else None
            for provider, _, verifier_duration, _ in rows
        ]
        skill_calls = sorted({call for provider in providers for call in provider.skill_calls})
        results[task_id] = TaskResult(
            task_id=task_id,
            attempts=len(rows),
            score=sum(scores) / len(scores) if scores else None,
            passed=any(score >= 1 for score in scores),
            duration_seconds=_sum_complete_float(durations),
            infrastructure_errors=errors,
            skill_calls=skill_calls,
            usage=Usage(
                input_tokens=_sum_complete_int([usage.input_tokens for usage in usages]),
                cached_tokens=_sum_complete_int([usage.cached_tokens for usage in usages]),
                output_tokens=_sum_complete_int([usage.output_tokens for usage in usages]),
                cost_usd=_sum_complete_float([usage.cost_usd for usage in usages]),
            ),
        )
    return results


def _snapshot_task_inputs(
    config: PluginbenchConfig, tasks: list[TaskSpec], inputs_dir: Path
) -> tuple[dict[str, Path], dict[str, Path]]:
    if config.dataset.adapter == "local":
        shutil.copy2(Path(config.dataset.source), inputs_dir / "task-catalog.yaml")
    else:
        selected_rows = [task.swebench.row for task in tasks if task.swebench is not None]
        (inputs_dir / "swebench-dataset.json").write_text(
            json.dumps(selected_rows, indent=2) + "\n"
        )
    tasks_dir = inputs_dir / "tasks"
    tasks_dir.mkdir()
    sources: dict[str, Path] = {}
    verifiers: dict[str, Path] = {}
    for task in tasks:
        task_dir = tasks_dir / _slug(task.task_id)
        task_dir.mkdir()
        if task.swebench is not None:
            repository = task_dir / "repository"
            materialize_repository(task.swebench, repository)
            sources[task.task_id] = repository
            metadata = {
                "task_id": task.task_id,
                "prompt": task.prompt,
                "digest": task.digest,
                "benchmark": {
                    "adapter": "swe-bench",
                    "dataset_name": task.swebench.dataset_name,
                    "dataset_version": task.swebench.dataset_version,
                    "repo": task.swebench.repo,
                    "base_commit": task.swebench.base_commit,
                    "image": task.swebench.image,
                    "harness_version": task.swebench.harness_version,
                    "image_platform": task.swebench.image_platform,
                },
            }
            (task_dir / "task.json").write_text(json.dumps(metadata, indent=2) + "\n")
            continue
        assert task.fixture is not None
        assert task.verifier is not None
        shutil.copytree(task.fixture, task_dir / "fixture")
        sources[task.task_id] = task_dir / "fixture"
        verifier_name = f"verifier{task.verifier.script.suffix}"
        shutil.copy2(task.verifier.script, task_dir / verifier_name)
        verifiers[task.task_id] = task_dir / verifier_name
        verifier_command = [*task.verifier.command[:-1], verifier_name]
        metadata = {
            "task_id": task.task_id,
            "prompt": task.prompt,
            "digest": task.digest,
            "verifier": {
                "command": verifier_command,
                "timeout_seconds": task.verifier.timeout_seconds,
            },
        }
        (task_dir / "task.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return sources, verifiers


def _execute_batch(
    *,
    config: PluginbenchConfig,
    bundle: SkillBundle,
    runner: Runner,
    run_dir: Path,
    auth_root: Path,
    arm: ArmName,
    tasks: list[TaskSpec],
    batch_number: int,
    task_sources: dict[str, Path],
    verifier_sources: dict[str, Path],
    execution_image: str | None,
) -> dict[str, TaskResult]:
    trials = [
        _prepare_trial(
            run_dir=run_dir,
            auth_root=auth_root / arm / f"batch-{batch_number:04d}",
            auth_file=config.agent.auth_file,
            arm=arm,
            task=task,
            attempt=attempt,
            bundle=bundle,
            source=task_sources[task.task_id],
            verifier_script=verifier_sources.get(task.task_id),
            model_name=config.agent.model,
            agent_timeout_seconds=config.execution.timeout_seconds,
            verifier_timeout_seconds=int(config.execution.timeout_seconds),
        )
        for task in tasks
        for attempt in range(1, config.execution.attempts + 1)
    ]
    batch_dir = run_dir / arm / "batches" / f"{batch_number:04d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    trial_groups = (
        [[trial] for trial in trials] if config.tooling.container_image is not None else [trials]
    )
    parsed: dict[str, PromptfooTrialResult] = {}
    for group in trial_groups:
        artifact_dir = (
            batch_dir if len(trial_groups) == 1 else batch_dir / "trials" / group[0].trial_id
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        promptfoo_config = artifact_dir / "promptfooconfig.yaml"
        promptfoo_output = artifact_dir / "promptfoo-results.json"
        promptfoo_config.write_text(
            yaml.safe_dump(build_promptfoo_config(config, group), sort_keys=False)
        )
        try:
            command_timeout = config.execution.timeout_seconds * math.ceil(
                len(group) / config.execution.concurrency
            )
            command = runner.execute(
                promptfoo_config,
                promptfoo_output,
                concurrency=config.execution.concurrency,
                timeout_seconds=command_timeout,
                container_image=execution_image,
            )
        except PromptfooExecutionError as exc:
            command = exc.command
            (artifact_dir / "command.json").write_text(
                json.dumps(list(command.argv), indent=2) + "\n"
            )
            (artifact_dir / "stdout.log").write_text(command.stdout)
            (artifact_dir / "stderr.log").write_text(command.stderr)
            raise
        (artifact_dir / "command.json").write_text(json.dumps(list(command.argv), indent=2) + "\n")
        (artifact_dir / "stdout.log").write_text(command.stdout)
        (artifact_dir / "stderr.log").write_text(command.stderr)
        group_results = parse_promptfoo_results(promptfoo_output)
        duplicate_ids = parsed.keys() & group_results.keys()
        if duplicate_ids:
            raise ValueError(
                "Promptfoo output contains duplicate trial IDs: " + ", ".join(sorted(duplicate_ids))
            )
        parsed.update(group_results)
    return _task_results(trials, parsed)


def _limit_reason(config: PluginbenchConfig, tasks: list[TaskResult]) -> str | None:
    if not tasks:
        return None
    usages = [task.usage for task in tasks]
    if config.limits.max_total_tokens is not None:
        totals = [
            usage.input_tokens + usage.output_tokens
            if usage.input_tokens is not None and usage.output_tokens is not None
            else None
            for usage in usages
        ]
        total = _sum_complete_int(totals)
        if total is None:
            return "token usage unavailable; cannot safely schedule another batch"
        if total >= config.limits.max_total_tokens:
            return f"best-effort token limit reached ({config.limits.max_total_tokens})"
    if config.limits.max_total_cost_usd is not None:
        total_cost = _sum_complete_float([usage.cost_usd for usage in usages])
        if total_cost is None:
            return "cost usage unavailable; cannot safely schedule another batch"
        if total_cost >= config.limits.max_total_cost_usd:
            return f"best-effort cost limit reached ({config.limits.max_total_cost_usd})"
    return None


def _arm_result(
    *,
    arm: ArmName,
    tasks: dict[str, TaskResult],
    compatibility: dict[str, Any],
    config: PluginbenchConfig,
    bundle: SkillBundle,
    started_at: datetime,
    finished_at: datetime,
) -> ArmResult:
    return ArmResult.from_tasks(
        arm=arm,
        compatibility_fingerprint=canonical_fingerprint(compatibility),
        compatibility_payload=compatibility,
        tasks=tasks,
        task_checksums={task: compatibility["dataset"]["task_digests"][task] for task in tasks},
        promptfoo_version=config.tooling.promptfoo_version,
        agent_versions=[config.agent.codex_sdk_version],
        model_identifiers=[config.agent.model],
        skill_digest=bundle.digest if arm == "treatment" else None,
        skill_source_path=bundle.source_path if arm == "treatment" else None,
        started_at=started_at,
        finished_at=finished_at,
        provenance={
            "engine": "promptfoo",
            "promptfoo_version": config.tooling.promptfoo_version,
            "codex_sdk_version": config.agent.codex_sdk_version,
            "model": config.agent.model,
            "runtime_image_id": compatibility["runtime"]["container_image_id"],
            "resolved_configuration": config.model_dump(mode="json"),
        },
    )


def _decision_kwargs(config: PluginbenchConfig) -> dict[str, Any]:
    return {
        "minimum_pass_rate_lift_pp": config.decision.minimum_pass_rate_lift_pp,
        "maximum_cost_overhead_pct": config.decision.maximum_cost_overhead_pct,
        "maximum_incremental_cost_per_additional_success_usd": (
            config.decision.maximum_incremental_cost_per_additional_success_usd
        ),
    }


def run_evaluation(
    config: PluginbenchConfig,
    bundle: SkillBundle,
    runner: Runner | PromptfooRunner,
    *,
    run_dir: Path | None = None,
    runtime_image_id: str | None = None,
) -> Path:
    if not config.agent.auth_file.is_file():
        raise ValueError(f"Codex auth file does not exist: {config.agent.auth_file}")
    tasks = load_tasks(config.dataset)
    _ensure_unique_task_slugs(tasks)
    version = runner.validate_version(config.tooling.promptfoo_version)
    runner.validate_codex_sdk_version(config.agent.codex_sdk_version)
    if config.tooling.container_image is None:
        runtime_image_id = None
    elif runtime_image_id is None:
        runtime_image_id = runner.validate_container_image(
            config.tooling.container_image,
            promptfoo_version=version,
            codex_sdk_version=config.agent.codex_sdk_version,
        )
    compatibility = compatibility_payload(
        config,
        tasks,
        promptfoo_version=version,
        runtime_image_id=runtime_image_id,
    )
    swebench_specs = [task.swebench for task in tasks if task.swebench is not None]
    for spec in swebench_specs:
        validate_harness(spec)
        prepare_image(spec)
    baseline: ArmResult | None = None
    if config.execution.baseline.mode == "reuse":
        assert config.execution.baseline.result is not None
        baseline = load_arm(config.execution.baseline.result)
        validate_baseline_compatibility(baseline, expected_payload=compatibility)

    destination = run_dir or (
        config.output.runs_dir
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{_slug(config.experiment.name)}"
    )
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "inputs").mkdir()
    shutil.copytree(bundle.source_path, destination / "inputs" / "skill-bundle")
    execution_bundle = inspect_skill_bundle(destination / "inputs" / "skill-bundle")
    if (
        execution_bundle.digest != bundle.digest
        or inspect_skill_bundle(bundle.source_path).digest != bundle.digest
    ):
        raise ValueError("skill bundle changed while run inputs were being snapshotted")
    task_sources, verifier_sources = _snapshot_task_inputs(config, tasks, destination / "inputs")
    refreshed_tasks = load_tasks(config.dataset)
    if {task.task_id: task.digest for task in refreshed_tasks} != {
        task.task_id: task.digest for task in tasks
    }:
        raise ValueError("task inputs changed while run inputs were being snapshotted")
    (destination / "resolved-config.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    started_at = datetime.now(UTC)
    collected: dict[ArmName, dict[str, TaskResult]] = {arm: {} for arm in _new_arms(config)}
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix=".pluginbench-auth-", dir=destination.parent
    ) as auth_temp:
        auth_root = Path(auth_temp)
        for batch_number, batch in enumerate(_task_batches(config, tasks), start=1):
            for arm in _new_arms(config):
                collected[arm].update(
                    _execute_batch(
                        config=config,
                        bundle=execution_bundle,
                        runner=runner,
                        run_dir=destination,
                        auth_root=auth_root,
                        arm=arm,
                        tasks=batch,
                        batch_number=batch_number,
                        task_sources=task_sources,
                        verifier_sources=verifier_sources,
                        execution_image=runtime_image_id,
                    )
                )
            new_results = [
                result for arm_results in collected.values() for result in arm_results.values()
            ]
            reason = _limit_reason(config, new_results)
            if reason is not None and batch_number < len(_task_batches(config, tasks)):
                warnings.append(reason)
                break

    finished_at = datetime.now(UTC)
    for arm, task_results in collected.items():
        result = _arm_result(
            arm=arm,
            tasks=task_results,
            compatibility=compatibility,
            config=config,
            bundle=bundle,
            started_at=started_at,
            finished_at=finished_at,
        )
        arm_dir = destination / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        save_arm(arm_dir / "arm.json", result)
        if arm == "baseline":
            baseline = result

    treatment = load_arm(destination / "treatment")
    comparison_baseline = (
        baseline
        if baseline is not None and baseline.tasks.keys() == treatment.tasks.keys()
        else None
    )
    report = build_report(comparison_baseline, treatment, **_decision_kwargs(config))
    write_reports(destination, report)
    manifest = {
        "schema_version": 1,
        "status": "stopped_by_limit" if warnings else "complete",
        "engine": "promptfoo",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "compatibility_fingerprint": canonical_fingerprint(compatibility),
        "warnings": warnings,
        "reused_baseline": (
            str(config.execution.baseline.result)
            if config.execution.baseline.mode == "reuse"
            else None
        ),
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return destination
