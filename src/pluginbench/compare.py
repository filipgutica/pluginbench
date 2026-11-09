"""Compare packages across saved evaluations and check declared gates.

Reads each evaluation's report/results.json and nothing else: no rebuild, no model calls.
"""

from __future__ import annotations

import json
import math
import random
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
TOKEN_METRICS = ["Input tokens", "Output tokens"]
BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 0
MIN_PAIRED_TASKS = 6


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
    bound: Literal["lower", "upper"] | None = None
    margin: float | None = Field(default=None, ge=-1, le=1)

    @model_validator(mode="after")
    def one_measure(self) -> Gate:
        if (self.correctness is None) == (self.metrics is None):
            raise ValueError(f"gate {self.name!r} needs exactly one of correctness or metrics")
        if self.bound is not None:
            if self.per_task or self.tolerance is not None or self.reference_floor is not None:
                raise ValueError("bound gates cannot use per_task, tolerance, or reference_floor")
            if self.statistic is not None:
                raise ValueError("bound gates average repeats within task; omit statistic")
            if self.correctness == "passes":
                if (
                    self.bound != "lower"
                    or self.direction != "higher"
                    or self.margin is None
                    or self.factor is not None
                ):
                    raise ValueError(
                        "pass-rate bounds need bound: lower, direction: higher, margin"
                    )
            elif self.metrics is not None and sorted(self.metrics) == sorted(TOKEN_METRICS):
                if (
                    self.bound != "upper"
                    or self.direction != "lower"
                    or self.factor is None
                    or self.margin is not None
                ):
                    raise ValueError("token bounds need bound: upper, direction: lower, factor")
            else:
                raise ValueError("bound gates support correctness: passes or input/output tokens")
        elif self.margin is not None:
            raise ValueError("margin needs a bound gate")
        elif (self.metrics is None) != (self.statistic is None):
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
    # Why a metric has no value, from the report's observation.
    reasons: dict[str, str] = field(default_factory=dict)


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
    # Tasks measured on only some attempts, with why the others have no value.
    partial: dict[str, str]
    measured: int
    assigned: int
    # Measured repeats per task, so a comparison can show where the two sides differ.
    repeats: dict[str, int]


@dataclass(frozen=True)
class PairedMeasure:
    """Equal task weights; each task contributes one difference or ratio."""

    tasks: dict[str, float]
    missing: dict[str, str]
    value: float | None
    interval: tuple[float, float] | None


@dataclass(frozen=True)
class PairedComparison:
    passes: PairedMeasure
    tokens: PairedMeasure


def _paired_summary(
    tasks: dict[str, float], missing: dict[str, str], *, ratio: bool = False
) -> PairedMeasure:
    # Sort before sampling so input order cannot change the deterministic interval.
    values = [math.log(tasks[task]) if ratio else tasks[task] for task in sorted(tasks)]
    value = statistics.mean(values) if values else None
    interval = None
    if len(values) >= MIN_PAIRED_TASKS:
        rng = random.Random(BOOTSTRAP_SEED)
        draws = [
            statistics.mean(rng.choices(values, k=len(values))) for _ in range(BOOTSTRAP_DRAWS)
        ]
        quantiles = statistics.quantiles(draws, n=20, method="inclusive")
        interval = (quantiles[0], quantiles[-1])
    if ratio:
        value = math.exp(value) if value is not None else None
        interval = (math.exp(interval[0]), math.exp(interval[1])) if interval is not None else None
    return PairedMeasure(tasks, missing, value, interval)


def sign_test(differences: list[float]) -> float:
    """Exact two-sided binomial sign test; ties do not count, all ties give p=1."""
    positive = sum(value > 0 for value in differences)
    negative = sum(value < 0 for value in differences)
    n = positive + negative
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(positive, negative) + 1)) / (1 << n))


def paired_comparison(
    evidence: Evidence, gates: Gates, candidate: str, against: str, tasks: list[str]
) -> PairedComparison:
    """Match tasks, average each arm's repeats, then resample whole task pairs."""
    differences: dict[str, float] = {}
    ratios: dict[str, float] = {}
    missing_passes: dict[str, str] = {}
    missing_tokens: dict[str, str] = {}
    states = PASS_STATES | ({"overlap_pass"} if gates.count_overlap_passes else set())
    for task in sorted(set(tasks)):
        sides = [
            [row for row in evidence.attempts if row.arm == arm and row.task == task]
            for arm in (candidate, against)
        ]
        if not all(sides):
            why = "no attempts: " + ", ".join(
                arm for arm, rows in zip((candidate, against), sides, strict=True) if not rows
            )
            missing_passes[task] = missing_tokens[task] = why
            continue
        if any(
            row.correctness not in {"pass", "fail", "overlap_pass"}
            for rows in sides
            for row in rows
        ):
            missing_passes[task] = "unknown correctness on at least one attempt"
        else:
            rates = [sum(row.correctness in states for row in rows) / len(rows) for rows in sides]
            differences[task] = rates[0] - rates[1]
        totals = [[_sum(row.values, TOKEN_METRICS) for row in rows] for rows in sides]
        if any(
            value is None or not math.isfinite(value) or value < 0
            for side in totals
            for value in side
        ):
            missing_tokens[task] = (
                "missing, nonfinite, or negative token total on at least one attempt"
            )
            continue
        means = [statistics.mean(value for value in side if value is not None) for side in totals]
        if min(means) <= 0:
            missing_tokens[task] = "nonpositive mean tokens"
            continue
        ratios[task] = means[0] / means[1]
    return PairedComparison(
        _paired_summary(differences, missing_passes),
        _paired_summary(ratios, missing_tokens, ratio=True),
    )


def load_gates(path: Path) -> Gates:
    return Gates.model_validate(yaml.safe_load(path.read_text()))


def load_evidence(specs: list[str]) -> Evidence:
    """Read runs given as DIR or DIR=ARM,ARM; one run may supply each package and task."""
    evidence = Evidence()
    owners: dict[tuple[str, str], int] = {}
    for index, spec in enumerate(specs):
        directory, _, selection = spec.partition("=")
        wanted = {ARM_NAMES.get(arm, arm) for arm in selection.split(",") if arm}
        results = Path(directory) / "report" / "results.json"
        if not results.is_file():
            raise ValueError(f"{directory} has no report/results.json; run `pluginbench report`")
        data = json.loads(results.read_text())
        values: dict[str, dict[str, float | None]] = {}
        reasons: dict[str, dict[str, str]] = {}
        for row in data["observations"]:
            measured = row.get("status") == "measured" and isinstance(row.get("value"), int | float)
            values.setdefault(row["attempt_id"], {})[row["metric"]] = (
                float(row["value"]) if measured else None
            )
            if not measured:
                reasons.setdefault(row["attempt_id"], {})[row["metric"]] = str(
                    row.get("reason") or row.get("status") or "no value"
                )
        found: set[str] = set()
        for row in data["attempts"]:
            arm = ARM_NAMES.get(row["guidance"], row["guidance"])
            found.add(arm)
            if wanted and arm not in wanted:
                continue
            # Repeats come from one input; a second input with the same package would double them.
            owner = owners.setdefault((arm, row["task"]), index)
            if owner != index:
                raise ValueError(
                    f"{arm} on {row['task']} comes from both {specs[owner]} and {spec}; "
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
                    reasons=reasons.get(row["id"], {}),
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
    partial: dict[str, str] = {}
    repeats: dict[str, int] = {}
    for task in tasks:
        attempts = [row for row in evidence.attempts if row.arm == arm and row.task == task]
        values = [_sum(row.values, gate.metrics) for row in attempts]
        present = [value for value in values if value is not None]
        repeats[task] = len(present)
        why = "; ".join(
            sorted(
                {
                    _missing_reason(row, gate.metrics)
                    for row, value in zip(attempts, values, strict=True)
                    if value is None
                }
            )
        )
        if present and len(present) < len(attempts):
            partial[task] = (
                f"{len(attempts) - len(present)} of {len(attempts)} attempts unmeasured: {why}"
            )
        if not present:
            missing[task] = f"no measured value: {why}" if attempts else "no attempts"
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
        partial=partial,
        measured=sum(repeats.values()),
        assigned=sum(row.arm == arm and row.task in repeats for row in evidence.attempts),
        repeats=repeats,
    )


def _missing_reason(row: Attempt, metrics: list[str]) -> str:
    metric = next(metric for metric in metrics if row.values.get(metric) is None)
    return f"{metric}: {row.reasons.get(metric, 'no observation')}"


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
    if gate.bound is not None:
        paired = paired_comparison(evidence, gates, candidate, gate.against, tasks)
        paired_measure = paired.passes if gate.correctness else paired.tokens
        detail = (
            f"{len(paired_measure.tasks)} of {len(set(tasks))} tasks; "
            f"estimate {_stat_number(paired_measure.value)}"
        )
        limit = gate.margin if gate.correctness else gate.factor
        if paired_measure.interval is None or paired_measure.missing:
            reason = (
                f"too few tasks for interval (need {MIN_PAIRED_TASKS})"
                if paired_measure.interval is None
                else "incomplete task pairs"
            )
            return Verdict(None, None, limit, "undetermined", f"{detail}; {reason}")
        value = paired_measure.interval[0 if gate.bound == "lower" else 1]
        assert limit is not None
        return Verdict(
            value,
            None,
            limit,
            "pass" if _within(gate, value, limit) else "fail",
            f"{detail}; {gate.bound} bound of two-sided 90% interval",
        )
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


def _stat_number(value: float | None) -> str:
    return "missing" if value is None else f"{value:.6g}"


def _paired_section(
    evidence: Evidence, gates: Gates, arms: list[str], tasks: list[str]
) -> list[str]:
    lines = [
        "",
        "Paired task statistics (candidate minus / divided by comparator)",
        f"90% percentile bootstrap intervals; {BOOTSTRAP_DRAWS} task draws; seed {BOOTSTRAP_SEED}.",
        f"Intervals need at least {MIN_PAIRED_TASKS} tasks; repeats are averaged within each task.",
    ]
    for against in dict.fromkeys(gate.against for gate in gates.gates):
        for candidate in arms:
            if candidate == against:
                continue
            paired = paired_comparison(evidence, gates, candidate, against, tasks)
            lines += ["", f"{candidate} against {against}"]
            summary = [
                [
                    "paired measure",
                    "estimate",
                    "90% interval",
                    "tasks",
                    "exact sign-test p (two-sided)",
                ]
            ]
            for name, result in (
                ("pass-rate difference", paired.passes),
                ("geometric token ratio", paired.tokens),
            ):
                interval = (
                    f"[{_stat_number(result.interval[0])}, {_stat_number(result.interval[1])}]"
                    if result.interval is not None
                    else "too few tasks"
                )
                summary.append(
                    [
                        name,
                        _stat_number(result.value),
                        interval,
                        f"{len(result.tasks)}/{len(set(tasks))}",
                        _stat_number(sign_test(list(result.tasks.values())))
                        if name == "pass-rate difference" and result.tasks
                        else "",
                    ]
                )
            lines += _table(summary)
            per_task = [["task", "pass-rate difference", "token ratio"]]
            for task in sorted(set(tasks)):
                per_task.append(
                    [
                        task,
                        _stat_number(paired.passes.tasks.get(task)),
                        _stat_number(paired.tokens.tasks.get(task)),
                    ]
                )
            lines += ["", *_table(per_task)]
            for name, result in (("passes", paired.passes), ("tokens", paired.tokens)):
                lines += [
                    f"  Excluded {name}, {task}: {why}" for task, why in result.missing.items()
                ]
    return lines


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
        if gate.metrics is None or gate.bound is not None:
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
            notes += [
                f"{gate.name}, {arm}, {task}: {why}"
                for found in (result.missing, result.partial)
                for task, why in found.items()
            ]
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
            number = _stat_number if gate.bound is not None else _number
            verdicts.append(
                [
                    gate.name,
                    candidate,
                    gate.against,
                    number(verdict.candidate) if verdict.candidate is not None else "",
                    number(verdict.comparator) if verdict.comparator is not None else "",
                    ("≤ " if gate.direction == "lower" else "≥ ") + number(verdict.limit)
                    if verdict.limit is not None
                    else "",
                    verdict.result,
                    verdict.detail,
                ]
            )
    lines += ["", "Gates", *_table(verdicts)]
    lines += _paired_section(evidence, gates, arms, tasks)
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
