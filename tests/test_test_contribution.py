from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pluginbench import test_contribution
from pluginbench.assessment_metrics import _test_contribution, inspect_snapshot
from pluginbench.collectors import CollectorUnavailable

POSITIVE = "tests/test_existing.py::test_positive"
DUPLICATE = "tests/test_added.py::test_duplicate"
NEGATIVE = "tests/test_added.py::test_negative"
SKIPPED = "tests/test_existing.py::test_skipped"


def _snapshots(root: Path) -> tuple[Path, Path]:
    before, after = root / "before", root / "after"
    for snapshot in (before, after):
        (snapshot / "src").mkdir(parents=True)
        (snapshot / "tests").mkdir()
        (snapshot / "src/logic.py").write_text(
            'def classify(value):\n    if value > 0:\n        return "positive"\n    return "other"\n'
        )
        (snapshot / "tests/test_existing.py").write_text("existing\n")
    (after / "tests/test_added.py").write_text("added\n")
    return before, after


def _coverage(excluded: str | None) -> dict[str, object]:
    arcs = [[2, 3]] if excluded == NEGATIVE else [[2, 3], [2, 4]]
    missing = [[2, 4]] if excluded == NEGATIVE else []
    return {
        "meta": {"format": 3, "version": "7.10.7", "branch_coverage": True},
        "files": {
            "src/logic.py": {
                "executed_lines": [1, 2, 3, 4],
                "missing_lines": [],
                "excluded_lines": [],
                "executed_branches": arcs,
                "missing_branches": missing,
                "summary": {
                    "num_statements": 4,
                    "covered_lines": 4,
                    "missing_lines": 0,
                    "excluded_lines": 0,
                    "num_branches": 2,
                    "covered_branches": len(arcs),
                    "missing_branches": len(missing),
                },
            }
        },
    }


def _native(monkeypatch: pytest.MonkeyPatch, fault: str | None = None) -> list[str | None]:
    calls: list[str | None] = []

    def execute(**kwargs: object) -> dict[str, object]:
        command, destination = kwargs["command"], kwargs["destination"]
        assert isinstance(command, list) and isinstance(destination, Path)
        settings = json.loads(command[-1])
        excluded = settings["excluded_id"]
        calls.append(excluded)
        receipt: dict[str, object] = {"exit_code": 0, "timed_out": False, "report_collected": True}
        statuses = {POSITIVE: "passed", DUPLICATE: "passed", NEGATIVE: "passed", SKIPPED: "pending"}
        if excluded is not None:
            statuses.pop(excluded)
        report = {
            "pytest_version": "8.4.2",
            "coverage_version": "7.10.7",
            "exit_code": 0,
            "errors": [],
            "collected_ids": [POSITIVE, DUPLICATE, NEGATIVE, SKIPPED],
            "statuses": statuses,
            "coverage": _coverage(excluded),
        }
        if fault == "timeout":
            receipt["timed_out"] = True
        elif fault == "native-failed":
            receipt["exit_code"] = 1
        elif fault == "no-report":
            receipt["report_collected"] = False
        elif fault == "collection-error":
            report["errors"] = ["collection failed"]
        elif fault == "failed-test":
            statuses[POSITIVE] = "failed"
        elif fault == "tool-version":
            report["coverage_version"] = "unexpected"
        elif fault == "missing-status":
            statuses.pop(POSITIVE)
        elif fault == "changed-suite" and excluded is not None:
            report["collected_ids"] = [DUPLICATE, NEGATIVE]
        elif fault == "changed-branches" and excluded is not None:
            coverage = report["coverage"]
            assert isinstance(coverage, dict)
            coverage["files"]["src/logic.py"]["missing_branches"].append([2, -1])
            coverage["files"]["src/logic.py"]["summary"]["num_branches"] += 1
            coverage["files"]["src/logic.py"]["summary"]["missing_branches"] += 1
        elif fault == "frozen-mutation":
            snapshot = kwargs["snapshot"]
            assert isinstance(snapshot, Path)
            (snapshot / "src/logic.py").write_text("mutated\n")
        destination.mkdir(parents=True)
        (destination / "report.raw").write_text(json.dumps(report))
        return receipt

    monkeypatch.setattr(test_contribution, "run_native_command", execute)
    return calls


def _collect(before: Path, after: Path, destination: Path) -> dict[str, object]:
    return test_contribution.collect_pytest_contribution(
        snapshot=after,
        before_snapshot=before,
        task_identity={"workdir": "/app"},
        destination=destination,
        source_scope=["src/**"],
        test_scope=["tests/**"],
        pytest_version="8.4.2",
        coverage_version="7.10.7",
    )


def test_complete_leave_one_out_counts_are_repeatable_and_scope_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, after = _snapshots(tmp_path)
    digests = [inspect_snapshot(root)["digest"] for root in (before, after)]
    calls = _native(monkeypatch)
    results = [_collect(before, after, tmp_path / str(index)) for index in range(2)]
    assert results[0] == results[1]
    assert calls == [None, DUPLICATE, NEGATIVE] * 2
    result = results[0]
    branches = result["branches"]
    assert isinstance(branches, dict)
    files = inspect_snapshot(after)["files"]
    assert isinstance(files, dict)
    normalized = _test_contribution(
        copy.deepcopy(result),
        set(files),
        set(branches["total"]),
        set(branches["covered"]),
        set(),
        set(),
    )
    assert normalized["reference_count"] == 3
    assert normalized["pending_count"] == 1
    assert normalized["candidate_count"] == normalized["tested_count"] == 2
    assert normalized["zero_marginal_ids"] == [DUPLICATE]
    assert "functions" not in result
    assert [inspect_snapshot(root)["digest"] for root in (before, after)] == digests


def test_unchanged_test_files_are_valid_zero_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, after = _snapshots(tmp_path)
    calls = _native(monkeypatch)
    result = _collect(after, after, tmp_path / "evidence")
    assert calls == [None]
    row = result["contribution"]
    assert isinstance(row, dict)
    assert row["candidate_ids"] == row["removal_runs"] == row["zero_marginal_ids"] == []


@pytest.mark.parametrize(
    "fault", ["timeout", "native-failed", "no-report", "collection-error", "failed-test"]
)
def test_failed_measurement_stays_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    before, after = _snapshots(tmp_path)
    _native(monkeypatch, fault)
    with pytest.raises(CollectorUnavailable):
        _collect(before, after, tmp_path / "evidence")


@pytest.mark.parametrize(
    "fault", ["tool-version", "missing-status", "changed-suite", "changed-branches"]
)
def test_malformed_or_unstable_evidence_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    before, after = _snapshots(tmp_path)
    _native(monkeypatch, fault)
    with pytest.raises(ValueError):
        _collect(before, after, tmp_path / "evidence")
