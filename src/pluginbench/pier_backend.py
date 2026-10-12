"""Read and freeze legacy Pier task catalogs; execution is retired."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


PIER_VERSION = "0.3.1"


class PierTaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    path: Path


class PierCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    source: dict[str, str]
    tasks: list[PierTaskEntry] = Field(min_length=1)


@dataclass(frozen=True)
class PierTask:
    task_id: str
    prompt: str
    directory: Path
    base_commit: str
    digest: str
    provenance: dict[str, str]


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _tree_digest(root: Path) -> str:
    rows = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Pier task contains an unsupported symlink: {file}")
        if file.is_file():
            rows.append(
                (
                    file.relative_to(root).as_posix(),
                    file.stat().st_mode & 0o777,
                    hashlib.sha256(file.read_bytes()).hexdigest(),
                )
            )
    return "sha256:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _pinned_task(directory: Path) -> tuple[str, dict[str, str]]:
    data = tomllib.loads((directory / "task.toml").read_text())
    metadata = data["metadata"]
    base = metadata["base_commit_hash"]
    if not isinstance(base, str) or re.fullmatch(r"[a-f0-9]{40}", base) is None:
        raise ValueError("Pier task requires an exact base_commit_hash")
    image = data["environment"]["docker_image"]
    if re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image) is None:
        raise ValueError("Pier task agent docker_image must be pinned by digest")
    verifier = data["verifier"]
    if verifier.get("environment_mode") != "separate":
        raise ValueError("Pier studies require separate verifier environments")
    if data["environment"].get("mcp_servers") or data["environment"].get("skills_dir"):
        raise ValueError("Pier studies supply text only, not task MCP servers or skills")
    dockerfile = (directory / "tests/Dockerfile").read_text()
    from_images = re.findall(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", dockerfile, re.MULTILINE)
    if not from_images or any(
        re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", item) is None for item in from_images
    ):
        raise ValueError("Pier verifier Dockerfile FROM images must be pinned by digest")
    return base, {
        "repository_url": metadata["repository_url"],
        "base_commit": base,
        "agent_image": image,
        "verifier_images": ",".join(from_images),
    }


def load_tasks(path: Path) -> tuple[PierTask, ...]:
    catalog = PierCatalog.model_validate(yaml.safe_load(path.read_text()))
    if re.fullmatch(r"[a-f0-9]{40}", catalog.source.get("revision", "")) is None:
        raise ValueError("Pier catalog requires an exact source revision")
    if len({entry.id for entry in catalog.tasks}) != len(catalog.tasks):
        raise ValueError("Pier task IDs must be unique")
    tasks = []
    for entry in catalog.tasks:
        candidate = path.parent / entry.path
        if (
            entry.path.is_absolute()
            or ".." in entry.path.parts
            or any(parent.is_symlink() for parent in (candidate, *candidate.parents))
            or not candidate.resolve().is_relative_to(path.parent.resolve())
        ):
            raise ValueError("Pier task directories must be relative, without symlinks")
        directory = candidate.resolve()
        base, metadata = _pinned_task(directory)
        tasks.append(
            PierTask(
                entry.id,
                (directory / "instruction.md").read_text(),
                directory,
                base,
                _tree_digest(directory),
                {**{f"catalog_{key}": value for key, value in catalog.source.items()}, **metadata},
            )
        )
    return tuple(tasks)


def freeze_tasks(tasks: tuple[PierTask, ...], directory: Path) -> tuple[PierTask, ...]:
    frozen = []
    for index, task in enumerate(tasks):
        if _tree_digest(task.directory) != task.digest:
            raise ValueError("Pier source task changed before freezing")
        target = directory / str(index)
        shutil.copytree(task.directory, target)
        frozen.append(
            PierTask(
                task.task_id,
                task.prompt,
                target,
                task.base_commit,
                _tree_digest(target),
                task.provenance,
            )
        )
    return tuple(frozen)
