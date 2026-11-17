"""Freeze reusable measurement settings independently of benchmark task names."""

from __future__ import annotations

from collections.abc import Iterable
import fnmatch
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tomllib
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from pluginbench.assessment_metrics import (
    inspect_snapshot,
    evaluate_evidence,
    _dead_changes,
    _object,
)
from pluginbench.code_diff import TEST_PATH_PATTERNS, _is_source_path, _is_test_path
from pluginbench import components
from pluginbench.collectors import Collector, collect_measurements, observer_identity

ROOT = Path(__file__).resolve().parent
MEASUREMENT_NAMES = frozenset(
    {
        "tokens",
        "lines",
        "iterations",
        "cyclomatic",
        "cognitive",
        "dead_code",
        "coverage",
        "mutation",
        "review",
    }
)
REMOVED_TEST_OVERLAP = (
    "test_overlap was removed: the leave-one-out test-overlap measure is no longer offered. "
    "Redundant added tests, measured from per-test coverage, replace it. "
    "Remove test_overlap from measurements."
)
REMOVED_TEST_CONTRIBUTION = (
    "measurement_settings.test_contribution was removed: it configured the leave-one-out "
    "test-overlap measure, which is no longer offered. Redundant added tests, measured from "
    "per-test coverage, replace it. Omit test_contribution or set it to null."
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

    command: list[str] = Field(
        min_length=1, description="Provides the coverage command as a nonempty argument list."
    )
    report_path: str = Field(
        min_length=1,
        description="Names the coverage report relative to the repository without parent traversal.",
    )
    tool_version: str = Field(
        min_length=1,
        description="Records the nonempty coverage tool version in the measurement profile.",
    )
    format: Literal["lcov", "coverage-py-json", "go-coverprofile"] = Field(
        default="lcov", description="Selects the parser for the coverage report."
    )
    timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=7200,
        description="Sets the coverage command timeout in seconds from one to 7200.",
    )
    source_scope: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Selects repository-relative source patterns for this coverage profile.",
    )

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


# Analysis and Fallow each run one command, at most this long.
OBSERVER_TIMEOUT = 7200
# Readiness collects every measure this many times before coding.
READINESS_REPEATS = 2


class MeasurementSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_scope: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Selects repository-relative source patterns for measurements.",
    )
    test_scope: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Selects repository-relative test patterns for measurements.",
    )
    fallow_config: Path | None = Field(
        default=None,
        description="Selects a Fallow configuration file relative to the evaluation config.",
    )
    coverage: CoverageSettings | None = Field(
        default=None,
        description="Provides the default coverage profile when no repository or language profile matches.",
    )
    coverage_profiles: dict[str, CoverageSettings] = Field(
        default_factory=dict,
        description="Maps repository URLs or language names to coverage profiles, with repository matches taking precedence.",
    )
    # Saved evaluations record this removed setting as null; only null is accepted.
    test_contribution: None = Field(
        default=None,
        description="Retains the removed test contribution setting as null for saved evaluations.",
    )
    require_ready: bool = Field(
        default=False,
        description="Requires every selected measurement to be ready on every task before coding.",
    )
    max_not_ready_share: Fraction | None = Field(
        default=None,
        description=(
            "Stops the check when a selected measurement is not ready on more than this share "
            'of tasks, given as an exact fraction such as "1/3" or a decimal such as 0.25.'
        ),
    )

    @field_validator("test_contribution", mode="before")
    @classmethod
    def removed_test_contribution(cls, value: object) -> None:
        if value is not None:
            raise ValueError(REMOVED_TEST_CONTRIBUTION)
        return None

    @field_validator("max_not_ready_share", mode="before")
    @classmethod
    def decimal_share(cls, value: object) -> object:
        # An unquoted YAML 0.33 arrives as a float; read it as the decimal written, 33/100.
        return Fraction(str(value)) if isinstance(value, float) else value

    @field_validator("max_not_ready_share")
    @classmethod
    def proper_share(cls, value: Fraction | None) -> Fraction | None:
        if value is not None and not 0 < value < 1:
            raise ValueError("max_not_ready_share must be greater than 0 and less than 1")
        return value

    @model_validator(mode="after")
    def safe_scopes(self) -> MeasurementSettings:
        _validate_scopes([self.source_scope, self.test_scope])
        return self

    @model_serializer(mode="wrap")
    def omit_unset_share(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        # Saved runs and comparison identities without a share keep their earlier shape, so an
        # earlier build still reads them and refuses them by schema version, not this new key.
        data: dict[str, object] = handler(self)
        if self.max_not_ready_share is None:
            data.pop("max_not_ready_share", None)
        return data


def selected_measurements(selection: Iterable[str] | None) -> frozenset[str]:
    selected = MEASUREMENT_NAMES if selection is None else frozenset(selection)
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
    result: dict[str, dict[str, object]] = {}
    for name in sorted(selected):
        reason = None
        collector = None
        # A measure with no collector for this task is not configured; a configured one that
        # cannot run (missing or wrong tool) is broken. See readiness_state.
        configured = True
        if name in FALLOW_MEASUREMENTS:
            collector = "fallow" if is_fallow else "analysis"
            reason = fallow_reason if is_fallow else analysis.get(name, {}).get("reason")
            configured = is_fallow or analysis.get(name, {}).get("configured") is not False
        elif name == "coverage":
            collector = "coverage"
            if coverage is None:
                reason = "Coverage requires an explicit test command, report format/path and tool version"
                configured = False
        elif name == "mutation":
            collector = "mutation"
            reason, configured = _mutation_capability(language, coverage)
        result[name] = {
            "status": "unavailable" if reason else "available",
            "reason": reason,
            **({"collector": collector} if collector else {}),
            **({"tool_version": fallow_version} if collector == "fallow" else {}),
            **({} if configured else {"configured": False}),
        }
    return result


def _mutation_capability(
    language: str, coverage: CoverageSettings | None
) -> tuple[str | None, bool]:
    """Why mutation cannot run on this task, if so, and whether it is configured at all."""
    from pluginbench.mutation import LANGUAGES

    if language not in LANGUAGES:
        return "Mutation supports Python, Go, TypeScript and JavaScript tasks", False
    if coverage is None:
        return "Mutation runs the task's coverage profile command; configure one", False
    tool = "go" if language == "go" else "node" if language != "python" else None
    if tool and shutil.which(tool) is None:
        return f"Mutation sites in {language} need {tool} to run the pinned site finder", True
    return None, True


def readiness_state(row: dict[str, object]) -> str:
    """ready, not_configured (no collector for this task), or broken (configured but failing)."""
    if row.get("status") == "available":
        return "ready"
    return "not_configured" if row.get("configured") is False else "broken"


def _ready_nowhere(matrix: dict[str, dict[str, dict[str, object]]]) -> list[str]:
    """Selected measures that no task can measure: a run would produce nothing for them."""
    measures = dict.fromkeys(
        name for rows in matrix.values() for name in rows if name in MEASUREMENT_NAMES
    )
    return [
        name
        for name in measures
        if all(rows[name].get("state") != "ready" for rows in matrix.values() if name in rows)
    ]


def _not_ready_shares(
    matrix: dict[str, dict[str, dict[str, object]]],
) -> dict[str, tuple[list[str], int]]:
    """For each selected measure, the tasks where it is not ready and the number of tasks."""
    shares: dict[str, tuple[list[str], int]] = {}
    for task, rows in matrix.items():
        for name, row in rows.items():
            if name in MEASUREMENT_NAMES:
                tasks, total = shares.get(name, ([], 0))
                shares[name] = ([*tasks, task] if row["state"] != "ready" else tasks, total + 1)
    return shares


def blocking_findings(
    matrix: dict[str, dict[str, dict[str, object]]],
    *,
    require_ready: bool,
    max_not_ready_share: Fraction | None,
) -> list[str]:
    """Rows that stop the run before any coding call.

    A broken check of the coding environment, submission source or reference agents blocks. A
    broken measure blocks only when no task can measure it, or when it is not ready (broken or
    not configured) on more than max_not_ready_share of the tasks; otherwise that task is recorded
    as not measured (see not_measured). require_ready makes every row that is not ready block.
    """
    nowhere = _ready_nowhere(matrix)
    findings = [
        f"{task}: {name} is {row['state']}: {row.get('reason')}"
        for task, rows in matrix.items()
        for name, row in rows.items()
        if row["state"] != "ready"
        and (
            require_ready
            or (name in nowhere if name in MEASUREMENT_NAMES else row["state"] == "broken")
        )
    ]
    if max_not_ready_share is not None:
        findings += [
            f"{name} is not ready on {len(tasks)} of {total} tasks, more than "
            f"max_not_ready_share {max_not_ready_share}: {', '.join(tasks)}"
            for name, (tasks, total) in _not_ready_shares(matrix).items()
            # A measure ready nowhere already blocks below with a clearer reason.
            if name not in nowhere and Fraction(len(tasks), total) > max_not_ready_share
        ]
    return findings + [
        f"{name} is ready on no task, so the run would measure nothing for it"
        for name in nowhere
    ]


def not_ready_share_warnings(
    matrix: dict[str, dict[str, dict[str, object]]], max_not_ready_share: Fraction | None
) -> list[str]:
    """Measures not ready on exactly max_not_ready_share of tasks: one more dropout would block."""
    if max_not_ready_share is None:
        return []
    return [
        f"{name} is not ready on {len(tasks)} of {total} tasks, exactly max_not_ready_share "
        f"{max_not_ready_share}, so any task that fails to measure it during the run crosses "
        f"that share: {', '.join(tasks)}"
        for name, (tasks, total) in _not_ready_shares(matrix).items()
        if Fraction(len(tasks), total) == max_not_ready_share
    ]


def not_measured(matrix: dict[str, dict[str, dict[str, object]]]) -> list[dict[str, object]]:
    """Broken measures that the run skips on their task while other tasks still measure them."""
    nowhere = _ready_nowhere(matrix)
    return [
        {"task": task, "measure": name, "reason": row.get("reason")}
        for task, rows in matrix.items()
        for name, row in rows.items()
        if name in MEASUREMENT_NAMES and name not in nowhere and row.get("state") == "broken"
    ]


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
        output = subprocess.check_output(["fallow", "--version"], text=True, timeout=5)
        # The npm launcher appends a `verified: ...` status line after the version.
        version = (output.strip().splitlines() or [""])[0].strip()
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
        from pluginbench.language_analysis import VERSION, prepare_analysis_config

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
                tool_version=VERSION,
                timeout_seconds=OBSERVER_TIMEOUT,
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
                timeout_seconds=OBSERVER_TIMEOUT,
            )
        )
    if "coverage" in selected and capabilities["coverage"]["status"] == "available":
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
                "observer_version": components.VERSIONS["coverage_observer"],
                "implementation": observer_identity(),
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
    if "mutation" in selected and capabilities["mutation"]["status"] == "available":
        collectors.append(
            _mutation_collector(Path(task_dir), output, settings, native_task_identity)
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


def _mutation_collector(
    task_dir: Path,
    output: Path,
    settings: MeasurementSettings,
    native_task_identity: dict[str, object],
) -> Collector:
    """Freeze the coverage command, site finder and source-only file split mutation uses."""
    from pluginbench import mutation, source_only
    from pluginbench.language_analysis import prepare_go_observer

    metadata = tomllib.loads((task_dir / "task.toml").read_text()).get("metadata", {})
    language = str(metadata.get("language", "")).strip().lower()
    repository = str(metadata.get("repository_url", ""))
    coverage = coverage_settings(task_dir, settings)
    assert coverage is not None
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pluginbench"

    def go_finder() -> dict[str, str]:
        binary = prepare_go_observer(
            cache / "language-tools/go-observer", mutation.GO_OBSERVER, "pluginbench-go-mutation"
        )
        return {"path": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}

    # A finder for each supported language an attempt may change. The task language's finder is
    # required; another one this host cannot prepare is left out, so a changed file in that
    # language stays unparsed and makes its attempt incomplete.
    finders: dict[str, dict[str, str]] = {}
    for kind, prepare, tool, languages in (
        ("go", go_finder, "go", {"go"}),
        (
            "oxc",
            lambda: prepare_observer("oxc-parser-0.126.0", cache / "observers"),
            "node",
            {"typescript", "javascript"},
        ),
    ):
        if language in languages:
            finders[kind] = prepare()
        elif shutil.which(tool) is not None:
            try:
                finders[kind] = prepare()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    try:
        # The files the source-only grade keeps although they look like tests.
        reference: list[str] | None = sorted(source_only._task_paths(task_dir)[1])
    except (OSError, ValueError):
        reference = None
    config = output / "mutation.json"
    _json(
        config,
        {
            "native_task_identity": native_task_identity,
            "language": language,
            "repository_url": repository,
            "coverage": coverage.model_dump(mode="json"),
            "generated_files": list(mutation.GENERATED_FILES.get(repository, ())),
            "suite_status": "process_exit"
            if repository in mutation.PROCESS_EXIT_REPOSITORIES
            else "marker",
            "site_finders": finders,
            "source_only_reference_paths": reference,
            "observer_version": components.VERSIONS["mutation_observer"],
            "implementation": mutation.observer_identity(),
        },
    )
    return Collector(
        kind="mutation",
        config=config,
        scope=["*"],
        tool_version=mutation.VERSION,
        timeout_seconds=coverage.timeout_seconds,
    )


def _metric_readiness(metric: str, first: dict[str, object]) -> tuple[bool, object]:
    ready, reason = True, None
    if metric in {"cognitive", "cyclomatic", "dead_code"}:
        statuses = first.get("metric_statuses")
        if isinstance(statuses, dict):
            status = statuses.get(metric)
            ready = isinstance(status, dict) and status.get("status") == "available"
            reason = status.get("reason") if isinstance(status, dict) else "missing metric status"
            unanalyzed = first.get("complexity_unanalyzed")
            if ready and metric != "dead_code" and isinstance(unanalyzed, dict) and unanalyzed:
                # Changed files are still measured; an attempt that changes one of these is
                # recorded as incomplete.
                reason = "declared source files without complexity: " + "; ".join(
                    f"{path} ({detail})" for path, detail in sorted(unanalyzed.items())
                )
        else:
            key = (
                "dead_code_complete_for_declared_scope"
                if metric == "dead_code"
                else "complexity_complete_for_declared_scope"
            )
            ready = first.get(key) is True
            reason = None if ready else "analysis population is incomplete"
            if metric == "dead_code" and not ready:
                reason = first.get("dead_code_incomplete_reason") or reason
        if metric == "dead_code" and ready:
            comparison = _dead_changes(first, first, set())
            ready = comparison["status"] == "available"
            reason = comparison.get("reason")
            excluded = first.get("dead_code_excluded")
            if ready and isinstance(excluded, dict) and excluded:
                # Findings in the build are still compared; an attempt that changes one of these
                # files, or moves a file into or out of the build, is recorded as incomplete.
                build = first.get("dead_code_build")
                assert isinstance(build, dict)  # _analysis requires it beside excluded files
                settings = " ".join(f"{key}={value}" for key, value in build.items())
                reason = f"declared source files outside the dead-code build ({settings}): " + (
                    "; ".join(f"{path} ({detail})" for path, detail in sorted(excluded.items()))
                )
    elif metric == "mutation":
        # The starting source changes no line, so a report with no candidates is ready.
        ready = first.get("status") == "available" or first.get("candidate_count") == 0
        reason = None if ready else first.get("reason")
    elif metric == "coverage":
        ready = any(
            first.get(f"{unit}_population_complete") is True
            for unit in ("line", "statement", "branch", "function")
        )
        reason = None if ready else "no complete supported coverage population"
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
    for index in range(READINESS_REPEATS):
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
            result[metric] = {
                **capability,
                "state": readiness_state(capability),
                "readiness": "not_applicable",
            }
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
        row = {
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
        result[metric] = {**row, "state": readiness_state(row)}
    _json(output / "readiness.json", result)
    return result
