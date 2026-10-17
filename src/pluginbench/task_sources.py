"""Remote task references and an optional fzf picker; no model calls."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Coroutine
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, PrivateAttr, TypeAdapter, model_validator

from pluginbench.harbor_backend import native_task_assets as task_fingerprint


class _CatalogMetadata(BaseModel):
    name: str
    version: str
    task_ids: list[dict[str, str]]


class _DownloadedTask(BaseModel):
    path: Path
    resolved_git_commit_id: str | None = None


def _error_messages(error: Exception) -> str:
    if isinstance(error, ExceptionGroup):
        return "; ".join(_error_messages(child) for child in error.exceptions)
    return f"{type(error).__name__}: {error}"


def _harbor_response(request: Coroutine[object, object, object]) -> BaseModel:
    try:
        response = asyncio.run(request)
    except Exception as error:
        raise ValueError(f"Harbor task lookup/download failed: {_error_messages(error)}") from error
    if not isinstance(response, BaseModel):
        raise ValueError("Harbor returned an invalid task response")
    return response


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "pluginbench/tasks"


def github_task(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 6
        or parts[2] != "tree"
        or parts[4] != "tasks"
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[index]) for index in (0, 1, 3, 5)
        )
    ):
        raise ValueError(
            "use a GitHub task-folder URL: https://github.com/owner/repo/tree/ref/tasks/id"
        )
    return parts[5], parts[3]


def dataset_catalog(
    dataset: str, version: str | None = None
) -> tuple[str, str, list[dict[str, str]]]:
    # Harbor is optional: importing the CLI and rebuilding reports must work without it.
    client = importlib.import_module("harbor.registry.client.package").PackageDatasetClient()

    if version and "@" in dataset:
        raise ValueError("provide the dataset version once")
    response = _harbor_response(
        client.get_dataset_metadata(f"{dataset}@{version}" if version else dataset)
    )
    metadata = _CatalogMetadata.model_validate_json(response.model_dump_json())
    if not metadata.version or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata.version):
        raise ValueError("Harbor dataset did not resolve to an immutable version")
    references = []
    for task in metadata.task_ids:
        if (
            set(task) != {"org", "name", "ref"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", task["ref"])
            or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task[key])
                for key in ("org", "name")
            )
        ):
            raise ValueError("dataset contains an unsupported or unresolved task reference")
        references.append(task)
    return metadata.name, metadata.version, references


def download_tasks(references: list[dict[str, str]]) -> list[tuple[Path, dict[str, str]]]:
    destination = _snapshot_destination(references)
    if destination.exists():
        _validate_snapshot(destination, references)
        return [(destination / "tasks" / task_name(ref), ref) for ref in references]
    ids = importlib.import_module("harbor.models.task.id")
    client = importlib.import_module("harbor.tasks.client").TaskClient()

    tasks = [
        ids.PackageTaskId.model_validate(ref) if "org" in ref else ids.GitTaskId.model_validate(ref)
        for ref in references
    ]
    # Harbor trusts existing cache directories without hashing them. Import fresh bytes once;
    # later reads use PluginBench's own complete asset fingerprint and origin receipt.
    cache_root().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=cache_root()) as temporary:
        result = _harbor_response(
            client.download_tasks(tasks, output_dir=Path(temporary), overwrite=True)
        )
        receipts = TypeAdapter(list[_DownloadedTask]).validate_python(
            result.model_dump(mode="json")["results"]
        )
        resolved = []
        for ref, downloaded in zip(references, receipts, strict=True):
            if "git_url" in ref:
                sha = downloaded.resolved_git_commit_id
                if not sha or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
                    raise ValueError("Git task did not resolve to an immutable commit")
                ref = {**ref, "git_commit_id": sha}
            resolved.append((downloaded.path, ref))
        destination = materialize_tasks(resolved)
        return [(destination / "tasks" / task_name(ref), ref) for _, ref in resolved]


def task_name(reference: dict[str, str]) -> str:
    return reference["name"] if "org" in reference else Path(reference["path"]).name


def _snapshot_destination(references: list[dict[str, str]]) -> Path:
    identity = hashlib.sha256(json.dumps(references, sort_keys=True).encode()).hexdigest()
    return cache_root() / "native-snapshots" / identity


def _validate_snapshot(destination: Path, references: list[dict[str, str]]) -> None:
    if destination.is_symlink() or any(
        (destination / name).is_symlink() for name in (".pluginbench-source.json", "tasks")
    ):
        raise ValueError("cached task provenance cannot follow symlinks")
    provenance = json.loads((destination / ".pluginbench-source.json").read_text())
    if provenance["references"] != references:
        raise ValueError("cached task references changed")
    names = {task_name(ref) for ref in references}
    if set(provenance["task_hashes"]) != names:
        raise ValueError("cached task inventory changed")
    for name in names:
        if task_fingerprint(destination / "tasks" / name) != provenance["task_hashes"][name]:
            raise ValueError(f"cached task assets changed: {name}")


def materialize_tasks(downloads: list[tuple[Path, dict[str, str]]]) -> Path:
    """Publish full task assets atomically and revalidate every later cache read."""
    references = [ref for _, ref in downloads]
    destination = _snapshot_destination(references)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("task snapshot cache cannot follow symlinks")
    if destination.exists():
        _validate_snapshot(destination, references)
        return destination
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=parent) as temporary:
        staging = Path(temporary) / "source"
        (staging / "tasks").mkdir(parents=True)
        fingerprints = {}
        for source, ref in downloads:
            name = task_name(ref)
            fingerprint = task_fingerprint(source)
            importlib.import_module("harbor.models.task.task").Task(source)
            target = staging / "tasks" / name
            shutil.copytree(source, target, symlinks=True)
            if task_fingerprint(target) != fingerprint or task_fingerprint(source) != fingerprint:
                raise ValueError("task assets changed while freezing input")
            fingerprints[name] = fingerprint
        (staging / ".pluginbench-source.json").write_text(
            json.dumps({"references": references, "task_hashes": fingerprints}, indent=2) + "\n"
        )
        try:
            staging.rename(destination)
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            # Another picker/run may have published the same immutable selection.
            return materialize_tasks(downloads)
    return destination


class Tasks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Path | None = None
    dataset: str | None = None
    version: str | None = None
    ids: list[str] = []
    urls: list[str] = []
    _resolved: Path | None = PrivateAttr(default=None)
    _references: list[dict[str, str]] = PrivateAttr(default_factory=list)

    @property
    def task_ids(self) -> list[str]:
        return [github_task(url)[0] for url in self.urls] or [
            value.split("/")[-1] for value in self.ids
        ]

    @model_validator(mode="after")
    def coherent(self) -> Tasks:
        if sum((self.source is not None, self.dataset is not None, bool(self.urls))) != 1:
            raise ValueError("choose exactly one task source: source, dataset, or urls")
        if self.urls and self.ids or self.version is not None and self.dataset is None:
            raise ValueError("ids require source/dataset; version requires dataset")
        if not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("task IDs must be nonempty and unique")
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?", value)
            for value in self.ids
        ):
            raise ValueError("invalid task ID")
        return self

    def resolve(self) -> Path:
        if self.source is not None:
            return self.source
        if self._resolved is not None:
            # Revalidate published source bytes, even within one process.
            return materialize_tasks(
                [(self._resolved / "tasks" / task_name(ref), ref) for ref in self._references]
            )
        references = []
        if self.dataset is not None:
            self.dataset, self.version, catalog = dataset_catalog(self.dataset, self.version)
            for selected in self.ids:
                matches = [
                    ref
                    for ref in catalog
                    if selected == f"{ref['org']}/{ref['name']}" or selected == ref["name"]
                ]
                if len(matches) != 1:
                    raise ValueError(f"task is missing or ambiguous in dataset: {selected}")
                references.append(matches[0])
        else:
            resolver = importlib.import_module(
                "harbor.registry.client.git_repo"
            ).resolve_repo_source

            for url in self.urls:
                github_task(url)
                repo = resolver(url)
                references.append(
                    {
                        "git_url": repo.git_url,
                        "git_commit_id": repo.ref or "HEAD",
                        "path": repo.subdir or "",
                    }
                )
        downloads = download_tasks(references)
        self._resolved = materialize_tasks(downloads)
        self._references = [ref for _, ref in downloads]
        if self.urls:
            self.urls = [
                f"{ref['git_url'].removesuffix('.git')}/tree/{ref['git_commit_id']}/{ref['path']}"
                for ref in self._references
            ]
        return self._resolved

    @property
    def references(self) -> list[dict[str, str]]:
        return self._references


def printable(text: str) -> str:
    return "".join(char for char in text if char.isprintable() or char in "\n\t")


def preview_task(packet: Path, index: int) -> str:
    references = json.loads(packet.read_text())
    ref = references[index]
    source, _ = download_tasks([ref])[0]
    if (
        source.is_symlink()
        or any(file.is_symlink() for file in source.rglob("*"))
        or any(not (source / name).is_file() for name in ("task.toml", "instruction.md"))
    ):
        raise ValueError("task preview cannot follow symlinks")
    definition = tomllib.loads((source / "task.toml").read_text())
    metadata = definition.get("metadata", {})
    name = task_name(ref)
    return printable(
        f"{name}\nNative Harbor task; selected measurements are checked before evaluation\n"
        f"Language: {metadata.get('language', 'unspecified')}\n"
        f"Repository: {metadata.get('repository_url', 'unspecified')}\n\n"
        + (source / "instruction.md").read_text()
    )


def pick_tasks(dataset: str, version: str | None = None) -> str | None:
    fzf = shutil.which("fzf")
    if fzf is None:
        raise ValueError("tasks pick requires fzf; install fzf or specify tasks in your config")
    if not sys.stderr.isatty():
        raise ValueError("tasks pick requires an interactive terminal")
    name, pin, references = dataset_catalog(dataset, version)
    lines = [f"{index}\t{ref['org']}/{ref['name']}" for index, ref in enumerate(references)]
    with tempfile.TemporaryDirectory(prefix="pluginbench-picker-") as temporary:
        packet = Path(temporary) / "catalog.json"
        packet.write_text(json.dumps(references))
        preview = (
            " ".join(
                shlex.quote(value)
                for value in (sys.executable, "-m", "pluginbench.task_sources", str(packet))
            )
            + " {1}"
        )
        result = subprocess.run(
            [
                fzf,
                "--multi",
                "--layout=reverse",
                "--delimiter=\t",
                "--with-nth=2..",
                "--prompt=Tasks > ",
                "--header=Tab select | Enter YAML | Esc cancel",
                "--preview",
                preview,
                "--preview-window=right,50%,wrap",
            ],
            input="\n".join(lines),
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise ValueError(f"fzf failed with exit code {result.returncode}")
    chosen = result.stdout.splitlines()
    if not chosen:
        return None
    if len(set(chosen)) != len(chosen) or any(line not in lines for line in chosen):
        raise ValueError("fzf returned an invalid task selection")
    selected = [references[int(line.split("\t", 1)[0])] for line in chosen]
    tasks = Tasks(
        dataset=name, version=pin, ids=[f"{ref['org']}/{ref['name']}" for ref in selected]
    )
    # Freeze selected task assets without resolving the dataset twice.
    materialize_tasks(download_tasks(selected))
    return yaml.safe_dump({"tasks": tasks.model_dump(exclude={"source", "urls"})}, sort_keys=False)


def copy_yaml(block: str) -> None:
    candidates = (
        [("pbcopy", [])]
        if sys.platform == "darwin"
        else [
            ("wl-copy", []),
            ("xclip", ["-selection", "clipboard"]),
            ("xsel", ["--clipboard", "--input"]),
        ]
    )
    for name, arguments in candidates:
        executable = shutil.which(name)
        if executable:
            subprocess.run([executable, *arguments], input=block, text=True, check=True)
            return
    raise ValueError("no clipboard command available; the tasks YAML was printed to stdout")


if __name__ == "__main__":
    try:
        print(preview_task(Path(sys.argv[1]), int(sys.argv[2])))
    except (OSError, ValueError, ImportError) as error:
        print(f"Preview unavailable: {error}", file=sys.stderr)
        raise SystemExit(2) from error
