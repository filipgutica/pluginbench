"""Source-bound visible coverage and fresh-process marginal branch measurements.

Usage: coverage.py VERIFIER OUTPUT [--config CONFIG] [--before SNAPSHOT].
Observer dependencies live outside submissions. No held-out patch is applied.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from fnmatch import fnmatchcase
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

from pluginbench.assessment_metrics import inspect_snapshot

ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class MeasurementUnavailable(ValueError):
    """Native execution failed after the collector integrity checks passed."""


class Observer(TypedDict):
    path: str
    sha256: str


class DependencyTree(Observer):
    relative: str


class ClockPolicy(TypedDict):
    policy: Literal["stepped-date-v1"]
    epoch_ms: Literal[0]
    step_ms: Literal[1]
    installation: Literal["node-require-before-mocha"]
    implementation_sha256: str


class Configuration(TypedDict):
    measure_test_overlap: NotRequired[bool]
    disable_coverage_thresholds: NotRequired[bool]
    disable_typechecking: NotRequired[bool]
    source_scope: list[str]
    test_scope: list[str]
    provider: str
    version: str
    before_digest: NotRequired[str]
    framework_version: NotRequired[str]
    observer: NotRequired[Observer]
    reference_test_files: NotRequired[list[str]]
    excluded_test_files: NotRequired[list[str]]
    excluded_full_titles: NotRequired[list[str]]
    runner_files: NotRequired[list[str]]
    run_timeout_seconds: NotRequired[int]
    clock: NotRequired[ClockPolicy]


class Payload(TypedDict):
    dependencies: str
    dependency_sha256: str
    trusted_files: dict[str, str]
    framework: NotRequired[str]
    test_cwd: NotRequired[str]
    extra_dependency_trees: NotRequired[list[DependencyTree]]


Location = dict[str, dict[str, int]]


class Branch(TypedDict):
    type: str
    loc: Location
    locations: list[Location]


class Function(TypedDict):
    loc: Location


class IstanbulFile(TypedDict):
    b: dict[str, list[int]]
    branchMap: dict[str, Branch]
    f: dict[str, int]
    fnMap: dict[str, Function]


class NativeCase(TypedDict, total=False):
    file: str
    fullTitle: str
    fullName: str
    status: str
    failureMessages: list[str]
    err: dict[str, str]


class VitestFile(TypedDict):
    name: str
    assertionResults: list[NativeCase]
    message: NotRequired[str]


class NativeTests(TypedDict, total=False):
    testResults: list[VitestFile]
    tests: list[NativeCase]
    failures: list[NativeCase]
    pending: list[NativeCase]
    passes: list[NativeCase]
    success: bool
    numPassedTests: int
    numFailedTests: int
    numFailedTestSuites: int


class NativeError(TypedDict, total=False):
    name: str
    message: str
    stack: str
    type: str
    VITEST_TEST_NAME: str


class NativeErrors(TypedDict):
    reason: str
    errors: list[NativeError]


class TestRow(TypedDict):
    file: str
    title: str
    status: str
    id: NotRequired[str]
    native_occurrence: NotRequired[int]


class Population(TypedDict):
    total: list[str]
    covered: list[str]


class Execution(TypedDict):
    timed_out: NotRequired[bool]
    name: str
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    workspace_copy: dict[str, str | float]


class CoverageRun(TypedDict):
    files_measured: list[str]
    branches: Population
    functions: Population
    test_population: str
    istanbul: dict[str, dict[str, object]]
    coordinate_method: str
    tests: NotRequired[list[TestRow]]
    execution: NotRequired[Execution]
    excluded_id: NotRequired[str]
    contribution: NotRequired[dict[str, object]]
    method: NotRequired[str]
    source_scope: NotRequired[list[str]]
    test_scope: NotRequired[list[str]]
    source_digest: NotRequired[str]
    before_digest: NotRequired[str]
    test_files: NotRequired[list[str]]
    selected_test_files: NotRequired[list[str]]
    reference_test_files: NotRequired[list[str] | None]
    selection_policy: NotRequired[str]
    excluded_test_files: NotRequired[list[str]]
    excluded_full_titles: NotRequired[list[str]]
    runner_files_restored: NotRequired[list[str]]
    executions: NotRequired[list[Execution]]
    tool: NotRequired[str]
    tool_version: NotRequired[str]
    framework_version: NotRequired[str]
    observer: NotRequired[Observer | None]
    dependency_trees: NotRequired[list[DependencyTree]]
    adapter_sha256: NotRequired[str]
    configuration_digest: NotRequired[str]
    verifier_sha256: NotRequired[str]
    config_sha256: NotRequired[str]
    node: NotRequired[str]
    clock: NotRequired[ClockPolicy]


KINDS: tuple[Literal["branches", "functions"], ...] = ("branches", "functions")
POPULATIONS: tuple[Literal["total", "covered"], ...] = ("total", "covered")

INSTRUMENT = r"""
const fs = require('node:fs');
const path = require('node:path');
const [observer, workspace, manifest, initial] = process.argv.slice(2);
const { createInstrumenter } = require(path.join(observer, 'istanbul-lib-instrument'));
const coverage = {};
for (const relative of JSON.parse(fs.readFileSync(manifest, 'utf8'))) {
  const filename = path.join(workspace, relative);
  const instrumenter = createInstrumenter({ produceSourceMap: false });
  const code = instrumenter.instrumentSync(fs.readFileSync(filename, 'utf8'), filename);
  coverage[filename] = instrumenter.lastFileCoverage();
  fs.writeFileSync(filename, code);
}
fs.writeFileSync(initial, JSON.stringify(coverage));
"""

PRELOAD = r"""
const fs = require('node:fs');
const path = require('node:path');
const { createCoverageMap } = require(path.join(process.env.PLUGINBENCH_OBSERVER, 'istanbul-lib-coverage'));
process.on('exit', () => {
  const coverage = createCoverageMap(JSON.parse(fs.readFileSync(process.env.PLUGINBENCH_INITIAL, 'utf8')));
  coverage.merge(global.__coverage__ || {});
  fs.writeFileSync(process.env.PLUGINBENCH_COVERAGE, JSON.stringify(coverage));
});
"""


VITEST_EXCLUSION_RUNNER = r"""
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const { VitestTestRunner } = await import(pathToFileURL(process.env.PLUGINBENCH_VITEST_RUNNERS).href);
const selector = JSON.parse(fs.readFileSync(process.env.PLUGINBENCH_EXCLUDED_CASE, 'utf8'));
const tests = suite => suite.tasks.flatMap(task => task.type === 'test' ? [task] : tests(task));
const title = test => {
  const ancestors = [];
  for (let suite = test.suite; suite; suite = suite.suite) ancestors.push(suite.name);
  return [...ancestors.reverse(), ...(test.name ? [test.name] : [])].join(' ');
};
export default class ExclusionRunner extends VitestTestRunner {
  onCollected(files) {
    for (const file of files) {
      const relative = path.relative(fs.realpathSync(process.env.PLUGINBENCH_WORKSPACE), fs.realpathSync(file.filepath)).split(path.sep).join('/');
      if (relative !== selector.file) continue;
      const matches = tests(file).filter(test => title(test) === selector.title);
      const selected = matches[(selector.native_occurrence || 1) - 1];
      if (!selected || !['run', 'queued'].includes(selected.mode)) throw new Error('excluded native case is not runnable');
      selected.mode = 'skip';
      fs.writeFileSync(process.env.PLUGINBENCH_EXCLUSION_RECEIPT, JSON.stringify(selector));
    }
  }
}
"""

VITEST_EXCLUSION_LAUNCHER = r"""
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const { startVitest } = await import(pathToFileURL(input.module).href);
const context = await startVitest('test', input.files, input.options);
if (!context) throw new Error('native Vitest did not start');
for (const error of context.state.getUnhandledErrors().slice(0, 3)) {
  console.error('native unhandled error: ' + String(error.message ?? error).split('\n')[0].slice(0, 240));
}
await context.close();
"""

VITEST_ERROR_REPORTER = r"""
import fs from 'node:fs';
export default class NativeErrorReporter {
  onTestRunEnd(_modules, errors, reason) {
    fs.writeFileSync(process.env.PLUGINBENCH_NATIVE_ERRORS, JSON.stringify({ errors, reason }));
  }
}
"""

MOCHA_EXCLUSION_HOOK = r"""
const fs = require('node:fs');
const path = require('node:path');
const selector = JSON.parse(fs.readFileSync(process.env.PLUGINBENCH_EXCLUDED_CASE, 'utf8'));
exports.mochaHooks = {
  beforeAll() {
    const matches = [];
    this.test.parent.eachTest(test => {
      const relative = path.relative(fs.realpathSync(process.env.PLUGINBENCH_WORKSPACE), fs.realpathSync(test.file)).split(path.sep).join('/');
      if (relative === selector.file && test.fullTitle() === selector.title) matches.push(test);
    });
    const selected = matches[(selector.native_occurrence || 1) - 1];
    if (!selected || selected.isPending()) throw new Error('excluded native case is not runnable');
    selected.pending = true;
    fs.writeFileSync(process.env.PLUGINBENCH_EXCLUSION_RECEIPT, JSON.stringify(selector));
  },
};
"""


CLOCK_PRELOAD = r"""
(() => {
  const NativeDate = globalThis.Date;
  let next = 0;
  const read = () => next++;
  function SteppedDate(...args) {
    if (!new.target) return new NativeDate(read()).toString();
    return Reflect.construct(NativeDate, args.length ? args : [read()], new.target);
  }
  Object.setPrototypeOf(SteppedDate, NativeDate);
  Object.defineProperties(SteppedDate, {
    name: { value: 'Date', configurable: true },
    length: { value: 7, configurable: true },
    prototype: { value: NativeDate.prototype, writable: false },
    now: { value: read, writable: true, configurable: true },
    parse: Object.getOwnPropertyDescriptor(NativeDate, 'parse'),
    UTC: Object.getOwnPropertyDescriptor(NativeDate, 'UTC'),
  });
  globalThis.Date = SteppedDate;
})();
"""
CLOCK_POLICY: ClockPolicy = {
    "policy": "stepped-date-v1",
    "epoch_ms": 0,
    "step_ms": 1,
    "installation": "node-require-before-mocha",
    "implementation_sha256": hashlib.sha256(CLOCK_PRELOAD.encode()).hexdigest(),
}


def validated_clock(config: Configuration, framework: str) -> ClockPolicy | None:
    if "clock" not in config:
        return None
    policy = config["clock"]
    if (
        framework != "mocha"
        or config["provider"] != "istanbul/mocha"
        or not isinstance(policy, dict)
        or policy != CLOCK_POLICY
        or type(policy.get("epoch_ms")) is not int
        or type(policy.get("step_ms")) is not int
    ):
        raise ValueError(
            "unsupported coverage clock: requires the pinned stepped-date-v1 Mocha policy"
        )
    return CLOCK_POLICY.copy()


def digest(root: Path) -> str:
    value = inspect_snapshot(root)["digest"]
    assert isinstance(value, str)
    return value


def copy_workspace(source: Path, destination: Path) -> str:
    """Copy independent writable files, using Darwin's copy-on-write when available."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    if sys.platform == "darwin":
        # cp -c falls back to copyfile across filesystems; -P preserves symlinks.
        try:
            copied = subprocess.run(
                ["/bin/cp", "-cRpP", str(source), str(destination)],
                capture_output=True,
                text=True,
            )
        except OSError:
            copied = None
        if copied is not None and copied.returncode == 0:
            return "darwin-clonefile-or-copyfile"
        if destination.exists():
            shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)
    return "ordinary-independent-copy"


def matched(root: Path, scope: list[str]) -> list[str]:
    files = inspect_snapshot(root)["files"]
    assert isinstance(files, dict)
    return sorted(path for path in files if any(fnmatchcase(path, pattern) for pattern in scope))


def position(location: Location) -> str:
    return ":".join(
        str(location[edge][coordinate])
        for edge in ("start", "end")
        for coordinate in ("line", "column")
    )


def select_tests(
    *, submitted: Path, before: Path, config: Configuration
) -> tuple[list[str], list[str], str]:
    eligible = [
        name
        for name in matched(submitted, config["test_scope"])
        if name not in config.get("excluded_test_files", [])
    ]
    changed = [
        name
        for name in eligible
        if not (before / name).exists()
        or (before / name).read_bytes() != (submitted / name).read_bytes()
    ]
    reference = config.get("reference_test_files")
    if reference is None:
        return eligible, changed, "all-declared-visible-test-files-v1"
    if (
        not isinstance(reference, list)
        or not reference
        or any(not isinstance(name, str) or name not in eligible for name in reference)
        or len(set(reference)) != len(reference)
    ):
        raise ValueError("fixed reference test files must be unique existing eligible paths")
    return (
        sorted(set(reference) | set(changed)),
        changed,
        "fixed-reference-plus-all-changed-test-files-v1",
    )


def normalize(
    report: dict[str, IstanbulFile], workspace: Path, scope: list[str] | None = None
) -> CoverageRun:
    scope = scope or ["src/**"]
    result: CoverageRun = {
        "files_measured": [],
        "branches": {"total": [], "covered": []},
        "functions": {"total": [], "covered": []},
        "test_population": "submitted visible tests only; no held-out tests",
        "istanbul": {},
        "coordinate_method": "source-location-and-arm-v1",
    }
    for filename, row in sorted(report.items()):
        path = Path(filename).resolve().relative_to(workspace.resolve()).as_posix()
        if not any(fnmatchcase(path, pattern) for pattern in scope):
            raise ValueError(f"coverage path outside declared scope: {path}")
        result["files_measured"].append(path)
        result["istanbul"][path] = {**row, "path": path}
        for identity, counts in row["b"].items():
            branch = row["branchMap"][identity]
            for index, count in enumerate(counts):
                location = branch["locations"][index]
                anchor = (
                    f"{position(branch['loc'])}:implicit-arm:{index}"
                    if location == {"start": {}, "end": {}}
                    else position(location)
                )
                coordinate = (
                    f"{path}:branch:{branch['type']}:{position(branch['loc'])}:arm:{index}:{anchor}"
                )
                result["branches"]["total"].append(coordinate)
                if count > 0:
                    result["branches"]["covered"].append(coordinate)
        for identity, count in row["f"].items():
            function = row["fnMap"][identity]
            coordinate = f"{path}:function:{position(function['loc'])}"
            result["functions"]["total"].append(coordinate)
            if count > 0:
                result["functions"]["covered"].append(coordinate)
    for kind in KINDS:
        if len(set(result[kind]["total"])) != len(result[kind]["total"]):
            raise ValueError(f"ambiguous {kind} source locations")
        for population in POPULATIONS:
            result[kind][population].sort()
    return result


def failure_summary(
    report: NativeTests, framework: str, native_errors: NativeErrors | None = None
) -> str:
    """Retain bounded test failures before the temporary native report is removed."""
    failures: list[str] = []
    for error in native_errors["errors"] if native_errors is not None else []:
        message = error.get("message", error.get("name", "unhandled error"))
        context = f" in {error['VITEST_TEST_NAME'][:160]}" if error.get("VITEST_TEST_NAME") else ""
        failures.append(f"native unhandled error{context}: {message.split('\n', 1)[0][:240]}")
    if framework == "vitest":
        for file in report.get("testResults", []):
            for case in file["assertionResults"]:
                if case["status"] == "failed":
                    messages = case.get("failureMessages", [])
                    message = messages[0] if messages else "failed"
                    failures.append(f"{case['fullName'][:160]}: {message.split('\n', 1)[0][:240]}")
            if file.get("message"):
                failures.append(file["message"].split("\n", 1)[0][:240])
    else:
        for case in report.get("failures", []):
            message = case.get("err", {}).get("message", "failed")
            failures.append(f"{case['fullTitle'][:160]}: {message.split('\n', 1)[0][:240]}")
    if not failures and framework == "vitest":
        failures.append(
            f"native report success={report.get('success')}; "
            f"failed tests={report.get('numFailedTests')}, "
            f"failed suites={report.get('numFailedTestSuites')}, "
            f"passed tests={report.get('numPassedTests')}"
        )
    return "; ".join(failures[:3])


def test_rows(report: NativeTests, framework: str, workspace: Path) -> list[TestRow]:
    rows: list[TestRow] = []
    if framework == "vitest":
        for file in report["testResults"]:
            for case in file["assertionResults"]:
                rows.append(
                    {"file": file["name"], "title": case["fullName"], "status": case["status"]}
                )
    else:
        for case in report["tests"]:
            rows.append(
                {
                    "file": case["file"],
                    "title": case["fullTitle"],
                    "status": "failed"
                    if case in report["failures"]
                    else "pending"
                    if case in report["pending"]
                    else "passed"
                    if case in report["passes"]
                    else "unknown",
                }
            )
    for row in rows:
        row["file"] = Path(row["file"]).resolve().relative_to(workspace.resolve()).as_posix()
        row["id"] = row["file"] + "::" + row["title"]
        if row["status"] in ("skipped", "todo", "disabled"):
            row["status"] = "pending"
    counts = Counter(row["id"] for row in rows)
    ordinals: Counter[str] = Counter()
    for row in rows:
        identity = row["id"]
        ordinals[identity] += 1
        if counts[identity] > 1:
            row["native_occurrence"] = ordinals[identity]
            row["id"] += f"::native-occurrence:{ordinals[identity]}"
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("ambiguous native test occurrence identities")
    return sorted(rows, key=lambda row: row["id"])


def validate_run(
    reference: CoverageRun, observed: CoverageRun, excluded: str | None = None
) -> None:
    for kind in KINDS:
        if reference[kind]["total"] != observed[kind]["total"]:
            raise ValueError(f"{kind} denominator changed between fresh runs")
    expected = {row["id"] for row in reference["tests"] if row["status"] == "passed"}
    if excluded:
        expected.remove(excluded)
    actual = {row["id"] for row in observed["tests"] if row["status"] == "passed"}
    if actual != expected or any(row["status"] == "failed" for row in observed["tests"]):
        raise ValueError("fresh run passing test population differs from reference")
    pending = {row["id"] for row in reference["tests"] if row["status"] == "pending"}
    observed_pending = {row["id"] for row in observed["tests"] if row["status"] == "pending"}
    if observed_pending - ({excluded} if excluded else set()) != pending:
        raise ValueError("fresh run pending test population differs from reference")
    if any(row["status"] not in ("passed", "pending") for row in observed["tests"]):
        raise ValueError("unsupported reported test status")
    if not set(observed["branches"]["covered"]) <= set(reference["branches"]["covered"]):
        raise ValueError("fresh exclusion run covers branches absent from reference")


def validate_repeat(reference: CoverageRun, repeat: CoverageRun) -> None:
    validate_run(reference, repeat)
    for kind in KINDS:
        if reference[kind]["covered"] != repeat[kind]["covered"]:
            raise ValueError(f"reference {kind} coverage is not repeatable")


def contribution(
    reference: CoverageRun, repeat: CoverageRun, removals: list[CoverageRun], changed: list[str]
) -> dict[str, object]:
    validate_repeat(reference, repeat)
    candidates = [
        row for row in reference["tests"] if row["file"] in changed and row["status"] == "passed"
    ]
    if [row["excluded_id"] for row in removals] != [row["id"] for row in candidates]:
        raise ValueError("missing or reordered candidate exclusion runs")
    zero = []
    runs = []
    for removal in removals:
        validate_run(reference, removal, removal["excluded_id"])
        unique = sorted(set(reference["branches"]["covered"]) - set(removal["branches"]["covered"]))
        if not unique:
            zero.append(removal["excluded_id"])
        runs.append(
            {
                "excluded_id": removal["excluded_id"],
                "executed_ids": [
                    row["id"] for row in removal["tests"] if row["status"] == "passed"
                ],
                "passing_ids": [row["id"] for row in removal["tests"] if row["status"] == "passed"],
                "covered_branches": removal["branches"]["covered"],
                "unique_branches": unique,
                "exit_code": removal["execution"]["exit_code"],
                "test_statuses": {row["id"]: row["status"] for row in removal["tests"]},
            }
        )
    passing = [row["id"] for row in reference["tests"] if row["status"] == "passed"]
    duplicate_groups: dict[str, list[TestRow]] = {}
    for row in reference["tests"]:
        if "native_occurrence" in row:
            identity = row["file"] + "::" + row["title"]
            duplicate_groups.setdefault(identity, []).append(row)
    return {
        "method": "leave-one-out-branches-v1",
        "label": "zero marginal branch contribution",
        "limitation": "coverage proxy among declared visible passing tests; not semantic redundancy",
        "reference_ids": [row["id"] for row in reference["tests"]],
        "reference_statuses": {row["id"]: row["status"] for row in reference["tests"]},
        "duplicate_reference_groups": {
            identity: {
                "record_ids": [row["id"] for row in rows],
                "aggregation": "worst-status",
                "worst_status": "failed"
                if any(row["status"] == "failed" for row in rows)
                else "pending"
                if any(row["status"] == "pending" for row in rows)
                else "passed",
            }
            for identity, rows in duplicate_groups.items()
        },
        "executed_ids": passing,
        "passing_ids": passing,
        "candidate_ids": [row["id"] for row in candidates],
        "changed_test_files": changed,
        "selection": "all runnable cases in added/modified visible test files",
        "removal_runs": runs,
        "zero_marginal_ids": zero,
        "repeatability": {
            "fresh_reference_runs": 2,
            "same_denominator": True,
            "same_covered_branches": True,
            "same_covered_functions": True,
        },
    }


def collect(
    *,
    submitted: Path,
    before: Path,
    payload: Payload,
    config: Configuration,
    dependency_digest: Callable[[Path], str],
    diagnostics: Path | None = None,
) -> CoverageRun:
    original = digest(submitted)
    baseline = digest(before)
    if config.get("before_digest", baseline) != baseline:
        raise ValueError("baseline source digest mismatch")
    framework = payload.get("framework", "vitest")
    clock = validated_clock(config, framework)
    cwd = payload.get("test_cwd", ".")
    source_scope = config["source_scope"]
    test_scope = config["test_scope"]
    for scope in (source_scope, test_scope):
        if (
            not isinstance(scope, list)
            or not scope
            or any(
                not isinstance(pattern, str)
                or not pattern
                or pattern.startswith("/")
                or ".." in pattern.split("/")
                for pattern in scope
            )
            or len(set(scope)) != len(scope)
        ):
            raise ValueError("coverage scopes must be nonempty unique relative glob lists")
    excluded_files = config.get("excluded_test_files", [])
    test_files, changed, selection_policy = select_tests(
        submitted=submitted, before=before, config=config
    )
    source_files = [
        name
        for name in matched(submitted, source_scope)
        if Path(name).suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
        and not name.endswith((".d.ts", ".d.mts", ".d.cts"))
    ]
    if not test_files:
        raise ValueError("declared visible test population is empty")
    trees: list[DependencyTree] = [
        {
            "relative": "node_modules",
            "path": payload["dependencies"],
            "sha256": payload["dependency_sha256"],
        },
        *payload.get("extra_dependency_trees", []),
    ]
    for tree in trees:
        if dependency_digest(Path(tree["path"])) != tree["sha256"]:
            raise ValueError(f"prepared dependency cache changed: {tree['relative']}")
    observer = config.get("observer")
    if observer and dependency_digest(Path(observer["path"])) != observer["sha256"]:
        raise ValueError("observer dependency cache changed")
    framework_package = Path(payload["dependencies"]) / framework / "package.json"
    framework_version = json.loads(framework_package.read_text())["version"]
    provider_root = Path(observer["path"] if observer else payload["dependencies"])
    coverage_provider = "istanbul" if config["provider"] == "vitest/istanbul" else "v8"
    provider_package = (
        f"@vitest/coverage-{coverage_provider}"
        if framework == "vitest"
        else "istanbul-lib-instrument"
    )
    provider_version = json.loads((provider_root / provider_package / "package.json").read_text())[
        "version"
    ]
    if provider_version != config["version"] or (
        framework == "vitest" and framework_version != provider_version
    ):
        raise ValueError("pinned coverage provider/framework version mismatch")
    if config.get("framework_version", framework_version) != framework_version:
        raise ValueError("pinned test framework version mismatch")
    try:
        with tempfile.TemporaryDirectory(prefix="pluginbench-visible-coverage-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            shutil.copytree(
                submitted, workspace, ignore=shutil.ignore_patterns("node_modules", ".git")
            )
            # Preserve every submitted visible test; restore only trusted runner assets.
            restored = []
            visible_files = matched(submitted, test_scope)
            for name, encoded in payload["trusted_files"].items():
                if name in visible_files and name not in config.get("runner_files", []):
                    continue
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(encoded))
                restored.append(name)
            for tree in trees:
                shutil.copytree(Path(tree["path"]), workspace / tree["relative"], symlinks=True)
            if observer and framework == "vitest":
                target = workspace / f"node_modules/@vitest/coverage-{coverage_provider}"
                if target.exists() or target.is_symlink():
                    target.unlink() if target.is_symlink() else shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(
                    Path(observer["path"]) / f"@vitest/coverage-{coverage_provider}",
                    target_is_directory=True,
                )
            work = workspace / cwd
            relative_tests = [os.path.relpath(workspace / name, work) for name in test_files]
            relative_scope = [
                os.path.relpath(workspace / pattern, work) for pattern in source_scope
            ]
            executions: list[Execution] = []
            preload = root / "observer-preload.cjs"
            if framework == "mocha":
                sources = [
                    name
                    for name in matched(submitted, source_scope)
                    if Path(name).suffix
                    in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
                ]
                if any(Path(name).suffix != ".js" for name in sources):
                    raise ValueError(
                        "Istanbul Mocha adapter requires an explicit JavaScript-only source scope"
                    )
                manifest = root / "source-files.json"
                manifest.write_text(json.dumps(sources))
                instrument = root / "instrument.cjs"
                instrument.write_text(INSTRUMENT)
                preload.write_text((CLOCK_PRELOAD + "\n" if clock else "") + PRELOAD)

            template = workspace

            def run_once(name: str, excluded: TestRow | None = None) -> CoverageRun:
                reports = root / name
                reports.mkdir()
                coverage_directory = reports / "coverage"
                coverage_directory.mkdir()
                workspace = reports / "workspace"
                copy_started = time.monotonic()
                copy_method = copy_workspace(template, workspace)
                copy_seconds = round(time.monotonic() - copy_started, 6)
                work = workspace / cwd
                initial = reports / "initial-coverage.json"
                if framework == "mocha":
                    assert observer is not None
                    instrumented = subprocess.run(
                        [
                            "node",
                            str(instrument),
                            observer["path"],
                            str(workspace),
                            str(manifest),
                            str(initial),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if instrumented.returncode:
                        raise ValueError(f"source instrumentation failed: {instrumented.stderr}")
                measured_hashes = {
                    name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
                    for name in [*source_files, *test_files]
                }
                test_report = reports / "tests.json"
                native_error_report = reports / "native-errors.json"
                excluded_case = reports / "excluded-case.json"
                exclusion_receipt = reports / "exclusion-receipt.json"
                pattern = (
                    "^(?!"
                    + "|".join(
                        f"(?:{re.escape(title)})$"
                        for title in config.get("excluded_full_titles", [])
                    )
                    + ").*$"
                    if config.get("excluded_full_titles")
                    else None
                )
                if excluded:
                    excluded_case.write_text(json.dumps(excluded))
                if framework == "vitest":
                    native_error_reporter = reports / "native-error-reporter.mjs"
                    native_error_reporter.write_text(VITEST_ERROR_REPORTER)
                    command = [
                        "node",
                        str(workspace / "node_modules/vitest/vitest.mjs"),
                        "run",
                        *relative_tests,
                        "--coverage",
                        f"--coverage.provider={coverage_provider}",
                        "--coverage.reporter=json",
                        f"--coverage.reportsDirectory={coverage_directory}",
                        "--reporter=json",
                        f"--reporter={native_error_reporter}",
                        f"--outputFile={test_report}",
                        "--testTimeout=30000",
                        "--no-file-parallelism",
                        "--maxWorkers=1",
                    ]
                    if config.get("disable_coverage_thresholds", False):
                        command.extend(
                            f"--coverage.thresholds.{metric}=0"
                            for metric in ("branches", "functions", "lines", "statements")
                        )
                    if config.get("disable_typechecking", False):
                        command.append("--typecheck.enabled=false")
                    command.extend(f"--coverage.include={pattern}" for pattern in relative_scope)
                    if pattern:
                        command.extend(["--testNamePattern", pattern])
                    if excluded:
                        # The native CLI has no file-scoped case filter or --runner flag.
                        # Its public startVitest API accepts an observer-owned runner.
                        runner = reports / "exclusion-runner.mjs"
                        runner.write_text(VITEST_EXCLUSION_RUNNER)
                        launcher = reports / "exclusion-launcher.mjs"
                        launcher.write_text(VITEST_EXCLUSION_LAUNCHER)
                        options = reports / "exclusion-options.json"
                        options.write_text(
                            json.dumps(
                                {
                                    "module": str(workspace / "node_modules/vitest/dist/node.js"),
                                    "files": relative_tests,
                                    "options": {
                                        "run": True,
                                        "runner": str(runner),
                                        "coverage": {
                                            "enabled": True,
                                            "provider": coverage_provider,
                                            "reporter": ["json"],
                                            "reportsDirectory": str(coverage_directory),
                                            "include": relative_scope,
                                            **(
                                                {
                                                    "thresholds": dict.fromkeys(
                                                        (
                                                            "branches",
                                                            "functions",
                                                            "lines",
                                                            "statements",
                                                        ),
                                                        0,
                                                    )
                                                }
                                                if config.get("disable_coverage_thresholds", False)
                                                else {}
                                            ),
                                        },
                                        "reporters": ["json", str(native_error_reporter)],
                                        "outputFile": str(test_report),
                                        "testTimeout": 30000,
                                        "fileParallelism": False,
                                        "maxWorkers": 1,
                                        **(
                                            {"typecheck": {"enabled": False}}
                                            if config.get("disable_typechecking", False)
                                            else {}
                                        ),
                                        **({"testNamePattern": pattern} if pattern else {}),
                                    },
                                }
                            )
                        )
                        command = ["node", str(launcher), str(options)]
                else:
                    command = [
                        "node",
                        "--require",
                        str(preload),
                        str(workspace / "node_modules/mocha/bin/mocha.js"),
                        *relative_tests,
                        "--reporter=json",
                        f"--reporter-option=output={test_report}",
                    ]
                    if pattern:
                        command.extend(["--grep", pattern])
                    if excluded:
                        hook = reports / "exclusion-hook.cjs"
                        hook.write_text(MOCHA_EXCLUSION_HOOK)
                        command.extend(["--require", str(hook)])
                started = time.monotonic()
                timed_out = False
                try:
                    execution = subprocess.run(
                        command,
                        cwd=work,
                        capture_output=True,
                        text=True,
                        timeout=config.get("run_timeout_seconds", 600),
                        env={
                            **os.environ,
                            "CI": "1",
                            "npm_config_userconfig": "/dev/null",
                            "PLUGINBENCH_OBSERVER": observer["path"] if observer else "",
                            "PLUGINBENCH_INITIAL": str(initial),
                            "PLUGINBENCH_COVERAGE": str(coverage_directory / "coverage-final.json"),
                            "PLUGINBENCH_NATIVE_ERRORS": str(native_error_report),
                            **(
                                {
                                    "PLUGINBENCH_WORKSPACE": str(workspace),
                                    "PLUGINBENCH_EXCLUDED_CASE": str(excluded_case),
                                    "PLUGINBENCH_EXCLUSION_RECEIPT": str(exclusion_receipt),
                                    "PLUGINBENCH_VITEST_RUNNERS": str(
                                        workspace / "node_modules/vitest/dist/runners.js"
                                    ),
                                }
                                if excluded
                                else {}
                            ),
                        },
                    )
                except subprocess.TimeoutExpired as error:
                    timed_out = True
                    execution = subprocess.CompletedProcess(
                        command,
                        124,
                        error.stdout.decode(errors="replace")
                        if isinstance(error.stdout, bytes)
                        else error.stdout or "",
                        error.stderr.decode(errors="replace")
                        if isinstance(error.stderr, bytes)
                        else error.stderr or "",
                    )
                receipt: Execution = {
                    "name": name,
                    "command": command,
                    "exit_code": execution.returncode,
                    "stdout": execution.stdout,
                    "stderr": execution.stderr,
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "workspace_copy": {"method": copy_method, "elapsed_seconds": copy_seconds},
                }
                if timed_out:
                    receipt["timed_out"] = True
                executions.append(receipt)
                if execution.returncode and diagnostics is not None:
                    destination = diagnostics / name
                    destination.mkdir(parents=True, exist_ok=False)
                    (destination / "execution.json").write_text(
                        json.dumps(receipt, indent=2) + "\n"
                    )
                    for report in (
                        test_report,
                        native_error_report,
                        excluded_case,
                        exclusion_receipt,
                    ):
                        if report.is_file():
                            shutil.copyfile(report, destination / report.name)
                if any(
                    not (workspace / name).is_file()
                    or hashlib.sha256((workspace / name).read_bytes()).hexdigest() != expected
                    for name, expected in measured_hashes.items()
                ):
                    raise ValueError(
                        "measured source/test bytes changed during visible test execution"
                    )
                if excluded:
                    if exclusion_receipt.is_symlink() or exclusion_receipt.exists():
                        if (
                            exclusion_receipt.is_symlink()
                            or not exclusion_receipt.is_file()
                            or json.loads(exclusion_receipt.read_text()) != excluded
                        ):
                            raise ValueError("native case exclusion receipt missing or mismatched")
                    elif not timed_out:
                        raise ValueError("native case exclusion receipt missing or mismatched")
                if execution.returncode:
                    failures = failure_summary(
                        json.loads(test_report.read_text()) if test_report.is_file() else {},
                        framework,
                        json.loads(native_error_report.read_text())
                        if native_error_report.is_file()
                        else None,
                    )
                    raise MeasurementUnavailable(
                        f"visible coverage run {name} {'timed out' if timed_out else 'failed'}: {failures}\n{execution.stdout}\n{execution.stderr}"
                        + (
                            f"\nNative diagnostic receipts: {diagnostics / name}"
                            if diagnostics
                            else ""
                        )
                    )
                result = normalize(
                    json.loads((coverage_directory / "coverage-final.json").read_text()),
                    workspace,
                    source_scope,
                )
                result["tests"] = test_rows(
                    json.loads(test_report.read_text()), framework, workspace
                )
                if any(row["file"] not in test_files for row in result["tests"]):
                    raise ValueError("reported test outside declared visible population")
                result["execution"] = receipt
                shutil.rmtree(workspace)
                if any(row["status"] == "failed" for row in result["tests"]):
                    raise ValueError("visible test reference includes failures")
                if excluded:
                    selected = [row for row in result["tests"] if row["id"] == excluded["id"]]
                    if len(selected) != 1 or selected[0]["status"] != "pending":
                        raise ValueError("excluded native case was not retained as pending")
                    result["excluded_id"] = excluded["id"]
                return result

            reference = run_once("reference")
            if reference["files_measured"] != source_files:
                raise ValueError("coverage file population differs from declared source inventory")
            repeat = run_once("reference-repeat")
            validate_repeat(reference, repeat)
            if config.get("measure_test_overlap", False):
                candidates = [
                    row
                    for row in reference["tests"]
                    if row["file"] in changed and row["status"] == "passed"
                ]
                removals = [
                    run_once(f"remove-{index}", row) for index, row in enumerate(candidates)
                ]
                reference["contribution"] = contribution(reference, repeat, removals, changed)
                reference["contribution"]["baseline_digest"] = baseline
                reference["contribution"]["before_digest"] = baseline
                reference["contribution"]["source_digest"] = original
            reference.update(
                {
                    "method": "visible-source-coverage-v2",
                    "source_scope": source_scope,
                    "test_scope": test_scope,
                    "source_digest": original,
                    "before_digest": baseline,
                    "test_files": test_files,
                    "selected_test_files": test_files,
                    "reference_test_files": config.get("reference_test_files"),
                    "selection_policy": selection_policy,
                    "excluded_test_files": excluded_files,
                    "excluded_full_titles": config.get("excluded_full_titles", []),
                    "runner_files_restored": restored,
                    "executions": executions,
                    "tool": config["provider"],
                    "tool_version": config["version"],
                    "framework_version": framework_version,
                    "observer": observer,
                    "dependency_trees": trees,
                    "adapter_sha256": ADAPTER_SHA256,
                    "configuration_digest": hashlib.sha256(
                        json.dumps(config, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
            if config.get("reference_test_files") is not None:
                reference["test_population"] = (
                    "submitted visible task corpus only; no held-out tests"
                )
            if clock is not None:
                reference["clock"] = clock
    finally:
        if digest(submitted) != original or digest(before) != baseline:
            raise ValueError("source changed during coverage collection")
        for tree in trees:
            if dependency_digest(Path(tree["path"])) != tree["sha256"]:
                raise ValueError(
                    f"prepared dependency cache changed during collection: {tree['relative']}"
                )
        if observer and dependency_digest(Path(observer["path"])) != observer["sha256"]:
            raise ValueError("observer dependency cache changed during collection")
    return reference


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verifier", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    arguments = parser.parse_args()
    spec = importlib.util.spec_from_file_location("prepared_verifier", arguments.verifier)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = bytecode_setting
    payload = module.PAYLOAD
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    if node != payload["node"]:
        raise ValueError(f"expected Node {payload['node']}, observed {node}")
    config = json.loads(arguments.config.read_text())
    before = arguments.before or Path(config["baseline_snapshot"])
    result = collect(
        submitted=Path.cwd(),
        before=before,
        payload=payload,
        config=config,
        dependency_digest=module.dependency_digest,
        diagnostics=arguments.output.parent / "native-failures",
    )
    result["verifier_sha256"] = hashlib.sha256(arguments.verifier.read_bytes()).hexdigest()
    result["config_sha256"] = hashlib.sha256(arguments.config.read_bytes()).hexdigest()
    result["node"] = node
    arguments.output.write_text(json.dumps(result, indent=2) + "\n")


def cli() -> int:
    """coverage-exit-v1: 3 means unavailable; all integrity/protocol errors use 2."""
    try:
        main()
    except MeasurementUnavailable as error:
        print(str(error), file=sys.stderr)
        return 3
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
