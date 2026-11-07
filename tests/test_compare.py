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


def test_pilot_climb_measures_match_the_study_analysis() -> None:
    # analyze.py's numbers for the pilot's climb split, from the same saved reports.
    evidence = load_evidence(PILOT)
    expected = {
        "tokens": {"vanilla": 1.67e6, "superpowers": 3.96e6, "workflow-main": 4.59e6},
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
    # Medians use the repeats each side measured, and the gate names the task where they differ.
    tokens = check_gate(evidence, gates, _gate("tokens"), "superpowers", CLIMB)
    assert tokens.detail == f"4 of 4 tasks; measured repeats differ: {first['task']} 2 vs 3"
    coverage = measure(evidence, _gate("changed-line coverage"), "vanilla", CLIMB)
    assert coverage.missing == {"happy-dom-abort-pending-body-reads": "no measured value"}


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

    incumbent = [f"{run}=workflow-main" for run in PILOT]
    result = CliRunner().invoke(cli.app, ["compare", *round_one, *incumbent, "--gates", gates])

    assert result.exit_code == 0, result.stderr
    climb = result.stdout.split("Split climb")[1].split("Split holdout")[0]
    table, verdicts = climb.split("\nGates\n")
    cells = [re.split(r"\s{2,}", line) for line in table.splitlines() if line]
    rows = {row[0]: [cell.split(" ")[0] for cell in row[1:]] for row in cells}
    assert rows["measure"] == ["vanilla", "workflow-r1", "workflow-main"]
    assert rows["tokens"] == ["1.67M", "2.91M", "4.59M"]
    assert rows["cognitive ratio to reference"] == ["0.15", "2.42", "2.95"]
    assert rows["high and medium review findings"] == ["11", "10", "9"]
    gate_lines = [line.split() for line in verdicts.splitlines() if line.startswith("tokens ")]
    assert ["workflow-r1", "vanilla", "2.91M", "1.67M", "≤", "2.17M", "fail"] in [
        line[1:8] for line in gate_lines
    ]
