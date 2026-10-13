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
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from pluginbench import codex, harbor_backend
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.assessment_models import AttemptInput
from pluginbench.datasets import TaskSpec


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


def _immutable_image(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    image = {}
    for key in ("image", "image_id", "platform", "workdir"):
        field = value.get(key)
        if not isinstance(field, str) or not field:
            return None
        image[key] = field
    return image


def _immutable_toolchain(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    image_id = value.get("image_id")
    if (
        not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
        or not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("platform", "harness", "harness_version", "install_policy")
        )
        or _immutable_image(value.get("base_image_identity")) is None
    ):
        return None
    return value


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


def _matches_settings(
    attempt: Path,
    record: dict[str, Any],
    coding: object,
    *,
    image: dict[str, str] | None = None,
    toolchain: dict[str, object] | None = None,
) -> bool:
    if not isinstance(coding, dict):
        return False
    settings = coding.get("study")
    if not isinstance(settings, dict) or not all(
        isinstance(settings.get(key), str) and settings[key]
        for key in ("harness", "model", "reasoning")
    ):
        return False
    harness = settings["harness"] + "-cli"
    version = coding.get("harness_version")
    if (
        not isinstance(version, str)
        or not version
        or image is None
        or toolchain is None
        or toolchain.get("harness") != settings["harness"]
        or toolchain.get("harness_version") != version
    ):
        return False
    environment = coding.get("task_environment")
    build = environment.get("build_timeout_seconds") if isinstance(environment, dict) else None
    agent = coding.get("timeout_seconds")
    if (
        type(agent) is not int
        or agent <= 0
        or not isinstance(build, str)
        or not build.isdecimal()
        or int(build) <= 0
    ):
        return False
    budgets = {"timeout_seconds": agent, "build_timeout_seconds": int(build)}
    runtime = record.get("runtime") or {}
    observations = [
        (record.get("model"), settings["model"]),
        (runtime.get("harness"), harness),
        (runtime.get("reasoning_effort"), settings["reasoning"]),
    ]
    if isinstance(version, str) and version:
        observations.append((runtime.get("harness_version"), version))
    for stage in record["stages"]:
        execution = json.loads((attempt / stage["id"] / "execution.json").read_text())
        invocation_path = attempt / stage["id"] / "artifacts/invocation.json"
        if not invocation_path.is_file():
            return False
        invocation = json.loads(invocation_path.read_text())
        if (
            not isinstance(invocation, dict)
            or _immutable_image(invocation.get("image_identity")) != image
            or _immutable_toolchain(invocation.get("toolchain_identity")) != toolchain
            or any(
                type(invocation.get(key)) is not int or invocation[key] != expected
                for key, expected in budgets.items()
            )
            or any(
                invocation.get(key) != expected
                for key, expected in (
                    ("backend", "harbor-docker-v1"),
                    ("harness", harness),
                    ("harness_version", version),
                    ("requested_model", settings["model"]),
                    ("requested_effort", settings["reasoning"]),
                )
            )
        ):
            return False
        if isinstance(version, str) and version:
            observations.append((execution.get("launch_harness_version"), version))
            startup = execution.get("startup")
            if isinstance(startup, list):
                observations.extend(
                    (codex.observed_cli_version([event], host=settings["harness"]), version)
                    for event in startup
                )
        for key, expected in (
            ("actual_primary_models", settings["model"]),
            ("runtime_effort_evidence", settings["reasoning"]),
        ):
            actual = execution.get(key)
            if isinstance(actual, list):
                observations.extend((value, expected) for value in actual)
        launch = execution.get("launch")
        if isinstance(launch, dict):
            if isinstance(version, str) and version:
                observations.append((launch.get("harness_version"), version))
            observations.extend(
                (launch.get(key), expected)
                for key, expected in (
                    ("harness", harness),
                    ("requested_model", settings["model"]),
                    ("requested_effort", settings["reasoning"]),
                )
            )
    return all(
        not isinstance(actual, str) or not actual or actual == expected
        for actual, expected in observations
    )


def _matches_harbor_runtime(
    attempt: Path,
    record: dict[str, Any],
    expected: dict[str, Any],
    *,
    image: dict[str, str],
    toolchain: dict[str, object],
) -> bool:
    engine = expected.get("docker_engine")
    resources = expected.get("resources")
    if (
        type(expected.get("submission_receipt_version")) is not int
        or expected["submission_receipt_version"] != 2
        or not isinstance(expected.get("harbor_version"), str)
        or not expected["harbor_version"]
        or not isinstance(expected.get("implementation_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected["implementation_sha256"])
        or not isinstance(engine, dict)
        or not all(
            isinstance(engine.get(key), str) and engine[key]
            for key in ("id", "version", "platform", "endpoint")
        )
        or not isinstance(resources, dict)
        or not all(
            type(resources.get(key)) is int and resources[key] > 0 for key in ("cpus", "memory_mb")
        )
    ):
        return False
    for stage in record["stages"]:
        artifacts = attempt / stage["id"] / "artifacts"
        if not _matches_docker_client(artifacts, expected):
            return False
        receipt = artifacts / "runtime-isolation.json"
        if not receipt.is_file():
            return False
        actual = json.loads(receipt.read_text())
        export = actual.get("export", {})
        cleanup = actual.get("cleanup", {})
        if (
            actual.get("container") != expected
            or not isinstance(export, dict)
            or export.get("status") != "complete"
            or export.get("stopped") is not True
            or export.get("resources") != {key: resources[key] for key in ("cpus", "memory_mb")}
            or not isinstance(export.get("derived_image_id"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", export["derived_image_id"])
            or _immutable_image(export.get("base_image_identity")) != image
            or _immutable_toolchain(export.get("toolchain_identity")) != toolchain
            or (
                stage.get("snapshot") is not None
                and export.get("snapshot_digest") != stage["snapshot"]["sha256"]
            )
            or not isinstance(cleanup, dict)
            or cleanup.get("status") != "complete"
            or cleanup.get("residuals") != {"containers": [], "networks": [], "images": []}
        ):
            return False
        try:
            proof = json.loads((artifacts / "container-state.json").read_text())
            if not isinstance(proof, dict):
                return False
            harbor_backend.stopped_container_state(proof.get("state"))
            worker = json.loads((artifacts / "worker.json").read_text())
        except (OSError, ValueError):
            return False
        owner, container_id = proof.get("owner"), proof.get("container_id")
        if (
            proof.get("resources") != export["resources"]
            or proof.get("derived_image_id") != export["derived_image_id"]
            or not isinstance(owner, str)
            or re.fullmatch(r"[0-9a-f]{32}", owner) is None
            or not isinstance(container_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or not isinstance(worker, dict)
            or worker.get("session_id") != f"pluginbench-{owner}__env"
        ):
            return False
    return True


def _eligible(attempt: Path, record: dict[str, Any], coding: object) -> bool:
    expected = coding.get("isolation") if isinstance(coding, dict) else None
    if isinstance(expected, dict) and expected.get("protocol") == "harbor-docker-v1":
        environment = coding.get("task_environment") if isinstance(coding, dict) else None
        image = (
            _immutable_image(environment.get("image_identity"))
            if isinstance(environment, dict)
            else None
        )
        toolchain = (
            _immutable_toolchain(environment.get("toolchain_identity"))
            if isinstance(environment, dict)
            else None
        )
        if (
            image is None
            or toolchain is None
            or _immutable_image(toolchain.get("base_image_identity")) != image
            or toolchain.get("platform") != image["platform"]
        ):
            return False
        return _matches_harbor_runtime(
            attempt, record, expected, image=image, toolchain=toolchain
        ) and _matches_settings(attempt, record, coding, image=image, toolchain=toolchain)
    return False


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
        self.entries = [entry for entry in entries if entry["arm"] == "baseline"]
        self.contexts = {}
        for task in tasks:
            assert task.fixture is not None
            coding = {
                key: value.get(task.task_id)
                if key in {"task_assets", "task_environment"} and isinstance(value, dict)
                else value
                for key, value in identity.items()
            }
            self.contexts[task.task_id] = {
                "protocol": "isolated-baseline-cohort-v1",
                "coding": coding,
                "task_id": task.task_id,
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
                cached / "attempts" / record["id"],
                record,
                self.contexts[task_id]["coding"],
            )
            for record in records
        ):
            coding = self.contexts[task_id]["coding"]
            runtime = coding.get("isolation") if isinstance(coding, dict) else None
            reason = "baseline cache cohort lacks complete isolated baseline evidence"
            if isinstance(runtime, dict) and runtime.get("protocol") == "harbor-docker-v1":
                reason += "; Harbor reuse requires immutable agent toolchain image evidence, not a CLI version alone"
            raise ValueError(reason)
        for record in records:
            for stage in record["stages"]:
                if (
                    stage["snapshot"] is not None
                    and inspect_snapshot(
                        cached / "attempts" / record["id"] / stage["id"] / "snapshot"
                    )["digest"]
                    != stage["snapshot"]["sha256"]
                ):
                    raise ValueError("baseline cache submitted snapshot changed")
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
                    self.root / "attempts" / record["id"],
                    record,
                    self.contexts[task_id]["coding"],
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
