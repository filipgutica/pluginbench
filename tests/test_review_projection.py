from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pluginbench import codex, remeasure, study, study_review
from pluginbench.collectors import Collector
from test_study_review import _existing_study, _judge


def _rated_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    def invoke(*, area: Path, **kwargs: object) -> dict[str, object]:
        packet_id = json.loads((area / "workspace/packet.json").read_text())["packet_id"]
        issue = {
            "issue_id": "one",
            "severity": "P2",
            "category": "maintainability",
            "description": "Synthetic projection finding",
            "rationale": "Exercises fixed review reporting",
            "path": "src/answer.py",
            "line": 1,
            "evidence": "answer = 1",
            "smallest_fix": "Apply the existing abstraction",
        }
        return {
            "successful_native_completion": True,
            "actual_primary_models": ["gpt-6-astra"],
            "runtime_effort_evidence": ["high"],
            "startup": [{"type": "session_meta", "payload": {"cli_version": "test"}}],
            "command_items": [],
            "usage": {"input_tokens": 20, "output_tokens": 10},
            "final": json.dumps(
                {
                    "packet_id": packet_id,
                    "status": "reviewed",
                    "issues": [issue],
                    "limitations": [],
                    "outside_packet_access": False,
                }
            ),
        }

    monkeypatch.setattr(codex, "invoke", invoke)


@pytest.mark.parametrize("measurement_name", ["metrics", "original"])
def test_fixed_review_projection_preserves_completed_measurements_and_old_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measurement_name: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root)
    study_review.review_study(root)
    config = tmp_path / "coverage-config.json"
    config.write_text(json.dumps({"test_scope": ["tests/**"]}))
    adapter = tmp_path / "coverage.py"
    adapter.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({"
        "'files_measured':['src/answer.py'],"
        "'branches':{'total':['src/answer.py:1:0'],'covered':['src/answer.py:1:0']},"
        "'functions':{'total':['src/answer.py:1:0'],'covered':['src/answer.py:1:0']}}))\n"
    )
    remeasure.propose_remeasurement(
        root,
        profiles={
            "answer": [
                Collector(
                    kind="coverage",
                    config=config,
                    scope=["src/answer.py"],
                    command=[sys.executable, str(adapter), "{snapshot}", "{output}"],
                    tool_version="projection-fixture",
                )
            ]
        },
        revision=measurement_name,
    )
    old_report, _ = study.report_study(root, measurement_revision=measurement_name)
    original = json.loads(old_report.read_text())
    protected = study._inventory(root)
    guidance = tmp_path / "review-guidance.md"
    guidance.write_text("Keep the code maintainable.\n")
    _rated_judge(monkeypatch)
    study_review.review_study(root, revision="fixed", guidance=[guidance])
    path, _ = study.report_study(
        root, measurement_revision=measurement_name, review_revision="fixed"
    )
    assert (
        path.parent == root / "review-revisions/fixed/report-native/measurements" / measurement_name
    )
    preserved_projection = path.read_bytes()
    native_path, _ = study.report_study(root, review_revision="fixed")
    assert native_path.parent == root / "review-revisions/fixed/report-native/original"
    assert path.read_bytes() == preserved_projection
    after = study._inventory(root)
    assert all(after[name] == digest for name, digest in protected.items())
    remeasure.validate_remeasurement(root, root / "observer-revisions" / measurement_name)
    projected = json.loads(path.read_text())
    assert projected["measurement_revision"] == original["measurement_revision"]
    for old, new in zip(original["attempts"], projected["attempts"], strict=True):
        assert {key: value for key, value in old.items() if key != "independent_review"} == {
            key: value for key, value in new.items() if key != "independent_review"
        }
        review = new["independent_review"]
        assert review["finding_count"] == 1
        assert review["severity_counts"] == {"P0": 0, "P1": 0, "P2": 1, "P3": 0}
        assert review["category_counts"] == {
            "correctness": 0,
            "scope": 0,
            "maintainability": 1,
            "tests": 0,
        }
        assert review["issues"][0]["evidence"] == "answer = 1"
        assert review["criteria_sha256"] == projected["independent_review"]["criteria_sha256"]


def test_legacy_review_has_categories_without_invented_severities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root)
    study_review.review_study(root)
    report = json.loads((root / "report/assessment-report.json").read_text())
    for attempt in report["attempts"]:
        review = attempt["independent_review"]
        assert review["category_counts"]["maintainability"] == 1
        assert review["severity_counts"] is None
        assert review["criteria_sha256"] is None
