from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import sys
from types import ModuleType

import pytest

from pluginbench.collectors import freeze_collectors
from pluginbench.remeasure import load_profiles
import pluginbench.task_data.deepswe.coverage as coverage_adapter

ROOT = Path(__file__).parents[1]
TASKS = {
    "ofetch-per-origin-circuit-breaker": {
        "source_scope": ["src/**"],
        "test_scope": ["test/*.test.ts", "test/**/*.test.ts"],
        "entry": ["src/index.ts"],
        "version": "4.0.5",
    },
    "happy-dom-abort-pending-body-reads": {
        "source_scope": ["packages/happy-dom/src/**"],
        "test_scope": ["packages/happy-dom/test/*.test.ts", "packages/happy-dom/test/**/*.test.ts"],
        "entry": ["packages/happy-dom/src/index.ts"],
        "version": "4.0.16",
        "reference_test_files": [
            "packages/happy-dom/test/fetch/Request.test.ts",
            "packages/happy-dom/test/fetch/Response.test.ts",
            "packages/happy-dom/test/browser/Browser.test.ts",
            "packages/happy-dom/test/browser/BrowserPage.test.ts",
            "packages/happy-dom/test/window/DetachedWindowAPI.test.ts",
        ],
    },
    "testem-per-launcher-reports": {
        "source_scope": ["lib/**", "testem.js"],
        "test_scope": ["tests/*_tests.js", "tests/**/*_tests.js"],
        "entry": ["testem.js"],
        "version": "6.0.3",
        "reference_test_files": [
            "tests/config_tests.js",
            "tests/strutils_tests.js",
            "tests/utils/report-file_tests.js",
        ],
        "excluded_test_files": [
            "tests/ci/ci_tests.js",
            "tests/ci/dev_tests.js",
            "tests/api_tests.js",
        ],
        "excluded_full_titles": [],
    },
    "true-myth-iterable-collection-combinators": {
        "source_scope": [
            "src/maybe.ts",
            "src/result.ts",
            "src/task.ts",
            "src/task/**",
            "src/toolbelt.ts",
        ],
        "test_scope": ["test/*.test.ts", "test/**/*.test.ts"],
        "entry": ["src/index.ts"],
        "version": "3.2.4",
        "reference_test_files": [
            "test/maybe.test.ts",
            "test/result.test.ts",
            "test/task.test.ts",
            "test/toolbelt.test.ts",
        ],
        "excluded_test_files": ["test/integration/standard-schema.test.ts"],
    },
    "happy-dom-deterministic-intersectionobserver": {
        "source_scope": ["packages/happy-dom/src/**"],
        "test_scope": ["packages/happy-dom/test/*.test.ts", "packages/happy-dom/test/**/*.test.ts"],
        "entry": ["packages/happy-dom/src/index.ts"],
        "version": "4.0.16",
        "reference_test_files": [
            "packages/happy-dom/test/event/EventTarget.test.ts",
            "packages/happy-dom/test/intersection-observer/IntersectionObserver.test.ts",
        ],
    },
}


@pytest.fixture
def measurements(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    directory = ROOT / "src/pluginbench/task_data/deepswe"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "real_coding_measurements", directory / "measurements.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *args, **kwargs: "fallow 3.18.0\n"
    )
    return module


def prepared_task(module: ModuleType, output: Path, task: str) -> tuple[Path, dict[str, str]]:
    fixture = output / "fixtures" / task
    fixture.mkdir(parents=True)
    (fixture / "source.js").write_text("export const value = 1;\n")
    dependencies = output / "dependencies" / task / "node_modules"
    version = TASKS[task]["version"]
    packages = {"vitest": version, "@vitest/coverage-v8": version}
    if task == "testem-per-launcher-reports":
        packages = {"mocha": "11.8.0"}
    for package, package_version in packages.items():
        path = dependencies / package
        path.mkdir(parents=True)
        (path / "package.json").write_text(json.dumps({"version": package_version}))
    if task == "ofetch-per-origin-circuit-breaker":
        (fixture / "pnpm-lock.yaml").write_text(
            json.dumps(
                {
                    "importers": {
                        ".": {"devDependencies": {name: {"version": "4.0.5"} for name in packages}}
                    }
                }
            )
        )
    payload = {
        "dependencies": str(dependencies),
        "dependency_sha256": module.dependency_digest(dependencies),
    }
    verifier = output / "verify.py"
    verifier.write_text("PAYLOAD = " + repr(payload) + "\n")
    (output / "tasks.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": task,
                        "fixture": f"fixtures/{task}",
                        "prompt": "Implement the feature.",
                        "verifier": {"command": [sys.executable], "script": "verify.py"},
                    }
                ],
            }
        )
    )
    return verifier, payload


@pytest.mark.parametrize("task", TASKS)
def test_generation_preserves_calibrated_corpus_and_emits_measurement_profile(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str
) -> None:
    output = tmp_path / "prepared"
    verifier, payload = prepared_task(measurements, output, task)
    observers = []

    def observer(name: str, cache: Path) -> dict[str, str]:
        observers.append(name)
        path = cache / name / "node_modules"
        path.mkdir(parents=True)
        return {"path": str(path), "sha256": "a" * 64}

    monkeypatch.setattr(measurements, "prepare_observer", observer)
    collectors = measurements.prepare_measurements(
        prepared=output,
        task=task,
        payload=payload,
        verifier=verifier,
        observer_cache=tmp_path / "observers",
    )
    profiles = load_profiles(output / "measurements.json")
    assert profiles[task] == collectors
    coverage = json.loads(collectors[0].config.read_text())
    fallow = json.loads(collectors[1].config.read_text())
    expected = TASKS[task]
    for key in (
        "source_scope",
        "test_scope",
        "version",
        "reference_test_files",
        "excluded_test_files",
        "excluded_full_titles",
    ):
        assert coverage.get(key) == expected.get(key)
    assert fallow["entry"] == expected["entry"]
    code_scope = (
        ["src/**"]
        if task == "true-myth-iterable-collection-combinators"
        else expected["source_scope"]
    )
    assert collectors[1].scope == code_scope
    assert json.loads(measurements.measurement_metadata(task)["source_scope"]) == code_scope
    assert fallow["production"] is True
    assert fallow["health"] == {"maxCognitive": 0, "maxCyclomatic": 0, "maxUnitSize": 0}
    assert coverage["run_timeout_seconds"] == 600
    assert all(collector.timeout_seconds == 7200 for collector in collectors)
    assert observers == [
        "oxc-parser-0.126.0",
        *([measurements.OBSERVER_NAMES[task]] if task in measurements.OBSERVER_NAMES else []),
    ]
    assert collectors[0].command[0] == str(Path(sys.executable).absolute())
    assert str(verifier) in collectors[0].command
    assert collectors[1].syntax_command[-1] == "a" * 64
    frozen = freeze_collectors(profiles[task], tmp_path / "frozen")
    assert frozen[0].command is not None and frozen[1].syntax_command is not None
    assert Path(frozen[0].command[2]).read_bytes() == verifier.read_bytes()
    assert (
        Path(frozen[0].command[1]).read_bytes()
        == (ROOT / "src/pluginbench/task_data/deepswe/coverage.py").read_bytes()
    )
    assert Path(frozen[1].syntax_command[1]).read_bytes() == measurements.PARSER.read_bytes()
    assert not (output / "study.json").exists()


def test_preparation_emits_profile_without_a_study(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = "ofetch-per-origin-circuit-breaker"
    verifier, payload = prepared_task(measurements, tmp_path, task)
    monkeypatch.setattr(
        measurements,
        "prepare_observer",
        lambda name, cache: {"path": str(cache / name / "node_modules"), "sha256": "a" * 64},
    )
    measurements.prepare_measurements(
        prepared=tmp_path, task=task, payload=payload, verifier=verifier
    )
    assert task in load_profiles(tmp_path / "measurements.json")
    assert not (tmp_path / "study.json").exists()


def test_observer_reuse_checks_lock_and_installed_bytes(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def install(argv: list[str], *, cwd: Path, **kwargs: object) -> None:
        calls.append(argv)
        (cwd / "node_modules").mkdir()
        (cwd / "node_modules/observer.js").write_text("pinned bytes")

    monkeypatch.setattr(measurements.subprocess, "run", install)
    observer = measurements.prepare_observer("oxc-parser-0.126.0", tmp_path)
    assert measurements.prepare_observer("oxc-parser-0.126.0", tmp_path) == observer
    alias = tmp_path / "cache-root-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    assert measurements.prepare_observer("oxc-parser-0.126.0", alias) == observer
    assert len(calls) == 1 and "--ignore-scripts" in calls[0]
    cache = Path(observer["path"]).parent
    (cache / "package-lock.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest or lock changed"):
        measurements.prepare_observer("oxc-parser-0.126.0", tmp_path)
    (cache / "package-lock.json").write_bytes(
        (measurements.ROOT / "observers/oxc-parser-0.126.0/package-lock.json").read_bytes()
    )
    (cache / "node_modules/observer.js").write_text("modified")
    with pytest.raises(ValueError, match="dependency cache changed"):
        measurements.prepare_observer("oxc-parser-0.126.0", tmp_path)


@pytest.mark.parametrize("descendant", ["name", "lock"])
def test_cache_descendant_symlinks_cannot_redirect_into_a_fixture(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descendant: str
) -> None:
    prepared = tmp_path / "prepared"
    task = "ofetch-per-origin-circuit-breaker"
    verifier, payload = prepared_task(measurements, prepared, task)
    cache = tmp_path / "outside-cache"
    cache.mkdir()
    name = "oxc-parser-0.126.0"
    source = measurements.ROOT / "observers" / name
    digest = hashlib.sha256((source / "package-lock.json").read_bytes()).hexdigest()
    target = prepared / "fixtures" / task / "observer-cache"
    target.mkdir()
    if descendant == "name":
        (cache / name).symlink_to(target, target_is_directory=True)
    else:
        (cache / name).mkdir()
        (cache / name / digest).symlink_to(target, target_is_directory=True)
        # A previously populated cache must still be rejected before reuse.
        for filename in ("package.json", "package-lock.json"):
            (target / filename).write_bytes((source / filename).read_bytes())
        (target / "node_modules").mkdir()
        (target / "receipt.json").write_text(
            json.dumps(
                {
                    "lock_sha256": digest,
                    "sha256": measurements.dependency_digest(target / "node_modules"),
                }
            )
        )
    before = {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    }

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("redirected cache must be rejected before installation")

    monkeypatch.setattr(measurements.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="symlinks"):
        measurements.prepare_measurements(
            prepared=prepared,
            task=task,
            payload=payload,
            verifier=verifier,
            observer_cache=cache,
        )
    assert {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    } == before
    assert not (target / digest).exists()


def test_install_failure_does_not_clean_up_a_redirected_external_directory(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "preserve.txt").write_text("unrelated bytes")

    def install(argv: list[str], *, cwd: Path, **kwargs: object) -> None:
        shutil.rmtree(cwd)
        cwd.symlink_to(external, target_is_directory=True)
        raise RuntimeError("install failed")

    monkeypatch.setattr(measurements.subprocess, "run", install)
    with pytest.raises(RuntimeError, match="install failed"):
        measurements.prepare_observer("oxc-parser-0.126.0", tmp_path / "cache")
    assert (external / "preserve.txt").read_text() == "unrelated bytes"


def test_returned_observer_paths_are_checked_before_profile_writes(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = "ofetch-per-origin-circuit-breaker"
    verifier, payload = prepared_task(measurements, tmp_path, task)
    redirected = tmp_path / "fixtures" / task / "observer/node_modules"
    monkeypatch.setattr(
        measurements,
        "prepare_observer",
        lambda name, cache: {"path": str(redirected), "sha256": "a" * 64},
    )
    with pytest.raises(ValueError, match="resolved observer caches must be outside"):
        measurements.prepare_measurements(
            prepared=tmp_path,
            task=task,
            payload=payload,
            verifier=verifier,
            observer_cache=tmp_path / "cache",
        )
    assert not (tmp_path / "measurements").exists()


def test_task_version_drift_and_fixture_observer_cache_are_rejected(
    measurements: ModuleType, tmp_path: Path
) -> None:
    task = "ofetch-per-origin-circuit-breaker"
    verifier, payload = prepared_task(measurements, tmp_path, task)
    with pytest.raises(ValueError, match="outside task fixtures"):
        measurements.prepare_measurements(
            prepared=tmp_path,
            task=task,
            payload=payload,
            verifier=verifier,
            observer_cache=tmp_path / "fixtures/cache",
        )
    package = Path(payload["dependencies"]) / "@vitest/coverage-v8/package.json"
    package.write_text('{"version": "4.0.6"}')
    payload["dependency_sha256"] = measurements.dependency_digest(Path(payload["dependencies"]))
    with pytest.raises(ValueError, match="locked Vitest/V8 4.0.5"):
        measurements.prepare_measurements(
            prepared=tmp_path, task=task, payload=payload, verifier=verifier
        )


def test_pinned_observer_declarations_have_no_machine_paths(measurements: ModuleType) -> None:
    for path in (measurements.ROOT / "observers").rglob("*.json"):
        text = path.read_text()
        assert "/Users/" not in text and "file:" not in text and "runs/" not in text
        data = json.loads(text)
        if path.name == "package-lock.json":
            assert all(
                package.get("integrity") for name, package in data["packages"].items() if name
            )
    testem = json.loads(
        (measurements.ROOT / "observers/testem-istanbul-6.0.3/package-lock.json").read_text()
    )
    assert "node_modules/c8" not in testem["packages"]


def test_oxc_digest_matches_path_order_and_symlink_targets(
    measurements: ModuleType, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    modules = cache / "node_modules"
    package = modules / "oxc-parser"
    (package / "src-js").mkdir(parents=True)
    lock = json.loads(
        (measurements.ROOT / "observers/oxc-parser-0.126.0/package-lock.json").read_text()
    )
    (cache / "package-lock.json").write_text(json.dumps(lock))
    (package / "package.json").write_text(json.dumps({"version": "0.126.0", "type": "module"}))
    (package / "src-js/index.js").write_text("export const parseSync = () => ({ errors: [] });")
    binding = modules / "@oxc-parser/binding-darwin-arm64"
    binding.mkdir(parents=True)
    (binding / "binding.node").write_bytes(b"fixture binding")
    (modules / "a").mkdir()
    (modules / "a/child").write_text("nested")
    (modules / "a.js").write_text("sibling")
    (modules / "file-link").symlink_to("a.js")
    (modules / "directory-link").symlink_to("a", target_is_directory=True)
    (modules / "dangling-link").symlink_to("missing")
    expected = measurements.dependency_digest(modules)
    inputs = tmp_path / "inputs.json"
    inputs.write_text('{"files": [], "file_count": 0}')
    output = tmp_path / "parsed.json"
    execution = subprocess.run(
        [
            "node",
            str(measurements.PARSER),
            str(cache),
            str(tmp_path),
            str(inputs),
            str(output),
            expected,
        ],
        capture_output=True,
        text=True,
    )
    assert execution.returncode == 0, execution.stderr
    assert json.loads(output.read_text())["dependency_digest"] == expected


def test_fallow_version_drift_is_rejected(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        measurements.subprocess, "check_output", lambda *args, **kwargs: "fallow 3.19.0\n"
    )
    with pytest.raises(ValueError, match="expected Fallow 3.18.0"):
        measurements.prepare_measurements(
            prepared=tmp_path,
            task="ofetch-per-origin-circuit-breaker",
            payload={},
            verifier=tmp_path / "verify.py",
        )


def test_oxc_expected_digest_uses_verifier_bytes_and_rejects_changes(
    measurements: ModuleType, tmp_path: Path
) -> None:
    lock = measurements.ROOT / "observers/oxc-parser-0.126.0/package-lock.json"

    cache = (
        measurements.observer_cache_root()
        / "oxc-parser-0.126.0"
        / hashlib.sha256(lock.read_bytes()).hexdigest()
    )
    if not (cache / "node_modules/oxc-parser").is_dir():
        pytest.skip("prepared pinned Oxc observer unavailable")
    snapshot = tmp_path / "source"
    snapshot.mkdir()
    (snapshot / "example.ts").write_text("export const value: number = 1;\n")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"files": ["example.ts"], "file_count": 1}))
    output = tmp_path / "parsed.json"
    expected = measurements.dependency_digest(cache / "node_modules")
    command = [
        "node",
        str(measurements.PARSER),
        str(cache),
        str(snapshot),
        str(inputs),
        str(output),
    ]
    execution = subprocess.run([*command, expected], capture_output=True, text=True)
    assert execution.returncode == 0, execution.stderr
    receipt = json.loads(output.read_text())
    assert receipt["dependency_digest"] == receipt["expected_dependency_digest"] == expected
    assert receipt["files"][0]["status"] == "valid"
    # Alter only a disposable copy, preserving the shared cache used by studies.

    copied = tmp_path / "cache"
    shutil.copytree(cache, copied, symlinks=True)
    (copied / "node_modules/changed.js").write_text("modified")
    command[2] = str(copied)
    execution = subprocess.run([*command, expected], capture_output=True, text=True)
    assert execution.returncode != 0 and "observer dependency cache changed" in execution.stderr


@pytest.mark.parametrize(
    "selection", [[], ["tokens"], ["lines"], ["review"], ["tokens", "lines", "review"]]
)
def test_non_analyzer_measurements_skip_all_observer_setup(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: list[str]
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unselected analyzer setup ran")

    monkeypatch.setattr(measurements.subprocess, "check_output", forbidden)
    monkeypatch.setattr(measurements, "prepare_observer", forbidden)
    monkeypatch.setattr(measurements, "validate_task_dependencies", forbidden)
    collectors = measurements.prepare_measurements(
        prepared=tmp_path,
        task="ofetch-per-origin-circuit-breaker",
        payload={},
        verifier=tmp_path / "verify.py",
        selection=selection,
    )
    assert collectors == []
    assert json.loads((tmp_path / "measurements.json").read_text()) == {
        "ofetch-per-origin-circuit-breaker": []
    }
    provenance = json.loads((tmp_path / "provenance.json").read_text())
    assert json.loads(provenance["source_scope"]) == ["src/**"]
    assert (
        json.loads(provenance["test_scope"])
        == TASKS["ofetch-per-origin-circuit-breaker"]["test_scope"]
    )
    assert provenance["measurement_profile_version"] == "deepswe-maintainability-v1"
    assert json.loads(provenance["selected_measurements"]) == sorted(selection)
    assert not (tmp_path / "measurements").exists()


@pytest.mark.parametrize("measurement", ["cyclomatic", "cognitive", "dead_code"])
def test_complexity_and_dead_code_require_only_fallow(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measurement: str
) -> None:
    observers: list[str] = []

    def observer(name: str, cache: Path) -> dict[str, str]:
        observers.append(name)
        return {"path": str(cache / name / "node_modules"), "sha256": "a" * 64}

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unselected coverage setup ran")

    monkeypatch.setattr(measurements, "prepare_observer", observer)
    monkeypatch.setattr(measurements, "validate_task_dependencies", forbidden)
    collectors = measurements.prepare_measurements(
        prepared=tmp_path,
        task="ofetch-per-origin-circuit-breaker",
        payload={},
        verifier=tmp_path / "verify.py",
        selection=[measurement],
        observer_cache=tmp_path / "cache",
    )
    assert [collector.kind for collector in collectors] == ["fallow"]
    assert observers == ["oxc-parser-0.126.0"]
    assert not (tmp_path / "measurements/coverage.json").exists()


@pytest.mark.parametrize("measurement", ["coverage", "test_overlap"])
@pytest.mark.parametrize("task", TASKS)
def test_coverage_selects_only_its_observer_and_requested_overlap(
    measurements: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    measurement: str,
    task: str,
) -> None:
    verifier, payload = prepared_task(measurements, tmp_path, task)
    observers: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unselected Fallow version check ran")

    def observer(name: str, cache: Path) -> dict[str, str]:
        observers.append(name)
        return {"path": str(cache / name / "node_modules"), "sha256": "a" * 64}

    monkeypatch.setattr(measurements.subprocess, "check_output", forbidden)
    monkeypatch.setattr(measurements, "prepare_observer", observer)
    collectors = measurements.prepare_measurements(
        prepared=tmp_path,
        task=task,
        payload=payload,
        verifier=verifier,
        selection=[measurement],
        observer_cache=tmp_path / "cache",
    )
    assert [collector.kind for collector in collectors] == ["coverage"]
    config = json.loads(collectors[0].config.read_text())
    assert config["measure_test_overlap"] is (measurement == "test_overlap")
    assert observers == (
        [measurements.OBSERVER_NAMES[task]] if task in measurements.OBSERVER_NAMES else []
    )
    assert not (tmp_path / "measurements/fallow.json").exists()


def test_unknown_measurement_is_rejected_before_setup_or_writes(
    measurements: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("invalid selection reached setup")

    monkeypatch.setattr(measurements.subprocess, "check_output", forbidden)
    monkeypatch.setattr(measurements, "prepare_observer", forbidden)
    with pytest.raises(ValueError, match="unsupported measurements"):
        measurements.prepare_measurements(
            prepared=tmp_path,
            task="ofetch-per-origin-circuit-breaker",
            payload={},
            verifier=tmp_path / "verify.py",
            selection=["unknown"],
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("framework", ["vitest", "mocha"])
def test_leave_one_out_preserves_duplicate_titles_and_native_occurrences(
    measurements: ModuleType,
    tmp_path: Path,
    framework: str,
) -> None:
    task = (
        "ofetch-per-origin-circuit-breaker"
        if framework == "vitest"
        else "testem-per-launcher-reports"
    )
    native_version = "4.0.5" if framework == "vitest" else "11.8.0"
    candidates = [
        *(ROOT / "benchmarks/real-coding/generated").glob(f"*/dependencies/{task}/node_modules"),
        *(ROOT / "runs").glob(f"*/inputs/{task}/dependencies/{task}/node_modules"),
    ]
    dependencies = next(
        (
            path
            for path in sorted(candidates)
            if (path / framework / "package.json").is_file()
            and json.loads((path / framework / "package.json").read_text())["version"]
            == native_version
        ),
        None,
    )
    if framework == "vitest":
        version, provider, observer = "4.0.5", "vitest/v8", None
    else:
        caches = sorted(
            (measurements.observer_cache_root() / "testem-istanbul-6.0.3").glob("*/node_modules")
        )
        version, provider = "6.0.3", "istanbul/mocha"
        observer = caches[0] if caches else None
    if dependencies is None or (framework == "mocha" and observer is None):
        pytest.skip("prepared pinned native test dependencies unavailable")
    submitted, before = tmp_path / "submitted", tmp_path / "before"
    for directory in (submitted, before):
        (directory / "src").mkdir(parents=True)
        (directory / "test").mkdir()
        source = (
            "export function pick(value) { return value ? 'yes' : 'no'; }"
            if framework == "vitest"
            else "exports.pick = function(value) { return value ? 'yes' : 'no'; };"
        )
        (directory / "src/pick.js").write_text(source)
        if framework == "vitest":
            (directory / "package.json").write_text('{"type":"module"}')
    imports = (
        "import { describe, it, expect } from 'vitest'; import { pick } from '../src/pick.js';"
        if framework == "vitest"
        else "const assert = require('node:assert/strict'); const { pick } = require('../src/pick');"
    )
    positive = (
        "expect(pick(true)).toBe('yes');"
        if framework == "vitest"
        else "assert.equal(pick(true), 'yes');"
    )
    negative = (
        "expect(pick(false)).toBe('no');"
        if framework == "vitest"
        else "assert.equal(pick(false), 'no');"
    )
    for directory in (submitted, before):
        (directory / "test/b.test.js").write_text(
            imports + "describe('shared', () => { it('works', () => {" + positive + "}); });"
        )
    (before / "test/a.test.js").write_text(
        imports
        + "describe('shared', () => { it('works', () => {"
        + positive
        + "}); it.skip('works', () => {}); });"
    )
    (submitted / "test/a.test.js").write_text(
        imports
        + "describe('shared', () => { it('works', () => {"
        + positive
        + "}); it('works', () => {"
        + negative
        + "}); it.skip('works', () => {}); });"
    )
    config: coverage_adapter.Configuration = {
        "source_scope": ["src/**"],
        "test_scope": ["test/*.test.js"],
        "provider": provider,
        "version": version,
        "framework_version": native_version,
    }
    if observer is not None:
        config["observer"] = {
            "path": str(observer),
            "sha256": measurements.dependency_digest(observer),
        }
    report = coverage_adapter.collect(
        submitted=submitted,
        before=before,
        payload={
            "dependencies": str(dependencies),
            "dependency_sha256": measurements.dependency_digest(dependencies),
            "trusted_files": {},
            "framework": framework,
        },
        config=config,
        dependency_digest=measurements.dependency_digest,
    )
    contribution = report["contribution"]
    first = "test/a.test.js::shared works::native-occurrence:1"
    second = "test/a.test.js::shared works::native-occurrence:2"
    pending = "test/a.test.js::shared works::native-occurrence:3"
    other_file = "test/b.test.js::shared works"
    assert contribution["candidate_ids"] == [first, second]
    assert contribution["zero_marginal_ids"] == [first]
    removals = contribution["removal_runs"]
    assert isinstance(removals, list)
    for removal, excluded in zip(removals, (first, second), strict=True):
        assert isinstance(removal, dict)
        assert removal["excluded_id"] == excluded
        assert other_file in removal["passing_ids"]
        assert removal["test_statuses"][pending] == "pending"
        assert removal["test_statuses"][excluded] == "pending"
        assert len(removal["test_statuses"]) == 4
    assert report["branches"]["total"] and report["branches"]["covered"]


def test_coverage_cli_does_not_write_bytecode_into_frozen_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, config, output = (
        tmp_path / "verify.py",
        tmp_path / "config.json",
        tmp_path / "out.json",
    )
    verifier.write_text("PAYLOAD = {'node': 'v22.20.0'}\ndef dependency_digest(path): return ''\n")
    config.write_text("{}")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coverage.py",
            str(verifier),
            str(output),
            "--config",
            str(config),
            "--before",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "v22.20.0")

    def collect(**arguments: object) -> coverage_adapter.CoverageRun:
        return {
            "files_measured": [],
            "branches": {"total": [], "covered": []},
            "functions": {"total": [], "covered": []},
            "test_population": "fixture",
            "istanbul": {},
            "coordinate_method": "source-location-and-arm-v1",
        }

    monkeypatch.setattr(coverage_adapter, "collect", collect)
    coverage_adapter.main()
    assert not (tmp_path / "__pycache__").exists()
    assert sys.dont_write_bytecode is False
    assert json.loads(output.read_text())["node"] == "v22.20.0"
