"""The pluginbench command line: init, check, reference-mutation, run, status, dashboard, stop,
compare, report, review, measure, judge."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import typer
import yaml
from typer.core import TyperGroup

from pluginbench import (
    __version__,
    control_store,
    coordinator,
    invocation_runtime,
    mutation,
    run_lock,
)
from pluginbench.isolation import RuntimeDriftError
from pluginbench.evaluation import (
    CheckFailed,
    Runner,
    Evaluation,
    load_evaluation,
    measure_evaluation,
    plan_evaluation,
    plan_measure,
    plan_resume,
    plan_review,
    regenerate_report,
    resume_evaluation,
    review_evaluation,
)
from pluginbench.dashboard import serve
from pluginbench.run_status import format_status, run_status

_EXPECTED_RUN_ERRORS = (
    OSError,
    ValueError,
    control_store.CleanupBlocked,
    yaml.YAMLError,
    subprocess.SubprocessError,
    RuntimeDriftError,
)


class _Commands(TyperGroup):
    """Every command. A PluginBench database that cannot be used, such as a corrupt, full or
    locked host control database, ends the command with an error rather than a traceback.
    `stop` handles its own, so that it still signals."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().main(*args, **kwargs)
        except sqlite3.Error as exc:
            path = run_lock.state_root() / control_store.CONTROL_NAME
            typer.echo(
                f"Error: a PluginBench database could not be used: {exc}. The host control "
                f"database is {path}",
                err=True,
            )
            raise SystemExit(2) from exc


app = typer.Typer(
    cls=_Commands,
    help="Compare native instructions, skills, and plugins with vanilla coding agents.",
    no_args_is_help=True,
    invoke_without_command=True,
)
tasks_app = typer.Typer(help="Browse tasks and emit a pinned evaluation YAML block.")
app.add_typer(tasks_app, name="tasks")
judge_app = typer.Typer(help="Blinded pairwise judging of two arms' matched submissions.")
app.add_typer(judge_app, name="judge")
app_commands = typer.Typer(
    help="The coordinator: one per account, outside any terminal, watching this host's "
    "execution. It runs no model and starts no container."
)
app.add_typer(app_commands, name="app")


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
    """Ask before paid work; when no answer can be read, explain how to start instead."""
    try:
        return typer.confirm(question)
    except typer.Abort:
        if sys.stdin.isatty():
            raise  # Ctrl-C at the prompt.
        typer.echo("No answer to confirm with. Pass --yes to start without confirmation.", err=True)
        raise typer.Exit(1) from None


def _error(exc: Exception, *, exit_code: int = 2) -> None:
    if isinstance(exc, CheckFailed):
        typer.echo(_readiness_table(exc.report), err=True)
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(exit_code)


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
    calls = f"Coding attempts: {plan['coding_calls']}   Review calls: {plan['review_calls']}"
    if "verifier_replays" in plan:
        calls += f"   Verifier replays: {plan['verifier_replays']}"
    lines.append(calls)
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


def _traceback_locations(exc: BaseException) -> str:
    """Keep chain types and stack locations without messages, notes, source, or locals."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    lines: list[str] = []
    for error in reversed(chain):
        lines.append("Traceback (most recent call last):\n")
        for frame, lineno in traceback.walk_tb(error.__traceback__):
            lines.append(
                f'  File "{frame.f_code.co_filename}", line {lineno}, in {frame.f_code.co_name}\n'
            )
        lines.append(f"{type(error).__name__}\n")
    return "".join(lines)


def _ended(event: Callable[..., None], exc: BaseException) -> None:
    """Record why work ended early: `stop` (SIGTERM) and Ctrl-C stop it; anything else fails."""
    if isinstance(exc, KeyboardInterrupt):
        event("stopped", reason=str(exc) or "interrupted")
    else:
        fields = {"error": f"{type(exc).__name__}: {exc}"}
        expected = isinstance(exc, _EXPECTED_RUN_ERRORS)
        # JSONDecodeError inherits ValueError, but a failed internal read needs its stack.
        if not expected or isinstance(exc, json.JSONDecodeError):
            fields["traceback"] = _traceback_locations(exc)
        event("failed", **fields)


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
            if isinstance(exc, Exception):
                _error(exc, exit_code=2 if isinstance(exc, _EXPECTED_RUN_ERRORS) else 1)
            raise
        event("finished", report=str(root / "report/report.html"))
    if notify:
        _notify(f"Run finished. Report: {root / 'report/report.html'}")
    typer.echo(f"Report: {root / 'report/report.html'}")
    typer.echo(f"Graphable data: {root / 'report/results.json'}")


def _execution(
    kind: str, target: Path, root: Path | None = None, *, launch_id: str | None = None
) -> AbstractContextManager[run_lock.ExecutionLock]:
    """Own this host's execution lock for `kind` work on `target`, whose evidence is in `root`.

    Every command that runs containers or models takes it, before any run lock. It fails at once
    while another command runs, whatever run that targets, or while cleanup is unverified.
    """
    # A host that cannot contain an operation refuses here, before any call is committed.
    invocation_runtime.require_user_manager()
    owner = run_lock.ExecutionOwner(
        kind=kind,
        eval=str(target.resolve()),
        root=str((root or target).resolve()),
        launch_id=launch_id,
    )
    return control_store.execute(owner)


def _preflight(
    mode: Literal["check", "prepare"],
    evaluation: Evaluation,
    output: Path,
    progress: Callable[[str], None],
) -> Any:
    """Check or prepare `evaluation` into `output` as one preflight operation, whose containers
    start on the Docker daemon commands reach now. A blocking finding raises CheckFailed with the
    report the check saved."""
    from pluginbench import harbor_backend, operations

    root = output.expanduser().resolve()
    try:
        return operations.run(
            "preflight",
            operations.Preflight(mode=mode, config=evaluation, output=root),
            timeout=operations.preflight_budget(evaluation),
            # Read-only: the daemon the preparation's own runtime identity names.
            docker=harbor_backend.runtime_identity(harness=evaluation.runner.harness),
            progress=progress,
        )
    except invocation_runtime.OperationFailed as failed:
        if failed.type != "pluginbench.evaluation.CheckFailed":
            raise
        report = json.loads((root / "check.json").read_text())
        raise CheckFailed(report, report["findings"], root / "check.json") from None


def _detach(
    root: Path, request: coordinator.LaunchRequest, reserved: control_store.Launch
) -> None:
    """Start the run as a worker in its own systemd scope, under the launch reserved for it;
    return once it owns the host and the run's run.lock."""
    launched = coordinator.launch(request, reserved=reserved)
    process = launched.process or {}
    # The worker claims its launch as it takes the host, then takes run.lock.
    deadline = time.monotonic() + invocation_runtime.GATE_SECONDS
    while (run_lock.owner(root) or {}).get("pid") != process.get("pid"):
        if not coordinator.running(launched.launch_id):
            # It claimed its launch and ended before this command saw it own run.lock, with or
            # without taking it: its status says how it ended.
            phase = run_status(root, containers=False)["phase"]
            if phase == "complete":
                typer.echo(f"The background run for {root} already finished.")
                typer.echo(f"Report: {root / 'report/report.html'}")
                return
            raise ValueError(
                f"the background run for {root} has already ended ({phase}); see "
                f"`pluginbench status {root}` and {root / 'run.log'}"
            )
        if time.monotonic() > deadline:
            raise ValueError(
                f"the background run did not take {root / run_lock.LOCK_NAME}; see "
                f"{root / 'run.log'}"
            )
        time.sleep(0.05)
    typer.echo(
        f"Started run {root} in the background (process {process.get('pid')}, in scope "
        f"{launched.scope})."
    )
    typer.echo(f"Watch it: pluginbench status {root} --watch")
    typer.echo(f"Events: {root / 'events.jsonl'}   Log: {root / 'run.log'}")
    typer.echo(f"Stop it: pluginbench stop {root}")
    typer.echo(f"Resume it after a stop or crash: pluginbench resume {root}")


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
    launch: str | None = typer.Option(
        None, "--launch", hidden=True, help="The launch a background worker claims."
    ),
) -> None:
    """Start a run from a config, or resume a run directory.

    Checks run first, in this process. The run then continues in the background unless
    --foreground is set. Every setting comes from the config; a resume uses the saved one.
    """
    lock: run_lock.RunLock | None = None
    try:
        # A `with`, so the host lock sees an exception that leaves it and stays fenced.
        with ExitStack() as owned:
            if as_json and not dry_run:
                raise ValueError("--json prints the dry-run plan; add --dry-run")
            if launch is not None and not (target.is_dir() and foreground and not dry_run):
                raise ValueError(
                    "--launch names a background worker's launch: it takes a run directory "
                    "and --foreground"
                )
            if not target.exists():
                raise ValueError(
                    f"{target} does not exist; give an evaluation YAML or JSON, or a run directory"
                )
            if not dry_run:
                # A host that cannot contain the run refuses before anything is planned.
                invocation_runtime.require_user_manager()
            evaluation = None
            if target.is_dir():
                if output is not None:
                    raise ValueError(
                        f"{target} is a run directory, which resumes in place; "
                        "--output names the directory for a new run from a config"
                    )
                if not dry_run:
                    owned.enter_context(_execution("run", target, launch_id=launch))
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
                owned.enter_context(_execution("run", target, destination))
                try:
                    root = Path(str(_preflight("prepare", evaluation, destination, typer.echo)))
                except BaseException as exc:
                    if notify:
                        _notify(f"Run failed: {str(exc) or type(exc).__name__}")
                    raise
            if foreground:
                _work(root, lock=lock, progress=progress, notify=notify)
            else:
                # The background run takes the host execution lock itself. Its launch is
                # recorded first, while this command still holds the host, so a command that
                # takes the host once it is released fences the launch: the two never race.
                request = coordinator.LaunchRequest("run", root, notify=notify)
                reserved = coordinator.reserve(request)
                owned.close()
                _detach(root, request, reserved)
    except _EXPECTED_RUN_ERRORS as exc:
        _error(exc)


@app.command("resume")
def resume(
    run_dir: Path = typer.Argument(..., help="A run directory to resume."),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
    notify: bool = typer.Option(
        False, "--notify", help="Desktop notifications when the run finishes or fails."
    ),
    foreground: bool = typer.Option(
        False, "--foreground", help="Run in this process instead of in the background."
    ),
) -> None:
    """Resume a run directory: the same as `pluginbench run RUN_DIR`."""
    if not run_dir.is_dir():
        _error(ValueError(f"{run_dir} is not a run directory; resume takes one"))
    run(run_dir, output=None, dry_run=False, as_json=False, yes=yes, notify=notify,
        foreground=foreground, launch=None)


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
        with _execution("preflight", config, destination):
            report = _preflight("check", load_evaluation(config), destination, typer.echo)
        typer.echo("\n".join(_vanilla_cache_lines(report)))
        typer.echo(_readiness_table(report))
        typer.echo(f"Ready. Check: {destination / 'check.json'}")
    except (
        OSError,
        ValueError,
        yaml.YAMLError,
        subprocess.SubprocessError,
        RuntimeDriftError,
        control_store.CleanupBlocked,
    ) as exc:
        _error(exc)


# The timing pilot's published evidence, and what an unpublished one records instead.
PILOT_FILE = "pilot.json"
PILOT_OUTCOME = "outcome.json"


@app.command("reference-mutation")
def reference_mutation(
    profile: Path = typer.Argument(
        ..., help="The task's prepared mutation profile, mutation.json, with its frozen runtime."
    ),
    before: Path = typer.Option(..., "--before", help="The task's frozen starting tree."),
    after: Path = typer.Option(
        ..., "--after", help="The starting tree with the verified oracle change applied."
    ),
    attempt_seconds: int = typer.Option(
        ..., "--attempt-seconds", min=1, help="Each repeat's whole attempt limit."
    ),
    headroom_percent: int = typer.Option(
        ..., "--headroom-percent", min=1,
        help="How far the mutation limit must exceed the projected reference time; 150 = 1.5x.",
    ),
    sample_size: int = typer.Option(
        mutation.SAMPLE_SIZE, "--sample-size", min=1, help="M, the mutants each repeat measures."
    ),
    lanes: int = typer.Option(
        mutation.LANES, "--lanes", min=1, max=mutation.LANES,
        help="Mutant containers each repeat runs at once.",
    ),
    output: Path | None = typer.Option(
        None, "--output", help="New directory for the pilot's repeats and timings."
    ),
) -> None:
    """Time a task's reference mutation for the timing pilot: three repeats, no model calls.

    Pilot only. It writes no qualification receipt and takes no pins, so `freeze` can never
    read what it measures. The receipts come from pins a verified suite manifest supplies.
    """
    from pluginbench import operations
    from pluginbench.measurement_profiles import CoverageSettings
    from pluginbench.reference_mutation import Contract

    try:
        # Everything is checked before the host is taken: a repeat can run for hours.
        destination = (output or _new_run_directory("reference-")).resolve()
        if destination.exists():
            raise ValueError(f"{destination} already exists; give a new --output")
        trees = before.resolve(), after.resolve()
        for tree in trees:
            if not tree.is_dir():
                raise ValueError(f"{tree} is not a directory")
            if destination.is_relative_to(tree):
                raise ValueError(f"{destination} is inside {tree}, which it would change")
        config = json.loads(profile.read_text())
        identity = config.get("native_task_identity") if isinstance(config, dict) else None
        if not isinstance(identity, dict):
            raise ValueError(f"{profile} is not a prepared mutation profile")
        # The worker reads the settings with their defaults filled in, as validated here.
        coverage = CoverageSettings.model_validate(config.get("coverage"))
        config = {**config, "coverage": coverage.model_dump(mode="json")}
        # The repeats run in a staging directory, published only once cleanup is verified.
        staging = destination / "staging"
        arguments = operations.Reference(
            before=trees[0], after=trees[1], config=config,
            contract=Contract(
                headroom_percent=headroom_percent, sample_size=sample_size, lanes=lanes
            ),
            destination=staging, attempt_ms=1000 * attempt_seconds,
        )
        # The frozen runtime the profile saved: its containers start on that daemon only.
        runtime = mutation.measurement_identity(identity)["isolation"]
        assert isinstance(runtime, dict)  # `measurement_identity` requires one.
        invocation_runtime.require_recoverable(runtime)
        with _execution("preflight", profile, destination):
            destination.mkdir(parents=True)
            try:
                # It returns only once the scope is gone and no labelled container is left.
                facts = operations.run(
                    "reference", arguments,
                    timeout=operations.reference_budget(arguments.attempt_ms), docker=runtime,
                )
            except BaseException as exc:
                _write_json(destination / PILOT_OUTCOME, _pilot_outcome(exc))
                raise
            staging.rename(destination / "repeats")
            _write_json(destination / PILOT_FILE, {"kind": "reference-mutation-pilot",
                                                   "facts": facts})
        for line in _reference_summary(facts):
            typer.echo(line)
        typer.echo(f"Pilot timings: {destination / PILOT_FILE}. Not a qualification receipt.")
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        RuntimeDriftError,
        control_store.CleanupBlocked,
    ) as exc:
        _error(exc)


def _pilot_outcome(exc: BaseException) -> dict[str, object]:
    """Why a pilot published nothing. A deadline, an unverified cleanup or a launch failure is
    infrastructure; the staged repeats stay for inspection and prove nothing."""
    infrastructure = isinstance(exc, (
        invocation_runtime.OperationDeadlineExceeded, control_store.CleanupBlocked,
    )) or (isinstance(exc, invocation_runtime.OperationFailed) and not exc.type)
    return {
        "published": False,
        "infrastructure": infrastructure,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _write_json(path: Path, value: object) -> None:
    """Write `value` whole or not at all: readers never see half a file."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(partial, path)


def _reference_summary(facts: dict[str, Any]) -> list[str]:
    """One line per reference repeat, in seconds from its start: each baseline's time and
    suite status, first dispatch, when the M = 10 prefix and the whole sample settled, and
    whether the repeat completed its required work, was censored by a limit, or neither."""
    sample = int(facts["contract"]["sample_size"])

    def seconds(ms: int | None) -> str:
        return "unsettled" if ms is None else f"{ms / 1000:.1f} s"

    def settled(sites: list[dict[str, Any]], count: int) -> int | None:
        chosen = [site for site in sites if site["site"] < count]
        if len(chosen) < count or any(site["settled_ms"] is None for site in chosen):
            return None
        return max((site["settled_ms"] for site in chosen), default=0)

    lines = []
    for index, repeat in enumerate(facts["repeats"]):
        sites, selected = repeat["sites"], len(repeat["selected"])
        baselines = ", ".join(_baseline_state(run) for run in repeat["baseline"])
        parts = [
            f"repeat {index}: {selected} of {repeat['candidate_count']} candidates",
            f"baselines {baselines or 'none'}",
            f"first mutant {seconds(min((s['dispatched_ms'] for s in sites), default=None))}",
        ]
        if selected > mutation.V1_SAMPLE_SIZE:
            parts.append(
                f"first {mutation.V1_SAMPLE_SIZE} settled "
                f"{seconds(settled(sites, mutation.V1_SAMPLE_SIZE))}"
            )
        parts.append(f"all settled {seconds(settled(sites, selected))}")
        parts.append(f"required work {seconds(repeat['required_settled_ms'])}")
        failure = repeat["infrastructure_failure"]
        if failure is not None:
            parts.append(f"censored ({failure})")
        else:
            parts.append("incomplete" if repeat["required_settled_ms"] is None else "complete")
        lines.append("; ".join(parts))
    lines.append(f"M = {sample}, {facts['contract']['lanes']} lanes. No model calls.")
    return lines


def _baseline_state(run: dict[str, Any]) -> str:
    """A baseline run's time and how its suite ended: green only on suite status 0."""
    if not run["dispatched"]:
        return "not run"
    took = f"{run['elapsed_ms'] / 1000:.1f} s"
    if run["timed_out"]:
        return f"{took} timed out"
    status = run["suite_status"]
    return f"{took} {'green' if status == 0 else f'red (suite status {status})'}"


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


@app.command("dashboard")
def dashboard(
    roots: list[Path] = typer.Argument(..., help="Directories that hold run directories."),
    port: int = typer.Option(8765, "--port", help="Port on 127.0.0.1."),
) -> None:
    """Serve a local, read-only web view of the runs under ROOTS until Ctrl-C."""
    try:
        server = serve(roots, port=port)
    except (OSError, ValueError) as exc:
        _error(exc)
        return
    typer.echo(f"Dashboard: http://127.0.0.1:{server.server_address[1]}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # Ctrl-C is how the dashboard ends.
    finally:
        server.server_close()


@app.command("stop")
def stop(
    run: Path = typer.Argument(..., help="A run directory started by `pluginbench run`."),
) -> None:
    """Stop a run: record the stop for its background launch, then SIGTERM only the process
    group its run.lock records; nothing else. The worker checks the recorded stop before it
    admits each new job, so a lost signal cannot let it start another."""
    unrecorded: Exception | None = None
    try:
        requested = coordinator.request_stop(run)
    except (OSError, sqlite3.Error) as exc:
        requested, unrecorded = [], exc
        typer.echo(f"Error: the stop could not be recorded ({exc}); signalling only.", err=True)
    if requested:
        typer.echo(f"Stop recorded for {run}: its background launch admits no further job.")
    try:
        owner = run_lock.stop(run)
    except (OSError, ValueError) as exc:
        if not requested:
            _error(exc)
        typer.echo(f"Nothing signalled: {exc}")
        return
    if owner is None:
        typer.echo(f"No live process holds {run / run_lock.LOCK_NAME}; nothing signalled.")
    else:
        typer.echo(
            f"Sent SIGTERM to process group {owner['pgid']}. Attempts already coding finish "
            "and are kept; no new attempt starts."
        )
    if unrecorded is not None:
        raise typer.Exit(1)


@app_commands.callback(invoke_without_command=True)
def app_start(
    context: typer.Context,
    foreground: bool = typer.Option(
        False, "--foreground", help="Run the coordinator in this terminal, for debugging."
    ),
) -> None:
    """Start the coordinator in the background, or show the one already running."""
    if context.invoked_subcommand is not None:
        return
    try:
        if foreground:
            raise typer.Exit(coordinator.run_foreground())
        found = coordinator.ensure_started()
    except (OSError, ValueError) as exc:
        _error(exc)
        return
    typer.echo(
        f"Coordinator running: process {found['pid']}, launch {found['launch_id']}, "
        f"since {found['started_at']}."
    )
    typer.echo(f"Log: {run_lock.state_root() / coordinator.COORDINATOR_LOG}")
    typer.echo("Stop it: pluginbench app stop")


@app_commands.command("stop")
def app_stop() -> None:
    """Stop the coordinator. A worker it started keeps running in its own scope, and the next
    coordinator adopts it; stop a run with `pluginbench stop RUN_DIR`."""
    try:
        found = coordinator.stop()
    except (OSError, ValueError) as exc:
        _error(exc)
        return
    if found is None:
        typer.echo("No coordinator runs on this host.")
    else:
        typer.echo(f"Stopped the coordinator (process {found['pid']}). Workers keep running.")


@app_commands.command("status")
def app_status(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object."),
) -> None:
    """Show the coordinator, who executes on this host, and background launches; changes no record."""
    try:
        current = coordinator.status()
    except (OSError, ValueError) as exc:
        _error(exc)
        return
    if as_json:
        typer.echo(json.dumps(current, indent=2))
        return
    found = current["coordinator"]
    typer.echo(
        f"Coordinator: process {found['pid']}, launch {found['launch_id']}"
        + (f", monitoring launch {found['adopted']}" if found.get("adopted") else "")
        if found
        else "Coordinator: not running. Start it: pluginbench app"
    )
    execution = current["execution"]
    owner = execution["owner"] or {}
    if not execution["held"]:
        typer.echo("Execution: idle")
    elif execution["verified"]:
        typer.echo(
            f"Execution: {owner.get('kind')} for {owner.get('root')} (process {owner.get('pid')}"
            + (f", launch {owner['launch_id']})" if owner.get("launch_id") else ", external run)")
        )
    else:
        typer.echo(
            f"Execution: held by a process PluginBench cannot verify; see "
            f"`lslocks | grep {run_lock.state_root() / run_lock.EXECUTION_LOCK}`"
        )
    if current["reconciling"]:
        typer.echo(f"Reconciling: {current['reconciling']}")
    for launch in current["launches"]:
        if launch["kind"] != "coordinator":
            typer.echo(
                f"Launch {launch['launch_id']}: {launch['kind']} for {launch['target']}, "
                f"{launch['status']}, desired {launch['desired']}"
            )


@app_commands.command("cleanup")
def app_cleanup(
    confirm_stopped: str | None = typer.Option(
        None,
        "--confirm-stopped",
        metavar="ROOT",
        help=(
            "Confirm you checked that nothing the crashed calls under ROOT started still runs. "
            "ROOT is the run root a host block names (it need not exist any more), or its "
            "obligation ID."
        ),
    ),
) -> None:
    """Verify everything a crashed command may have left, so the host can execute again. No
    model call and no new work.

    With --confirm-stopped ROOT, ROOT must be exactly the root a host block names, or its
    obligation ID. Every check runs again first. Only checks that still cannot tell, and that
    were recorded before this PluginBench could prove them, are confirmed: never one that finds
    something running, and never one this PluginBench recorded, which waits for Docker or
    systemd to answer again. Each confirmation is recorded with its time.
    """
    try:
        if confirm_stopped is None:
            state = str(run_lock.state_root())
            owner = run_lock.ExecutionOwner(kind="recovery", eval=state, root=state)
            with control_store.execute(owner):
                pass
            typer.echo("Every check passed; the host is clean.")
            return
        target = str(Path(confirm_stopped).expanduser().resolve())
        owner = run_lock.ExecutionOwner(kind="recovery", eval=target, root=target)
        with run_lock.hold_execution(owner) as lock:
            confirmed, at, remaining = control_store.confirm_stopped(lock, confirm_stopped)
    except (OSError, ValueError, control_store.CleanupBlocked) as exc:
        _error(exc)
        return
    for check, reason in confirmed:
        typer.echo(f"Confirmed stopped at {at}: {check['id']} ({reason.split('. Once')[0]})")
    if not confirmed:
        typer.echo(f"Every check for {confirm_stopped} passed; nothing needed confirming.")
    typer.echo(
        f"{remaining} other run{'s' if remaining != 1 else ''} still wait for cleanup."
        if remaining
        else "The host is unblocked."
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
    reviewer_harness: str | None = typer.Option(
        None, "--reviewer-harness", help="Review with this harness instead of the frozen one."
    ),
    reviewer_model: str | None = typer.Option(
        None, "--reviewer-model", help="Review with this model instead of the frozen one."
    ),
    reviewer_effort: str | None = typer.Option(
        None, "--reviewer-effort", help="Review with this effort instead of the frozen one."
    ),
    select: bool = typer.Option(
        True, "--select/--no-select", help="Select the new revision for the report."
    ),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
) -> None:
    """Review every submission again with the run's frozen reviewer and criteria.

    The three --reviewer options together review with another harness, model and effort.
    Earlier review revisions stay in evidence. The new one is selected and the report rebuilt,
    unless --no-select is set.
    """
    try:
        override = (reviewer_harness, reviewer_model, reviewer_effort)
        reviewer = None
        if any(value is not None for value in override):
            if None in override:
                raise ValueError(
                    "--reviewer-harness, --reviewer-model and --reviewer-effort go together"
                )
            reviewer = Runner.model_validate(
                {"harness": reviewer_harness, "model": reviewer_model, "reasoning": reviewer_effort}
            )
        with _execution("review", run), run_lock.hold(run):
            plan = plan_review(run, revision)
            by = (
                f" by {reviewer.harness} {reviewer.model} ({reviewer.reasoning})"
                if reviewer
                else ""
            )
            typer.echo(f"{plan['review_calls']} review calls as review revision {revision}{by}.")
            if not yes and not _confirm("Start review?"):
                raise typer.Exit()
            _review(run, revision, reviewer=reviewer, select=select)
    except (OSError, ValueError, subprocess.SubprocessError, control_store.CleanupBlocked) as exc:
        _error(exc)


def _review(run: Path, revision: str, *, reviewer: Runner | None, select: bool) -> None:
    """Run a planned review while this process owns the run, recording it in events.jsonl."""
    with _events(run) as event:
        event("started", command="review", pid=os.getpid(), revision=revision)

        def logged(message: str) -> None:
            event("progress", message=message)
            typer.echo(message)

        try:
            recorded = review_evaluation(
                run, revision, reviewer=reviewer, select=select, progress=logged
            )
        except BaseException as exc:
            _ended(event, exc)
            raise
        event("finished", command="review", revision=recorded)
    if not select:
        typer.echo(f"Recorded review revision {recorded}; the selected revision is unchanged.")
        return
    typer.echo(f"Selected review revision {recorded}.")
    typer.echo(f"Report: {run / 'report/report.html'}")


@app.command("measure")
def measure(
    run: Path = typer.Argument(..., help="A finished run directory."),
    measurements: str = typer.Option(
        ...,
        "--measurements",
        help=(
            "Comma-separated measures to add: cyclomatic, cognitive, dead_code, coverage, "
            "mutation, iterations."
        ),
    ),
    revision: str = typer.Option(
        ..., "--revision", help="Name for the new measurement revision, such as measurements-2."
    ),
    coverage_profiles: Path | None = typer.Option(
        None,
        "--coverage-profiles",
        help="A YAML coverage_profiles mapping to add to the run's settings, with coverage.",
    ),
    tasks: str | None = typer.Option(
        None, "--tasks", help="Comma-separated task ids to measure; only with mutation alone."
    ),
    yes: bool = typer.Option(False, "--yes", help="Start without confirmation."),
) -> None:
    """Add measures to a finished run from its saved submissions, with no model calls.

    The new measurement revision is selected and the report rebuilt. Earlier revisions stay in
    evidence and measurement history. If it stops while collecting, run the same command again:
    it keeps every submission already measured and measures only the rest.
    """
    names = [name.strip() for name in measurements.split(",") if name.strip()]
    task_ids = (
        sorted(task.strip() for task in tasks.split(",") if task.strip())
        if tasks is not None else None
    )
    try:
        with _execution("measure", run), run_lock.hold(run):
            plan = plan_measure(run, names, revision, coverage_profiles, tasks=task_ids)
            collected, carried = plan["collected"], plan["carried"]
            assert isinstance(collected, list) and isinstance(carried, list)
            carries = f"; carries {', '.join(carried)} forward unchanged" if carried else ""
            scope = ""
            if task_ids is not None:
                excluded = plan["excluded_tasks"]
                assert isinstance(excluded, list)
                scope = (
                    f"; measures tasks: {', '.join(task_ids)}; leaves out: "
                    f"{', '.join(excluded) or 'none'}"
                )
            typer.echo(
                f"{'Resumes' if plan['resume'] else 'Adds'} {', '.join(names)} on "
                f"{plan['tasks']} tasks as measurement revision {revision}, collecting "
                f"{', '.join(collected)} from saved submissions{carries}{scope}. 0 model calls."
            )
            if not yes and not _confirm("Start measurement?"):
                raise typer.Exit()
            _measure(run, names, revision, coverage_profiles, tasks=task_ids)
    except (
        OSError,
        ValueError,
        yaml.YAMLError,
        subprocess.SubprocessError,
        control_store.CleanupBlocked,
    ) as exc:
        _error(exc)


def _measure(
    run: Path,
    names: list[str],
    revision: str,
    coverage_profiles: Path | None,
    *,
    tasks: list[str] | None = None,
) -> None:
    """Run a planned measurement while this process owns the run, recording it in events.jsonl."""
    with _events(run) as event:
        event("started", command="measure", pid=os.getpid(), revision=revision)

        def logged(message: str) -> None:
            event("progress", message=message)
            typer.echo(message)

        try:
            measure_evaluation(
                run, names, revision, coverage_profiles=coverage_profiles, tasks=tasks,
                progress=logged,
            )
        except BaseException as exc:
            _ended(event, exc)
            raise
        event("finished", command="measure", revision=revision)
    typer.echo(f"Selected measurement revision {revision}.")
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
        with _execution("judge", directory):
            failed = run_judges(
                directory, judges=judges, timeout=timeout, jobs=jobs, progress=typer.echo
            )
    except (OSError, ValueError, KeyError, control_store.CleanupBlocked) as exc:
        _error(exc)
        return
    if failed:
        typer.echo(
            f"{failed} judge calls failed; run again to retry them, up to three calls per judge "
            "and task.",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(f"All judge calls complete. Tally: pluginbench judge tally {directory}")


@judge_app.command("tally")
def judge_tally(
    directory: Path = typer.Argument(..., help="A directory whose judge calls are complete."),
    require_bound: bool = typer.Option(
        False,
        help="Refuse legacy verdicts without input bindings or that need a rebind.",
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
