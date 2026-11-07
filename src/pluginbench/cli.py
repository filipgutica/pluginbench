"""Run an evaluation or rebuild its saved report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from pluginbench import __version__, run_lock
from pluginbench.evaluation import (
    CheckFailed,
    Evaluation,
    check_evaluation,
    load_evaluation,
    plan_evaluation,
    plan_resume,
    prepare_evaluation,
    regenerate_report,
    resolve_evaluation,
    resume_evaluation,
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
    if isinstance(exc, CheckFailed):
        typer.echo(_readiness_table(exc.report), err=True)
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(2)


def _readiness_table(report: dict[str, object]) -> str:
    """One row per task, one column per measure or check, each cell a readiness state."""
    matrix = report["matrix"]
    assert isinstance(matrix, dict)
    columns = list(dict.fromkeys(name for rows in matrix.values() for name in rows))
    table = [["task", *columns]] + [
        [task, *(str(rows.get(name, {}).get("state", "-")) for name in columns)]
        for task, rows in matrix.items()
    ]
    widths = [max(len(row[index]) for row in table) for index in range(len(columns) + 1)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in table
    )


def _vanilla_cache_lines(report: dict[str, object]) -> list[str]:
    """One line per vanilla attempt: would `run` reuse it from the cache?"""
    rows = report["vanilla_cache"]
    assert isinstance(rows, list)
    hits = sum(row["status"] == "hit" for row in rows)
    return [f"Vanilla cache: {hits} of {len(rows)} attempts hit"] + [
        f"  {row['task_id']} repeat {row['repeat']}: {row['status']} ({row['cache_key'][:12]})"
        for row in rows
    ]


def _new_run_directory(prefix: str = "") -> Path:
    return Path("runs") / (prefix + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))


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


@contextmanager
def _events(root: Path) -> Iterator[Callable[..., None]]:
    """Append one JSON line per event to events.jsonl, so a background run can be followed."""
    lock = threading.Lock()
    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:

        def event(kind: str, **fields: object) -> None:
            line = json.dumps({"at": datetime.now(UTC).isoformat(), "event": kind, **fields})
            with lock:
                stream.write(line + "\n")
                stream.flush()

        yield event


def _work(root: Path, *, progress: Callable[[str], None], notify: bool) -> None:
    """Own the run directory and run what it has left; SIGTERM from `stop` interrupts it."""
    with run_lock.hold(root) as lock, _events(root) as event:
        event("started", pid=os.getpid(), root=str(root))

        def logged(message: str) -> None:
            event("progress", message=message)
            progress(message)

        try:
            resume_evaluation(root, progress=logged, guard=lock.assert_in_place)
        except BaseException as exc:
            event("failed", error=f"{type(exc).__name__}: {exc}")
            if notify:
                _notify(f"Run failed: {str(exc) or type(exc).__name__}")
            raise
        event("finished", report=str(root / "report/report.html"))
    if notify:
        _notify(f"Run finished. Report: {root / 'report/report.html'}")
    typer.echo(f"Report: {root / 'report/report.html'}")
    typer.echo(f"Graphable data: {root / 'report/results.json'}")


def _worker_argv(root: Path, *, notify: bool) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pluginbench",
        "run",
        str(root),
        "--yes",
        "--foreground",
        *(["--notify"] if notify else []),
    ]


_DETACH_TIMEOUT = 30.0


def _detach(root: Path, *, notify: bool) -> None:
    """Start the run in its own session and return once that process holds run.lock."""
    log = root / "run.log"
    with log.open("ab") as output:
        child = subprocess.Popen(
            _worker_argv(root, notify=notify),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + _DETACH_TIMEOUT
    while (run_lock.owner(root) or {}).get("pid") != child.pid:
        if child.poll() is not None:
            raise ValueError(
                f"background run exited with code {child.returncode} before it started; see {log}"
            )
        if time.monotonic() > deadline:
            raise ValueError(f"background run did not start within {_DETACH_TIMEOUT:g}s; see {log}")
        time.sleep(0.05)
    typer.echo(f"Started run {root} in the background (process group {child.pid}).")
    typer.echo(f"Events: {root / 'events.jsonl'}   Log: {log}")
    typer.echo(f"Stop it: pluginbench stop {root}")
    typer.echo(f"Resume it after a stop or crash: pluginbench run {root}")


@app.callback()
def main(version: bool = typer.Option(False, "--version", is_eager=True)) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("run")
def run(
    target: Path | None = typer.Argument(
        None,
        help="Evaluation YAML or JSON to start, or a run directory to resume.",
    ),
    resume: Path | None = typer.Option(
        None,
        "--resume",
        help="Run directory to resume: keeps complete attempts and codes only what is left.",
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
        max=4,
        help="Jobs per phase (default 2, at most 4); fails if Docker capacity is lower.",
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
    foreground: bool = typer.Option(
        False,
        "--foreground",
        help="Run in this process instead of in the background.",
    ),
) -> None:
    """Start or resume a run: vanilla and native treatments, measurements, and fixed reviews.

    Checks run first, in this process. The run then continues in the background unless
    --foreground is set.
    """
    try:
        if target is not None:
            if resume or config:
                raise ValueError("give a config or run directory once, not also --resume/--config")
            if target.is_dir():
                resume = target
            else:
                config = target
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
        if resume:
            root = resume.expanduser().absolute()
        else:
            if evaluation is None:
                raise ValueError("missing evaluation settings")
            destination = output or _new_run_directory()
            try:
                root = prepare_evaluation(evaluation, destination, progress=typer.echo)
            except BaseException as exc:
                if notify:
                    _notify(f"Run failed: {str(exc) or type(exc).__name__}")
                raise
        if foreground:
            _work(root, progress=progress, notify=notify)
        else:
            _detach(root, notify=notify)
    except (OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        _error(exc)


@app.command("check")
def check(
    config: Path = typer.Argument(
        ..., help="Evaluation YAML or JSON; relative paths use its directory."
    ),
    output: Path | None = typer.Option(
        None, "--output", help="New directory for prepared inputs and check.json."
    ),
) -> None:
    """Prepare tasks and run every no-model check; exit 2 if the run would fail or lose data."""
    try:
        destination = output or _new_run_directory("check-")
        report = check_evaluation(load_evaluation(config), destination, progress=typer.echo)
        typer.echo("\n".join(_vanilla_cache_lines(report)))
        typer.echo(_readiness_table(report))
        typer.echo(f"Ready. Check: {destination / 'check.json'}")
    except (OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        _error(exc)


@app.command("stop")
def stop(run: Path = typer.Argument(..., help="A run directory started by `pluginbench run`.")) -> None:
    """Stop a run: SIGTERM only the process group its run.lock records; nothing else."""
    try:
        owner = run_lock.stop(run)
    except (OSError, ValueError) as exc:
        _error(exc)
        return
    if owner is None:
        typer.echo(f"No active run owns {run}; nothing signalled.")
    else:
        typer.echo(
            f"Sent SIGTERM to process group {owner['pgid']}. Attempts already coding finish "
            "and are kept; no new attempt starts."
        )


@app.command("report")
def report(directory: Path = typer.Argument(..., help="A saved evaluation directory.")) -> None:
    """Rebuild JSON, CSV and charts from saved evidence; no model calls."""
    try:
        path = regenerate_report(directory)
        typer.echo(f"Report: {path.parent / 'report.html'}")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _error(exc)
