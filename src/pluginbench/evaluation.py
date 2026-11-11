"""One evaluation: prepare tasks, run vanilla and guidance, measure, optionally review."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from collections.abc import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from pluginbench import (
    baselines,
    claude,
    codex,
    components,
    harbor_backend,
    isolation,
    run_lock,
    submissions,
)
from pluginbench.treatments import Treatment, resolve_treatment, treatment_inventory
from pluginbench.native_treatments import NativeCapabilities, DELIVERY_POLICY, admit
from pluginbench.task_sources import Tasks, cache_root
from pluginbench.measurement_profiles import (
    REMOVED_TEST_OVERLAP,
    MeasurementSettings,
    blocking_findings,
    not_measured,
    capability_plan,
    prepare_profiles,
    probe_measurements,
    readiness_state,
)
from pluginbench.assessment_metrics import scoped_code_size, scoped_test_size
from pluginbench.datasets import TaskCatalog, TaskEntry, load_tasks, portable_native_identity
from pluginbench.collectors import Collector
from pluginbench.evaluation_report import write_evaluation_report
from pluginbench.remeasure import (
    _native_state,
    _object,
    _preserved_inventory,
    load_profiles,
    propose_remeasurement,
    validate_remeasurement,
)
from pluginbench import study
from pluginbench.study import _text_files, load_study, report_study, resume_study, run_study
from pluginbench.study_review import (
    _criteria_digest,
    _fixed_criteria,
    _review_directory,
    recover_review_citations,
    review_study,
    validate_review_revision,
)

Measurement = Literal[
    "tokens", "lines", "cyclomatic", "cognitive", "dead_code", "coverage", "review"
]


class Runner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    harness: Literal["codex", "claude"]
    model: str = Field(min_length=1)
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

    @model_validator(mode="after")
    def supported_effort(self) -> Runner:
        if self.harness == "claude" and re.fullmatch(r"claude-[a-z0-9-]+", self.model) is None:
            raise ValueError(
                "Claude requires an exact claude-* model ID; floating aliases such as sonnet or opus cannot be compared reliably"
            )
        if self.harness == "claude" and self.reasoning not in {
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Claude reasoning must be low, medium, high, xhigh or max")
        return self


class Reviewer(Runner):
    timeout_seconds: int | None = Field(default=None, strict=True, ge=1)
    guidance: list[Path] = Field(default_factory=list)


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="evaluation", min_length=1)
    runner: Runner
    treatments: dict[str, Treatment]
    capabilities: NativeCapabilities = Field(default_factory=NativeCapabilities)
    tasks: Tasks
    measurements: list[Measurement] = Field(min_length=1)
    measurement_settings: MeasurementSettings = Field(default_factory=MeasurementSettings)
    reviewer: Reviewer | None = None
    repeats: int = Field(default=3, strict=True, ge=1)
    seed: int = Field(default=42, strict=True)
    timeout_seconds: int = Field(default=1800, strict=True, ge=1)
    # A fixed default keeps the comparison identity the same on every machine; 4 is the
    # most harbor_backend.capacity_jobs ever allows.
    jobs: int = Field(default=2, strict=True, ge=1, le=4)
    container_cpus: int | None = Field(default=None, strict=True, ge=1)
    container_memory_mb: int | None = Field(default=None, strict=True, ge=256)

    @model_validator(mode="before")
    @classmethod
    def legacy_guidance(cls, value: object) -> object:
        return study.normalize_legacy_guidance(value)

    @field_validator("measurements", mode="before")
    @classmethod
    def removed_measurements(cls, value: object) -> object:
        if isinstance(value, list) and "test_overlap" in value:
            raise ValueError(REMOVED_TEST_OVERLAP)
        return value

    @model_validator(mode="after")
    def coherent(self) -> Evaluation:
        if self.runner.reasoning == "ultra":
            raise ValueError("Harbor does not support ultra reasoning; select a supported effort")
        if len(set(self.measurements)) != len(self.measurements):
            raise ValueError("measurements must be unique")
        if ("review" in self.measurements) != (self.reviewer is not None):
            raise ValueError("provide reviewer settings exactly when review is selected")
        if any(
            not name.strip()
            or name in {"baseline", "vanilla"}
            or not (item.guidance or item.skills or item.plugins)
            for name, item in self.treatments.items()
        ):
            raise ValueError("treatments need nonempty named sets; vanilla is automatic")
        return self


def _resolve(base: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).absolute()


def resolve_evaluation(raw: object, base: Path) -> Evaluation:
    config = Evaluation.model_validate(raw)
    if config.tasks.source is not None:
        config.tasks.source = _resolve(base, config.tasks.source)
    if config.measurement_settings.fallow_config is not None:
        config.measurement_settings.fallow_config = _resolve(
            base, config.measurement_settings.fallow_config
        )
    config.treatments = {
        name: resolve_treatment(item, base, config.runner.harness)
        for name, item in config.treatments.items()
    }
    if config.reviewer:
        files = [
            file for path in config.reviewer.guidance for file in _text_files(_resolve(base, path))
        ]
        if len(set(files)) != len(files):
            raise ValueError("review guidance supplies the same text twice")
        config.reviewer.guidance = files
    return config


def load_evaluation(path: Path) -> Evaluation:
    source = path.expanduser().absolute()
    return resolve_evaluation(yaml.safe_load(source.read_text()), source.parent)


def _task_limits(configuration: dict[str, object]) -> dict[str, int]:
    fields = {
        "cpus": ("environment", "cpus"),
        "memory_mb": ("environment", "memory_mb"),
        "build_timeout_seconds": ("environment", "build_timeout_sec"),
        "agent_timeout_seconds": ("agent", "timeout_sec"),
    }
    limits = {}
    for name, (section, key) in fields.items():
        value = _object(configuration[section]).get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            or int(value) != value
        ):
            raise ValueError(f"{section}.{key} must be a positive whole number")
        limits[name] = int(value)
    return limits


def _container_limits(config: Evaluation, declared: dict[str, dict[str, int]]) -> dict[str, int]:
    limits = {}
    for key, field, fallback in (
        ("cpus", "container_cpus", 1),
        ("memory_mb", "container_memory_mb", 2048),
    ):
        value = getattr(config, field)
        requested = {row[key] for row in declared.values()}
        if value is None:
            if len(requested) > 1:
                raise ValueError(
                    f"Selected tasks declare different {key}; provide an explicit common {field}"
                )
            value = next(iter(requested), fallback)
        for task, row in declared.items():
            if value < row[key]:
                raise ValueError(
                    f"{field}={value} is below {row[key]} requested by task {task}; reduced resources require native fitness validation and are currently unsupported"
                )
        limits[key] = value
    return limits


def plan_evaluation(config: Evaluation) -> dict[str, object]:
    for treatment in config.treatments.values():
        admit(treatment, config.runner.harness)
    source = config.tasks.resolve()
    suite = source.parent if source.name == "tasks" else source
    identities = {}
    declared = {}
    capabilities = {}
    coding_images = {}
    submission_sources: dict[str, dict[str, object]] = {}
    for task in config.tasks.task_ids:
        path = suite / "tasks" / task
        admission = harbor_backend.validate_native_task(path)
        identities[task] = admission["task_assets_sha256"]
        coding_images[task] = admission["coding_image"]
        task_limits = _task_limits(_object(admission["configuration"]))
        verifier = _object(admission["verifier_configuration"])
        for key in ("cpus", "memory_mb"):
            native_value = verifier.get(key)
            if type(native_value) is not int or native_value <= 0:
                raise ValueError(f"verifier.{key} must be a positive whole number")
            task_limits[key] = max(task_limits[key], native_value)
        declared[task] = task_limits
        capabilities[task] = capability_plan(path, config.measurements, config.measurement_settings)
        try:
            contract = submissions.reviewed_contract(path)
        except (OSError, ValueError) as error:
            submission_sources[task] = {"status": "unsupported", "reason": str(error)}
        else:
            submission_sources[task] = {
                "status": "requires_preparation",
                "contract": contract,
                "reason": "grader baseline and frozen fixture must be qualified before coding",
            }
    limits = _container_limits(config, declared)
    capacity = harbor_backend.capacity_jobs(**limits)
    if config.jobs > capacity:
        # jobs is part of the comparison and vanilla cache identity, so it is never lowered.
        raise ValueError(
            f"jobs={config.jobs} does not fit Docker capacity for containers of "
            f"{limits['cpus']} CPUs and {limits['memory_mb']} MB; set jobs: {capacity} or fewer"
        )
    jobs = config.jobs
    criteria = (
        _fixed_criteria([file.read_text() for file in config.reviewer.guidance])
        if config.reviewer
        else None
    )
    comparison: dict[str, object] = {
        "tasks": identities,
        "runner": config.runner.model_dump(mode="json"),
        "delivery": DELIVERY_POLICY,
        "capabilities": config.capabilities.model_dump(mode="json"),
        "measurement_settings": config.measurement_settings.model_dump(mode="json"),
        "fallow_config_sha256": hashlib.sha256(
            config.measurement_settings.fallow_config.read_bytes()
        ).hexdigest()
        if config.measurement_settings.fallow_config is not None
        else None,
        "measurements": sorted(config.measurements),
        "reviewer": config.reviewer.model_dump(mode="json", exclude={"guidance"})
        if config.reviewer
        else None,
        "review_criteria_sha256": _criteria_digest(criteria) if criteria else None,
        "jobs": jobs,
        "backend": "harbor-native",
        "source_policy": submissions.SOURCE_POLICY,
        "task_limits": {
            task: {
                "declared": row,
                "effective": {**row, **limits, "agent_timeout_seconds": config.timeout_seconds},
                "adapted_agent_budget": config.timeout_seconds != row["agent_timeout_seconds"],
            }
            for task, row in declared.items()
        },
        "isolation": harbor_backend.runtime_identity(**limits),
    }
    if config.tasks.references:
        comparison["task_sources"] = config.tasks.references
    coding = len(config.tasks.task_ids) * (1 + len(config.treatments)) * config.repeats
    return {
        "name": config.name,
        "coding_calls": coding,
        "review_calls": coding if config.reviewer else 0,
        "runner": config.runner.model_dump(mode="json"),
        "treatments": {name: treatment_inventory(item) for name, item in config.treatments.items()},
        "delivery": DELIVERY_POLICY,
        "capabilities": config.capabilities.model_dump(mode="json"),
        "tasks": config.tasks.task_ids,
        "measurements": config.measurements,
        "measurement_capabilities": capabilities,
        "coding_images": coding_images,
        "submission_sources": submission_sources,
        "repeats": config.repeats,
        "jobs": jobs,
        "container_limits": limits,
        "vanilla": "reuse a verified isolated vanilla cohort when available; otherwise fresh vanilla attempts; native built-in instructions remain",
        "call_estimate": "maximum new root attempts; native subagent turns are additional and metered from session traces; baseline hits resolve before coding",
        "comparison_identity": comparison,
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


@dataclass(frozen=True)
class _Caches:
    """Caches every output folder shares, so a new output folder reuses their entries."""

    native: Path
    vanilla: Path
    reference_agents: Path


def _caches(output: Path) -> _Caches:
    """Resolve the cache root and check where it leads before anything is prepared.

    A symlinked cache location is followed here once, so the later no-symlink checks in each
    cache see its real path. A symlink inside the root, or a root that overlaps the output,
    fails now instead of after image preparation.
    """
    base = cache_root().parent.resolve()
    run = output.resolve()
    if run.is_relative_to(base) or base.is_relative_to(run):
        raise ValueError(f"output {run} must not overlap the PluginBench cache {base}")
    caches = _Caches(
        native=base / "prepared-images/native",
        vanilla=base / "vanilla",
        reference_agents=base / "reference-agents",
    )
    for path in (caches.native, caches.vanilla, caches.reference_agents):
        if path.resolve() != path:
            raise ValueError(f"PluginBench cache directories must not be symlinks: {path}")
    return caches


def _launcher_sha256(harness: str) -> dict[str, str]:
    return {name: components.source_sha256(name) for name in components.launcher_files(harness)}


def _assert_launcher(
    selection: dict[str, object], identity: dict[str, object] | None, harness: str
) -> None:
    """Coding resumes only with the launcher version that coded the run's earlier attempts."""
    current = components.VERSIONS["launcher"]
    try:
        if identity is None:
            isolation = _object(_object(selection["comparison_identity"])["isolation"])
            saved = harbor_backend.runtime_contract(isolation)["launcher_version"]
        elif "launcher_version" in identity:
            saved = identity["launcher_version"]
        else:
            saved = components.saved_launcher_version(harness, identity.get("launcher_sha256"))
    except components.UnknownSourceError as error:
        raise ValueError(
            f"{error}. This build's launcher is version {current}: resume this run with the "
            "build that started it, or start a new run"
        ) from error
    if saved != current:
        raise ValueError(
            f"this run codes with PluginBench launcher version {saved}, and this build's "
            f"launcher is version {current}: resume this run with the build that started it, "
            "or start a new run"
        )


def _vanilla_identity(
    config: Evaluation,
    comparison: dict[str, object],
    harness_version: str,
    task_environment: dict[str, object],
) -> dict[str, object]:
    """Everything a vanilla cohort's cache key binds; `check` previews with the run's own key."""
    return {
        "delivery": DELIVERY_POLICY,
        "capabilities": config.capabilities.model_dump(mode="json"),
        "task_assets": comparison["tasks"],
        "task_environment": task_environment,
        "runner": comparison["runner"],
        "harness_version": harness_version,
        "isolation": comparison["isolation"],
        "timeout_seconds": config.timeout_seconds,
        "jobs": config.jobs,
        "seed": config.seed,
        "launcher_version": components.VERSIONS["launcher"],
        "launcher_sha256": _launcher_sha256(config.runner.harness),
        # run_study binds the same settings from its frozen recipe.
        "study": {
            "harness": config.runner.harness,
            "model": config.runner.model,
            "reasoning": config.runner.reasoning,
            "jobs": config.jobs,
            "timeout_seconds": config.timeout_seconds,
            "seed": config.seed,
        },
    }


def _reference_agents(
    identity: dict[str, object], destination: Path, cache: Path
) -> dict[str, dict[str, object]]:
    """Oracle and nop qualify a preparation, not a run: reuse a pass for the same preparation.

    Only passing results are kept, so a broken or flaky result is always retried.
    """
    key = str(identity["preparation_key"])
    images = {
        name: _object(identity[name])["image_id"]
        for name in ("image_identity", "grader_image_identity")
    }
    entry = cache / f"{key}.json"
    if entry.is_file():
        saved = _object(json.loads(entry.read_text()))
        if saved.get("preparation_key") != key:
            raise ValueError(f"reference agent cache entry changed: {entry}")
        # A re-prepared image under the same key has not been qualified yet, nor has a pass
        # with a different supplied git identity (absent in both when the task has its own).
        if saved.get("images") == images and saved.get("supplied_git_identity") == identity.get(
            "supplied_git_identity"
        ):
            return TypeAdapter(dict[str, dict[str, object]]).validate_python(saved["rows"])
    rows = _reference_agent_rows(harbor_backend.run_reference_agents(identity, destination))
    if all(row["state"] != "broken" for row in rows.values()):
        cache.mkdir(parents=True, exist_ok=True)
        temporary = cache / f".{key}.{os.getpid()}.tmp"
        passed: dict[str, object] = {"preparation_key": key, "images": images, "rows": rows}
        if "supplied_git_identity" in identity:
            passed["supplied_git_identity"] = identity["supplied_git_identity"]
        _write(temporary, passed)
        temporary.replace(entry)
    return rows


class CheckFailed(ValueError):
    """A blocking check finding; no coding call was made. `report` holds the whole matrix."""

    def __init__(self, report: dict[str, object], findings: list[str], path: Path) -> None:
        super().__init__(
            "Check failed; no coding calls were made. Repair the inputs and start a new "
            f"evaluation. See {path}.\n" + "\n".join(findings)
        )
        self.report = report


@dataclass
class _Checked:
    """What a passed check hands to coding, all in the same new run root."""

    config: Evaluation
    root: Path
    comparison: dict[str, object]
    harness_versions: dict[str, str]
    profiles: dict[str, list[Collector]]
    scopes: dict[str, object]
    task_environment: dict[str, object]
    capabilities: dict[str, object]
    caches: _Caches
    vanilla_identity: dict[str, object]
    report: dict[str, object]


def _harness_version(harness: str) -> str:
    native = (claude.preflight if harness == "claude" else codex.preflight)()
    version = str(native.get("harness_version", ""))
    match = re.search(r"(?<![\w.+-])\d+\.\d+\.\d+(?![\w.+-])", version)
    if match is None:
        raise ValueError(f"cannot pin {harness} CLI version: {version}")
    if harness == "codex":
        codex.admit_version(match.group())
    with tempfile.TemporaryDirectory(prefix="pluginbench-auth-check-") as temporary:
        codex.runtime_environment(Path(temporary) / "private", harness)
    return match.group()


def _coding_environment_row(
    image: dict[str, object], probe: dict[str, object]
) -> dict[str, object]:
    passed = probe.get("status") == "passed"
    supplied = probe.get("supplied_git_identity")
    note = None
    if isinstance(supplied, dict):
        note = (
            "the task sets no git identity, so PluginBench supplied "
            f"{supplied['name']} <{supplied['email']}> to its agents"
        )
    return {
        "state": "ready" if passed else "broken",
        "reason": None if passed else probe.get("reason"),
        **({"note": note} if note else {}),
        "image_source": image["source"],
        "ignored_dockerfile": image["ignored_dockerfile"],
        "git_probe": probe,
    }


def _reference_agent_rows(results: dict[str, object]) -> dict[str, dict[str, object]]:
    """Oracle must produce a nonempty patch with reward 1; nop must get reward 0."""
    rows: dict[str, dict[str, object]] = {}
    for agent, expected in (("oracle", True), ("nop", False)):
        result = _object(results[agent])
        if result["status"] == "not_configured":
            rows[agent] = {"state": "not_configured", "reason": result["reason"]}
            continue
        reward = harbor_backend.binary_native_reward(result.get("verifier_result"))
        reason = None
        if result["status"] != "complete":
            reason = str(result.get("reason") or "trial did not complete")
        elif _object(result["cleanup"]).get("status") != "complete":
            reason = "trial cleanup incomplete"
        elif result.get("exception_info") is not None:
            reason = f"trial failed: {result['exception_info']}"
        elif expected and not result.get("patch_bytes"):
            reason = "reference solution produced an empty graded patch"
        elif reward is not expected:
            reason = f"verifier reward is {reward}; expected {int(expected)}"
        rows[agent] = {"state": "broken" if reason else "ready", "reason": reason}
    return rows


def _check(config: Evaluation, output: Path, progress: Callable[[str], None] | None) -> _Checked:
    """Every no-model check, in a new run root. Raises CheckFailed on any blocking finding."""
    plan = plan_evaluation(config)
    source = config.tasks.resolve()
    suite = source.parent if source.name == "tasks" else source
    limits = TypeAdapter(dict[str, int]).validate_python(plan["container_limits"])
    config = config.model_copy(
        update={
            "jobs": plan["jobs"],
            "container_cpus": limits["cpus"],
            "container_memory_mb": limits["memory_mb"],
        }
    )
    comparison = _object(plan["comparison_identity"])
    root = output.expanduser().resolve()
    if root.exists():
        raise ValueError(f"output must be a new directory: {root}")
    caches = _caches(root)
    harbor_backend.preflight(jobs=config.jobs, **limits)
    harness_versions: dict[str, str] = {
        harness: _harness_version(harness)
        for harness in {
            config.runner.harness,
            *([config.reviewer.harness] if config.reviewer else []),
        }
    }
    if config.reviewer and config.reviewer.harness == "codex":
        isolation.codex_code_mode_host()
    coding_images = _object(plan["coding_images"])
    warnings = [
        f"{task}: runs the prebuilt image {image['image']}; {image['ignored_dockerfile']} is "
        "never built, so changes to it have no effect"
        for task, value in coding_images.items()
        if (image := _object(value))["ignored_dockerfile"]
    ]
    if progress:
        for warning in warnings:
            progress("Warning: " + warning)
    root.mkdir(parents=True)
    prepared = root / "inputs"
    prepared.mkdir()
    entries = []
    profiles = {}
    scopes: dict[str, object] = {}
    task_environment: dict[str, object] = {}
    capabilities: dict[str, object] = {}
    matrix: dict[str, dict[str, dict[str, object]]] = {}
    for task in config.tasks.task_ids:
        if progress:
            progress(f"Preparing native Harbor task {task}")
        destination = prepared / task
        effective = TypeAdapter(dict[str, int]).validate_python(
            _object(_object(comparison["task_limits"])[task])["effective"]
        )
        identity = harbor_backend.prepare_native_task(
            task_dir=suite / "tasks" / task,
            destination=destination,
            harness=config.runner.harness,
            version=harness_versions[config.runner.harness],
            limits={**limits, "setup_timeout": effective["build_timeout_seconds"]},
            cache=caches.native,
        )
        native_task = Path(str(identity["task_dir"]))
        fixture = Path(str(identity["fixture_dir"]))
        if identity["original_task_assets_sha256"] != _object(comparison["tasks"])[task]:
            raise ValueError("native task assets changed after planning")
        # Before anything saves the identity: a task without a git identity gets PluginBench's.
        git_probe = harbor_backend.probe_coding_environment(identity)
        if "supplied_git_identity" in git_probe:
            identity["supplied_git_identity"] = git_probe["supplied_git_identity"]
            codex.write_json(destination / "artifacts/native-preparation.json", identity)
        model = importlib.import_module("harbor.models.task.task").Task(native_task)
        entries.append(
            TaskEntry(
                id=task,
                prompt=model.instruction,
                fixture=fixture.relative_to(prepared),
                native_task=native_task.relative_to(prepared),
                provenance={"native_task_identity": json.dumps(identity, sort_keys=True)},
            )
        )
        profiles[task] = prepare_profiles(
            fixture,
            destination / "measurements",
            config.measurements,
            config.measurement_settings,
            identity,
        )
        provenance = json.loads(
            (destination / "measurements/measurement-provenance.json").read_text()
        )
        scopes[task] = {
            "source_scope": json.loads(provenance["source_scope"]),
            "test_scope": json.loads(provenance["test_scope"]),
            "version": provenance["measurement_profile_version"],
        }
        rows = probe_measurements(
            fixture,
            destination / "measurement-readiness",
            profiles[task],
            config.measurements,
            _object(provenance["capabilities"]),
        )
        qualification = _object(identity.get("source_qualification", {}))
        qualified = (
            identity.get("source_policy") == submissions.SOURCE_POLICY
            and qualification.get("status") == "complete"
        )
        submission_source: dict[str, object] = {
            "status": "available" if qualified else "unavailable",
            "source_policy": submissions.SOURCE_POLICY,
            "reason": None
            if qualified
            else qualification.get(
                "reason", "canonical submission source has no compatible preparation proof"
            ),
        }
        # A task without a reviewed patch-grading contract has no submission source to qualify.
        if _object(_object(plan["submission_sources"])[task])["status"] == "unsupported":
            submission_source["configured"] = False
        rows["submission_source"] = {
            **submission_source,
            "state": readiness_state(submission_source),
        }
        capabilities[task] = rows
        task_environment[task] = portable_native_identity(identity)
        matrix[task] = {
            name: {"state": row["state"], "reason": row.get("reason")} for name, row in rows.items()
        }
        matrix[task]["coding_environment"] = _coding_environment_row(
            _object(coding_images[task]), git_probe
        )
        matrix[task].update(
            _reference_agents(identity, root / "check" / task, caches.reference_agents)
        )
        if progress:
            for name, row in matrix[task].items():
                if row["state"] != "ready":
                    progress(f"{task}: {name} {row['state']} — {row['reason']}")
                elif row.get("note"):
                    progress(f"{task}: {name} ready — {row['note']}")
    _write(
        prepared / "tasks.json",
        TaskCatalog(schema_version=1, tasks=entries).model_dump(mode="json"),
    )
    _write(root / "measurement-readiness.json", capabilities)
    # Saved here, and loaded back as a resumed run loads them, so check fails where run would.
    _write(
        prepared / _PROFILES,
        {
            task: [
                {
                    **collector.model_dump(mode="json"),
                    "config": str(collector.config.relative_to(prepared)),
                }
                for collector in collectors
            ]
            # A task with no collector stays listed: it gets every measure except the missing ones.
            for task, collectors in profiles.items()
        },
    )
    _saved_profiles(root)
    vanilla_identity = _vanilla_identity(
        config, comparison, harness_versions[config.runner.harness], task_environment
    )
    vanilla_cache = baselines.preview(
        path=caches.vanilla,
        tasks=tuple(load_tasks(prepared / "tasks.json")),
        repeats=config.repeats,
        identity=vanilla_identity,
    )
    findings = blocking_findings(matrix, require_ready=config.measurement_settings.require_ready)
    # Decided once here and saved with the run: status and the report read this list. Under
    # require_ready a broken measure blocks instead, so nothing is skipped.
    skipped = [] if config.measurement_settings.require_ready else not_measured(matrix)
    for row in skipped:
        warning = (
            f"{row['task']}: {row['measure']} is broken, so it is not measured for this task: "
            f"{row['reason']}"
        )
        if progress:
            progress("Warning: " + warning)
        warnings.append(warning)
    report: dict[str, object] = {
        "status": "blocked" if findings else "ready",
        "model_calls": 0,
        "require_ready": config.measurement_settings.require_ready,
        "harness_versions": harness_versions,
        "coding_images": coding_images,
        "warnings": warnings,
        "findings": findings,
        "not_measured": skipped,
        "matrix": matrix,
        "vanilla_cache": vanilla_cache,
    }
    _write(root / "check.json", report)
    if findings:
        raise CheckFailed(report, findings, root / "check.json")
    return _Checked(
        config=config,
        root=root,
        comparison=comparison,
        harness_versions=harness_versions,
        profiles=profiles,
        scopes=scopes,
        task_environment=task_environment,
        capabilities=capabilities,
        caches=caches,
        vanilla_identity=vanilla_identity,
        report=report,
    )


def check_evaluation(
    config: Evaluation, output: Path, *, progress: Callable[[str], None] | None = None
) -> dict[str, object]:
    """Prepare every task and run every no-model check; never starts a coding attempt."""
    return _check(config, output, progress).report


def run_evaluation(
    config: Evaluation, output: Path, *, progress: Callable[[str], None] | None = None
) -> Path:
    return resume_evaluation(
        prepare_evaluation(config, output, progress=progress), progress=progress
    )


def prepare_evaluation(
    config: Evaluation, output: Path, *, progress: Callable[[str], None] | None = None
) -> Path:
    """Check, then save everything coding needs, so any later process can run or resume it."""
    checked = _check(config, output, progress)
    config = checked.config
    root = checked.root
    comparison = checked.comparison
    harness_versions = checked.harness_versions
    profiles = checked.profiles
    scopes = checked.scopes
    task_environment = checked.task_environment
    capabilities = checked.capabilities
    prepared = root / "inputs"
    fixed_guidance = []
    if config.reviewer:
        for index, original in enumerate(config.reviewer.guidance):
            target = prepared / f"review-{index}.md"
            shutil.copy2(original, target)
            fixed_guidance.append(target)
        if (
            _criteria_digest(_fixed_criteria([file.read_text() for file in fixed_guidance]))
            != comparison["review_criteria_sha256"]
        ):
            raise ValueError("fixed review criteria changed during preparation")
    supplied = any("supplied_git_identity" in _object(row) for row in task_environment.values())
    recipe = {
        # 4 makes builds that cannot supply a git identity refuse the run before coding.
        "schema_version": 4 if supplied else 3,
        "backend": "harbor",
        "harness_version": harness_versions[config.runner.harness],
        "container_cpus": config.container_cpus,
        "container_memory_mb": config.container_memory_mb,
        "name": config.name,
        "harness": config.runner.harness,
        "tasks": str(prepared / "tasks.json"),
        "treatments": {
            name: item.model_dump(mode="json") for name, item in config.treatments.items()
        },
        "capabilities": config.capabilities.model_dump(mode="json"),
        "model": config.runner.model,
        "reasoning": config.runner.reasoning,
        "repeats": config.repeats,
        "seed": config.seed,
        "timeout_seconds": config.timeout_seconds,
        "jobs": config.jobs,
    }
    _write(prepared / "study.json", recipe)
    comparison.update(
        task_environment=task_environment,
        harness_version=harness_versions[config.runner.harness],
        measurement_capabilities=capabilities,
    )
    selection = {
        **config.model_dump(mode="json"),
        "comparison_identity": comparison,
        "task_scopes": scopes,
        "measurement_capabilities": capabilities,
        "measurement_readiness": capabilities,
        "not_measured": checked.report["not_measured"],
        "artifact_base": "evidence",
        "measurement_required": any(profiles.values()),
        "measurement_revision": None,
        "review_revision": None,
    }
    _write(prepared / "vanilla-identity.json", checked.vanilla_identity)
    _save_selection(root, selection)
    return root


# Measurement profiles saved at preparation, so measurement can start after coding resumes.
_PROFILES = "profiles.json"


def _saved_profiles(root: Path) -> dict[str, list[Collector]]:
    path = root / "inputs" / _PROFILES
    if not path.is_file():
        raise ValueError(
            "this run predates saved measurement profiles, so measurement cannot start on "
            "resume; its coding evidence is kept"
        )
    return load_profiles(path)


def _measurement_unstarted(root: Path, selection: dict[str, object]) -> bool:
    """Measurement is required and no observer revision has started: it uses saved profiles."""
    return bool(selection["measurement_required"]) and not any(
        (root / "evidence/observer-revisions" / str(candidate)).exists()
        for candidate in (
            selection.get("pending_measurement_revision", "measurements"),
            selection.get("recovery_measurement_revision"),
        )
        if candidate is not None
    )


def _review_preflight(selection: dict[str, object]) -> None:
    """The reviewer's CLI, credentials and sandbox host, checked before any paid review call."""
    reviewer = _object(selection["reviewer"])
    harness = str(reviewer["harness"])
    (claude.preflight if harness == "claude" else codex.preflight)()
    with tempfile.TemporaryDirectory(prefix="pluginbench-auth-check-") as temporary:
        codex.runtime_environment(Path(temporary) / "private", harness)
    if harness == "codex":
        isolation.codex_code_mode_host()


def _assert_resumable(root: Path, selection: dict[str, object]) -> None:
    """Fail before any resumed coding call on what would otherwise fail after it."""
    if _measurement_unstarted(root, selection):
        _saved_profiles(root)
    if selection.get("reviewer") and selection["review_revision"] is None:
        _review_preflight(selection)


def _code(
    root: Path,
    selection: dict[str, object],
    *,
    progress: Callable[[str], None] | None,
    guard: Callable[[], None] | None,
) -> None:
    """Start coding, or resume it: complete attempts are kept and never coded again."""
    evidence = root / "evidence"
    config = _saved_evaluation(selection)
    recipe = _object(json.loads((root / "inputs/study.json").read_text()))
    identity_path = root / "inputs/vanilla-identity.json"
    # Runs prepared before vanilla identities were saved resume without the vanilla cache.
    identity = _object(json.loads(identity_path.read_text())) if identity_path.is_file() else None
    resuming = (evidence / "run.json").is_file()

    def same_coder() -> None:
        # Checked only once reconciliation has found a slot left to code.
        version = _harness_version(config.runner.harness)
        if version != recipe["harness_version"]:
            raise ValueError(
                f"{config.runner.harness} CLI is {version}, but this run codes with "
                f"{recipe['harness_version']}; install that version to resume it"
            )
        # One comparison must not hold attempts coded by two launchers.
        _assert_launcher(selection, identity, config.runner.harness)

    if resuming:
        resume_study(
            evidence,
            progress=progress,
            baseline_cache=_caches(root).vanilla if identity else None,
            baseline_identity=identity,
            guard=guard,
            before_coding=same_coder,
        )
    else:
        run_study(
            load_study(root / "inputs/study.json"),
            evidence,
            progress=progress,
            baseline_cache=_caches(root).vanilla if identity else None,
            baseline_identity=identity,
            guard=guard,
        )
    if not (root / "inputs" / _PROFILES).is_file():
        return
    # Measurement contexts refer to the same run-owned frozen task assets as grading/review.
    frozen_catalog = TaskCatalog.model_validate_json((evidence / "frozen/tasks.json").read_text())
    frozen_tasks = {
        entry.id: evidence / "frozen" / entry.native_task for entry in frozen_catalog.tasks
    }
    for task, collectors in _saved_profiles(root).items():
        for collector in collectors:
            if collector.kind == "analysis" or (
                collector.kind == "coverage" and collector.report_format is not None
            ):
                payload = json.loads(collector.config.read_text())
                payload["native_task_identity"]["task_dir"] = str(frozen_tasks[task])
                _write(collector.config, payload)


def _saved_evaluation(selection: dict[str, object]) -> Evaluation:
    normalized = _object(study.normalize_legacy_guidance(selection))
    return Evaluation.model_validate(
        {key: normalized[key] for key in Evaluation.model_fields if key in normalized}
    )


def _finish_evaluation(
    root: Path,
    *,
    selection: dict[str, object],
    profiles: dict[str, list[Collector]],
    guidance: list[Path],
    progress: Callable[[str], None] | None,
) -> Path:
    evidence = root / "evidence"
    config = _saved_evaluation(selection)
    if profiles and selection["measurement_revision"] is None:
        revision = _next_measurement_revision(evidence)
        if selection.get("pending_measurement_revision") is not None:
            selection["recovery_measurement_revision"] = selection["pending_measurement_revision"]
        selection["pending_measurement_revision"] = revision
        _save_selection(root, selection)
        propose_remeasurement(
            evidence,
            profiles=profiles,
            revision=revision,
            progress=progress,
            jobs=config.jobs,
            masks_listed_pairs=bool(selection.get("not_measured")),
        )
        _adopt_measurement_revision(root, selection, revision)
        _save_selection(root, selection)
    if config.reviewer and selection["review_revision"] is None:
        if progress:
            progress("Reviewing all submissions independently with fixed criteria")
        review_study(
            evidence,
            harness=config.reviewer.harness,
            model=config.reviewer.model,
            effort=config.reviewer.reasoning,
            timeout=config.reviewer.timeout_seconds or config.timeout_seconds,
            seed=config.seed,
            revision="review",
            guidance=guidance,
            jobs=config.jobs,
        )
        selection["review_revision"] = "review"
        _save_selection(root, selection)
    if config.reviewer and selection["review_revision"] is not None:
        selection["review_revision"] = recover_review_citations(
            evidence, str(selection["review_revision"])
        )
    _save_selection(root, selection)
    regenerate_report(root)
    return root


def _load_selection(root: Path) -> dict[str, object]:
    path = root / "evaluation.json"
    if not path.is_file():
        raise ValueError(
            f"{root} is not a run directory (it has no evaluation.json); give the directory "
            "that `pluginbench run` printed"
        )
    binding = json.loads((root / "evaluation-binding.json").read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
        raise ValueError("saved evaluation settings changed")
    return _object(json.loads(path.read_text()))


def _selected_revision(selection: dict[str, object], key: str) -> str | None:
    value = selection[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"saved {key} must be a string or null")
    return value


def _adopt_measurement_revision(root: Path, selection: dict[str, object], revision: str) -> None:
    evidence = root / "evidence"
    directory = evidence / "observer-revisions" / revision
    validate_remeasurement(evidence, directory)
    state = json.loads((directory / "revision.json").read_text())
    saved_history = selection.get("measurement_history", [])
    if not isinstance(saved_history, list):
        raise ValueError("measurement history must be a list of bound revision receipts")
    history = list(saved_history)
    prior = selection.get("recovery_measurement_revision")
    if prior is not None:
        path = evidence / "observer-revisions" / str(prior) / "revision.json"
        receipt = study._reference(evidence, path)
        if receipt not in history:
            history.append(receipt)
    selection["measurement_history"] = history
    profiles = load_profiles(study._bound_path(evidence, state["frozen_profiles"]))
    comparison = _object(selection["comparison_identity"])
    comparison["effective_observer_scripts"] = {
        task: {
            collector.kind: hashlib.sha256(Path(command[1]).read_bytes()).hexdigest()
            for collector in collectors
            for command in (collector.command or collector.syntax_command,)
            if command and len(command) > 1 and Path(command[1]).is_file()
        }
        for task, collectors in profiles.items()
    }
    selection["comparison_identity"] = comparison
    selection["measurement_revision"] = revision
    selection.pop("pending_measurement_revision", None)
    selection.pop("recovery_measurement_revision", None)


def _next_measurement_revision(evidence: Path) -> str:
    revision = "measurements"
    index = 2
    while (evidence / "observer-revisions" / revision).exists():
        revision = f"measurements-{index}"
        index += 1
    return revision


def _sole_owner(root: Path, *, resuming: bool) -> bool:
    """Whether this process may treat measurement or review state left active as dead.

    Only the holder of run.lock collects or reviews, and its flock ends with its process, even
    on SIGKILL. So state the holder finds still active was left by a process that ended. A plan
    may assume the same while no live process holds the lock; a resume acts on it only as the
    holder. A held lock with no owner record yet counts as another owner.
    """
    current = run_lock.owner(root)
    if current is None:
        return not resuming
    return current.get("pid") == os.getpid()


def _measurement_to_recover(
    evidence: Path, selection: dict[str, object]
) -> tuple[str, dict[str, object]] | None:
    """The first revision a resume names whose saved state it can use, with that state.

    None when each one has no state file or failed before it saved frozen profiles.
    """
    for candidate in (
        selection.get("pending_measurement_revision", "measurements"),
        selection.get("recovery_measurement_revision"),
    ):
        if candidate is None:
            continue
        revision = str(candidate)
        directory = evidence / "observer-revisions" / revision
        if (
            directory.is_symlink()
            or directory.resolve().parent != (evidence / "observer-revisions").resolve()
        ):
            raise ValueError("observer recovery must stay inside its native study")
        if not (directory / "revision.json").exists():
            continue
        state = _object(json.loads((directory / "revision.json").read_text()))
        if state["status"] != "failed" or all(
            key in state for key in ("profile_inventory", "frozen_profiles")
        ):
            return revision, state
    return None


def _resume_inputs(
    root: Path, *, resuming: bool, progress: Callable[[str], None] | None = None
) -> tuple[dict[str, object], dict[str, list[Collector]], list[Path], int]:
    """What a resume has left after coding. With `resuming`, dead active state is closed on disk."""
    selection = _load_selection(root)
    config = _saved_evaluation(selection)
    evidence = root / "evidence"
    run, manifest, original = _native_state(evidence)
    profiles: dict[str, list[Collector]] = {}

    def measuring_again(abandoned: str, why: str) -> None:
        if progress:
            progress(
                f"Measurement revision {abandoned} {why}, so it cannot continue. Its files stay; "
                f"measurement starts again as {_next_measurement_revision(evidence)}"
            )

    if selection["measurement_revision"] is not None:
        validate_remeasurement(
            evidence, evidence / "observer-revisions" / str(selection["measurement_revision"])
        )
    elif _measurement_unstarted(root, selection):
        profiles = _saved_profiles(root)
    elif selection["measurement_required"]:
        found = _measurement_to_recover(evidence, selection)
        if found is None:
            # Each named revision ended before it saved frozen profiles: a kill left its
            # directory with no state file, or a stop recorded it as failed first. Its directory
            # stays as it is; measurement starts again under a new name with the prepared profiles.
            if not _sole_owner(root, resuming=resuming):
                raise ValueError(
                    "observer recovery has no saved frozen profiles and another process may own "
                    "this run; cannot replay it"
                )
            measuring_again(
                str(selection.get("pending_measurement_revision", "measurements")),
                "ended before it saved its frozen profiles",
            )
            # Nothing to bind in measurement history: the revision saved no state.
            selection["pending_measurement_revision"] = None
            profiles = _saved_profiles(root)
        else:
            revision, state = found
            directory = evidence / "observer-revisions" / revision
            selection["pending_measurement_revision"] = revision
            if state["status"] == "collecting" and _sole_owner(root, resuming=resuming):
                # Its process ended without recording why; record it as failed, keep its files.
                state.update(
                    status="failed",
                    error="its process ended while collecting; found by a later resume",
                )
                if resuming:
                    study._write(directory / "revision.json", state)
                measuring_again(revision, "was left collecting by a process that ended")
            if state["status"] == "complete":
                validate_remeasurement(evidence, directory)
                _adopt_measurement_revision(root, selection, revision)
            elif state["status"] == "failed":
                if (
                    study._bound_document(evidence, state["source_run"]) != run
                    or study._bound_path(evidence, state["source_assessment"]) != original
                    or _preserved_inventory(evidence, manifest, original.parent)
                    != state["preserved"]
                ):
                    raise ValueError(
                        "saved native inputs changed after failed observer collection"
                    )
                if study._inventory(directory / "profiles") != state["profile_inventory"]:
                    raise ValueError("frozen observer profiles changed")
                profiles = load_profiles(study._bound_path(evidence, state["frozen_profiles"]))
            else:
                raise ValueError(
                    "observer collection is still marked active and another process may own "
                    "this run; cannot replay it"
                )
    guidance = []
    calls = 0
    if config.reviewer:
        if selection["review_revision"] is not None:
            validate_review_revision(evidence, str(selection["review_revision"]))
        else:
            if (evidence / "review-revisions/review").exists():
                raise ValueError("review evidence already exists; model calls cannot be replayed")
            guidance = _frozen_review_guidance(root, selection, config.reviewer)
            calls = sum(record.stages[-1].snapshot is not None for record in manifest.records)
    return selection, profiles, guidance, calls


def _frozen_review_guidance(
    root: Path, selection: dict[str, object], reviewer: Reviewer
) -> list[Path]:
    """The run's saved review standards, refused if they no longer match its comparison."""
    guidance = [root / "inputs" / f"review-{index}.md" for index in range(len(reviewer.guidance))]
    criteria = _fixed_criteria([file.read_text() for file in guidance])
    if (
        _criteria_digest(criteria)
        != _object(selection["comparison_identity"])["review_criteria_sha256"]
    ):
        raise ValueError("frozen review criteria changed")
    return guidance


def _assigned(selection: dict[str, object]) -> int:
    config = _saved_evaluation(selection)
    return len(config.tasks.task_ids) * config.repeats * (len(config.treatments) + 1)


def _coding_done(root: Path) -> bool:
    run = root / "evidence/run.json"
    return run.is_file() and json.loads(run.read_text())["status"] == "complete"


def _coding_left(root: Path, selection: dict[str, object]) -> int:
    """Coding calls a resume makes: one per slot without a complete attempt."""
    run = root / "evidence/run.json"
    if not run.is_file():
        return _assigned(selection)
    states = _object(json.loads(run.read_text())["attempt_states"])
    return sum(state != "complete" for state in states.values())


def plan_resume(root: Path) -> dict[str, object]:
    root = root.expanduser().resolve()
    selection = _load_selection(root)
    if not _coding_done(root):
        if _measurement_unstarted(root, selection):
            _saved_profiles(root)
        reviewer = selection.get("reviewer")
        return {
            "name": selection["name"],
            "coding_calls": _coding_left(root, selection),
            # At most one review per assigned attempt; only submitted attempts are reviewed.
            "review_calls": _assigned(selection) if reviewer else 0,
            "collect_measurements": bool(selection["measurement_required"]),
            "jobs": selection["jobs"],
            "vanilla": "keep complete attempts; code pending slots and replace interrupted ones",
        }
    selection, profiles, _, calls = _resume_inputs(root, resuming=False)
    return {
        "name": selection["name"],
        "coding_calls": 0,
        "review_calls": calls,
        "collect_measurements": bool(profiles),
        "jobs": selection["jobs"],
        "vanilla": "reuse immutable saved submissions; no coding replay",
    }


def resume_evaluation(
    root: Path,
    *,
    progress: Callable[[str], None] | None = None,
    guard: Callable[[], None] | None = None,
) -> Path:
    """Run whatever a prepared evaluation has left: coding, then measurement and reviews."""
    root = root.expanduser().resolve()
    selection = _load_selection(root)
    if not _coding_done(root):
        _assert_resumable(root, selection)
        _code(root, selection, progress=progress, guard=guard)
    if guard:
        guard()
    selection, profiles, guidance, calls = _resume_inputs(root, resuming=True, progress=progress)
    if calls:
        _review_preflight(selection)
    return _finish_evaluation(
        root, selection=selection, profiles=profiles, guidance=guidance, progress=progress
    )


def plan_review(root: Path, revision: str) -> dict[str, object]:
    """Check that a finished run can be reviewed again as `revision`, and count its calls."""
    root = root.expanduser().resolve()
    selection = _load_selection(root)
    config = _saved_evaluation(selection)
    if config.reviewer is None:
        raise ValueError("this run has no reviewer, so it has no review to repeat")
    if selection["review_revision"] is None:
        raise ValueError(
            f"this run has not finished its first review; finish it with `pluginbench run {root}`"
        )
    if _review_directory(root / "evidence", revision).exists():
        raise ValueError(f"review revision {revision} already exists; choose a new --revision name")
    _frozen_review_guidance(root, selection, config.reviewer)
    _, manifest, _ = _native_state(root / "evidence")
    return {
        "name": selection["name"],
        "revision": revision,
        "review_calls": sum(record.stages[-1].snapshot is not None for record in manifest.records),
    }


def review_evaluation(
    root: Path, revision: str, *, progress: Callable[[str], None] | None = None
) -> str:
    """Review every submission again with the frozen reviewer as `revision`, then select it.

    Earlier revisions stay in evidence. Returns the selected revision: `<revision>-citations`
    when offline citation recovery repaired its formatting.
    """
    root = root.expanduser().resolve()
    plan_review(root, revision)
    selection = _load_selection(root)
    config = _saved_evaluation(selection)
    if config.reviewer is None:
        raise ValueError("this run has no reviewer, so it has no review to repeat")
    guidance = _frozen_review_guidance(root, selection, config.reviewer)
    _review_preflight(selection)
    if progress:
        progress(f"Reviewing all submissions again as review revision {revision}")
    review_study(
        root / "evidence",
        harness=config.reviewer.harness,
        model=config.reviewer.model,
        effort=config.reviewer.reasoning,
        timeout=config.reviewer.timeout_seconds or config.timeout_seconds,
        seed=config.seed,
        revision=revision,
        guidance=guidance,
        jobs=config.jobs,
    )
    selected = recover_review_citations(root / "evidence", revision)
    selection["review_revision"] = selected
    _save_selection(root, selection)
    regenerate_report(root)
    return selected


def _save_selection(root: Path, selection: object) -> None:
    # A stop between these writes would leave settings that every later command refuses.
    with run_lock.deferred_stop():
        _write(root / "evaluation.json", selection)
        _write(
            root / "evaluation-binding.json",
            {"sha256": hashlib.sha256((root / "evaluation.json").read_bytes()).hexdigest()},
        )


def _measurement_collection(root: Path, selection: dict[str, object]) -> dict[str, object] | None:
    revision = _selected_revision(selection, "measurement_revision")
    if revision is None:
        return None
    evidence = root / "evidence"
    path = evidence / "observer-revisions" / revision / "revision.json"
    state = study._bound_document(evidence, study._reference(evidence, path))
    manifest = study._bound_document(evidence, state["proposed_assessment"])
    tasks = {record["id"]: record["task_id"] for record in manifest["records"]}
    available = 0
    failures = []
    for observation in state["observations"]:
        outcomes = observation["outcomes"]
        for family, outcome in outcomes.items():
            if outcome["status"] == "available":
                available += 1
                continue
            receipts = [repeat[family] for repeat in observation["repeats"]]
            diagnostics = [
                study._bound_document(evidence, receipt).get("reason")
                for receipt in receipts
                if str(receipt["path"]).endswith("failure.json")
            ]
            failures.append(
                {
                    "attempt_id": observation["attempt_id"],
                    "task": tasks[observation["attempt_id"]],
                    "stage": observation["stage"],
                    "family": family,
                    **outcome,
                    "receipts": receipts,
                    "diagnostics": diagnostics,
                }
            )
    history = []
    saved_history = selection.get("measurement_history", [])
    if not isinstance(saved_history, list):
        raise ValueError("measurement history must be a list of bound revision receipts")
    for receipt in saved_history:
        previous = study._bound_document(evidence, receipt)
        history.append(
            {"receipt": receipt, "status": previous["status"], "revision": previous["revision"]}
        )
    return {
        "revision": revision,
        "status": "partial" if failures else "complete",
        "procedure_status": state["status"],
        "count_scope": "attempt/stage/family observations",
        "available_families": available,
        "unavailable_families": len(failures),
        "failures": failures,
        "revision_receipt": study._reference(evidence, path),
        "history": history,
    }


def regenerate_report(root: Path, *, review_revision: str | None = None) -> Path:
    root = root.expanduser().resolve()
    config = _load_selection(root)
    comparison = _object(config["comparison_identity"])
    config["comparison_identity"] = comparison
    evidence = root / "evidence"
    output = root / "report"
    if review_revision is not None:
        review_path = validate_review_revision(evidence, review_revision)
        state = study._bound_document(evidence, study._reference(evidence, review_path))
        protocol = study._bound_document(evidence, state["protocol"])
        reviewer = _object(config["reviewer"])
        expected = {
            "harness": reviewer["harness"],
            "model": reviewer["model"],
            "effort": reviewer["reasoning"],
            "criteria_sha256": comparison["review_criteria_sha256"],
            "seed": config["seed"],
            "jobs": config["jobs"],
            "timeout_seconds": reviewer.get("timeout_seconds") or config["timeout_seconds"],
            "stage": "handoff",
        }
        if any(protocol.get(key) != value for key, value in expected.items()):
            raise ValueError("review projection differs from the fixed comparison settings")
        original_revision = _selected_revision(config, "review_revision")
        previous_path = (
            validate_review_revision(evidence, original_revision) if original_revision else None
        )
        states = {review_path}
        if previous_path is not None:
            states.add(previous_path)
        starts = {
            study._bound_path(evidence, row["native_start"]).resolve()
            for path in states
            for row in study._bound_document(evidence, study._reference(evidence, path))["records"]
            if row.get("native_start") is not None
        }
        config["review_projection"] = {
            "original_revision": original_revision,
            "selected_revision": review_revision,
            "revision_receipts": [study._reference(evidence, path) for path in sorted(states)],
            "native_invocations_in_these_revisions": len(starts),
            "usage_accounting": "Charts show the selected revision; bound prior revision receipts retain failed calls and their usage.",
        }
        config["review_revision"] = review_revision
        output = review_path.parent / "evaluation-report"
    report_path = report_study(
        evidence,
        measurement_revision=_selected_revision(config, "measurement_revision"),
        review_revision=_selected_revision(config, "review_revision"),
    )
    report = json.loads(report_path.read_text())
    recipe = json.loads((evidence / "frozen/resolved-recipe.json").read_text())
    packages = {}
    if "treatments" in recipe:
        packages = {
            name: treatment_inventory(
                resolve_treatment(
                    Treatment.model_validate(item), evidence / "frozen", recipe["harness"]
                )
            )
            for name, item in recipe["treatments"].items()
        }
    else:
        for name, files in recipe["guidance"].items():
            rows = [
                {
                    "path": str(Path(file).relative_to(root)),
                    "sha256": hashlib.sha256(Path(file).read_bytes()).hexdigest(),
                }
                for file in files
            ]
            packages[name] = {
                "files": rows,
                "sha256": hashlib.sha256(
                    json.dumps([row["sha256"] for row in rows]).encode()
                ).hexdigest(),
            }
    config["guidance_packages"] = packages
    runtime_profiles = {
        json.dumps(
            {
                "model": attempt["model"],
                **{
                    key: attempt["provenance"].get(key)
                    for key in (
                        "harness",
                        "harness_version",
                        "reasoning_effort",
                        "comparison_reasoning_effort",
                        "effort_evidence_source",
                    )
                },
            },
            sort_keys=True,
        )
        for attempt in report["attempts"]
    }
    comparison["observed_runtime_profiles"] = [
        json.loads(profile) for profile in sorted(runtime_profiles)
    ]
    if config.get("reviewer"):
        reviewer = _object(config["reviewer"])
        judge_profiles = set()
        for attempt in report["attempts"]:
            review = attempt.get("independent_review", {})
            execution = review.get("execution") or {}
            host = review.get("requested_harness", reviewer["harness"])
            profile = {
                "harness": f"{host}-cli",
                "actual_models": execution.get("actual_primary_models"),
                "effective_effort": execution.get("runtime_effort_evidence"),
                "harness_version": codex.observed_cli_version(execution.get("startup"), host=host)
                or execution.get("launch_harness_version"),
                "requested_effort": review.get("requested_reasoning_effort"),
                "effort_evidence_source": execution.get(
                    "effort_evidence_source",
                    "native session event"
                    if execution.get("runtime_effort_evidence")
                    else "unknown",
                ),
            }
            judge_profiles.add(json.dumps(profile, sort_keys=True))
        comparison["observed_reviewer_profiles"] = [
            json.loads(profile) for profile in sorted(judge_profiles)
        ]
    manifest = json.loads((evidence / "run.json").read_text())
    if manifest["status"] != "complete":
        # A run stopped during coding reports what it has, and the report says it is partial.
        coded = list(manifest["attempt_states"].values())
        config["unfinished_run"] = {"complete": coded.count("complete"), "assigned": len(coded)}
    input_path = evidence / manifest["assessment_input"]["path"]
    records = {row["id"]: row for row in json.loads(input_path.read_text())["records"]}
    for attempt in report["attempts"]:
        scope = _object(_object(config["task_scopes"])[attempt["task_id"]])
        # Canonical attempt contains digest only; bound input owns the snapshot paths.
        record = records[attempt["id"]]
        # An attempt that is not coded yet has no starting snapshot.
        if record.get("before") is None:
            continue
        before = input_path.parent / record["before"]["path"]
        for stage in attempt["stages"]:
            submitted = next(row for row in record["stages"] if row["id"] == stage["id"])
            if submitted["snapshot"] is None:
                continue
            after = input_path.parent / submitted["snapshot"]["path"]
            stage["code_size"] = scoped_code_size(
                before=before,
                after=after,
                scope=TypeAdapter(list[str]).validate_python(scope["source_scope"]),
                change=stage["change"],
            )
            stage["test_code_size"] = scoped_test_size(
                before=before,
                after=after,
                scope=TypeAdapter(list[str]).validate_python(scope["test_scope"]),
                change=stage["change"],
            )
    for task, value in _object(report.get("references", {})).items():
        # Reference solutions use the task's declared source scope, like every submission.
        reference = _object(value)
        if reference.get("status") != "available":
            continue
        reference["code_size"] = scoped_code_size(
            before=evidence / str(_object(reference["before"])["path"]),
            after=evidence / str(_object(reference["after"])["path"]),
            scope=TypeAdapter(list[str]).validate_python(
                _object(_object(config["task_scopes"])[task])["source_scope"]
            ),
            change=_object(reference["change"]),
        )
    prices = root / "prices.json"
    if prices.is_file():
        # Dated prices are reporting input, not measurement settings, so they stay outside the binding.
        config["prices"] = _object(json.loads(prices.read_text()))
    config["measurement_collection"] = _measurement_collection(root, config)
    return write_evaluation_report(report, config, output)
