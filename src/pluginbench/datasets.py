"""Frozen source and standard Harbor task contracts for evaluation studies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pluginbench import harbor_backend, submissions
from pluginbench.assessment_metrics import inspect_snapshot


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskEntry(CatalogModel):
    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    fixture: Path
    native_task: Path
    provenance: dict[str, str]


class TaskCatalog(CatalogModel):
    schema_version: Literal[1]
    tasks: list[TaskEntry] = Field(min_length=1)


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    fixture: Path
    native_task: Path
    digest: str
    provenance: dict[str, str]


def native_identity(task: TaskSpec | TaskEntry) -> dict[str, object]:
    """Read the prepared runtime payload without hiding incomplete preparation."""
    value = json.loads(task.provenance.get("native_task_identity", "null"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("task requires a prepared native_task_identity")
    cleanup = value.get("cleanup")
    if (
        value.get("status") != "complete"
        or not isinstance(cleanup, dict)
        or cleanup.get("status") != "complete"
    ):
        raise ValueError("native task preparation or cleanup is incomplete")
    return value


def portable_native_identity(identity: dict[str, object]) -> dict[str, object]:
    """Run-folder locations are not part of a reusable coding identity."""
    keys = {
        "task_assets_sha256",
        "original_task_assets_sha256",
        "source_digest",
        "workdir",
        "image_identity",
        "toolchain_identity",
        "grader_image_identity",
        "resources",
        "isolation",
        "trusted_tests_sha256",
        "verifier_test_delivery",
        "transport_policy",
        "preparation_key",
        "preparation_policy",
    }
    missing = keys - identity.keys()
    if missing:
        raise ValueError("native coding identity is incomplete: " + ", ".join(sorted(missing)))
    return {
        **{key: identity[key] for key in sorted(keys)},
        "source_policy": identity.get("source_policy", "legacy-unqualified"),
        "source_qualification": submissions.portable_qualification(
            identity.get("source_qualification")
        ),
    }


def _catalog_path(source: Path, item: Path) -> Path:
    if item.is_absolute() or ".." in item.parts:
        raise ValueError("task source and native assets must be relative to their catalog")
    raw = source.parent / item
    if any(candidate.is_symlink() for candidate in (raw, *raw.parents)):
        raise ValueError(f"task input must not be a symlink: {raw}")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise ValueError(f"task input is not a directory: {resolved}")
    return resolved


def source_metadata(source: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(source).as_posix(): {
            "mode": path.stat().st_mode & 0o111,
            "directory": path.is_dir(),
        }
        for path in sorted(source.rglob("*"))
    }


def _task_digest(entry: TaskEntry, fixture: Path, native_task: Path) -> str:
    identity = native_identity(entry)
    source_digest = inspect_snapshot(fixture)["digest"]
    asset_digest = harbor_backend.native_task_assets(native_task)
    if source_digest != identity.get("source_digest"):
        raise ValueError("prepared native source changed")
    if asset_digest != identity.get("task_assets_sha256"):
        raise ValueError("prepared native task assets changed")
    provenance = {
        key: value for key, value in entry.provenance.items() if key != "native_task_identity"
    }
    contract = {
        "id": entry.id,
        "prompt": entry.prompt,
        "provenance": provenance,
        "source_digest": source_digest,
        "source_metadata": source_metadata(fixture),
        "native_task_identity": portable_native_identity(identity),
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_tasks(source: Path, task_ids: list[str] | None = None) -> list[TaskSpec]:
    try:
        loaded = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read task catalog {source}: {exc}") from exc
    catalog = TaskCatalog.model_validate(loaded)
    by_id = {entry.id: entry for entry in catalog.tasks}
    if len(by_id) != len(catalog.tasks):
        raise ValueError("task catalog contains duplicate task IDs")
    selected = list(by_id) if task_ids is None else task_ids
    if not selected or any(not task_id.strip() for task_id in selected):
        raise ValueError("selected task IDs must be nonempty")
    if len(set(selected)) != len(selected):
        raise ValueError("selected task IDs must be unique")
    missing = [task_id for task_id in selected if task_id not in by_id]
    if missing:
        raise ValueError("selected task IDs are missing from catalog: " + ", ".join(missing))
    tasks = []
    for task_id in selected:
        entry = by_id[task_id]
        fixture = _catalog_path(source, entry.fixture)
        native_task = _catalog_path(source, entry.native_task)
        if native_task.is_relative_to(fixture) or fixture.is_relative_to(native_task):
            raise ValueError("native task assets must be outside the writable source fixture")
        tasks.append(
            TaskSpec(
                task_id,
                entry.prompt,
                fixture,
                native_task,
                _task_digest(entry, fixture, native_task),
                entry.provenance,
            )
        )
    return tasks
