"""Explicitly selected pytest leave-one-out coverage, on fresh frozen runtimes."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from pluginbench.assessment_metrics import _object, _strings, inspect_snapshot
from pluginbench.code_diff import _is_test_path
from pluginbench.coverage_report import parse_python_coverage
from pluginbench.harbor_backend import run_native_command

# Runs entirely inside the prepared image. It writes only a report and temporary
# coverage data; removal means deselecting one case, never editing test files.
_OBSERVER = r"""
import fnmatch, json, os, pathlib, sys, tempfile
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import coverage, pytest
settings = json.loads(sys.argv[1])
if pytest.__version__ != settings["pytest_version"] or coverage.__version__ != settings["coverage_version"]:
    raise RuntimeError("pytest/coverage versions differ from frozen settings")
root = pathlib.Path.cwd()
class Observer:
    def __init__(self):
        self.statuses = {}
        self.collected = []
        self.errors = []
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        selected, removed = [], []
        for item in items:
            path = pathlib.Path(item.path).resolve().relative_to(root).as_posix()
            if path not in settings["test_files"] or not any(fnmatch.fnmatchcase(path, p) for p in settings["test_scope"]):
                removed.append(item)
                continue
            self.collected.append(item.nodeid)
            if item.nodeid == settings["excluded_id"]:
                removed.append(item)
            else:
                selected.append(item)
                self.statuses[item.nodeid] = "pending"
        if len(self.collected) != len(set(self.collected)):
            self.errors.append("duplicate pytest case identities")
        items[:] = selected
        config.hook.pytest_deselected(items=removed)
    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(str(report.longrepr))
    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.statuses:
            self.errors.append("case escaped collected population: " + report.nodeid)
        elif report.failed:
            self.statuses[report.nodeid] = "failed"
        elif report.skipped or hasattr(report, "wasxfail"):
            self.statuses[report.nodeid] = "pending"
        elif report.when == "call" and self.statuses[report.nodeid] != "failed":
            self.statuses[report.nodeid] = "passed"
observer = Observer()
with tempfile.TemporaryDirectory(prefix="pluginbench-pytest-") as temp:
    cov = coverage.Coverage(branch=True, config_file=False, source=[str(root)], data_file=temp + "/coverage")
    cov.start()
    code = int(pytest.main(settings["pytest_args"], plugins=[observer]))
    cov.stop()
    cov.json_report(morfs=settings["source_files"], outfile=temp + "/coverage.json")
    result = {
        "exit_code": code,
        "statuses": observer.statuses,
        "collected_ids": sorted(observer.collected),
        "errors": observer.errors,
        "pytest_version": pytest.__version__,
        "coverage_version": coverage.__version__,
        "coverage": json.loads(pathlib.Path(temp + "/coverage.json").read_text()),
    }
    target = root / settings["report_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result))
"""


def _run(
    *,
    snapshot: Path,
    task_identity: dict[str, object],
    destination: Path,
    python: str,
    settings: dict[str, object],
    timeout: int,
) -> tuple[dict[str, object], dict[str, object]]:
    from pluginbench.collectors import CollectorUnavailable

    report_path = ".pluginbench-measure/pytest-contribution.json"
    receipt = run_native_command(
        snapshot=snapshot,
        task_identity=task_identity,
        destination=destination,
        command=[
            python,
            "-B",
            "-c",
            _OBSERVER,
            json.dumps({**settings, "report_path": report_path}),
        ],
        report_path=report_path,
        timeout=timeout,
    )
    if (
        receipt.get("timed_out")
        or receipt.get("exit_code") != 0
        or not receipt.get("report_collected")
    ):
        raise CollectorUnavailable("pytest contribution failed or timed out; see execution.json")
    report = _object(json.loads((destination / "report.raw").read_text()), "pytest report")
    if (
        report.get("pytest_version") != settings["pytest_version"]
        or report.get("coverage_version") != settings["coverage_version"]
    ):
        raise ValueError("pytest contribution tool versions differ from frozen settings")
    if report.get("errors") != []:
        raise CollectorUnavailable("pytest contribution collection failed; see report.raw")
    statuses = _object(report.get("statuses"), "pytest statuses")
    if report.get("exit_code") not in {0, 5} or any(
        status not in {"passed", "pending"} for status in statuses.values()
    ):
        raise CollectorUnavailable(
            "pytest contribution requires a completely passing reference/removal suite"
        )
    coverage_report = destination / "coverage.raw.json"
    coverage_report.write_text(json.dumps(report.get("coverage")))
    normalized = parse_python_coverage(
        coverage_report,
        snapshot=snapshot,
        source_scope=sorted(_strings(settings["source_scope"], "source scope")),
        test_scope=sorted(_strings(settings["test_scope"], "test scope")),
        workdir=str(task_identity["workdir"]),
    )
    return report, normalized


def collect_pytest_contribution(
    *,
    snapshot: Path,
    before_snapshot: Path,
    task_identity: dict[str, object],
    destination: Path,
    source_scope: list[str],
    test_scope: list[str],
    pytest_version: str,
    coverage_version: str,
    python: str = "python",
    pytest_args: list[str] | None = None,
    timeout: int = 600,
) -> dict[str, object]:
    """Measure all runnable cases in changed visible test files; never remove tests."""
    from pluginbench.collectors import CollectorUnavailable

    before, current = inspect_snapshot(before_snapshot), inspect_snapshot(snapshot)
    files = _object(current["files"], "snapshot files")
    before_files = _object(before["files"], "before files")
    source_files = sorted(
        name
        for name in files
        if name.endswith(".py")
        and not _is_test_path(name)
        and any(fnmatch.fnmatchcase(name, pattern) for pattern in source_scope)
    )
    test_files = sorted(
        name
        for name in files
        if _is_test_path(name) and any(fnmatch.fnmatchcase(name, pattern) for pattern in test_scope)
    )
    if not source_files or not test_files:
        raise CollectorUnavailable(
            "pytest contribution requires measured Python sources and visible test files"
        )
    settings: dict[str, object] = {
        "pytest_args": pytest_args if pytest_args is not None else ["-q"],
        "source_scope": source_scope,
        "test_scope": test_scope,
        "source_files": source_files,
        "test_files": test_files,
        "pytest_version": pytest_version,
        "coverage_version": coverage_version,
        "excluded_id": None,
    }
    reference, result = _run(
        snapshot=snapshot,
        task_identity=task_identity,
        destination=destination / "reference",
        python=python,
        settings=settings,
        timeout=timeout,
    )
    statuses = _object(reference["statuses"], "reference statuses")
    reference_ids = set(_strings(reference.get("collected_ids"), "reference identities"))
    if set(statuses) != reference_ids:
        raise ValueError("pytest reference statuses omit collected cases")
    runnable = {name for name, status in statuses.items() if status == "passed"}
    if not runnable:
        raise CollectorUnavailable("pytest contribution reference has no passing runnable cases")
    changed = {name for name in test_files if before_files.get(name) != files[name]}
    candidates = sorted(name for name in runnable if name.partition("::")[0] in changed)
    branches = _object(result.get("branches"), "branch coverage")
    total = set(_strings(branches.get("total"), "branch population"))
    covered = set(_strings(branches.get("covered"), "covered branches"))
    runs: list[dict[str, object]] = []
    zero: list[str] = []
    for index, excluded_id in enumerate(candidates):
        native, measured = _run(
            snapshot=snapshot,
            task_identity=task_identity,
            destination=destination / f"removal-{index}",
            python=python,
            settings={**settings, "excluded_id": excluded_id},
            timeout=timeout,
        )
        remaining_statuses = _object(native["statuses"], "remaining statuses")
        if set(
            _strings(native.get("collected_ids"), "removal identities")
        ) != reference_ids or remaining_statuses != {
            key: value for key, value in statuses.items() if key != excluded_id
        }:
            raise ValueError("pytest removal changed the reference test population or statuses")
        population = _object(measured.get("branches"), "removal branches")
        remaining = set(_strings(population.get("covered"), "remaining branches"))
        if set(_strings(population.get("total"), "removal branch population")) != total:
            raise ValueError("pytest removal changed the measured branch population")
        if remaining == covered:
            zero.append(excluded_id)
        runs.append(
            {
                "excluded_id": excluded_id,
                "exit_code": 0,
                "executed_ids": sorted(runnable - {excluded_id}),
                "passing_ids": sorted(runnable - {excluded_id}),
                "test_statuses": remaining_statuses,
                "covered_branches": sorted(remaining),
                "unique_branches": sorted(covered - remaining),
            }
        )
    if (
        inspect_snapshot(before_snapshot)["digest"] != before["digest"]
        or inspect_snapshot(snapshot)["digest"] != current["digest"]
    ):
        raise ValueError("pytest contribution modified frozen snapshots")
    result.update(
        source_digest=current["digest"],
        before_digest=before["digest"],
        producer={
            "runner": "pytest",
            "pytest_version": pytest_version,
            "coverage_version": coverage_version,
        },
        contribution={
            "method": "leave-one-out-branches-v1",
            "reference_ids": sorted(reference_ids),
            "reference_statuses": statuses,
            "executed_ids": sorted(runnable),
            "passing_ids": sorted(runnable),
            "candidate_ids": candidates,
            "selection": "all runnable cases in added/modified visible test files",
            "changed_test_files": sorted(changed),
            "removal_runs": runs,
            "zero_marginal_ids": zero,
        },
    )
    return result
