"""The pluginbench command line: init, check, run, status, stop, compare, report, review, judge."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from pluginbench import __version__, run_lock
from pluginbench.isolation import RuntimeDriftError
from pluginbench.evaluation import (
    CheckFailed,
    check_evaluation,
    load_evaluation,
    plan_evaluation,
    plan_resume,
    plan_review,
    prepare_evaluation,
    regenerate_report,
    resume_evaluation,
    review_evaluation,
)
from pluginbench.run_status import format_status, run_status

app = typer.Typer(
    help="Compare native instructions, skills, and plugins with vanilla coding agents.",
    no_args_is_help=True,
    invoke_without_command=True,
)
tasks_app = typer.Typer(help="Browse tasks and emit a pinned evaluation YAML block.")
app.add_typer(tasks_app, name="tasks")
judge_app = typer.Typer(help="Blinded pairwise judging of two arms' matched submissions.")
app.add_typer(judge_app, name="judge")


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


def _confirm(question: str) -> bool:
    """Ask before paid work; without a terminal, explain how to start instead of aborting."""
    if not sys.stdin.isatty():
        typer.echo("No terminal to confirm in. Pass --yes to start without confirmation.", err=True)
        raise typer.Exit(1)
    return typer.confirm(question)


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


def _plan_summary(plan: dict[str, object]) -> str:
    """The readable form of a plan from plan_evaluation or plan_resume."""
    lines = [f"Evaluation: {plan['name']}"] if "name" in plan else []
    runner = plan.get("runner")
    if isinstance(runner, dict):
        lines.append(
            f"Runner: {runner['harness']} {runner['model']}, reasoning {runner['reasoning']}"
        )
    treatments = plan.get("treatments")
    if isinstance(treatments, dict):
        lines.append("Arms: " + ", ".join(["vanilla", *treatments]))
    tasks = plan.get("tasks")
    if isinstance(tasks, list):
        lines.append(f"Tasks ({len(tasks)}): " + ", ".join(tasks))
    for label, key in (("Repeats", "repeats"), ("Jobs", "jobs")):
        if key in plan:
            lines.append(f"{label}: {plan[key]}")
    lines.append(f"Coding attempts: {plan['coding_calls']}   Review calls: {plan['review_calls']}")
    measurements = plan.get("measurements")
    if isinstance(measurements, list):
        lines.append("Measurements: " + (", ".join(measurements) or "none"))
    capabilities = plan.get("measurement_capabilities")
    if isinstance(capabilities, dict):
        unavailable = [
            f"  {task}: {name}: {row.get('reason')}"
            for task, rows in capabilities.items()
            for name, row in rows.items()
            if row.get("status") != "available"
        ]
        if unavailable:
            lines += ["Unavailable measurements:", *unavailable]
    if "collect_measurements" in plan:
        pending = "collected after coding" if plan["collect_measurements"] else "none pending"
        lines.append(f"Measurements: {pending}")
    if "vanilla" in plan:
        lines.append(f"Reuse: {plan['vanilla']}")
    return "\n".join(lines)


def _notifier() -> str:
    return "osascript" if sys.platform == "darwin" else "notify-send"


def _notify(message: str) -> None:
    """Best-effort desktop notification; a failed notice never fails the run."""
    if sys.platform == "darwin":
        text = json.dumps(message, ensure_ascii=False)
        command = [_notifier(), "-e", f'display notification {text} with title "PluginBench"']
    else:
        command = [_notifier(), "PluginBench", message]
    try:
        subprocess.run(command, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass  # No notifier installed, or it hung: the run's outcome does not depend on it.


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


def _ended(event: Callable[..., None], exc: BaseException) -> None:
    """Record why work ended early: `stop` (SIGTERM) and Ctrl-C stop it; anything else fails."""
    if isinstance(exc, KeyboardInterrupt):
        event("stopped", reason=str(exc) or "interrupted")
    else:
        event("failed", error=f"{type(exc).__name__}: {exc}")


def _work(
    root: Path,
    *,
    lock: run_lock.RunLock | None,
    progress: Callable[[str], None],
    notify: bool,
) -> None:
    """Own the run directory and run what it has left; SIGTERM from `stop` interrupts it."""
    owned = nullcontext(lock) if lock else run_lock.hold(root)
    with owned as lock, _events(root) as event:
        event("started", pid=os.getpid(), root=str(root))

        def logged(message: str) -> None:
            event("progress", message=message)
            progress(message)

        try:
            resume_evaluation(root, progress=logged, guard=lock.assert_in_place)
        except BaseException as exc:
            _ended(event, exc)
            if notify:
                outcome = "stopped" if isinstance(exc, KeyboardInterrupt) else "failed"
                _notify(f"Run {outcome}: {str(exc) or type(exc).__name__}")
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
_SYSTEMD_SCOPE = ["systemd-run", "--user", "--scope", "--collect"]


def _systemd_scope() -> list[str]:
    """`systemd-run` arguments that start a command in its own user scope, if this host can.

    A scope keeps the command's PID, environment, and output, but moves it out of the
    launcher's cgroup, so stopping the launching terminal, agent session, or service does not
    kill the run. Hosts without a reachable systemd user manager get an empty list.
    """
    if shutil.which(_SYSTEMD_SCOPE[0]) is None:
        return []
    try:
        probe = subprocess.run(
            [*_SYSTEMD_SCOPE, "--quiet", "true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _SYSTEMD_SCOPE if probe.returncode == 0 else []


def _detach(root: Path, *, notify: bool) -> None:
    """Start the run in its own session and user scope; return once it holds run.lock."""
    log = root / "run.log"
    scope = _systemd_scope()
    with log.open("ab") as output:
        # systemd-run execs the worker in place, so the child's PID is the worker's.
        child = subprocess.Popen(
            [*scope, *_worker_argv(root, notify=notify)],
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
    where = "a systemd user scope" if scope else "a new session"
    typer.echo(f"Started run {root} in the background (process group {child.pid}, in {where}).")
    typer.echo(f"Watch it: pluginbench status {root} --watch")
    typer.echo(f"Events: {root / 'events.jsonl'}   Log: {log}")
    typer.echo(f"Stop it: pluginbench stop {root}")
    typer.echo(f"Resume it after a stop or crash: pluginbench run {root}")


@app.callback()
def main(version: bool = typer.Option(False, "--version", is_eager=True)) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command("init")
def init(
    config: Path = typer.Argument(
        Path("evaluation.yaml"), help="Where to write the starter evaluation config."
    ),
) -> None:
    """Write a starter evaluation config to edit; no checks and no model calls."""
    try:
        if config.exists():
            raise ValueError(f"{config} already exists; edit it, or give another path")
        starter = Path(__file__).with_name("evaluation.example.yaml").read_text()
        with config.open("x", encoding="utf-8") as stream:
            stream.write(starter)
    except (OSError, ValueError) as exc:
        _error(exc)
    typer.echo(f"Wrote {config}. Replace its /path/to placeholders, then run:")
    typer.echo(f"  pluginbench check {config}")


@app.command("run")
def run(
    target: Path = typer.Argument(
        ...,
        help="Evaluation YAML or JSON to start, or a run directory to resume.",
    ),
    output: Path | None = typer.Option(
        None, "--output", help="Directory for a new run; default runs/<timestamp>."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would run; no preparation and no model calls.",
    ),
    as_json: bool = typer.Option(False, "--json", help="With --dry-run, print the plan as JSON."),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
    notify: bool = typer.Option(
        False,
        "--notify",
        help="Desktop notifications when coding starts and when the run finishes or fails.",
    ),
    foreground: bool = typer.Option(
        False,
        "--foreground",
        help="Run in this process instead of in the background.",
    ),
) -> None:
    """Start a run from a config, or resume a run directory.

    Checks run first, in this process. The run then continues in the background unless
    --foreground is set. Every setting comes from the config; a resume uses the saved one.
    """
    owned = ExitStack()
    lock: run_lock.RunLock | None = None
    try:
        if as_json and not dry_run:
            raise ValueError("--json prints the dry-run plan; add --dry-run")
        if not target.exists():
            raise ValueError(
                f"{target} does not exist; give an evaluation YAML or JSON, or a run directory"
            )
        evaluation = None
        if target.is_dir():
            if output is not None:
                raise ValueError(
                    f"{target} is a run directory, which resumes in place; "
                    "--output names the directory for a new run from a config"
                )
            if foreground and not dry_run:
                # Own the run before planning, so a background start never races its parent.
                lock = owned.enter_context(run_lock.hold(target))
            plan = plan_resume(target)
        else:
            evaluation = load_evaluation(target)
            plan = plan_evaluation(evaluation)
        if dry_run:
            typer.echo(json.dumps(plan, indent=2) if as_json else _plan_summary(plan))
            return
        typer.echo(_plan_summary(plan))
        if notify and shutil.which(_notifier()) is None:
            typer.echo(
                f"Warning: --notify needs {_notifier()}, which is not on PATH; "
                "no notification will be sent.",
                err=True,
            )
        if not yes and not _confirm("Start evaluation?"):
            raise typer.Exit()
        progress = _notifying_progress(plan["coding_calls"]) if notify else typer.echo
        if evaluation is None:
            root = target.expanduser().resolve()
        else:
            destination = output or _new_run_directory()
            try:
                root = prepare_evaluation(evaluation, destination, progress=typer.echo)
            except BaseException as exc:
                if notify:
                    _notify(f"Run failed: {str(exc) or type(exc).__name__}")
                raise
        if foreground:
            _work(root, lock=lock, progress=progress, notify=notify)
        else:
            _detach(root, notify=notify)
    except (
        OSError,
        ValueError,
        yaml.YAMLError,
        subprocess.SubprocessError,
        RuntimeDriftError,
    ) as exc:
        _error(exc)
    finally:
        owned.close()


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
    except (
        OSError,
        ValueError,
        yaml.YAMLError,
        subprocess.SubprocessError,
        RuntimeDriftError,
    ) as exc:
        _error(exc)


@app.command("status")
def status(
    run: Path = typer.Argument(..., help="A run directory started by `pluginbench run`."),
    watch: bool = typer.Option(
        False, "--watch", help="Redraw every 2 seconds; exit when the run ends or is stopped."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print one JSON object (schema in docs/running-evaluations.md)."
    ),
) -> None:
    """Show a run's phase, attempts, outcomes, tokens, reuse and measures; never writes."""
    try:
        if watch and as_json:
            raise ValueError("--watch redraws the table; for JSON, run status --json repeatedly")
        decode_errors = 0
        while True:
            try:
                current = run_status(run)
            except json.JSONDecodeError:
                # A settings or report file caught mid-write; skip this redraw, not the watch.
                decode_errors += 1
                if not watch or decode_errors > _WATCH_DECODE_RETRIES:
                    raise
                time.sleep(_WATCH_INTERVAL)
                continue
            decode_errors = 0
            if as_json:
                typer.echo(json.dumps(current, indent=2))
                return
            if watch and sys.stdout.isatty():
                typer.echo("\033[H\033[2J", nl=False)  # Clear the screen before redrawing.
            typer.echo(format_status(current))
            if not watch or not current["active"]:
                return
            time.sleep(_WATCH_INTERVAL)
    except (OSError, ValueError) as exc:
        _error(exc)


_WATCH_INTERVAL = 2.0
_WATCH_DECODE_RETRIES = 5


@app.command("stop")
def stop(
    run: Path = typer.Argument(..., help="A run directory started by `pluginbench run`."),
) -> None:
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


@app.command("compare")
def compare(
    runs: list[str] = typer.Argument(
        ...,
        help="Evaluation directories. DIR=ARM,ARM takes only those packages from DIR.",
    ),
    gates: Path = typer.Option(..., "--gates", help="Gates YAML: splits and gate rules."),
) -> None:
    """Print per-package measures and gate results from saved reports; no model calls."""
    from pluginbench.compare import compare as compare_runs

    try:
        typer.echo(compare_runs(runs, gates))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _error(exc)


@app.command("report")
def report(
    run: Path = typer.Argument(..., help="A run directory started by `pluginbench run`."),
) -> None:
    """Rebuild JSON, CSV and charts from saved evidence; no model calls."""
    try:
        path = regenerate_report(run)
        typer.echo(f"Report: {path.parent / 'report.html'}")
        unfinished = json.loads(path.read_text()).get("unfinished_run")
        if unfinished:
            resume = shlex.join(["pluginbench", "run", str(run)])
            typer.echo(
                f"This run is not finished: {unfinished['complete']} of "
                f"{unfinished['assigned']} attempts are complete, so the report is partial. "
                f"Finish it with: {resume}",
                err=True,
            )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _error(exc)


@app.command("review")
def review(
    run: Path = typer.Argument(..., help="A finished run directory."),
    revision: str = typer.Option(
        ..., "--revision", help="Name for the new review revision, such as review-2."
    ),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
) -> None:
    """Review every submission again with the run's frozen reviewer and criteria.

    Earlier review revisions stay in evidence. The new one is selected and the report rebuilt.
    """
    try:
        with run_lock.hold(run):
            plan = plan_review(run, revision)
            typer.echo(f"{plan['review_calls']} review calls as review revision {revision}.")
            if not yes and not _confirm("Start review?"):
                raise typer.Exit()
            _review(run, revision)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _error(exc)


def _review(run: Path, revision: str) -> None:
    """Run a planned review while this process owns the run, recording it in events.jsonl."""
    with _events(run) as event:
        event("started", command="review", pid=os.getpid(), revision=revision)

        def logged(message: str) -> None:
            event("progress", message=message)
            typer.echo(message)

        try:
            selected = review_evaluation(run, revision, progress=logged)
        except BaseException as exc:
            _ended(event, exc)
            raise
        event("finished", command="review", revision=selected)
    typer.echo(f"Selected review revision {selected}.")
    typer.echo(f"Report: {run / 'report/report.html'}")


@judge_app.command("prepare")
def judge_prepare(
    arms: list[str] = typer.Argument(
        ..., help="Two RUN_DIR=ARM specs; both may name the same run."
    ),
    tasks: str = typer.Option(..., "--tasks", help="Comma-separated task names to judge."),
    exclude: str = typer.Option(
        "", "--exclude", help="Comma-separated task names to refuse, such as holdout tasks."
    ),
    criteria: Path = typer.Option(..., "--criteria", help="The review standard judges apply."),
    seed: int = typer.Option(..., "--seed", help="Seed for each pair's A/B order."),
    output: Path = typer.Option(..., "--output", help="New directory for briefs and the key."),
) -> None:
    """Write blinded judge briefs for each task and repeat both arms have; no model calls."""
    from pluginbench.judge import prepare

    def names(value: str) -> list[str]:
        return [name.strip() for name in value.split(",") if name.strip()]

    try:
        directory = prepare(
            arms=arms,
            tasks=names(tasks),
            exclude=names(exclude),
            criteria=criteria,
            seed=seed,
            output=output,
        )
    except (OSError, ValueError, KeyError) as exc:
        _error(exc)
        return
    typer.echo(f"Prepared {directory}. The key is {directory / 'key.json'}; keep it from judges.")
    omitted = json.loads((directory / "key.json").read_text()).get("omitted") or []
    if omitted:
        slots = ", ".join(f"{row['arm']} {row['task']} repeat {row['repeat']}" for row in omitted)
        typer.echo(
            f"Left out, with their pairs, the attempts that have no saved submission: {slots}.",
            err=True,
        )


@judge_app.command("run")
def judge_run(
    directory: Path = typer.Argument(..., help="A directory from `pluginbench judge prepare`."),
    judges: list[str] = typer.Option(
        ..., "--judge", help="HARNESS:MODEL:EFFORT, such as codex:gpt-6-astra:high; repeatable."
    ),
    timeout: int = typer.Option(900, "--timeout", help="Seconds per judge call."),
    jobs: int = typer.Option(1, "--jobs", help="Judge calls at once, 1 to 16."),
) -> None:
    """Make one read-only call per judge and task; completed calls are skipped."""
    from pluginbench.judge import run as run_judges

    try:
        failed = run_judges(
            directory, judges=judges, timeout=timeout, jobs=jobs, progress=typer.echo
        )
    except (OSError, ValueError, KeyError) as exc:
        _error(exc)
        return
    if failed:
        typer.echo(f"{failed} judge calls failed; run again to retry them.", err=True)
        raise typer.Exit(2)
    typer.echo(f"All judge calls complete. Tally: pluginbench judge tally {directory}")


@judge_app.command("tally")
def judge_tally(
    directory: Path = typer.Argument(..., help="A directory whose judge calls are complete."),
    require_bound: bool = typer.Option(
        False, help="Refuse legacy verdicts without input bindings."
    ),
) -> None:
    """Unblind with the key and score each pair; writes tally.json; no model calls."""
    from pluginbench.judge import format_tally, tally

    try:
        result = tally(directory, require_bound=require_bound)
        (directory / "tally.json").write_text(json.dumps(result, indent=2) + "\n")
    except (OSError, ValueError, KeyError) as exc:
        _error(exc)
        return
    typer.echo(format_tally(result))
    typer.echo(f"\nTally: {directory / 'tally.json'}")
