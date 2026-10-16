"""Reuse complete isolated baseline cohorts, never their observer results."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import tomllib
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from pluginbench import harbor_backend, submissions
from pluginbench.native_treatments import DELIVERY_POLICY, NativeCapabilities
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.assessment_models import AttemptInput
from pluginbench.datasets import TaskSpec, native_identity, portable_native_identity


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _inventory(root: Path) -> dict[str, dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"baseline cache contains an unsupported artifact: {path}")
        if path == root / "manifest.json":
            continue
        files[path.relative_to(root).as_posix()] = {
            "mode": path.stat().st_mode & 0o777,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        }
    return files


def _copy_attempt(source: Path, target: Path) -> None:
    def ignored(directory: str, names: list[str]) -> list[str]:
        return [
            name
            for name in names
            if name == "unsupported-submission"
            and Path(directory).parent == source
            and (Path(directory) / "unsupported-submission.json").is_file()
        ]

    shutil.copytree(source, target, ignore=ignored, symlinks=True)
    for stage in source.iterdir():
        raw = stage / "unsupported-submission"
        if raw.is_dir():
            # Keep unsupported link evidence inert; never extract or follow its targets.
            with tarfile.open(target / stage.name / "unsupported-submission.tar", "w") as archive:
                archive.add(raw, arcname="unsupported-submission", recursive=True)


def _docker_executable(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    if not all(
        isinstance(value.get(key), str) and Path(value[key]).is_absolute()
        for key in ("path", "resolved_path")
    ):
        return None
    digest, version = value.get("sha256"), value.get("version")
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or not isinstance(version, str)
        or not version
    ):
        return None
    return {key: field for key, field in value.items() if key != "path"}


def _matches_docker_client(artifacts: Path, expected: dict[str, object]) -> bool:
    try:
        actual = json.loads((artifacts / "docker-client.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(actual, dict):
        return False
    cli = _docker_executable(expected.get("docker_cli"))
    if cli is None or _docker_executable(actual.get("docker_cli")) != cli:
        return False
    plugins, observed = expected.get("docker_plugins"), actual.get("docker_plugins")
    if (
        not isinstance(plugins, dict)
        or not isinstance(observed, dict)
        or set(plugins) != {"compose", "buildx"}
        or set(observed) != set(plugins)
    ):
        return False
    for name, identity in plugins.items():
        binary = _docker_executable(identity)
        if binary is None or _docker_executable(observed[name]) != binary:
            return False
    return True


def _eligible(*, attempt: Path, record: dict[str, Any], coding: object, task: TaskSpec) -> bool:
    """Admission binds actual native source, private runtime and cleanup receipts."""
    if not isinstance(coding, dict):
        return False
    expected = coding.get("isolation")
    identity = coding.get("native_task_identity")
    settings = coding.get("study")
    if (
        not isinstance(expected, dict)
        or not isinstance(identity, dict)
        or not isinstance(settings, dict)
    ):
        return False
    image = identity.get("image_identity")
    toolchain = identity.get("toolchain_identity")
    if (
        not isinstance(image, dict)
        or not isinstance(toolchain, dict)
        or toolchain.get("image_id") != image.get("image_id")
        or toolchain.get("harness") != settings.get("harness")
        or toolchain.get("harness_version") != coding.get("harness_version")
        or identity.get("isolation") != expected
    ):
        return False
    runtime = record.get("runtime") or {}
    for actual, requested in (
        (record.get("model"), settings.get("model")),
        (runtime.get("harness"), str(settings.get("harness")) + "-cli"),
        (runtime.get("reasoning_effort"), settings.get("reasoning")),
        (runtime.get("harness_version"), coding.get("harness_version")),
    ):
        if actual is not None and actual != requested:
            return False
    try:
        return all(
            _matches_native_stage(
                target=attempt / stage["id"], stage=stage, coding=coding, task=task
            )
            for stage in record["stages"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _matches_treatment_delivery(
    *, artifacts: Path, invocation: dict[str, Any], execution: dict[str, Any],
    coding: dict[str, Any],
) -> bool:
    """Bind a native vanilla cache entry to its installed policy and empty treatment."""
    if "delivery" not in coding:
        return True  # Historical cache identities cannot equal a native identity.
    if coding["delivery"] != DELIVERY_POLICY:
        return False
    inventory: dict[str, list[object]] = {"guidance": [], "skills": [], "plugins": []}
    if (invocation.get("delivery") != DELIVERY_POLICY
            or invocation.get("capabilities") != coding.get("capabilities")
            or invocation.get("treatment_inventory") != inventory
            or (artifacts / "prompt.txt").read_bytes() != b""):
        return False
    receipt = json.loads((artifacts / "native-treatment.json").read_text())
    if (not isinstance(receipt, dict) or receipt.get("status") != "verified"
            or receipt.get("delivery") != DELIVERY_POLICY
            or receipt.get("inventory") != inventory
            or execution.get("native_treatment") != receipt):
        return False
    configuration = receipt.get("effective_config")
    if not isinstance(configuration, str):
        return False
    actual = tomllib.loads(configuration)
    policy = NativeCapabilities.model_validate(coding.get("capabilities"))
    expected = policy.codex_config(
        model=coding["study"]["model"], effort=coding["study"]["reasoning"],
    )
    for key, value in expected.items():
        if isinstance(value, dict):
            observed = actual.get(key)
            if not isinstance(observed, dict) or any(
                observed.get(setting) != required for setting, required in value.items()
            ):
                return False
        elif actual.get(key) != value:
            return False
    return True


def _matches_native_stage(
    *, target: Path, stage: dict[str, Any], coding: dict[str, Any], task: TaskSpec
) -> bool:
    identity, expected, settings = (
        coding["native_task_identity"],
        coding["isolation"],
        coding["study"],
    )
    artifacts = target / "artifacts"
    if not _matches_docker_client(artifacts, expected):
        return False
    invocation = json.loads((artifacts / "invocation.json").read_text())
    observed_identity = invocation.get("native_task_identity")
    if (
        not isinstance(observed_identity, dict)
        or portable_native_identity(observed_identity) != identity
    ):
        return False
    if any(
        invocation.get(key) != value
        for key, value in {
            "backend": harbor_backend.PROTOCOL,
            "harness": str(settings.get("harness")) + "-cli",
            "harness_version": coding.get("harness_version"),
            "requested_model": settings.get("model"),
            "requested_effort": settings.get("reasoning"),
            "timeout_seconds": settings.get("timeout_seconds", coding.get("timeout_seconds")),
            "task_assets_sha256": identity.get("task_assets_sha256"),
        }.items()
    ):
        return False
    execution = json.loads((target / "execution.json").read_text())
    if not _matches_treatment_delivery(
        artifacts=artifacts, invocation=invocation, execution=execution, coding=coding,
    ):
        return False
    if execution.get("private_runtime_removed") is not True:
        return False
    for key, requested in (
        ("actual_primary_models", settings.get("model")),
        ("runtime_effort_evidence", settings.get("reasoning")),
    ):
        values = execution.get(key, [])
        if not isinstance(values, list) or any(value != requested for value in values):
            return False
    receipt = json.loads((artifacts / "runtime-isolation.json").read_text())
    before = json.loads((artifacts / "before-source.json").read_text())
    exported = submissions.validate_export(
        diagnostic=target / "diagnostic-source", artifacts=artifacts, execution=execution
    )
    if (
        receipt.get("export") != exported
        or not _native_export_matches(before, identity, identity.get("source_digest"))
        or not _native_export_matches(exported, identity, exported["snapshot_digest"])
    ):
        return False
    if before.get("container_id") != exported.get("container_id") or before.get(
        "project"
    ) != exported.get("project"):
        return False
    if execution.get("native_verifier_result") is not None and not _native_grader_matches(
        receipt.get("verifier"), identity
    ):
        return False
    if stage.get("snapshot"):
        source = submissions.validate(
            before=task.fixture,
            task_dir=task.native_task,
            identity=native_identity(task),
            artifacts=artifacts,
            execution=execution,
            snapshot=target / "snapshot",
        )
        if source["snapshot_digest"] != stage["snapshot"]["sha256"]:
            return False
    else:
        failure = json.loads((target / "unsupported-submission.json").read_text())
        if failure.get("source_policy") != submissions.SOURCE_POLICY:
            return False
        with tempfile.TemporaryDirectory(prefix="pluginbench-cache-source-") as temporary:
            try:
                submissions.reconstruct(
                    before=task.fixture,
                    task_dir=task.native_task,
                    identity=native_identity(task),
                    artifacts=artifacts,
                    execution=execution,
                    destination=Path(temporary).resolve() / "source",
                )
            except ValueError:
                pass
            else:
                return False
    return _native_cleanup_matches(receipt.get("cleanup"))


def _native_grader_matches(verifier: object, identity: dict[str, Any]) -> bool:
    if not isinstance(verifier, dict):
        return False
    if verifier.get("image_id") != identity["grader_image_identity"].get("image_id"):
        return False
    harbor_backend.stopped_container_state(verifier.get("state"))
    limits = identity["isolation"]["resources"]
    resources = verifier.get("resources")
    if not isinstance(resources, dict):
        return False
    return bool(
        resources.get("NanoCpus") == limits["cpus"] * 1_000_000_000
        and resources.get("Memory") == limits["memory_mb"] * 1024 * 1024
    )


def _native_cleanup_matches(cleanup: object) -> bool:
    if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
        return False
    projects = cleanup.get("projects")
    if not isinstance(projects, dict) or not projects:
        return False
    return all(
        isinstance(project, dict)
        and project.get("status") == "complete"
        and project.get("residuals") == {"containers": [], "networks": [], "images": []}
        for project in projects.values()
    )


def _native_export_matches(value: object, identity: dict[str, Any], digest: object) -> bool:
    if not isinstance(value, dict) or not isinstance(identity.get("image_identity"), dict):
        return False
    image = identity["image_identity"]
    if (
        value.get("status") != "complete"
        or value.get("stopped") is not True
        or value.get("snapshot_digest") != digest
        or not isinstance(digest, str)
        or value.get("image_id") != image.get("image_id")
        or value.get("workdir") != identity.get("workdir")
        or value.get("resources")
        != {key: identity["isolation"]["resources"][key] for key in ("cpus", "memory_mb")}
        or not isinstance(value.get("container_id"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["container_id"])
        or not isinstance(value.get("project"), str)
        or not re.fullmatch(r"pluginbench-[a-f0-9]{32}__env", value["project"])
    ):
        return False
    harbor_backend.stopped_container_state(value.get("state"))
    return True


class BaselineCache:
    """Reserve all missing keys before calls; hold each lock until atomic publication."""

    def __init__(
        self,
        *,
        path: Path,
        root: Path,
        tasks: tuple[TaskSpec, ...],
        entries: list[dict[str, Any]],
        identity: dict[str, object],
    ) -> None:
        self.path = path.absolute()
        self.root = root
        self.tasks = {task.task_id: task for task in tasks}
        self.entries = [entry for entry in entries if entry["arm"] == "baseline"]
        self.contexts = {}
        for task in tasks:
            coding = {
                key: value.get(task.task_id)
                if key in {"task_assets", "task_environment"} and isinstance(value, dict)
                else value
                for key, value in identity.items()
            }
            coding["native_task_identity"] = portable_native_identity(native_identity(task))
            self.contexts[task.task_id] = {
                "protocol": "native-harbor-baseline-cohort-v3",
                "source_policy": submissions.SOURCE_POLICY,
                "coding": coding,
                "task_id": task.task_id,
                "task_digest": task.digest,
                "prompt": task.prompt,
                "fixture_digest": inspect_snapshot(task.fixture)["digest"],
                "repeats": sorted(
                    entry["attempt"] for entry in self.entries if entry["task_id"] == task.task_id
                ),
            }
        self.keys = {task_id: _hash(context) for task_id, context in self.contexts.items()}
        self.hits: dict[str, dict[str, Any]] = {}
        self._resources = ExitStack()

    def reserve(self) -> BaselineCache:
        if any(path.is_symlink() for path in (self.path, *self.path.parents)):
            raise ValueError("baseline cache must not follow symlinks")
        (self.path / ".locks").mkdir(parents=True, exist_ok=True)
        if (self.path / ".locks").is_symlink():
            raise ValueError("baseline cache locks must not follow symlinks")
        try:
            for task_id, key in sorted(self.keys.items(), key=lambda item: item[1]):
                lock_path = self.path / ".locks" / f"{key}.lock"
                if lock_path.is_symlink():
                    raise ValueError("baseline cache lock must not follow symlinks")
                lock = self._resources.enter_context(lock_path.open("a"))
                cached = self.path / key
                if cached.is_symlink():
                    raise ValueError("baseline cache cohort must not follow symlinks")
                try:
                    mode = fcntl.LOCK_SH if cached.exists() else fcntl.LOCK_EX
                    fcntl.flock(lock, mode | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError(
                        f"baseline cache cohort is in use: {key}; wait for its owning run to finish"
                    ) from exc
                if cached.exists():
                    self.hits[task_id] = self._validate(task_id, cached)
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        self._resources.close()

    def _validate(self, task_id: str, cached: Path) -> dict[str, Any]:
        if cached.is_symlink() or not cached.is_dir():
            raise ValueError("baseline cache cohort must be a directory")
        manifest: dict[str, Any] = json.loads((cached / "manifest.json").read_text())
        if (
            manifest["context"] != self.contexts[task_id]
            or manifest["inventory"] != _inventory(cached)
            or manifest["origin_status"] != "complete"
        ):
            raise ValueError(f"baseline cache cohort changed: {cached.name}")
        records = manifest["records"]
        for record in records:
            AttemptInput.model_validate(record)
            for identifier in [record["id"], *(stage["id"] for stage in record["stages"])]:
                if Path(identifier).name != identifier or identifier in {".", ".."}:
                    raise ValueError(
                        "baseline cache artifact identity must be a single path component"
                    )
        if sorted(record["attempt"] for record in records) != manifest["context"]["repeats"] or any(
            record["task_id"] != task_id
            or record["arm"] != "baseline"
            or not _eligible(
                attempt=cached / "attempts" / record["id"],
                record=record,
                coding=self.contexts[task_id]["coding"],
                task=self.tasks[task_id],
            )
            for record in records
        ):
            raise ValueError(
                "baseline cache cohort lacks complete isolated native baseline evidence"
            )
        return manifest

    def decisions(self) -> dict[str, dict[str, Any]]:
        decisions = {}
        for entry in self.entries:
            task_id = entry["task_id"]
            hit = self.hits.get(task_id)
            old = (
                next((row for row in hit["records"] if row["attempt"] == entry["attempt"]), None)
                if hit
                else None
            )
            decisions[entry["id"]] = {
                "status": "hit" if hit else "miss",
                "cache_key": self.keys[task_id],
                "origin_run": hit["origin_run"] if hit else None,
                "origin_attempt": old["id"] if old else None,
            }
        return decisions

    def import_attempt(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        if entry["arm"] != "baseline":
            return None
        manifest = self.hits.get(entry["task_id"])
        if manifest is None:
            return None
        record: dict[str, Any] = copy.deepcopy(
            next(row for row in manifest["records"] if row["attempt"] == entry["attempt"])
        )
        old_id = record["id"]
        key = self.keys[entry["task_id"]]
        cached = self.path / key
        attempt = self.root / "attempts" / entry["id"]
        attempt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cached / "attempts" / old_id, attempt, symlinks=True)
        prefix = f"attempts/{old_id}/"
        if _inventory(attempt) != {
            name.removeprefix(prefix): metadata
            for name, metadata in manifest["inventory"].items()
            if name.startswith(prefix)
        }:
            raise ValueError("baseline cache artifacts changed while importing")
        origin = self.root / "baseline-evidence" / entry["id"]
        origin.mkdir(parents=True)
        shutil.copyfile(cached / "native-starts" / f"{old_id}.json", origin / "native-starts.json")
        # Retain observer evidence, but only the current verifier/collectors may grade this run.
        for path in [
            attempt / "before-evidence",
            *[
                attempt / stage["id"] / name
                for stage in record["stages"]
                for name in ("correctness.json", "verifier-result.json", "evidence")
            ],
        ]:
            if path.exists():
                target = origin / "observers" / path.relative_to(attempt)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
        native_artifacts = [
            path
            for stage in record["stages"]
            for path in (attempt / stage["id"] / "artifacts").rglob("*")
            if path.is_file()
        ]
        raw_archives = list(attempt.glob("*/unsupported-submission.tar"))
        temporary = origin / "origin.tmp"
        historical = [path for path in origin.rglob("*") if path.is_file()]
        temporary.write_text(
            json.dumps(
                {
                    **self.decisions()[entry["id"]],
                    "context": manifest["context"],
                    "original_record": record,
                    "original_task_digest": manifest["task_digest"],
                    "historical_evidence": [
                        {
                            "path": path.relative_to(self.root).as_posix(),
                            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                        for path in sorted([*historical, *native_artifacts, *raw_archives])
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        temporary.replace(origin / "origin.json")
        record.update(id=entry["id"], before=None, before_evidence=None)
        for stage in record["stages"]:
            if stage["snapshot"]:
                stage["snapshot"]["path"] = f"attempts/{entry['id']}/{stage['id']}/snapshot"
            stage.update(correctness=None, evidence=None)
        return record

    def publish(self, records: list[dict[str, Any]], tasks: tuple[TaskSpec, ...]) -> None:
        task_digests = {task.task_id: task.digest for task in tasks}
        for task_id, key in self.keys.items():
            if task_id in self.hits:
                continue
            cohort = [
                record
                for record in records
                if record["arm"] == "baseline" and record["task_id"] == task_id
            ]
            if not all(
                _eligible(
                    attempt=self.root / "attempts" / record["id"],
                    record=record,
                    coding=self.contexts[task_id]["coding"],
                    task=self.tasks[task_id],
                )
                for record in cohort
            ):
                continue
            with tempfile.TemporaryDirectory(prefix=f".{key}-", dir=self.path) as temporary:
                staged = Path(temporary) / "cohort"
                (staged / "attempts").mkdir(parents=True)
                (staged / "native-starts").mkdir()
                for record in cohort:
                    _copy_attempt(
                        self.root / "attempts" / record["id"], staged / "attempts" / record["id"]
                    )
                    receipts = {
                        stage["id"]: json.loads(
                            (
                                self.root / "invocations" / record["id"] / f"{stage['id']}.json"
                            ).read_text()
                        )
                        for stage in record["stages"]
                    }
                    (staged / "native-starts" / f"{record['id']}.json").write_text(
                        json.dumps(receipts, indent=2, sort_keys=True) + "\n"
                    )
                manifest = {
                    "context": self.contexts[task_id],
                    "records": cohort,
                    "origin_run": str(self.root),
                    "origin_status": "complete",
                    "task_digest": task_digests[task_id],
                    "inventory": _inventory(staged),
                }
                (staged / "manifest.json").write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                )
                self._validate(task_id, staged)
                staged.rename(self.path / key)
