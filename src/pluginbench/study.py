"""One native Codex study: frozen text conditions, independent attempts, one report."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import partial
from threading import Event
from concurrent.futures import CancelledError
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pluginbench import codex, isolation, harbor_backend
from pluginbench.baselines import BaselineCache
from pluginbench.parallel import run_jobs, validate_jobs
from pluginbench.assessment import build_assessment, write_assessment
from pluginbench.assessment_metrics import EXCLUDED_NAMES, inspect_snapshot
from pluginbench.assessment_models import (
    ArtifactRef,
    AssessmentInput,
    CorrectnessReceipt,
)
from pluginbench.collectors import (
    Collector,
    collect_measurements,
    freeze_collectors,
    resolve_collector_command,
)
from pluginbench.datasets import TaskCatalog, TaskSpec, load_tasks


class StudyRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    backend: Literal["harbor"] = "harbor"
    harness: Literal["codex", "claude"] = "codex"
    tasks: Path
    harness_version: str | None = None
    container_cpus: int = Field(default=1, strict=True, ge=1)
    container_memory_mb: int = Field(default=2048, strict=True, ge=256)
    guidance: dict[str, list[Path]] = Field(min_length=1)
    model: str = Field(default="gpt-6.1-sol", min_length=1)
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "medium"
    repeats: int = Field(default=3, strict=True, ge=1)
    jobs: int = Field(default=1, strict=True, ge=1, le=16)
    timeout_seconds: int = Field(default=600, strict=True, ge=1)
    seed: int = Field(default=42, strict=True)
    auth_file: Path = Path("~/.codex/auth.json")
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
    def supported_effort(self) -> StudyRecipe:
        if self.reasoning == "ultra":
            raise ValueError("Harbor does not support ultra effort")
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
    tasks: tuple[TaskSpec, ...]
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
    tasks = _load_tasks(recipe)
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


def _load_tasks(recipe: StudyRecipe) -> tuple[TaskSpec, ...]:
    tasks = load_tasks(recipe.tasks)
    for task in tasks:
        assert task.fixture is not None
        codex.reject_guidance(task.fixture, include_ancestors=False, reject_git=True)
        image = (task.provenance or {}).get("agent_image", "")
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("Harbor tasks require a pinned agent_image in provenance")
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
        "model": recipe.model,
        "requested_reasoning_effort": recipe.reasoning,
        "delivery": "forced text in task prompt; automatic activation is not measured",
        "guidance_selection": study.guidance_selection,
        "baseline": "no supplied custom guidance; native built-in instructions remain",
        "seed": recipe.seed,
        "jobs": recipe.jobs,
        "initial_calls": len(entries),
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
    _freeze_tasks(study, frozen)
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
    public_recipe = recipe.model_dump(mode="json", exclude={"auth_file"})
    _write(frozen / "resolved-recipe.json", public_recipe)
    _write(root / "frozen-inventory.json", _inventory(frozen))
    tasks = _load_tasks(recipe)
    return ResolvedStudy(
        recipe,
        tasks,
        {key: tuple(value) for key, value in guidance.items()},
        study.source,
        study.guidance_selection,
    )


def _freeze_tasks(study: ResolvedStudy, frozen: Path) -> None:
    entries = []
    catalog = TaskCatalog.model_validate(yaml.safe_load(study.recipe.tasks.read_text()))
    declared = {entry.id: entry for entry in catalog.tasks}
    for task in study.tasks:
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


def _prompt(task: TaskSpec, guidance: tuple[Path, ...]) -> str:
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
    receipt = directory / "runtime-isolation.json"
    try:
        result = isolation.run(
            list(task.verifier.command),
            cwd=workspace,
            timeout=task.verifier.timeout_seconds,
            env={"PATH": os.environ["PATH"], "PLUGINBENCH_TASK_ID": task.task_id},
            readable=isolation.verifier_paths(task.verifier.script),
            receipt=receipt,
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
            "filesystem": json.loads(receipt.read_text()),
        }
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {
            "pass": None,
            "error": str(exc),
            "filesystem": json.loads(receipt.read_text()) if receipt.is_file() else None,
        }


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
    on_native_start: Callable[[], None],
    expected_runtime: dict[str, object],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _assert_frozen(root)
    interrupted: KeyboardInterrupt | None = None
    try:
        codex.reject_guidance(area / "workspace", include_ancestors=True, reject_git=False)
        on_native_start()
        execution = harbor_backend.invoke(
            area=area,
            entry={**entry, "host": study.recipe.harness},
            protocol={
                "models": {study.recipe.harness: study.recipe.model},
                "reasoning": study.recipe.reasoning,
                "isolation": expected_runtime,
                "agent_image": (task.provenance or {})["agent_image"],
                "image_identity": json.loads((task.provenance or {})["image_identity"])
                if (task.provenance or {}).get("image_identity")
                else None,
                "toolchain_identity": json.loads((task.provenance or {})["toolchain_identity"])
                if (task.provenance or {}).get("toolchain_identity")
                else None,
                "harness_version": study.recipe.harness_version,
                "container_cpus": study.recipe.container_cpus,
                "container_memory_mb": study.recipe.container_memory_mb,
                "build_timeout_seconds": int(
                    (task.provenance or {}).get("build_timeout_seconds", "300")
                ),
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
        execution = codex.parse_events(
            "",
            [],
            study.recipe.harness,
            requested_effort=study.recipe.reasoning,
        )
        try:
            codex.validate_artifacts(artifacts)
        except (OSError, ValueError):
            pass
        else:
            stdout = artifacts / "stdout.jsonl"
            execution.update(
                codex.parse_events(
                    stdout.read_text() if stdout.is_file() else "",
                    [path.read_text() for path in sorted(artifacts.glob("host-trace-*.jsonl"))],
                    study.recipe.harness,
                    requested_effort=study.recipe.reasoning,
                )
            )
            runtime_receipt = artifacts / "execution.json"
            if runtime_receipt.is_file():
                execution.update(json.loads(runtime_receipt.read_text()))
        execution.update(successful_native_completion=False, error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, KeyboardInterrupt):
            interrupted = exc
    target = root / "attempts" / entry["id"] / stage_id
    target.mkdir(parents=True)
    artifacts = area / "artifacts"
    artifact_error = None
    if artifacts.exists() or artifacts.is_symlink():
        artifact_error = codex.copy_artifacts(artifacts, target / "artifacts")
        if artifacts.is_symlink():
            artifacts.unlink()
        else:
            shutil.rmtree(artifacts)
    if artifact_error:
        execution = {
            **codex.parse_events("", [], study.recipe.harness),
            "successful_native_completion": False,
            "submission_collected": False,
            "error": artifact_error,
        }
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
    if artifact_error or execution.get("submission_collected") is not True:
        exported_digest = execution.get("submission_export_digest")
        if not artifact_error and isinstance(exported_digest, str):
            try:
                exported = inspect_snapshot(area / "workspace")
                if exported["digest"] != exported_digest:
                    raise ValueError("stopped export changed before failure evidence was frozen")
                shutil.copytree(
                    area / "workspace",
                    target / "ungraded-submission",
                    symlinks=True,
                    ignore=shutil.ignore_patterns(*EXCLUDED_NAMES),
                )
                _write(
                    target / "ungraded-submission.json",
                    {
                        "status": "stopped export; cleanup proof incomplete; not graded",
                        "digest": exported_digest,
                    },
                )
            except ValueError as error:
                execution["error"] = str(error)
                _write(target / "execution.json", execution)
        _write(
            target / "unsupported-submission.json",
            {"error": execution.get("error", "Harbor did not freeze a complete submission")},
        )
        return (
            {
                "id": stage_id,
                "snapshot": None,
                "correctness": None,
                "evidence": None,
                "work": _work(execution),
            },
            execution,
        )
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
        )
    snapshot = target / "snapshot"
    shutil.copytree(
        area / "workspace", snapshot, symlinks=True, ignore=shutil.ignore_patterns(*EXCLUDED_NAMES)
    )
    stage = _verify_snapshot(
        root=root,
        snapshot=snapshot,
        task=task,
        study=study,
        target=target,
        stage_id=stage_id,
        execution=execution,
    )
    return stage, execution


def _verify_snapshot(
    *,
    root: Path,
    snapshot: Path,
    task: TaskSpec,
    study: ResolvedStudy,
    target: Path,
    stage_id: str,
    execution: dict[str, Any],
) -> dict[str, Any]:
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
    return {
        "id": stage_id,
        "snapshot": snapshot_ref,
        "correctness": _reference(root, target / "correctness.json"),
        "evidence": evidence or None,
        "work": _work(execution),
    }


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
    calls = sum(len(starts) for starts in invocation_starts.values())
    _write(
        root / "run.json",
        {
            **{key: existing[key] for key in ("evidence_kind",) if key in existing},
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
            "baseline_origin": {
                path.parent.name: {
                    **json.loads(path.read_text()),
                    "evidence": _reference(root, path),
                }
                for path in sorted((root / "baseline-evidence").glob("*/origin.json"))
            },
        },
    )


def run_study(
    study: ResolvedStudy,
    output: Path,
    *,
    progress: Callable[[str], None] | None = None,
    baseline_cache: Path | None = None,
    baseline_identity: dict[str, object] | None = None,
) -> Path:
    validate_jobs(study.recipe.jobs)
    if baseline_cache is not None and not baseline_identity:
        raise ValueError("baseline reuse requires a fixed coding identity")
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
    }
    _checkpoint(root, frozen, manifest, states, status="preflight")
    tasks = {task.task_id: task for task in frozen.tasks}
    cache = None
    try:
        expected_runtime = harbor_backend.runtime_identity(
            cpus=frozen.recipe.container_cpus, memory_mb=frozen.recipe.container_memory_mb
        )
        if baseline_identity is not None:
            requested_runtime = baseline_identity.get("isolation")
            if not isinstance(requested_runtime, dict):
                raise ValueError("baseline reuse requires a fixed filesystem isolation identity")
            harbor_backend.assert_runtime(requested_runtime)
            expected_runtime = requested_runtime
        if baseline_cache is not None:
            assert baseline_identity is not None
            cache = BaselineCache(
                path=baseline_cache,
                root=root,
                tasks=frozen.tasks,
                entries=plan["entries"],
                identity={
                    **baseline_identity,
                    "study": {
                        key: getattr(frozen.recipe, key)
                        for key in (
                            "harness",
                            "model",
                            "reasoning",
                            "jobs",
                            "timeout_seconds",
                            "seed",
                        )
                    },
                },
            ).reserve()
            plan["baseline_cache"] = cache.decisions()
            _write(root / "plan.json", plan)
            _checkpoint(root, frozen, manifest, states, status="preflight")
        preflight = harbor_backend.preflight(
            jobs=frozen.recipe.jobs,
            cpus=frozen.recipe.container_cpus,
            memory_mb=frozen.recipe.container_memory_mb,
        )
        preflight["filesystem_isolation"] = expected_runtime
        preflight["harness_version"] = frozen.recipe.harness_version
        _write(root / "runtime-preflight.json", preflight)
        _checkpoint(
            root,
            frozen,
            manifest,
            states,
            status="running",
        )

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
                harbor_backend.assert_runtime(expected_runtime)
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
                assert task.fixture is not None
                attempt_dir = root / "attempts" / entry["id"]
                imported = cache.import_attempt(entry) if cache is not None else None
                if imported is not None:
                    record = imported
                before = _reference(root, task.fixture, snapshot=True)
                before_evidence = _collect(
                    root,
                    task.fixture,
                    attempt_dir / "before-evidence",
                    frozen.recipe.collectors,
                    before=task.fixture,
                )
                record.update(before=before, before_evidence=before_evidence or None)
                if imported is not None:
                    for stage in record["stages"]:
                        target = attempt_dir / stage["id"]
                        execution = json.loads((target / "execution.json").read_text())
                        if stage["snapshot"] is not None:
                            verified = _verify_snapshot(
                                root=root,
                                snapshot=root / stage["snapshot"]["path"],
                                task=task,
                                study=frozen,
                                target=target,
                                stage_id=stage["id"],
                                execution=execution,
                            )
                            stage.update(verified)
                    completed = True
                    return
                if cancelled.is_set():
                    return
                with tempfile.TemporaryDirectory(prefix="pluginbench-study-") as temporary:
                    area = Path(temporary) / "handoff"
                    shutil.copytree(task.fixture, area / "workspace")
                    stage, execution = _execute_stage(
                        root=root,
                        area=area,
                        entry=entry,
                        task=task,
                        study=frozen,
                        stage_id="handoff",
                        prompt=_prompt(task, frozen.guidance.get(entry["arm"], ())),
                        on_native_start=started,
                        expected_runtime=expected_runtime,
                    )
                    record.update(
                        stages=[stage],
                        model=_consistent_observation([execution], "actual_primary_models"),
                        runtime={
                            "harness": f"{study.recipe.harness}-cli",
                            "harness_version": _observed_harness_version([execution]),
                            "requested_reasoning_effort": study.recipe.reasoning,
                            "reasoning_effort": _consistent_observation(
                                [execution], "runtime_effort_evidence"
                            ),
                        },
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
        _checkpoint(root, frozen, manifest, states, status="complete")
        if cache is not None:
            cache.publish(records, frozen.tasks)
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
    finally:
        if cache is not None:
            cache.close()
    report_study(root)
    return root


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


def report_study(
    root: Path,
    *,
    measurement_revision: str | None = None,
    review_revision: str | None = None,
) -> Path:
    root = root.resolve()
    _assert_frozen(root)
    run = json.loads((root / "run.json").read_text())
    _bound_document(root, run["assessment_input"])
    plan = _bound_document(root, run["plan"])
    preflight = _bound_document(root, run["runtime_preflight"])
    assigned_harness_version = preflight.get("harness_version")
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
    review_path = None
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
    report["study_progress"] = {
        "status": run.get("status", "complete"),
        "completed_attempts": run["completed_attempts"],
        "assigned_attempts": len(plan["entries"]),
        "native_invocations": run["actual_calls"],
        "error": run.get("error"),
    }
    report["baseline_origin"] = run.get("baseline_origin", {})
    for origin in report["baseline_origin"].values():
        if _bound_document(root, origin["evidence"]) != {
            key: value for key, value in origin.items() if key != "evidence"
        }:
            raise ValueError("baseline origin differs from its bound evidence")
        for reference in origin["historical_evidence"]:
            _bound_path(root, reference)
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
        for row in review["records"]:
            observation = (
                _bound_document(root, row["observation"]) if row.get("observation") else None
            )
            if observation and observation.get("recovery"):
                _bound_document(root, observation["recovery"]["original_observation"])
            issues = None
            severity_counts = None
            category_counts = None
            if row["status"] == "reviewed" and observation and observation.get("response"):
                from pluginbench.study_review import JudgeResponse

                response = JudgeResponse.model_validate(observation["response"])
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
                severity_counts = {
                    severity: sum(issue["severity"] == severity for issue in issues)
                    for severity in ("P0", "P1", "P2", "P3")
                }
            observations.append(
                {
                    "attempt_id": row["id"],
                    "stage": protocol["stage"],
                    **protocol["selections"][row["id"]],
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
            "stage": protocol["stage"],
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
                "execution_backend": "harbor",
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
