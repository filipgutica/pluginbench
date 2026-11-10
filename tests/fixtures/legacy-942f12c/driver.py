"""Run inside an export of commit 942f12c: write the saved state that version produced.

`generate.py` starts this file with `python -I`, so only the exported tree is importable.
Docker is stubbed; every task, prompt, patch and trace is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

EXPORT, OUTPUT, WORK = (Path(argument) for argument in sys.argv[1:4])
sys.path[:0] = [str(EXPORT / "src"), str(EXPORT / "tests")]

import native_support  # noqa: E402
import test_baselines  # noqa: E402
import test_native_harbor  # noqa: E402
from pluginbench import (  # noqa: E402
    baselines,
    collectors,
    evaluation,
    harbor_backend,
    language_analysis,
    submissions,
)
from pluginbench.assessment_metrics import inspect_snapshot  # noqa: E402
from pluginbench.datasets import native_identity  # noqa: E402

# Synthetic Docker, reported as 942f12c's `docker info` parsing expects it.
ENGINE = {
    "ID": "fixture-engine-at-preparation",
    "ServerVersion": "29.0.0",
    "OSType": "linux",
    "Architecture": "x86_64",
    "NCPU": 24,
    "MemTotal": 25198305280,
}


def _executable(name: str, digest: str) -> dict[str, str]:
    path = "/fixture/bin/" + name
    return {"path": path, "resolved_path": path, "sha256": digest * 64, "version": "29.0.0"}


harbor_backend._engine = lambda: (ENGINE, "unix:///var/run/docker.sock")  # type: ignore[assignment]
harbor_backend._docker_cli = lambda info: _executable("docker", "1")  # type: ignore[assignment]
harbor_backend._docker_plugins = lambda info: {  # type: ignore[assignment]
    "compose": _executable("docker-compose", "2"),
    "buildx": _executable("docker-buildx", "3"),
}
harbor_backend._assert_codex_transport_source = lambda: harbor_backend._CODEX_RUN_SHA256  # type: ignore[assignment]


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def vanilla() -> None:
    """A two-repeat vanilla cohort, published and then admitted by 942f12c's own cache."""
    legacy = harbor_backend.runtime_identity(cpus=1, memory_mb=2048)
    native_support.runtime_identity = lambda: legacy  # type: ignore[assignment]
    test_baselines.runtime_identity = lambda: legacy  # type: ignore[assignment]
    submissions.reviewed_contract = native_support.synthetic_contract  # type: ignore[assignment]
    synthetic_coding = test_baselines._coding

    def coding(tasks: Any) -> dict[str, object]:
        # The shape evaluation._vanilla_identity saved at 942f12c, with real launcher hashes.
        settings = {
            "harness": "codex",
            "model": "gpt-6.1-sol",
            "reasoning": "medium",
            "jobs": 2,
            "timeout_seconds": 1800,
            "seed": 42,
        }
        return {
            **synthetic_coding(tasks),
            "task_assets": {
                task.task_id: native_identity(task)["original_task_assets_sha256"] for task in tasks
            },
            "runner": {"harness": "codex", "model": "gpt-6.1-sol", "reasoning": "medium"},
            "jobs": 2,
            "seed": 42,
            "launcher_sha256": evaluation._launcher_sha256("codex"),
            "study": settings,
        }

    test_baselines._coding = coding  # type: ignore[assignment]
    synthetic_execution = test_baselines.native_execution

    synthetic_entry = test_baselines.task_entry

    def task_entry(root: Path, name: str, **kwargs: Any) -> dict[str, Any]:
        # The tasks' own paths would be this machine's; admission reads the catalog's instead.
        entry = synthetic_entry(root, name, **kwargs)
        identity = json.loads(entry["provenance"]["native_task_identity"])
        identity.update(task_dir="/fixture/native/" + name, fixture_dir="/fixture/fixtures/" + name)
        entry["provenance"]["native_task_identity"] = json.dumps(identity)
        return entry

    test_baselines.task_entry = task_entry  # type: ignore[assignment]

    def execution(area: Path, identity: dict[str, Any], **kwargs: Any) -> object:
        if not (area / "before").exists():
            shutil.copytree(area.parent / "config/fixtures/answer", area / "before")
        result = synthetic_execution(area, identity, **kwargs)
        # Real export receipts hold only the two enforced limits, not the whole resources.
        resources = identity["isolation"]["resources"]
        limits = {key: resources[key] for key in ("cpus", "memory_mb")}
        for name in ("submission-source.json", "before-source.json", "runtime-isolation.json"):
            path = area / "artifacts" / name
            receipt = json.loads(path.read_text())
            (receipt["export"] if "export" in receipt else receipt)["resources"] = limits
            path.write_text(json.dumps(receipt))
        return result

    test_baselines.native_execution = execution  # type: ignore[assignment]
    cache_path = WORK / "vanilla"
    first, tasks, records = test_baselines._cohort(WORK / "published", cache_path)
    test_baselines._publish(first, records)
    for task_id, context in first.contexts.items():
        cohort = cache_path / baselines._hash(context)
        assert cohort.is_dir(), f"942f12c did not publish {task_id}; its evidence is not eligible"
        assert sorted(path.name for path in cohort.iterdir()) == ["1", "2"]
    # 942f12c must admit what it published, through its own eligibility checks.
    second, _, entries = test_baselines._cohort(WORK / "admitted", cache_path)
    try:
        second.reserve()
        assert all(row["status"] == "hit" for row in second.decisions().values())
    finally:
        second.close()
    # 942f12c's study loader needs the recipe and guidance to publish; admission reads neither.
    shutil.copytree(
        WORK / "published/config",
        OUTPUT / "vanilla-task",
        ignore=shutil.ignore_patterns("recipe.yaml", "guidance.md"),
    )
    shutil.copytree(cache_path, OUTPUT / "vanilla", ignore=shutil.ignore_patterns(".locks"))
    write(OUTPUT / "vanilla-identity.json", coding(tasks))


def prepared() -> None:
    """A prepared-image cache entry and its reference-agent result, reused once by 942f12c."""
    task = test_native_harbor.native_task(Path("prepared-input"))
    image = {
        "image": "sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64,
        "platform": "linux/amd64",
        "workdir": "/app",
    }
    harbor_backend._native_image = lambda **kwargs: image  # type: ignore[assignment]
    harbor_backend._inspect_image = lambda reference, **kwargs: {"Id": reference}  # type: ignore[assignment]
    preparations: list[Path] = []

    def prepare(*, task_dir: Path, destination: Path, resolved: Any, **kwargs: Any) -> object:
        preparations.append(destination)
        (destination / "artifacts").mkdir(parents=True)
        frozen = destination / "task"
        shutil.copytree(task_dir, frozen)
        fixture = destination / "fixture"
        fixture.mkdir()
        (fixture / "code.py").write_text('starting = "source"\n')
        receipt = {
            "status": "complete",
            "cleanup": {"status": "complete", "project": "fixture"},
            # Placeholders: a cache hit replaces both with the run's own copy.
            "task_dir": "/prepared/task",
            "fixture_dir": "/prepared/fixture",
            "isolation": resolved.runtime,
            "source_digest": inspect_snapshot(fixture)["digest"],
            "task_assets_sha256": harbor_backend.native_task_assets(frozen),
            "image_identity": image,
            "toolchain_identity": {"image_id": "sha256:" + "b" * 64, "harness": "codex"},
            "grader_image_identity": image,
        }
        write(destination / "artifacts/native-preparation.json", receipt)
        return receipt

    harbor_backend._prepare_native_task = prepare  # type: ignore[assignment]
    limits = {"cpus": 1, "memory_mb": 256, "setup_timeout": 600}

    def run(destination: Path) -> dict[str, object]:
        return harbor_backend.prepare_native_task(
            task_dir=task,
            destination=destination,
            harness="codex",
            version="0.208.0",
            limits=limits,
            cache=Path("prepared"),
        )

    first = run(Path("prepared-first"))
    second = run(Path("prepared-second"))
    assert len(preparations) == 1 and first["preparation_key"] == second["preparation_key"]
    key = str(first["preparation_key"])
    packet = json.loads((Path("prepared") / key / "cache.json").read_text())
    assert key == hashlib.sha256(json.dumps(packet["inputs"], sort_keys=True).encode()).hexdigest()
    shutil.copytree(task, OUTPUT / "prepared-task")
    shutil.copytree(Path("prepared") / key, OUTPUT / "prepared" / key)
    harbor_backend.run_reference_agents = native_support.passing_reference_agents  # type: ignore[assignment]
    evaluation._reference_agents(first, Path("reference-run"), Path("reference-agents"))
    shutil.copytree(Path("reference-agents"), OUTPUT / "reference-agents")
    write(OUTPUT / "prepared-limits.json", limits)


def observers() -> None:
    """The observer identities 942f12c froze into measurement profiles."""
    source = Path(language_analysis.__file__).read_bytes()
    write(
        OUTPUT / "observer-identities.json",
        {
            "analysis_implementation_sha256": hashlib.sha256(source).hexdigest(),
            "coverage_implementation": collectors.observer_identity(),
        },
    )


def runtime() -> None:
    write(OUTPUT / "runtime.json", harbor_backend.runtime_identity(cpus=2, memory_mb=8192))


if __name__ == "__main__":
    os.umask(0o022)  # Vanilla manifests record file modes; keep them the same on every machine.
    os.chdir(WORK)
    runtime()
    vanilla()
    prepared()
    observers()
