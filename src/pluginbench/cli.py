"""Run an evaluation or rebuild its saved report."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from pluginbench import __version__
from pluginbench.evaluation import (
    Evaluation,
    load_evaluation,
    plan_evaluation,
    plan_resume,
    regenerate_report,
    resolve_evaluation,
    resume_evaluation,
    run_evaluation,
)

app = typer.Typer(
    help="Compare native instructions, skills, and plugins with vanilla coding agents.",
    no_args_is_help=True,
    invoke_without_command=True,
)
tasks_app = typer.Typer(help="Browse tasks and emit a pinned evaluation YAML block.")
app.add_typer(tasks_app, name="tasks")


@tasks_app.command("pick")
def pick(
    dataset: str = typer.Option(
        ..., "--dataset", help="Harbor dataset, e.g. datacurve/deep-swe-1-1."
    ),
    version: str | None = typer.Option(
        None, "--version", help="Dataset version; resolved and pinned if omitted."
    ),
    copy: bool = typer.Option(
        False, "--copy", help="Also copy the emitted tasks YAML to the clipboard."
    ),
) -> None:
    """Search, preview and multiselect tasks with fzf; no evaluation calls."""
    from pluginbench.task_sources import copy_yaml, pick_tasks

    try:
        block = pick_tasks(dataset, version)
        if block is not None:
            typer.echo(block, nl=False)
            if copy:
                copy_yaml(block)
                typer.echo("Copied tasks YAML to clipboard.", err=True)
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        _error(exc)


def _error(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(2)


def _notify(message: str) -> None:
    """Best-effort macOS notification; a failed notice never fails the run."""
    text = json.dumps(message, ensure_ascii=False)
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {text} with title "PluginBench"'],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _notifying_progress(attempts: object) -> Callable[[str], None]:
    started = False

    def progress(message: str) -> None:
        nonlocal started
        typer.echo(message)
        # The first coding attempt starts only after preparation and readiness checks pass.
        if not started and message.startswith("Coding "):
            started = True
            _notify(f"Setup passed; {attempts} coding attempts started.")

    return progress


@app.callback()
def main(version: bool = typer.Option(False, "--version", is_eager=True)) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("run")
def run(
    resume: Path | None = typer.Option(
        None,
        "--resume",
        help="Finish collection/reviews or recover citation formatting from saved attempts.",
    ),
    config: Path | None = typer.Option(
        None, "--config", help="Evaluation YAML or JSON; relative paths use its directory."
    ),
    harness: str | None = typer.Option(
        None, "--harness", help="Coding harness; native treatments currently require codex."
    ),
    model: str | None = typer.Option(None, "--model"),
    reasoning: str | None = typer.Option(None, "--reasoning"),
    guidance: list[Path] | None = typer.Option(
        None,
        "--guidance",
        help="Instruction file; composed into native AGENTS.md, never the task prompt.",
    ),
    skill: list[Path] | None = typer.Option(
        None, "--skill", help="Native skill directory or collection; repeat for one treatment."
    ),
    plugin: list[Path] | None = typer.Option(
        None, "--plugin", help="Complete native plugin package; repeat for one treatment."
    ),
    guidance_name: str = typer.Option(
        "guidance", "--guidance-name", help="Bundle label when using flags."
    ),
    tasks: Path | None = typer.Option(
        None, "--tasks", help="Local Harbor task directory or checkout."
    ),
    task: list[str] | None = typer.Option(
        None, "--task", help="Exact task ID; repeat to select tasks."
    ),
    measure: list[str] | None = typer.Option(
        None,
        "--measure",
        help="tokens, lines, cyclomatic, cognitive, dead_code, coverage, review; repeat.",
    ),
    review_harness: str | None = typer.Option(None, "--review-harness"),
    review_model: str | None = typer.Option(None, "--review-model"),
    review_reasoning: str | None = typer.Option(None, "--review-reasoning"),
    review_guidance: list[Path] | None = typer.Option(
        None, "--review-guidance", help="Fixed review standards for every output; repeat."
    ),
    repeats: int | None = typer.Option(None, "--repeats", min=1),
    seed: int | None = typer.Option(None, "--seed"),
    timeout: int | None = typer.Option(None, "--timeout", min=1),
    jobs: int | None = typer.Option(
        None,
        "--jobs",
        min=1,
        max=16,
        help="Maximum jobs per phase; Harbor coding is bounded by Docker CPU/memory capacity (up to 4).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate inputs and show planned attempts; no model calls or preparation.",
    ),
    output: Path | None = typer.Option(None, "--output", help="New run directory."),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
    notify: bool = typer.Option(
        False,
        "--notify",
        help="Send macOS notifications when coding starts and when the run finishes or fails.",
    ),
) -> None:
    """Run vanilla and native treatments, selected measurements, and fixed reviews."""
    try:
        overrides = [
            harness,
            model,
            reasoning,
            guidance,
            skill,
            plugin,
            tasks,
            task,
            measure,
            review_harness,
            review_model,
            review_reasoning,
            review_guidance,
            repeats,
            seed,
            timeout,
        ]
        evaluation = None
        if resume:
            if (
                config
                or output
                or jobs is not None
                or any(value is not None for value in overrides)
                or guidance_name != "guidance"
            ):
                raise ValueError(
                    "--resume uses saved settings; --dry-run/--yes are its only other options"
                )
            plan = plan_resume(resume)
        elif config:
            if any(value is not None for value in overrides) or guidance_name != "guidance":
                raise ValueError(
                    "use --config or evaluation flags, not both; --jobs/--output/--dry-run/--yes work with either"
                )
            evaluation = load_evaluation(config)
            if jobs is not None:
                evaluation = Evaluation.model_validate(
                    {**evaluation.model_dump(mode="json"), "jobs": jobs}
                )
        else:
            raw: dict[str, object] = {
                "runner": {"harness": harness, "model": model, "reasoning": reasoning},
                "treatments": {
                    guidance_name: {
                        "guidance": guidance or [],
                        "skills": skill or [],
                        "plugins": plugin or [],
                    }
                },
                "tasks": {"source": tasks, "ids": task or []},
                "measurements": measure or [],
            }
            if any(
                value is not None
                for value in (review_harness, review_model, review_reasoning, review_guidance)
            ):
                raw["reviewer"] = {
                    "harness": review_harness,
                    "model": review_model,
                    "reasoning": review_reasoning,
                    "guidance": review_guidance or [],
                }
            for key, value in (
                ("repeats", repeats),
                ("seed", seed),
                ("timeout_seconds", timeout),
                ("jobs", jobs),
            ):
                if value is not None:
                    raw[key] = value
            evaluation = resolve_evaluation(raw, Path.cwd())
        if evaluation is not None:
            plan = plan_evaluation(evaluation)
        if dry_run:
            typer.echo(json.dumps(plan, indent=2))
            return
        typer.echo(
            f"{plan['coding_calls']} coding attempts + {plan['review_calls']} review calls; "
            + ("saved submissions reused." if resume else "vanilla reused when compatible.")
        )
        if not yes and not typer.confirm("Start evaluation?"):
            raise typer.Exit()
        progress = _notifying_progress(plan["coding_calls"]) if notify else typer.echo
        try:
            if resume:
                root = resume_evaluation(resume, progress=progress)
            else:
                if evaluation is None:
                    raise ValueError("missing evaluation settings")
                destination = output or Path("runs") / datetime.now(UTC).strftime(
                    "%Y%m%dT%H%M%S%fZ"
                )
                root = run_evaluation(evaluation, destination, progress=progress)
        except BaseException as exc:
            if notify:
                _notify(f"Run failed: {str(exc) or type(exc).__name__}")
            raise
        if notify:
            _notify(f"Run finished. Report: {root / 'report/report.html'}")
        typer.echo(f"Report: {root / 'report/report.html'}")
        typer.echo(f"Graphable data: {root / 'report/results.json'}")
    except (OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        _error(exc)


@app.command("report")
def report(directory: Path = typer.Argument(..., help="A saved evaluation directory.")) -> None:
    """Rebuild JSON, CSV and charts from saved evidence; no model calls."""
    try:
        path = regenerate_report(directory)
        typer.echo(f"Report: {path.parent / 'report.html'}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _error(exc)
