from fractions import Fraction
import json
import platform
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

from pydantic import ValidationError
import pytest

from pluginbench import measurement_profiles as profiles
from pluginbench.assessment_metrics import _analysis_candidates
from pluginbench.measurement_profiles import (
    CoverageSettings,
    MeasurementSettings,
    blocking_findings,
    capability_plan,
    not_measured,
    prepare_profiles,
    readiness_state,
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
        ["tokens", "lines", "cyclomatic", "coverage", "review"],
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
    collectors = prepare_profiles(source, tmp_path / "profiles", ["coverage"], settings, identity)
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


def test_fallow_version_ignores_the_npm_launcher_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = task(tmp_path, "typescript")
    monkeypatch.setattr(shutil, "which", lambda _: "/tool")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: "fallow 3.18.0\nverified: yes (cache hit at /x); fallow 3.18.0 signed\n",
    )
    result = capability_plan(native, ["cognitive"], MeasurementSettings())
    assert result["cognitive"]["status"] == "available"
    assert result["cognitive"]["tool_version"] == "3.18.0"


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
    install_binding(modules, runtime_binding())
    return modules


def runtime_binding() -> str:
    """The binding oxc-parser loads here, named as npm installs it."""
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    libc = ""
    if sys.platform == "linux":
        libc = "-gnu" if platform.libc_ver()[0] == "glibc" else "-musl"
    return f"binding-{sys.platform}-{arch}{libc}"


def install_binding(modules: Path, name: str) -> None:
    binding = modules / "@oxc-parser" / name
    binding.mkdir(parents=True)
    (binding / "binding.node").write_bytes(b"fixture binding")


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


def test_oxc_observer_accepts_the_other_libc_binding_npm_also_installs(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    modules = observer_stub(cache)
    other = "binding-linux-x64-musl" if runtime_binding().endswith("-gnu") else "binding-linux-x64-gnu"
    install_binding(modules, other)
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
        profiles.dependency_digest(modules),
    ]
    execution = subprocess.run(command, capture_output=True, text=True)
    assert execution.returncode == 0, execution.stderr


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


def test_missing_collector_is_not_configured_but_a_failing_tool_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tasks whose images ship no coverage tool are not configured, not broken.
    (tmp_path / "plain").mkdir()
    (tmp_path / "ts").mkdir()
    plain = capability_plan(
        task(tmp_path / "plain", "cobol"), ["coverage", "cyclomatic"], MeasurementSettings()
    )
    assert readiness_state(plain["coverage"]) == "not_configured"
    assert readiness_state(plain["cyclomatic"]) == "not_configured"
    monkeypatch.setattr(shutil, "which", lambda _: "/tool")
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "fallow 3.19.0")
    wrong = capability_plan(
        task(tmp_path / "ts", "typescript"), ["cognitive", "lines"], MeasurementSettings()
    )
    assert readiness_state(wrong["cognitive"]) == "broken"
    assert readiness_state(wrong["lines"]) == "ready"


@pytest.mark.parametrize("require_ready", [False, True])
def test_unready_measures_block_only_when_required_or_ready_on_no_task(
    require_ready: bool,
) -> None:
    matrix: dict[str, dict[str, dict[str, object]]] = {
        "covered": {"coverage": {"state": "ready", "reason": None}},
        "uncovered": {"coverage": {"state": "not_configured", "reason": "no coverage tool"}},
    }
    assert blocking_findings(matrix, require_ready=require_ready, max_not_ready_share=None) == (
        ["uncovered: coverage is not_configured: no coverage tool"] if require_ready else []
    )
    # Now no task can measure coverage.
    matrix["covered"]["coverage"] = {"state": "broken", "reason": "test command exited 3"}
    assert "covered: coverage is broken: test command exited 3" in blocking_findings(
        matrix, require_ready=require_ready, max_not_ready_share=None
    )


@pytest.mark.parametrize("require_ready", [False, True])
def test_a_measure_broken_on_one_task_is_not_measured_there(require_ready: bool) -> None:
    matrix: dict[str, dict[str, dict[str, object]]] = {
        "rust": {
            "dead_code": {"state": "broken", "reason": "No escaped character"},
            "cyclomatic": {"state": "ready", "reason": None},
            "oracle": {"state": "ready", "reason": None},
        },
        "python": {
            "dead_code": {"state": "ready", "reason": None},
            "cyclomatic": {"state": "ready", "reason": None},
            "oracle": {"state": "ready", "reason": None},
        },
    }

    assert blocking_findings(matrix, require_ready=require_ready, max_not_ready_share=None) == (
        ["rust: dead_code is broken: No escaped character"] if require_ready else []
    )
    assert not_measured(matrix) == [
        {"task": "rust", "measure": "dead_code", "reason": "No escaped character"}
    ]
    # Checks that protect paid attempts still block in both modes.
    matrix["python"]["oracle"] = {"state": "broken", "reason": "verifier reward is 0"}
    assert "python: oracle is broken: verifier reward is 0" in blocking_findings(
        matrix, require_ready=require_ready, max_not_ready_share=None
    )


@pytest.mark.parametrize("require_ready", [False, True])
def test_a_measure_ready_on_no_task_blocks(require_ready: bool) -> None:
    matrix: dict[str, dict[str, dict[str, object]]] = {
        "rust": {"coverage": {"state": "broken", "reason": "test command exited 3"}},
        "cobol": {"coverage": {"state": "not_configured", "reason": "no coverage tool"}},
    }

    findings = blocking_findings(matrix, require_ready=require_ready, max_not_ready_share=None)

    assert findings[-1] == "coverage is ready on no task, so the run would measure nothing for it"
    assert "rust: coverage is broken: test command exited 3" in findings
    assert not_measured(matrix) == []
    # A measure that no task configures is ready nowhere too.
    matrix["rust"]["coverage"] = {"state": "not_configured", "reason": "no coverage tool"}
    assert (
        blocking_findings(matrix, require_ready=require_ready, max_not_ready_share=None)[-1]
        == findings[-1]
    )


def _dead_code_matrix(
    tasks: int, broken: int, not_configured: int = 0
) -> dict[str, dict[str, dict[str, object]]]:
    """dead_code on `tasks` tasks: the first `broken` are broken, the next not configured."""
    states = ["broken"] * broken + ["not_configured"] * not_configured
    states += ["ready"] * (tasks - len(states))
    return {
        f"task-{index:02}": {
            "dead_code": {"state": state, "reason": None if state == "ready" else "no parser"},
            "lines": {"state": "ready", "reason": None},
        }
        for index, state in enumerate(states)
    }


@pytest.mark.parametrize(
    ("tasks", "broken", "not_configured", "blocks", "warns"),
    [
        (13, 4, 0, False, False),
        (13, 5, 0, True, False),
        # Not configured counts as not ready too.
        (13, 2, 3, True, False),
        # Exactly on the line continues, warning that any dropout during the run crosses it.
        (12, 4, 0, False, True),
        (12, 1, 3, False, True),
    ],
)
def test_a_measure_not_ready_on_more_than_the_share_of_tasks_blocks(
    tasks: int, broken: int, not_configured: int, blocks: bool, warns: bool
) -> None:
    matrix = _dead_code_matrix(tasks, broken, not_configured)
    share = Fraction(1, 3)
    unready = ", ".join(f"task-{index:02}" for index in range(broken + not_configured))

    findings = blocking_findings(matrix, require_ready=False, max_not_ready_share=share)

    assert findings == (
        [
            f"dead_code is not ready on {broken + not_configured} of {tasks} tasks, more than "
            f"max_not_ready_share 1/3: {unready}"
        ]
        if blocks
        else []
    )
    assert profiles.not_ready_share_warnings(matrix, share) == (
        [
            f"dead_code is not ready on {broken + not_configured} of {tasks} tasks, exactly "
            "max_not_ready_share 1/3, so any task that fails to measure it during the run "
            f"crosses that share: {unready}"
        ]
        if warns
        else []
    )
    # Without a share, a measure ready somewhere never blocks, as before.
    assert blocking_findings(matrix, require_ready=False, max_not_ready_share=None) == []
    assert profiles.not_ready_share_warnings(matrix, None) == []


def test_require_ready_still_blocks_each_unready_task_under_a_share() -> None:
    matrix = _dead_code_matrix(13, 1)
    share = Fraction(1, 3)

    assert blocking_findings(matrix, require_ready=True, max_not_ready_share=share) == [
        "task-00: dead_code is broken: no parser"
    ]


def test_a_measure_ready_on_no_task_blocks_once_under_a_share() -> None:
    share = Fraction(1, 3)

    findings = blocking_findings(
        _dead_code_matrix(3, 3), require_ready=False, max_not_ready_share=share
    )

    assert findings == [
        "task-00: dead_code is broken: no parser",
        "task-01: dead_code is broken: no parser",
        "task-02: dead_code is broken: no parser",
        "dead_code is ready on no task, so the run would measure nothing for it",
    ]


@pytest.mark.parametrize(
    ("value", "share"),
    [("1/3", Fraction(1, 3)), ("0.33", Fraction(33, 100)), (0.33, Fraction(33, 100)), (None, None)],
)
def test_max_not_ready_share_reads_an_exact_fraction_or_decimal(
    value: object, share: Fraction | None
) -> None:
    settings = MeasurementSettings.model_validate({"max_not_ready_share": value})

    assert settings.max_not_ready_share == share
    # Saved with the run as an exact string and read back unchanged.
    saved = settings.model_dump(mode="json")
    # Unset, it is left out, so settings saved before it existed keep their exact shape.
    assert saved.get("max_not_ready_share", "unset") == ("unset" if share is None else str(share))
    assert MeasurementSettings.model_validate(saved) == settings


@pytest.mark.parametrize("value", ["0", "1", "4/3", "-1/3", 0, 1, 1.5, True, "a third"])
def test_max_not_ready_share_rejects_values_outside_zero_and_one(value: object) -> None:
    with pytest.raises(ValidationError, match="max_not_ready_share") as error:
        MeasurementSettings.model_validate({"max_not_ready_share": value})

    assert "Extra inputs are not permitted" not in str(error.value)


@pytest.mark.parametrize(
    ("repository", "rule"),
    [("https://github.com/csstree/csstree", "process_exit"), ("https://example.invalid/x", "marker")],
)
def test_mutation_profile_freezes_every_finder_the_host_prepares_and_the_status_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: str, rule: str
) -> None:
    from pluginbench import language_analysis

    native = tmp_path / "native"
    native.mkdir()
    (native / "task.toml").write_text(
        f'[metadata]\nlanguage = "python"\nrepository_url = "{repository}"\n'
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print(1)\n")
    oxc = {"path": "/cache/oxc/node_modules", "sha256": "oxc"}
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(profiles, "prepare_observer", lambda *args: oxc)

    def no_go(*args: object) -> Path:
        raise subprocess.CalledProcessError(1, ["go", "build"])

    monkeypatch.setattr(language_analysis, "prepare_go_observer", no_go)
    settings = MeasurementSettings(
        coverage=CoverageSettings(
            command=["bash", "-c", "pytest; echo PB-SUITE-EXIT $?"],
            report_path="coverage.info",
            tool_version="pytest-cov 7.0",
        )
    )
    identity: dict[str, object] = {"task_dir": str(native), "workdir": "/app"}
    [collector] = prepare_profiles(source, tmp_path / "profiles", ["mutation"], settings, identity)
    frozen = json.loads(collector.config.read_text())
    # A Python task still parses changed JavaScript and TypeScript; this host cannot build the
    # Go finder, so a changed Go file would leave its attempt incomplete.
    assert frozen["site_finders"] == {"oxc": oxc}
    assert frozen["suite_status"] == rule
