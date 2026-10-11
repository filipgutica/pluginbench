"""One native Codex study: frozen text conditions, independent attempts, one report."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from functools import partial
from threading import Event
from concurrent.futures import CancelledError
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pluginbench import claude, codex, pier_backend
from pluginbench.parallel import run_jobs, validate_jobs
from pluginbench.assessment import build_assessment, write_assessment
from pluginbench.assessment_metrics import EXCLUDED_NAMES, inspect_snapshot
from pluginbench.assessment_models import (
    ArtifactRef,
    AssessmentInput,
    CorrectnessReceipt,
    Standards,
)
from pluginbench.collectors import (
    Collector,
    collect_measurements,
    freeze_collectors,
    resolve_collector_command,
)
from pluginbench.config import DatasetConfig
from pluginbench.datasets import TaskCatalog, TaskSpec, load_tasks


class StudyRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    backend: Literal["local", "pier"] = "local"
    harness: Literal["codex", "claude"] = "codex"
    tasks: Path
    codex_version: str = Field(default="0.162.0", pattern=r"^\d+\.\d+\.\d+$")
    pier_python: Path = Path(sys.executable)
    guidance: dict[str, list[Path]] = Field(min_length=1)
    model: str = Field(default="gpt-6.1-sol", min_length=1)
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "medium"
    repeats: int = Field(default=3, strict=True, ge=1)
    jobs: int = Field(default=1, strict=True, ge=1, le=16)
    timeout_seconds: int = Field(default=600, strict=True, ge=1)
    seed: int = Field(default=42, strict=True)
    max_repairs: int = Field(default=0, strict=True, ge=0, le=3)
    auth_file: Path = Path("~/.codex/auth.json")
    standards: Path | None = None
    collectors: list[Collector] = Field(default_factory=list)

    @field_validator("guidance")
    @classmethod
    def condition_names(cls, value: dict[str, list[Path]]) -> dict[str, list[Path]]:
        if any(
            not name.strip() or name == "baseline" or not paths for name, paths in value.items()
        ):
            raise ValueError("guidance needs named, nonempty conditions; baseline is automatic")
        return value

    @field_validator("collectors")
    @classmethod
    def distinct_collectors(cls, value: list[Collector]) -> list[Collector]:
        if len({collector.kind for collector in value}) != len(value):
            raise ValueError("each collector kind may appear only once")
        return value

    @model_validator(mode="after")
    def supported_repairs(self) -> StudyRecipe:
        if self.backend == "pier" and self.max_repairs:
            raise ValueError("Pier studies currently support max_repairs: 0 only")
        if self.backend == "pier" and self.harness != "codex":
            raise ValueError("Pier execution currently supports Codex only")
        if self.harness == "claude" and self.reasoning not in {
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("unsupported Claude effort")
        return self


@dataclass(frozen=True)
class ResolvedStudy:
    recipe: StudyRecipe
    tasks: tuple[TaskSpec | pier_backend.PierTask, ...]
    guidance: dict[str, tuple[Path, ...]]
    source: Path
    guidance_selection: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def _resolve(base: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).resolve()


def _text_files(path: Path) -> list[Path]:
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError(f"guidance must not be a symlink: {path}")
    all_candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
    for candidate in all_candidates:
        if candidate.is_symlink():
            raise ValueError(f"guidance contains a symlink: {candidate}")
    selected = path / "skills" if (path / "skills").is_dir() else path
    if selected.is_dir() and not (selected / "SKILL.md").is_file():
        skill_roots = [item for item in selected.iterdir() if (item / "SKILL.md").is_file()]
        candidates = (
            [file for skill in sorted(skill_roots) for file in sorted(skill.rglob("*"))]
            if skill_roots
            else all_candidates
        )
    else:
        candidates = sorted(selected.rglob("*")) if selected.is_dir() else [selected]
    files = []
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in {".md", ".txt"}:
            if not path.is_dir():
                raise ValueError(f"select text guidance only (.md/.txt): {candidate}")
            continue
        candidate.read_text(encoding="utf-8")
        files.append(candidate.resolve())
    if not files:
        raise ValueError(f"guidance contains no text: {path}")
    return files


def load_study(path: Path) -> ResolvedStudy:
    source = path.resolve()
    recipe = StudyRecipe.model_validate(yaml.safe_load(source.read_text()))
    base = source.parent
    recipe.tasks = _resolve(base, recipe.tasks)
    recipe.auth_file = _resolve(base, recipe.auth_file)
    # Resolving a venv's Python symlink selects the base interpreter and loses its packages.
    python = recipe.pier_python.expanduser()
    recipe.pier_python = python.absolute() if python.is_absolute() else (base / python).absolute()
    recipe.standards = _resolve(base, recipe.standards) if recipe.standards else None
    if recipe.standards:
        Standards.model_validate(yaml.safe_load(recipe.standards.read_text()))
    tasks: tuple[TaskSpec | pier_backend.PierTask, ...] = (
        pier_backend.load_tasks(recipe.tasks)
        if recipe.backend == "pier"
        else _load_local_tasks(recipe)
    )
    guidance = {}
    selection = {}
    for name, paths in recipe.guidance.items():
        files = []
        skipped: list[str] = []
        for item in paths:
            expanded = item.expanduser()
            candidate = expanded if expanded.is_absolute() else base / expanded
            selected = _text_files(candidate)
            files.extend(selected)
            if candidate.is_dir():
                skipped.extend(
                    str(file.resolve())
                    for file in sorted(candidate.rglob("*"))
                    if file.is_file() and file.resolve() not in selected
                )
        if len(set(files)) != len(files):
            raise ValueError(f"guidance condition supplies the same text twice: {name}")
        guidance[name] = tuple(files)
        selection[name] = {"supplied_text": [str(file) for file in files], "skipped": skipped}
    recipe.guidance = {name: list(files) for name, files in guidance.items()}
    for collector in recipe.collectors:
        collector.config = _resolve(base, collector.config)
        for command_field in ("command", "syntax_command"):
            command = getattr(collector, command_field)
            if command:
                setattr(
                    collector,
                    command_field,
                    resolve_collector_command(base, command),
                )
    return ResolvedStudy(recipe, tasks, guidance, source, selection)


def _load_local_tasks(recipe: StudyRecipe) -> tuple[TaskSpec, ...]:
    catalog = TaskCatalog.model_validate(yaml.safe_load(recipe.tasks.read_text()))
    for entry in catalog.tasks:
        for item in (entry.fixture, entry.verifier.script):
            if item.is_absolute() or ".." in item.parts:
                raise ValueError(
                    "task fixtures and verifier scripts must be relative to their catalog"
                )
            raw_path = recipe.tasks.parent / item
            if any(candidate.is_symlink() for candidate in (raw_path, *raw_path.parents)):
                raise ValueError(f"task input must not be a symlink: {raw_path}")
    tasks = load_tasks(
        DatasetConfig(
            name=recipe.name,
            version="1",
            source=recipe.tasks,
            task_ids=[task.id for task in catalog.tasks],
        )
    )
    for task in tasks:
        assert task.fixture is not None
        codex.reject_guidance(task.fixture, include_ancestors=False, reject_git=True)
    return tuple(tasks)


def plan_study(study: ResolvedStudy) -> dict[str, Any]:
    recipe = study.recipe
    entries = [
        {"task_id": task.task_id, "arm": arm, "attempt": repeat}
        for repeat in range(1, recipe.repeats + 1)
        for task in study.tasks
        for arm in ["baseline", *study.guidance]
    ]
    random.Random(recipe.seed).shuffle(entries)
    for index, entry in enumerate(entries):
        identity = json.dumps([recipe.seed, index, entry], sort_keys=True).encode()
        entry["id"] = hashlib.sha256(identity).hexdigest()[:16]
    return {
        "schema_version": 1,
        "name": recipe.name,
        "host": recipe.harness,
        "backend": recipe.backend,
        "codex_version": recipe.codex_version if recipe.backend == "pier" else None,
        "model": recipe.model,
        "requested_reasoning_effort": recipe.reasoning,
        "delivery": "forced text in task prompt; automatic activation is not measured",
        "guidance_selection": study.guidance_selection,
        "baseline": "no supplied custom guidance; native built-in instructions remain",
        "seed": recipe.seed,
        "jobs": recipe.jobs,
        "initial_calls": len(entries),
        "maximum_calls": len(entries) * (1 + recipe.max_repairs),
        "repair_policy": "same external verifier feedback, bounded repairs; not independent review",
        "entries": entries,
    }


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _reference(root: Path, path: Path, *, snapshot: bool = False) -> dict[str, str]:
    digest = str(inspect_snapshot(path)["digest"]) if snapshot else _digest(path)
    return {"path": path.relative_to(root).as_posix(), "sha256": digest}


def _inventory(root: Path) -> dict[str, str]:
    inventory = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"frozen input contains a symlink: {path}")
        if path.is_file():
            inventory[path.relative_to(root).as_posix()] = _digest(path)
            inventory[path.relative_to(root).as_posix() + ":mode"] = str(
                path.stat().st_mode & 0o777
            )
    return inventory


def _assert_frozen(root: Path) -> None:
    expected = json.loads((root / "frozen-inventory.json").read_text())
    if _inventory(root / "frozen") != expected:
        raise ValueError("frozen study inputs changed")


def _freeze(study: ResolvedStudy, root: Path) -> ResolvedStudy:
    frozen = root / "frozen"
    frozen.mkdir()
    shutil.copyfile(study.source, frozen / "source-recipe.yaml")
    tasks: tuple[TaskSpec | pier_backend.PierTask, ...]
    if study.recipe.backend == "pier":
        tasks = pier_backend.freeze_tasks(
            tuple(task for task in study.tasks if isinstance(task, pier_backend.PierTask)),
            frozen / "suite",
        )
        source = yaml.safe_load(study.recipe.tasks.read_text())["source"]
        _write(
            frozen / "tasks.json",
            {
                "schema_version": 1,
                "source": source,
                "tasks": [
                    {"id": task.task_id, "path": str(task.directory.relative_to(frozen))}
                    for task in tasks
                    if isinstance(task, pier_backend.PierTask)
                ],
            },
        )
    else:
        _freeze_local_tasks(study, frozen)
        tasks = ()
    runner = frozen / "runner"
    runner.mkdir()
    for source_file in sorted(Path(__file__).parent.glob("*.py")):
        shutil.copyfile(source_file, runner / source_file.name)
    _write(frozen / "guidance-selection.json", study.guidance_selection)
    guidance: dict[str, list[Path]] = {}
    for index, (name, files) in enumerate(study.guidance.items()):
        guidance[name] = []
        for file_index, source_file in enumerate(files):
            target = frozen / "guidance" / str(index) / str(file_index) / source_file.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target)
            guidance[name].append(target)
    recipe = study.recipe.model_copy(deep=True)
    recipe.tasks = frozen / "tasks.json"
    recipe.guidance = guidance
    recipe.collectors = freeze_collectors(recipe.collectors, frozen / "collectors")
    standards = Standards(
        schema_version=1, required_correctness_checks=["behavior", "native_completion"]
    )
    if recipe.standards:
        shutil.copyfile(recipe.standards, frozen / "source-standards.yaml")
        standards = Standards.model_validate(yaml.safe_load(recipe.standards.read_text()))
        if "native_completion" not in standards.required_correctness_checks:
            standards.required_correctness_checks.append("native_completion")
    if recipe.backend == "pier":
        for check in ("delivery_complete", "submission_collected"):
            if check not in standards.required_correctness_checks:
                standards.required_correctness_checks.append(check)
    recipe.standards = frozen / "standards.yaml"
    _write(recipe.standards, standards.model_dump(mode="json"))
    public_recipe = recipe.model_dump(mode="json", exclude={"auth_file"})
    _write(frozen / "resolved-recipe.json", public_recipe)
    _write(root / "frozen-inventory.json", _inventory(frozen))
    if recipe.backend == "local":
        tasks = _load_local_tasks(recipe)
    return ResolvedStudy(
        recipe,
        tasks,
        {key: tuple(value) for key, value in guidance.items()},
        study.source,
        study.guidance_selection,
    )


def _freeze_local_tasks(study: ResolvedStudy, frozen: Path) -> None:
    entries = []
    catalog = TaskCatalog.model_validate(yaml.safe_load(study.recipe.tasks.read_text()))
    declared = {entry.id: entry for entry in catalog.tasks}
    for task in study.tasks:
        assert isinstance(task, TaskSpec)
        assert task.fixture is not None and task.verifier is not None
        entry = declared[task.task_id]
        fixture = frozen / "suite" / entry.fixture
        if not fixture.exists():
            shutil.copytree(task.fixture, fixture)
        script = frozen / "suite" / entry.verifier.script
        script.parent.mkdir(parents=True, exist_ok=True)
        if not script.exists():
            shutil.copy2(task.verifier.script, script)
        entries.append(
            {
                "id": task.task_id,
                "prompt": task.prompt,
                "fixture": str(fixture.relative_to(frozen)),
                "provenance": task.provenance,
                "verifier": {
                    "command": list(task.verifier.command[:-1]),
                    "script": str(script.relative_to(frozen)),
                    "timeout_seconds": task.verifier.timeout_seconds,
                },
            }
        )
    _write(frozen / "tasks.json", {"schema_version": 1, "tasks": entries})


def _prompt(task: TaskSpec | pier_backend.PierTask, guidance: tuple[Path, ...]) -> str:
    if not guidance:
        return task.prompt
    text = "\n\n".join(
        f"Guidance text {index + 1} ({path.name}):\n{path.read_text()}"
        for index, path in enumerate(guidance)
    )
    return (
        "Apply the supplied guidance to this task. Use the supplied document text for references "
        "to other supplied documents; source-host paths and plugins may be unavailable here. "
        "The task's execution protocol takes precedence.\n\n"
        f"{text}\n\nTask:\n{task.prompt}"
    )


def _grade(snapshot: Path, task: TaskSpec, directory: Path) -> dict[str, Any]:
    assert task.verifier is not None
    workspace = directory / "workspace"
    shutil.copytree(snapshot, workspace)
    before = inspect_snapshot(workspace)["digest"]
    try:
        result = subprocess.run(
            task.verifier.command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=task.verifier.timeout_seconds,
            check=False,
            env={"PATH": os.environ["PATH"], "PLUGINBENCH_TASK_ID": task.task_id},
        )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            report = None
        declared = report.get("pass") if isinstance(report, dict) else None
        if isinstance(declared, bool):
            passed = declared if result.returncode == (0 if declared else 1) else None
        else:
            passed = (result.returncode == 0) if result.returncode in (0, 1) else None
        immutable = inspect_snapshot(workspace)["digest"] == before
        return {
            "pass": passed if immutable else False,
            "report": report,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "grader_immutable": immutable,
        }
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"pass": None, "error": str(exc)}


def _collect(
    root: Path,
    snapshot: Path,
    directory: Path,
    collectors: list[Collector],
    *,
    before: Path | None = None,
) -> dict[str, dict[str, str]]:
    files = collect_measurements(snapshot, directory, collectors, before=before)
    return {kind: _reference(root, path) for kind, path in files.items()}


def _work(execution: dict[str, Any]) -> dict[str, Any]:
    return {
        "elapsed_seconds": execution.get("elapsed_seconds"),
        "tokens": execution.get("usage"),
        "dollars": execution.get("native_dollars"),
        "token_format": execution.get(
            "token_format", "native Codex; cached input and reasoning output are subsets"
        ),
    }


def _consistent_observation(executions: list[dict[str, Any]], key: str) -> str | None:
    observations = [execution.get(key, []) for execution in executions]
    if not all(isinstance(value, list) and len(value) == 1 for value in observations):
        return None
    values = {value[0] for value in observations}
    return next(iter(values)) if len(values) == 1 else None


def _observed_harness_version(executions: list[dict[str, Any]]) -> str | None:
    versions = [
        codex.observed_cli_version(
            execution.get("startup"),
            host="claude"
            if execution.get("launch", {}).get("harness") == "claude-cli"
            else "codex",
        )
        or execution.get("launch_harness_version")
        for execution in executions
    ]
    return versions[0] if versions and versions[0] and len(set(versions)) == 1 else None


def _execute_stage(
    *,
    root: Path,
    area: Path,
    entry: dict[str, Any],
    task: TaskSpec,
    study: ResolvedStudy,
    stage_id: str,
    prompt: str,
    on_native_start: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _assert_frozen(root)
    interrupted: KeyboardInterrupt | None = None
    try:
        codex.reject_guidance(area / "workspace", include_ancestors=True, reject_git=False)
        if on_native_start:
            on_native_start()
        execution = codex.invoke(
            area=area,
            entry={**entry, "host": study.recipe.harness},
            protocol={
                "models": {study.recipe.harness: study.recipe.model},
                "reasoning": study.recipe.reasoning,
                **(
                    {"auth_file": str(study.recipe.auth_file)}
                    if study.recipe.harness == "codex"
                    else {}
                ),
            },
            prompt=prompt,
            timeout=study.recipe.timeout_seconds,
        )
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        artifacts = area / "artifacts"
        stdout = artifacts / "stdout.jsonl"
        traces = [path.read_text() for path in sorted(artifacts.glob("host-trace-*.jsonl"))]
        execution = codex.parse_events(
            stdout.read_text() if stdout.is_file() else "",
            traces,
            study.recipe.harness,
            requested_effort=study.recipe.reasoning,
        )
        runtime_receipt = artifacts / "execution.json"
        if runtime_receipt.is_file():
            execution.update(json.loads(runtime_receipt.read_text()))
        execution.update(successful_native_completion=False, error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, KeyboardInterrupt):
            interrupted = exc
    target = root / "attempts" / entry["id"] / stage_id
    target.mkdir(parents=True)
    if (area / "artifacts").exists():
        shutil.copytree(area / "artifacts", target / "artifacts")
        shutil.rmtree(area / "artifacts")
    _write(target / "execution.json", execution)
    if interrupted is not None:
        shutil.copytree(
            area / "workspace",
            target / "interrupted-source",
            symlinks=True,
            ignore=shutil.ignore_patterns(*EXCLUDED_NAMES),
        )
        _write(
            target / "interrupted-source.json",
            {"status": "unsubmitted interrupted workspace; not graded"},
        )
        raise interrupted
    try:
        inspect_snapshot(area / "workspace")
    except ValueError as exc:
        raw = target / "unsupported-submission"
        shutil.copytree(area / "workspace", raw, symlinks=True)
        links = {}
        for directory, names, files in os.walk(raw, followlinks=False):
            for name in [*names, *files]:
                path = Path(directory) / name
                if path.is_symlink():
                    links[path.relative_to(raw).as_posix()] = os.readlink(path)
        _write(target / "unsupported-submission.json", {"error": str(exc), "symlinks": links})
        return (
            {
                "id": stage_id,
                "snapshot": None,
                "correctness": None,
                "evidence": None,
                "work": _work(execution),
            },
            execution,
            {"pass": None, "error": "unsupported submitted snapshot"},
        )
    snapshot = target / "snapshot"
    shutil.copytree(
        area / "workspace", snapshot, symlinks=True, ignore=shutil.ignore_patterns(*EXCLUDED_NAMES)
    )
    snapshot_ref = _reference(root, snapshot, snapshot=True)
    with tempfile.TemporaryDirectory(prefix="pluginbench-grade-") as grader:
        result = _grade(snapshot, task, Path(grader))
    _write(target / "verifier-result.json", result)
    checks: dict[str, bool | None] = {"behavior": result["pass"]}
    report = result.get("report")
    if isinstance(report, dict):
        checks.update(
            {
                key.removesuffix("_pass"): value
                for key, value in report.items()
                if key.endswith("_pass") and isinstance(value, bool)
            }
        )
    checks["verifier"] = result["pass"]
    if "grader_immutable" in result:
        checks["grader_immutable"] = result["grader_immutable"]
    checks["native_completion"] = execution.get("successful_native_completion") is True
    receipt = CorrectnessReceipt(
        schema_version=1,
        snapshot_digest=snapshot_ref["sha256"],
        checks=checks,
        command=list(task.verifier.command) if task.verifier else ["unavailable"],
        tool="external task verifier",
    )
    _write(target / "correctness.json", receipt.model_dump(mode="json"))
    evidence = _collect(
        root, snapshot, target / "evidence", study.recipe.collectors, before=task.fixture
    )
    if _reference(root, snapshot, snapshot=True) != snapshot_ref:
        raise ValueError("submitted snapshot changed during verification or collection")
    _assert_frozen(root)
    return (
        {
            "id": stage_id,
            "snapshot": snapshot_ref,
            "correctness": _reference(root, target / "correctness.json"),
            "evidence": evidence or None,
            "work": _work(execution),
        },
        execution,
        result,
    )


def _versioned_artifact(path: Path) -> Path:
    version = path.with_name(path.stem + "." + _digest(path).removeprefix("sha256:") + path.suffix)
    if not version.exists():
        shutil.copyfile(path, version)
    return version


def _checkpoint(
    root: Path,
    study: ResolvedStudy,
    manifest: dict[str, Any],
    states: dict[str, str],
    calls: int = 0,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Keep assigned attempts visible, including pending and interrupted work."""
    existing = json.loads((root / "run.json").read_text()) if (root / "run.json").is_file() else {}
    for record in manifest["records"]:
        for stage in record["stages"]:
            execution = root / "attempts" / record["id"] / stage["id"] / "execution.json"
            if stage["snapshot"] is None and execution.is_file():
                stage["work"] = _work(json.loads(execution.read_text()))
    AssessmentInput.model_validate(manifest)
    _write(root / "assessment-input.json", manifest)
    checkpoint_input = _versioned_artifact(root / "assessment-input.json")
    checkpoint_preflight = _versioned_artifact(root / "runtime-preflight.json")
    executions: dict[str, dict[str, dict[str, str]]] = {}
    verifier_results: dict[str, dict[str, dict[str, str]]] = {}
    submission_errors: dict[str, dict[str, dict[str, str]]] = {}
    for record in manifest["records"]:
        executions[record["id"]] = {}
        verifier_results[record["id"]] = {}
        submission_errors[record["id"]] = {}
        for stage in record["stages"]:
            target = root / "attempts" / record["id"] / stage["id"]
            progress = root / "progress" / record["id"] / stage["id"]
            execution = target / "execution.json"
            if not execution.is_file():
                execution = progress / f"execution.{states[record['id']]}.json"
                _write(execution, {"attempt_status": states[record["id"]], "usage": None})
            executions[record["id"]][stage["id"]] = _reference(root, execution)
            verifier_result = target / "verifier-result.json"
            if verifier_result.is_file():
                verifier_results[record["id"]][stage["id"]] = _reference(root, verifier_result)
            if stage["snapshot"] is None:
                failure = target / "unsupported-submission.json"
                if not failure.is_file():
                    failure = progress / f"submission.{states[record['id']]}.json"
                    _write(failure, {"status": states[record["id"]], "error": error})
                submission_errors[record["id"]][stage["id"]] = _reference(root, failure)
    invocation_starts = {
        path.name: {marker.stem: _reference(root, marker) for marker in sorted(path.glob("*.json"))}
        for path in sorted((root / "invocations").glob("*"))
        if path.is_dir()
    }
    if (root / "invocations").is_dir():
        calls = sum(len(starts) for starts in invocation_starts.values())
    _write(
        root / "run.json",
        {
            **{
                key: existing[key]
                for key in ("review", "observer_revision", "evidence_kind")
                if key in existing
            },
            "schema_version": 1,
            "status": status,
            "error": error,
            "attempt_states": states,
            "completed_attempts": sum(state == "complete" for state in states.values()),
            "initial_calls": len(states),
            "actual_calls": calls,
            "invocation_starts": invocation_starts,
            "jobs": study.recipe.jobs,
            "call_unit": "native invocation requests; startup may fail before a model call",
            "assessment_input": _reference(root, checkpoint_input),
            "plan": _reference(root, root / "plan.json"),
            "runtime_preflight": _reference(root, checkpoint_preflight),
            "task_provenance": {
                task.task_id: task.provenance for task in study.tasks if task.provenance is not None
            },
            "calibration": _reference(root, root / "calibration.json")
            if (root / "calibration.json").is_file()
            else None,
            "executions": executions,
            "verifier_results": verifier_results,
            "submission_errors": submission_errors,
            "correction_burden": "unknown: deterministic verifier repair stages are not independent review",
        },
    )


def run_study(
    study: ResolvedStudy, output: Path, *, progress: Callable[[str], None] | None = None
) -> Path:
    validate_jobs(study.recipe.jobs)
    root = output.resolve()
    if root.exists():
        raise ValueError(f"study output must be a new directory: {root}")
    root.mkdir(parents=True)
    frozen = _freeze(study, root)
    plan = plan_study(frozen)
    _write(root / "plan.json", plan)
    preflight: dict[str, Any] = {"status": "not started"}
    _write(root / "runtime-preflight.json", preflight)
    records: list[dict[str, Any]] = [
        {
            **{key: entry[key] for key in ("id", "task_id", "attempt", "arm")},
            "model": None,
            "stages": [{"id": "handoff", "snapshot": None, "work": None}],
        }
        for entry in plan["entries"]
    ]
    states = {entry["id"]: "pending" for entry in plan["entries"]}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "name": study.recipe.name,
        "records": records,
        "standards": str(frozen.recipe.standards.relative_to(root))
        if frozen.recipe.standards
        else None,
    }
    _checkpoint(root, frozen, manifest, states, status="preflight")
    tasks = {task.task_id: task for task in frozen.tasks}
    try:
        preflight = (
            pier_backend.preflight(frozen.recipe.pier_python)
            if frozen.recipe.backend == "pier"
            else claude.preflight()
            if frozen.recipe.harness == "claude"
            else codex.preflight()
        )
        _write(root / "runtime-preflight.json", preflight)
        _checkpoint(
            root,
            frozen,
            manifest,
            states,
            status="calibration" if frozen.recipe.backend == "pier" else "running",
        )
        if frozen.recipe.backend == "pier":
            pier_tasks = tuple(
                task for task in frozen.tasks if isinstance(task, pier_backend.PierTask)
            )
            calibration = pier_backend.calibrate(
                pier_tasks, root / "calibration", frozen.recipe.pier_python
            )
            _assert_frozen(root)
            _write(root / "calibration.json", calibration)

        def commit(index: int, record: dict[str, Any], status: str) -> None:
            records[index] = record
            states[record["id"]] = status
            _checkpoint(root, frozen, manifest, states, status="running")

        def submitted(index: int) -> None:
            entry = plan["entries"][index]
            if progress:
                progress(
                    f"Coding {index + 1}/{len(plan['entries'])}: {entry['task_id']} · {entry['arm']} · repeat {entry['attempt']}"
                )
            states[entry["id"]] = "running"
            _checkpoint(root, frozen, manifest, states, status="running")

        def attempt(
            index: int, cancelled: Event, emit: Callable[[Callable[[], None]], None]
        ) -> None:
            entry = plan["entries"][index]
            task = tasks[entry["task_id"]]
            record = copy.deepcopy(records[index])
            stage_id = "handoff"
            completed = False

            def started() -> None:
                if cancelled.is_set():
                    raise CancelledError("attempt cancelled before its next native invocation")
                receipt = root / "invocations" / entry["id"] / f"{stage_id}.json"
                _write(
                    receipt,
                    {
                        "attempt_id": entry["id"],
                        "stage": stage_id,
                        "status": "native invocation started",
                    },
                )
                emit(partial(commit, index, copy.deepcopy(record), "running"))

            try:
                if isinstance(task, pier_backend.PierTask):
                    record = _run_pier_attempt(
                        root=root, entry=entry, task=task, study=frozen, on_native_start=started
                    )
                else:
                    assert task.fixture is not None
                    attempt_dir = root / "attempts" / entry["id"]
                    before = _reference(root, task.fixture, snapshot=True)
                    before_evidence = _collect(
                        root,
                        task.fixture,
                        attempt_dir / "before-evidence",
                        frozen.recipe.collectors,
                        before=task.fixture,
                    )
                    record.update(before=before, before_evidence=before_evidence or None)
                    stages: list[dict[str, Any]] = []
                    executions: list[dict[str, Any]] = []
                    with tempfile.TemporaryDirectory(prefix="pluginbench-study-") as temporary:
                        previous_snapshot = task.fixture
                        guidance = frozen.guidance.get(entry["arm"], ())
                        prompt = _prompt(task, guidance)
                        for round_number in range(frozen.recipe.max_repairs + 1):
                            if cancelled.is_set():
                                break
                            stage_id = "handoff" if round_number == 0 else f"repair-{round_number}"
                            area = Path(temporary) / stage_id
                            shutil.copytree(previous_snapshot, area / "workspace")
                            stages.append({"id": stage_id, "snapshot": None, "work": None})
                            record["stages"] = list(stages)
                            record["model"] = None
                            _assert_frozen(root)
                            codex.reject_guidance(
                                area / "workspace", include_ancestors=True, reject_git=False
                            )
                            emit(partial(commit, index, copy.deepcopy(record), "running"))
                            stage, execution, result = _execute_stage(
                                root=root,
                                area=area,
                                entry=entry,
                                task=task,
                                study=frozen,
                                stage_id=stage_id,
                                prompt=prompt,
                                on_native_start=started,
                            )
                            stages[-1] = stage
                            executions.append(execution)
                            record.update(
                                stages=list(stages),
                                model=_consistent_observation(executions, "actual_primary_models"),
                                runtime={
                                    "harness": f"{study.recipe.harness}-cli",
                                    "harness_version": _observed_harness_version(executions),
                                    "requested_reasoning_effort": study.recipe.reasoning,
                                    "reasoning_effort": _consistent_observation(
                                        executions, "runtime_effort_evidence"
                                    ),
                                },
                            )
                            emit(partial(commit, index, copy.deepcopy(record), "running"))
                            if stage["snapshot"] is not None:
                                previous_snapshot = root / stage["snapshot"]["path"]
                            if (
                                result["pass"] is not False
                                or execution.get("successful_native_completion") is not True
                            ):
                                break
                            prompt = _prompt(task, guidance) + (
                                "\n\nThe external verifier rejected your previous submission. "
                                "Fix its failures in the current workspace and verify the result.\n"
                                + json.dumps(result, sort_keys=True)
                            )
                completed = True
            finally:
                emit(
                    partial(
                        commit,
                        index,
                        copy.deepcopy(record),
                        "complete" if completed and not cancelled.is_set() else "interrupted",
                    )
                )

        run_jobs(
            list(range(len(plan["entries"]))),
            jobs=frozen.recipe.jobs,
            operation=attempt,
            on_submit=submitted,
        )
    except BaseException as exc:
        for attempt_id, state in states.items():
            if state == "running":
                states[attempt_id] = "interrupted"
        for record in records:
            observed = []
            for stage in record["stages"]:
                execution_path = root / "attempts" / record["id"] / stage["id"] / "execution.json"
                observed.append(
                    json.loads(execution_path.read_text()) if execution_path.is_file() else {}
                )
            record["model"] = _consistent_observation(observed, "actual_primary_models")
        _checkpoint(
            root,
            frozen,
            manifest,
            states,
            status="interrupted",
            error=f"{type(exc).__name__}: {exc}",
        )
        report_study(root)
        raise
    _checkpoint(root, frozen, manifest, states, status="complete")
    report_study(root)
    return root


def _run_pier_attempt(
    *,
    root: Path,
    entry: dict[str, Any],
    task: pier_backend.PierTask,
    study: ResolvedStudy,
    on_native_start: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _assert_frozen(root)
    target = root / "attempts" / entry["id"] / "handoff"
    if on_native_start:
        on_native_start()
    result = pier_backend.invoke(
        target=target,
        task=task,
        prompt=_prompt(task, study.guidance.get(entry["arm"], ())),
        model=study.recipe.model,
        reasoning=study.recipe.reasoning,
        auth_file=study.recipe.auth_file,
        codex_version=study.recipe.codex_version,
        python=study.recipe.pier_python,
        timeout=study.recipe.timeout_seconds,
    )
    execution = result["execution"]
    _write(target / "execution.json", execution)
    _write(target / "verifier-result.json", result)
    before = Path(result["before"]) if result.get("before") else None
    handoff = Path(result["handoff"]) if result.get("handoff") else None
    correctness = None
    if handoff is not None:
        snapshot = _reference(root, handoff, snapshot=True)
        receipt = CorrectnessReceipt(
            schema_version=1,
            snapshot_digest=snapshot["sha256"],
            checks={
                **result["checks"],
                "native_completion": execution.get("successful_native_completion") is True,
            },
            command=["pier", "trial", str(task.directory)],
            tool="DeepSWE separate verifier",
            tool_version=pier_backend.PIER_VERSION,
        )
        _write(target / "correctness.json", receipt.model_dump(mode="json"))
        correctness = _reference(root, target / "correctness.json")
    else:
        _write(
            target / "unsupported-submission.json",
            {
                "error": "Pier did not export a bindable graded submission",
                "checks": result.get("checks"),
                "execution_error": execution.get("error"),
            },
        )
    before_ref = _reference(root, before, snapshot=True) if before else None
    before_evidence = (
        _collect(
            root, before, target.parent / "before-evidence", study.recipe.collectors, before=before
        )
        if before
        else None
    )
    evidence = (
        _collect(root, handoff, target / "evidence", study.recipe.collectors, before=before)
        if handoff
        else None
    )
    _assert_frozen(root)
    return {
        "id": entry["id"],
        "task_id": task.task_id,
        "attempt": entry["attempt"],
        "arm": entry["arm"],
        "model": _consistent_observation([execution], "actual_primary_models"),
        "runtime": {
            "harness": "codex-cli",
            "harness_version": _observed_harness_version([execution]),
            "requested_reasoning_effort": study.recipe.reasoning,
            "reasoning_effort": _consistent_observation([execution], "runtime_effort_evidence"),
        },
        "before": before_ref,
        "before_evidence": before_evidence or None,
        "stages": [
            {
                "id": "handoff",
                "snapshot": _reference(root, handoff, snapshot=True) if handoff else None,
                "correctness": correctness,
                "evidence": evidence or None,
                "work": _work(execution),
            }
        ],
    }


def _bound_path(root: Path, reference: object) -> Path:
    artifact = ArtifactRef.model_validate(reference)
    path = root / artifact.path
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("run artifact must remain inside its run directory")
    if _digest(path) != artifact.sha256:
        raise ValueError(f"run artifact changed after the run: {artifact.path}")
    return path


def _bound_document(root: Path, reference: object) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(_bound_path(root, reference).read_text())
    return document


def _observer_tasks(
    root: Path, tasks: dict[str, TaskSpec | pier_backend.PierTask]
) -> dict[str, TaskSpec | pier_backend.PierTask]:
    run = json.loads((root / "run.json").read_text())
    if not run.get("observer_revision"):
        return tasks
    revision = _bound_document(root, run["observer_revision"])
    result = dict(tasks)
    for task_id, reference in revision.get("task_verifiers", {}).items():
        task = tasks[task_id]
        if not isinstance(task, TaskSpec) or task.verifier is None:
            raise ValueError("revised observer requires a local task verifier")
        path = _bound_path(root, reference)
        result[task_id] = replace(
            task,
            verifier=replace(
                task.verifier,
                script=path,
                command=tuple(
                    str(path) if argument == str(task.verifier.script) else argument
                    for argument in task.verifier.command
                ),
            ),
        )
    return result


def report_study(
    root: Path,
    *,
    measurement_revision: str | None = None,
    review_revision: str | None = None,
) -> tuple[Path, Path]:
    root = root.resolve()
    _assert_frozen(root)
    run = json.loads((root / "run.json").read_text())
    _bound_document(root, run["assessment_input"])
    plan = _bound_document(root, run["plan"])
    preflight = _bound_document(root, run["runtime_preflight"])
    assigned_harness_version = (
        plan["codex_version"] if plan.get("backend") == "pier" else preflight.get("harness_version")
    )
    if isinstance(assigned_harness_version, str):
        assigned_harness_version = assigned_harness_version.removeprefix("codex-cli ")
    if run.get("calibration"):
        _bound_document(root, run["calibration"])
    manifest_path = root / run["assessment_input"]["path"]
    output = root / "report"
    measurement_state = None
    if measurement_revision is not None:
        from pluginbench.remeasure import validate_remeasurement

        directory = root / "observer-revisions" / measurement_revision
        manifest_path = validate_remeasurement(root, directory)
        output = directory / "report-native"
        measurement_state = json.loads((directory / "revision.json").read_text())
    review_path = _bound_path(root, run["review"]) if run.get("review") else None
    if review_revision is not None:
        from pluginbench.study_review import validate_review_revision

        review_path = validate_review_revision(root, review_revision)
        output = review_path.parent / "report-native"
        output = (
            output / "measurements" / measurement_revision
            if measurement_revision is not None
            else output / "original"
        )
        if output.resolve() != output:
            raise ValueError("review projection output cannot follow symlinks")
    report = build_assessment(manifest_path)
    if measurement_state is not None:
        report["measurement_revision"] = measurement_state
    if run.get("observer_revision"):
        revision = _bound_document(root, run["observer_revision"])
        _bound_document(root, revision["original_run"])
        _bound_document(root, revision["original_assessment"])
        for group in ("task_verifiers", "measurement_configs", "original_reports"):
            for reference in revision.get(group, {}).values():
                _bound_path(root, reference)
        manifest = _bound_document(root, run["assessment_input"])
        records = {record["id"]: record for record in manifest["records"]}
        attempts = {attempt["id"]: attempt for attempt in report["attempts"]}
        for observation in revision["observations"]:
            attempt = attempts[observation["attempt_id"]]
            stage = next(row for row in attempt["stages"] if row["id"] == observation["stage"])
            if (
                observation["snapshot_digest"] != stage["snapshot_digest"]
                or observation["before_digest"] != attempt["before_digest"]
            ):
                raise ValueError("revised observer source differs from its original attempt")
            input_stage = next(
                row for row in records[attempt["id"]]["stages"] if row["id"] == stage["id"]
            )
            if observation.get("new_correctness_ref") != input_stage.get("correctness"):
                raise ValueError("revised observer correctness differs from current assessment")
            for key in ("old_correctness_ref", "new_correctness_ref", "result_ref"):
                if observation.get(key):
                    _bound_document(root, observation[key])
            for key in ("before_evidence", "after_evidence"):
                for reference in (observation.get(key) or {}).values():
                    _bound_document(root, reference)
            if observation.get("result_ref"):
                stage["task_result"] = _bound_document(root, observation["result_ref"])
        report["observer_revision"] = revision
    report["study_progress"] = {
        "status": run.get("status", "complete"),
        "completed_attempts": run["completed_attempts"],
        "assigned_attempts": len(plan["entries"]),
        "native_invocations": run["actual_calls"],
        "error": run.get("error"),
    }
    if review_path is not None:
        review = json.loads(review_path.read_text())
        recovery = None
        if review.get("recovery"):
            recovery = _bound_document(root, review["recovery"])
            original_review = _bound_document(root, recovery["original_state"])
            if original_review["protocol"] != review["protocol"]:
                original_protocol = _bound_document(root, original_review["protocol"])
                recovered_protocol = _bound_document(root, review["protocol"])
                if review_revision is None or recovered_protocol != {
                    **original_protocol,
                    "revision": review_revision,
                }:
                    raise ValueError("recovered review protocol differs from its original review")
            if recovery.get("original_protocol"):
                if recovery["original_protocol"] != original_review["protocol"]:
                    raise ValueError("review recovery refers to a different original protocol")
                _bound_document(root, recovery["original_protocol"])
            for reference in [
                recovery["parser"],
                recovery["adapter"],
                *recovery["native_artifacts"],
            ]:
                _bound_path(root, reference)
        protocol = _bound_document(root, review["protocol"])
        _bound_document(root, review["original_run"])
        adapter = ArtifactRef.model_validate(review["adapter"])
        if _digest(root / adapter.path) != adapter.sha256:
            raise ValueError("review adapter changed after the review")
        observations = []
        review_attempts = {attempt["id"]: attempt for attempt in report["attempts"]}
        for row in review["records"]:
            observation = (
                _bound_document(root, row["observation"]) if row.get("observation") else None
            )
            if observation and observation.get("recovery"):
                _bound_document(root, observation["recovery"]["original_observation"])
            if row.get("packet"):
                packet = _bound_document(root, row["packet"])
                attempt = review_attempts[row["id"]]
                selection = protocol.get("selections", {}).get(row["id"], {})
                stage_id = packet.get("stage_id", "handoff")
                selected = next(
                    (stage for stage in attempt["stages"] if stage["id"] == stage_id), None
                )
                submission_digest = packet.get("submission_digest", packet.get("handoff_digest"))
                if (
                    packet["before_digest"] != attempt["before_digest"]
                    or selected is None
                    or submission_digest != selected["snapshot_digest"]
                    or (
                        selection
                        and (
                            selection["stage_id"] != stage_id
                            or stage_id != attempt.get(protocol.get("stage", "handoff"))
                            or selection["submission_digest"] != submission_digest
                            or row.get("stage_id") != stage_id
                            or row.get("submission_digest") != submission_digest
                            or (
                                observation
                                and (
                                    observation.get("stage_id") != stage_id
                                    or observation.get("submission_digest") != submission_digest
                                )
                            )
                        )
                    )
                ):
                    raise ValueError("review packet source differs from its attempt")
                packet_directory = _bound_path(root, row["packet"]).with_suffix("")
                if _inventory(packet_directory) != packet["files"]:
                    raise ValueError("archived review packet changed after review")
                if observation and observation["packet"] != row["packet"]:
                    raise ValueError("review observation differs from its bound packet")
            issues = None
            severity_counts = None
            category_counts = None
            if row["status"] == "reviewed" and observation and observation.get("response"):
                from pluginbench.study_review import JudgeResponse, RatedJudgeResponse

                response = (
                    RatedJudgeResponse.model_validate(observation["response"])
                    if review_revision is not None
                    else JudgeResponse.model_validate(observation["response"])
                )
                if (
                    response.status != "reviewed"
                    or response.outside_packet_access
                    or response.packet_id != row["packet_id"]
                ):
                    raise ValueError("reviewed record differs from its validated response")
                issues = [issue.model_dump(mode="json") for issue in response.issues]
                category_counts = {
                    category: sum(issue["category"] == category for issue in issues)
                    for category in ("correctness", "scope", "maintainability", "tests")
                }
                if review_revision is not None:
                    severity_counts = {
                        severity: sum(issue["severity"] == severity for issue in issues)
                        for severity in ("P0", "P1", "P2", "P3")
                    }
            observations.append(
                {
                    "attempt_id": row["id"],
                    "stage": protocol.get("stage", "handoff"),
                    **protocol.get("selections", {}).get(row["id"], {}),
                    "status": row["status"],
                    "finding_count": len(issues) if issues is not None else None,
                    "issues": issues,
                    "severity_counts": severity_counts,
                    "category_counts": category_counts,
                    "criteria_sha256": protocol.get("criteria_sha256"),
                    "reason": row.get("reason"),
                    "recovered": bool(
                        row["status"] == "reviewed" and observation and observation.get("recovery")
                    ),
                    "work": _work(observation["execution"]) if observation else None,
                    "execution": observation.get("execution") if observation else None,
                    "limitations": observation.get("response", {}).get("limitations", [])
                    if observation and observation.get("response")
                    else [],
                }
            )
        report["independent_review"] = {
            "status": review["status"],
            "stage": protocol.get("stage", "handoff"),
            "assigned": review["assigned"],
            "native_invocation_requests": review["native_invocations"],
            "requested_model": protocol["model"],
            "requested_harness": protocol.get("harness", "codex"),
            "requested_reasoning_effort": protocol["effort"],
            "criteria_sha256": protocol.get("criteria_sha256"),
            "revision": review_revision,
            "records": observations,
            "recovery": {
                "method": recovery["method"],
                "counts": recovery.get("counts"),
                "native_invocations": recovery["native_invocations"],
            }
            if recovery
            else None,
            "limitations": review["limitations"],
            "cost_accounting": "Judge work is separate from implementation work; unavailable usage stays unknown.",
        }
        by_attempt = {row["attempt_id"]: row for row in observations}
        for attempt in report["attempts"]:
            attempt["independent_review"] = {
                **by_attempt[attempt["id"]],
                "requested_model": protocol["model"],
                "requested_harness": protocol.get("harness", "codex"),
                "requested_reasoning_effort": protocol["effort"],
            }
    entries = {entry["id"]: entry for entry in plan["entries"]}
    configured = json.loads((root / "frozen/resolved-recipe.json").read_text())["collectors"]
    revised_profiles = (
        _bound_document(root, measurement_state["frozen_profiles"])
        if measurement_state is not None
        else {}
    )
    for attempt in report["attempts"]:
        entry = entries[attempt["id"]]
        if any(attempt[key] != entry[key] for key in ("task_id", "arm", "attempt")):
            raise ValueError("assessment attempt differs from its frozen plan")
        attempt["configured_metrics"] = sorted(
            set(attempt["configured_metrics"])
            | {collector["kind"] for collector in configured}
            | {collector["kind"] for collector in revised_profiles.get(attempt["task_id"], [])}
        )
        attempt["provenance"]["attempt_status"] = run.get("attempt_states", {}).get(
            attempt["id"], "complete"
        )
        executions = []
        for stage in attempt["stages"]:
            execution = _bound_document(root, run["executions"][attempt["id"]][stage["id"]])
            executions.append(execution)
            verifier_result = (
                run.get("verifier_results", {}).get(attempt["id"], {}).get(stage["id"])
            )
            if verifier_result:
                original_result = _bound_document(root, verifier_result)
                if "task_result" not in stage:
                    stage["task_result"] = original_result
            stage["execution_provenance"] = {
                "artifact": run["executions"][attempt["id"]][stage["id"]],
                "observed_harness_version": codex.observed_cli_version(
                    execution.get("startup"), host=plan.get("host", "codex")
                ),
                **{
                    key: execution.get(key)
                    for key in (
                        "actual_primary_models",
                        "runtime_effort_evidence",
                        "successful_native_completion",
                        "timed_out",
                        "exit_code",
                        "partial_usage",
                        "error",
                        "attempt_status",
                    )
                },
            }
            if stage["snapshot_digest"] is None:
                stage["submission_error"] = _bound_document(
                    root, run["submission_errors"][attempt["id"]][stage["id"]]
                )
        if attempt["model"] != _consistent_observation(executions, "actual_primary_models"):
            raise ValueError("assessment model differs from its bound execution evidence")
        attempt["provenance"].update(
            {
                "input": f"{plan.get('host', 'codex')}-study",
                "host": plan.get("host", "codex"),
                "harness": f"{plan.get('host', 'codex')}-cli",
                "harness_version": _observed_harness_version(executions),
                "reasoning_effort": _consistent_observation(executions, "runtime_effort_evidence"),
                "runtime_source": "native execution receipts",
                "guidance_delivery": plan["delivery"],
                "requested_model": plan["model"],
                "assigned_model": plan["model"],
                "assigned_harness_version": assigned_harness_version,
                "assigned_reasoning_effort": plan["requested_reasoning_effort"],
                "study_seed": plan["seed"],
                "execution_backend": plan.get("backend", "local"),
                "task": run.get("task_provenance", {}).get(attempt["task_id"]),
            }
        )
        if (
            plan.get("host") == "claude"
            and all(
                codex.validated_claude_launch(
                    execution, plan["model"], plan["requested_reasoning_effort"]
                )
                for execution in executions
            )
            and executions
        ):
            attempt["provenance"].update(
                comparison_reasoning_effort=plan["requested_reasoning_effort"],
                launch_harness_version=_observed_harness_version(executions),
                effort_evidence_source="native session event"
                if _consistent_observation(executions, "runtime_effort_evidence")
                else "requested launch setting; effective effort unverified",
            )
    return write_assessment(report, output)
