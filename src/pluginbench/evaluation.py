"""One evaluation: prepare tasks, run vanilla and guidance, measure, optionally review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal
from collections.abc import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from pluginbench import claude, codex
from pluginbench.assessment_metrics import scoped_code_size, scoped_test_size
from pluginbench.datasets import TaskCatalog, TaskEntry
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

ADAPTER = Path(__file__).parent / "task_data/deepswe"
SUPPORTED_TASKS = (
    "ofetch-per-origin-circuit-breaker",
    "testem-per-launcher-reports",
    "happy-dom-abort-pending-body-reads",
    "true-myth-iterable-collection-combinators",
    "happy-dom-deterministic-intersectionobserver",
)
SUITE_SHA = "0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea"
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


class Tasks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Path
    ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def supported(self) -> Tasks:
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("task IDs must be unique")
        unsupported = set(self.ids) - set(SUPPORTED_TASKS)
        if unsupported:
            raise ValueError(
                f"No calibrated adapter for: {', '.join(sorted(unsupported))}. Supported DeepSWE tasks: {', '.join(SUPPORTED_TASKS)}"
            )
        return self


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="evaluation", min_length=1)
    runner: Runner
    guidance: dict[str, list[Path]] = Field(min_length=1)
    tasks: Tasks
    measurements: list[Measurement] = Field(min_length=1)
    reviewer: Reviewer | None = None
    repeats: int = Field(default=3, strict=True, ge=1)
    seed: int = Field(default=42, strict=True)
    timeout_seconds: int = Field(default=1800, strict=True, ge=1)
    jobs: int = Field(default_factory=lambda: min(4, os.cpu_count() or 1), strict=True, ge=1, le=16)

    @model_validator(mode="after")
    def coherent(self) -> Evaluation:
        if len(set(self.measurements)) != len(self.measurements):
            raise ValueError("measurements must be unique")
        if ("review" in self.measurements) != (self.reviewer is not None):
            raise ValueError("provide reviewer settings exactly when review is selected")
        if any(
            not name.strip() or name in {"baseline", "vanilla"} or not paths
            for name, paths in self.guidance.items()
        ):
            raise ValueError("guidance needs nonempty named sets; vanilla is automatic")
        return self


def _resolve(base: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).absolute()


def resolve_evaluation(raw: object, base: Path) -> Evaluation:
    config = Evaluation.model_validate(raw)
    config.tasks.source = _resolve(base, config.tasks.source)
    for name, paths in config.guidance.items():
        files = [file for path in paths for file in _text_files(_resolve(base, path))]
        if len(set(files)) != len(files):
            raise ValueError(f"guidance supplies the same text twice: {name}")
        config.guidance[name] = files
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


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if "__pycache__" in file.parts:
            continue
        if file.is_symlink():
            raise ValueError(f"task input contains a symlink: {file}")
        if file.is_file():
            digest.update(str(file.relative_to(path)).encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def plan_evaluation(config: Evaluation) -> dict[str, object]:
    source = config.tasks.source
    suite = source.parent if source.name == "tasks" else source
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=suite, text=True).strip()
    if revision != SUITE_SHA:
        raise ValueError(f"DeepSWE tasks must be checked out at {SUITE_SHA}; found {revision}")
    identities = {}
    for task in config.tasks.ids:
        path = suite / "tasks" / task
        for required in ("task.toml", "instruction.md", "environment", "tests", "solution"):
            if not (path / required).exists():
                raise ValueError(f"missing Harbor task input: {path / required}")
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--", f"tasks/{task}"], cwd=suite, text=True
        )
        if dirty:
            raise ValueError(f"selected DeepSWE task has uncommitted changes: {task}")
        identities[task] = _tree_hash(path)
    preset = json.loads((ADAPTER / "measurement-presets.json").read_text())
    methods = {task: preset[task] for task in config.tasks.ids}
    criteria = (
        _fixed_criteria([file.read_text() for file in config.reviewer.guidance], "handoff")
        if config.reviewer
        else None
    )
    comparison = {
        "tasks": identities,
        "suite_revision": revision,
        "runner": config.runner.model_dump(mode="json"),
        "measurement_profiles": methods,
        "measurement_adapter_sha256": _tree_hash(ADAPTER),
        "measurements": sorted(config.measurements),
        "reviewer": config.reviewer.model_dump(mode="json", exclude={"guidance"})
        if config.reviewer
        else None,
        "review_criteria_sha256": _criteria_digest(criteria) if criteria else None,
        "jobs": config.jobs,
    }
    coding = len(config.tasks.ids) * (1 + len(config.guidance)) * config.repeats
    return {
        "name": config.name,
        "coding_calls": coding,
        "review_calls": coding if config.reviewer else 0,
        "runner": config.runner.model_dump(mode="json"),
        "guidance": list(config.guidance),
        "tasks": config.tasks.ids,
        "measurements": config.measurements,
        "repeats": config.repeats,
        "jobs": config.jobs,
        "baseline": "fresh vanilla attempts, no supplied text guidance; native built-in instructions remain",
        "comparison_identity": comparison,
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def run_evaluation(
    config: Evaluation, output: Path, *, progress: Callable[[str], None] | None = None
) -> Path:
    plan = plan_evaluation(config)
    comparison = plan["comparison_identity"]
    if not isinstance(comparison, dict):
        raise ValueError("evaluation comparison identity is malformed")
    root = output.expanduser().absolute()
    if root.exists():
        raise ValueError(f"output must be a new directory: {root}")
    # Both runtimes are checked before any task calls; preparation validates selected observers too.
    for harness in {config.runner.harness, *([config.reviewer.harness] if config.reviewer else [])}:
        (claude.preflight if harness == "claude" else codex.preflight)()
        with tempfile.TemporaryDirectory(prefix="pluginbench-auth-check-") as temporary:
            codex.runtime_environment(Path(temporary) / "private", harness)
    root.mkdir(parents=True)
    prepared = root / "inputs"
    prepared.mkdir()
    evidence = root / "evidence"
    entries: list[TaskEntry] = []
    profiles: dict[str, list[Collector]] = {}
    scopes = {}
    task_environment = {}
    for task in config.tasks.ids:
        if progress:
            progress(f"Preparing and calibrating {task}")
        destination = prepared / task
        subprocess.run(
            [
                sys.executable,
                str(ADAPTER / "prepare.py"),
                "--suite",
                str(config.tasks.source),
                "--task",
                task,
                "--output",
                str(destination),
                *[
                    arg
                    for measurement in config.measurements
                    for arg in ("--measurement", measurement)
                ],
            ],
            check=True,
        )
        task_catalog = TaskCatalog.model_validate_json((destination / "tasks.json").read_text())
        for item in task_catalog.tasks:
            item.fixture = Path(task) / item.fixture
            item.verifier.script = Path(task) / item.verifier.script
            entries.append(item)
        profiles.update(load_profiles(destination / "measurements.json"))
        suite = (
            config.tasks.source.parent
            if config.tasks.source.name == "tasks"
            else config.tasks.source
        )
        subprocess.run(
            [
                sys.executable,
                str(ADAPTER / "calibrate.py"),
                "--suite",
                str(suite),
                "--prepared",
                str(destination),
            ],
            check=True,
        )
        provenance = json.loads((destination / "provenance.json").read_text())
        scopes[task] = {
            "source_scope": json.loads(provenance["source_scope"]),
            "test_scope": json.loads(provenance["test_scope"]),
            "version": provenance["measurement_profile_version"],
        }
        task_environment[task] = {
            key: provenance.get(key)
            for key in (
                "base_commit",
                "node",
                "package_manager",
                "lock_sha256",
                "dependency_sha256",
                "test_patch_sha256",
                "instruction_sha256",
            )
        }
    _write(
        prepared / "tasks.json",
        TaskCatalog(schema_version=1, tasks=entries).model_dump(mode="json"),
    )
    # Freeze fixed review criteria before coding; candidates never receive this separate packet.
    fixed_guidance = []
    if config.reviewer:
        for index, source in enumerate(config.reviewer.guidance):
            target = prepared / f"review-{index}.md"
            shutil.copy2(source, target)
            fixed_guidance.append(target)
        if (
            _criteria_digest(
                _fixed_criteria([file.read_text() for file in fixed_guidance], "handoff")
            )
            != comparison["review_criteria_sha256"]
        ):
            raise ValueError("fixed review guidance changed during preparation")
    recipe = {
        "schema_version": 1,
        "name": config.name,
        "harness": config.runner.harness,
        "tasks": str(prepared / "tasks.json"),
        "guidance": {
            name: [str(file) for file in files] for name, files in config.guidance.items()
        },
        "model": config.runner.model,
        "reasoning": config.runner.reasoning,
        "repeats": config.repeats,
        "seed": config.seed,
        "timeout_seconds": config.timeout_seconds,
        "jobs": config.jobs,
    }
    _write(prepared / "study.json", recipe)
    selection = {
        **config.model_dump(mode="json"),
        "comparison_identity": plan["comparison_identity"],
        "task_scopes": scopes,
        "artifact_base": "evidence",
        "measurement_revision": None,
        "review_revision": None,
    }
    comparison["task_environment"] = task_environment
    _save_selection(root, selection)
    run_study(load_study(prepared / "study.json"), evidence, progress=progress)
    selected_profiles = {task: collectors for task, collectors in profiles.items() if collectors}
    return _finish_evaluation(
        root,
        selection=selection,
        profiles=selected_profiles,
        guidance=fixed_guidance,
        progress=progress,
    )


def _saved_evaluation(selection: dict[str, object]) -> Evaluation:
    # Runs created before jobs existed used sequential execution.
    return Evaluation.model_validate(
        {
            key: selection.get(key, 1) if key == "jobs" else selection[key]
            for key in Evaluation.model_fields
        }
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
            allow_partial=True,
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
    elif set(config.measurements) & {
        "coverage",
        "test_overlap",
        "cognitive",
        "cyclomatic",
        "dead_code",
    }:
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
            # The calibrated coverage adapter is the only script refreshed on recovery.
            # Its exact bytes are frozen and recorded in the new observer revision.
            for collectors in profiles.values():
                for collector in collectors:
                    if (
                        collector.kind == "coverage"
                        and collector.command
                        and "--before" in collector.command
                    ):
                        collector.command[1] = str(ADAPTER / "coverage.py")
                        collector.failure_protocol = "coverage-exit-v1"
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
            criteria = _fixed_criteria([file.read_text() for file in guidance], "handoff")
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
        "jobs": selection.get("jobs", 1),
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
        outcomes = observation.get("outcomes")
        if outcomes is None:  # Validated v1 revisions require every family to match.
            available += len(observation["repeats"][0])
            continue
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


def regenerate_report(root: Path) -> Path:
    root = root.expanduser().absolute()
    config = _load_selection(root)
    config.setdefault("jobs", 1)
    comparison = _object(config["comparison_identity"])
    comparison.setdefault("jobs", config["jobs"])
    config["comparison_identity"] = comparison
    evidence = root / "evidence"
    report_path, _ = report_study(
        evidence,
        measurement_revision=_selected_revision(config, "measurement_revision"),
        review_revision=_selected_revision(config, "review_revision"),
    )
    report = json.loads(report_path.read_text())
    recipe = json.loads((evidence / "frozen/resolved-recipe.json").read_text())
    packages = {}
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
    return write_evaluation_report(report, config, root / "report")
