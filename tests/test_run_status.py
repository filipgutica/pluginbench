"""`pluginbench status` reads a run directory and never writes to it."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pluginbench import cli, control_store, coordinator, harbor_backend, run_lock, run_status
from pluginbench.control_store import ControlStore
from pluginbench.ledger import LEDGER_NAME, Ledger

USAGE = {
    "input_tokens": 1000,
    "cached_input_tokens": 600,
    "output_tokens": 50,
    "reasoning_output_tokens": 10,
    "total_tokens": 1050,
}
ENTRIES = [
    {"id": "a1", "task_id": "task-a", "arm": "baseline", "attempt": 1},
    {"id": "a2", "task_id": "task-a", "arm": "workflow", "attempt": 1},
    {"id": "a3", "task_id": "task-a", "arm": "baseline", "attempt": 2},
    {"id": "a4", "task_id": "task-a", "arm": "workflow", "attempt": 2},
]


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


# Where the grade stage keeps its index, as `evaluation.json` names it.
INDEX = "source-only/filter-3/index.json"


def _grade(evidence: Path, attempt: str, grading: dict[str, Any]) -> None:
    """Record one attempt's grade, or its grade-stage infrastructure error, in the index."""
    path = evidence / INDEX
    index: dict[str, Any] = (
        json.loads(path.read_text())
        if path.is_file()
        else {"grade_contract": 1, "records": {}, "pending": {}}
    )
    if grading.get("status") == "infra":
        index["pending"][attempt] = {"handoff": grading["reason"]}
    else:
        record = f"source-only/filter-3/{attempt}/handoff/grade.json"
        _write(evidence / record, grading)
        index["records"][attempt] = {"handoff": {"path": record, "sha256": "sha256:fixture"}}
    _write(path, index)


def _passed(passed: bool, **attributes: bool) -> dict[str, Any]:
    return {
        "grade_contract": 1,
        "status": "complete",
        "passed": passed,
        "fail_reason": None,
        "attributes": {"timed_out": False, "agent_error": False, **attributes},
    }


def _finished(evidence: Path, attempt: str, *, behavior: bool | None, usage: object) -> None:
    handoff = evidence / "attempts" / attempt / "handoff"
    _write(handoff / "execution.json", {"usage": usage, "elapsed_seconds": 600})
    if behavior is not None:
        _grade(evidence, attempt, _passed(behavior))


def test_an_attempt_whose_coding_hit_an_infrastructure_error_reads_infra(
    tmp_path: Path,
) -> None:
    root = _run(tmp_path / "run", states={**STATES, "a3": "interrupted"}, ledger=True)
    handoff = root / "evidence/attempts/a3/handoff"
    _write(handoff / "execution.json", {"usage": None, "elapsed_seconds": 600})
    _write(handoff / "infrastructure-error.json", {
        "grade_contract": 1, "stage": "coding", "reason": "the Harbor worker was lost",
    })

    status = run_status.run_status(root)
    row = next(row for row in status["attempts"] if row["id"] == "a3")

    assert (row["state"], row["outcome"]) == ("interrupted", "infra")
    assert row["outcome_reason"] == "the Harbor worker was lost"
    assert status["outcomes"] == {"pass": 1, "fail": 1, "infra": 1}


def _run(root: Path, *, states: dict[str, str], ledger: bool) -> Path:
    """A run directory as Phase 3 writes it: a1 reused and passing, a2 failing, a3 running."""
    evidence = root / "evidence"
    _write(
        root / "evaluation.json",
        {
            "name": "fixture",
            "measurements": ["tokens"],
            "reviewer": None,
            "measurement_required": False,
            "measurement_revision": None,
            "review_revision": None,
            "source_only_grades": {"path": INDEX, "sha256": "sha256:fixture"},
        },
    )
    _write(
        evidence / "plan.json",
        {
            "grade_contract": 1,
            "entries": ENTRIES,
            "baseline_cache": {"a1": {"status": "hit"}, "a3": {"status": "miss"}},
        },
    )
    _write(
        evidence / "run.json",
        {
            "status": "running",
            "error": None,
            "attempt_states": states,
            "baseline_origin": {"a1": {"status": "hit"}},
        },
    )
    _finished(evidence, "a1", behavior=True, usage=USAGE)
    _finished(evidence, "a2", behavior=False, usage=USAGE)
    if ledger:
        Ledger.create(evidence / LEDGER_NAME, ENTRIES, states).close()
    return root


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }


STATES = {"a1": "complete", "a2": "complete", "a3": "running", "a4": "pending"}


@pytest.mark.parametrize(("version", "arm"), [(1, "baseline"), (2, "vanilla")])
def test_status_reads_both_report_arm_names(tmp_path: Path, version: int, arm: str) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=False)
    _write(root / "report/results.json", {
        "schema_version": version,
        "attempts": [{"id": "a1", "guidance": arm}],
        "observations": [{
            "attempt_id": "a1", "arm": arm, "metric": "Input tokens",
            "value": 42, "status": "measured", "unit": "tokens",
        }],
    })
    before = _tree(root)
    result = CliRunner().invoke(cli.app, ["status", str(root), "--json"])
    assert result.exit_code == 0, result.output
    attempt = next(row for row in json.loads(result.stdout)["attempts"] if row["id"] == "a1")
    assert attempt["arm"] == "vanilla"
    assert attempt["measures"]["Input tokens"] == {
        "value": 42, "status": "measured", "unit": "tokens", "reason": None,
    }
    assert _tree(root) == before


def test_an_old_run_without_a_ledger_or_owner_reads_as_stopped_and_is_not_touched(
    tmp_path: Path,
) -> None:
    # The interrupted pilot: run.json still says running, but no process owns the run.
    root = _run(tmp_path / "run", states=STATES, ledger=False)
    before = _tree(root)

    status = run_status.run_status(root)
    table = CliRunner().invoke(cli.app, ["status", str(root)])
    as_json = CliRunner().invoke(cli.app, ["status", str(root), "--json"])

    assert _tree(root) == before
    assert not (root / "evidence" / LEDGER_NAME).exists()
    assert table.exit_code == 0 and as_json.exit_code == 0, table.output + as_json.output
    assert json.loads(as_json.stdout) == {
        **status,
        "heartbeat": json.loads(as_json.stdout)["heartbeat"],
    }
    assert status["phase"] == "stopped" and not status["active"]
    assert status["states_source"] == "run.json"
    assert status["states"] == {"complete": 2, "running": 1, "pending": 1}
    assert "Phase: stopped" in table.stdout


def test_status_counts_outcomes_tokens_reuse_and_discarded_attempts(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    evidence = root / "evidence"
    _write(
        evidence / "discarded/d1.json",
        {
            "id": "d1",
            "task_id": "task-a",
            "arm": "workflow",
            "attempt": 2,
            "slot": 3,
            "replaced_by": "a4",
            "stages": {
                "handoff": {
                    "execution": {"path": "attempts/d1/handoff/execution.json", "sha256": "x"},
                    "work": {"tokens": {"total_tokens": 7}},
                }
            },
        },
    )
    _write(evidence / "attempts/d1/handoff/execution.json", {"usage": {"total_tokens": 7}})

    with run_lock.hold(root):
        status = run_status.run_status(root)

    assert status["schema_version"] == run_status.SCHEMA_VERSION == 3
    assert status["phase"] == "coding" and status["active"]
    assert status["states_source"] == "ledger"
    assert status["outcomes"] == {"pass": 1, "fail": 1}
    assert set(status["arms"]) == {"vanilla", "workflow"}
    assert status["arms"]["vanilla"]["reused"] == 1
    assert status["reuse"] == {"vanilla_attempts": 2, "planned_hits": 1, "reused": 1}
    # Spent tokens leave out the reused vanilla attempt and include the discarded one; the
    # running attempt has no saved usage yet, so the total is a lower bound.
    assert status["tokens"]["total_tokens"] == 1050 + 7
    assert status["tokens"]["lower_bound"] is True
    assert status["arms"]["vanilla"]["tokens"]["total_tokens"] == 1050
    assert [row["id"] for row in status["discarded"]] == ["d1"]
    rows = {row["id"]: row for row in status["attempts"]}
    assert rows["a2"]["outcome"] == "fail" and rows["a3"]["outcome"] is None
    assert rows["a3"]["tokens_status"] == "unknown"
    assert rows["a4"]["tokens_status"] == "not_started"
    assert rows["a1"]["minutes"] == 10.0


def test_a_lower_bound_receipt_marks_the_arm_and_an_unknown_outcome_keeps_its_reason(
    tmp_path: Path,
) -> None:
    root = _run(tmp_path / "run", states={**STATES, "a3": "complete"}, ledger=True)
    _finished(
        root / "evidence",
        "a3",
        behavior=None,
        usage=None,
    )
    execution = root / "evidence/attempts/a3/handoff/execution.json"
    _write(execution, {"usage": None, "usage_accounting": {"observed_usage_lower_bound": USAGE}})

    status = run_status.run_status(root)

    vanilla = status["arms"]["vanilla"]
    assert vanilla["tokens"]["total_tokens"] == 2100 and vanilla["tokens"]["lower_bound"]
    assert status["arms"]["workflow"]["tokens"]["lower_bound"] is False
    row = next(row for row in status["attempts"] if row["id"] == "a3")
    assert (row["outcome"], row["outcome_reason"]) == (None, "not graded yet")
    assert "2.1k+" in run_status.format_status(status)


def test_status_names_a_crashed_owner_of_this_run_that_blocks_the_next_command(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    # A run killed before it released the host lock: schema 1 records always count as unreleased.
    crashed = {"schema": 1, "pid": 7, "acquired_at": "earlier", "kind": "run",
               "eval": str(root.resolve()), "root": str(root.resolve())}
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text(json.dumps(crashed))

    status = run_status.run_status(root)

    host = status["host"]
    assert host["error"] is None and host["execution"] is None and host["launches"] == []
    assert host["blocked"] == [
        {"name": "7-earlier", "kind": "run", "root": crashed["root"], "this_run": True,
         "problem": None}
    ]
    shown = run_status.format_status(status)
    assert "Host: no coordinator; idle" in shown
    assert "Blocked: crashed run for this run (7-earlier) must be verified first" in shown


def test_status_names_the_execution_owner_running_this_run(
    tmp_path: Path, host_execution: run_lock.ExecutionLock
) -> None:
    root = _run(tmp_path, states=STATES, ledger=True)

    status = run_status.run_status(root)

    assert status["host"]["blocked"] == []
    assert status["host"]["execution"]["this_run"] is True
    assert "; executing this run (run, process" in run_status.format_status(status)
    assert run_lock.unreleased() is None  # Never the holder's own record, as a crash.


def test_a_released_record_never_names_the_holder(
    tmp_path: Path, isolated_host_state: Path, host_execution: run_lock.ExecutionLock
) -> None:
    # Between taking the lock and publishing its new record, a holder's lock descriptor can
    # match the record it released earlier.
    path = isolated_host_state / run_lock.EXECUTION_OWNER
    path.write_text(json.dumps({**json.loads(path.read_text()), "state": "released"}))

    snapshot = run_lock.execution_owner()

    assert snapshot.held and not snapshot.verified


def test_status_names_the_fence_an_unreadable_owner_record_leaves(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    (isolated_host_state / run_lock.EXECUTION_OWNER).write_text("{")

    status = run_status.run_status(root)

    [fence] = status["host"]["blocked"]
    assert fence["root"] == str(isolated_host_state) and fence["this_run"] is False
    assert "cannot be read" in fence["problem"]
    assert (
        f"`pluginbench app cleanup --confirm-stopped {isolated_host_state}`"
        in run_status.format_status(status)
    )


def test_status_lists_only_this_runs_launches(tmp_path: Path, isolated_host_state: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    with ControlStore.open(isolated_host_state) as store:
        for launch_id, target in (("mine", root.resolve()), ("other", tmp_path / "other")):
            store.create_launch(coordinator._ticket(launch_id, "run", "cli", target, "scope"))

    status = run_status.run_status(root)

    assert status["host"]["launches"] == [
        {"launch_id": "mine", "kind": "run", "status": "launching", "desired": "run"}
    ]
    assert "Launch mine: run, launching, desired run" in run_status.format_status(status)


# A writer killed mid-transaction leaves a hot journal (see test_control_store).
_KILLED_MID_WRITE = """
import os, signal, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size = 1")
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE host_state SET reconciling = 'half written'")
db.execute("CREATE TABLE spill (x)")
db.executemany("INSERT INTO spill VALUES (?)", [("x" * 1000,) for _ in range(200)])
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_status_leaves_a_killed_control_write_for_app_status_to_roll_back(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    database = isolated_host_state / control_store.CONTROL_NAME
    with ControlStore.open(isolated_host_state):
        pass
    killed = subprocess.run([sys.executable, "-c", _KILLED_MID_WRITE, str(database)], check=False)
    assert killed.returncode == -signal.SIGKILL
    journal = database.with_name(f"{database.name}-journal")
    before = (database.read_bytes(), journal.read_bytes())

    status = run_status.run_status(root)

    assert (database.read_bytes(), journal.read_bytes()) == before
    assert "`pluginbench app status` rolls it back" in status["host"]["error"]
    assert status["phase"] == "stopped"  # The run's own status still reads.


def test_a_control_database_without_its_host_row_is_reported_not_raised(
    tmp_path: Path, isolated_host_state: Path
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    with ControlStore.open(isolated_host_state):
        pass
    with sqlite3.connect(isolated_host_state / control_store.CONTROL_NAME) as db:
        db.execute("DELETE FROM host_state")

    assert "no host_state row" in run_status.run_status(root)["host"]["error"]


def test_a_failed_run_shows_its_error(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    (root / "events.jsonl").write_text(
        json.dumps({"at": "2026-11-07T00:00:00+00:00", "event": "started"})
        + "\n"
        + json.dumps({"at": "2026-11-07T00:01:00+00:00", "event": "failed", "error": "boom"})
        + "\n"
    )

    status = run_status.run_status(root, now=datetime(2026, 11, 7, 0, 2, tzinfo=UTC))

    assert status["phase"] == "failed" and status["error"] == "boom"
    assert status["last_event"]["event"] == "failed"
    assert status["heartbeat"] is not None


def test_an_unreadable_ledger_falls_back_to_run_json(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=False)
    (root / "evidence" / LEDGER_NAME).write_bytes(b"not a database")

    status = run_status.run_status(root)

    assert status["states_source"] == "run.json" and len(status["attempts"]) == 4


def test_the_status_reader_cannot_write_the_ledger(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    ledger = Ledger.read(root / "evidence" / LEDGER_NAME)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ledger.set_state("a4", "running")
    finally:
        ledger.close()


def test_watch_redraws_until_the_run_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    owners: list[dict[str, Any] | None] = [
        {"pid": 1, "pgid": 1, "started_at": "t"},
        {"pid": 1, "pgid": 1, "started_at": "t"},
        None,
    ]
    monkeypatch.setattr(run_lock, "owner", lambda path: owners.pop(0))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    result = CliRunner().invoke(cli.app, ["status", str(root), "--watch"])

    assert result.exit_code == 0, result.output
    assert not owners
    assert result.stdout.count("Phase: coding") == 2
    assert result.stdout.count("Phase: stopped") == 1


@pytest.mark.parametrize(
    "arguments,message",
    [
        (["--watch", "--json"], "run status --json repeatedly"),
        ([], "not a prepared run"),
    ],
)
def test_status_errors_say_what_to_do(tmp_path: Path, arguments: list[str], message: str) -> None:
    result = CliRunner().invoke(cli.app, ["status", str(tmp_path), *arguments])
    assert result.exit_code == 2 and message in result.stderr


@pytest.mark.parametrize("command, phase", [("review", "reviewing"), ("measure", "measuring")])
def test_a_finished_run_reads_as_revised_while_a_command_owns_it(
    tmp_path: Path, command: str, phase: str
) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    run = json.loads((root / "evidence/run.json").read_text())
    _write(root / "evidence/run.json", {**run, "status": "complete"})
    _write(root / "report/report.html", "")
    assert run_status.run_status(root)["phase"] == "complete"
    (root / "events.jsonl").write_text(
        json.dumps({"at": "2026-11-07T00:00:00+00:00", "event": "started", "command": command})
        + "\n"
    )

    with run_lock.hold(root):
        assert run_status.run_status(root)["phase"] == phase


def test_a_stopped_run_reads_as_stopped_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `stop` sends SIGTERM, which the run lock turns into KeyboardInterrupt in the worker.
    root = _run(tmp_path / "run", states=STATES, ledger=True)

    def stopped(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt("stopped by SIGTERM")

    monkeypatch.setattr(cli, "resume_evaluation", stopped)
    with pytest.raises(KeyboardInterrupt):
        cli._work(root, lock=None, progress=lambda message: None, notify=False)

    status = run_status.run_status(root)
    assert status["phase"] == "stopped" and status["error"] is None
    assert status["last_event"]["event"] == "stopped"
    assert status["resume_command"] == f"pluginbench run {root}"
    # Nothing owns the run, so its recorded `running` attempt is shown as interrupted.
    assert "  task-a  vanilla   pass interrupted" in run_status.format_status(status)


def _killed(root: Path, **started: str) -> None:
    """A worker that began and never recorded how it ended, as after SIGKILL or power loss."""
    (root / "events.jsonl").write_text(
        json.dumps({"event": "started", **started})
        + "\n"
        + json.dumps({"event": "progress", "message": "Coding 3/4: task-a"})
        + "\n"
    )


def test_a_killed_run_reads_as_interrupted_and_says_how_to_resume(tmp_path: Path) -> None:
    # The space checks that the printed command can be pasted into a shell.
    root = _run(tmp_path / "my run", states=STATES, ledger=True)
    _killed(root)

    status = run_status.run_status(root)
    shown = run_status.format_status(status)

    assert status["phase"] == "interrupted" and status["error"] is None
    assert status["resume_command"] == f"pluginbench run '{root}'"
    assert "Phase: interrupted (no exit record)" in shown
    assert f"Resume: pluginbench run '{root}'" in shown
    # The summary agrees with the table: no process is running the recorded `running` attempt.
    assert "Attempts: 4 slots: 2 complete, 1 interrupted, 1 pending" in shown
    with run_lock.hold(root):
        owned = run_status.run_status(root)
        assert owned["phase"] == "coding" and owned["resume_command"] is None
        assert "1 running" in run_status.format_status(owned)


def test_a_killed_run_names_the_containers_it_left_running_until_a_resume_owns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    _killed(root)
    task = root / "evidence/frozen/suite/0/task"
    task.mkdir(parents=True)
    project = "pluginbench-" + "a" * 32 + "__env"
    rows = [
        [cid, f"{project}-{service}-1", project, str(task.resolve() / "environment")]
        for cid, service in (("c1", "main"), ("c2", "sidecar"))
    ]
    # Another run's trial: never named.
    rows.append(["c3", "x", "pluginbench-" + "b" * 32 + "__env", str(tmp_path / "other/task")])
    queries: list[tuple[str, ...]] = []

    def docker(*arguments: str, **kwargs: object) -> str:
        queries.append(arguments)
        return "".join(json.dumps(row) + "\n" for row in rows)

    monkeypatch.setattr(harbor_backend, "_docker", docker)

    status = run_status.run_status(root)
    shown = run_status.format_status(status)
    as_json = CliRunner().invoke(cli.app, ["status", str(root), "--json"])

    assert status["left_running"] == {
        "checked": True,
        "error": None,
        "containers": [
            {"id": "c1", "name": f"{project}-main-1", "project": project},
            {"id": "c2", "name": f"{project}-sidecar-1", "project": project},
        ],
        "stop_command": "docker stop c1 c2",
    }
    assert json.loads(as_json.stdout)["left_running"] == status["left_running"]
    assert f"Containers left running with no live owner: {project}" in shown
    assert "Stop them: docker stop c1 c2" in shown
    # Only `docker ps`: status reports and never stops anything.
    assert {query[0] for query in queries} == {"ps"}
    queries.clear()
    with run_lock.hold(root):
        assert run_status.run_status(root)["left_running"] is None
    assert not queries
    # Observers that poll many runs skip Docker and host state; the rest is unchanged.
    quiet = run_status.run_status(root, containers=False)
    assert quiet["left_running"] is None and quiet["host"] is None
    assert not queries
    assert {**quiet, "left_running": None, "heartbeat": None} == {
        **status,
        "left_running": None,
        "host": None,
        "heartbeat": None,
    }


def test_status_says_it_could_not_check_for_containers_when_docker_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    _killed(root)
    (root / "evidence/frozen/suite/0/task").mkdir(parents=True)

    def docker(*arguments: str, **kwargs: object) -> str:
        raise ValueError("docker ps failed: Cannot connect to the Docker daemon")

    monkeypatch.setattr(harbor_backend, "_docker", docker)

    status = run_status.run_status(root)
    table = CliRunner().invoke(cli.app, ["status", str(root)])

    assert status["left_running"] == {
        "checked": False,
        "error": "docker ps failed: Cannot connect to the Docker daemon",
        "containers": [],
        "stop_command": None,
    }
    assert table.exit_code == 0, table.output
    assert "Containers: could not check for ones left running: docker ps failed" in table.output


def test_a_killed_review_of_a_finished_run_offers_no_resume(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    run = json.loads((root / "evidence/run.json").read_text())
    _write(root / "evidence/run.json", {**run, "status": "complete"})
    _write(root / "report/report.html", "")
    _killed(root, command="review")

    status = run_status.run_status(root)

    assert status["phase"] == "interrupted" and status["resume_command"] is None
    assert "Resume:" not in run_status.format_status(status)
    # While a process owns a finished run, the same events are not a sign that it died.
    with run_lock.hold(root):
        assert run_status.run_status(root)["phase"] == "reviewing"
    _killed(root)
    with run_lock.hold(root):
        assert run_status.run_status(root)["phase"] == "complete"


@pytest.mark.parametrize(
    "phase,revision,shown",
    [
        ("measuring", "observer-revisions/measurements/revision.json", "Measurement: measurements"),
        ("reviewing", "review-revisions/review/review-state.json", "Review: review"),
    ],
)
def test_a_run_killed_after_coding_shows_its_revision_as_interrupted(
    tmp_path: Path, phase: str, revision: str, shown: str
) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    run = json.loads((root / "evidence/run.json").read_text())
    _write(root / "evidence/run.json", {**run, "status": "complete"})
    selection = json.loads((root / "evaluation.json").read_text())
    started: dict[str, dict[str, object]] = {
        "measuring": {"measurement_required": True},
        "reviewing": {"reviewer": {"harness": "codex"}},
    }
    _write(root / "evaluation.json", {**selection, **started[phase]})
    in_progress = {"measuring": "collecting", "reviewing": "running"}[phase]
    _write(root / "evidence" / revision, {"status": in_progress})
    _killed(root)

    status = run_status.run_status(root)
    text = run_status.format_status(status)

    assert status["phase"] == "interrupted"
    # `run` decides whether this state can continue; status only names the command.
    assert f"Resume: {status['resume_command']}" in text
    assert f"{shown} interrupted" in text
    with run_lock.hold(root):
        owned = run_status.run_status(root)
        assert owned["phase"] == phase and owned["resume_command"] is None
        assert f"{shown} {in_progress}" in run_status.format_status(owned)


def test_review_refuses_an_active_run_before_planning(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    with run_lock.hold(root):
        result = CliRunner().invoke(cli.app, ["review", str(root), "--revision", "r2", "--yes"])
    assert result.exit_code == 2 and "run is already active" in result.stderr


@pytest.mark.parametrize(
    "usage,total,lower_bound",
    [
        # Codex receipts without subagents have no total: input plus output, as Codex counts it.
        ({"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 50}, 1050, False),
        ({"input_tokens": 1000}, 0, True),
    ],
)
def test_a_receipt_without_a_total_derives_it_or_stays_a_lower_bound(
    tmp_path: Path, usage: dict[str, int], total: int, lower_bound: bool
) -> None:
    root = _run(tmp_path / "run", states={**STATES, "a3": "pending"}, ledger=True)
    _write(root / "evidence/attempts/a2/handoff/execution.json", {"usage": usage})

    tokens = run_status.run_status(root)["arms"]["workflow"]["tokens"]

    assert (tokens["total_tokens"], tokens["lower_bound"]) == (total, lower_bound)


def test_a_wal_ledger_is_never_opened_and_status_falls_back(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    path = root / "evidence" / LEDGER_NAME
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    before = _tree(root)

    status = run_status.run_status(root)

    assert status["states_source"] == "run.json"
    assert _tree(root) == before  # Opening a WAL database read-only still creates -wal/-shm.


def test_watch_skips_a_redraw_when_a_file_is_mid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    owners: list[dict[str, Any] | None] = [{"pid": 1, "pgid": 1, "started_at": "t"}, None]
    monkeypatch.setattr(run_lock, "owner", lambda path: owners.pop(0))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    real = run_status.run_status
    calls = 0

    def flaky(path: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise json.JSONDecodeError("Expecting value", "", 0)  # evaluation.json truncated
        return real(path)

    monkeypatch.setattr(cli, "run_status", flaky)

    result = CliRunner().invoke(cli.app, ["status", str(root), "--watch"])

    assert result.exit_code == 0, result.output
    assert result.stdout.count("Phase: coding") == 1 and "Phase: stopped" in result.stdout


@pytest.mark.parametrize("ending", ["stopped", "failed"])
def test_a_finished_run_whose_review_ended_early_shows_that_ending(
    tmp_path: Path, ending: str
) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    run = json.loads((root / "evidence/run.json").read_text())
    _write(root / "evidence/run.json", {**run, "status": "complete"})
    _write(root / "report/report.html", "")
    (root / "events.jsonl").write_text(
        json.dumps({"event": "started", "command": "review"})
        + "\n"
        + json.dumps(
            {"event": ending, "error": "boom"} if ending == "failed" else {"event": ending}
        )
        + "\n"
    )

    status = run_status.run_status(root)

    assert status["phase"] == ending
    assert status["report"] == str(root / "report/report.html")


def test_a_discarded_attempt_keeps_its_observed_lower_bound(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states={**STATES, "a3": "pending"}, ledger=True)
    evidence = root / "evidence"
    _write(
        evidence / "attempts/d1/handoff/execution.json",
        {"usage": None, "usage_accounting": {"observed_usage_lower_bound": USAGE}},
    )
    _write(
        evidence / "discarded/d1.json",
        {
            "id": "d1",
            "task_id": "task-a",
            "arm": "workflow",
            "attempt": 2,
            "slot": 3,
            "replaced_by": "a4",
            "stages": {
                "handoff": {
                    "execution": {"path": "attempts/d1/handoff/execution.json", "sha256": "x"},
                    "work": {"tokens": None},
                }
            },
        },
    )

    status = run_status.run_status(root)

    assert status["discarded"][0]["tokens"]["total_tokens"] == 1050
    assert status["discarded"][0]["tokens_status"] == "lower_bound"
    assert status["tokens"]["total_tokens"] == 1050 + 1050


@pytest.mark.parametrize(
    ("grading", "expected"),
    [
        # Attributes sit beside the grade; none of them changes it.
        (_passed(True, agent_error=True), ("pass", None)),
        (_passed(True, timed_out=True), ("pass", None)),
        (_passed(False), ("fail", None)),
        (
            {**_passed(False), "fail_reason": "unsupported_submission:symlink"},
            ("fail", "unsupported_submission:symlink"),
        ),
        ({"status": "infra", "reason": "the verifier replay failed"},
         ("infra", "the verifier replay failed")),
    ],
)
def test_status_reads_the_one_grade_record(
    tmp_path: Path, grading: dict[str, Any], expected: tuple[str, str | None]
) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    _grade(root / "evidence", "a1", grading)
    # A failed session never vetoes a grade, and raises no alert of its own.
    _write(root / "evidence/attempts/a1/handoff/execution.json", {
        "successful_native_completion": False, "treatment_error": "ignored",
    })

    status = run_status.run_status(root, containers=False)
    row = next(row for row in status["attempts"] if row["id"] == "a1")

    assert (row["outcome"], row["outcome_reason"]) == expected
    # Only a grade-stage infrastructure error, which leaves the run unfinished, alerts.
    assert [item["kind"] for item in status["alerts"]["items"]] == (
        ["grades_pending"] if expected[0] == "infra" else []
    )
    assert status["alerts"]["schema_version"] == run_status.ALERTS_SCHEMA_VERSION == 3


def test_a_legacy_run_labels_its_complete_attempts_legacy(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    plan = json.loads((root / "evidence/plan.json").read_text())
    del plan["grade_contract"]
    _write(root / "evidence/plan.json", plan)
    before = _tree(root)

    result = CliRunner().invoke(cli.app, ["status", str(root), "--json"])

    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["outcomes"] == {"legacy": 2}
    assert _tree(root) == before


def test_a_run_saved_before_the_not_measured_list_shows_no_such_warning(tmp_path: Path) -> None:
    # Readiness rows in the older shape: status and readiness, no state, and no saved list.
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    selection = json.loads((root / "evaluation.json").read_text())
    readiness = {
        "covered": {"coverage": {"status": "available", "readiness": "passed", "reason": None}},
        "rust": {"coverage": {"status": "unavailable", "readiness": "failed", "reason": "exit 3"}},
    }
    _write(root / "evaluation.json", {**selection, "measurement_readiness": readiness})

    status = run_status.run_status(root)

    assert status["measures"]["not_measured"] == []
    assert "Warning:" not in run_status.format_status(status)


@pytest.mark.parametrize(
    "projection",
    [
        "validated",
        "unavailable",
        "historical",
        "claude",
        "claude-validated",
        "report-validated",
        "report-rejected",
        "current-over-report",
    ],
)
def test_status_uses_current_usage_and_labels_historical_receipts(
    tmp_path: Path, projection: str
) -> None:
    root = _run(tmp_path / "run", states={**STATES, "a3": "pending"}, ledger=True)
    handoff = root / "evidence/attempts/a1/handoff"
    if projection.startswith("claude"):
        # Claude usage is trusted only once its saved transcripts are revalidated, as Codex's is.
        execution = json.loads((handoff / "execution.json").read_text())
        execution["launch"] = {"harness": "claude-cli"}
        _write(handoff / "execution.json", execution)
    if projection in {"validated", "unavailable", "current-over-report", "claude-validated"}:
        _write(handoff / "execution-current.json", {
            "usage": USAGE if projection != "unavailable" else None,
            "usage_validation": {
                "status": "unavailable" if projection == "unavailable" else "validated",
                "reasons": ["rejected saved rollout"] if projection == "unavailable" else [],
            },
        })
    if projection.startswith("report-") or projection == "current-over-report":
        # A report rebuilt after the upgrade re-validated this attempt's saved usage. A rejection
        # still carries the re-parsed usage, and the receipt an observed floor; neither counts.
        validated = projection == "report-validated"
        if projection == "report-rejected":
            execution = json.loads((handoff / "execution.json").read_text())
            execution["usage_accounting"] = {"observed_usage_lower_bound": USAGE}
            _write(handoff / "execution.json", execution)
        validation = {
            "status": "validated" if validated else "unavailable",
            "validated_usage": USAGE,
            "reasons": [] if validated else ["rejected saved rollout"],
        }
        _write(root / "report/results.json", {"observations": [{
            "attempt_id": "a1", "metric": "Input tokens", "status": "measured",
            "value": USAGE["input_tokens"], "provenance": {"usage_validation": validation},
        }]})
    before = _tree(root)
    status = run_status.run_status(root)
    row = next(row for row in status["attempts"] if row["id"] == "a1")
    expected = {"validated": "exact", "unavailable": "unknown",
                "historical": "unvalidated", "claude": "unvalidated", "claude-validated": "exact",
                "report-validated": "exact", "report-rejected": "unknown",
                "current-over-report": "exact"}
    assert row["tokens_status"] == expected[projection]
    if projection in {"unavailable", "report-rejected"}:
        assert row["tokens"] is None
        assert "rejected saved rollout" in row["tokens_reason"]
    else:
        assert row["tokens"] == USAGE
    if projection in {"historical", "claude"}:
        assert status["arms"]["vanilla"]["tokens"]["unvalidated"] is True
        note = f"rebuild with `pluginbench report {root}` to re-validate token usage"
        assert note in row["tokens_reason"]
        assert note in run_status.format_status(status)
        # Codex and Claude runs share the note, so it names neither harness.
        assert "Recorded token usage is unvalidated" in run_status.format_status(status)
    assert _tree(root) == before


def _select_measurement(root: Path, revision: str) -> None:
    selection = json.loads((root / "evaluation.json").read_text())
    _write(root / "evaluation.json", {**selection, "measurement_revision": revision})


def _collected(root: Path, revision: str, failures: list[dict[str, Any]]) -> None:
    """A report built from `revision`, whose collection failed on `failures`."""
    _write(
        root / "report/results.json",
        {"measurement_collection": {"revision": revision, "failures": failures}},
    )


def _failure(attempt: str, task: str, family: str, *diagnostics: str) -> dict[str, Any]:
    return {
        "attempt_id": attempt,
        "task": task,
        "stage": "handoff",
        "family": family,
        "status": "unavailable",
        "reason": "collector unavailable in both repeats",
        "diagnostics": list(diagnostics),
    }


@pytest.mark.parametrize("ending", ["failed", "stopped"])
def test_a_failed_operation_alerts_but_a_stop_does_not(tmp_path: Path, ending: str) -> None:
    root = _run(tmp_path / "run", states=STATES, ledger=True)
    _write(root / "check.json", {"status": "ready", "findings": []})
    ended = {"event": ending, **({"error": "boom"} if ending == "failed" else {"reason": "x"})}
    (root / "events.jsonl").write_text(
        json.dumps({"event": "started"}) + "\n" + json.dumps(ended) + "\n"
    )

    status = run_status.run_status(root)

    if ending == "failed":
        assert status["alerts"]["items"] == [
            {"kind": "run_failed", "severity": "error", "message": "boom"}
        ]
        assert "Error: boom" in run_status.format_status(status)
    else:
        assert status["alerts"]["items"] == []


def test_measurement_failures_alert_per_family_and_arm_vanilla_first(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    _select_measurement(root, "m1")
    _collected(
        root,
        "m1",
        [
            _failure("a2", "task-a", "coverage", "no lcov file"),
            _failure("a1", "task-a", "coverage", "image gone"),
            _failure("a3", "task-b", "coverage", "image gone", "no lcov file"),
            _failure("a3", "task-b", "fallow"),
        ],
    )

    result = CliRunner().invoke(cli.app, ["status", str(root), "--json"])
    assert result.exit_code == 0, result.output
    alerts = json.loads(result.stdout)["alerts"]

    assert (alerts["schema_version"], alerts["measurement_revision"]) == (3, "m1")
    rows = [
        (row["severity"], row["family"], row["arm"], row["attempts"], row["tasks"], row["reasons"])
        for row in alerts["items"]
    ]
    # The baseline arm is vanilla: its missing values leave comparisons partial or unavailable.
    assert rows == [
        ("error", "coverage", "vanilla", 2, ["task-a", "task-b"], ["image gone", "no lcov file"]),
        ("error", "fallow", "vanilla", 1, ["task-b"], ["collector unavailable in both repeats"]),
        ("warning", "coverage", "workflow", 1, ["task-a"], ["no lcov file"]),
    ]
    text = run_status.format_status(run_status.run_status(root))
    assert (
        "Error: coverage unavailable for 2 vanilla attempts on 2 tasks: image gone; no lcov file"
        in text
    )


def test_a_multiline_collector_reason_prints_as_one_status_line(tmp_path: Path) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    _select_measurement(root, "m1")
    _collected(root, "m1", [_failure("a1", "task-a", "coverage", "exit 2\nno lcov file\n")])

    status = run_status.run_status(root)

    assert status["alerts"]["items"][0]["reasons"] == ["exit 2\nno lcov file\n"]
    [line] = [row for row in run_status.format_status(status).splitlines() if "coverage" in row]
    assert line.endswith("exit 2 no lcov file")


@pytest.mark.parametrize("report_revision", ["m2", "m1"])
def test_a_later_measurement_revision_leaves_no_stale_measure_alerts(
    tmp_path: Path, report_revision: str
) -> None:
    root = _run(tmp_path / "run", states=dict.fromkeys(STATES, "complete"), ledger=True)
    # m2 measured what m1 lost; the report is rebuilt from m2, or not yet rebuilt at all.
    _select_measurement(root, "m2")
    failures = [] if report_revision == "m2" else [_failure("a1", "task-a", "coverage", "gone")]
    _collected(root, report_revision, failures)

    alerts = run_status.run_status(root)["alerts"]

    assert alerts["items"] == []
    assert alerts["measurement_revision"] == ("m2" if report_revision == "m2" else None)
