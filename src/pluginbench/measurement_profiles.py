"""Freeze reusable measurement settings independently of benchmark task names."""

from __future__ import annotations

from collections.abc import Iterable
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pluginbench.assessment_metrics import (
    inspect_snapshot,
    evaluate_evidence,
    _dead_changes,
    _object,
)
from pluginbench.code_diff import TEST_PATH_PATTERNS, _is_source_path, _is_test_path
from pluginbench.collectors import Collector, collect_measurements, observer_identity

ROOT = Path(__file__).resolve().parent
MEASUREMENT_NAMES = frozenset(
    {
        "tokens",
        "lines",
        "cyclomatic",
        "cognitive",
        "dead_code",
        "coverage",
        "test_overlap",
        "review",
    }
)
FALLOW_MEASUREMENTS = {"cyclomatic", "cognitive", "dead_code"}
PROFILE_VERSION = "native-before-after-v2"


def _validate_scopes(scopes: Iterable[list[str] | None]) -> None:
    for scope in scopes:
        if scope and any(
            not pattern or pattern.startswith("/") or ".." in PurePosixPath(pattern).parts
            for pattern in scope
        ):
            raise ValueError("measurement scopes require nonempty repository-relative patterns")


class CoverageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1)
    report_path: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    format: Literal["lcov", "coverage-py-json", "go-coverprofile"] = "lcov"
    timeout_seconds: int = Field(default=600, ge=1, le=7200)
    source_scope: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def safe_report_path(self) -> CoverageSettings:
        report = PurePosixPath(self.report_path)
        if (
            report.is_absolute()
            or ".." in report.parts
            or report.as_posix() in {".", ""}
            or "\\" in self.report_path
            or any(not item.strip() for item in self.command)
            or not self.tool_version.strip()
        ):
            raise ValueError(
                "coverage requires a relative report path and nonempty command/version"
            )
        _validate_scopes([self.source_scope])
        return self


class TestContributionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python: str = Field(default="python", min_length=1)
    pytest_args: list[str] = Field(default_factory=list)
    pytest_version: str = Field(min_length=1)
    coverage_version: str = Field(min_length=1)
    timeout: int = Field(default=600, ge=1, le=7200)


class MeasurementSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_scope: list[str] | None = Field(default=None, min_length=1)
    test_scope: list[str] | None = Field(default=None, min_length=1)
    fallow_config: Path | None = None
    coverage: CoverageSettings | None = None
    coverage_profiles: dict[str, CoverageSettings] = Field(default_factory=dict)
    test_contribution: TestContributionSettings | None = None
    require_ready: bool = False

    @model_validator(mode="after")
    def safe_scopes(self) -> MeasurementSettings:
        _validate_scopes([self.source_scope, self.test_scope])
        return self


def selected_measurements(selection: Iterable[str] | None) -> frozenset[str]:
    selected = MEASUREMENT_NAMES - {"test_overlap"} if selection is None else frozenset(selection)
    unknown = selected - MEASUREMENT_NAMES
    if unknown:
        raise ValueError("unsupported measurements: " + ", ".join(sorted(unknown)))
    return selected


def capability_plan(
    task_dir: Path, selected: Iterable[str], settings: MeasurementSettings
) -> dict[str, dict[str, object]]:
    metadata = tomllib.loads((task_dir / "task.toml").read_text()).get("metadata", {})
    language = str(metadata.get("language", "")).strip().lower()
    selected = selected_measurements(selected)
    is_fallow = language in {"javascript", "typescript", "javascript/typescript", "js", "ts"}
    fallow_reason, fallow_version = (
        _fallow_capability(language)
        if selected & FALLOW_MEASUREMENTS and is_fallow
        else (None, None)
    )
    analysis = {}
    if selected & FALLOW_MEASUREMENTS and not is_fallow:
        from pluginbench.language_analysis import language_capability

        analysis = language_capability(language)
    coverage = coverage_settings(task_dir, settings)
    contribution = (
        "test_overlap" in selected
        and settings.test_contribution is not None
        and language == "python"
    )
    if contribution and "coverage" in selected and coverage is not None:
        raise ValueError(
            "pytest test_overlap supplies its own reference coverage; omit the ordinary coverage profile for this repository rather than silently replacing it"
        )
    result: dict[str, dict[str, object]] = {}
    for name in sorted(selected):
        reason = None
        collector = None
        if name in FALLOW_MEASUREMENTS:
            collector = "fallow" if is_fallow else "analysis"
            reason = fallow_reason if is_fallow else analysis.get(name, {}).get("reason")
        elif name == "coverage":
            collector = "coverage"
            if coverage is None and not contribution:
                reason = "Coverage requires an explicit test command, report format/path and tool version"
        elif name == "test_overlap":
            collector = "coverage"
            if settings.test_contribution is None or language != "python":
                reason = "Test overlap requires per-test execution evidence; configure pytest test_contribution for Python"
        result[name] = {
            "status": "unavailable" if reason else "available",
            "reason": reason,
            **({"collector": collector} if collector else {}),
            **({"tool_version": fallow_version} if collector == "fallow" else {}),
        }
    return result


def coverage_settings(task_dir: Path, settings: MeasurementSettings) -> CoverageSettings | None:
    """Explicit repository profile wins over language profile and shared default."""
    metadata = tomllib.loads((task_dir / "task.toml").read_text()).get("metadata", {})
    repository = str(metadata.get("repository_url", ""))
    language = str(metadata.get("language", "")).lower()
    return settings.coverage_profiles.get(
        repository, settings.coverage_profiles.get(language, settings.coverage)
    )


def _fallow_capability(language: str) -> tuple[str | None, str | None]:
    if language not in {"javascript", "typescript", "javascript/typescript", "js", "ts"}:
        return (
            "Fallow supports JavaScript/TypeScript; this task does not declare that language",
            None,
        )
    if shutil.which("fallow") is None or shutil.which("node") is None:
        return "Fallow and Node are required for source analysis", None
    try:
        version = subprocess.check_output(["fallow", "--version"], text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not determine Fallow version: {exc}", None
    if version != "fallow 3.18.0":
        return f"Analysis requires Fallow 3.18.0; observed {version}", version
    return None, "3.18.0"


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def dependency_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            content = b"link:" + os.readlink(path).encode()
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def _observer_cache_safe(root: Path, destination: Path) -> bool:
    return destination.is_relative_to(root) and destination.resolve() == destination


def prepare_observer(name: str, cache: Path) -> dict[str, str]:
    """Reuse only an exact locked parser manifest, lock, and installed byte population."""
    if name != "oxc-parser-0.126.0":
        raise ValueError("only the pinned generic syntax observer is supported")
    source = ROOT / "observer_data" / name
    declared = {
        file: (source / file).read_bytes() for file in ("package.json", "package-lock.json")
    }
    lock_digest = hashlib.sha256(declared["package-lock.json"]).hexdigest()
    cache_root = cache.expanduser().resolve()
    destination = cache_root / name / lock_digest
    if not _observer_cache_safe(cache_root, destination) or not _observer_cache_safe(
        cache_root, destination / "node_modules"
    ):
        raise ValueError("observer cache descendants must not redirect outside the cache")
    if destination.exists():
        if any((destination / file).read_bytes() != content for file, content in declared.items()):
            raise ValueError("observer cache manifest or lock changed")
        expected = json.loads((destination / "receipt.json").read_text())
        if (
            expected["lock_sha256"] != lock_digest
            or not (destination / "node_modules").is_dir()
            or dependency_digest(destination / "node_modules") != expected["sha256"]
        ):
            raise ValueError("observer dependency cache changed")
    else:
        destination.mkdir(parents=True)
        try:
            for file, content in declared.items():
                (destination / file).write_bytes(content)
            subprocess.run(
                ["corepack", "npm@10.9.2", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=destination,
                env={**os.environ, "npm_config_userconfig": "/dev/null"},
                check=True,
            )
            expected = {
                "lock_sha256": lock_digest,
                "sha256": dependency_digest(destination / "node_modules"),
            }
            _json(destination / "receipt.json", expected)
        except Exception:
            if _observer_cache_safe(cache_root, destination):
                shutil.rmtree(destination)
            raise
    return {"path": str((destination / "node_modules").resolve()), "sha256": expected["sha256"]}


def _fallow_config(snapshot: Path, settings: MeasurementSettings) -> dict[str, object]:
    config: dict[str, object] = {}
    candidate = settings.fallow_config
    if candidate is None:
        candidate = next(
            (
                snapshot / name
                for name in (".fallowrc.json", ".fallowrc.jsonc")
                if (snapshot / name).is_file()
            ),
            None,
        )
    if candidate is not None:
        if candidate.is_symlink():
            raise ValueError("Fallow settings must not be a symlink")
        raw = json.loads(candidate.read_text())
        if not isinstance(raw, dict) or raw.get("extends"):
            raise ValueError("Fallow settings require fully resolved JSON without extends")
        config.update(raw)
    config["production"] = True
    config["health"] = {"maxCognitive": 0, "maxCyclomatic": 0, "maxUnitSize": 0}
    if "ignorePatterns" not in config:
        config["ignorePatterns"] = list(TEST_PATH_PATTERNS)
    # Root inference remains Fallow's responsibility. Never promote every file to an entry.
    return config


def prepare_profiles(
    snapshot: Path,
    output: Path,
    selected: Iterable[str],
    settings: MeasurementSettings,
    native_task_identity: dict[str, object],
) -> list[Collector]:
    selected = selected_measurements(selected)
    task_dir = native_task_identity.get("task_dir")
    if not isinstance(task_dir, str):
        raise ValueError("native measurement identity requires frozen task_dir")
    capabilities = capability_plan(Path(task_dir), selected, settings)
    output.mkdir(parents=True, exist_ok=True)
    source_scope = settings.source_scope or ["*"]
    test_scope = settings.test_scope or ["*"]
    collectors = []
    analysis_available = any(
        capabilities[name]["status"] == "available"
        and capabilities[name].get("collector") == "analysis"
        for name in selected & FALLOW_MEASUREMENTS
    )
    if analysis_available:
        from pluginbench.language_analysis import prepare_analysis_config

        metadata = tomllib.loads((Path(task_dir) / "task.toml").read_text()).get("metadata", {})
        language = str(metadata.get("language", "")).lower()
        analysis_config = output / "analysis.json"
        _json(
            analysis_config,
            prepare_analysis_config(
                language,
                source_scope=source_scope,
                test_scope=test_scope,
                native_task_identity=native_task_identity,
            ),
        )
        collectors.append(
            Collector(
                kind="analysis",
                config=analysis_config,
                scope=source_scope,
                tool_version="language-analysis-v1",
                timeout_seconds=7200,
            )
        )
    if any(
        capabilities[name]["status"] == "available"
        and capabilities[name].get("collector") == "fallow"
        for name in selected & FALLOW_MEASUREMENTS
    ):
        cache = (
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pluginbench/observers"
        )
        parser = prepare_observer("oxc-parser-0.126.0", cache)
        if Path(parser["path"]).is_relative_to(snapshot.resolve()):
            raise ValueError("syntax observer cache must be outside measured source")
        fallow_config = output / "fallow.json"
        _json(fallow_config, _fallow_config(snapshot, settings))
        collectors.append(
            Collector(
                kind="fallow",
                config=fallow_config,
                scope=source_scope,
                syntax_command=[
                    "node",
                    str(ROOT / "fallow_parse.mjs"),
                    str(Path(parser["path"]).parent),
                    "{snapshot}",
                    "{inputs}",
                    "{output}",
                    parser["sha256"],
                ],
                tool_version="3.18.0",
                timeout_seconds=7200,
            )
        )
    contribution = (
        "test_overlap" in selected and capabilities["test_overlap"]["status"] == "available"
    )
    if contribution:
        configured = settings.test_contribution
        assert configured is not None
        coverage_config = output / "coverage.json"
        _json(
            coverage_config,
            {
                "native_task_identity": native_task_identity,
                "source_scope": source_scope,
                "test_scope": test_scope,
                "test_contribution": configured.model_dump(mode="json"),
                "implementation": observer_identity("pytest-contribution"),
            },
        )
        collectors.append(
            Collector(
                kind="coverage",
                config=coverage_config,
                scope=source_scope,
                report_format="pytest-contribution",
                report_path=".pluginbench-measure/contribution.json",
                tool_version=f"pytest-{configured.pytest_version}/coverage-{configured.coverage_version}",
                timeout_seconds=configured.timeout,
            )
        )
    elif "coverage" in selected and capabilities["coverage"]["status"] == "available":
        coverage = coverage_settings(Path(task_dir), settings)
        assert coverage is not None
        coverage_scope = coverage.source_scope or source_scope
        coverage_config = output / "coverage.json"
        _json(
            coverage_config,
            {
                "native_task_identity": native_task_identity,
                "source_scope": coverage_scope,
                "test_scope": test_scope,
                "coverage": coverage.model_dump(mode="json"),
                "implementation": observer_identity(coverage.format),
            },
        )
        collectors.append(
            Collector(
                kind="coverage",
                config=coverage_config,
                scope=coverage_scope,
                command=coverage.command,
                report_format=coverage.format,
                report_path=coverage.report_path,
                tool_version=coverage.tool_version,
                timeout_seconds=coverage.timeout_seconds,
            )
        )
    inventory = inspect_snapshot(snapshot)["files"]
    assert isinstance(inventory, dict)
    _json(output / "measurement-capabilities.json", capabilities)
    _json(
        output / "measurement-provenance.json",
        {
            "measurement_profile_version": PROFILE_VERSION,
            "source_scope": json.dumps(source_scope),
            "test_scope": json.dumps(test_scope),
            "selected_measurements": json.dumps(sorted(selected)),
            "starting_source_paths": sorted(
                path
                for path in inventory
                if _is_source_path(path)
                and any(fnmatch.fnmatchcase(path, pattern) for pattern in source_scope)
            ),
            "starting_test_paths": sorted(
                path
                for path in inventory
                if _is_test_path(path)
                and any(fnmatch.fnmatchcase(path, pattern) for pattern in test_scope)
            ),
            "capabilities": capabilities,
        },
    )
    return collectors


def _metric_readiness(metric: str, first: dict[str, object]) -> tuple[bool, object]:
    ready, reason = True, None
    if metric in {"cognitive", "cyclomatic", "dead_code"}:
        statuses = first.get("metric_statuses")
        if isinstance(statuses, dict):
            status = statuses.get(metric)
            ready = isinstance(status, dict) and status.get("status") == "available"
            reason = status.get("reason") if isinstance(status, dict) else "missing metric status"
        else:
            key = (
                "dead_code_complete_for_declared_scope"
                if metric == "dead_code"
                else "complexity_complete_for_declared_scope"
            )
            ready = first.get(key) is True
            reason = None if ready else "analysis population is incomplete"
        if metric == "dead_code" and ready:
            comparison = _dead_changes(first, first)
            ready = comparison["status"] == "available"
            reason = comparison.get("reason")
    elif metric == "coverage":
        ready = any(
            first.get(f"{unit}_population_complete") is True
            for unit in ("line", "statement", "branch", "function")
        )
        reason = None if ready else "no complete supported coverage population"
    elif metric == "test_overlap":
        contribution = first.get("test_contribution")
        ready = isinstance(contribution, dict) and contribution.get("status") == "available"
        reason = None if ready else "no complete passing contribution reference"
    return ready, reason


def probe_measurements(
    snapshot: Path,
    output: Path,
    collectors: list[Collector],
    selected: Iterable[str],
    capabilities: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Exercise fixed collectors twice before coding; malformed evidence stays fatal."""
    repeats = []
    for index in range(2):
        directory = output / str(index)
        directory.mkdir(parents=True)
        refs = collect_measurements(snapshot, directory, collectors, before=snapshot)
        repeats.append(
            evaluate_evidence(snapshot, {kind: str(path) for kind, path in refs.items()}, directory)
        )
    result = {}
    for metric in sorted(selected_measurements(selected)):
        capability = _object(capabilities[metric], "measurement capability")
        kind = capability.get("collector")
        if not isinstance(kind, str):
            result[metric] = {**capability, "readiness": "not_applicable"}
            continue
        measurements = [repeat.get(kind) for repeat in repeats]
        values = [row.get("metrics") if isinstance(row, dict) else None for row in measurements]
        reason = capability.get("reason")
        ready = capability["status"] == "available" and all(
            isinstance(value, dict) for value in values
        )
        if ready:
            first = values[0]
            assert isinstance(first, dict)
            ready, reason = _metric_readiness(metric, first)
            if ready and values[0] != values[1]:
                ready, reason = False, "prepared-input measurements differed across two repeats"
        elif not reason:
            failures = [
                json.loads((output / str(index) / kind / "failure.json").read_text())
                for index in range(2)
                if (output / str(index) / kind / "failure.json").exists()
            ]
            reason = (
                "; ".join(str(failure["reason"]) for failure in failures) or "no bound measurement"
            )
        result[metric] = {
            **capability,
            "status": "available" if ready else "unavailable",
            "readiness": "passed" if ready else "failed",
            "reason": reason,
            "repeats": 2,
            **(
                {"test_command_exit_code": values[0].get("test_command_exit_code")}
                if metric == "coverage" and isinstance(values[0], dict)
                else {}
            ),
        }
    _json(output / "readiness.json", result)
    return result
