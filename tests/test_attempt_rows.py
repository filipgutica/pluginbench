"""Attempt rows copy named report fields only, keep unknowns unknown, and read both schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pluginbench import dashboard
from pluginbench.attempt_rows import attempt_rows

SENTINEL = "PRIVATE-SENTINEL"
RUNNER = {"harness": "codex", "model": "m", "reasoning": "high", "cli_version": "0.1"}


def _observation(attempt: str, metric: str, value: float, status: str = "measured") -> dict[str, Any]:
    return {
        "attempt_id": attempt,
        "metric": metric,
        "value": value,
        "status": status,
        "reason": SENTINEL,
        "provenance": {"path": SENTINEL},
        "snapshot_digest": SENTINEL,
    }


def _report(*, schema: int) -> dict[str, Any]:
    vanilla = "vanilla" if schema == 2 else "baseline"
    origin = "vanilla_origin" if schema == 2 else "baseline_origin"
    return {
        "schema_version": schema,
        "name": SENTINEL,
        "limitations": [SENTINEL],
        "attempts": [
            {
                "id": "a1",
                "task": "task-a",
                "repeat": 1,
                "guidance": vanilla,
                "correctness": "pass",
                "requested_model": "m",
                "actual_model": "m",
                "runtime": {
                    "harness": "codex-cli",
                    "harness_version": "0.1",
                    "reasoning_effort": "high",
                    "requested_reasoning_effort": "high",
                    "effort_evidence_source": SENTINEL,
                },
                "execution": {
                    "status": "complete",
                    "timed_out": False,
                    "exit_code": 0,
                    "error": SENTINEL,
                    "partial_usage": [{"session_id": SENTINEL}],
                },
                "artifacts": {"patch": SENTINEL},
                origin: {"origin_cohort": SENTINEL},
            },
            {"id": "a2", "task": "task-a", "repeat": 1, "guidance": "workflow"},
        ],
        "observations": [
            _observation("a1", "Input tokens", 100),
            _observation("a1", "Output tokens", 10),
            _observation("a1", "Coding time", 61.5),
            _observation("a1", "Source lines added", 0),
            _observation("a1", "Test lines added", 7, status="unknown"),
            _observation("a2", "Input tokens", 5, status="unknown"),
            _observation("a1", "P0 review findings", 0),
            _observation("a1", "P1 review findings", 2),
            _observation("a1", "P2 review findings", 3, status="unknown"),
            _observation("a1", "P3 review findings", 4),
            _observation("a1", "Verification iterations (transcript count)", 5),
            _observation("a1", "Net cognitive complexity added", -1),
            _observation("a1", "Changed-line coverage %", 9, status="unavailable"),
        ],
    }


@pytest.mark.parametrize("schema", [1, 2])
def test_rows_copy_named_fields_and_never_private_ones(schema: int) -> None:
    first, second = attempt_rows(_report(schema=schema), RUNNER)

    assert tuple(first) == (
        "runner",
        "attempt_id",
        "task",
        "repeat",
        "arm",
        "correctness",
        "execution_status",
        "timed_out",
        "exit_code",
        "requested_model",
        "actual_model",
        "harness",
        "harness_version",
        "reasoning_effort",
        "requested_reasoning_effort",
        "tokens",
        "coding_seconds",
        "lines",
        "measures",
        "reused_vanilla",
    )
    assert first["runner"] == RUNNER
    assert first["arm"] == "vanilla"
    assert first["reused_vanilla"] is True
    assert first["harness"] == "codex-cli"
    assert (first["execution_status"], first["timed_out"], first["exit_code"]) == ("complete", False, 0)
    assert first["tokens"] == {
        "input": 100,
        "cached_input": None,
        "uncached_input": None,
        "output": 10,
        "reasoning_output": None,
    }
    assert first["coding_seconds"] == 61.5
    # A measured zero stays zero; an unknown measure stays null.
    assert first["lines"] == {
        "source_added": 0,
        "source_removed": None,
        "test_added": None,
        "test_removed": None,
    }
    # Each measure keeps the report's status: a measured zero, a measure that came back unknown,
    # one the run did not measure, and one the report has no row for all stay distinct.
    assert first["measures"] == {
        "p0_findings": {"value": 0, "status": "measured"},
        "p1_findings": {"value": 2, "status": "measured"},
        "p2_findings": {"value": None, "status": "unknown"},
        "verification_iterations": {"value": 5, "status": "measured"},
        "net_cognitive_complexity": {"value": -1, "status": "measured"},
        "changed_line_coverage_percent": {"value": None, "status": "unavailable"},
        "mutation_bug_catch_percent": {"value": None, "status": None},
    }
    assert second["tokens"]["input"] is None
    assert second["reused_vanilla"] is False
    assert second["execution_status"] is None
    assert SENTINEL not in json.dumps([first, second])


def test_the_api_lists_rows_of_runs_with_a_report_and_keeps_them_when_one_is_mid_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    (root / "report").mkdir(parents=True)
    (root / "evaluation.json").write_text(json.dumps({"name": "fixture", "runner": RUNNER}))
    (root / "evaluation-binding.json").write_text("{}")
    unreported = tmp_path / "pending"
    unreported.mkdir()
    (unreported / "evaluation.json").write_text("{}")
    (unreported / "evaluation-binding.json").write_text("{}")
    (root / "report/results.json").write_text(json.dumps(_report(schema=2)))
    runs = dashboard.Runs([tmp_path])

    listed = runs.attempts()
    (root / "report/results.json").write_text('{"attempts": [')  # A report caught mid-write.
    during = runs.attempts()

    key = dashboard.run_key(root)
    assert [(row["run_key"], row["run_name"], row["attempt_id"]) for row in listed["attempts"]] == [
        (key, "fixture", "a1"),
        (key, "fixture", "a2"),
    ]
    assert listed["errors"] == []
    assert during["attempts"] == listed["attempts"]
    assert [(error["key"], error["run"], error["kept"]) for error in during["errors"]] == [
        (key, "fixture", True)
    ]
    assert SENTINEL not in json.dumps(listed)
    # Read first while mid-write: no rows to keep, and the error says so.
    [first] = dashboard.Runs([tmp_path]).attempts()["errors"]
    assert (first["run"], first["kept"]) == (str(root), False)
