from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
import yaml

from pluginbench import __version__
from pluginbench.config import ConfigOverrides, PluginbenchConfig, load_config
from pluginbench.datasets import load_tasks
from pluginbench.experiment import build_dry_run, run_evaluation
from pluginbench.promptfoo import PromptfooRunner
from pluginbench.reporting import build_report, write_reports
from pluginbench.review import (
    build_gold_calibration_dry_run,
    build_review_dry_run,
    resolve_review_study,
    run_gold_calibration,
    run_review_evaluation,
)
from pluginbench.review_models import ReviewLimitsConfig, load_review_config
from pluginbench.review_reporting import regenerate_review_reports
from pluginbench.results import ArmResult, load_arm
from pluginbench.skills import inspect_skill_bundle
from pluginbench.swebench import validate_harness

app = typer.Typer(
    name="pluginbench",
    help="Evaluate local Codex skills with paired Promptfoo runs.",
    no_args_is_help=True,
    invoke_without_command=True,
)
review_app = typer.Typer(
    name="review",
    help="Run blinded reviewer-proxy evaluations over saved SWE-bench artifacts.",
    no_args_is_help=True,
)
app.add_typer(review_app, name="review")


def _runner(executable: Path | None) -> PromptfooRunner:
    return PromptfooRunner(executable)


def _show_error(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(2)


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", help="Show the version and exit.", is_eager=True
    ),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


def _node_version() -> tuple[bool, str]:
    node = shutil.which("node")
    if node is None:
        return False, "node executable was not found on PATH"
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "node exited unsuccessfully"
    value = result.stdout.strip().removeprefix("v")
    try:
        parts = tuple(int(part) for part in value.split(".")[:3])
    except ValueError:
        return False, f"unrecognized Node.js version: {value}"
    return parts >= (22, 22, 0), value


def _docker_version() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker executable was not found on PATH"
    try:
        result = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = result.stdout.strip() or result.stderr.strip()
    return result.returncode == 0, detail


@app.command()
def doctor(
    config: Path | None = typer.Argument(None, help="Optional evaluation YAML to validate."),
    promptfoo_executable: Path | None = typer.Option(
        None, "--promptfoo-executable", help="Path to the Promptfoo CLI."
    ),
) -> None:
    """Check Node.js, Promptfoo, Codex auth, skills, and task fixtures."""
    checks: list[tuple[str, bool, str]] = []
    node_ok, node_detail = _node_version()
    checks.append(("Node.js >=22.22.0", node_ok, node_detail))
    resolved: PluginbenchConfig | None = None
    if config is not None:
        try:
            resolved = load_config(config)
            bundle = inspect_skill_bundle(resolved.skill.path)
            tasks = load_tasks(resolved.dataset)
            swebench_specs = [task.swebench for task in tasks if task.swebench is not None]
            if swebench_specs:
                versions = sorted({spec.harness_version for spec in swebench_specs})
                for spec in swebench_specs:
                    validate_harness(spec)
                checks.append(("SWE-bench", True, ", ".join(versions)))
                docker_ok, docker_detail = _docker_version()
                checks.append(("Docker", docker_ok, docker_detail))
            checks.append(
                (
                    "Codex auth",
                    resolved.agent.auth_file.is_file(),
                    "file present" if resolved.agent.auth_file.is_file() else "file missing",
                )
            )
            checks.append(
                (
                    "Configuration",
                    True,
                    f"valid; {len(tasks)} task(s); skill digest {bundle.digest}",
                )
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            checks.append(("Configuration", False, str(exc)))
    runner: PromptfooRunner | None = None
    try:
        runner = _runner(promptfoo_executable)
    except (OSError, ValueError) as exc:
        checks.append(("Promptfoo", False, str(exc)))
    if runner is not None:
        expected = resolved.tooling.promptfoo_version if resolved is not None else None
        try:
            promptfoo_version = runner.validate_version(expected)
            checks.append(("Promptfoo", True, promptfoo_version))
        except (OSError, ValueError) as exc:
            checks.append(("Promptfoo", False, str(exc)))
        else:
            if resolved is not None:
                try:
                    sdk = runner.validate_codex_sdk_version(resolved.agent.codex_sdk_version)
                    checks.append(("Codex SDK", True, sdk))
                except (OSError, ValueError) as exc:
                    checks.append(("Codex SDK", False, str(exc)))
                else:
                    if resolved.tooling.container_image is not None:
                        try:
                            image_id = runner.validate_container_image(
                                resolved.tooling.container_image,
                                promptfoo_version=promptfoo_version,
                                codex_sdk_version=sdk,
                            )
                            checks.append(
                                (
                                    "Promptfoo runtime container",
                                    True,
                                    f"{resolved.tooling.container_image} ({image_id})",
                                )
                            )
                        except (OSError, ValueError) as exc:
                            checks.append(("Promptfoo runtime container", False, str(exc)))
    for name, passed, detail in checks:
        typer.echo(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
    if not all(passed for _, passed, _ in checks):
        raise typer.Exit(1)


@app.command(name="run")
def run_command(
    config: Path = typer.Argument(..., help="Evaluation YAML file."),
    baseline: Path | None = typer.Option(None, "--baseline", help="Reuse this baseline arm."),
    skip_baseline: bool = typer.Option(
        False, "--skip-baseline", help="Run treatment only and omit lift claims."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show resolved work only."),
    yes: bool = typer.Option(False, "--yes", help="Start model trials without prompting."),
    dataset: str | None = typer.Option(None, "--dataset"),
    dataset_version: str | None = typer.Option(None, "--dataset-version"),
    task_id: list[str] | None = typer.Option(None, "--task-id"),
    skill: Path | None = typer.Option(None, "--skill"),
    model: str | None = typer.Option(None, "--model"),
    reasoning: str | None = typer.Option(None, "--reasoning"),
    attempts: int | None = typer.Option(None, "--attempts", min=1),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1),
    batch_size: int | None = typer.Option(None, "--batch-size", min=1),
    max_tasks: int | None = typer.Option(None, "--max-tasks", min=1),
    max_tokens: int | None = typer.Option(None, "--max-tokens", min=1),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd", min=0),
    output: Path | None = typer.Option(None, "--output"),
    promptfoo_executable: Path | None = typer.Option(None, "--promptfoo-executable"),
) -> None:
    """Run paired baseline/treatment trials through Promptfoo."""
    try:
        resolved = load_config(
            config,
            ConfigOverrides(
                baseline_result=baseline,
                skip_baseline=skip_baseline,
                dataset=dataset,
                dataset_version=dataset_version,
                task_ids=tuple(task_id or ()),
                skill=skill,
                model=model,
                reasoning=reasoning,
                attempts=attempts,
                concurrency=concurrency,
                batch_size=batch_size,
                max_tasks=max_tasks,
                max_tokens=max_tokens,
                max_cost_usd=max_cost_usd,
                output=output,
            ),
        )
        bundle = inspect_skill_bundle(resolved.skill.path)
        tasks = load_tasks(resolved.dataset)
        runner = _runner(promptfoo_executable)
        version = runner.validate_version(resolved.tooling.promptfoo_version)
        sdk_version = runner.validate_codex_sdk_version(resolved.agent.codex_sdk_version)
        runtime_image_id = (
            runner.validate_container_image(
                resolved.tooling.container_image,
                promptfoo_version=version,
                codex_sdk_version=sdk_version,
            )
            if resolved.tooling.container_image is not None
            else None
        )
        plan = build_dry_run(
            resolved,
            bundle,
            tasks,
            runner.executable,
            version,
            runtime_image_id,
        )
        if dry_run:
            typer.echo(json.dumps(plan, indent=2))
            return
        if not resolved.agent.auth_file.is_file():
            raise ValueError(f"Codex auth file does not exist: {resolved.agent.auth_file}")
        cost = plan["possible_cost_exposure_usd"]
        typer.echo(
            f"About to start {plan['estimated_trial_count']} trial(s). Possible cost exposure: "
            f"{'unknown' if cost is None else f'${cost:.4f} USD'}."
        )
        typer.echo(plan["warning"])
        if not yes and not typer.confirm("Continue?"):
            raise typer.Abort()
        destination = run_evaluation(resolved, bundle, runner, runtime_image_id=runtime_image_id)
        typer.echo(f"Run artifacts: {destination}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _show_error(exc)


@review_app.command(name="run")
def review_run_command(
    config: Path = typer.Argument(..., help="Review evaluation YAML file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve work without model calls."),
    yes: bool = typer.Option(False, "--yes", help="Start reviewer calls without prompting."),
    task_id: list[str] | None = typer.Option(
        None, "--task-id", help="Restrict to a mutually scoreable task; repeatable."
    ),
    output: Path | None = typer.Option(None, "--output", help="Exact review-run directory."),
    promptfoo_executable: Path | None = typer.Option(None, "--promptfoo-executable"),
) -> None:
    """Review saved patches in fresh, blinded, read-only Codex conversations."""
    try:
        resolved = load_review_config(config, task_ids=tuple(task_id or ()))
        study = resolve_review_study(resolved)
        runner = _runner(promptfoo_executable)
        promptfoo_version = runner.validate_version(resolved.tooling.promptfoo_version)
        sdk_version = (
            runner.validate_codex_sdk_version(resolved.reviewer.codex_sdk_version)
            if resolved.reviewer.codex_sdk_version is not None
            else None
        )
        runtime_image_id = None
        if sdk_version is not None:
            runtime_image_id = runner.validate_container_image(
                resolved.tooling.container_image,
                promptfoo_version=promptfoo_version,
                codex_sdk_version=sdk_version,
            )
        plan = build_review_dry_run(study)
        if dry_run:
            typer.echo(json.dumps(plan, indent=2))
            return
        typer.echo(
            f"About to start {plan['estimated_review_call_count']} blinded review call(s); "
            "no implementation agents or SWE-bench graders will run."
        )
        estimated_cost = plan["estimated_cost_exposure_usd"]
        maximum_cost = plan["maximum_cost_exposure_usd"]
        typer.echo(
            "Estimated cost exposure: "
            f"{'unknown' if estimated_cost is None else f'${estimated_cost:.2f} USD'}; "
            "configured maximum: "
            f"{'unset' if maximum_cost is None else f'${maximum_cost:.2f} USD'}."
        )
        typer.echo(plan["warning"])
        if not yes and not typer.confirm("Continue?"):
            raise typer.Abort()
        destination = run_review_evaluation(
            study=study,
            runner=runner,
            run_directory=output.resolve() if output is not None else None,
            runtime_image_id=runtime_image_id,
        )
        typer.echo(f"Review run artifacts: {destination}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _show_error(exc)


@review_app.command(name="report")
def review_report_command(
    run_directory: Path = typer.Argument(..., help="PluginBench review-run directory."),
) -> None:
    """Regenerate reviewer-proxy JSON and Markdown reports from saved artifacts."""
    try:
        regenerate_review_reports(run_directory)
        typer.echo(f"Review reports: {run_directory}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _show_error(exc)


@review_app.command(name="calibrate")
def review_calibrate_command(
    run_directory: Path = typer.Argument(..., help="Completed PluginBench review-run directory."),
    config: Path = typer.Argument(..., help="Review configuration with gold calibration enabled."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve gold work without model calls."),
    yes: bool = typer.Option(False, "--yes", help="Start gold reviewer calls without prompting."),
    max_tokens: int | None = typer.Option(None, "--max-tokens", min=1),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd", min=0),
    promptfoo_executable: Path | None = typer.Option(None, "--promptfoo-executable"),
) -> None:
    """Append blinded gold-patch calibration to a completed review run."""
    try:
        resolved = load_review_config(config)
        if max_tokens is not None or max_cost_usd is not None:
            resolved = resolved.model_copy(
                update={
                    "limits": ReviewLimitsConfig(
                        max_tokens=(
                            max_tokens if max_tokens is not None else resolved.limits.max_tokens
                        ),
                        max_cost_usd=(
                            max_cost_usd
                            if max_cost_usd is not None
                            else resolved.limits.max_cost_usd
                        ),
                    )
                }
            )
        study = resolve_review_study(resolved)
        plan = build_gold_calibration_dry_run(study, run_directory.resolve())
        runner = _runner(promptfoo_executable)
        promptfoo_version = runner.validate_version(resolved.tooling.promptfoo_version)
        sdk_version = (
            runner.validate_codex_sdk_version(resolved.reviewer.codex_sdk_version)
            if resolved.reviewer.codex_sdk_version is not None
            else None
        )
        runtime_image_id = None
        if sdk_version is not None:
            runtime_image_id = runner.validate_container_image(
                resolved.tooling.container_image,
                promptfoo_version=promptfoo_version,
                codex_sdk_version=sdk_version,
            )
        if dry_run:
            typer.echo(json.dumps(plan, indent=2))
            return
        typer.echo(
            f"About to start {plan['estimated_review_call_count']} blinded gold review call(s); "
            "no implementation agents or SWE-bench graders will run."
        )
        maximum_cost = plan["maximum_cost_exposure_usd"]
        typer.echo(
            "Configured calibration maximum: "
            f"{'unset' if maximum_cost is None else f'${maximum_cost:.2f} USD'}."
        )
        typer.echo(plan["warning"])
        if not yes and not typer.confirm("Continue?"):
            raise typer.Abort()
        destination = run_gold_calibration(
            study=study,
            source_run=run_directory.resolve(),
            runner=runner,
            runtime_image_id=runtime_image_id,
        )
        typer.echo(f"Gold calibration artifacts: {destination}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _show_error(exc)


def _decision_kwargs(config: PluginbenchConfig | None) -> dict[str, Any]:
    if config is None:
        return {}
    return {
        "minimum_pass_rate_lift_pp": config.decision.minimum_pass_rate_lift_pp,
        "maximum_cost_overhead_pct": config.decision.maximum_cost_overhead_pct,
        "maximum_incremental_cost_per_additional_success_usd": (
            config.decision.maximum_incremental_cost_per_additional_success_usd
        ),
    }


def _config_from_arm(arm: ArmResult) -> PluginbenchConfig | None:
    resolved = arm.provenance.get("resolved_configuration")
    return PluginbenchConfig.model_validate(resolved) if isinstance(resolved, dict) else None


@app.command()
def compare(
    baseline_result: Path = typer.Argument(..., help="Baseline arm.json or directory."),
    treatment_result: Path = typer.Argument(..., help="Treatment arm.json or directory."),
    output: Path = typer.Option(Path("comparison-report"), "--output"),
) -> None:
    """Compare compatible saved baseline and treatment arms."""
    try:
        baseline = load_arm(baseline_result)
        treatment = load_arm(treatment_result)
        if baseline.arm != "baseline" or treatment.arm != "treatment":
            raise ValueError("compare expects baseline followed by treatment")
        config = _config_from_arm(treatment)
        report_data = build_report(
            baseline,
            treatment,
            evaluate_decision=config is not None,
            **_decision_kwargs(config),
        )
        output.mkdir(parents=True, exist_ok=True)
        write_reports(output, report_data)
        typer.echo(f"Reports: {output}")
    except (OSError, ValueError) as exc:
        _show_error(exc)


@app.command()
def report(run_directory: Path = typer.Argument(..., help="pluginbench run directory.")) -> None:
    """Regenerate JSON and Markdown reports from saved arm artifacts."""
    try:
        treatment = load_arm(run_directory / "treatment")
        if treatment.arm != "treatment":
            raise ValueError("report expects treatment/arm.json to have arm=treatment")
        baseline_path = run_directory / "baseline" / "arm.json"
        baseline = load_arm(baseline_path) if baseline_path.exists() else None
        if baseline is None:
            manifest_path = run_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            reused = manifest.get("reused_baseline")
            baseline = load_arm(Path(reused)) if isinstance(reused, str) else None
        if baseline is not None and baseline.arm != "baseline":
            raise ValueError("report expects the baseline artifact to have arm=baseline")
        config = _config_from_arm(treatment)
        comparison_baseline = (
            baseline
            if baseline is not None and baseline.tasks.keys() == treatment.tasks.keys()
            else None
        )
        generated = build_report(comparison_baseline, treatment, **_decision_kwargs(config))
        write_reports(run_directory, generated)
        typer.echo(f"Reports: {run_directory}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _show_error(exc)


def entrypoint() -> None:
    app(prog_name=Path(sys.argv[0]).name)
