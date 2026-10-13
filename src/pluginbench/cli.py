"""Run an evaluation or rebuild its saved report."""

from __future__ import annotations

import json
import subprocess
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
    help="Compare text skills and guidance with vanilla coding agents.",
    no_args_is_help=True,
    invoke_without_command=True,
)


def _error(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(2)


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
    harness: str | None = typer.Option(None, "--harness", help="codex or claude."),
    model: str | None = typer.Option(None, "--model"),
    reasoning: str | None = typer.Option(None, "--reasoning"),
    guidance: list[Path] | None = typer.Option(
        None, "--guidance", help="Text file, skill, or package directory; repeat for one bundle."
    ),
    guidance_name: str = typer.Option(
        "guidance", "--guidance-name", help="Bundle label when using flags."
    ),
    tasks: Path | None = typer.Option(
        None, "--tasks", help="Pinned DeepSWE checkout or tasks directory."
    ),
    task: list[str] | None = typer.Option(
        None, "--task", help="Exact task ID; repeat to select tasks."
    ),
    measure: list[str] | None = typer.Option(
        None,
        "--measure",
        help="tokens, lines, cyclomatic, cognitive, dead_code, coverage, test_overlap, review; repeat.",
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
        help="Validate inputs and show planned calls; no model calls or preparation.",
    ),
    output: Path | None = typer.Option(None, "--output", help="New run directory."),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
) -> None:
    """Run vanilla and guidance attempts, selected measurements, and fixed reviews."""
    try:
        overrides = [
            harness,
            model,
            reasoning,
            guidance,
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
                "guidance": {guidance_name: guidance or []},
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
            f"{plan['coding_calls']} coding calls + {plan['review_calls']} review calls; "
            + ("saved submissions reused." if resume else "vanilla reused when compatible.")
        )
        if not yes and not typer.confirm("Start evaluation?"):
            raise typer.Exit()
        if resume:
            root = resume_evaluation(resume, progress=typer.echo)
        else:
            if evaluation is None:
                raise ValueError("missing evaluation settings")
            destination = output or Path("runs") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            root = run_evaluation(evaluation, destination, progress=typer.echo)
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
