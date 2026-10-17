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
    (after / "tests/fixture.zip").write_bytes(b"PK\x00\xff")
    fingerprints = inspect_snapshot(before), inspect_snapshot(after)
    result = compare_snapshots(before, after)
    assert result["files_changed"] == 5
    assert result["source_lines_added"] == 2
    assert result["source_lines_removed"] == 1
    assert result["test_lines_added"] == 1
    assert result["dependency_manifest_changes"] == ["package.json"]
    assert result["binary_patch"] is True
    scoped = assessment_metrics.scoped_test_size(
        before=before, after=after, scope=["*"], change=result
    )
    assert scoped["test_lines_added"] == 1
    assert scoped["after_paths"] == ["tests/a.test.ts"]
    files = inspect_snapshot(after)["files"]
    assert isinstance(files, dict)
    assert "tests/fixture.zip" in files
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


@pytest.mark.parametrize("test_source", [False, True])
def test_scoped_code_size_refuses_non_text_source_instead_of_counting_zero(
    tmp_path: Path, test_source: bool
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    path = "tests/index.py" if test_source else "src/index.ts"
    _write(after / path, "temporary\n")
    (after / path).write_bytes(b"\xff")
    change = compare_snapshots(before, after)
    assert inspect_snapshot(after)["source_lines"] == 0
    with pytest.raises(ValueError, match="UTF-8"):
        measure = (
            assessment_metrics.scoped_test_size
            if test_source
            else assessment_metrics.scoped_code_size
        )
        measure(before=before, after=after, scope=["*"], change=change)


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
    result = _metric(evaluate_evidence(tmp_path, None, tmp_path), "fallow")
    assert result["status"] == "unavailable"
    assert result["metrics"] is None


def test_fallow_import_retains_raw_complexity_and_unknown_boundaries(tmp_path: Path) -> None:
    snapshot, receipt = _receipt(tmp_path, "fallow", _fallow())
    result = _metric(
        evaluate_evidence(
            snapshot,
            {"fallow": str(receipt)},
            tmp_path,
        ),
        "fallow",
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["boundary_violations"] is None
    assert metrics["complexity_distribution"] == {
        "cognitive": [17],
        "cyclomatic": [3],
        "line_count": [8],
    }


def test_unresolved_graph_keeps_dead_code_population_incomplete(tmp_path: Path) -> None:
    report = _fallow()
    check = report["check"]
    assert isinstance(check, dict)
    check["unused_exports"] = []
    check["unresolved_imports"] = [{"path": "src/index.ts"}]
    snapshot, receipt = _receipt(tmp_path, "fallow", report)
    result = _metric(
        evaluate_evidence(snapshot, {"fallow": str(receipt)}, tmp_path),
        "fallow",
    )
    assert _metric(result, "metrics")["unresolved_imports"] == 1
    assert not _metric(result, "metrics")["dead_code_complete_for_declared_scope"]


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
        evaluate_evidence(snapshot, {"fallow": str(receipt)}, tmp_path)


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
        evaluate_evidence(snapshot, {"coverage": str(receipt)}, tmp_path),
        "coverage",
    )
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
        evaluate_evidence(snapshot, {"coverage": str(receipt)}, tmp_path)


def test_empty_coverage_denominator_is_incomplete(tmp_path: Path) -> None:
    report: dict[str, object] = {
        key: {"total": [], "covered": []} for key in ("branches", "functions")
    }
    snapshot, receipt = _receipt(tmp_path, "coverage", report)
    result = _metric(
        evaluate_evidence(snapshot, {"coverage": str(receipt)}, tmp_path),
        "coverage",
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["per_test_count"] is None


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
            tmp_path,
        ),
        "fallow",
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["complexity_complete_for_declared_scope"] is (unmeasured_file is None)
    assert metrics["clone_groups_complete_for_declared_scope"] is (unmeasured_file is None)


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
        evaluate_evidence(snapshot, {"fallow": str(receipt)}, tmp_path),
        "fallow",
    )
    assert not _metric(result, "metrics")["complexity_complete_for_declared_scope"]


@pytest.mark.parametrize("kind", ["coverage", "fallow"])
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


@pytest.mark.parametrize("kind,key", [("coverage", "branch_denominator_digest")])
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
        evaluate_evidence(snapshot, {"fallow": str(receipt)}, tmp_path),
        "fallow",
    )
    assert _metric(result, "metrics")["boundary_rules_declared"] is False
    assert _metric(result, "metrics")["boundary_violations"] is None


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
            tmp_path,
        ),
        "fallow",
    )
    assert result["status"] == "available"
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["dead_code"] == metrics["clone_groups"] == metrics["cycles"] == 0


@pytest.mark.parametrize("discovered_files", [2, 3])
def test_fallow_clone_scope_checks_its_own_production_population(
    tmp_path: Path, discovered_files: int
) -> None:
    snapshot, receipt = _receipt(tmp_path, "fallow", _fallow())
    _write(snapshot / "src/helper.ts", "export const helper = 1;\n")
    _write(snapshot / "src/app.config.ts", "export const app = 1;\n")
    _write(snapshot / "test/index.test.ts", "test();\n")
    report = _fallow()
    report["dupes"] = {"clone_groups": [], "stats": {"total_files": 2, "clone_groups": 0}}
    # All three production inputs must be discovered; native production excludes tests.
    # The nested app.config.ts remains production code, unlike a root tool config.
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
        ["src/helper.ts", "src/index.ts", "src/app.config.ts"][:discovered_files],
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
            tmp_path,
        ),
        "fallow",
    )
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
            tmp_path,
        ),
        "fallow",
    )
    assert not _metric(result, "metrics")["dead_code_complete_for_declared_scope"]
    assert not _metric(result, "metrics")["cycles_complete_for_declared_scope"]


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
    return snapshot, evaluate_evidence(snapshot, {"fallow": "evidence.json"}, directory)


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


def test_duplicated_added_test_lines_count_repeated_blocks_in_tests_only(tmp_path: Path) -> None:
    block = "".join(f"    assert parse('{index}') == {index}\n" for index in range(6))
    tests = "def test_one():\n" + block + "\ndef test_two():\n" + block
    _write(tmp_path / "before/src/a.py", "x = 1\n")
    _write(tmp_path / "after/src/a.py", "x = 1\n" + block + block)
    _write(tmp_path / "after/tests/test_a.py", tests)
    duplication = _metric(
        compare_snapshots(tmp_path / "before", tmp_path / "after"), "test_duplication"
    )
    assert duplication["window_lines"] == 6
    assert duplication["added_test_lines"] == 14
    # Both six-line assertion blocks repeat; the two def lines are unique.
    assert duplication["duplicated_added_test_lines"] == 12


def test_code_added_counts_new_grown_and_deleted_source_functions(tmp_path: Path) -> None:
    existing = "function existing(x) {\n  if (x) { return 1; }\n  return 2;\n}\n"
    removed = "function removed() {\n  return 5;\n}\n"
    grown = existing.replace("  return 2;\n", "  if (!x) { return 3; }\n  return 2;\n")
    added = "function added(y) {\n  return y ? 4 : 5;\n}\n"
    before, left = _bound_fallow(
        tmp_path / "before",
        {"src/index.ts": existing + removed},
        [_function("existing", 1, 4, 3), _function("removed", 5, 3, 5)],
    )
    after, right = _bound_fallow(
        tmp_path / "after",
        {"src/index.ts": grown + added, "src/helper.ts": "function helper() {\n  return 1;\n}\n"},
        [
            _function("existing", 1, 5, 18),
            _function("added", 6, 3, 20),
            _function("helper", 1, 3, 2, path="src/helper.ts"),
        ],
    )
    comparison = _metric(
        compare_evidence(left, right, change=compare_snapshots(before, after)), "fallow"
    )
    added_code = _metric(comparison, "code_added")
    assert added_code["status"] == "available"
    # Growth counts only the increase; deletion subtracts; new functions count in full.
    assert added_code["net_cognitive"] == (18 + 20 + 2) - (3 + 5)
    assert added_code["net_cyclomatic"] == (19 + 21 + 3) - (4 + 6)
    assert added_code["functions_added"] == 2
    assert added_code["source_files_added"] == 1
    # Hotspots: new or grown functions above the cognitive threshold.
    assert added_code["hotspots_added"] == 2
    assert added_code["hotspot_threshold"] == 15


def test_code_added_excludes_test_functions_and_unchanged_hotspots() -> None:
    def row(name: str, path: str, cognitive: int) -> dict[str, object]:
        return {**_function(name, 1, 3, cognitive, path=path)}

    result = assessment_metrics._code_added(
        before_all=[row("legacy", "src/a.py", 40), row("other", "src/b.py", 9)],
        after_all=[
            row("legacy", "src/a.py", 30),
            row("other", "src/b.py", 9),
            row("test_it", "tests/test_a.py", 50),
        ],
        changed_paths={"src/a.py", "tests/test_a.py"},
        new_paths=["tests/test_a.py"],
        complete=True,
    )
    assert result["net_cognitive"] == -10
    assert result["functions_added"] == 0
    assert result["source_files_added"] == 0
    # A shrinking complex function is not a new hotspot.
    assert result["hotspots_added"] == 0
    incomplete = assessment_metrics._code_added(
        before_all=[], after_all=[], changed_paths=set(), new_paths=[], complete=False
    )
    assert incomplete["status"] == "incomplete" and incomplete.get("net_cognitive") is None


def test_code_added_counts_only_growth_when_a_python_function_grows_at_its_end() -> None:
    # RCA spans end at the last statement, so appended lines sit outside the before span.
    before = [{**_function("parse", 1, 5, 20, path="src/a.py"), "cyclomatic": 6}]
    after = [{**_function("parse", 1, 7, 21, path="src/a.py"), "cyclomatic": 7}]
    result = assessment_metrics._code_added(
        before_all=before, after_all=after, changed_paths={"src/a.py"}, new_paths=[], complete=True
    )
    assert result["net_cognitive"] == 1 and result["net_cyclomatic"] == 1
    assert result["functions_added"] == 0
    assert result["hotspots_added"] == 1  # grew and is above the threshold


def test_code_added_ignores_go_closure_rows_and_line_numbers_in_names() -> None:
    before = [{**_function("method@10:Parse", 10, 5, 4, path="pkg/a.go")}]
    after = [
        # The method moved down and grew; its score already includes the closure's.
        {**_function("method@14:Parse", 14, 8, 6, path="pkg/a.go")},
        {**_function("closure@16:5", 16, 3, 2, path="pkg/a.go")},
    ]
    result = assessment_metrics._code_added(
        before_all=before, after_all=after, changed_paths={"pkg/a.go"}, new_paths=[], complete=True
    )
    assert result["net_cognitive"] == 2
    assert result["functions_added"] == 0
    # A package-level function literal has no enclosing declaration, so it counts.
    package_level = assessment_metrics._code_added(
        before_all=[],
        after_all=[{**_function("closure@3:9", 3, 4, 5, path="pkg/b.go")}],
        changed_paths={"pkg/b.go"},
        new_paths=[],
        complete=True,
    )
    assert package_level["net_cognitive"] == 5
    nested = assessment_metrics._code_added(
        before_all=[],
        after_all=[
            {**_function("closure@3:9", 3, 6, 5, path="pkg/b.go")},
            {**_function("closure@5:2", 5, 2, 2, path="pkg/b.go")},
        ],
        changed_paths={"pkg/b.go"},
        new_paths=[],
        complete=True,
    )
    # The inner literal is already inside the package-level literal's score.
    assert nested["net_cognitive"] == 5


def test_code_added_counts_new_source_files_only_when_they_contain_analyzed_functions() -> None:
    result = assessment_metrics._code_added(
        before_all=[],
        after_all=[_function("run", 1, 3, 1, path="src/new.py")],
        changed_paths={"src/new.py", "reproduce.py"},
        new_paths=["reproduce.py", "src/new.py"],
        complete=True,
    )
    assert result["source_files_added"] == 1 and result["functions_added"] == 1


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
        _metric(evaluate_evidence(snapshot, {"fallow": str(receipt)}, tmp_path), "fallow"),
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
        results.append(evaluate_evidence(snapshot, {"fallow": str(receipt)}, directory))
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


def test_coverage_units_compare_independently_without_inventing_go_branches(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    _write(snapshot / "src/main.go", "package main\nfunc main() {}\n")
    report: dict[str, object] = {
        "statements": {
            "unit": "statement",
            "total": ["src/main.go:2.1:2.15"],
            "covered": ["src/main.go:2.1:2.15"],
            "weights": {"src/main.go:2.1:2.15": 3},
        },
        "files_measured": ["src/main.go"],
    }
    metrics = assessment_metrics._coverage(report, {"src/main.go"}, ["src/**"])
    assert metrics["statement_total"] == metrics["statement_covered"] == 3
    assert metrics["statement_percent"] == 100
    assert metrics["branch_total"] is metrics["function_total"] is None
    provenance = {"tool": "go", "tool_version": "1", "config_digest": "same", "scope": ["src/**"]}
    before: dict[str, object] = {
        "coverage": {
            "provenance": provenance,
            "metrics": {**metrics, "statement_covered": 2, "statement_percent": 200 / 3},
        }
    }
    after: dict[str, object] = {"coverage": {"provenance": provenance, "metrics": metrics}}
    compared = _metric(compare_evidence(before, after), "coverage")
    assert _metric(compared, "deltas")["statement_covered"] == 1
    assert _metric(_metric(compared, "units"), "branch")["status"] == "unavailable"
    changed = {
        **metrics,
        "statement_denominator_digest": "different",
        "line_denominator_digest": "line",
        "line_population_complete": True,
        "line_total": 2,
        "line_covered": 1,
    }
    left = {
        **metrics,
        "line_denominator_digest": "line",
        "line_population_complete": True,
        "line_total": 2,
        "line_covered": 0,
    }
    compared = _metric(
        compare_evidence(
            {"coverage": {"provenance": provenance, "metrics": left}},
            {"coverage": {"provenance": provenance, "metrics": changed}},
        ),
        "coverage",
    )
    assert _metric(compared, "deltas")["line_covered"] == 1
    assert "statement_covered" not in _metric(compared, "deltas")


def test_changed_line_coverage_and_redundant_added_tests(tmp_path: Path) -> None:
    before_dir, after_dir = tmp_path / "before", tmp_path / "after"
    _write(before_dir / "src/a.py", "one = 1\ntwo = 2\n")
    _write(after_dir / "src/a.py", "one = 1\ntwo = 2\nthree = 3\n# note\nfive = 5\n")
    _write(after_dir / "tests/test_a.py", "def test_new():\n    pass\n")
    change = compare_snapshots(before_dir, after_dir)
    files = {"src/a.py"}

    def measured(
        lines: list[int], covered: list[int], per_test: dict[str, list[int]]
    ) -> dict[str, object]:
        report: dict[str, object] = {
            "lines": {
                "total": [f"src/a.py:{line}" for line in lines],
                "covered": [f"src/a.py:{line}" for line in covered],
            },
            "per_test_lines": {
                name: [f"src/a.py:{line}" for line in items] for name, items in per_test.items()
            },
            "files_measured": ["src/a.py"],
        }
        return assessment_metrics._coverage(report, files, ["src/**"])

    provenance = {
        "tool": "coverage.py",
        "tool_version": "7",
        "config_digest": "c",
        "scope": ["src/**"],
    }
    before = measured([1, 2], [1, 2], {"test_old": [1, 2]})
    after = measured(
        [1, 2, 3, 5],
        [1, 2, 3],
        {"test_old": [1, 2], "new_a": [3], "new_dup": [3], "new_none": [1]},
    )
    compared = _metric(
        compare_evidence(
            {
                "coverage": {"provenance": provenance, "metrics": before},
                "snapshot_digest": change["before_digest"],
            },
            {
                "coverage": {"provenance": provenance, "metrics": after},
                "snapshot_digest": change["after_digest"],
            },
            change=change,
        ),
        "coverage",
    )
    changed = _metric(compared, "changed_lines")
    # Lines 3 and 5 are added and executable; the comment on line 4 is not executable.
    assert changed["added_executable_lines"] == 2
    assert changed["added_lines_covered"] == 1
    assert changed["line_percent"] == 50
    assert changed["added_tests"] == 3
    # Of two tests covering exactly line 3, one is flagged: removing every flagged test
    # together keeps added-line coverage unchanged.
    assert changed["redundant_added_test_names"] == ["new_a"]
    assert changed["redundant_added_tests"] == 1
    # A test that reaches no added line is reported separately, not as redundant.
    assert changed["added_tests_without_added_lines"] == 1


def test_changed_line_coverage_is_incomplete_when_an_added_source_file_is_unmeasured(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "before/src/a.py", "one = 1\n")
    _write(tmp_path / "after/src/a.py", "one = 1\ntwo = 2\n")
    _write(tmp_path / "after/src/new.py", "three = 3\n")
    change = compare_snapshots(tmp_path / "before", tmp_path / "after")
    report: dict[str, object] = {
        "lines": {"total": ["src/a.py:1", "src/a.py:2"], "covered": ["src/a.py:1", "src/a.py:2"]},
        "files_measured": ["src/a.py"],
    }
    after = assessment_metrics._coverage(report, {"src/a.py", "src/new.py"}, ["src/**"])
    result = assessment_metrics._changed_line_coverage({}, after, change)
    # Leaving the untested new file out of the report must not raise the percentage.
    assert result["status"] == "incomplete"
    assert "src/new.py" in str(result["reason"])


@pytest.mark.parametrize("complete", [False, None])
def test_partial_coverage_cannot_claim_gain_and_preserves_unit_unavailability(
    complete: bool | None,
) -> None:
    reason = "Repeated LCOV function names have mixed hits"
    metrics = {
        "format": "lcov",
        "function_denominator_digest": "same-subset",
        "function_population_complete": complete,
        "function_total": 2,
        "function_covered": 0,
        "line_denominator_digest": "complete-lines",
        "line_population_complete": True,
        "line_total": 5,
        "line_covered": 4,
        "unavailable_populations": {"functions": {"reason": reason, "files": ["callbacks.js"]}},
    }
    compared = _metric(
        compare_evidence(
            {"coverage": {"provenance": {}, "metrics": metrics}},
            {
                "coverage": {
                    "provenance": {},
                    "metrics": {**metrics, "function_covered": 2, "line_covered": 5},
                }
            },
        ),
        "coverage",
    )
    assert _metric(compared, "deltas")["line_covered"] == 1
    assert "function_covered" not in _metric(compared, "deltas")
    units = _metric(compared, "units")
    assert _metric(units, "function")["status"] == "unavailable"
    assert reason in str(_metric(units, "function")["reason"])
    assert "lcov" in str(_metric(units, "statement")["reason"])
    assert "not supplied" in str(_metric(units, "statement")["reason"])


def _python_candidate_evidence(
    root: Path, source: str, candidates: list[dict[str, object]]
) -> dict[str, object]:
    _write(root / "source.py", source)
    config: dict[str, object] = {
        "language": "python",
        "tool_identities": {},
        "settings": {},
        "supported_suffixes": [".py"],
    }
    report: dict[str, object] = {
        "version": "language-analysis-v1",
        "language": "python",
        "tools": {},
        "settings": {},
        "supported_suffixes": [".py"],
        "analyzed_paths": ["source.py"],
        "functions": [],
        "metrics": {
            key: {"status": "available"} for key in ("cognitive", "cyclomatic", "dead_code")
        },
        "unused_candidates": [{"path": "source.py", **row} for row in candidates],
    }
    metrics = assessment_metrics._analysis(report, {"source.py"}, ["*"], root, config)
    return {"analysis": {"provenance": config, "metrics": metrics}}


@pytest.mark.parametrize("remove_second", [False, True])
def test_python_unused_declarations_match_lexical_scopes_after_line_shifts_and_deletion(
    tmp_path: Path, remove_second: bool
) -> None:
    source = "class First:\n    node_id: str\n\nclass Second:\n    node_id: str\n"
    candidates = [
        {"category": "vulture-unused-variable", "symbol": "node_id", "line": line}
        for line in (2, 5)
    ]
    before = _python_candidate_evidence(tmp_path / "before", source, candidates)
    after_source = "\n" + (source.split("class Second:")[0] if remove_second else source)
    after_candidates = []
    for row in candidates[: 1 if remove_second else 2]:
        line = row["line"]
        assert isinstance(line, int)
        after_candidates.append({**row, "line": line + 1})
    after = _python_candidate_evidence(tmp_path / "after", after_source, after_candidates)
    dead = _metric(_metric(compare_evidence(before, after), "analysis"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == 0
    assert dead["resolved_count"] == int(remove_second)


@pytest.mark.parametrize(
    "source,candidate",
    [
        (
            "def duplicate():\n    item = 1\ndef duplicate():\n    item = 1\n",
            {"category": "vulture-unused-variable", "symbol": "item", "line": 2},
        ),
        ("value = 1\n", {"category": "vulture-unused-variable", "symbol": "wrong", "line": 1}),
        (
            "def owner():\n    item = 1; item = 2\n",
            {"category": "vulture-unused-variable", "symbol": "item", "line": 2},
        ),
        (
            "item = 1\nitem = 1\n",
            {"category": "vulture-unused-variable", "symbol": "item", "line": 1},
        ),
        (
            "@overload\ndef owner(item: str): ...\n@overload\ndef owner(item: str): ...\n",
            {"category": "vulture-unused-variable", "symbol": "item", "line": 2},
        ),
        (
            "one = lambda item: 1\ntwo = lambda item: 1\n",
            {"category": "vulture-unused-variable", "symbol": "item", "line": 1},
        ),
        (
            "values = [item for item in ()]\n",
            {"category": "vulture-unused-variable", "symbol": "item", "line": 1},
        ),
    ],
)
def test_python_unused_declarations_require_unique_source_binding(
    tmp_path: Path, source: str, candidate: dict[str, object]
) -> None:
    before = _python_candidate_evidence(tmp_path / "before", source, [candidate])
    after = _python_candidate_evidence(tmp_path / "after", source, [])
    dead = _metric(_metric(compare_evidence(before, after), "analysis"), "dead_findings")
    assert dead["status"] == "unavailable"
    assert dead["resolved_count"] is None
    assert _metric(_metric(before, "analysis"), "metrics")["dead_code"] == 1


def test_python_declaration_roles_bind_parameters_imports_attributes_accessors_and_overloads(
    tmp_path: Path,
) -> None:
    source = (
        "from pathlib import (\n    Path as Alias,\n)\n\n"
        "class First:\n    @property\n    def value(self):\n        return 1\n\n"
        "    @value.setter\n    def value(self, unused):\n        self.unused_attr = unused\n\n"
        "class Second:\n    def value(self, unused):\n        unused_local = 1\n\n"
        "@overload\ndef operation(unused: str) -> str: ...\n"
        "@overload\ndef operation(unused: int) -> int: ...\n\n"
        "one = lambda ignored: 1\ntwo = lambda ignored: 2\n"
    )
    candidates: list[dict[str, object]] = [
        {"category": f"vulture-unused-{role}", "symbol": symbol, "line": line}
        for role, symbol, line in (
            ("import", "Alias", 1),
            ("class", "First", 5),
            ("property", "value", 6),
            ("method", "value", 10),
            ("variable", "unused", 11),
            ("attribute", "unused_attr", 12),
            ("variable", "unused", 15),
            ("variable", "unused_local", 16),
            ("function", "operation", 18),
            ("variable", "unused", 19),
            ("function", "operation", 20),
            ("variable", "unused", 21),
            ("variable", "ignored", 23),
            ("variable", "ignored", 24),
        )
    ]
    before = _python_candidate_evidence(tmp_path / "before", source, candidates)
    shifted_candidates = []
    for row in candidates[1:]:
        line = row["line"]
        assert isinstance(line, int)
        shifted_candidates.append({**row, "line": line + 1})
    after = _python_candidate_evidence(tmp_path / "after", "\n" + source, shifted_candidates)
    dead = _metric(_metric(compare_evidence(before, after), "analysis"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == 0 and dead["resolved_count"] == 1
    assert dead["before_count"] == 14 and dead["after_count"] == 13


def test_python_declaration_matching_uses_full_source_collision_population(tmp_path: Path) -> None:
    source = "def owner():\n    unused = 1\n    unused = 2\n"
    candidate = {"category": "vulture-unused-variable", "symbol": "unused"}
    before = _python_candidate_evidence(tmp_path / "before", source, [{**candidate, "line": 2}])
    after = _python_candidate_evidence(tmp_path / "after", source, [{**candidate, "line": 3}])
    dead = _metric(_metric(compare_evidence(before, after), "analysis"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == dead["resolved_count"] == 1
    after = _python_candidate_evidence(
        tmp_path / "deleted",
        "def owner():\n    unused = 1\n",
        [{**candidate, "line": 2}],
    )
    dead = _metric(_metric(compare_evidence(before, after), "analysis"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == dead["resolved_count"] == 0


def test_python_default_expression_has_outer_lexical_scope(tmp_path: Path) -> None:
    candidate = {"category": "vulture-unused-variable", "symbol": "ignored", "line": 1}
    before = _python_candidate_evidence(
        tmp_path / "before",
        "def owner(callback=lambda ignored: 1): pass\n",
        [candidate],
    )
    after = _python_candidate_evidence(
        tmp_path / "after",
        "def renamed(callback=lambda ignored: 1): pass\n",
        [candidate],
    )
    dead = _metric(_metric(compare_evidence(before, after), "analysis"), "dead_findings")
    assert dead["status"] == "available"
    assert dead["introduced_count"] == dead["resolved_count"] == 0
