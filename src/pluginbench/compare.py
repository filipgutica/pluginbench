"""Compare packages across saved evaluations and check declared gates.

Reads each evaluation's report/results.json and nothing else: no rebuild, no model calls.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pluginbench.evaluation_report import correctness_state

# Older runs call the package without guidance "baseline".
ARM_NAMES = {"baseline": "vanilla"}
PASS_STATES = {"pass"}


class Gate(BaseModel):
    """One gate: a candidate's value against another package's, within a limit."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    against: str = Field(min_length=1)
    direction: Literal["higher", "lower"]
    correctness: Literal["passes", "unknown"] | None = None
    per_task: bool = False
    metrics: list[str] | None = Field(default=None, min_length=1)
    statistic: Literal["total", "mean_of_task_medians", "mean_of_task_median_ratios"] | None = None
    reference_floor: float | None = None
    tolerance: float | None = Field(default=None, ge=0)
    factor: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def one_measure(self) -> Gate:
        if (self.correctness is None) == (self.metrics is None):
            raise ValueError(f"gate {self.name!r} needs exactly one of correctness or metrics")
        if (self.metrics is None) != (self.statistic is None):
            raise ValueError(f"gate {self.name!r}: metrics and statistic go together")
        if self.per_task and self.correctness is None:
            raise ValueError(f"gate {self.name!r}: per_task applies to correctness gates only")
        if self.reference_floor is not None and self.statistic != "mean_of_task_median_ratios":
            raise ValueError(f"gate {self.name!r}: reference_floor needs a ratio statistic")
        if self.tolerance is not None and self.factor is not None:
            raise ValueError(f"gate {self.name!r}: give a tolerance or a factor, not both")
        return self


class Gates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required: whether an overlap pass counts as a pass is the study's choice, never a default.
    count_overlap_passes: bool
    splits: dict[str, list[str]] = Field(default_factory=dict)
    gates: list[Gate]

    @model_validator(mode="after")
    def unique_names(self) -> Gates:
        names = [gate.name for gate in self.gates]
        if len(names) != len(set(names)):
            raise ValueError("gate names must be unique")
        return self


@dataclass(frozen=True)
class Attempt:
    id: str
    run: str
    task: str
    arm: str
    correctness: str
    values: dict[str, float | None]


@dataclass
class Evidence:
    attempts: list[Attempt] = field(default_factory=list)
    references: dict[tuple[str, str], float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Measure:
    """One package's value for a metric gate over a split's tasks."""

    value: float | None
    tasks: dict[str, float]
    missing: dict[str, str]
    measured: int
    assigned: int
    # Measured repeats per task, so a comparison can show where the two sides differ.
    repeats: dict[str, int]


def load_gates(path: Path) -> Gates:
    return Gates.model_validate(yaml.safe_load(path.read_text()))


def load_evidence(specs: list[str]) -> Evidence:
    """Read runs given as DIR or DIR=ARM,ARM; one run may supply each package and task."""
    evidence = Evidence()
    owners: dict[tuple[str, str], str] = {}
    for spec in specs:
        directory, _, selection = spec.partition("=")
        wanted = {ARM_NAMES.get(arm, arm) for arm in selection.split(",") if arm}
        results = Path(directory) / "report" / "results.json"
        if not results.is_file():
            raise ValueError(f"{directory} has no report/results.json; run `pluginbench report`")
        data = json.loads(results.read_text())
        values: dict[str, dict[str, float | None]] = {}
        for row in data["observations"]:
            measured = row.get("status") == "measured" and isinstance(row.get("value"), int | float)
            values.setdefault(row["attempt_id"], {})[row["metric"]] = (
                float(row["value"]) if measured else None
            )
        found: set[str] = set()
        for row in data["attempts"]:
            arm = ARM_NAMES.get(row["guidance"], row["guidance"])
            found.add(arm)
            if wanted and arm not in wanted:
                continue
            owner = owners.setdefault((arm, row["task"]), directory)
            if owner != directory:
                raise ValueError(
                    f"{arm} on {row['task']} comes from both {owner} and {directory}; "
                    "select packages with DIR=ARM,ARM"
                )
            evidence.attempts.append(
                Attempt(
                    id=row["id"],
                    run=directory,
                    task=row["task"],
                    arm=arm,
                    correctness=correctness_state(row["correctness"], row.get("native_grading")),
                    values=values.get(row["id"], {}),
                )
            )
        if wanted - found:
            raise ValueError(f"{directory} has no package {', '.join(sorted(wanted - found))}")
        for row in data.get("reference_values", []):
            key = (row["task"], row["metric"])
            value = float(row["value"]) if row.get("status") == "measured" else None
            if evidence.references.setdefault(key, value) != value:
                raise ValueError(f"runs disagree on the reference {row['metric']} of {row['task']}")
    return evidence


def _sum(values: dict[str, float | None], metrics: list[str]) -> float | None:
    parts = [values.get(metric) for metric in metrics]
    return None if any(part is None for part in parts) else sum(part or 0.0 for part in parts)


def _reference(evidence: Evidence, task: str, gate: Gate) -> tuple[float | None, str | None]:
    assert gate.metrics is not None
    parts = [evidence.references.get((task, metric)) for metric in gate.metrics]
    if any(part is None for part in parts):
        return None, "no measured reference"
    reference = sum(part or 0.0 for part in parts)
    if gate.reference_floor is not None and reference < gate.reference_floor:
        return (
            None,
            f"reference below floor ({_number(reference)} < {_number(gate.reference_floor)})",
        )
    if reference == 0:
        return None, "reference is zero"
    return reference, None


def measure(evidence: Evidence, gate: Gate, arm: str, tasks: list[str]) -> Measure:
    """Per-task statistics first, then one value over the tasks that have one."""
    assert gate.metrics is not None
    per_task: dict[str, float] = {}
    missing: dict[str, str] = {}
    repeats: dict[str, int] = {}
    for task in tasks:
        attempts = [row for row in evidence.attempts if row.arm == arm and row.task == task]
        values = [_sum(row.values, gate.metrics) for row in attempts]
        present = [value for value in values if value is not None]
        repeats[task] = len(present)
        if not present:
            missing[task] = "no attempts" if not attempts else "no measured value"
            continue
        if gate.statistic == "total":
            per_task[task] = sum(present)
        elif gate.statistic == "mean_of_task_medians":
            per_task[task] = statistics.median(present)
        else:
            reference, reason = _reference(evidence, task, gate)
            if reference is None:
                missing[task] = str(reason)
                continue
            per_task[task] = statistics.median(value / reference for value in present)
    return Measure(
        value=_combine(gate, list(per_task.values())),
        tasks=per_task,
        missing=missing,
        measured=sum(repeats.values()),
        assigned=sum(row.arm == arm and row.task in repeats for row in evidence.attempts),
        repeats=repeats,
    )


def _combine(gate: Gate, values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) if gate.statistic == "total" else statistics.mean(values)


@dataclass(frozen=True)
class Verdict:
    candidate: float | None
    comparator: float | None
    limit: float | None
    result: str
    detail: str


def _limit(gate: Gate, comparator: float) -> float:
    if gate.factor is not None:
        # Scale by the comparator's size, so a negative comparator still allows the same slack.
        return comparator + abs(comparator) * (gate.factor - 1)
    tolerance = gate.tolerance or 0.0
    return comparator + tolerance if gate.direction == "lower" else comparator - tolerance


def _within(gate: Gate, value: float, limit: float) -> bool:
    return value <= limit if gate.direction == "lower" else value >= limit


def _correctness_counts(
    evidence: Evidence, gates: Gates, arm: str, tasks: list[str], kind: str
) -> dict[str, int | None]:
    states = PASS_STATES | ({"overlap_pass"} if gates.count_overlap_passes else set())
    if kind == "unknown":
        states = {"unknown"}
    counts: dict[str, int | None] = {}
    for task in tasks:
        rows = [row for row in evidence.attempts if row.arm == arm and row.task == task]
        counts[task] = sum(row.correctness in states for row in rows) if rows else None
    return counts


def check_gate(
    evidence: Evidence, gates: Gates, gate: Gate, candidate: str, tasks: list[str]
) -> Verdict:
    if gate.correctness is not None:
        mine = _correctness_counts(evidence, gates, candidate, tasks, gate.correctness)
        theirs = _correctness_counts(evidence, gates, gate.against, tasks, gate.correctness)
        absent = sorted(task for task in tasks if mine[task] is None or theirs[task] is None)
        if absent:
            return Verdict(None, None, None, "undetermined", "no attempts: " + ", ".join(absent))
        if gate.per_task:
            failing = [
                task
                for task in tasks
                if not _within(gate, float(mine[task] or 0), _limit(gate, float(theirs[task] or 0)))
            ]
            return Verdict(
                None,
                None,
                None,
                "fail" if failing else "pass",
                f"{len(tasks) - len(failing)} of {len(tasks)} tasks within limit"
                + (": fails " + ", ".join(failing) if failing else ""),
            )
        value = float(sum(count or 0 for count in mine.values()))
        comparator = float(sum(count or 0 for count in theirs.values()))
        limit = _limit(gate, comparator)
        result = "pass" if _within(gate, value, limit) else "fail"
        return Verdict(value, comparator, limit, result, f"{len(tasks)} of {len(tasks)} tasks")
    mine_measure = measure(evidence, gate, candidate, tasks)
    theirs_measure = measure(evidence, gate, gate.against, tasks)
    if gate.statistic == "total" and (
        mine_measure.measured < mine_measure.assigned
        or theirs_measure.measured < theirs_measure.assigned
    ):
        return Verdict(
            mine_measure.value,
            theirs_measure.value,
            None,
            "undetermined",
            f"missing values: {mine_measure.measured}/{mine_measure.assigned} and "
            f"{theirs_measure.measured}/{theirs_measure.assigned} attempts measured",
        )
    # Both sides over the same tasks, so a task missing on one side is not compared.
    common = [task for task in tasks if task in mine_measure.tasks and task in theirs_measure.tasks]
    if not common:
        return Verdict(None, None, None, "undetermined", f"0 of {len(tasks)} tasks comparable")
    unequal = [
        f"{task} {mine_measure.repeats[task]} vs {theirs_measure.repeats[task]}"
        for task in common
        if mine_measure.repeats[task] != theirs_measure.repeats[task]
    ]
    mine_value = _combine(gate, [mine_measure.tasks[task] for task in common])
    theirs_value = _combine(gate, [theirs_measure.tasks[task] for task in common])
    assert mine_value is not None and theirs_value is not None
    limit = _limit(gate, theirs_value)
    return Verdict(
        mine_value,
        theirs_value,
        limit,
        "pass" if _within(gate, mine_value, limit) else "fail",
        f"{len(common)} of {len(tasks)} tasks"
        + ("; measured repeats differ: " + ", ".join(unequal) if unequal else ""),
    )


def _number(value: float | None) -> str:
    if value is None:
        return "missing"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


def _arms(evidence: Evidence) -> list[str]:
    seen = list(dict.fromkeys(row.arm for row in evidence.attempts))
    return sorted(seen, key=lambda arm: (arm != "vanilla", seen.index(arm)))


def _section(evidence: Evidence, gates: Gates, split: str, tasks: list[str]) -> list[str]:
    present = sorted({row.task for row in evidence.attempts} & set(tasks))
    lines = [f"Split {split}: {len(present)} of {len(tasks)} tasks have attempts"]
    if not present:
        return lines
    arms = _arms(evidence)
    rows = [["measure", *arms]]
    counts: dict[str, dict[str, int]] = {arm: {} for arm in arms}
    for arm in arms:
        mine = [row for row in evidence.attempts if row.arm == arm and row.task in tasks]
        counts[arm] = {
            "attempts": len(mine),
            "confirmed passes": sum(row.correctness == "pass" for row in mine),
            "overlap passes": sum(row.correctness == "overlap_pass" for row in mine),
            "unknown": sum(row.correctness == "unknown" for row in mine),
        }
        counts[arm]["passes"] = counts[arm]["confirmed passes"] + (
            counts[arm]["overlap passes"] if gates.count_overlap_passes else 0
        )
    for name in ("attempts", "confirmed passes", "overlap passes", "passes", "unknown"):
        rows.append([name, *(str(counts[arm][name]) for arm in arms)])
    passes = {arm: _correctness_counts(evidence, gates, arm, tasks, "passes") for arm in arms}
    for task in tasks:
        cells = []
        for arm in arms:
            assigned = sum(row.arm == arm and row.task == task for row in evidence.attempts)
            cells.append(f"{passes[arm][task]}/{assigned}" if assigned else "no attempts")
        rows.append([f"passes: {task}", *cells])
    notes: list[str] = []
    shown: set[tuple[object, ...]] = set()
    for gate in gates.gates:
        if gate.metrics is None:
            continue
        key = (tuple(gate.metrics), gate.statistic, gate.reference_floor)
        if key in shown:
            continue
        shown.add(key)
        cells = []
        for arm in arms:
            result = measure(evidence, gate, arm, tasks)
            count = f"{result.measured}/{result.assigned} attempts"
            if gate.statistic != "total":
                count = f"{len(result.tasks)}/{len(tasks)} tasks, {count}"
            cells.append(f"{_number(result.value)} ({count})")
            notes += [f"{gate.name}, {arm}, {task}: {why}" for task, why in result.missing.items()]
        rows.append([gate.name, *cells])
    lines += ["", *_table(rows)]
    if notes:
        lines += ["", "Missing values (not counted as 0):", *(f"  {note}" for note in notes)]
    verdicts = [
        ["gate", "candidate", "against", "value", "comparator", "limit", "result", "detail"]
    ]
    for gate in gates.gates:
        if gate.against not in arms:
            raise ValueError(
                f"gate {gate.name!r} compares against {gate.against}, which no run has"
            )
        for candidate in arms:
            if candidate == gate.against:
                continue
            verdict = check_gate(evidence, gates, gate, candidate, tasks)
            verdicts.append(
                [
                    gate.name,
                    candidate,
                    gate.against,
                    _number(verdict.candidate) if verdict.candidate is not None else "",
                    _number(verdict.comparator) if verdict.comparator is not None else "",
                    ("≤ " if gate.direction == "lower" else "≥ ") + _number(verdict.limit)
                    if verdict.limit is not None
                    else "",
                    verdict.result,
                    verdict.detail,
                ]
            )
    lines += ["", "Gates", *_table(verdicts)]
    return lines


def compare(specs: list[str], gates_path: Path) -> str:
    """The gate table for each split, then for every task the runs supply."""
    gates = load_gates(gates_path)
    evidence = load_evidence(specs)
    all_tasks = sorted({row.task for row in evidence.attempts})
    lines = [
        "Overlap passes count as passes."
        if gates.count_overlap_passes
        else "Overlap passes do not count as passes."
    ]
    for split, tasks in [*gates.splits.items(), ("all", all_tasks)]:
        lines += ["", *_section(evidence, gates, split, tasks)]
    return "\n".join(lines)
