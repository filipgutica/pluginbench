from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from types import ModuleType

import pytest


def template(name: str) -> ModuleType:
    path = Path(__file__).parents[1] / f"src/pluginbench/task_data/deepswe/{name}.py"
    spec = importlib.util.spec_from_file_location(f"real_coding_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pinned_suite_rejects_modified_selected_task_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(
        str(Path(__file__).parents[1] / "src/pluginbench/task_data/deepswe")
    )
    module = template("prepare")
    calls = []

    def git(argv: list[str], cwd: Path | None = None) -> bytes:
        calls.append((argv, cwd))
        return (
            module.SUITE_SHA.encode()
            if "rev-parse" in argv
            else b" M tasks/ofetch-per-origin-circuit-breaker/tests/test.patch\n"
        )

    monkeypatch.setattr(module, "command", git)
    with pytest.raises(ValueError, match="selected DeepSWE task assets are modified"):
        module.validate_suite(tmp_path, "ofetch-per-origin-circuit-breaker")
    assert calls[-1][0][-1] == "tasks/ofetch-per-origin-circuit-breaker"


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path, Path]:
    module = template("verify")
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    workspace = tmp_path / "submission"
    workspace.mkdir()
    (workspace / "source.ts").write_text("export const untouched = 1;\n")
    monkeypatch.setattr(
        module,
        "PAYLOAD",
        {
            "node": "v22.20.0",
            "dependencies": str(dependencies),
            "dependency_sha256": module.dependency_digest(dependencies),
            "trusted_files": {},
            "test_patch": "",
            "config": {"f2p_node_ids": ["feature: works"], "p2p_node_ids": ["base: retained"]},
        },
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "v22.20.0\n")
    return module, workspace, dependencies


@pytest.mark.parametrize(
    "feature", ["", '<testcase classname="feature" name="works"><skipped /></testcase>']
)
def test_missing_or_skipped_required_tests_do_not_pass(
    verifier: tuple[ModuleType, Path, Path], monkeypatch: pytest.MonkeyPatch, feature: str
) -> None:
    module, workspace, _ = verifier

    def execute(
        command: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        output = next((arg for arg in command if arg.startswith("--outputFile=")), None)
        if output:
            cases = (
                feature
                if "circuit-breaker" in " ".join(command)
                else '<testcase classname="base" name="retained" />'
            )
            Path(output.split("=", 1)[1]).write_text(f"<testsuite>{cases}</testsuite>")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "run", execute)
    result = module.verify()
    assert result["pass"] is False
    assert result["groups"]["f2p_node_ids"]["failed"] == ["feature: works"]
    assert (workspace / "source.ts").read_text() == "export const untouched = 1;\n"
    assert sorted(path.name for path in workspace.iterdir()) == ["source.ts"]


def test_changed_dependency_cache_is_rejected(
    verifier: tuple[ModuleType, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, dependencies = verifier
    (dependencies / "modified.js").write_text("changed")
    monkeypatch.setattr(
        module, "run", lambda command, cwd, env: subprocess.CompletedProcess(command, 0, "", "")
    )
    with pytest.raises(ValueError, match="dependency cache changed"):
        module.verify()


def test_duplicate_test_identity_preserves_worst_status(
    verifier: tuple[ModuleType, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, _ = verifier

    def execute(
        command: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        output = next((arg for arg in command if arg.startswith("--outputFile=")), None)
        if output:
            failure = (
                '<failure message="regression" />' if "circuit-breaker" in " ".join(command) else ""
            )
            Path(output.split("=", 1)[1]).write_text(
                f'<testsuite><testcase classname="base" name="retained">{failure}</testcase></testsuite>'
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module, "run", execute)
    report = module.verify()
    assert report["pass"] is False
    assert report["groups"]["p2p_node_ids"]["failed"] == ["base: retained"]
    assert report["duplicate_rows"] == {"base: retained": 2}


def test_coverage_coordinates_preserve_uncovered_obligations_and_reject_escape(
    tmp_path: Path,
) -> None:
    module = template("coverage")
    source = tmp_path / "src/example.ts"
    row = {
        "path": str(source),
        "b": {"0": [1, 0]},
        "branchMap": {
            "0": {
                "type": "if",
                "loc": {"start": {"line": 2, "column": 0}, "end": {"line": 2, "column": 8}},
                "locations": [
                    {"start": {"line": 2, "column": 0}, "end": {"line": 2, "column": 4}},
                    {"start": {"line": 2, "column": 5}, "end": {"line": 2, "column": 8}},
                ],
            }
        },
        "f": {"0": 0},
        "fnMap": {
            "0": {"loc": {"start": {"line": 1, "column": 0}, "end": {"line": 3, "column": 1}}}
        },
    }
    report = module.normalize({str(source): row}, tmp_path)
    assert report["branches"] == {
        "total": [
            "src/example.ts:branch:if:2:0:2:8:arm:0:2:0:2:4",
            "src/example.ts:branch:if:2:0:2:8:arm:1:2:5:2:8",
        ],
        "covered": ["src/example.ts:branch:if:2:0:2:8:arm:0:2:0:2:4"],
    }
    assert report["functions"] == {"total": ["src/example.ts:function:1:0:3:1"], "covered": []}
    assert report["files_measured"] == ["src/example.ts"]
    with pytest.raises(ValueError):
        module.normalize({str(tmp_path.parent / "outside.ts"): row}, tmp_path)


def test_mocha_pending_and_duplicate_failure_are_not_passes(tmp_path: Path) -> None:
    module = template("verify")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "tests": [
                    {"fullTitle": name} for name in ["works", "duplicated", "duplicated", "pending"]
                ],
                "passes": [{"fullTitle": name} for name in ["works", "duplicated"]],
                "failures": [{"fullTitle": "duplicated", "err": {"message": "fails"}}],
                "pending": [{"fullTitle": "pending"}],
            }
        )
    )
    rows = module.reported_tests(report, "mocha")
    assert [(name, passed) for name, passed, detail in rows] == [
        ("works", True),
        ("duplicated", False),
        ("duplicated", False),
        ("pending", False),
    ]
    assert rows[1][2] == {"message": "fails"}


def test_stepped_date_preserves_explicit_dates_and_native_timers() -> None:
    module = template("coverage")
    setup = """
const assert = require('node:assert/strict');
const NativeDate = Date;
const timers = Object.fromEntries(['setTimeout', 'clearTimeout', 'setInterval',
  'clearInterval', 'setImmediate', 'clearImmediate'].map(name => [name, globalThis[name]]));
const nativePerformance = performance.now;
const nativeHrtime = process.hrtime;
"""
    checks = """
assert.deepEqual([Date.now(), +new Date(), Date.now()], [0, 1, 2]);
const start = new Date();
const end = new Date();
assert.equal(end - start, 1);
assert.equal(+new Date(0), 0); // Explicit zero duration remains zero.
assert.equal(new Date(0) - new Date(0), 0);
for (const value of [0, -1, 12.5, null, undefined, NaN, 'invalid', '2020-02-03T04:05:06Z']) {
  assert.equal(+new Date(value), +new NativeDate(value));
}
const args = [2020, 1, 3, 4, 5, 6, 7];
assert.equal(+new Date(...args), +new NativeDate(...args));
assert.equal(Date.parse, NativeDate.parse);
assert.equal(Date.UTC, NativeDate.UTC);
assert.equal(Date.UTC(...args), NativeDate.UTC(...args));
assert.equal(Date.prototype, NativeDate.prototype);
class ChildDate extends Date {}
const child = new ChildDate('2020-02-03T04:05:06Z');
assert(child instanceof ChildDate && child instanceof Date && child instanceof NativeDate);
const inherited = new ChildDate();
assert.equal(+inherited, 5);
assert.equal(Object.prototype.toString.call(inherited), '[object Date]');
assert.equal(Date(), new NativeDate(6).toString());
assert.equal(Date('ignored'), new NativeDate(7).toString());
assert.equal(Date.now(), 8);
assert.equal(Date.name, 'Date');
assert.equal(Date.length, 7);
for (const [name, value] of Object.entries(timers)) assert.equal(globalThis[name], value);
assert.equal(performance.now, nativePerformance);
assert.equal(process.hrtime, nativeHrtime);
"""
    execution = subprocess.run(
        ["node", "-e", setup + module.CLOCK_PRELOAD + checks], capture_output=True, text=True
    )
    assert execution.returncode == 0, execution.stderr
    # The same explicit policy starts a fresh counter in each native process.
    repeat = subprocess.run(
        ["node", "-e", setup + module.CLOCK_PRELOAD + checks], capture_output=True, text=True
    )
    assert repeat.returncode == 0, repeat.stderr


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "stepped-date-v1",
        {},
        {"epoch_ms": False},
        {"step_ms": True},
        {"epoch_ms": 1},
        {"step_ms": 0},
        {"step_ms": 2},
        {"extra": True},
        {"implementation_sha256": "changed"},
        {"installation": "after-tests"},
    ],
)
def test_coverage_rejects_invalid_clock_policy_before_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: object
) -> None:
    module = template("coverage")
    fixture = tmp_path / "source"
    fixture.mkdir()
    policy: object
    if isinstance(invalid, dict) and invalid:
        updated = dict(module.CLOCK_POLICY)
        updated.update(invalid)
        policy = updated
    else:
        policy = invalid
    config = {"provider": "istanbul/mocha", "clock": policy}

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("invalid clock must be rejected before subprocess execution")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="unsupported coverage clock"):
        module.collect(
            submitted=fixture,
            before=fixture,
            payload={"framework": "mocha"},
            config=config,
            dependency_digest=lambda path: "unused",
        )


def test_clock_is_optional_mocha_only_and_preset_binds_implementation() -> None:
    module = template("coverage")
    assert module.validated_clock({"provider": "vitest/v8"}, "vitest") is None
    with pytest.raises(ValueError, match="unsupported coverage clock"):
        module.validated_clock({"provider": "vitest/v8", "clock": module.CLOCK_POLICY}, "vitest")
    with pytest.raises(ValueError, match="unsupported coverage clock"):
        module.validated_clock(
            {"provider": "istanbul/mocha", "clock": module.CLOCK_POLICY}, "other"
        )
    presets = json.loads(
        (
            Path(__file__).parents[1] / "src/pluginbench/task_data/deepswe/measurement-presets.json"
        ).read_text()
    )
    assert presets["testem-per-launcher-reports"]["coverage"]["clock"] == module.CLOCK_POLICY
    assert "clock" not in presets["ofetch-per-origin-circuit-breaker"]["coverage"]
    assert "clock" not in presets["happy-dom-abort-pending-body-reads"]["coverage"]


def test_stepped_date_survives_sinon_date_install_and_restore() -> None:
    module = template("coverage")
    sinon = (
        Path(__file__).parents[1]
        / "benchmarks/real-coding/generated/testem/dependencies/testem-per-launcher-reports/node_modules/sinon"
    )
    if not sinon.is_dir():
        pytest.skip("prepared Testem Sinon cache unavailable")
    checks = """
const assert = require('node:assert/strict');
const sinon = require(process.argv[1]);
const controlled = Date;
const before = Date.now();
const fake = sinon.useFakeTimers({ now: 12345, toFake: ['Date'] });
assert.equal(Date.now(), 12345);
assert.equal(+new Date(), 12345);
fake.restore();
assert.equal(Date, controlled);
assert(Date.now() > before);
const start = new Date();
assert(new Date() > start);
"""
    execution = subprocess.run(
        ["node", "-e", module.CLOCK_PRELOAD + checks, str(sinon)], capture_output=True, text=True
    )
    assert execution.returncode == 0, execution.stderr


@pytest.mark.parametrize("controlled", [False, True])
def test_mocha_process_clock_is_installed_before_visible_test_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controlled: bool
) -> None:
    module = template("coverage")
    submitted = tmp_path / "source"
    (submitted / "lib").mkdir(parents=True)
    (submitted / "tests").mkdir()
    (submitted / "lib/example.js").write_text("module.exports = 0;\n")
    (submitted / "tests/example_tests.js").write_text("// selected visible test\n")
    dependencies = tmp_path / "dependencies"
    (dependencies / "mocha/bin").mkdir(parents=True)
    (dependencies / "mocha/package.json").write_text('{"version":"11.8.0"}')
    # A tiny native runner observes the production adapter's actual Node --require
    # ordering; it never executes a real-task collection or changes shared caches.
    (dependencies / "mocha/bin/mocha.js").write_text("""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const controlled = process.env.CLOCK_EXPECTED === '1';
const start = new Date();
const end = new Date();
if (controlled) assert.equal(end - start, 1);
else assert(Date.now() > 1000000000000);
assert.equal(new Date(0) - new Date(0), 0);
const file = path.resolve(process.argv[2]);
const test = { file, fullTitle: 'visible test sees expected Date', status: 'passed' };
const output = process.argv.find(arg => arg.startsWith('--reporter-option=output=')).split('output=')[1];
fs.writeFileSync(output, JSON.stringify({ tests: [test], passes: [test], pending: [], failures: [] }));
""")
    observer = tmp_path / "observer"
    (observer / "istanbul-lib-instrument").mkdir(parents=True)
    (observer / "istanbul-lib-instrument/package.json").write_text('{"version":"6.0.3"}')
    (observer / "istanbul-lib-coverage").mkdir()
    (observer / "istanbul-lib-coverage/index.js").write_text(
        "exports.createCoverageMap = data => ({ merge() {}, toJSON() { return data; } });"
    )
    config = {
        "provider": "istanbul/mocha",
        "version": "6.0.3",
        "framework_version": "11.8.0",
        "source_scope": ["lib/**"],
        "test_scope": ["tests/*_tests.js"],
        "observer": {"path": str(observer), "sha256": "digest"},
    }
    if controlled:
        config["clock"] = module.CLOCK_POLICY
    native_run = subprocess.run
    observed_preloads = []

    def execute(
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] != "node":
            assert text
            return native_run(
                argv, capture_output=capture_output, text=True, timeout=timeout, cwd=cwd, env=env
            )
        if "instrument.cjs" in Path(argv[1]).name:
            workspace = Path(argv[3])
            source = str(workspace / "lib/example.js")
            Path(argv[5]).write_text(
                json.dumps(
                    {
                        source: {
                            "path": source,
                            "b": {},
                            "branchMap": {},
                            "f": {},
                            "fnMap": {},
                        }
                    }
                )
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        preload = Path(argv[2]).read_text()
        assert argv[:2] == ["node", "--require"]
        assert preload == (module.CLOCK_PRELOAD + "\n" if controlled else "") + module.PRELOAD
        observed_preloads.append(preload)
        assert text and env is not None
        env["CLOCK_EXPECTED"] = "1" if controlled else "0"
        return native_run(
            argv,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )

    monkeypatch.setattr(module.subprocess, "run", execute)
    report = module.collect(
        submitted=submitted,
        before=submitted,
        payload={
            "dependencies": str(dependencies),
            "dependency_sha256": "digest",
            "trusted_files": {},
            "framework": "mocha",
        },
        config=config,
        dependency_digest=lambda path: "digest",
    )
    assert len(observed_preloads) == 2
    assert report["contribution"]["candidate_ids"] == []
    assert report.get("clock") == (module.CLOCK_POLICY if controlled else None)


def test_preparation_accepts_original_task_directory_or_checkout_root(tmp_path: Path) -> None:
    module = template("prepare")
    task = "ofetch-per-origin-circuit-breaker"
    (tmp_path / "tasks" / task).mkdir(parents=True)
    assert module.suite_root(tmp_path, task) == tmp_path
    assert module.suite_root(tmp_path / "tasks", task) == tmp_path
