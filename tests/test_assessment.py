from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from fallow_fixture import syntax_proof

from pluginbench.assessment import _code_size, build_assessment, write_assessment
from pluginbench.assessment_metrics import (
    EvidenceBinding,
    bind_evidence,
    compare_snapshots,
    inspect_snapshot,
)
from pluginbench.assessment_models import Attempt, Snapshot, Stage

DEMO = Path(__file__).resolve().parents[1] / "tests/fixtures/standards-assessment"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _demo(tmp_path: Path) -> Path:
    shutil.copytree(DEMO, tmp_path / "study")
    return tmp_path / "study/study.yaml"


def _save(path: Path, data: object) -> None:
    path.write_text(json.dumps(data))


def _rebind_ledger(study: Path) -> None:
    manifest = yaml.safe_load(study.read_text())
    manifest["records"][0]["correction_ledger"]["sha256"] = _digest(study.parent / "ledger.json")
    _save(study, manifest)


def test_portable_demo_and_immutable_output(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    report = build_assessment(study)
    attempt = report["attempts"][0]
    assert [stage["accepted"] for stage in attempt["stages"]] == [False, True]
    assert attempt["correction_burden"]["verified_issue_count"] == 1
    assert attempt["work_to_acceptance"] is None
    json_path, markdown_path = write_assessment(report, tmp_path / "output")
    assert json.loads(json_path.read_text()) == report
    assert "answer-demo/final" in markdown_path.read_text()
    with pytest.raises(ValueError, match="outside every input snapshot"):
        write_assessment(report, study.parent / "snapshots/final/report")
    assert not (study.parent / "snapshots/final/report").exists()


@pytest.mark.parametrize("status, count", [("reviewed", 0), ("reviewed", 4), ("failed", None)])
def test_markdown_separates_selected_review_from_recorded_ledger(
    tmp_path: Path, status: str, count: int | None
) -> None:
    report = build_assessment(_demo(tmp_path))
    report["independent_review"] = {
        "status": "complete",
        "stage": "handoff",
        "native_invocation_requests": 1,
        "requested_model": "gpt-6-astra",
        "requested_reasoning_effort": "high",
    }
    report["attempts"][0]["independent_review"] = {
        "stage": "handoff",
        "stage_id": "handoff",
        "status": status,
        "finding_count": count,
    }
    _, markdown_path = write_assessment(report, tmp_path / "output")
    text = markdown_path.read_text()
    assert "| Selected review findings | Recorded ledger issues/resolutions/rounds |" in text
    task_id = report["attempts"][0]["task_id"]
    rows = [line for line in text.splitlines() if line.startswith(f"| {task_id} /")]
    assert len(rows) == 2
    cells = [row.split("|")[-3:-1] for row in rows]
    assert [[cell.strip() for cell in row] for row in cells] == [
        [str(count) if count is not None else "unknown", "1/1/unknown"],
        ["not selected", "1/1/unknown"],
    ]


def test_infrastructure_failure_retained_without_inventing_source(tmp_path: Path) -> None:
    study = tmp_path / "study.json"
    _save(
        study,
        {
            "schema_version": 1,
            "name": "failed setup",
            "records": [
                {
                    "id": "assigned",
                    "task_id": "feature",
                    "arm": "baseline",
                    "before": None,
                    "stages": [{"id": "handoff"}],
                }
            ],
        },
    )
    report = build_assessment(study)
    attempt = report["attempts"][0]
    assert attempt["before_digest"] is None
    assert attempt["before_snapshot_metadata"] is None
    assert attempt["before_metrics"] == {}
    assert attempt["stages"][0]["change"] is None
    assert attempt["stages"][0]["selected_checks_pass"] is None
    assert report["snapshot_roots"] == []
    json_path, _ = write_assessment(report, tmp_path / "output")
    assert len(json.loads(json_path.read_text())["attempts"]) == 1


@pytest.mark.parametrize(
    "target", ["snapshots/handoff/src/answer.py", "evidence/final-correctness.json"]
)
def test_rejects_tampered_source_and_receipts(tmp_path: Path, target: str) -> None:
    study = _demo(tmp_path)
    path = study.parent / target
    path.write_text(path.read_text() + "\nmodified")
    with pytest.raises(ValueError, match="digest mismatch"):
        build_assessment(study)


def test_reports_actual_filtering_without_changing_snapshot_binding(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    for name in ("before", "handoff", "final"):
        excluded = study.parent / "snapshots" / name / ".agents"
        excluded.mkdir()
        (excluded / "SKILL.md").write_text(f"Excluded guidance for {name}\n")
    attempt = build_assessment(study)["attempts"][0]
    metadata = [
        attempt["before_snapshot_metadata"],
        *(stage["snapshot_metadata"] for stage in attempt["stages"]),
    ]
    assert [item["digest"] for item in metadata] == [
        attempt["before_digest"],
        *(stage["snapshot_digest"] for stage in attempt["stages"]),
    ]
    for item in metadata:
        assert ".agents" in item["exclusion_names"]
        assert item["excluded_paths"] == [".agents"]
        assert set(item["files"]) == {"src/answer.py"}
    stages = attempt["stages"]
    assert stages[-1]["change"]["before_digest"] == metadata[0]["digest"]
    assert stages[-1]["change"]["after_digest"] == metadata[-1]["digest"]
    assert stages[-1]["correctness"]["snapshot_digest"] == metadata[-1]["digest"]


def test_rejects_receipt_bound_to_another_stage(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    receipt_path = study.parent / "evidence/final-correctness.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["snapshot_digest"] = "sha256:" + "a" * 64
    _save(receipt_path, receipt)
    manifest = yaml.safe_load(study.read_text())
    manifest["records"][0]["stages"][1]["correctness"]["sha256"] = _digest(receipt_path)
    _save(study, manifest)
    with pytest.raises(ValueError, match="bound to another snapshot"):
        build_assessment(study)


def test_verified_review_issue_blocks_passing_handoff(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    receipt_path = study.parent / "evidence/handoff-correctness.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["checks"]["behavior"] = True
    _save(receipt_path, receipt)
    manifest = yaml.safe_load(study.read_text())
    manifest["records"][0]["stages"][0]["correctness"]["sha256"] = _digest(receipt_path)
    for stage in manifest["records"][0]["stages"]:
        stage["work"] = {"elapsed_seconds": 2.0}
    _save(study, manifest)
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    proof_path = study.parent / "evidence/issue-answer.json"
    proof = json.loads(proof_path.read_text())
    proof["category"] = ledger["corrections"][0]["category"] = "maintainability"
    _save(proof_path, proof)
    ledger["corrections"][0]["external_verification"]["sha256"] = _digest(proof_path)
    _save(ledger_path, ledger)
    _rebind_ledger(study)
    attempt = build_assessment(study)["attempts"][0]
    handoff, final = attempt["stages"]
    assert handoff["selected_checks_pass"] is True
    assert handoff["accepted"] is False
    assert handoff["outstanding_verified_issues"] == ["answer-42"]
    assert final["accepted"] is True
    assert attempt["work_to_acceptance"] == {"stage": "final", "elapsed_seconds": 4.0}


@pytest.mark.parametrize("history", ["missing", "unattested_empty", "attested_empty"])
def test_correction_unknown_lower_bound_and_known_zero(tmp_path: Path, history: str) -> None:
    study = _demo(tmp_path)
    manifest = yaml.safe_load(study.read_text())
    if history == "missing":
        del manifest["records"][0]["correction_ledger"]
        _save(study, manifest)
    else:
        ledger_path = study.parent / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["corrections"] = []
        if history == "unattested_empty":
            del ledger["completeness"]
        _save(ledger_path, ledger)
        _rebind_ledger(study)
    attempt = build_assessment(study)["attempts"][0]
    burden = attempt["correction_burden"]
    assert (
        burden["status"]
        == {
            "missing": "unavailable",
            "unattested_empty": "incomplete",
            "attested_empty": "available",
        }[history]
    )
    assert burden["verified_issue_count"] == (None if history == "missing" else 0)
    assert attempt["stages"][-1]["accepted"] == (True if history == "attested_empty" else None)


def test_repeats_are_independent_and_missing_checks_do_not_pass(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    manifest = yaml.safe_load(study.read_text())
    first = manifest["records"][0]
    del first["correction_ledger"]
    second = json.loads(json.dumps(first))
    second.update(id="repeat", attempt=2)
    del second["stages"][1]["correctness"]
    manifest["records"].append(second)
    _save(study, manifest)
    attempts = build_assessment(study)["attempts"]
    assert len(attempts) == 2
    assert attempts[1]["stages"][-1]["correctness"]["status"] == "unknown"
    assert all(attempt["correction_burden"]["verified_issue_count"] is None for attempt in attempts)


def test_issue_count_is_distinct_from_correction_events_and_rounds(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    correction = json.loads(json.dumps(ledger["corrections"][0]))
    correction["round"] = 2
    proof = json.loads((study.parent / "evidence/issue-answer.json").read_text())
    proof["round"] = 2
    proof_path = study.parent / "evidence/issue-again.json"
    _save(proof_path, proof)
    correction["external_verification"] = {
        "path": "evidence/issue-again.json",
        "sha256": _digest(proof_path),
    }
    ledger["corrections"].append(correction)
    _save(ledger_path, ledger)
    _rebind_ledger(study)
    burden = build_assessment(study)["attempts"][0]["correction_burden"]
    assert burden["verified_issue_count"] == 1
    assert burden["verified_correction_events"] == 2
    assert burden["external_rounds"] is None  # Legacy issue receipts do not count reviews.


def _record_reviews(study: Path, reviews: list[tuple[str, list[str]]]) -> None:
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["review_rounds"] = []
    for number, (stage, issue_ids) in enumerate(reviews, start=1):
        receipt_path = study.parent / f"evidence/review-{number}.json"
        _save(
            receipt_path,
            {
                "schema_version": 1,
                "round": number,
                "stage": stage,
                "issue_ids": issue_ids,
                "stage_digests": {stage: ledger["stage_digests"][stage]},
                "reviewer": "independent-reviewer",
                "independent": True,
            },
        )
        ledger["review_rounds"].append(
            {
                "round": number,
                "stage": stage,
                "issue_ids": issue_ids,
                "external_verification": {
                    "path": str(receipt_path.relative_to(study.parent)),
                    "sha256": _digest(receipt_path),
                },
            }
        )
    _save(ledger_path, ledger)
    _rebind_ledger(study)


def _leave_issue_unresolved(study: Path) -> None:
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["corrections"][0]["to_stage"] = None
    proof_path = study.parent / "evidence/issue-answer.json"
    proof = json.loads(proof_path.read_text())
    proof["to_stage"] = None
    proof["stage_digests"].pop("final")
    _save(proof_path, proof)
    ledger["corrections"][0]["external_verification"]["sha256"] = _digest(proof_path)
    _save(ledger_path, ledger)
    _rebind_ledger(study)


def test_reviews_include_no_finding_round_after_verified_resolution(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    _record_reviews(study, [("handoff", ["answer-42"]), ("final", [])])
    attempt = build_assessment(study)["attempts"][0]
    burden = attempt["correction_burden"]
    assert burden["status"] == "available"
    assert burden["external_rounds"] == 2
    assert burden["verified_correction_events"] == 1
    assert [stage["accepted"] for stage in attempt["stages"]] == [False, True]


def test_no_finding_review_records_zero_corrections(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["corrections"] = []
    _save(ledger_path, ledger)
    _record_reviews(study, [("final", [])])
    attempt = build_assessment(study)["attempts"][0]
    burden = attempt["correction_burden"]
    assert burden["external_rounds"] == 1
    assert burden["verified_issue_count"] == burden["verified_correction_events"] == 0
    assert burden["status"] == "incomplete"
    assert burden["completeness_verified"] is False
    assert burden["counts_are_lower_bounds"] is True
    assert burden["outstanding_by_stage"]["handoff"] is None
    assert attempt["stages"][0]["outstanding_verified_issues"] is None
    assert attempt["stages"][0]["accepted"] is False  # Its external behavior check failed.
    assert attempt["stages"][-1]["accepted"] is True
    assert attempt["work_to_acceptance"] is None


def test_unverified_selected_stage_finding_cannot_establish_clean_acceptance(
    tmp_path: Path,
) -> None:
    study = _demo(tmp_path)
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    issue = ledger["corrections"][0]
    issue.update(from_stage="final", to_stage=None)
    issue.pop("external_verification")
    _save(ledger_path, ledger)
    _record_reviews(study, [("final", ["answer-42"])])
    attempt = build_assessment(study)["attempts"][0]
    assert attempt["correction_burden"]["unverified_issue_ids"] == ["answer-42"]
    assert attempt["correction_burden"]["completeness_by_stage"]["final"] is False
    assert attempt["stages"][-1]["accepted"] is None


@pytest.mark.parametrize("complete", [True, False])
def test_unresolved_issue_blocks_final_acceptance_after_failed_repair(
    tmp_path: Path, complete: bool
) -> None:
    study = _demo(tmp_path)
    _leave_issue_unresolved(study)
    _record_reviews(study, [("handoff", ["answer-42"]), ("final", ["answer-42"])])
    if not complete:
        ledger_path = study.parent / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger.pop("completeness")
        _save(ledger_path, ledger)
        _rebind_ledger(study)
    attempt = build_assessment(study)["attempts"][0]
    burden = attempt["correction_burden"]
    assert burden["external_rounds"] == 2
    assert burden["verified_issue_count"] == 1
    assert burden["verified_correction_events"] == 0
    assert burden["outstanding_by_stage"]["final"] == ["answer-42"]
    assert attempt["stages"][-1]["selected_checks_pass"] is True
    assert attempt["stages"][-1]["accepted"] is False
    assert attempt["work_to_acceptance"] is None


@pytest.mark.parametrize("proof_state", ["missing", "not_independent", "false_completeness"])
def test_unverified_review_history_cannot_establish_acceptance(
    tmp_path: Path, proof_state: str
) -> None:
    study = _demo(tmp_path)
    _record_reviews(study, [("handoff", ["answer-42"]), ("final", [])])
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    if proof_state == "missing":
        ledger["review_rounds"][1].pop("external_verification")
    elif proof_state == "not_independent":
        receipt = study.parent / "evidence/review-2.json"
        proof = json.loads(receipt.read_text())
        proof["independent"] = False
        _save(receipt, proof)
        ledger["review_rounds"][1]["external_verification"]["sha256"] = _digest(receipt)
    else:
        receipt = study.parent / "evidence/ledger-completeness.json"
        proof = json.loads(receipt.read_text())
        proof["complete"] = False
        _save(receipt, proof)
        ledger["completeness"]["sha256"] = _digest(receipt)
    _save(ledger_path, ledger)
    _rebind_ledger(study)
    attempt = build_assessment(study)["attempts"][0]
    burden = attempt["correction_burden"]
    assert burden["status"] == "incomplete"
    assert burden["counts_are_lower_bounds"] is True
    assert burden["external_rounds"] == (2 if proof_state == "false_completeness" else 1)
    assert attempt["stages"][-1]["accepted"] is None


def test_passing_unreviewed_stage_is_not_accepted(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    _record_reviews(study, [("handoff", ["answer-42"])])
    attempt = build_assessment(study)["attempts"][0]
    assert attempt["correction_burden"]["status"] == "incomplete"
    assert attempt["stages"][-1]["selected_checks_pass"] is True
    assert attempt["stages"][-1]["accepted"] is None


@pytest.mark.parametrize("tamper", ["snapshot", "issue_ids", "origin_round", "gap"])
def test_review_receipts_reject_conflicting_or_unbound_history(tmp_path: Path, tamper: str) -> None:
    study = _demo(tmp_path)
    _record_reviews(study, [("handoff", ["answer-42"]), ("final", [])])
    ledger_path = study.parent / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    if tamper in {"snapshot", "issue_ids"}:
        receipt = study.parent / "evidence/review-2.json"
        proof = json.loads(receipt.read_text())
        if tamper == "snapshot":
            proof["stage_digests"]["final"] = ledger["stage_digests"]["handoff"]
        else:
            proof["issue_ids"] = ledger["review_rounds"][1]["issue_ids"] = ["answer-42"]
        _save(receipt, proof)
        ledger["review_rounds"][1]["external_verification"]["sha256"] = _digest(receipt)
    elif tamper == "origin_round":
        ledger["review_rounds"] = []
    else:
        ledger["review_rounds"][1]["round"] = 3
    _save(ledger_path, ledger)
    _rebind_ledger(study)
    with pytest.raises(ValueError, match="review"):
        build_assessment(study)


def test_unknown_earlier_stage_prevents_work_to_acceptance(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    manifest = yaml.safe_load(study.read_text())
    for stage in manifest["records"][0]["stages"]:
        stage["work"] = {"elapsed_seconds": 1.0}
    del manifest["records"][0]["stages"][0]["correctness"]
    _save(study, manifest)
    attempt = build_assessment(study)["attempts"][0]
    assert attempt["stages"][-1]["accepted"] is True
    assert attempt["work_to_acceptance"] is None


def test_required_evidence_and_scope_are_separate_gates(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    standards_path = study.parent / "standards.yaml"
    standards = yaml.safe_load(standards_path.read_text())
    standards["required_metrics"] = ["coverage"]
    _save(standards_path, standards)
    final = build_assessment(study)["attempts"][0]["stages"][-1]
    assert final["correctness"]["status"] == "pass"
    assert final["standards"]["status"] == "unknown"
    assert final["accepted"] is None
    standards["allowed_paths"] = ["other/**"]
    _save(standards_path, standards)
    final = build_assessment(study)["attempts"][0]["stages"][-1]
    assert final["standards"]["status"] == "fail"
    assert final["accepted"] is False


@pytest.mark.parametrize("checks", [[], ["behavior", "behavior"]])
def test_rejects_empty_or_duplicate_required_checks(tmp_path: Path, checks: list[str]) -> None:
    study = _demo(tmp_path)
    standards = yaml.safe_load((study.parent / "standards.yaml").read_text())
    standards["required_correctness_checks"] = checks
    _save(study.parent / "standards.yaml", standards)
    with pytest.raises(ValueError):
        build_assessment(study)


def test_baseline_evidence_produces_bound_before_after_delta(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    base = study.parent
    config = base / "coverage-config.json"
    _save(config, {"scope": ["src/**"]})
    manifest = yaml.safe_load(study.read_text())
    record = manifest["records"][0]
    for name, covered in (("before", []), ("final", ["src/answer.py:branch-1"])):
        raw = base / f"{name}-coverage.json"
        _save(
            raw,
            {
                "branches": {"total": ["src/answer.py:branch-1"], "covered": covered},
                "functions": {"total": ["src/answer.py:answer"], "covered": []},
            },
        )
        receipt = base / f"{name}-coverage-receipt.json"
        _save(
            receipt,
            bind_evidence(
                EvidenceBinding(
                    base / f"snapshots/{name}", raw, config, "coverage", "1", ("src/**",)
                )
            ),
        )
        ref = {"coverage": {"path": receipt.name, "sha256": _digest(receipt)}}
        if name == "before":
            record["before_evidence"] = ref
        else:
            record["stages"][-1]["evidence"] = ref
    _save(study, manifest)
    report = build_assessment(study)
    final = report["attempts"][0]["stages"][-1]
    assert final["code_size"]["status"] == "available"
    assert final["code_size"]["scope"] == ["src/**"]
    assert final["code_size"]["after_paths"] == ["src/answer.py"]
    assert final["metric_comparison"]["coverage"]["status"] == "available"
    assert final["metric_comparison"]["coverage"]["deltas"]["branch_percent"] == 100
    _, markdown = write_assessment(report, tmp_path / "report")
    assert "branch % 100 (Δ +100)" in markdown.read_text()
    assert "under src/**" in markdown.read_text()


@pytest.mark.parametrize("scope_fault", [None, "conflict", "missing-before"])
def test_scoped_source_size_preserves_raw_generated_counts_and_requires_agreement(
    tmp_path: Path, scope_fault: str | None
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    for root, lines in ((before, 2), (after, 4)):
        (root / "src").mkdir(parents=True)
        (root / "dist").mkdir()
        (root / "src/index.ts").write_text("export const x = 1;\n" * lines)
        (root / "dist/index.mjs").write_text("export const generated = 1;\n" * (lines * 10))
    change = compare_snapshots(before, after)
    stage = Stage(
        "handoff",
        Snapshot(after, str(inspect_snapshot(after)["digest"])),
        None,
        {"fallow": "unused", "coverage": "unused"},
        None,
    )
    record = Attempt(
        "a",
        "task",
        1,
        "baseline",
        "model",
        Snapshot(before, str(inspect_snapshot(before)["digest"])),
        (stage,),
        None,
        tmp_path,
        {},
        {"fallow": "unused", "coverage": "unused"},
    )
    before_metrics: dict[str, object] = {
        kind: {"provenance": {"scope": ["src/**"]}} for kind in ("fallow", "coverage")
    }
    after_metrics = json.loads(json.dumps(before_metrics))
    if scope_fault == "conflict":
        after_metrics["coverage"]["provenance"]["scope"] = ["dist/**"]
    elif scope_fault == "missing-before":
        del before_metrics["coverage"]
    size = _code_size(record, stage, before_metrics, after_metrics, change)
    assert change["source_lines_added"] == 22
    if scope_fault:
        assert size["status"] == "unavailable"
        assert "source_lines_added" not in size
    else:
        assert size["status"] == "available"
        assert size["source_lines_added"] == 2
        assert size["after_source_lines"] == 4
        assert size["after_paths"] == ["src/index.ts"]


def test_task_quality_reaches_canonical_json_markdown_and_csv(tmp_path: Path) -> None:
    source = "export function existing(x) {\n  if (x) { return 1; }\n  return 2;\n}\nexport const old = 1;\n"
    config = tmp_path / "config.json"
    _save(config, {"entry": ["src/index.ts"]})
    snapshots, receipts = {}, {}
    for name, text, cognitive, symbol in (
        ("before", source, 20, "old"),
        ("handoff", source.replace("1", "3").replace("old", "fresh"), 10, "fresh"),
    ):
        root = tmp_path / name
        (root / "src").mkdir(parents=True)
        (root / "src/index.ts").write_text(text)
        raw = tmp_path / f"{name}-raw.json"
        _save(
            raw,
            {
                "kind": "combined",
                "version": "3.18.0",
                "check": {
                    **{
                        key: []
                        for key in (
                            "unused_files",
                            "unused_exports",
                            "unused_types",
                            "unused_dependencies",
                            "unused_dev_dependencies",
                            "unused_optional_dependencies",
                            "unused_enum_members",
                            "unused_class_members",
                            "unused_catalog_entries",
                            "unused_dependency_overrides",
                            "unresolved_imports",
                        )
                    },
                    "unused_exports": [{"path": "src/index.ts", "export_name": symbol}],
                    "entry_points": {"total": 1},
                },
                "dupes": {},
                "health": {
                    "summary": {"files_analyzed": 1, "functions_analyzed": 1},
                    "findings": [
                        {
                            "path": "src/index.ts",
                            "name": "existing",
                            "line": 1,
                            "col": 0,
                            "line_count": 4,
                            "cognitive": cognitive,
                            "cyclomatic": 2,
                        }
                    ],
                },
                "discovery": {"files": ["src/index.ts"], "file_count": 1},
            },
        )
        raw_report = json.loads(raw.read_text())
        syntax_proof(raw_report, root, config, ["src/index.ts"])
        _save(raw, raw_report)
        receipt = tmp_path / f"{name}-receipt.json"
        _save(
            receipt,
            bind_evidence(EvidenceBinding(root, raw, config, "fallow", "3.18.0", ("src/**",))),
        )
        snapshots[name] = {"path": name, "sha256": inspect_snapshot(root)["digest"]}
        receipts[name] = {"fallow": {"path": receipt.name, "sha256": _digest(receipt)}}
    study = tmp_path / "study.json"
    _save(
        study,
        {
            "schema_version": 1,
            "name": "raw task metrics",
            "records": [
                {
                    "id": "quality",
                    "task_id": "quality",
                    "arm": "baseline",
                    "model": "fixture",
                    "before": snapshots["before"],
                    "before_evidence": receipts["before"],
                    "stages": [
                        {
                            "id": "handoff",
                            "snapshot": snapshots["handoff"],
                            "evidence": receipts["handoff"],
                        }
                    ],
                }
            ],
        },
    )
    report = build_assessment(study)
    comparison = report["attempts"][0]["stages"][0]["metric_comparison"]["fallow"]
    assert comparison["changed_functions"]["before"]["max_cognitive_reported"] == 20
    assert comparison["changed_functions"]["after"]["max_cognitive_reported"] == 10
    assert (
        comparison["dead_findings"]["introduced_count"]
        == comparison["dead_findings"]["resolved_count"]
        == 1
    )
    json_path, markdown = write_assessment(report, tmp_path / "output")
    assert json.loads(json_path.read_text()) == report
    assert "1/20/2/4 → 1/10/2/4" in markdown.read_text()
    assert "dead introduced/resolved 1/1" in markdown.read_text()
    assert "not matched functions" in markdown.read_text()
    assert (
        "After changed-function cognitive maximum,10.0"
        in (tmp_path / "output/assessment-charts.csv").read_text()
    )


def test_manifest_runtime_metadata_preserves_requested_and_observed_effort(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    manifest = yaml.safe_load(study.read_text())
    runtime = {
        "harness": "declared-harness",
        "harness_version": "exact-version",
        "requested_reasoning_effort": "high",
        "reasoning_effort": "medium",
    }
    manifest["records"][0]["runtime"] = runtime
    _save(study, manifest)
    report = build_assessment(study)
    provenance = report["attempts"][0]["provenance"]
    assert all(provenance[key] == value for key, value in runtime.items())
    assert provenance["input"] == "manifest"
    assert provenance["manifest"] == str(study)
    assert [stage["accepted"] for stage in report["attempts"][0]["stages"]] == [False, True]


@pytest.mark.parametrize(
    "field", ["harness", "harness_version", "requested_reasoning_effort", "reasoning_effort"]
)
def test_manifest_runtime_fields_do_not_coerce_numbers_to_strings(
    tmp_path: Path, field: str
) -> None:
    study = _demo(tmp_path)
    manifest = yaml.safe_load(study.read_text())
    manifest["records"][0]["runtime"] = {field: 1}
    _save(study, manifest)
    with pytest.raises(ValueError, match="valid string"):
        build_assessment(study)
