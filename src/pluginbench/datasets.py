from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifierEntry(CatalogModel):
    command: list[str] = Field(min_length=1)
    script: Path
    timeout_seconds: float = Field(default=300, gt=0)


class TaskEntry(CatalogModel):
    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    fixture: Path
    verifier: VerifierEntry
    provenance: dict[str, str] | None = None


class TaskCatalog(CatalogModel):
    schema_version: Literal[1]
    tasks: list[TaskEntry] = Field(min_length=1)


@dataclass(frozen=True)
class VerifierSpec:
    command: tuple[str, ...]
    script: Path
    timeout_seconds: float


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    fixture: Path | None
    verifier: VerifierSpec | None
    digest: str
    provenance: dict[str, str] | None = None


def _fixture_entries(root: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    directories: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *filenames]:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise ValueError(f"task fixture contains unsupported symlink: {candidate}")
        directories.extend(directory_path / name for name in names)
        files.extend(directory_path / name for name in filenames)

    def key(path: Path) -> str:
        return path.relative_to(root).as_posix()

    return sorted(directories, key=key), sorted(files, key=key)


def _digest_path_metadata(digest: Any, root: Path, path: Path, kind: str) -> None:
    relative = path.relative_to(root).as_posix().encode()
    digest.update(kind.encode())
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))


def _task_digest(entry: TaskEntry, fixture: Path, verifier_script: Path) -> str:
    digest = hashlib.sha256()
    contract = {
        "id": entry.id,
        "prompt": entry.prompt,
        "verifier": entry.verifier.model_dump(mode="json"),
    }
    if entry.provenance is not None:
        contract["provenance"] = entry.provenance
    digest.update(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode())
    directories, files = _fixture_entries(fixture)
    for path in directories:
        _digest_path_metadata(digest, fixture, path, "directory")
    for path in files:
        _digest_path_metadata(digest, fixture, path, "file")
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    digest.update(b"verifier")
    digest.update((verifier_script.stat().st_mode & 0o111).to_bytes(2, "big"))
    verifier_content = verifier_script.read_bytes()
    digest.update(len(verifier_content).to_bytes(8, "big"))
    digest.update(verifier_content)
    return f"sha256:{digest.hexdigest()}"


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

    tasks: list[TaskSpec] = []
    for task_id in selected:
        entry = by_id[task_id]
        for item in (entry.fixture, entry.verifier.script):
            if item.is_absolute() or ".." in item.parts:
                raise ValueError(
                    "task fixtures and verifier scripts must be relative to their catalog"
                )
            raw_path = source.parent / item
            if any(candidate.is_symlink() for candidate in (raw_path, *raw_path.parents)):
                raise ValueError(f"task input must not be a symlink: {raw_path}")
        fixture = (source.parent / entry.fixture).resolve()
        if not fixture.is_dir():
            raise ValueError(f"task fixture is not a directory for {task_id}: {fixture}")
        verifier_script = (source.parent / entry.verifier.script).resolve()
        if not verifier_script.is_file():
            raise ValueError(f"task verifier script is not a file for {task_id}: {verifier_script}")
        if verifier_script.is_relative_to(fixture):
            raise ValueError(
                f"task verifier must be outside the writable fixture for {task_id}: "
                f"{verifier_script}"
            )
        tasks.append(
            TaskSpec(
                task_id=task_id,
                prompt=entry.prompt,
                fixture=fixture,
                verifier=VerifierSpec(
                    command=(*entry.verifier.command, str(verifier_script)),
                    script=verifier_script,
                    timeout_seconds=entry.verifier.timeout_seconds,
                ),
                digest=_task_digest(entry, fixture, verifier_script),
                provenance=entry.provenance,
            )
        )
    return tasks
