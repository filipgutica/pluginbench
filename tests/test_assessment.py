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

DEMO = Path(__file__).resolve().parents[1] / "tests/fixtures/handoff-assessment"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _demo(tmp_path: Path) -> Path:
    shutil.copytree(DEMO, tmp_path / "study")
    return tmp_path / "study/study.yaml"


def _save(path: Path, data: object) -> None:
    path.write_text(json.dumps(data))


def test_portable_demo_and_immutable_output(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    report = build_assessment(study)
    attempt = report["attempts"][0]
    assert attempt["stages"][0]["correctness"]["status"] == "fail"
    json_path = write_assessment(report, tmp_path / "output")
    assert json.loads(json_path.read_text()) == report
    with pytest.raises(ValueError, match="outside every input snapshot"):
        write_assessment(report, study.parent / "snapshots/handoff/report")
    assert not (study.parent / "snapshots/handoff/report").exists()


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
    assert attempt["stages"][0]["correctness"]["status"] == "unknown"
    assert report["snapshot_roots"] == []
    json_path = write_assessment(report, tmp_path / "output")
    assert len(json.loads(json_path.read_text())["attempts"]) == 1


@pytest.mark.parametrize(
    "target", ["snapshots/handoff/src/answer.py", "evidence/handoff-correctness.json"]
)
def test_rejects_tampered_source_and_receipts(tmp_path: Path, target: str) -> None:
    study = _demo(tmp_path)
    path = study.parent / target
    path.write_text(path.read_text() + "\nmodified")
    with pytest.raises(ValueError, match="digest mismatch"):
        build_assessment(study)


def test_reports_actual_filtering_without_changing_snapshot_binding(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    for name in ("before", "handoff"):
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
    receipt_path = study.parent / "evidence/handoff-correctness.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["snapshot_digest"] = "sha256:" + "a" * 64
    _save(receipt_path, receipt)
    manifest = yaml.safe_load(study.read_text())
    manifest["records"][0]["stages"][0]["correctness"]["sha256"] = _digest(receipt_path)
    _save(study, manifest)
    with pytest.raises(ValueError, match="bound to another snapshot"):
        build_assessment(study)


def test_repeats_are_independent_and_missing_checks_do_not_pass(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    manifest = yaml.safe_load(study.read_text())
    first = manifest["records"][0]
    second = json.loads(json.dumps(first))
    second.update(id="repeat", attempt=2)
    del second["stages"][0]["correctness"]
    manifest["records"].append(second)
    _save(study, manifest)
    attempts = build_assessment(study)["attempts"]
    assert len(attempts) == 2
    assert attempts[1]["stages"][-1]["correctness"]["status"] == "unknown"


def test_baseline_evidence_produces_bound_before_after_delta(tmp_path: Path) -> None:
    study = _demo(tmp_path)
    base = study.parent
    config = base / "coverage-config.json"
    _save(config, {"scope": ["src/**"]})
    manifest = yaml.safe_load(study.read_text())
    record = manifest["records"][0]
    for name, covered in (("before", []), ("handoff", ["src/answer.py:branch-1"])):
        raw = base / f"{name}-coverage.json"
        _save(
            raw,
            {
                "branches": {"total": ["src/answer.py:branch-1"], "covered": covered},
                "functions": {"total": ["src/answer.py:answer"], "covered": []},
                "files_measured": ["src/answer.py"],
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
    handoff = report["attempts"][0]["stages"][0]
    assert handoff["code_size"]["status"] == "available"
    assert handoff["code_size"]["scope"] == ["src/**"]
    assert handoff["code_size"]["after_paths"] == ["src/answer.py"]
    assert handoff["metric_comparison"]["coverage"]["status"] == "available"
    assert handoff["metric_comparison"]["coverage"]["deltas"]["branch_percent"] == 100
    path = write_assessment(report, tmp_path / "report")
    assert json.loads(path.read_text()) == report


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


def test_task_quality_reaches_canonical_json(tmp_path: Path) -> None:
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
    json_path = write_assessment(report, tmp_path / "output")
    assert json.loads(json_path.read_text()) == report


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
