from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pluginbench.config import DatasetConfig


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


class TaskCatalog(CatalogModel):
    schema_version: Literal[1]
    tasks: list[TaskEntry] = Field(min_length=1)


@dataclass(frozen=True)
class VerifierSpec:
    command: tuple[str, ...]
    script: Path
    timeout_seconds: float


@dataclass(frozen=True)
class SWEbenchSpec:
    repo: str
    base_commit: str
    image: str
    dataset_name: str
    dataset_version: str
    harness_executable: Path
    harness_version: str
    image_platform: str | None
    row: dict[str, Any]


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    fixture: Path | None
    verifier: VerifierSpec | None
    digest: str
    swebench: SWEbenchSpec | None = None


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


def _load_local_tasks(config: DatasetConfig) -> list[TaskSpec]:
    source = Path(config.source)
    try:
        loaded = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read local task catalog {source}: {exc}") from exc
    catalog = TaskCatalog.model_validate(loaded)
    by_id = {entry.id: entry for entry in catalog.tasks}
    if len(by_id) != len(catalog.tasks):
        raise ValueError("local task catalog contains duplicate task IDs")
    missing = [task_id for task_id in config.task_ids if task_id not in by_id]
    if missing:
        raise ValueError("selected task IDs are missing from local catalog: " + ", ".join(missing))

    tasks: list[TaskSpec] = []
    for task_id in config.task_ids:
        entry = by_id[task_id]
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
            )
        )
    return tasks


class SWEbenchRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    instance_id: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    problem_statement: str = Field(min_length=1)
    version: str = Field(min_length=1)
    image: str = Field(pattern=r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
    eval_script: str = Field(min_length=1)
    log_parser: str = Field(min_length=1)
    eval_type: str = Field(min_length=1)
    FAIL_TO_PASS: list[str] | str
    PASS_TO_PASS: list[str] | str


def _read_swebench_rows(source: Path) -> list[dict[str, Any]]:
    try:
        if source.suffix == ".jsonl":
            loaded = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
        else:
            loaded = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read SWE-bench snapshot {source}: {exc}") from exc
    if not isinstance(loaded, list) or not all(isinstance(row, dict) for row in loaded):
        raise ValueError("SWE-bench snapshot must contain a JSON list of task objects")
    return loaded


def _swebench_digest(row: dict[str, Any]) -> str:
    encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _load_swebench_tasks(config: DatasetConfig) -> list[TaskSpec]:
    assert isinstance(config.source, Path)
    assert config.harness is not None
    rows = [SWEbenchRow.model_validate(row) for row in _read_swebench_rows(config.source)]
    by_id = {row.instance_id: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("SWE-bench snapshot contains duplicate task IDs")
    missing = [task_id for task_id in config.task_ids if task_id not in by_id]
    if missing:
        raise ValueError(
            "selected task IDs are missing from SWE-bench snapshot: " + ", ".join(missing)
        )

    tasks: list[TaskSpec] = []
    for task_id in config.task_ids:
        row = by_id[task_id]
        serialized = row.model_dump(mode="json")
        tasks.append(
            TaskSpec(
                task_id=task_id,
                prompt=row.problem_statement,
                fixture=None,
                verifier=None,
                digest=_swebench_digest(serialized),
                swebench=SWEbenchSpec(
                    repo=row.repo,
                    base_commit=row.base_commit,
                    image=row.image,
                    dataset_name=config.name,
                    dataset_version=config.version,
                    harness_executable=config.harness.executable,
                    harness_version=config.harness.version,
                    image_platform=config.harness.image_platform,
                    row=serialized,
                ),
            )
        )
    return tasks


def load_tasks(config: DatasetConfig) -> list[TaskSpec]:
    if config.adapter == "local":
        return _load_local_tasks(config)
    if config.adapter == "swe-bench":
        return _load_swebench_tasks(config)
    raise ValueError(
        f"dataset adapter {config.adapter!r} requires its native benchmark harness; "
        "the Promptfoo POC currently executes local fixture catalogs only"
    )
