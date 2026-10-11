from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from fallow_fixture import syntax_proof as _syntax_proof

from pluginbench import assessment_metrics
from pluginbench.assessment_metrics import (
    EvidenceBinding,
    bind_evidence,
    compare_evidence,
    compare_snapshots,
    evaluate_evidence,
    inspect_snapshot,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _receipt(tmp_path: Path, kind: str, report: dict[str, object]) -> tuple[Path, Path]:
    snapshot = tmp_path / "snapshot"
    _write(snapshot / "src/index.ts", "export const value = 1;\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"entry": ["src/index.ts"]}))
    raw = tmp_path / "report.json"
    raw.write_text(json.dumps(report))
    envelope = bind_evidence(EvidenceBinding(snapshot, raw, config, kind, "3.18.0", ("src/**",)))
    receipt = tmp_path / "evidence.json"
    receipt.write_text(json.dumps(envelope))
    return snapshot, receipt


def _metric(result: dict[str, object], kind: str) -> dict[str, object]:
    value = result[kind]
    assert isinstance(value, dict)
    return value


def _fallow() -> dict[str, object]:
    return {
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
                    "circular_dependencies",
                    "boundary_violations",
                    "boundary_coverage_violations",
                    "boundary_call_violations",
                )
            },
            "unused_exports": [{"path": "src/index.ts", "name": "old"}],
            "boundary_violations": [{"path": "src/index.ts"}],
        },
        "dupes": {"clone_groups": [{"instances": [1, 2]}]},
        "health": {
            "findings": [
                {
                    "path": "src/index.ts",
                    "name": "x",
                    "line": 1,
                    "cognitive": 17,
                    "cyclomatic": 3,
                    "line_count": 8,
                }
            ]
        },
    }


def _rebind_fallow(
    directory: Path, snapshot: Path, report: dict[str, object], scope: tuple[str, ...] = ("src/**",)
) -> Path:
    raw = directory / "report.json"
    raw.write_text(json.dumps(report))
    receipt = directory / "evidence.json"
    receipt.write_text(
        json.dumps(
            bind_evidence(
                EvidenceBinding(snapshot, raw, directory / "config.json", "fallow", "3.18.0", scope)
            )
        )
    )
    return receipt


def test_snapshot_hashes_all_included_files_and_records_exclusions(tmp_path: Path) -> None:
    _write(tmp_path / "src/index.ts", "one\ntwo\n")
    _write(tmp_path / "tests/index.test.ts", "test\n")
    _write(tmp_path / ".agents/guide.md", "ignored")
    first = inspect_snapshot(tmp_path)
    assert first["source_lines"] == 2
    assert first["test_lines"] == 1
    assert first["excluded_paths"] == [".agents"]
    _write(tmp_path / ".agents/guide.md", "changed ignored")
    assert inspect_snapshot(tmp_path)["digest"] == first["digest"]
    _write(tmp_path / "src/index.ts", "changed\n")
    assert inspect_snapshot(tmp_path)["digest"] != first["digest"]


def test_snapshot_rejects_links_instead_of_following(tmp_path: Path) -> None:
    (tmp_path / "outside").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        inspect_snapshot(tmp_path)


@pytest.mark.parametrize("suffix", [".mjs", ".cjs", ".mts", ".cts"])
def test_module_extensions_count_as_production_lines(tmp_path: Path, suffix: str) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    _write(after / f"src/module{suffix}", "export const value = 1;\n")
    assert inspect_snapshot(after)["source_lines"] == 1
    change = compare_snapshots(before, after)
    assert change["source_lines_added"] == 1
    assert change["test_lines_added"] == 0


def test_snapshot_diff_counts_source_tests_dependencies_and_binary(tmp_path: Path) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    _write(before / "src/a.ts", "const a = 1;\n")
    _write(after / "src/a.ts", "const a = 2;\nconst b = 3;\n")
    _write(after / "tests/a.test.ts", "test();\n")
    _write(after / "package.json", "{}\n")
    (after / "asset.bin").write_bytes(b"\x00\xff")
    fingerprints = inspect_snapshot(before), inspect_snapshot(after)
    result = compare_snapshots(before, after)
    assert result["files_changed"] == 4
    assert result["source_lines_added"] == 2
    assert result["source_lines_removed"] == 1
    assert result["test_lines_added"] == 1
    assert result["dependency_manifest_changes"] == ["package.json"]
    assert result["binary_patch"] is True
    assert (inspect_snapshot(before), inspect_snapshot(after)) == fingerprints


def test_snapshot_diff_empty_directories_and_unicode_paths(tmp_path: Path) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    assert compare_snapshots(before, after)["empty_patch"] is True
    _write(after / "src/with space é.ts", "one\n")
    assert compare_snapshots(before, after)["changed_paths"] == ["src/with space é.ts"]


def test_scoped_code_size_counts_inventory_source_without_generated_dist_or_analyzer_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    _write(before / "src/index.ts", "export const stable = 1;\nexport const value = 1;\n")
    _write(after / "src/index.ts", "export const stable = 1;\nexport const value = 2;\nextra();\n")
    _write(after / "src/ignored.ts", "ignoredByAnalyzer();\n" * 181)
    _write(after / ".fallowrc.json", '{"ignore": ["src/ignored.ts"]}\n')
    _write(after / "dist/index.mjs", "generated();\n" * 800)
    _write(after / "dist/index.cjs", "generated();\n" * 10)
    _write(after / "src/index.test.ts", "test();\n" * 5)
    change = compare_snapshots(before, after)
    raw = inspect_snapshot(after)
    assert raw["source_lines"] == 994
    assert raw["test_lines"] == 5
    assert change["source_lines_added"] == 993

    def no_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scoped measurement must reuse the supplied diff")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    result = assessment_metrics.scoped_code_size(
        before=before, after=after, scope=["./src/**", "src/**"], change=change
    )
    assert result["method"] == "scoped-source-lines-v1"
    assert result["scope"] == ["src/**"]
    assert result["before_paths"] == ["src/index.ts"]
    assert result["after_paths"] == ["src/ignored.ts", "src/index.ts"]
    assert result["before_digest"] == change["before_digest"]
    assert result["after_digest"] == change["after_digest"]
    assert result["before_source_lines"] == 2
    assert result["after_source_lines"] == 184
    assert result["source_lines_added"] == 183
    assert result["source_lines_removed"] == 1
    assert inspect_snapshot(after) == raw


def test_scoped_code_size_includes_deletions_and_ignores_line_shift_context(tmp_path: Path) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    _write(before / "src/removed.ts", "removed();\nremovedAgain();\n")
    _write(before / "src/shifted.ts", "unchanged();\n")
    _write(after / "src/shifted.ts", "inserted();\nunchanged();\n")
    change = compare_snapshots(before, after)
    result = assessment_metrics.scoped_code_size(
        before=before, after=after, scope=["src/**"], change=change
    )
    assert result["before_source_lines"] == 3
    assert result["after_source_lines"] == 2
    assert result["source_lines_added"] == 1
    assert result["source_lines_removed"] == 2


@pytest.mark.parametrize("side", ["before", "after"])
def test_scoped_code_size_rejects_inconsistent_bound_change(tmp_path: Path, side: str) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    _write(before / "src/index.ts", "old();\n")
    _write(after / "src/index.ts", "new();\n")
    change = compare_snapshots(before, after)
    change[f"{side}_digest"] = "sha256:wrong-snapshot"
    with pytest.raises(ValueError, match="source diff.*snapshot"):
        assessment_metrics.scoped_code_size(
            before=before, after=after, scope=["src/**"], change=change
        )


@pytest.mark.parametrize(
    "ranges",
    [
        None,
        {},
        {"src/index.ts": {"before": [[1, 1]], "after": [[1, 10]]}},
        {"src/index.ts": {"before": [[1, 1], [1, 1]], "after": [[1, 1]]}},
    ],
)
def test_scoped_code_size_refuses_missing_or_invalid_hunks(tmp_path: Path, ranges: object) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    _write(before / "src/index.ts", "old();\n")
    _write(after / "src/index.ts", "new();\n")
    change = compare_snapshots(before, after)
    change["changed_line_ranges"] = ranges
    with pytest.raises(ValueError, match="changed-line|omits changed"):
        assessment_metrics.scoped_code_size(
            before=before, after=after, scope=["src/**"], change=change
        )


@pytest.mark.parametrize("scope", [[], [""], ["../src/**"], ["/src/**"]])
def test_scoped_code_size_refuses_absent_or_escaping_scope(
    tmp_path: Path, scope: list[str]
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    with pytest.raises(ValueError, match="code-size scope"):
        assessment_metrics.scoped_code_size(
            before=before, after=after, scope=scope, change=compare_snapshots(before, after)
        )


def test_scoped_code_size_refuses_non_text_source_instead_of_counting_zero(tmp_path: Path) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    _write(after / "src/index.ts", "temporary\n")
    (after / "src/index.ts").write_bytes(b"\xff")
    change = compare_snapshots(before, after)
    assert inspect_snapshot(after)["source_lines"] == 0
    with pytest.raises(ValueError, match="UTF-8"):
        assessment_metrics.scoped_code_size(
            before=before, after=after, scope=["src/**"], change=change
        )


def test_scoped_code_size_rejects_mutation_after_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    _write(before / "src/index.ts", "old();\n")
    changed = after / "src/index.ts"
    _write(changed, "new();\n")
    change = compare_snapshots(before, after)
    read_bytes = Path.read_bytes
    reads = 0

    def mutate_after_read(path: Path) -> bytes:
        nonlocal reads
        data = read_bytes(path)
        if path == changed:
            reads += 1
            if reads == 2:
                path.write_text("mutated();\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)
    with pytest.raises(ValueError, match="snapshot changed during"):
        assessment_metrics.scoped_code_size(
            before=before, after=after, scope=["src/**"], change=change
        )


def test_absent_evidence_is_unknown_not_zero(tmp_path: Path) -> None:
    _write(tmp_path / "index.ts", "x\n")
    result = _metric(
        evaluate_evidence(tmp_path, None, {"fallow": {"max_dead_code": 0}}, tmp_path), "fallow"
    )
    assert result["status"] == "unavailable"
    assert result["metrics"] is None
    assert result["unknown_standards"] == ["max_dead_code"]


@pytest.mark.parametrize("threshold", [-1, True, float("nan"), 1.2])
def test_invalid_threshold_rejected_even_without_evidence(
    tmp_path: Path, threshold: object
) -> None:
    with pytest.raises(ValueError):
        evaluate_evidence(tmp_path, None, {"fallow": {"max_dead_code": threshold}}, tmp_path)


def test_fallow_observed_violations_not_artificial_collection_thresholds(tmp_path: Path) -> None:
    snapshot, receipt = _receipt(tmp_path, "fallow", _fallow())
    result = _metric(
        evaluate_evidence(
            snapshot,
            {"fallow": str(receipt)},
            {
                "fallow": {
                    "max_cognitive": 15,
                    "max_cyclomatic": 20,
                    "max_dead_code": 0,
                    "max_clone_groups": 0,
                    "max_boundary_violations": 0,
                }
            },
            tmp_path,
        ),
        "fallow",
    )
    assert result["violations"] == ["max_cognitive", "max_dead_code", "max_clone_groups"]
    assert result["unknown_standards"] == ["max_cyclomatic", "max_boundary_violations"]
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["boundary_violations"] is None
    assert metrics["complexity_distribution"] == {
        "cognitive": [17],
        "cyclomatic": [3],
        "line_count": [8],
    }


def test_unresolved_graph_prevents_clean_dead_code_pass(tmp_path: Path) -> None:
    report = _fallow()
    check = report["check"]
    assert isinstance(check, dict)
    check["unused_exports"] = []
    check["unresolved_imports"] = [{"path": "src/index.ts"}]
    snapshot, receipt = _receipt(tmp_path, "fallow", report)
    result = _metric(
        evaluate_evidence(
            snapshot, {"fallow": str(receipt)}, {"fallow": {"max_dead_code": 0}}, tmp_path
        ),
        "fallow",
    )
    assert result["violations"] == []
    assert result["unknown_standards"] == ["max_dead_code"]


@pytest.mark.parametrize("tamper", ["source", "report", "config", "version"])
def test_tampered_evidence_rejected(tmp_path: Path, tamper: str) -> None:
    snapshot, receipt = _receipt(tmp_path, "fallow", _fallow())
    if tamper == "source":
        _write(snapshot / "src/index.ts", "modified\n")
    elif tamper == "version":
        envelope = json.loads(receipt.read_text())
        envelope["tool_version"] = "different"
        receipt.write_text(json.dumps(envelope))
    else:
        (tmp_path / f"{tamper}.json").write_text("{}")
    with pytest.raises(ValueError, match="bind|digest|version"):
        evaluate_evidence(snapshot, {"fallow": str(receipt)}, {}, tmp_path)


def test_coverage_fixed_denominator_and_zero_unique_proxy(tmp_path: Path) -> None:
    report: dict[str, object] = {
        "branches": {
            "total": ["src/index.ts:b1", "src/index.ts:b2", "src/index.ts:b3"],
            "covered": ["src/index.ts:b1", "src/index.ts:b2"],
        },
        "functions": {"total": ["src/index.ts:f1"], "covered": ["src/index.ts:f1"]},
        "per_test": {
            "one": ["src/index.ts:b1"],
            "duplicate": ["src/index.ts:b1"],
            "unique": ["src/index.ts:b2"],
        },
    }
    snapshot, receipt = _receipt(tmp_path, "coverage", report)
    result = _metric(
        evaluate_evidence(
            snapshot, {"coverage": str(receipt)}, {"coverage": {"min_branch_percent": 80}}, tmp_path
        ),
        "coverage",
    )
    assert result["violations"] == ["min_branch_percent"]
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["branch_percent"] == pytest.approx(200 / 3)
    assert metrics["zero_unique_branch_contribution_tests"] == ["duplicate", "one"]
    assert metrics["per_test_count"] == 3
    assert "not proof" in str(metrics["per_test_detection"])


@pytest.mark.parametrize("bad_coordinate", ["src/index.ts:outside", "other.ts:b1"])
def test_coverage_rejects_denominator_or_source_scope_escape(
    tmp_path: Path, bad_coordinate: str
) -> None:
    report: dict[str, object] = {
        "branches": {"total": ["src/index.ts:b1"], "covered": [bad_coordinate]},
        "functions": {"total": [], "covered": []},
    }
    if bad_coordinate.startswith("other"):
        report["branches"] = {"total": [bad_coordinate], "covered": []}
    snapshot, receipt = _receipt(tmp_path, "coverage", report)
    with pytest.raises(ValueError, match="denominator|scope"):
        evaluate_evidence(snapshot, {"coverage": str(receipt)}, {}, tmp_path)


def test_empty_coverage_denominator_is_incomplete(tmp_path: Path) -> None:
    report: dict[str, object] = {
        key: {"total": [], "covered": []} for key in ("branches", "functions")
    }
    snapshot, receipt = _receipt(tmp_path, "coverage", report)
    result = _metric(
        evaluate_evidence(
            snapshot, {"coverage": str(receipt)}, {"coverage": {"min_branch_percent": 80}}, tmp_path
        ),
        "coverage",
    )
    assert result["unknown_standards"] == ["min_branch_percent"]
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["per_test_count"] is None


def test_mutation_counts_invalid_and_timeout_with_fixed_mutant_set(tmp_path: Path) -> None:
    report: dict[str, object] = {
        "mutants": {
            key: {"path": "src/index.ts", "status": status}
            for key, status in {
                "m1": "killed",
                "m2": "timeout",
                "m3": "survived",
                "m4": "compile_error",
            }.items()
        },
        "per_test": {"one": ["m1"], "duplicate": ["m1"]},
    }
    snapshot, receipt = _receipt(tmp_path, "mutation", report)
    result = _metric(
        evaluate_evidence(
            snapshot, {"mutation": str(receipt)}, {"mutation": {"min_score_percent": 70}}, tmp_path
        ),
        "mutation",
    )
    assert result["violations"] == ["min_score_percent"]
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["score_percent"] == pytest.approx(200 / 3)
    assert metrics["invalid"] == 1
    assert metrics["per_test_count"] == 2
    assert metrics["zero_unique_mutant_kill_tests"] == ["duplicate", "one"]


def test_binding_rejects_unfrozen_extends(tmp_path: Path) -> None:
    snapshot, _ = _receipt(tmp_path, "fallow", _fallow())
    (tmp_path / "config.json").write_text('{"extends":["changing.json"]}')
    with pytest.raises(ValueError, match="resolved"):
        bind_evidence(
            EvidenceBinding(
                snapshot,
                tmp_path / "report.json",
                tmp_path / "config.json",
                "fallow",
                "3.18.0",
                ("src/**",),
            )
        )


def test_receipt_contains_content_hashes_not_filename_hashes(tmp_path: Path) -> None:
    _, receipt = _receipt(tmp_path, "fallow", _fallow())
    envelope = json.loads(receipt.read_text())
    assert (
        envelope["report_digest"]
        == "sha256:" + hashlib.sha256((tmp_path / "report.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("unmeasured_file", [None, "native.C", "answer.PY"])
def test_fallow_completeness_covers_every_scoped_source_language(
    tmp_path: Path, unmeasured_file: str | None
) -> None:
    report = _fallow()
    health = report["health"]
    assert isinstance(health, dict)
    health["summary"] = {"files_analyzed": 1, "files_scored": 1, "functions_analyzed": 1}
    report["dupes"] = {"clone_groups": [], "stats": {"total_files": 1, "clone_groups": 0}}
    snapshot, receipt = _receipt(tmp_path, "fallow", report)
    _syntax_proof(report, snapshot, tmp_path / "config.json", ["src/index.ts"])
    receipt = _rebind_fallow(tmp_path, snapshot, report)
    if unmeasured_file is not None:
        _write(snapshot / "src" / unmeasured_file, "unmeasured source\n")
        envelope = json.loads(receipt.read_text())
        envelope["snapshot_digest"] = inspect_snapshot(snapshot)["digest"]
        receipt.write_text(json.dumps(envelope))
    result = _metric(
        evaluate_evidence(
            snapshot,
            {"fallow": str(receipt)},
            {"fallow": {"max_cognitive": 20, "max_clone_groups": 0}},
            tmp_path,
        ),
        "fallow",
    )
    assert result["violations"] == []
    if unmeasured_file is None:
        assert result["unknown_standards"] == []
        assert result["status"] == "available"
    else:
        unknown = result["unknown_standards"]
        assert isinstance(unknown, list)
        assert set(unknown) == {"max_cognitive", "max_clone_groups"}
        assert result["status"] == "incomplete"


def test_duplicate_function_identity_does_not_claim_complete_complexity(tmp_path: Path) -> None:
    report = _fallow()
    health = report["health"]
    assert isinstance(health, dict)
    findings = health["findings"]
    assert isinstance(findings, list)
    findings.append(dict(findings[0]))
    health["summary"] = {"files_analyzed": 1, "files_scored": 1, "functions_analyzed": 2}
    snapshot, receipt = _receipt(tmp_path, "fallow", report)
    result = _metric(
        evaluate_evidence(
            snapshot, {"fallow": str(receipt)}, {"fallow": {"max_cognitive": 20}}, tmp_path
        ),
        "fallow",
    )
    assert result["unknown_standards"] == ["max_cognitive"]


@pytest.mark.parametrize("kind", ["coverage", "mutation", "fallow"])
def test_metric_delta_rejects_changed_collection_provenance(tmp_path: Path, kind: str) -> None:
    left: dict[str, object] = {
        kind: {
            "provenance": {
                "tool": "tool",
                "tool_version": "1",
                "config_digest": "fixed",
                "scope": ["src/**"],
            },
            "metrics": {"count": 1},
        }
    }
    right: dict[str, object] = {
        kind: {
            "provenance": {
                "tool": "tool",
                "tool_version": "2",
                "config_digest": "fixed",
                "scope": ["src/**"],
            },
            "metrics": {"count": 2},
        }
    }
    result = _metric(compare_evidence(left, right), kind)
    assert result["status"] == "incomplete"
    assert result["deltas"] is None


@pytest.mark.parametrize(
    "kind,key", [("coverage", "branch_denominator_digest"), ("mutation", "mutant_set_digest")]
)
def test_metric_delta_rejects_changed_denominator(kind: str, key: str) -> None:
    provenance = {
        "tool": "tool",
        "tool_version": "1",
        "config_digest": "fixed",
        "scope": ["src/**"],
    }
    left: dict[str, object] = {
        kind: {"provenance": provenance, "metrics": {key: "one", "count": 1}}
    }
    right: dict[str, object] = {
        kind: {"provenance": provenance, "metrics": {key: "two", "count": 2}}
    }
    result = _metric(compare_evidence(left, right), kind)
    assert result["status"] == "incomplete"
    assert result["deltas"] is None


def test_compatible_metrics_report_net_delta_not_introduced_finding_identity() -> None:
    provenance = {
        "tool": "fallow",
        "tool_version": "1",
        "config_digest": "fixed",
        "scope": ["src/**"],
    }
    left: dict[str, object] = {"fallow": {"provenance": provenance, "metrics": {"dead_code": 2}}}
    right: dict[str, object] = {"fallow": {"provenance": provenance, "metrics": {"dead_code": 1}}}
    result = _metric(compare_evidence(left, right), "fallow")
    assert result["deltas"] == {"dead_code": -1.0}
    assert "not identity" in str(result["interpretation"])
    assert _metric(compare_evidence({}, right), "fallow")["status"] == "unavailable"


def test_boundary_declaration_without_configured_rules_stays_incomplete(tmp_path: Path) -> None:
    snapshot, receipt = _receipt(tmp_path, "fallow", _fallow())
    envelope = json.loads(receipt.read_text())
    envelope["boundary_rules_declared"] = True
    receipt.write_text(json.dumps(envelope))
    result = _metric(
        evaluate_evidence(
            snapshot, {"fallow": str(receipt)}, {"fallow": {"max_boundary_violations": 0}}, tmp_path
        ),
        "fallow",
    )
    assert result["unknown_standards"] == ["max_boundary_violations"]
    assert result["violations"] == []


@pytest.mark.parametrize("change", ["invalid_denominator", "source_identity", "genuine_detection"])
def test_mutation_score_comparison_requires_stable_eligible_mutants_and_source(
    tmp_path: Path, change: str
) -> None:
    before_mutants = {
        "m1": {"path": "src/index.ts", "status": "killed"},
        "m2": {"path": "src/index.ts", "status": "survived"},
        "m3": {"path": "src/index.ts", "status": "survived"},
    }
    after_mutants = {key: dict(row) for key, row in before_mutants.items()}
    if change == "invalid_denominator":
        after_mutants["m2"]["status"] = "compile_error"
        after_mutants["m3"]["status"] = "runtime_error"
    elif change == "source_identity":
        after_mutants["m2"]["path"] = "src/other.ts"
    else:
        after_mutants["m2"]["status"] = "killed"

    measurements: list[dict[str, object]] = []
    for name, mutants in (("before", before_mutants), ("after", after_mutants)):
        directory = tmp_path / name
        directory.mkdir()
        snapshot, receipt = _receipt(directory, "mutation", {"mutants": mutants})
        _write(snapshot / "src/other.ts", "export const other = 2;\n")
        receipt.write_text(
            json.dumps(
                bind_evidence(
                    EvidenceBinding(
                        snapshot,
                        directory / "report.json",
                        directory / "config.json",
                        "mutation",
                        "3.18.0",
                        ("src/**",),
                    )
                )
            )
        )
        measurements.append(evaluate_evidence(snapshot, {"mutation": str(receipt)}, {}, directory))

    result = _metric(compare_evidence(measurements[0], measurements[1]), "mutation")
    if change == "genuine_detection":
        assert result["status"] == "available"
        deltas = result["deltas"]
        assert isinstance(deltas, dict)
        assert deltas["score_percent"] == pytest.approx(100 / 3)
    else:
        assert result["status"] == "incomplete"
        assert result["deltas"] is None
    if change == "invalid_denominator":
        before = _metric(measurements[0], "mutation")["metrics"]
        after = _metric(measurements[1], "mutation")["metrics"]
        assert isinstance(before, dict) and isinstance(after, dict)
        assert before["score_percent"] == pytest.approx(100 / 3)
        assert after["score_percent"] == 100
        assert before["detected"] == after["detected"] == 1
        assert after["invalid"] == 2


@pytest.mark.parametrize("population", ["unsupported_production", "unsupported_test", "empty"])
def test_fallow_clean_counts_cannot_pass_unsupported_or_empty_analysis_scope(
    tmp_path: Path, population: str
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    if population == "unsupported_production":
        _write(snapshot / "src/answer.py", "answer = 42\n")
    elif population == "unsupported_test":
        _write(snapshot / "test/test_answer.py", "assert True\n")
    report = _fallow()
    check = report["check"]
    assert isinstance(check, dict)
    check["unused_exports"] = []
    report["dupes"] = {"clone_groups": [], "stats": {"total_files": 0}}
    report["health"] = {
        "findings": [],
        "summary": {
            "functions_analyzed": 0,
            "files_analyzed": 0,
            "files_scored": 0,
        },
    }
    config = tmp_path / "config.json"
    config.write_text("{}")
    raw = tmp_path / "report.json"
    raw.write_text(json.dumps(report))
    receipt = tmp_path / "evidence.json"
    receipt.write_text(
        json.dumps(
            bind_evidence(
                EvidenceBinding(
                    snapshot,
                    raw,
                    config,
                    "fallow",
                    "3.18.0",
                    ("**",),
                )
            )
        )
    )
    result = _metric(
        evaluate_evidence(
            snapshot,
            {"fallow": str(receipt)},
            {
                "fallow": {
                    "max_dead_code": 0,
                    "max_clone_groups": 0,
                    "max_cycles": 0,
                    "max_cognitive": 15,
                }
            },
            tmp_path,
        ),
        "fallow",
    )
    assert result["status"] == "incomplete"
    assert result["violations"] == []
    assert result["unknown_standards"] == [
        "max_dead_code",
        "max_clone_groups",
        "max_cycles",
        "max_cognitive",
    ]
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["dead_code"] == metrics["clone_groups"] == metrics["cycles"] == 0


@pytest.mark.parametrize("discovered_files,expected", [(2, "incomplete"), (3, "available")])
def test_fallow_clone_scope_checks_its_own_file_population_including_tests(
    tmp_path: Path, discovered_files: int, expected: str
) -> None:
    snapshot, receipt = _receipt(tmp_path, "fallow", _fallow())
    _write(snapshot / "src/helper.ts", "export const helper = 1;\n")
    _write(snapshot / "test/index.test.ts", "test();\n")
    report = _fallow()
    report["dupes"] = {"clone_groups": [], "stats": {"total_files": 2, "clone_groups": 0}}
    # Two streams qualify, but all three inputs must be discovered and parsed.
    report["health"] = {
        "findings": [],
        "summary": {
            "functions_analyzed": 0,
            "files_analyzed": 3,
            "files_scored": 3,
        },
    }
    _syntax_proof(
        report,
        snapshot,
        tmp_path / "config.json",
        ["src/helper.ts", "src/index.ts", "test/index.test.ts"][:discovered_files],
    )
    raw = tmp_path / "report.json"
    raw.write_text(json.dumps(report))
    receipt.write_text(
        json.dumps(
            bind_evidence(
                EvidenceBinding(
                    snapshot,
                    raw,
                    tmp_path / "config.json",
                    "fallow",
                    "3.18.0",
                    ("src/**", "test/**"),
                )
            )
        )
    )
    result = _metric(
        evaluate_evidence(
            snapshot,
            {"fallow": str(receipt)},
            {
                "fallow": {
                    "max_clone_groups": 0,
                }
            },
            tmp_path,
        ),
        "fallow",
    )
    assert result["status"] == expected
    assert result["unknown_standards"] == (["max_clone_groups"] if discovered_files == 2 else [])
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["complexity_complete_for_declared_scope"] is (discovered_files == 3)
    assert metrics["clone_groups_complete_for_declared_scope"] is (discovered_files == 3)


def test_fallow_zero_graph_findings_do_not_invent_scanned_graph_population(tmp_path: Path) -> None:
    report = _fallow()
    check = report["check"]
    assert isinstance(check, dict)
    check["unused_exports"] = []
    check["entry_points"] = {"total": 1}
    snapshot, receipt = _receipt(tmp_path, "fallow", report)
    result = _metric(
        evaluate_evidence(
            snapshot,
            {"fallow": str(receipt)},
            {
                "fallow": {
                    "max_dead_code": 0,
                    "max_cycles": 0,
                }
            },
            tmp_path,
        ),
        "fallow",
    )
    assert result["unknown_standards"] == ["max_dead_code", "max_cycles"]
    assert result["violations"] == []


def _bound_fallow(
    directory: Path,
    sources: dict[str, str],
    functions: list[dict[str, object]],
    exports: list[dict[str, object]] | None = None,
    *,
    complete: bool = True,
) -> tuple[Path, dict[str, object]]:
    snapshot = directory / "snapshot"
    for path, source in sources.items():
        _write(snapshot / path, source)
    report = _fallow()
    report["health"] = {
        "findings": functions,
        "summary": {"functions_analyzed": len(functions), "files_analyzed": len(sources)},
    }
    check = report["check"]
    assert isinstance(check, dict)
    check["unused_exports"] = exports or []
    check["entry_points"] = {"total": 1}
    _write(directory / "config.json", '{"entry":["src/index.ts"]}')
    _syntax_proof(report, snapshot, directory / "config.json", sorted(sources))
    if not complete:
        report.pop("discovery")
    _write(directory / "report.json", json.dumps(report))
    _write(
        directory / "evidence.json",
        json.dumps(
            bind_evidence(
                EvidenceBinding(
                    snapshot,
                    directory / "report.json",
                    directory / "config.json",
                    "fallow",
                    "3.18.0",
                    ("src/**",),
                )
            )
        ),
    )
    return snapshot, evaluate_evidence(snapshot, {"fallow": "evidence.json"}, {}, directory)


def _function(
    name: str, line: int, size: int, cognitive: int, *, path: str = "src/index.ts"
) -> dict[str, object]:
    return {
        "path": path,
        "name": name,
        "line": line,
        "col": 0,
        "line_count": size,
        "cognitive": cognitive,
        "cyclomatic": cognitive + 1,
    }


def test_new_complex_function_is_visible_beside_unchanged_inherited_hotspot(tmp_path: Path) -> None:
    hotspot = "function inherited() {\n  return 1;\n}\n"
    added = "function added(x) {\n  if (x) { return 2; }\n  return 3;\n}\n"
    before, left = _bound_fallow(
        tmp_path / "before", {"src/index.ts": hotspot}, [_function("inherited", 1, 3, 100)]
    )
    after, right = _bound_fallow(
        tmp_path / "after",
        {"src/index.ts": hotspot + added},
        [_function("inherited", 1, 3, 100), _function("added", 4, 4, 20)],
    )
    comparison = _metric(
        compare_evidence(left, right, change=compare_snapshots(before, after)), "fallow"
    )
    assert _metric(comparison, "deltas")["max_cognitive_reported"] == 0
    changed = _metric(comparison, "changed_functions")
    before_population, after_population = _metric(changed, "before"), _metric(changed, "after")
    assert before_population["function_count_reported"] == 0
    assert before_population["max_cognitive_reported"] is None
    assert after_population["function_count_reported"] == 1
    assert after_population["max_cognitive_reported"] == 20
    assert _metric(after_population, "complexity_distribution")["cognitive"] == [20]
    functions = after_population["functions"]
    assert isinstance(functions, list) and isinstance(functions[0], dict)
    assert functions[0]["name"] == "added"
    assert (
        _metric(_metric(comparison, "new_file_functions"), "after")["function_count_reported"] == 0
    )


def test_earlier_insertion_only_shifts_function_and_dead_finding_coordinates(
    tmp_path: Path,
) -> None:
    source = "export function legacy() {\n  return 1;\n}\n"
    before, left = _bound_fallow(
        tmp_path / "before",
        {"src/index.ts": source},
        [_function("legacy", 1, 3, 20)],
        [{"path": "src/index.ts", "export_name": "legacy", "line": 1, "col": 16}],
    )
    after, right = _bound_fallow(
        tmp_path / "after",
        {"src/index.ts": "// added header\n" + source},
        [_function("legacy", 2, 3, 20)],
        [{"path": "src/index.ts", "export_name": "legacy", "line": 2, "col": 16}],
    )
    comparison = _metric(
        compare_evidence(left, right, change=compare_snapshots(before, after)), "fallow"
    )
    changed = _metric(comparison, "changed_functions")
    assert (
        _metric(changed, "before")["function_count_reported"]
        == _metric(changed, "after")["function_count_reported"]
        == 0
    )
    dead = _metric(comparison, "dead_findings")
    assert dead["introduced"] == dead["resolved"] == []


@pytest.mark.parametrize("change", ["modify", "delete_line"])
def test_modified_existing_function_has_raw_before_after_population(
    tmp_path: Path, change: str
) -> None:
    source = "function existing(x) {\n  if (x) { return 1; }\n  return 2;\n}\n"
    updated = (
        source.replace("1", "3")
        if change == "modify"
        else source.replace("  if (x) { return 1; }\n", "")
    )
    before, left = _bound_fallow(
        tmp_path / "before", {"src/index.ts": source}, [_function("existing", 1, 4, 3)]
    )
    after, right = _bound_fallow(
        tmp_path / "after",
        {"src/index.ts": updated},
        [_function("existing", 1, len(updated.splitlines()), 1)],
    )
    comparison = _metric(
        compare_evidence(left, right, change=compare_snapshots(before, after)), "fallow"
    )
    changed = _metric(comparison, "changed_functions")
    assert (
        _metric(changed, "before")["function_count_reported"]
        == _metric(changed, "after")["function_count_reported"]
        == 1
    )
    assert _metric(changed, "before")["max_cognitive_reported"] == 3
    assert _metric(changed, "after")["max_cognitive_reported"] == 1


def test_new_file_functions_are_separate_and_partial_health_stays_incomplete(
    tmp_path: Path,
) -> None:
    before, left = _bound_fallow(tmp_path / "before", {"src/index.ts": "const x = 1;\n"}, [])
    source = "function added() {\n  return 1;\n}\n"
    after, right = _bound_fallow(
        tmp_path / "after",
        {"src/index.ts": "const x = 1;\n", "src/new.ts": source, "src/unmeasured.py": "x = 1\n"},
        [_function("added", 1, 3, 20, path="src/new.ts")],
    )
    comparison = _metric(
        compare_evidence(left, right, change=compare_snapshots(before, after)), "fallow"
    )
    new = _metric(comparison, "new_file_functions")
    assert new["status"] == "incomplete"
    assert new["paths"] == ["src/new.ts", "src/unmeasured.py"]
    assert _metric(new, "after")["max_cognitive_reported"] == 20
    assert _metric(new, "after")["function_count_reported"] == 1
    assert _metric(new, "after")["complete_for_declared_scope"] is False


@pytest.mark.parametrize("complete", [True, False])
def test_dead_identity_comparison_exposes_offsetting_introduction_and_resolution(
    tmp_path: Path, complete: bool
) -> None:
    source = {"src/index.ts": "export const old = 1, fresh = 2;\n"}
    _, left = _bound_fallow(
        tmp_path / "before",
        source,
        [],
        [{"path": "src/index.ts", "export_name": "old"}],
        complete=complete,
    )
    _, right = _bound_fallow(
        tmp_path / "after", source, [], [{"path": "src/index.ts", "export_name": "fresh"}]
    )
    comparison = _metric(compare_evidence(left, right), "fallow")
    assert _metric(comparison, "deltas")["dead_code"] == 0
    dead = _metric(comparison, "dead_findings")
    if complete:
        assert dead["introduced_count"] == dead["resolved_count"] == 1
        assert dead["introduced"] == [
            {"category": "unused_exports", "path": "src/index.ts", "export_name": "fresh"}
        ]
        resolved = dead["resolved"]
        assert isinstance(resolved, list) and isinstance(resolved[0], dict)
        assert resolved[0]["export_name"] == "old"
    else:
        assert dead["status"] == "unavailable"
        assert dead["introduced_count"] is dead["resolved_count"] is None


def test_function_changes_reject_source_diff_from_other_snapshot(tmp_path: Path) -> None:
    before, left = _bound_fallow(tmp_path / "before", {"src/index.ts": "x();\n"}, [])
    after, right = _bound_fallow(tmp_path / "after", {"src/index.ts": "y();\n"}, [])
    change = compare_snapshots(before, after)
    change["before_digest"] = "wrong"
    with pytest.raises(ValueError, match="does not bind"):
        compare_evidence(left, right, change=change)


@pytest.mark.parametrize("identity", ["unsupported", "duplicate"])
def test_complete_graph_with_ambiguous_dead_identity_cannot_claim_introductions(
    tmp_path: Path, identity: str
) -> None:
    source = {"src/index.ts": "export const value = 1;\n"}
    finding: dict[str, object] = {"path": "src/index.ts", "export_name": "value"}
    exports: list[dict[str, object]] = (
        [{"path": "src/index.ts", "name": "value"}]
        if identity == "unsupported"
        else [finding, dict(finding)]
    )
    _, left = _bound_fallow(tmp_path / "before", source, [], exports)
    _, right = _bound_fallow(tmp_path / "after", source, [], [])
    comparison = _metric(compare_evidence(left, right), "fallow")
    dead = _metric(comparison, "dead_findings")
    assert dead["status"] == "unavailable"
    assert dead["introduced_count"] is None
    assert dead["resolved_count"] is None


@pytest.mark.parametrize(
    "before_lines,after_lines,introduced,resolved",
    [
        (["dot: '.',", "dot: '\\u00b7',"], ["", "dot: '.',", "dot: '\\u00b7',"], 0, 0),
        (["dot: '.',", "dot: '\\u00b7',"], ["dot: '.',"], 0, 1),
        (["dot: '.',"], ["dot: '.',", "dot: '\\u00b7',"], 1, 0),
        (["dot: '.',", "dot: '\\u00b7',"], ["dot: '.',", "dot: 'changed',"], 1, 1),
    ],
)
def test_colliding_dead_symbols_use_bound_context_across_shifts_and_population_changes(
    tmp_path: Path, before_lines: list[str], after_lines: list[str], introduced: int, resolved: int
) -> None:
    observed = []
    for name, lines in (("before", before_lines), ("after", after_lines)):
        source = "\n".join(lines) + "\n"
        findings: list[dict[str, object]] = []
        offset = 0
        for number, line in enumerate(lines, 1):
            if line:
                findings.append(
                    {
                        "path": "src/index.ts",
                        "export_name": "dot",
                        "line": number,
                        "col": 0,
                        "span_start": offset,
                    }
                )
            offset += len((line + "\n").encode("utf-8"))
        _, result = _bound_fallow(tmp_path / name, {"src/index.ts": source}, [], findings)
        observed.append(result)
    dead = _metric(_metric(compare_evidence(*observed), "fallow"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == introduced and dead["resolved_count"] == resolved


def test_identical_colliding_declaration_contexts_remain_unknown(tmp_path: Path) -> None:
    source = {"src/index.ts": "dot: '.',\ndot: '.',\n"}
    findings: list[dict[str, object]] = [
        {"path": "src/index.ts", "export_name": "dot", "line": number, "col": 0}
        for number in (1, 2)
    ]
    _, left = _bound_fallow(tmp_path / "before", source, [], findings)
    _, right = _bound_fallow(tmp_path / "after", source, [], findings)
    dead = _metric(_metric(compare_evidence(left, right), "fallow"), "dead_findings")
    assert dead["status"] == "unavailable" and dead["introduced_count"] is None


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "parse",
        "hash",
        "version",
        "config",
        "producer",
        "truncated",
        "utf8",
        "metadata",
        "count",
    ],
)
def test_clone_eligibility_is_not_input_population_and_requires_complete_receipts(
    tmp_path: Path, fault: str | None
) -> None:
    snapshot, _ = _receipt(tmp_path, "fallow", _fallow())
    _write(snapshot / "src/types.ts", "export interface Short { value: number }\n")
    report = _fallow()
    report["dupes"] = {"clone_groups": [], "stats": {"total_files": 1, "clone_groups": 0}}
    health = report["health"]
    assert isinstance(health, dict)
    health["summary"] = {"functions_analyzed": 1, "files_analyzed": 2}
    check = report["check"]
    assert isinstance(check, dict)
    check["entry_points"] = {"total": 1}
    _syntax_proof(report, snapshot, tmp_path / "config.json", ["src/index.ts", "src/types.ts"])
    proof = report["syntax_validation"]
    assert isinstance(proof, dict)
    rows = proof["files"]
    assert isinstance(rows, list)
    if fault == "missing":
        rows.pop()
    elif fault == "parse":
        rows[0]["status"], rows[0]["errors"] = "invalid", ["parse error"]
    elif fault == "hash":
        rows[0]["source_digest"] = "sha256:wrong"
    elif fault == "version":
        proof["version"] = "0.96.0"
    elif fault == "config":
        proof["config_digest"] = "sha256:wrong"
    elif fault in {"producer", "truncated"}:
        executions = proof["producer_execution"]
        assert isinstance(executions, list)
        if fault == "producer":
            executions[0]["exit_code"] = 2
        else:
            executions[0]["command"] += ["--top", "1"]
    elif fault == "utf8":
        (snapshot / "src/types.ts").write_bytes(b"\xff")
        import hashlib

        rows[1]["source_digest"] = "sha256:" + hashlib.sha256(b"\xff").hexdigest()
    elif fault == "metadata":
        del report["syntax_validation"]
    elif fault == "count":
        report["dupes"] = {"clone_groups": [], "stats": {"total_files": 3, "clone_groups": 0}}
    receipt = _rebind_fallow(tmp_path, snapshot, report)
    metrics = _metric(
        _metric(evaluate_evidence(snapshot, {"fallow": str(receipt)}, {}, tmp_path), "fallow"),
        "metrics",
    )
    assert metrics["clone_groups_complete_for_declared_scope"] is (fault is None)
    assert metrics["clone_input_file_count"] == 2
    expected_other_complete = fault in {None, "count"}
    assert metrics["complexity_complete_for_declared_scope"] is expected_other_complete
    assert metrics["dead_code_complete_for_declared_scope"] is expected_other_complete
    if fault is None:
        assert metrics["clone_eligible_stream_count"] == 1
        assert "eligibility count producer-reported" in str(metrics["clone_detection"])


def test_clone_identity_comparison_tracks_changed_groups_despite_net_zero_and_line_shifts(
    tmp_path: Path,
) -> None:
    results = []
    for name, fragment, prefix in [
        ("before", "const original = 1;", ""),
        ("shifted", "const original = 1;", "// shift\n"),
        ("replaced", "const replacement = 2;", ""),
    ]:
        directory = tmp_path / name
        snapshot, _ = _receipt(directory, "fallow", _fallow())
        for path in ("src/index.ts", "src/other.ts"):
            _write(snapshot / path, prefix + fragment + "\n")
        report = _fallow()
        report["dupes"] = {
            "clone_groups": [
                {
                    "fingerprint": "irrelevant-ordinal",
                    "instances": [
                        {"file": path, "fragment": fragment, "start_line": 1 + bool(prefix)}
                        for path in ("src/index.ts", "src/other.ts")
                    ],
                }
            ],
            "stats": {"total_files": 2, "clone_groups": 1},
        }
        _syntax_proof(report, snapshot, directory / "config.json", ["src/index.ts", "src/other.ts"])
        receipt = _rebind_fallow(directory, snapshot, report)
        results.append(evaluate_evidence(snapshot, {"fallow": str(receipt)}, {}, directory))
    shifted = _metric(_metric(compare_evidence(results[0], results[1]), "fallow"), "clone_findings")
    assert shifted["introduced_count"] == shifted["resolved_count"] == 0
    changed = _metric(compare_evidence(results[0], results[2]), "fallow")
    assert _metric(changed, "deltas")["clone_groups"] == 0
    assert _metric(changed, "clone_findings")["introduced_count"] == 1
    assert _metric(changed, "clone_findings")["resolved_count"] == 1


def test_static_comparison_rejects_changed_syntax_observer_identity() -> None:
    provenance = {
        "tool": "fallow",
        "tool_version": "3.18.0",
        "config_digest": "same",
        "scope": ["src/**"],
        "syntax_observer": {"dependency_hashes": {"parser": "sha256:original"}},
    }
    left: dict[str, object] = {"fallow": {"provenance": provenance, "metrics": {"clone_groups": 0}}}
    right = json.loads(json.dumps(left))
    right["fallow"]["provenance"]["syntax_observer"]["dependency_hashes"]["parser"] = (
        "sha256:changed"
    )
    comparison = _metric(compare_evidence(left, right), "fallow")
    assert comparison["status"] == "incomplete"
    assert comparison["deltas"] is None


def test_colliding_dead_context_preserves_raw_crlf_span_offsets(tmp_path: Path) -> None:
    observed = []
    for name, newline in (("lf", "\n"), ("crlf", "\r\n")):
        lines = [""] * 30 + ["const chars = {", "dot: '.',", "dot: '\\u00b7',", "};"]
        source = newline.join(lines) + newline
        findings = []
        offset = 0
        for number, line in enumerate(lines, 1):
            if line.startswith("dot:"):
                findings.append(
                    {
                        "path": "src/index.ts",
                        "export_name": "dot",
                        "line": number,
                        "span_start": offset,
                    }
                )
            offset += len((line + newline).encode("utf-8"))
        _, result = _bound_fallow(tmp_path / name, {"src/index.ts": source}, [], findings)
        observed.append(result)
    dead = _metric(_metric(compare_evidence(*observed), "fallow"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == dead["resolved_count"] == 0
