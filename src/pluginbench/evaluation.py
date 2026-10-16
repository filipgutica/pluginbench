"""One evaluation: prepare tasks, run vanilla and guidance, measure, optionally review."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal
from collections.abc import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from pluginbench import claude, codex, harbor_backend, submissions
from pluginbench.treatments import Treatment, resolve_treatment, treatment_inventory
from pluginbench.native_treatments import NativeCapabilities, DELIVERY_POLICY, admit
from pluginbench.task_sources import Tasks
from pluginbench.measurement_profiles import (
    MeasurementSettings,
    capability_plan,
    prepare_profiles,
    probe_measurements,
)
from pluginbench.assessment_metrics import scoped_code_size, scoped_test_size
from pluginbench.datasets import TaskCatalog, TaskEntry, portable_native_identity
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
from pluginbench.study import _text_files, load_study, report_study, run_study
from pluginbench.study_review import (
    _criteria_digest,
    _fixed_criteria,
    recover_review_citations,
    review_study,
    validate_review_revision,
)

Measurement = Literal[
    "tokens", "lines", "cyclomatic", "cognitive", "dead_code", "coverage", "test_overlap", "review"
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
    guidance: list[Path] = Field(default_factory=list)


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="evaluation", min_length=1)
    runner: Runner
    treatments: dict[str, Treatment] = Field(min_length=1)
    capabilities: NativeCapabilities = Field(default_factory=NativeCapabilities)
    tasks: Tasks
    measurements: list[Measurement] = Field(min_length=1)
    measurement_settings: MeasurementSettings = Field(default_factory=MeasurementSettings)
    reviewer: Reviewer | None = None
    repeats: int = Field(default=3, strict=True, ge=1)
    seed: int = Field(default=42, strict=True)
    timeout_seconds: int = Field(default=1800, strict=True, ge=1)
    jobs: int = Field(default_factory=lambda: min(4, os.cpu_count() or 1), strict=True, ge=1, le=16)
    container_cpus: int | None = Field(default=None, strict=True, ge=1)
    container_memory_mb: int | None = Field(default=None, strict=True, ge=256)

    @model_validator(mode="before")
    @classmethod
    def legacy_guidance(cls, value: object) -> object:
        return study.normalize_legacy_guidance(value)

    @model_validator(mode="after")
    def coherent(self) -> Evaluation:
        if self.runner.reasoning == "ultra":
            raise ValueError("Harbor does not support ultra reasoning; select a supported effort")
        if len(set(self.measurements)) != len(self.measurements):
            raise ValueError("measurements must be unique")
        if ("review" in self.measurements) != (self.reviewer is not None):
            raise ValueError("provide reviewer settings exactly when review is selected")
        if any(
            not name.strip() or name in {"baseline", "vanilla"}
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
    submission_sources: dict[str, dict[str, object]] = {}
    for task in config.tasks.task_ids:
        path = suite / "tasks" / task
        admission = harbor_backend.validate_native_task(path)
        identities[task] = admission["task_assets_sha256"]
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
    jobs = min(config.jobs, harbor_backend.capacity_jobs(**limits))
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
        "treatments": {
            name: treatment_inventory(item) for name, item in config.treatments.items()
        },
        "delivery": DELIVERY_POLICY,
        "capabilities": config.capabilities.model_dump(mode="json"),
        "tasks": config.tasks.task_ids,
        "measurements": config.measurements,
        "measurement_capabilities": capabilities,
        "submission_sources": submission_sources,
        "repeats": config.repeats,
        "jobs": jobs,
        "container_limits": limits,
        "baseline": "reuse a verified isolated vanilla cohort when available; otherwise fresh vanilla attempts; native built-in instructions remain",
        "call_estimate": "maximum new root attempts; native subagent turns are additional and metered from session traces; baseline hits resolve before coding",
        "comparison_identity": comparison,
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def run_evaluation(
    config: Evaluation, output: Path, *, progress: Callable[[str], None] | None = None
) -> Path:
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
    root = output.expanduser().absolute()
    if root.exists():
        raise ValueError(f"output must be a new directory: {root}")
    harbor_backend.preflight(jobs=config.jobs, **limits)
    harness_versions = {}
    for harness in {config.runner.harness, *([config.reviewer.harness] if config.reviewer else [])}:
        native = (claude.preflight if harness == "claude" else codex.preflight)()
        version = str(native.get("harness_version", ""))
        match = re.search(r"(?<![\w.+-])\d+\.\d+\.\d+(?![\w.+-])", version)
        if match is None:
            raise ValueError(f"cannot pin {harness} CLI version: {version}")
        harness_versions[harness] = match.group()
        with tempfile.TemporaryDirectory(prefix="pluginbench-auth-check-") as temporary:
            codex.runtime_environment(Path(temporary) / "private", harness)
    root.mkdir(parents=True)
    prepared = root / "inputs"
    prepared.mkdir()
    evidence = root / "evidence"
    entries = []
    profiles = {}
    scopes = {}
    task_environment = {}
    capabilities = {}
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
            cache=root.parent / ".prepared-images/native",
        )
        native_task = Path(str(identity["task_dir"]))
        fixture = Path(str(identity["fixture_dir"]))
        if identity["original_task_assets_sha256"] != _object(comparison["tasks"])[task]:
            raise ValueError("native task assets changed after planning")
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
        capabilities[task] = provenance["capabilities"]
        capabilities[task] = probe_measurements(
            fixture,
            destination / "measurement-readiness",
            profiles[task],
            config.measurements,
            _object(capabilities[task]),
        )
        qualification = _object(identity.get("source_qualification", {}))
        qualified = (
            identity.get("source_policy") == submissions.SOURCE_POLICY
            and qualification.get("status") == "complete"
        )
        capabilities[task]["submission_source"] = {
            "status": "available" if qualified else "unavailable",
            "source_policy": submissions.SOURCE_POLICY,
            "reason": None
            if qualified
            else qualification.get(
                "reason", "canonical submission source has no compatible preparation proof"
            ),
        }
        task_environment[task] = portable_native_identity(identity)
        if progress:
            for metric, capability in _object(capabilities[task]).items():
                row = _object(capability)
                if row["status"] == "unavailable":
                    progress(f"{task}: {metric} unavailable — {row['reason']}")
    _write(
        prepared / "tasks.json",
        TaskCatalog(schema_version=1, tasks=entries).model_dump(mode="json"),
    )
    _write(root / "measurement-readiness.json", capabilities)
    if config.measurement_settings.require_ready and any(
        row.get("status") == "unavailable"
        for task in capabilities.values()
        for row in _object(task).values()
        if isinstance(row, dict)
    ):
        raise ValueError(
            "Selected measurements or submission sources are not ready; no coding calls were made. See measurement-readiness.json. Repair the inputs and start a new evaluation."
        )
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
    recipe = {
        "schema_version": 2,
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
        "artifact_base": "evidence",
        "measurement_required": any(profiles.values()),
        "measurement_revision": None,
        "review_revision": None,
    }
    _save_selection(root, selection)
    coding_identity = {
        "delivery": DELIVERY_POLICY,
        "capabilities": config.capabilities.model_dump(mode="json"),
        "task_assets": comparison["tasks"],
        "task_environment": task_environment,
        "runner": comparison["runner"],
        "harness_version": harness_versions[config.runner.harness],
        "isolation": comparison["isolation"],
        "timeout_seconds": config.timeout_seconds,
        "jobs": config.jobs,
        "seed": config.seed,
        "launcher_sha256": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in (
                "codex.py",
                "harbor_backend.py",
                "processes.py",
                "native_treatments.py",
                "native_treatment_runtime.js",
                "treatments.py",
                *(["claude.py"] if config.runner.harness == "claude" else []),
            )
        },
    }
    run_study(
        load_study(prepared / "study.json"),
        evidence,
        progress=progress,
        baseline_cache=root.parent / ".baseline-cache",
        baseline_identity=coding_identity,
    )
    # Measurement contexts refer to the same run-owned frozen task assets as grading/review.
    frozen_catalog = TaskCatalog.model_validate_json((evidence / "frozen/tasks.json").read_text())
    frozen_tasks = {
        entry.id: evidence / "frozen" / entry.native_task for entry in frozen_catalog.tasks
    }
    for task, collectors in profiles.items():
        for collector in collectors:
            if collector.kind == "analysis" or (
                collector.kind == "coverage" and collector.report_format is not None
            ):
                payload = json.loads(collector.config.read_text())
                payload["native_task_identity"]["task_dir"] = str(frozen_tasks[task])
                _write(collector.config, payload)
    return _finish_evaluation(
        root,
        selection=selection,
        profiles={task: collectors for task, collectors in profiles.items() if collectors},
        guidance=fixed_guidance,
        progress=progress,
    )


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
        revision = "measurements"
        index = 2
        while (evidence / "observer-revisions" / revision).exists():
            revision = f"measurements-{index}"
            index += 1
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
            timeout=config.timeout_seconds,
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


def _resume_inputs(
    root: Path,
) -> tuple[dict[str, object], dict[str, list[Collector]], list[Path], int]:
    selection = _load_selection(root)
    config = _saved_evaluation(selection)
    evidence = root / "evidence"
    run, manifest, original = _native_state(evidence)
    profiles: dict[str, list[Collector]] = {}
    if selection["measurement_revision"] is not None:
        validate_remeasurement(
            evidence, evidence / "observer-revisions" / str(selection["measurement_revision"])
        )
    elif selection["measurement_required"]:
        candidates = (
            selection.get("pending_measurement_revision", "measurements"),
            selection.get("recovery_measurement_revision"),
        )
        for candidate in candidates:
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
            state = json.loads((directory / "revision.json").read_text())
            if state["status"] != "failed" or all(
                key in state for key in ("profile_inventory", "frozen_profiles")
            ):
                break
        else:
            raise ValueError("observer recovery has no saved frozen profiles")
        selection["pending_measurement_revision"] = revision
        if state["status"] == "complete":
            validate_remeasurement(evidence, directory)
            _adopt_measurement_revision(root, selection, revision)
        elif state["status"] == "failed":
            if (
                study._bound_document(evidence, state["source_run"]) != run
                or study._bound_path(evidence, state["source_assessment"]) != original
                or _preserved_inventory(evidence, manifest, original.parent) != state["preserved"]
            ):
                raise ValueError("saved native inputs changed after failed observer collection")
            if study._inventory(directory / "profiles") != state["profile_inventory"]:
                raise ValueError("frozen observer profiles changed")
            profiles = load_profiles(study._bound_path(evidence, state["frozen_profiles"]))
        else:
            raise ValueError("observer collection is still marked active; cannot replay it")
    guidance = []
    calls = 0
    if config.reviewer:
        if selection["review_revision"] is not None:
            validate_review_revision(evidence, str(selection["review_revision"]))
        else:
            if (evidence / "review-revisions/review").exists():
                raise ValueError("review evidence already exists; model calls cannot be replayed")
            guidance = [
                root / "inputs" / f"review-{index}.md"
                for index in range(len(config.reviewer.guidance))
            ]
            criteria = _fixed_criteria([file.read_text() for file in guidance])
            if (
                _criteria_digest(criteria)
                != _object(selection["comparison_identity"])["review_criteria_sha256"]
            ):
                raise ValueError("frozen review criteria changed")
            calls = sum(record.stages[-1].snapshot is not None for record in manifest.records)
    return selection, profiles, guidance, calls


def plan_resume(root: Path) -> dict[str, object]:
    selection, profiles, _, calls = _resume_inputs(root.expanduser().absolute())
    return {
        "name": selection["name"],
        "coding_calls": 0,
        "review_calls": calls,
        "collect_measurements": bool(profiles),
        "jobs": selection["jobs"],
        "baseline": "reuse immutable saved submissions; no coding replay",
    }


def resume_evaluation(root: Path, *, progress: Callable[[str], None] | None = None) -> Path:
    root = root.expanduser().absolute()
    selection, profiles, guidance, calls = _resume_inputs(root)
    if calls:
        reviewer = _object(selection["reviewer"])
        harness = str(reviewer["harness"])
        (claude.preflight if harness == "claude" else codex.preflight)()
        with tempfile.TemporaryDirectory(prefix="pluginbench-auth-check-") as temporary:
            codex.runtime_environment(Path(temporary) / "private", harness)
    return _finish_evaluation(
        root, selection=selection, profiles=profiles, guidance=guidance, progress=progress
    )


def _save_selection(root: Path, selection: object) -> None:
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
    root = root.expanduser().absolute()
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
            "timeout_seconds": config["timeout_seconds"],
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
                resolve_treatment(Treatment.model_validate(item), evidence / "frozen", recipe["harness"])
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
    input_path = evidence / manifest["assessment_input"]["path"]
    records = {row["id"]: row for row in json.loads(input_path.read_text())["records"]}
    for attempt in report["attempts"]:
        scope = _object(_object(config["task_scopes"])[attempt["task_id"]])
        # Canonical attempt contains digest only; bound input owns the snapshot paths.
        record = records[attempt["id"]]
        if record["before"] is None:
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
    config["measurement_collection"] = _measurement_collection(root, config)
    return write_evaluation_report(report, config, output)
