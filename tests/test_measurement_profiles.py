import json
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from pluginbench import measurement_profiles as profiles
from pluginbench.assessment_metrics import _analysis_candidates
from pluginbench.measurement_profiles import (
    CoverageSettings,
    MeasurementSettings,
    capability_plan,
    prepare_profiles,
)


def task(tmp_path: Path, language: str) -> Path:
    native = tmp_path / "native"
    native.mkdir()
    (native / "task.toml").write_text(f'[metadata]\nlanguage = "{language}"\n')
    return native


@pytest.mark.parametrize("path", ["/tmp/lcov.info", "../lcov.info", ".", "a\\b"])
def test_coverage_report_path_cannot_escape(path: str) -> None:
    with pytest.raises(ValueError, match="relative report"):
        CoverageSettings(command=["npm", "test"], report_path=path, tool_version="1")


def test_capabilities_are_per_metric_not_per_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = task(tmp_path, "python")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    from pluginbench import language_analysis

    monkeypatch.setattr(language_analysis, "default_tool_paths", lambda _: {})
    result = capability_plan(
        native,
        ["tokens", "lines", "cyclomatic", "coverage", "test_overlap", "review"],
        MeasurementSettings(),
    )
    assert (
        result["tokens"]["status"]
        == result["lines"]["status"]
        == result["review"]["status"]
        == "available"
    )
    assert result["cyclomatic"]["status"] == "unavailable"
    assert result["coverage"]["status"] == "unavailable"
    assert "per-test" in str(result["test_overlap"]["reason"])


@pytest.mark.parametrize("coverage_scope", [None, ["main.py"]])
def test_coverage_profile_is_fixed_operator_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coverage_scope: list[str] | None
) -> None:
    monkeypatch.setattr(
        profiles,
        "prepare_observer",
        lambda *args: pytest.fail("coverage does not need a syntax observer"),
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: pytest.fail("coverage does not probe Fallow"),
    )
    native = task(tmp_path, "python")
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print(1)\n")
    (source / "test_main.py").write_text("def test_main(): pass\n")
    settings = MeasurementSettings(
        coverage=CoverageSettings(
            command=["python", "-m", "pytest", "--cov", "--cov-report=lcov:coverage.info"],
            report_path="coverage.info",
            tool_version="pytest-cov 7.0",
            source_scope=coverage_scope,
        )
    )
    identity: dict[str, object] = {
        "task_dir": str(native),
        "workdir": "/app",
        "source_digest": "fixed",
    }
    collectors = prepare_profiles(
        source, tmp_path / "profiles", ["coverage", "test_overlap"], settings, identity
    )
    assert len(collectors) == 1
    collector = collectors[0]
    assert settings.coverage is not None
    assert collector.command == settings.coverage.command
    assert collector.report_format == "lcov"
    frozen = json.loads(collector.config.read_text())
    assert frozen["native_task_identity"] == identity
    provenance = json.loads((tmp_path / "profiles/measurement-provenance.json").read_text())
    assert provenance["starting_source_paths"] == ["main.py"]
    assert provenance["starting_test_paths"] == ["test_main.py"]
    assert provenance["capabilities"]["test_overlap"]["status"] == "unavailable"
    assert collector.scope == (coverage_scope or ["*"])
    assert frozen["source_scope"] == collector.scope
    assert json.loads(provenance["source_scope"]) == ["*"]


@pytest.mark.parametrize("scope", [["/private/**"], ["../src/**"], [""]])
def test_coverage_scope_cannot_escape_declared_repository(scope: list[str]) -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        CoverageSettings(
            command=["npm", "test"],
            report_path="coverage.info",
            tool_version="1",
            source_scope=scope,
        )


def test_fallow_keeps_real_entrypoints_and_reports_all_complexity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = task(tmp_path, "typescript")
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.ts").write_text("export function value() { return 1; }\n")
    operator = tmp_path / "fallow.json"
    operator.write_text(
        json.dumps(
            {
                "entry": ["index.ts"],
                "ignorePatterns": ["generated/**"],
                "health": {"maxCognitive": 15},
            }
        )
    )
    monkeypatch.setattr(shutil, "which", lambda _: "/tool")
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "fallow 3.18.0")
    monkeypatch.setattr(
        profiles,
        "prepare_observer",
        lambda name, cache: {"path": str(tmp_path / "cache/node_modules"), "sha256": "fixed"},
    )
    collectors = prepare_profiles(
        source,
        tmp_path / "profiles",
        ["cognitive", "dead_code"],
        MeasurementSettings(fallow_config=operator),
        {"task_dir": str(native)},
    )
    config = json.loads(collectors[0].config.read_text())
    assert config["entry"] == ["index.ts"]
    assert config["health"] == {"maxCognitive": 0, "maxCyclomatic": 0, "maxUnitSize": 0}
    assert config["production"] is True


def test_fallow_extends_is_not_resolved_from_agent_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = task(tmp_path, "typescript")
    source = tmp_path / "source"
    source.mkdir()
    config = source / ".fallowrc.json"
    config.write_text('{"extends": ["../private.json"]}')
    monkeypatch.setattr(shutil, "which", lambda _: "/tool")
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "fallow 3.18.0")
    monkeypatch.setattr(
        profiles,
        "prepare_observer",
        lambda name, cache: {"path": str(tmp_path / "cache/node_modules"), "sha256": "fixed"},
    )
    with pytest.raises(ValueError, match="without extends"):
        prepare_profiles(
            source,
            tmp_path / "profiles",
            ["dead_code"],
            MeasurementSettings(),
            {"task_dir": str(native)},
        )


@pytest.mark.parametrize("version", ["fallow 3.19.0", "fallow 3.18.0"])
def test_fallow_version_capability_is_measured_once_and_does_not_block_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    native = task(tmp_path, "typescript")
    monkeypatch.setattr(shutil, "which", lambda _: "/tool")
    calls: list[object] = []

    def observed(*args: object, **kwargs: object) -> str:
        calls.append(args)
        return version

    monkeypatch.setattr(subprocess, "check_output", observed)
    result = capability_plan(
        native, ["cognitive", "cyclomatic", "dead_code", "lines"], MeasurementSettings()
    )
    assert len(calls) == 1
    assert result["lines"]["status"] == "available"
    for name in ("cognitive", "cyclomatic", "dead_code"):
        assert result[name]["status"] == (
            "available" if version == "fallow 3.18.0" else "unavailable"
        )
        assert result[name]["tool_version"] == ("3.18.0" if version == "fallow 3.18.0" else version)


def test_observer_reuse_checks_lock_and_installed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def install(argv: list[str], *, cwd: Path, **kwargs: object) -> None:
        calls.append(argv)
        (cwd / "node_modules").mkdir()
        (cwd / "node_modules/observer.js").write_text("pinned bytes")

    monkeypatch.setattr(subprocess, "run", install)
    observer = profiles.prepare_observer("oxc-parser-0.126.0", tmp_path)
    assert profiles.prepare_observer("oxc-parser-0.126.0", tmp_path) == observer
    alias = tmp_path / "cache-root-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    assert profiles.prepare_observer("oxc-parser-0.126.0", alias) == observer
    assert len(calls) == 1 and "--ignore-scripts" in calls[0]
    cache = Path(observer["path"]).parent
    (cache / "package-lock.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest or lock changed"):
        profiles.prepare_observer("oxc-parser-0.126.0", tmp_path)
    (cache / "package-lock.json").write_bytes(
        (profiles.ROOT / "observer_data/oxc-parser-0.126.0/package-lock.json").read_bytes()
    )
    (cache / "node_modules/observer.js").write_text("modified")
    with pytest.raises(ValueError, match="dependency cache changed"):
        profiles.prepare_observer("oxc-parser-0.126.0", tmp_path)


def test_install_failure_does_not_clean_up_a_redirected_external_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "preserve.txt").write_text("unrelated bytes")

    def install(argv: list[str], *, cwd: Path, **kwargs: object) -> None:
        shutil.rmtree(cwd)
        cwd.symlink_to(external, target_is_directory=True)
        raise RuntimeError("install failed")

    monkeypatch.setattr(subprocess, "run", install)
    with pytest.raises(RuntimeError, match="install failed"):
        profiles.prepare_observer("oxc-parser-0.126.0", tmp_path / "cache")
    assert (external / "preserve.txt").read_text() == "unrelated bytes"


@pytest.mark.parametrize("descendant", ["name", "lock", "node_modules"])
def test_observer_cache_redirect_is_rejected_without_touching_external_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descendant: str
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    external = tmp_path / "unrelated"
    external.mkdir()
    sentinel = external / "preserve.txt"
    sentinel.write_text("unrelated bytes")
    name = "oxc-parser-0.126.0"
    digest = hashlib.sha256(
        (profiles.ROOT / "observer_data" / name / "package-lock.json").read_bytes()
    ).hexdigest()
    destination = cache / name / digest
    redirected = {
        "name": cache / name,
        "lock": destination,
        "node_modules": destination / "node_modules",
    }[descendant]
    redirected.parent.mkdir(parents=True, exist_ok=True)
    redirected.symlink_to(external, target_is_directory=True)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("redirect must be rejected before dependency installation")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="redirect"):
        profiles.prepare_observer(name, cache)
    assert sentinel.read_text() == "unrelated bytes"
    assert list(external.iterdir()) == [sentinel]


def test_returned_observer_inside_source_is_rejected_before_profile_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = task(tmp_path, "typescript")
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "profiles"
    monkeypatch.setattr(shutil, "which", lambda _: "/tool")
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "fallow 3.18.0")
    monkeypatch.setattr(
        profiles,
        "prepare_observer",
        lambda name, cache: {"path": str(source / "observer/node_modules"), "sha256": "fixed"},
    )
    with pytest.raises(ValueError, match="outside measured source"):
        prepare_profiles(
            source, output, ["dead_code"], MeasurementSettings(), {"task_dir": str(native)}
        )
    assert list(output.iterdir()) == []


def test_locked_syntax_observer_contains_only_portable_integrity_bound_dependencies() -> None:
    for path in (profiles.ROOT / "observer_data").rglob("*.json"):
        text = path.read_text()
        assert "/Users/" not in text and "file:" not in text and "runs/" not in text
        if path.name == "package-lock.json":
            lock = json.loads(text)
            assert all(
                package.get("integrity") for name, package in lock["packages"].items() if name
            )


def observer_stub(cache: Path) -> Path:
    modules = cache / "node_modules"
    package = modules / "oxc-parser"
    (package / "src-js").mkdir(parents=True)
    lock = profiles.ROOT / "observer_data/oxc-parser-0.126.0/package-lock.json"
    (cache / "package-lock.json").write_bytes(lock.read_bytes())
    (package / "package.json").write_text(json.dumps({"version": "0.126.0", "type": "module"}))
    (package / "src-js/index.js").write_text("export const parseSync = () => ({ errors: [] });")
    binding = modules / "@oxc-parser/binding-darwin-arm64"
    binding.mkdir(parents=True)
    (binding / "binding.node").write_bytes(b"fixture binding")
    return modules


def test_oxc_observer_and_python_dependency_digest_match_path_order_and_links(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    modules = observer_stub(cache)
    (modules / "a").mkdir()
    (modules / "a/child").write_text("nested")
    (modules / "a.js").write_text("sibling")
    (modules / "file-link").symlink_to("a.js")
    (modules / "directory-link").symlink_to("a", target_is_directory=True)
    (modules / "dangling-link").symlink_to("missing")
    expected = profiles.dependency_digest(modules)
    inputs = tmp_path / "inputs.json"
    inputs.write_text('{"files": [], "file_count": 0}')
    output = tmp_path / "parsed.json"
    command = [
        "node",
        str(profiles.ROOT / "fallow_parse.mjs"),
        str(cache),
        str(tmp_path),
        str(inputs),
        str(output),
        expected,
    ]
    execution = subprocess.run(command, capture_output=True, text=True)
    assert execution.returncode == 0, execution.stderr
    receipt = json.loads(output.read_text())
    assert receipt["dependency_digest"] == receipt["expected_dependency_digest"] == expected
    # The same observer rejects new bytes against the original frozen dependency identity.
    (modules / "changed.js").write_text("modified")
    execution = subprocess.run(command, capture_output=True, text=True)
    assert execution.returncode != 0 and "observer dependency cache changed" in execution.stderr


def test_non_analyzer_selection_does_not_prepare_observers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = task(tmp_path, "typescript")
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(
        profiles, "prepare_observer", lambda *args: pytest.fail("no observer needed")
    )
    monkeypatch.setattr(
        subprocess, "check_output", lambda *args, **kwargs: pytest.fail("no analyzer probe needed")
    )
    assert (
        prepare_profiles(
            source,
            tmp_path / "profiles",
            ["tokens", "lines", "review"],
            MeasurementSettings(),
            {"task_dir": str(native)},
        )
        == []
    )


def test_prepared_readiness_distinguishes_absent_nonrepeatable_and_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    capability: dict[str, object] = {
        "coverage": {"status": "available", "collector": "coverage", "reason": None}
    }
    monkeypatch.setattr(profiles, "collect_measurements", lambda *args, **kwargs: {})
    counts = {"line_total": 2, "line_covered": 1, "line_population_complete": True}
    observations = iter(
        [
            {"coverage": {"metrics": counts}},
            {"coverage": {"metrics": {**counts, "line_covered": 2}}},
        ]
    )
    monkeypatch.setattr(profiles, "evaluate_evidence", lambda *args: next(observations))
    result = profiles.probe_measurements(source, tmp_path / "varying", [], ["coverage"], capability)
    assert result["coverage"]["readiness"] == "failed"
    reason = result["coverage"]["reason"]
    assert isinstance(reason, str)
    assert "differed" in reason
    monkeypatch.setattr(
        profiles,
        "evaluate_evidence",
        lambda *args: {"coverage": {"metrics": {**counts, "test_command_exit_code": 1}}},
    )
    result = profiles.probe_measurements(
        source, tmp_path / "failing-tests", [], ["coverage"], capability
    )
    assert result["coverage"]["readiness"] == "passed"
    assert result["coverage"]["test_command_exit_code"] == 1

    for name, scope_name in (("ambiguous", "owner"), ("unique", "second")):
        (source / "source.py").write_text(
            f"def owner():\n    unused = 1\ndef {scope_name}():\n    unused = 1\n"
        )
        candidates: list[object] = [
            {
                "category": "vulture-unused-variable",
                "path": "source.py",
                "symbol": "unused",
                "line": line,
            }
            for line in (2, 4)
        ]
        _, identities = _analysis_candidates(
            {"language": "python", "unused_candidates": candidates},
            analyzed={"source.py"},
            root=source,
        )
        metrics = {
            "metric_statuses": {"dead_code": {"status": "available", "reason": None}},
            "dead_code": 2,
            "dead_code_complete_for_declared_scope": True,
            "dead_finding_identities": identities,
        }
        monkeypatch.setattr(
            profiles, "evaluate_evidence", lambda *args: {"analysis": {"metrics": metrics}}
        )
        result = profiles.probe_measurements(
            source,
            tmp_path / name,
            [],
            ["dead_code"],
            {"dead_code": {"status": "available", "collector": "analysis", "reason": None}},
        )
        assert result["dead_code"]["readiness"] == ("failed" if name == "ambiguous" else "passed")
        assert metrics["dead_code"] == 2
        if name == "ambiguous":
            reason = result["dead_code"]["reason"]
            assert isinstance(reason, str)
            assert "unique source-bound lexical declarations" in reason

    def invalid(*args: object, **kwargs: object) -> dict[str, Path]:
        raise ValueError("source mutated")

    monkeypatch.setattr(profiles, "collect_measurements", invalid)
    with pytest.raises(ValueError, match="source mutated"):
        profiles.probe_measurements(source, tmp_path / "integrity", [], ["coverage"], capability)


def test_repository_coverage_profile_overrides_language_default(tmp_path: Path) -> None:
    native = task(tmp_path, "go")
    with (native / "task.toml").open("a") as stream:
        stream.write('repository_url = "https://github.com/example/repo"\n')
    language = CoverageSettings(
        command=["go", "test"],
        report_path="coverage.out",
        tool_version="1",
        format="go-coverprofile",
    )
    repository = language.model_copy(update={"command": ["go", "test", "./pkg"]})
    settings = MeasurementSettings(
        coverage_profiles={"go": language, "https://github.com/example/repo": repository}
    )
    assert profiles.coverage_settings(native, settings) == repository


def test_optional_contribution_does_not_silently_replace_coverage(tmp_path: Path) -> None:
    native = task(tmp_path, "python")
    contribution = profiles.TestContributionSettings(
        pytest_version="8.4.2", coverage_version="7.10.7"
    )
    ordinary = CoverageSettings(
        command=["python", "-m", "coverage"],
        report_path="coverage.json",
        format="coverage-py-json",
        tool_version="7.10.7",
    )
    with pytest.raises(ValueError, match="silently replacing"):
        capability_plan(
            native,
            ["coverage", "test_overlap"],
            MeasurementSettings(coverage=ordinary, test_contribution=contribution),
        )
    plan = capability_plan(
        native, ["coverage", "test_overlap"], MeasurementSettings(test_contribution=contribution)
    )
    assert plan["coverage"]["status"] == plan["test_overlap"]["status"] == "available"
