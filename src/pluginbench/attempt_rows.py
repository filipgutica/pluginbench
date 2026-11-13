"""One flat row per coding attempt, copied from a run's report/results.json and its runner.

Values are copied, never derived: a measure the report did not measure is null, not 0. Names
follow the results site plan's per-attempt table where it copies the same value; the plan's
normalized fields (token states, reason codes, activation) are not computed here. Only the
keys in FIELDS reach a row, so private report fields such as paths, provenance, artifacts and
execution errors never do.
"""

from __future__ import annotations

from typing import Any

from pluginbench.arms import normalize_arm_name

RUNNER = ("harness", "model", "reasoning", "cli_version")
RUNTIME = ("harness", "harness_version", "reasoning_effort", "requested_reasoning_effort")
TOKENS = {
    "input": "Input tokens",
    "cached_input": "Cached input tokens",
    "uncached_input": "Uncached input tokens",
    "output": "Output tokens",
    "reasoning_output": "Reasoning output tokens",
}
LINES = {
    "source_added": "Source lines added",
    "source_removed": "Source lines removed",
    "test_added": "Test lines added",
    "test_removed": "Test lines removed",
}
FIELDS = (
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
    *RUNTIME,
    "tokens",
    "coding_seconds",
    "lines",
    "reused_vanilla",
)


def attempt_rows(report: dict[str, Any], runner: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for every attempt in a schema 1 or 2 report, in the report's order."""
    measured: dict[tuple[str, str], Any] = {
        (row["attempt_id"], row["metric"]): row.get("value")
        for row in report.get("observations", [])
        if row.get("status") == "measured"
    }
    rows = []
    for attempt in report.get("attempts", []):
        identifier = attempt["id"]
        execution = attempt.get("execution") or {}
        runtime = attempt.get("runtime") or {}
        # Schema 1 reports call the reuse record baseline_origin.
        origin = attempt.get("vanilla_origin", attempt.get("baseline_origin"))
        row = {
            "runner": {key: runner.get(key) for key in RUNNER},
            "attempt_id": identifier,
            "task": attempt.get("task"),
            "repeat": attempt.get("repeat"),
            "arm": normalize_arm_name(str(attempt.get("guidance"))),
            "correctness": attempt.get("correctness"),
            "execution_status": execution.get("status"),
            "timed_out": execution.get("timed_out"),
            "exit_code": execution.get("exit_code"),
            "requested_model": attempt.get("requested_model"),
            "actual_model": attempt.get("actual_model"),
            **{key: runtime.get(key) for key in RUNTIME},
            "tokens": {key: measured.get((identifier, metric)) for key, metric in TOKENS.items()},
            "coding_seconds": measured.get((identifier, "Coding time")),
            "lines": {key: measured.get((identifier, metric)) for key, metric in LINES.items()},
            "reused_vanilla": origin is not None,
        }
        rows.append({key: row[key] for key in FIELDS})
    return rows
