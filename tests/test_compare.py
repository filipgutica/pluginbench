"""`pluginbench compare` reproduces the climb card's gate table from saved reports."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pluginbench import cli
from pluginbench.compare import (
    Attempt,
    Evidence,
    Gate,
    check_gate,
    load_evidence,
    load_gates,
    measure,
)

FIXTURE = Path(__file__).parent / "fixtures" / "compare"
PILOT = [str(FIXTURE / "pilot-coverage"), str(FIXTURE / "pilot-no-coverage")]
CLIMB = [
    "true-myth-iterable-collection-combinators",
    "csstree-shorthand-expansion-compression",
    "happy-dom-abort-pending-body-reads",
    "adaptix-name-mapping-aliases",
]


def _gate(name: str) -> Gate:
    return next(gate for gate in load_gates(FIXTURE / "card-gates.yaml").gates if gate.name == name)


def _gates_file(tmp_path: Path, **changes: object) -> Path:
    raw = yaml.safe_load((FIXTURE / "card-gates.yaml").read_text())
    for name, change in changes.items():
        if name == "count_overlap_passes":
            raw[name] = change
            continue
        gate = next(gate for gate in raw["gates"] if gate["name"] == name.replace("_", " "))
        gate.update(change)
    path = tmp_path / "gates.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(("version", "arm"), [(1, "baseline"), (2, "vanilla")])
def test_compare_reads_both_report_arm_names(tmp_path: Path, version: int, arm: str) -> None:
    root = tmp_path / "run"
    report = root / "report/results.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({
        "schema_version": version,
        "attempts": [{"id": "stored-id", "task": "task", "guidance": arm, "correctness": "pass"}],
        "observations": [{
            # Not a token metric: unvalidated token figures read as unknown (PR #59).
            "attempt_id": "stored-id", "arm": arm, "metric": "Source lines added",
            "value": 42, "status": "measured",
        }],
    }))
    gates = tmp_path / "gates.yaml"
    gates.write_text("count_overlap_passes: false\ngates: []\n")
    before = report.read_bytes()
    result = CliRunner().invoke(cli.app, ["compare", f"{root}=vanilla", "--gates", str(gates)])
    assert result.exit_code == 0, result.output
    assert "vanilla" in result.stdout and "baseline" not in result.stdout
    [attempt] = load_evidence([f"{root}=baseline"]).attempts
    assert (attempt.id, attempt.arm, attempt.values) == (
        "stored-id", "vanilla", {"Source lines added": 42}
    )
    assert report.read_bytes() == before


def test_pilot_climb_measures_match_the_study_analysis() -> None:
    # analyze.py's numbers for the pilot's climb split, from the same saved reports.
    evidence = load_evidence(PILOT)
    expected: dict[str, dict[str, float]] = {
        "cognitive ratio to reference": {
            "vanilla": 0.15,
            "superpowers": -0.09,
            "workflow-main": 2.95,
        },
        "complexity hotspots": {"vanilla": 8, "superpowers": 10, "workflow-main": 10},
        "introduced dead findings": {"vanilla": 4, "superpowers": 6, "workflow-main": 4},
        "duplicated test lines": {"vanilla": 288, "superpowers": 263, "workflow-main": 46},
        "changed-line coverage": {"vanilla": 99.0, "superpowers": 99.4, "workflow-main": 99.6},
        "high and medium review findings": {"vanilla": 8, "superpowers": 12, "workflow-main": 9},
    }
    for name, arms in expected.items():
        for arm, value in arms.items():
            result = measure(evidence, _gate(name), arm, CLIMB).value
            assert result == pytest.approx(value, rel=0.01, abs=0.05), (name, arm)
    # These archived reports have no validation receipts; their token totals stay unknown.
    for arm in ("vanilla", "superpowers", "workflow-main"):
        assert measure(evidence, _gate("tokens"), arm, CLIMB).value is None
    assert evidence.unvalidated_runs == set(PILOT)


def test_ratios_to_reference_are_taken_per_task_before_averaging() -> None:
    # A ratio of summed medians gives 0.51 for vanilla; the card's gate 3 averages task ratios.
    result = measure(load_evidence(PILOT), _gate("cognitive ratio to reference"), "vanilla", CLIMB)
    assert result.value == pytest.approx(0.15, abs=0.005)
    assert result.tasks["happy-dom-abort-pending-body-reads"] == pytest.approx(-1.0)


def test_reference_below_the_floor_is_missing_and_the_gate_counts_its_tasks(
    tmp_path: Path,
) -> None:
    gates = load_gates(_gates_file(tmp_path, cognitive_ratio_to_reference={"reference_floor": 10}))
    gate = next(gate for gate in gates.gates if gate.name == "cognitive ratio to reference")
    evidence = load_evidence(PILOT)

    result = measure(evidence, gate, "vanilla", CLIMB)
    verdict = check_gate(evidence, gates, gate, "vanilla", CLIMB)

    assert result.missing == {
        "happy-dom-abort-pending-body-reads": "reference below floor (3 < 10)"
    }
    assert result.value == pytest.approx(0.53, abs=0.005)
    assert verdict.detail == "3 of 4 tasks" and verdict.result == "pass"


def test_a_partly_measured_task_below_the_floor_shows_both_notes(tmp_path: Path) -> None:
    runs = []
    for name in ("pilot-coverage", "pilot-no-coverage"):
        shutil.copytree(FIXTURE / name, tmp_path / name)
        runs.append(str(tmp_path / name))
    task = "happy-dom-abort-pending-body-reads"
    results = next(
        path
        for path in (
            tmp_path / "pilot-coverage/report/results.json",
            tmp_path / "pilot-no-coverage/report/results.json",
        )
        if any(row["task"] == task for row in json.loads(path.read_text())["attempts"])
    )
    data = json.loads(results.read_text())
    first = next(
        row for row in data["attempts"] if row["task"] == task and row["guidance"] == "baseline"
    )
    for row in data["observations"]:
        if row["attempt_id"] == first["id"] and row["metric"] == "Net cognitive complexity added":
            row.update(value=None, status="unknown")
    results.write_text(json.dumps(data))
    gates = _gates_file(tmp_path, cognitive_ratio_to_reference={"reference_floor": 10})

    printed = CliRunner().invoke(cli.app, ["compare", *runs, "--gates", str(gates)]).stdout

    note = f"cognitive ratio to reference, vanilla, {task}: "
    assert note + "reference below floor (3 < 10)" in printed
    assert note + "1 of 3 attempts unmeasured" in printed
    # The unmeasured attempt is on the task the floor excludes, so the gate still decides.
    loaded = load_gates(gates)
    gate = next(gate for gate in loaded.gates if gate.name == "cognitive ratio to reference")
    verdict = check_gate(load_evidence(runs), loaded, gate, "vanilla", CLIMB)
    assert (verdict.result, verdict.detail) == ("pass", "3 of 4 tasks")


def test_a_ratio_gate_ignores_missing_values_on_a_task_it_excludes() -> None:
    metric = "Net cognitive complexity added"
    values = {("candidate", "A"): 1.0, ("vanilla", "A"): 2.0, ("candidate", "B"): None}
    evidence = Evidence(
        attempts=[
            Attempt(
                f"{arm}-{task}", "run", task, arm, "pass", {metric: values.get((arm, task), 2.0)}
            )
            for arm in ("candidate", "vanilla")
            for task in ("A", "B")
        ],
        references={("A", metric): 10.0, ("B", metric): 0.0},
    )
    gate = _gate("cognitive ratio to reference").model_copy(update={"against": "vanilla"})

    verdict = check_gate(
        evidence, load_gates(FIXTURE / "card-gates.yaml"), gate, "candidate", ["A", "B"]
    )

    assert (verdict.result, verdict.detail) == ("pass", "1 of 2 tasks")


def test_a_missing_value_stays_missing_and_leaves_a_total_undetermined(tmp_path: Path) -> None:
    runs = []
    for name in ("pilot-coverage", "pilot-no-coverage"):
        shutil.copytree(FIXTURE / name, tmp_path / name)
        runs.append(str(tmp_path / name))
    results = tmp_path / "pilot-coverage/report/results.json"
    data = json.loads(results.read_text())
    first = next(row for row in data["attempts"] if row["guidance"] == "superpowers")
    for row in data["observations"]:
        if row["attempt_id"] == first["id"] and row["metric"] in {
            "Complexity hotspots added",
            "Input tokens",
        }:
            row.update(value=None, status="unknown")
    results.write_text(json.dumps(data))
    evidence = load_evidence(runs)
    gates = load_gates(FIXTURE / "card-gates.yaml")
    gate = _gate("complexity hotspots")

    result = measure(evidence, gate, "superpowers", CLIMB)
    verdict = check_gate(evidence, gates, gate, "superpowers", CLIMB)

    assert (result.measured, result.assigned) == (11, 12)
    assert verdict.result == "undetermined" and "11/12" in verdict.detail
    # A mean gate cannot decide on the repeats that were measured either.
    tokens = check_gate(evidence, gates, _gate("tokens"), "superpowers", CLIMB)
    assert tokens.result == "undetermined"
    assert tokens.detail == (
        "missing Input tokens, Output tokens values: 0/12 and 0/12 attempts measured"
    )
    coverage = measure(evidence, _gate("changed-line coverage"), "vanilla", CLIMB)
    assert coverage.missing == {
        "happy-dom-abort-pending-body-reads": (
            "no measured value: Changed-line coverage %: no observation"
        )
    }
    # A task measured on only some attempts is listed too, with the report's reason.
    hotspots = measure(evidence, gate, "superpowers", CLIMB)
    assert hotspots.partial == {
        first["task"]: "1 of 3 attempts unmeasured: Complexity hotspots added: unknown"
    }
    printed = CliRunner().invoke(
        cli.app, ["compare", *runs, "--gates", str(FIXTURE / "card-gates.yaml")]
    )
    assert f"complexity hotspots, superpowers, {first['task']}: 1 of 3 attempts unmeasured" in printed.stdout


@pytest.mark.parametrize(
    "gate_name", ["changed-line coverage", "tokens", "cognitive ratio to reference"]
)
def test_a_mean_gate_with_a_partly_measured_side_is_undetermined(gate_name: str) -> None:
    gate = _gate(gate_name).model_copy(update={"against": "vanilla"})
    metrics = gate.metrics or []
    # The candidate's measured attempt beats the comparator; its two failed attempts have no value.
    better = 100.0 if gate.direction == "higher" else 1.0
    worse = 50.0 if gate.direction == "higher" else 1000.0

    def attempts(arm: str, values: list[float | None]) -> list[Attempt]:
        return [
            Attempt(
                f"{arm}-{n}",
                "run",
                "task",
                arm,
                "pass" if value is not None else "fail",
                {name: value for name in metrics},
                {} if value is not None else {name: "unknown" for name in metrics},
            )
            for n, value in enumerate(values)
        ]

    gates = load_gates(FIXTURE / "card-gates.yaml")
    # A usable reference, so a ratio gate keeps the task.
    references: dict[tuple[str, str], float | None] = {("task", name): 20.0 for name in metrics}
    complete = Evidence(
        attempts("candidate", [better] * 3) + attempts("vanilla", [worse] * 3), references
    )
    partial = Evidence(
        attempts("candidate", [better, None, None]) + attempts("vanilla", [worse] * 3),
        references,
    )

    decided = check_gate(complete, gates, gate, "candidate", ["task"])
    verdict = check_gate(partial, gates, gate, "candidate", ["task"])

    assert (decided.result, decided.detail) == ("pass", "1 of 1 tasks")
    assert verdict.result == "undetermined" and verdict.limit is None
    assert verdict.detail == (f"missing {', '.join(metrics)} values: 1/3 and 3/3 attempts measured")
    # A split that names the task twice counts its attempts once.
    repeated = check_gate(partial, gates, gate, "candidate", ["task", "task"])
    assert (repeated.result, repeated.detail) == (verdict.result, verdict.detail)


def test_a_factor_limit_keeps_its_slack_when_the_comparator_is_negative() -> None:
    metric = "Net cognitive complexity added"
    evidence = Evidence(
        attempts=[
            Attempt(f"{arm}-{n}", "run", "task", arm, "pass", {metric: -5.0})
            for arm in ("vanilla", "candidate")
            for n in range(3)
        ],
        references={("task", metric): 10.0},
    )
    gate = _gate("cognitive ratio to reference").model_copy(update={"against": "vanilla"})

    verdict = check_gate(
        evidence, load_gates(FIXTURE / "card-gates.yaml"), gate, "candidate", ["task"]
    )

    assert (verdict.candidate, verdict.comparator) == (-0.5, -0.5)
    assert verdict.limit == pytest.approx(-0.45) and verdict.result == "pass"


@pytest.mark.parametrize("counted", [True, False])
def test_gates_choose_whether_overlap_passes_count(tmp_path: Path, counted: bool) -> None:
    gates = load_gates(_gates_file(tmp_path, count_overlap_passes=counted))
    gate = next(gate for gate in gates.gates if gate.name == "passes")
    # One workflow-main climb attempt is an overlap pass; one vanilla one has reward 0 (a fail).
    verdict = check_gate(load_evidence(PILOT), gates, gate, "workflow-main", CLIMB)
    assert (verdict.candidate, verdict.comparator) == ((12, 11) if counted else (11, 11))


def test_gates_file_must_choose_overlap_passes(tmp_path: Path) -> None:
    raw = yaml.safe_load((FIXTURE / "card-gates.yaml").read_text())
    del raw["count_overlap_passes"]
    path = tmp_path / "gates.yaml"
    path.write_text(yaml.safe_dump(raw))
    result = CliRunner().invoke(cli.app, ["compare", *PILOT, "--gates", str(path)])
    assert result.exit_code == 2 and "count_overlap_passes" in result.stderr


def test_round_one_takes_the_incumbent_from_the_pilot_only_when_selected() -> None:
    round_one = [str(FIXTURE / "round-1-coverage"), str(FIXTURE / "round-1-no-coverage")]
    gates = str(FIXTURE / "card-gates.yaml")
    # Round 1 reused and re-reviewed the pilot's vanilla attempts, so they are not one sample.
    clash = CliRunner().invoke(
        cli.app, ["compare", *round_one, str(FIXTURE / "pilot-coverage"), "--gates", gates]
    )
    assert clash.exit_code == 2 and "comes from both" in clash.stderr
    # The same run named twice would double its attempts.
    twice = CliRunner().invoke(
        cli.app, ["compare", *PILOT, f"{PILOT[0]}=workflow-main", "--gates", gates]
    )
    assert twice.exit_code == 2 and "comes from both" in twice.stderr

    incumbent = [f"{run}=workflow-main" for run in PILOT]
    result = CliRunner().invoke(cli.app, ["compare", *round_one, *incumbent, "--gates", gates])

    assert result.exit_code == 0, result.stderr
    # These reports predate saved comparison identities, so they compare unchecked.
    assert (
        f"Run identity: {PILOT[0]} and {round_one[0]} saved no comparison identity; not checked"
        in result.stdout
    )
    climb = result.stdout.split("Split climb")[1].split("Split holdout")[0]
    table, verdicts = climb.split("\nGates\n")
    cells = [re.split(r"\s{2,}", line) for line in table.splitlines() if line]
    rows = {row[0]: [cell.split(" ")[0] for cell in row[1:]] for row in cells}
    assert rows["measure"] == ["vanilla", "workflow-r1", "workflow-main"]
    assert rows["tokens"] == ["missing", "missing", "missing"]
    assert rows["cognitive ratio to reference"] == ["0.15", "2.42", "2.95"]
    assert rows["high and medium review findings"] == ["11", "10", "9"]
    gate_lines = [line.split() for line in verdicts.splitlines() if line.startswith("tokens ")]
    assert all("undetermined" in line for line in gate_lines)


def _write_run(directory: Path, attempts: list[Attempt], references: dict[str, float]) -> str:
    """A minimal saved report holding these attempts and one reference metric per task."""
    report = directory / "report"
    report.mkdir(parents=True)
    data = {
        "attempts": [
            {"id": row.id, "task": row.task, "guidance": row.arm, "correctness": row.correctness}
            for row in attempts
        ],
        "observations": [
            {"attempt_id": row.id, "metric": metric, "value": value, "status": "measured"}
            for row in attempts
            for metric, value in row.values.items()
        ],
        "reference_values": [
            {"task": task, "metric": "Net cognitive complexity added", "value": value}
            | {"status": "measured"}
            for task, value in references.items()
        ],
    }
    (report / "results.json").write_text(json.dumps(data))
    return str(directory)


def test_a_ratio_gate_leaves_out_a_task_with_a_negative_reference(tmp_path: Path) -> None:
    # Against a reference of -20, adding 10 would score -0.5 and beat removing 15 (0.75).
    metric = "Net cognitive complexity added"
    evidence = Evidence(
        attempts=[
            Attempt(f"{arm}-{n}", "run", "task", arm, "pass", {metric: value})
            for arm, value in (("candidate", 10.0), ("vanilla", -15.0))
            for n in range(3)
        ],
        references={("task", metric): -20.0},
    )
    gate = _gate("cognitive ratio to reference").model_copy(update={"against": "vanilla"})

    verdict = check_gate(
        evidence, load_gates(FIXTURE / "card-gates.yaml"), gate, "candidate", ["task"]
    )

    assert (verdict.result, verdict.detail) == ("undetermined", "0 of 1 tasks comparable")
    run = _write_run(tmp_path / "run", evidence.attempts, {"task": -20.0})
    raw = yaml.safe_load((FIXTURE / "card-gates.yaml").read_text())
    for row in raw["gates"]:
        row["against"] = "vanilla"
    (tmp_path / "gates.yaml").write_text(yaml.safe_dump(raw))
    printed = (
        CliRunner()
        .invoke(cli.app, ["compare", run, "--gates", str(tmp_path / "gates.yaml")])
        .stdout
    )
    assert "cognitive ratio to reference, candidate, task: reference is not positive (-20)" in (
        printed
    )
    # Each task's value is printed, with the reference a ratio divides by.
    assert re.search(r"cognitive ratio to reference: task \(reference -20\)\s+missing", printed)
    assert re.search(r"tokens: task\s+missing\s+missing", printed)


def test_a_pass_count_gate_with_unequal_attempts_per_task_is_undetermined() -> None:
    # 3 passes of 5 would beat 2 of 3 on count, at a lower pass rate.
    evidence = Evidence(
        attempts=[
            Attempt(f"{arm}-{n}", "run", "task", arm, "pass" if n < passes else "fail", {})
            for arm, passes, repeats in (("candidate", 3, 5), ("vanilla", 2, 3))
            for n in range(repeats)
        ]
    )
    gates = load_gates(FIXTURE / "card-gates.yaml")

    for name in ("passes", "passes per task"):
        gate = _gate(name).model_copy(update={"tolerance": 0})
        verdict = check_gate(evidence, gates, gate, "candidate", ["task"])
        assert (verdict.result, verdict.detail) == (
            "undetermined",
            "attempts per task differ: task 5 vs 3",
        )


@pytest.mark.parametrize("name", ["passes", "passes per task"])
@pytest.mark.parametrize("counted", [True, False])
def test_unknown_grades_leave_a_pass_count_gate_open_when_they_could_change_it(
    tmp_path: Path, name: str, counted: bool
) -> None:
    evidence = Evidence(
        attempts=[
            Attempt(f"{arm}-{n}", "run", "task", arm, state, {})
            for arm, states in (
                ("candidate", ["pass", "overlap_pass", "unknown"]),
                ("vanilla", ["pass", "pass", "pass"]),
            )
            for n, state in enumerate(states)
        ]
    )
    gates = load_gates(_gates_file(tmp_path, count_overlap_passes=counted))
    gate = next(gate for gate in gates.gates if gate.name == name)

    verdict = check_gate(evidence, gates, gate, "candidate", ["task"])

    unknowns = "1 candidate and 0 comparator attempts with unknown correctness"
    if counted:
        # 2 or 3 passes against 3 with tolerance 1: the unknown cannot change the result.
        assert verdict.result == "pass"
        assert verdict.detail.endswith(f"; {unknowns} cannot change the result")
    else:
        # An overlap pass is a known non-pass here: 1 or 2 passes, and the unknown decides.
        assert (verdict.result, verdict.detail) == (
            "undetermined",
            f"{unknowns} could change the result",
        )


def test_a_metric_gate_with_a_task_on_one_side_only_is_undetermined() -> None:
    metric = "Net cognitive complexity added"
    values: dict[str, float | None] = {"Input tokens": 10.0, "Output tokens": 10.0, metric: 5.0}
    evidence = Evidence(
        attempts=[
            Attempt(f"{arm}-{task}-{n}", "run", task, arm, "pass", values)
            for arm, tasks in (("candidate", ["A", "B"]), ("vanilla", ["A"]))
            for task in tasks
            for n in range(3)
        ],
        references={("A", metric): 10.0},
    )
    gates = load_gates(FIXTURE / "card-gates.yaml")

    tokens = check_gate(evidence, gates, _gate("tokens"), "candidate", ["A", "B"])
    ratio = _gate("cognitive ratio to reference").model_copy(update={"against": "vanilla"})
    excluded = check_gate(evidence, gates, ratio, "candidate", ["A", "B"])

    assert (tokens.result, tokens.detail) == ("undetermined", "attempts on one side only: B")
    # A ratio gate leaves B out for its missing reference, so B's attempts do not matter.
    assert (excluded.result, excluded.detail) == ("pass", "1 of 2 tasks")


def _with_identities(tmp_path: Path, identities: dict[str, dict[str, object]]) -> list[str]:
    """Round 1 with the pilot's incumbent, each report given a saved comparison identity."""
    specs = []
    for name in ("round-1-coverage", "round-1-no-coverage", "pilot-coverage", "pilot-no-coverage"):
        shutil.copytree(FIXTURE / name, tmp_path / name)
        results = tmp_path / name / "report/results.json"
        data = json.loads(results.read_text())
        data["comparison_identity"] = identities[name]
        results.write_text(json.dumps(data))
        specs.append(str(tmp_path / name) + ("=workflow-main" if name.startswith("pilot") else ""))
    return specs


def _identity(**changes: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "runner": {"harness": "codex", "model": "model-a", "reasoning": "high"},
        "tasks": {task: f"digest-{task}" for task in CLIMB},
        "jobs": 2,
        "fallow_config_sha256": "config-a",
    }
    return identity | changes


def test_compare_refuses_runs_whose_saved_identities_differ(tmp_path: Path) -> None:
    gates = str(FIXTURE / "card-gates.yaml")
    task = "csstree-shorthand-expansion-compression"
    differing = _identity(
        runner={"harness": "codex", "model": "model-b", "reasoning": "high"},
        tasks={**{name: f"digest-{name}" for name in CLIMB}, task: "digest-changed"},
    )
    refused = CliRunner().invoke(
        cli.app,
        [
            "compare",
            *_with_identities(
                tmp_path / "refused",
                {
                    "round-1-coverage": _identity(),
                    "round-1-no-coverage": _identity(),
                    "pilot-coverage": differing,
                    "pilot-no-coverage": _identity(),
                },
            ),
            "--gates",
            gates,
        ],
    )

    assert refused.exit_code == 2
    assert 'runner.model: "model-b" vs "model-a"' in refused.stderr
    assert f'tasks.{task}: "digest-changed" vs "digest-{task}"' in refused.stderr
    assert "pilot-no-coverage" not in refused.stderr

    # Jobs may differ, and a field only one run saved is named but not compared.
    accepted = CliRunner().invoke(
        cli.app,
        [
            "compare",
            *_with_identities(
                tmp_path / "accepted",
                {
                    "round-1-coverage": _identity(jobs=4, review_criteria_sha256="criteria"),
                    "round-1-no-coverage": _identity(),
                    "pilot-coverage": _identity(),
                    "pilot-no-coverage": _identity(),
                },
            ),
            "--gates",
            gates,
        ],
    )

    assert accepted.exit_code == 0, accepted.stderr
    assert "saved by only one, not checked: review_criteria_sha256" in accepted.stdout
    assert "saved no comparison identity" not in accepted.stdout


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize(
    "host,validation,expected",
    [("codex", None, "undetermined"), ("codex", "unavailable", "undetermined"),
     ("codex", "validated", "pass"), ("claude", None, "undetermined"),
     ("claude", "unavailable", "undetermined"), ("claude", "validated", "pass")],
)
def test_saved_token_gates_require_validation_provenance(
    tmp_path: Path, schema: int, host: str, validation: str | None, expected: str
) -> None:
    attempts = [
        Attempt(f"{arm}-{task}", "run", task, arm, "pass",
                {"Input tokens": value, "Output tokens": value})
        for task in (f"task-{index}" for index in range(6))
        for arm, value in (("vanilla", 100.0), ("candidate", 1.0))
    ]
    directory = _write_run(tmp_path / "run", attempts, {})
    results = Path(directory) / "report/results.json"
    data = json.loads(results.read_text())
    data["schema_version"] = schema
    for row in data["observations"]:
        row["host"] = host
        status = "validated" if row["attempt_id"].startswith("vanilla-") else validation
        if status is not None:
            row["provenance"] = {"usage_validation": {"status": status}}
    results.write_text(json.dumps(data))
    before = results.read_bytes()
    specs = [directory + "=vanilla", directory + "=candidate"]
    evidence = load_evidence(specs)
    gates_path = tmp_path / "gates.yaml"
    gates_path.write_text(yaml.safe_dump({"count_overlap_passes": True,
                                        "gates": [_gate("tokens").model_dump(exclude_none=True)]}))
    gates = load_gates(gates_path)
    tasks = [f"task-{index}" for index in range(6)]
    for gate in (
        _gate("tokens"),
        Gate(name="paired tokens", against="vanilla", direction="lower",
             metrics=["Input tokens", "Output tokens"], bound="upper", factor=1.3),
    ):
        assert check_gate(evidence, gates, gate, "candidate", tasks).result == expected
    output = CliRunner().invoke(cli.app, ["compare", *specs, "--gates",
                                        str(gates_path)])
    assert output.exit_code == 0, output.output
    note = f"rebuild with `pluginbench report {directory}` to re-validate token usage"
    assert output.stdout.count(note) == (1 if expected == "undetermined" else 0)
    assert results.read_bytes() == before
