from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import ModuleType

import pytest


@pytest.mark.parametrize("fallback", [False, True])
def test_fresh_workspace_copies_have_independent_writable_files_and_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    module = adapter()
    template = tmp_path / "template"
    (template / "node_modules/package").mkdir(parents=True)
    (template / "source.js").write_text("original source")
    dependency = template / "node_modules/package/index.js"
    dependency.write_text("original dependency")
    dependency.chmod(0o755)
    (template / "node_modules/linked").symlink_to("package", target_is_directory=True)
    if fallback:
        monkeypatch.setattr(module.sys, "platform", "darwin")

        def unsupported(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            # A failed native copy can leave an incomplete destination behind.
            command = args[0]
            assert isinstance(command, list)
            destination = Path(str(command[-1]))
            destination.mkdir()
            (destination / "partial").write_text("partial")
            return subprocess.CompletedProcess(["cp"], 1, "", "cloning unsupported")

        monkeypatch.setattr(module.subprocess, "run", unsupported)
    first = tmp_path / "first"
    second = tmp_path / "second"
    module.copy_workspace(template, first)
    module.copy_workspace(template, second)
    assert (first / "node_modules/linked").is_symlink()
    assert (first / "node_modules/linked").readlink() == Path("package")
    assert (first / "node_modules/package/index.js").stat().st_mode & 0o777 == 0o755
    assert not (first / "partial").exists()
    (first / "source.js").write_text("mutated source")
    (first / "node_modules/linked/index.js").write_text("mutated dependency")
    assert (template / "source.js").read_text() == "original source"
    assert dependency.read_text() == "original dependency"
    assert (second / "source.js").read_text() == "original source"
    assert (second / "node_modules/package/index.js").read_text() == "original dependency"
    with pytest.raises(FileExistsError):
        module.copy_workspace(template, first)
    assert (first / "source.js").read_text() == "mutated source"


def adapter() -> ModuleType:
    path = Path(
        os.environ.get(
            "COVERAGE_ADAPTER",
            str(Path(__file__).parents[1] / "src/pluginbench/task_data/deepswe/coverage.py"),
        )
    )
    spec = importlib.util.spec_from_file_location("metric_coverage", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reference() -> dict[str, object]:
    return {
        "branches": {"total": ["a", "b", "uncovered"], "covered": ["a", "b"]},
        "functions": {"total": ["fn", "unexecuted"], "covered": ["fn"]},
        "tests": [
            {
                "id": "test/new.ts::async hook",
                "file": "test/new.ts",
                "title": "async hook",
                "status": "passed",
            },
            {
                "id": "test/new.ts::overlap",
                "file": "test/new.ts",
                "title": "overlap",
                "status": "passed",
            },
            {
                "id": "test/base.ts::retained",
                "file": "test/base.ts",
                "title": "retained",
                "status": "passed",
            },
            {
                "id": "test/new.ts::pending",
                "file": "test/new.ts",
                "title": "pending",
                "status": "pending",
            },
        ],
        "execution": {"exit_code": 0},
    }


def removal(identity: str, covered: list[str]) -> dict[str, object]:
    row = reference()
    tests = row["tests"]
    branches = row["branches"]
    assert isinstance(tests, list) and isinstance(branches, dict)
    row["tests"] = [test for test in tests if test["id"] != identity]
    branches["covered"] = covered
    row["excluded_id"] = identity
    return row


def test_fresh_removals_preserve_population_and_identify_zero_marginal_proxy() -> None:
    module = adapter()
    result = module.contribution(
        reference(),
        reference(),
        [
            removal("test/new.ts::async hook", ["a"]),
            removal("test/new.ts::overlap", ["a", "b"]),
        ],
        ["test/new.ts"],
    )
    assert result["candidate_ids"] == ["test/new.ts::async hook", "test/new.ts::overlap"]
    assert result["zero_marginal_ids"] == ["test/new.ts::overlap"]
    assert result["removal_runs"][0]["unique_branches"] == ["b"]
    assert result["removal_runs"][1]["unique_branches"] == []
    assert len(result["reference_ids"]) == 4
    assert len(result["passing_ids"]) == 3
    assert result["reference_statuses"]["test/new.ts::pending"] == "pending"
    assert result["label"] == "zero marginal branch contribution"


def test_unchanged_test_files_have_valid_zero_of_zero_candidates() -> None:
    result = adapter().contribution(reference(), reference(), [], [])
    assert result["candidate_ids"] == []
    assert result["removal_runs"] == []
    assert result["zero_marginal_ids"] == []
    assert len(result["reference_ids"]) == 4


def test_inconsistent_bound_baseline_is_refused_before_running_tools(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "source.js").write_text("module.exports = 1;")
    (after / "source.js").write_text("module.exports = 2;")

    def forbidden_digest(path: Path) -> str:
        pytest.fail(f"unbound collection accessed dependencies: {path}")

    with pytest.raises(ValueError, match="baseline source digest mismatch"):
        adapter().collect(
            submitted=after,
            before=before,
            payload={},
            config={"before_digest": "sha256:incorrect"},
            dependency_digest=forbidden_digest,
        )


def test_fixed_reference_adds_every_modified_or_new_runnable_visible_file(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    for root in (before, after):
        (root / "test").mkdir(parents=True)
        (root / "test/base.test.ts").write_text("it('reference',()=>{});")
        (root / "test/unrelated.test.ts").write_text("it('unrelated',()=>{});")
        (root / "test/modified.test.ts").write_text("it('old',()=>{});")
    (after / "test/modified.test.ts").write_text("it('changed',()=>{});")
    (after / "test/added.test.ts").write_text("it('new',()=>{});")
    (after / "test/setup.ts").write_text("setup();")
    config = {
        "test_scope": ["test/*.test.ts"],
        "reference_test_files": ["test/base.test.ts"],
        "excluded_test_files": [],
    }
    selected, changed, policy = adapter().select_tests(
        submitted=after, before=before, config=config
    )
    assert selected == ["test/added.test.ts", "test/base.test.ts", "test/modified.test.ts"]
    assert changed == ["test/added.test.ts", "test/modified.test.ts"]
    assert policy == "fixed-reference-plus-all-changed-test-files-v1"
    assert adapter().select_tests(submitted=before, before=before, config=config)[:2] == (
        ["test/base.test.ts"],
        [],
    )
    config["reference_test_files"] = ["test/absent.test.ts"]
    with pytest.raises(ValueError, match="existing eligible"):
        adapter().select_tests(submitted=after, before=before, config=config)


@pytest.mark.parametrize(
    "fault", ["missing", "wrong_case", "gained_branch", "denominator", "repeat"]
)
def test_incomplete_or_unstable_fresh_runs_are_refused(fault: str) -> None:
    module = adapter()
    repeat = reference()
    runs = [removal("test/new.ts::async hook", ["a"]), removal("test/new.ts::overlap", ["a", "b"])]
    if fault == "missing":
        runs.pop()
    elif fault == "wrong_case":
        runs[0]["tests"] = []
    elif fault == "gained_branch":
        branches = runs[0]["branches"]
        assert isinstance(branches, dict)
        branches["covered"] = ["a", "b", "uncovered"]
    elif fault == "denominator":
        branches = runs[0]["branches"]
        assert isinstance(branches, dict)
        branches["total"] = ["a", "b"]
    else:
        branches = repeat["branches"]
        assert isinstance(branches, dict)
        branches["covered"] = ["a"]
    with pytest.raises(ValueError):
        module.contribution(reference(), repeat, runs, ["test/new.ts"])


@pytest.mark.parametrize(
    "fault",
    [
        "branches",
        "functions",
        "test_population",
        "native_failure",
        "native_unhandled",
        "native_timeout",
        "inner_mutation",
        "submitted_mutation",
        "before_mutation",
        "dependency_mutation",
        "observer_mutation",
        "timeout_mutation",
        "exclusion_receipt",
        "exclusion_unavailable",
        "exclusion_timeout",
        "exclusion_timeout_mismatch",
        "exclusion_timeout_receipt_directory",
        "exclusion_timeout_receipt_symlink",
        "exclusion_timeout_mutation",
        None,
    ],
)
@pytest.mark.parametrize("disable_thresholds", [False, True])
@pytest.mark.parametrize("provider", ["v8", "istanbul"])
def test_reference_repeat_validation_and_coverage_only_avoid_candidate_exclusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
    disable_thresholds: bool,
    provider: str,
) -> None:
    module = adapter()
    before = tmp_path / "before"
    after = tmp_path / "after"
    dependencies = tmp_path / "dependencies"
    for package in ("vitest", f"@vitest/coverage-{provider}"):
        directory = dependencies / package
        directory.mkdir(parents=True)
        (directory / "package.json").write_text('{"version":"4.0.5"}')
    (before / "src").mkdir(parents=True)
    (before / "test").mkdir()
    (before / "src/choose.js").write_text("export function choose(x) { return x ? 1 : 2; }")
    (before / "test/base.test.ts").write_text("it('retained',()=>{});")
    shutil.copytree(before, after)
    (after / "test/new.test.ts").write_text("it('candidate',()=>{});")
    observer = tmp_path / "observer"
    shutil.copytree(dependencies, observer)
    calls: list[str] = []

    def copy(source: Path, destination: Path) -> str:
        shutil.copytree(source, destination, symlinks=True)
        return "test-independent-copy"

    def run(command: list[str], *, cwd: Path, **kwargs: object) -> subprocess.CompletedProcess[str]:
        exclusion = command[-1].endswith("exclusion-options.json")
        if exclusion:
            assert fault and fault.startswith("exclusion_")
            options = json.loads(Path(command[-1]).read_text())["options"]
            report = Path(options["outputFile"])
        else:
            assert "--testNamePattern" not in command, "unstable repeat triggered an exclusion run"
            assert f"--coverage.provider={provider}" in command
            assert ("--typecheck.enabled=false" in command) is disable_thresholds
            assert all(
                (f"--coverage.thresholds.{metric}=0" in command) is disable_thresholds
                for metric in ("branches", "functions", "lines", "statements")
            )
            report = Path(
                next(arg.split("=", 1)[1] for arg in command if arg.startswith("--outputFile="))
            )
        workspace = report.parent / "workspace"
        calls.append(report.parent.name)
        repeated = report.parent.name == "reference-repeat"
        location = {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 50}}
        coverage = {
            str(workspace / "src/choose.js"): {
                "b": {"0": [1, 0 if repeated and fault == "branches" else 1]},
                "branchMap": {
                    "0": {"type": "cond-expr", "loc": location, "locations": [location, location]}
                },
                "f": {"0": 0 if repeated and fault == "functions" else 1},
                "fnMap": {"0": {"loc": location}},
            }
        }
        tests = [
            {
                "name": str(workspace / "test/base.test.ts"),
                "assertionResults": [{"fullName": "retained", "status": "passed"}],
            },
            {
                "name": str(workspace / "test/new.test.ts"),
                "assertionResults": [
                    {
                        "fullName": "candidate",
                        "failureMessages": [
                            "AssertionError: visible contract failed\nprivate stack/source"
                        ],
                        "status": "failed"
                        if fault == "native_failure"
                        else "pending"
                        if exclusion or repeated and fault == "test_population"
                        else "passed",
                    }
                ],
            },
        ]
        native = {
            "testResults": tests,
            "success": fault != "native_failure",
            "numPassedTests": 2,
            "numFailedTests": int(fault == "native_failure"),
            "numFailedTestSuites": 0,
        }
        report.write_text(json.dumps(native))
        errors = (
            [
                {
                    "name": "AbortError",
                    "message": "unhandled stream abort\nprivate source",
                    "stack": "private stack",
                }
            ]
            if fault == "native_unhandled"
            else []
        )
        (report.parent / "native-errors.json").write_text(
            json.dumps({"reason": "failed" if errors else "passed", "errors": errors})
        )
        (report.parent / "coverage/coverage-final.json").write_text(json.dumps(coverage))
        if exclusion and fault == "exclusion_unavailable":
            shutil.copyfile(
                report.parent / "excluded-case.json", report.parent / "exclusion-receipt.json"
            )
        if exclusion and fault == "exclusion_timeout_mismatch":
            (report.parent / "exclusion-receipt.json").write_text("{}")
        if exclusion and fault == "exclusion_timeout_receipt_directory":
            (report.parent / "exclusion-receipt.json").mkdir()
        if exclusion and fault == "exclusion_timeout_receipt_symlink":
            (report.parent / "exclusion-receipt.json").symlink_to(report.parent / "missing-receipt")
        mutation = {
            "inner_mutation": workspace / "src/choose.js",
            "timeout_mutation": workspace / "src/choose.js",
            "exclusion_timeout_mutation": workspace / "src/choose.js" if exclusion else None,
            "submitted_mutation": after / "src/choose.js",
            "before_mutation": before / "src/choose.js",
            "dependency_mutation": dependencies / "mutation-marker",
            "observer_mutation": observer / "mutation-marker",
        }.get(fault or "")
        if mutation is not None:
            mutation.write_text("mutated during native execution")
        if fault in {"native_timeout", "timeout_mutation"} or (
            exclusion and fault and fault.startswith("exclusion_timeout")
        ):
            raise subprocess.TimeoutExpired(command, 1, output=b"partial output", stderr=b"timeout")
        return subprocess.CompletedProcess(
            command,
            int(
                fault in {"native_failure", "native_unhandled"} or mutation is not None or exclusion
            ),
            "",
            "",
        )

    monkeypatch.setattr(module, "copy_workspace", copy)
    monkeypatch.setattr(module.subprocess, "run", run)

    def collect() -> dict[str, object]:
        result = module.collect(
            submitted=after,
            before=before,
            payload={
                "dependencies": str(dependencies),
                "dependency_sha256": "bound",
                "trusted_files": {},
            },
            config={
                "source_scope": ["src/**"],
                "test_scope": ["test/*.test.ts"],
                "provider": f"vitest/{provider}",
                "version": "4.0.5",
                **({"measure_test_overlap": True} if fault is not None else {}),
                "disable_coverage_thresholds": disable_thresholds,
                "disable_typechecking": disable_thresholds,
                "observer": {"path": str(observer), "sha256": "bound"},
            },
            dependency_digest=lambda path: (
                "changed" if (path / "mutation-marker").exists() else "bound"
            ),
            diagnostics=tmp_path / "native-diagnostics",
        )
        assert isinstance(result, dict)
        return result

    if fault is None:
        result = collect()
        assert "contribution" not in result
        branches = result["branches"]
        assert isinstance(branches, dict) and branches["covered"]
    elif fault in {"native_failure", "native_unhandled", "native_timeout"}:
        message = (
            "candidate: AssertionError: visible contract failed"
            if fault == "native_failure"
            else "native unhandled error: unhandled stream abort"
            if fault == "native_unhandled"
            else "visible coverage run reference timed out"
        )
        with pytest.raises(module.MeasurementUnavailable, match=message) as error:
            collect()
        assert "private stack/source" not in str(error.value)
        assert "private source" not in str(error.value)
        assert "private stack" not in str(error.value)
        saved = tmp_path / "native-diagnostics/reference"
        assert json.loads((saved / "tests.json").read_text())["success"] is (
            fault != "native_failure"
        )
        assert json.loads((saved / "execution.json").read_text())["exit_code"] == (
            124 if fault == "native_timeout" else 1
        )
        if fault == "native_timeout":
            assert json.loads((saved / "execution.json").read_text())["timed_out"] is True
        assert (saved / "native-errors.json").is_file()
        assert calls == ["reference"]
        return
    elif fault in {
        "exclusion_receipt",
        "exclusion_unavailable",
        "exclusion_timeout",
        "exclusion_timeout_mismatch",
        "exclusion_timeout_receipt_directory",
        "exclusion_timeout_receipt_symlink",
    }:
        unavailable = fault in {"exclusion_unavailable", "exclusion_timeout"}
        expected = module.MeasurementUnavailable if unavailable else ValueError
        message = (
            f"visible coverage run remove-0 {'timed out' if fault == 'exclusion_timeout' else 'failed'}"
            if unavailable
            else "native case exclusion receipt missing or mismatched"
        )
        with pytest.raises(expected, match=message) as error:
            collect()
        assert isinstance(error.value, module.MeasurementUnavailable) is unavailable
        if fault == "exclusion_timeout":
            saved = tmp_path / "native-diagnostics/remove-0"
            execution = json.loads((saved / "execution.json").read_text())
            assert execution["timed_out"] is True
            assert execution["exit_code"] == 124
            assert execution["stdout"] == "partial output"
            assert not (saved / "exclusion-receipt.json").exists()
        assert calls == ["reference", "reference-repeat", "remove-0"]
        return
    elif fault and fault.endswith("mutation"):
        message = (
            "measured source/test bytes changed"
            if fault in {"inner_mutation", "timeout_mutation", "exclusion_timeout_mutation"}
            else "source changed during coverage collection"
            if fault in {"submitted_mutation", "before_mutation"}
            else "dependency cache changed during collection"
        )
        with pytest.raises(ValueError, match=message) as error:
            collect()
        assert not isinstance(error.value, module.MeasurementUnavailable)
        assert calls == (
            ["reference", "reference-repeat", "remove-0"]
            if fault == "exclusion_timeout_mutation"
            else ["reference"]
        )
        return
    else:
        with pytest.raises(ValueError, match="not repeatable|population differs") as error:
            collect()
        assert not isinstance(error.value, module.MeasurementUnavailable)
    assert calls == ["reference", "reference-repeat"]


def test_source_locations_survive_istanbul_sequence_id_changes(tmp_path: Path) -> None:
    module = adapter()
    source = tmp_path / "packages/example/src/unused.js"
    row = {
        "path": str(source),
        "b": {"0": [0]},
        "branchMap": {
            "0": {
                "type": "if",
                "loc": {"start": {"line": 2, "column": 0}, "end": {"line": 4, "column": 5}},
                "locations": [{"start": {"line": 2, "column": 3}, "end": {"line": 4, "column": 5}}],
            }
        },
        "f": {"0": 0},
        "fnMap": {
            "0": {"loc": {"start": {"line": 1, "column": 0}, "end": {"line": 5, "column": 1}}}
        },
    }
    changed = deepcopy(row)
    for name in ("b", "branchMap", "f", "fnMap"):
        values = changed[name]
        assert isinstance(values, dict)
        values["99"] = values.pop("0")
    first = module.normalize({str(source): row}, tmp_path, ["packages/example/src/**"])
    second = module.normalize({str(source): changed}, tmp_path, ["packages/example/src/**"])
    assert first["branches"] == second["branches"]
    assert first["functions"] == second["functions"]
    assert first["branches"]["covered"] == []
    assert first["functions"]["covered"] == []
    assert first["files_measured"] == ["packages/example/src/unused.js"]

    # An implicit else has no arm location in Istanbul. Anchor it to its
    # enclosing source branch, retaining its arm number rather than a map ID.
    branch_map = row["branchMap"]
    counts = row["b"]
    assert isinstance(branch_map, dict) and isinstance(counts, dict)
    branch_map["0"]["locations"].append({"start": {}, "end": {}})
    counts["0"].append(0)
    implicit = module.normalize({str(source): row}, tmp_path, ["packages/example/src/**"])
    assert len(implicit["branches"]["total"]) == 2
    assert any("implicit-arm:1" in coordinate for coordinate in implicit["branches"]["total"])


def test_reported_file_full_title_and_native_occurrences_preserve_all_records(
    tmp_path: Path,
) -> None:
    module = adapter()
    case = {"file": str(tmp_path / "tests/async.js"), "fullTitle": "suite async hook"}
    report = {"tests": [case], "passes": [case], "failures": [], "pending": []}
    rows = module.test_rows(report, "mocha", tmp_path)
    assert rows == [
        {
            "file": "tests/async.js",
            "title": "suite async hook",
            "status": "passed",
            "id": "tests/async.js::suite async hook",
        }
    ]
    report["tests"].append(case)
    duplicated = module.test_rows(report, "mocha", tmp_path)
    assert [row["id"] for row in duplicated] == [
        "tests/async.js::suite async hook::native-occurrence:1",
        "tests/async.js::suite async hook::native-occurrence:2",
    ]
    assert [row["status"] for row in duplicated] == ["passed", "passed"]
    full = reference()
    full["tests"] = duplicated
    result = module.contribution(full, full, [], [])
    assert len(result["reference_ids"]) == 2
    assert result["duplicate_reference_groups"]["tests/async.js::suite async hook"] == {
        "record_ids": [row["id"] for row in duplicated],
        "aggregation": "worst-status",
        "worst_status": "passed",
    }


@pytest.mark.parametrize("kind", ["unavailable", "integrity", "protocol", "unexpected_timeout"])
def test_coverage_exit_contract_keeps_integrity_and_protocol_fatal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    module = adapter()
    error = (
        module.MeasurementUnavailable("native unavailable")
        if kind == "unavailable"
        else ValueError("integrity violated")
        if kind == "integrity"
        else KeyError("protocol violated")
        if kind == "protocol"
        else subprocess.TimeoutExpired(["unexpected process"], 1)
    )

    def failed() -> None:
        raise error

    monkeypatch.setattr(module, "main", failed)
    assert module.cli() == (3 if kind == "unavailable" else 2)
    assert capsys.readouterr().err
