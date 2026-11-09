"""Task pairs, rather than correlated attempts, own the uncertainty estimate."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from pluginbench import cli
from pluginbench.compare import (
    Attempt,
    Evidence,
    Gate,
    Gates,
    check_gate,
    paired_comparison,
    sign_test,
)

TASKS = [f"task-{n}" for n in range(6)]
PASS_GATE = Gate(
    name="correctness bound",
    against="vanilla",
    direction="higher",
    correctness="passes",
    bound="lower",
    margin=-0.05,
)
TOKEN_GATE = Gate(
    name="token bound",
    against="vanilla",
    direction="lower",
    metrics=["Input tokens", "Output tokens"],
    bound="upper",
    factor=1.3,
)
GATES = Gates(count_overlap_passes=True, gates=[PASS_GATE, TOKEN_GATE])


def _evidence() -> Evidence:
    attempts = []
    # Differences: -1, -1/2, 0, 0, 1/2, 1. Unequal repeat counts are deliberate.
    for n, (mine, theirs) in enumerate(
        [
            ([0, 0], [1]),
            ([0, 1], [1]),
            ([0, 0], [0]),
            ([1, 1], [1]),
            ([0, 1], [0]),
            ([1, 1], [0]),
        ]
    ):
        for arm, outcomes in (("candidate", mine), ("vanilla", theirs)):
            for repeat, passed in enumerate(outcomes):
                # Candidate mean = 100 * ratio; median differs on task 0 after duplication below.
                tokens = 100 * 2 ** [-2, -1, 0, 0, 1, 2][n] if arm == "candidate" else 100
                attempts.append(
                    Attempt(
                        f"{arm}-{n}-{repeat}",
                        "run",
                        TASKS[n],
                        arm,
                        "pass" if passed else "fail",
                        {"Input tokens": tokens, "Output tokens": 0},
                    )
                )
    return Evidence(attempts)


def test_bootstrap_numbers_and_task_weighting_are_fixed() -> None:
    evidence = _evidence()
    # Three repeats with median 12.5 and mean 25: ratios must use the mean.
    first = evidence.attempts.pop(0)
    second = evidence.attempts.pop(0)
    evidence.attempts += [
        replace(first, id=f"extra-{n}", values={"Input tokens": tokens, "Output tokens": 0})
        for n, tokens in enumerate([12.5, 12.5, 50])
    ]
    assert first.correctness == second.correctness == "fail"
    result = paired_comparison(evidence, GATES, "candidate", "vanilla", TASKS)

    assert list(result.passes.tasks.values()) == [-1, -0.5, 0, 0, 0.5, 1]
    assert list(result.tokens.tasks.values()) == [0.25, 0.5, 1, 1, 2, 4]
    assert result.passes.value == 0
    assert result.passes.interval == pytest.approx((-5 / 12, 5 / 12))
    assert result.tokens.value == pytest.approx(1)
    assert result.tokens.interval == pytest.approx((2 ** (-5 / 6), 2 ** (5 / 6)))
    # Duplicating a task's attempts must not turn them into independent samples.
    evidence.attempts += [
        replace(row, id=f"copy-{row.id}") for row in evidence.attempts if row.task == TASKS[0]
    ]
    reordered = paired_comparison(
        Evidence(list(reversed(evidence.attempts))),
        GATES,
        "candidate",
        "vanilla",
        list(reversed(TASKS)),
    )
    assert reordered == result


@pytest.mark.parametrize(
    ("differences", "expected"),
    [
        ([1] * 6, 0.03125),
        ([1] * 5 + [-0.5, 0], 0.21875),
        ([-1] * 6, 0.03125),
        ([0] * 6, 1),
        ([], 1),
    ],
)
def test_exact_sign_test_ignores_ties(differences: list[float], expected: float) -> None:
    assert sign_test(differences) == expected


def test_bound_gates_use_the_interval_endpoint_and_need_six_tasks() -> None:
    evidence = _evidence()
    correctness = check_gate(evidence, GATES, PASS_GATE, "candidate", TASKS)
    tokens = check_gate(evidence, GATES, TOKEN_GATE, "candidate", TASKS)
    assert correctness.result == tokens.result == "fail"
    assert correctness.candidate == pytest.approx(-5 / 12)
    assert tokens.candidate == pytest.approx(2 ** (5 / 6))
    assert (
        check_gate(
            evidence, GATES, PASS_GATE.model_copy(update={"margin": -0.5}), "candidate", TASKS
        ).result
        == "pass"
    )
    assert (
        check_gate(
            evidence, GATES, TOKEN_GATE.model_copy(update={"factor": 2}), "candidate", TASKS
        ).result
        == "pass"
    )
    for gate in (PASS_GATE, TOKEN_GATE):
        verdict = check_gate(evidence, GATES, gate, "candidate", TASKS[:5])
        assert verdict.result == "undetermined" and "too few tasks" in verdict.detail
        assert verdict.candidate is None


def test_exclusions_are_visible_and_cannot_pass_a_bound_gate() -> None:
    evidence = _evidence()
    original = evidence.attempts[0]
    # Six good pairs remain; missing evidence must still block a gate.
    evidence.attempts += [
        replace(original, id="unknown", task="unknown", correctness="unknown"),
        replace(original, id="unknown-baseline", task="unknown", arm="vanilla"),
        replace(original, id="missing-tokens", task="missing-tokens", values={}),
        replace(original, id="missing-baseline", task="missing-tokens", arm="vanilla"),
    ]
    tasks = [*TASKS, "unknown", "missing-tokens", "absent"]
    result = paired_comparison(evidence, GATES, "candidate", "vanilla", tasks)
    assert result.passes.interval is not None and result.tokens.interval is not None
    assert set(result.passes.missing) == {"unknown", "absent"}
    assert set(result.tokens.missing) == {"missing-tokens", "absent"}
    for gate in (PASS_GATE, TOKEN_GATE):
        verdict = check_gate(evidence, GATES, gate, "candidate", tasks)
        assert verdict.result == "undetermined" and "incomplete task pairs" in verdict.detail


@pytest.mark.parametrize("bad_total", [None, 0, -1, math.inf, math.nan])
def test_incomplete_or_invalid_tokens_are_excluded(bad_total: float | None) -> None:
    evidence = _evidence()
    evidence.attempts[0] = replace(
        evidence.attempts[0], values={"Input tokens": bad_total, "Output tokens": 0}
    )
    # A single missing repeat excludes the task; zero tokens is invalid only when the task mean is zero.
    if bad_total == 0:
        evidence.attempts[1] = replace(
            evidence.attempts[1], values={"Input tokens": 0, "Output tokens": 0}
        )
    result = paired_comparison(evidence, GATES, "candidate", "vanilla", TASKS)
    assert TASKS[0] in result.tokens.missing
    assert len(result.passes.tasks) == 6


def test_overlap_policy_applies_to_paired_pass_rates() -> None:
    evidence = _evidence()
    evidence.attempts = [
        replace(row, correctness="overlap_pass") if row.arm == "candidate" else row
        for row in evidence.attempts
    ]
    counted = paired_comparison(evidence, GATES, "candidate", "vanilla", TASKS)
    excluded = paired_comparison(
        evidence,
        GATES.model_copy(update={"count_overlap_passes": False}),
        "candidate",
        "vanilla",
        TASKS,
    )
    assert counted.passes.value == 0.5
    assert excluded.passes.value == -0.5


@pytest.mark.parametrize(
    "changes",
    [
        {"bound": None},
        {"direction": "lower"},
        {"bound": "upper"},
        {"margin": None},
        {"tolerance": 1},
        {"per_task": True},
        {"correctness": "unknown"},
        {"statistic": "total"},
        {"factor": 1.3},
        {"margin": -1.1},
    ],
)
def test_invalid_bound_contracts_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Gate.model_validate(PASS_GATE.model_dump() | changes)


def test_cli_cross_run_pairs_match_a_single_run(tmp_path: Path) -> None:
    evidence = _evidence()
    for name, attempts in (
        ("combined", evidence.attempts),
        ("candidate", [row for row in evidence.attempts if row.arm == "candidate"]),
        ("baseline", [row for row in evidence.attempts if row.arm == "vanilla"]),
    ):
        report = tmp_path / name / "report"
        report.mkdir(parents=True)
        (report / "results.json").write_text(
            json.dumps(
                {
                    "attempts": [
                        {
                            "id": row.id,
                            "task": row.task,
                            "guidance": row.arm,
                            "correctness": row.correctness,
                        }
                        for row in attempts
                    ],
                    "observations": [
                        {
                            "attempt_id": row.id,
                            "metric": metric,
                            "value": value,
                            "status": "measured",
                        }
                        for row in attempts
                        for metric, value in row.values.items()
                    ],
                }
            )
        )
    gates = tmp_path / "gates.yaml"
    gates.write_text(yaml.safe_dump(GATES.model_dump(exclude_none=True)))
    runner = CliRunner()
    single = runner.invoke(cli.app, ["compare", str(tmp_path / "combined"), "--gates", str(gates)])
    cross = runner.invoke(
        cli.app,
        [
            "compare",
            f"{tmp_path / 'baseline'}=vanilla",
            f"{tmp_path / 'candidate'}=candidate",
            "--gates",
            str(gates),
        ],
    )
    assert single.exit_code == cross.exit_code == 0
    assert single.stdout == cross.stdout
    assert "[-0.416667, 0.416667]" in single.stdout
    assert "[0.561231, 1.7818]" in single.stdout
    assert "exact sign-test p (two-sided)" in single.stdout
    assert "task-0" in single.stdout
