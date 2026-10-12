from __future__ import annotations

import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
from typing import Any

import pytest
import yaml

from pluginbench import baselines, codex, isolation, study
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.datasets import TaskSpec


def _study(directory: Path, *, guidance: str = "candidate") -> study.ResolvedStudy:
    directory.mkdir()
    (directory / "fixture").mkdir()
    (directory / "fixture/answer.py").write_text("answer = 0\n")
    (directory / "verify.py").write_text(
        "from pathlib import Path\nimport json\n"
        "print(json.dumps({'pass': Path('answer.py').read_text() == 'answer = 1\\n'}))\n"
    )
    (directory / "tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "answer",
                        "prompt": "Set the answer to one.",
                        "fixture": "fixture",
                        "verifier": {"command": [sys.executable], "script": "verify.py"},
                    }
                ],
            }
        )
    )
    (directory / "guidance.md").write_text(f"Follow {guidance}.\n")
    recipe = directory / "recipe.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": guidance,
                "tasks": "tasks.yaml",
                "repeats": 2,
                "guidance": {guidance: ["guidance.md"]},
            }
        )
    )
    return study.load_study(recipe)


def _isolation_identity() -> dict[str, object]:
    return {
        "protocol": "local-filesystem-v1",
        "implementation_sha256": "a" * 64,
        "backend": "/usr/bin/sandbox-exec",
        "backend_sha256": "b" * 64,
        "os": "Darwin",
        "os_release": "25.0.0",
        "system_read_roots": ["/System", "/usr/lib"],
        "tool_read_roots": ["/opt/tools"],
        "tools": {"codex": {"executable": "/opt/tools/codex", "binary_sha256": "c" * 64}},
        "filesystem": "deny reads and writes outside selected and owned roots",
        "network": "agent egress allowed",
        "process_limit": "POSIX process group",
    }


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unsupported: bool = False,
    isolated: bool = True,
    filesystem: dict[str, object] | None = None,
    observations: dict[str, object] | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "codex-cli test"})
    monkeypatch.setattr(isolation, "runtime_identity", _isolation_identity)

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        if unsupported and kwargs["entry"]["arm"] == "baseline":
            (area / "workspace/escape").symlink_to("/tmp/shared")
        artifacts = area / "artifacts"
        artifacts.mkdir()
        (artifacts / "runtime-isolation.json").write_text(
            json.dumps(
                {
                    "filesystem": {
                        **_isolation_identity(),
                        "protocol": "local-filesystem-v1" if isolated else "old",
                        "readable": [str(area / "workspace")],
                        "writable": [str(area / "workspace")],
                        "policy_sha256": str(area),
                        **(filesystem or {}),
                    },
                }
            )
        )
        return {
            "successful_native_completion": not (
                kwargs["entry"]["arm"] == "baseline" and kwargs["entry"]["attempt"] == 1
            ),
            "usage": None if kwargs["entry"]["attempt"] == 1 else {"input_tokens": 13},
            "elapsed_seconds": 7.0,
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
            **(observations or {}),
        }

    monkeypatch.setattr(codex, "invoke", invoke)
    return calls


def _run(
    config: study.ResolvedStudy,
    output: Path,
    cache: Path,
    identity: dict[str, object] | None = None,
) -> Path:
    return study.run_study(
        config,
        output,
        baseline_cache=cache,
        baseline_identity={
            "runtime": "isolated-test",
            "isolation": _isolation_identity(),
            **(identity or {}),
        },
    )


def test_reuse_keeps_failed_attempts_unknown_work_and_regrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    grades: list[Path] = []
    original_grade = study._grade

    def grade(snapshot: Path, task: TaskSpec, directory: Path) -> dict[str, Any]:
        grades.append(snapshot)
        return original_grade(snapshot, task, directory)

    monkeypatch.setattr(study, "_grade", grade)
    first = _run(_study(tmp_path / "one"), tmp_path / "first", tmp_path / "cache")
    second = _run(
        _study(tmp_path / "two", guidance="another"), tmp_path / "second", tmp_path / "cache"
    )
    assert len(calls) == 6
    assert len(grades) == 8
    run = json.loads((second / "run.json").read_text())
    assert run["actual_calls"] == 2
    assert len(run["baseline_origin"]) == 2
    records = json.loads((second / "assessment-input.json").read_text())["records"]
    baseline = sorted(
        (row for row in records if row["arm"] == "baseline"), key=lambda row: row["attempt"]
    )
    assert baseline[0]["stages"][0]["work"]["tokens"] is None
    assert baseline[1]["stages"][0]["work"]["tokens"] == {"input_tokens": 13}
    assert all(row["stages"][0]["work"]["elapsed_seconds"] == 7.0 for row in baseline)
    correctness = second / baseline[0]["stages"][0]["correctness"]["path"]
    assert json.loads(correctness.read_text())["checks"]["native_completion"] is False
    assert all(item["origin_run"] == str(first) for item in run["baseline_origin"].values())
    assert not any(path.is_symlink() for path in second.rglob("*"))


def test_corrupt_cache_is_rejected_before_any_new_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    next(cache.glob("*/attempts/*/handoff/snapshot/answer.py")).write_text("tampered\n")
    with pytest.raises(ValueError, match="baseline cache"):
        _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 4


def test_imported_report_is_self_contained_and_binds_historical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    cache = tmp_path / "cache"
    first = _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    second = _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    shutil.rmtree(first)
    shutil.rmtree(cache)
    study.report_study(second)
    next(second.glob("baseline-evidence/*/native-starts.json")).write_text("{}")
    with pytest.raises(ValueError, match="run artifact changed"):
        study.report_study(second)
    assert len(calls) == 6


@pytest.mark.parametrize("change", ["identity", "prompt", "fixture", "reasoning", "repeats"])
def test_changed_coding_context_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    calls = _runtime(monkeypatch)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    config = _study(tmp_path / "two")
    identity: dict[str, object] = {"runtime": "isolated-test"}
    if change == "identity":
        identity["runtime"] = "different"
    elif change == "prompt":
        from dataclasses import replace

        config = replace(config, tasks=(replace(config.tasks[0], prompt="Different task."),))
    elif change == "fixture":
        assert isinstance(config.tasks[0], TaskSpec)
        assert config.tasks[0].fixture is not None
        (config.tasks[0].fixture / "answer.py").write_text("answer = -1\n")
    elif change == "reasoning":
        config.recipe.reasoning = "high"
    else:
        config.recipe.repeats = 1
    _run(config, tmp_path / "second", cache, identity)
    assert len(calls) == (6 if change == "repeats" else 8)


def test_unsupported_submissions_are_inert_and_stay_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch, unsupported=True)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    second = _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 6
    records = json.loads((second / "assessment-input.json").read_text())["records"]
    baseline = [row for row in records if row["arm"] == "baseline"]
    assert all(row["stages"][0]["snapshot"] is None for row in baseline)
    assert len(list(second.glob("attempts/*/handoff/unsupported-submission.tar"))) == 2
    assert not any(path.is_symlink() for path in cache.rglob("*"))
    assert not any(path.is_symlink() for path in second.rglob("*"))


def test_missing_actual_isolation_receipt_never_seeds_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch, isolated=False)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 8


@pytest.mark.parametrize(
    "field,value",
    [
        ("backend", "/mismatched/runtime"),
        ("implementation_sha256", "d" * 64),
        ("tools", {"codex": {"executable": "/other/codex", "binary_sha256": "e" * 64}}),
        ("system_read_roots", ["/tmp"]),
        ("filesystem", "unrestricted"),
    ],
)
def test_actual_runtime_identity_mismatch_never_seeds_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    calls = _runtime(monkeypatch, filesystem={field: value})
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    assert not list(cache.glob("*/manifest.json"))
    assert len(calls) == 4


def test_cached_actual_runtime_mismatch_is_rejected_before_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    manifest_path = next(cache.glob("*/manifest.json"))
    receipt_path = next(
        manifest_path.parent.glob("attempts/*/handoff/artifacts/runtime-isolation.json")
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["filesystem"]["backend"] = "/mismatched/runtime"
    receipt_path.write_text(json.dumps(receipt))
    # Reproduce a cohort published with matching byte fingerprints but the wrong runtime label.
    manifest = json.loads(manifest_path.read_text())
    manifest["inventory"] = baselines._inventory(manifest_path.parent)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="complete isolated baseline evidence"):
        _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 4


@pytest.mark.parametrize(
    "observations",
    [
        {"actual_primary_models": ["different-observed-model"]},
        {"runtime_effort_evidence": ["high"]},
        {"launch": {"harness": "claude-cli"}},
        {"actual_primary_models": ["gpt-6.1-sol", "different-observed-model"]},
        {"runtime_effort_evidence": ["medium", "high"]},
    ],
)
def test_known_native_condition_contradiction_never_seeds_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observations: dict[str, object],
) -> None:
    calls = _runtime(monkeypatch, observations=observations)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    assert not list(cache.glob("*/manifest.json"))
    assert len(calls) == 4


@pytest.mark.parametrize("contradiction", ["model", "effort", "harness", "stage_model"])
def test_cached_known_condition_contradiction_rejects_before_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contradiction: str,
) -> None:
    calls = _runtime(monkeypatch)
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    manifest_path = next(cache.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    record = manifest["records"][0]
    if contradiction == "model":
        record["model"] = "different-observed-model"
    elif contradiction == "effort":
        record["runtime"]["reasoning_effort"] = "high"
    elif contradiction == "harness":
        record["runtime"]["harness"] = "claude-cli"
    else:
        execution_path = manifest_path.parent / "attempts" / record["id"] / "handoff/execution.json"
        execution = json.loads(execution_path.read_text())
        execution["actual_primary_models"] = ["different-observed-model"]
        execution_path.write_text(json.dumps(execution))
        manifest["inventory"] = baselines._inventory(manifest_path.parent)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="complete isolated baseline evidence"):
        _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 4


def test_unknown_native_observations_remain_reusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(
        monkeypatch,
        observations={
            "actual_primary_models": [],
            "runtime_effort_evidence": [],
        },
    )
    cache = tmp_path / "cache"
    _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    second = _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 6
    records = json.loads((second / "assessment-input.json").read_text())["records"]
    assert all(
        record["model"] is None and record["runtime"]["reasoning_effort"] is None
        for record in records
    )


def test_adding_tasks_preserves_existing_key_and_maps_new_plan_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    cache = tmp_path / "cache"
    identity: dict[str, object] = {
        "task_assets": {"answer": "fixed", "other": "unused"},
        "task_environment": {"answer": {"node": "22"}, "other": {"node": "20"}},
    }
    first = _run(_study(tmp_path / "one"), tmp_path / "first", cache, identity)
    config = _study(tmp_path / "two")
    catalog = yaml.safe_load(config.recipe.tasks.read_text())
    catalog["tasks"].append({**catalog["tasks"][0], "id": "other", "prompt": "Other answer."})
    config.recipe.tasks.write_text(yaml.safe_dump(catalog))
    config = study.load_study(config.source)
    identity["task_assets"] = {"answer": "fixed", "other": "new"}
    identity["task_environment"] = {"answer": {"node": "22"}, "other": {"node": "24"}}
    second = _run(config, tmp_path / "second", cache, identity)
    assert len(calls) == 10
    run = json.loads((second / "run.json").read_text())
    assert run["actual_calls"] == 6
    assert len(run["baseline_origin"]) == 2
    assert any(
        new_id != origin["origin_attempt"] for new_id, origin in run["baseline_origin"].items()
    )
    assert all(origin["origin_run"] == str(first) for origin in run["baseline_origin"].values())


def test_active_missing_cohort_rejects_a_second_run_before_duplicate_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    invoke = codex.invoke
    entered = Event()
    release = Event()

    def blocked(**kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return invoke(**kwargs)

    monkeypatch.setattr(codex, "invoke", blocked)
    first = _study(tmp_path / "one")
    second = _study(tmp_path / "two")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_run, first, tmp_path / "first", tmp_path / "cache")
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match="baseline cache cohort is in use"):
                _run(second, tmp_path / "second", tmp_path / "cache")
            assert not calls
            assert not (tmp_path / "second/invocations").exists()
        finally:
            release.set()
        pending.result(timeout=10)
    assert len(calls) == 4


def test_interrupted_run_never_publishes_an_incomplete_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _runtime(monkeypatch)
    invoke = codex.invoke
    cache = tmp_path / "cache"

    def interrupt(**kwargs: Any) -> dict[str, Any]:
        raise KeyboardInterrupt("test interruption")

    monkeypatch.setattr(codex, "invoke", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run(_study(tmp_path / "one"), tmp_path / "first", cache)
    assert not list(cache.glob("*/manifest.json"))
    monkeypatch.setattr(codex, "invoke", invoke)
    _run(_study(tmp_path / "two"), tmp_path / "second", cache)
    assert len(calls) == 4


def _harbor_identity() -> dict[str, object]:
    return {
        "isolation": {
            "protocol": "harbor-docker-v1",
            "harbor_version": "0.24.0",
            "implementation_sha256": "a" * 64,
            "docker_cli": {
                "path": "/tools/bin/docker",
                "resolved_path": "/tools/docker/docker",
                "sha256": "1" * 64,
                "version": "29.5.3",
            },
            "docker_plugins": {
                "compose": {
                    "path": "/tools/cli-plugins/docker-compose",
                    "resolved_path": "/tools/plugins/docker-compose",
                    "sha256": "2" * 64,
                    "version": "v2.38.0",
                },
                "buildx": {
                    "path": "/tools/cli-plugins/docker-buildx",
                    "resolved_path": "/tools/plugins/docker-buildx",
                    "sha256": "3" * 64,
                    "version": "v0.30.1",
                },
            },
            "docker_engine": {
                "id": "daemon-one",
                "version": "29.5.3",
                "platform": "linux/arm64",
                "endpoint": "unix:///var/run/docker.sock",
            },
            "resources": {"cpus": 1, "memory_mb": 1024},
            "submission_receipt_version": 2,
        },
        "harness_version": "0.143.0",
        "timeout_seconds": 1800,
        "study": {"harness": "codex", "model": "gpt-6.1-sol", "reasoning": "medium"},
        "task_environment": {
            "answer": {
                "image_identity": {
                    "image": "registry.example/fixture@sha256:" + "b" * 64,
                    "image_id": "sha256:" + "c" * 64,
                    "platform": "linux/arm64",
                    "workdir": "/app",
                    "original_image": "registry.example/fixture:source-provenance",
                },
                "toolchain_identity": {
                    "image_id": "sha256:" + "e" * 64,
                    "platform": "linux/arm64",
                    "harness": "codex",
                    "harness_version": "0.143.0",
                    "install_policy": "base-runtime-frozen-support-exact-npm-cli-v1",
                    "base_image_identity": {
                        "image": "registry.example/fixture@sha256:" + "b" * 64,
                        "image_id": "sha256:" + "c" * 64,
                        "platform": "linux/arm64",
                        "workdir": "/app",
                    },
                },
                "cli_version": "0.143.0",
                "cpus": 1,
                "memory_mb": 1024,
                "build_timeout_seconds": "1800",
            }
        },
    }


def _harbor_cohort(
    directory: Path, cache_path: Path, identity: dict[str, object] | None = None
) -> tuple[baselines.BaselineCache, tuple[TaskSpec, ...], list[dict[str, Any]]]:
    directory.mkdir()
    config = _study(directory / "config")
    tasks = tuple(task for task in config.tasks if isinstance(task, TaskSpec))
    root = directory / "run"
    identity = identity or _harbor_identity()
    runtime = identity["isolation"]
    assert isinstance(runtime, dict)
    resources = runtime.get("resources", {"cpus": 1, "memory_mb": 1024})
    assert isinstance(resources, dict)
    environments = identity["task_environment"]
    assert isinstance(environments, dict)
    environment = environments["answer"]
    assert isinstance(environment, dict)
    image = environment["image_identity"]
    assert isinstance(image, dict)
    toolchain = environment["toolchain_identity"]
    assert isinstance(toolchain, dict)
    records = []
    for repeat in (1, 2):
        identifier = f"baseline-{repeat}"
        target = root / "attempts" / identifier / "handoff"
        snapshot = target / "snapshot"
        snapshot.mkdir(parents=True)
        (snapshot / "answer.py").write_text("answer = 1\n")
        digest = inspect_snapshot(snapshot)["digest"]
        artifacts = target / "artifacts"
        artifacts.mkdir()
        owner = f"{repeat:032x}"
        (artifacts / "worker.json").write_text(
            json.dumps({"session_id": f"pluginbench-{owner}__env"})
        )
        (artifacts / "container-state.json").write_text(
            json.dumps(
                {
                    "container_id": f"{repeat:064x}",
                    "owner": owner,
                    "state": {
                        "Running": False,
                        "Status": "exited",
                        "OOMKilled": False,
                        "ExitCode": 137,
                        "Error": "",
                        "StartedAt": "2026-10-11T19:00:00Z",
                        "FinishedAt": "2026-10-11T19:30:00Z",
                    },
                    "resources": {key: resources[key] for key in ("cpus", "memory_mb")},
                    "derived_image_id": "sha256:" + "f" * 64,
                }
            )
        )
        if "docker_cli" in runtime or "docker_plugins" in runtime:
            plugins = runtime["docker_plugins"]
            assert isinstance(plugins, dict)
            cli = runtime["docker_cli"]
            assert isinstance(cli, dict)
            (artifacts / "docker-client.json").write_text(
                json.dumps(
                    {
                        "docker_cli": {**cli, "path": str(directory / "private/docker-bin/docker")},
                        "docker_plugins": {
                            name: {
                                **binary,
                                "path": str(directory / "private/cli-plugins" / ("docker-" + name)),
                            }
                            for name, binary in plugins.items()
                        },
                    }
                )
            )
        (artifacts / "runtime-isolation.json").write_text(
            json.dumps(
                {
                    "container": identity["isolation"],
                    "export": {
                        "status": "complete",
                        "stopped": True,
                        "snapshot_digest": digest,
                        "resources": {key: resources[key] for key in ("cpus", "memory_mb")},
                        "derived_image_id": "sha256:" + "f" * 64,
                        "base_image_identity": {
                            key: image[key] for key in ("image", "image_id", "platform", "workdir")
                        },
                        "toolchain_identity": toolchain,
                    },
                    "cleanup": {
                        "status": "complete",
                        "residuals": {"containers": [], "networks": [], "images": []},
                    },
                }
            )
        )
        (artifacts / "invocation.json").write_text(
            json.dumps(
                {
                    "backend": "harbor-docker-v1",
                    "harness": "codex-cli",
                    "harness_version": identity["harness_version"],
                    "requested_model": "gpt-6.1-sol",
                    "requested_effort": "medium",
                    "timeout_seconds": identity["timeout_seconds"],
                    "build_timeout_seconds": int(environment["build_timeout_seconds"]),
                    "image_identity": image,
                    "toolchain_identity": toolchain,
                }
            )
        )
        (target / "execution.json").write_text(
            json.dumps(
                {
                    "successful_native_completion": repeat == 2,
                    "actual_primary_models": [],
                    "runtime_effort_evidence": [],
                }
            )
        )
        invocation = root / "invocations" / identifier / "handoff.json"
        invocation.parent.mkdir(parents=True)
        invocation.write_text(json.dumps({"status": "native invocation started"}))
        records.append(
            {
                "id": identifier,
                "task_id": "answer",
                "attempt": repeat,
                "arm": "baseline",
                "model": None,
                "before": None,
                "before_evidence": None,
                "runtime": {"harness": "codex-cli", "reasoning_effort": None},
                "stages": [
                    {
                        "id": "handoff",
                        "snapshot": {"path": str(snapshot.relative_to(root)), "sha256": digest},
                        "correctness": None,
                        "evidence": None,
                        "work": {
                            "tokens": None if repeat == 1 else {"input_tokens": 13},
                            "elapsed_seconds": 7.0,
                        },
                    }
                ],
            }
        )
    cache = baselines.BaselineCache(
        path=cache_path, root=root, tasks=tasks, entries=records, identity=identity
    )
    return cache, tasks, records


@pytest.mark.parametrize("bind_docker", [True, False])
@pytest.mark.parametrize("receipt_version", [None, 2])
def test_harbor_cache_reuses_complete_deliveries_including_failed_unknown_usage(
    tmp_path: Path,
    bind_docker: bool,
    receipt_version: int | None,
) -> None:
    cache_path = tmp_path / "cache"
    identity = _harbor_identity()
    runtime = identity["isolation"]
    assert isinstance(runtime, dict)
    if receipt_version is None:
        del runtime["submission_receipt_version"]
    if not bind_docker:
        del runtime["docker_cli"]
        del runtime["docker_plugins"]
    first, tasks, records = _harbor_cohort(tmp_path / "first", cache_path, identity)
    for record in records:
        artifacts = first.root / "attempts" / record["id"] / "handoff/artifacts"
        path = artifacts / "container-state.json"
        receipt = json.loads(path.read_text())
        receipt["state"]["OOMKilled"] = True
        if receipt_version is None:
            path.unlink()
            (artifacts / "worker.json").unlink()
            invocation_path = artifacts / "invocation.json"
            invocation = json.loads(invocation_path.read_text())
            del invocation["timeout_seconds"]
            del invocation["build_timeout_seconds"]
            invocation_path.write_text(json.dumps(invocation))
        else:
            path.write_text(json.dumps(receipt))
    try:
        first.reserve().publish(records, tasks)
    finally:
        first.close()
    second, _, entries = _harbor_cohort(tmp_path / "second", cache_path, identity)
    shutil.rmtree(second.root)
    second.root.mkdir()
    try:
        second.reserve()
        assert all(row["status"] == "hit" for row in second.decisions().values())
        imported = [second.import_attempt(entry) for entry in entries]
    finally:
        second.close()
    assert imported == records
    failed = imported[0]
    assert failed is not None
    assert failed["stages"][0]["work"]["tokens"] is None
    execution = second.root / "attempts/baseline-1/handoff/execution.json"
    assert json.loads(execution.read_text())["successful_native_completion"] is False


@pytest.mark.parametrize(
    "change",
    ["version", "wrapper", "engine", "image", "toolchain", "resources", "harness", "local-backend"],
)
def test_harbor_changed_execution_identity_misses(tmp_path: Path, change: str) -> None:
    cache_path = tmp_path / "cache"
    first, tasks, records = _harbor_cohort(tmp_path / "first", cache_path)
    try:
        first.reserve().publish(records, tasks)
    finally:
        first.close()
    identity = _harbor_identity()
    runtime = identity["isolation"]
    assert isinstance(runtime, dict)
    environments = identity["task_environment"]
    assert isinstance(environments, dict)
    environment = environments["answer"]
    assert isinstance(environment, dict)
    if change == "version":
        runtime["harbor_version"] = "0.25.0"
    elif change == "wrapper":
        runtime["implementation_sha256"] = "c" * 64
    elif change == "engine":
        runtime["docker_engine"]["id"] = "daemon-two"
    elif change == "image":
        image = environment["image_identity"]
        assert isinstance(image, dict)
        image["image"] = "registry.example/fixture@sha256:" + "d" * 64
        image["image_id"] = "sha256:" + "e" * 64
        toolchain = environment["toolchain_identity"]
        assert isinstance(toolchain, dict)
        toolchain["base_image_identity"] = {
            key: image[key] for key in ("image", "image_id", "platform", "workdir")
        }
    elif change == "toolchain":
        toolchain = environment["toolchain_identity"]
        assert isinstance(toolchain, dict)
        toolchain["image_id"] = "sha256:" + "f" * 64
    elif change == "resources":
        environment["memory_mb"] = 2048
        limits = runtime["resources"]
        assert isinstance(limits, dict)
        limits["memory_mb"] = 2048
    elif change == "harness":
        environment["cli_version"] = "0.144.0"
        identity["harness_version"] = "0.144.0"
        toolchain = environment["toolchain_identity"]
        assert isinstance(toolchain, dict)
        toolchain["harness_version"] = "0.144.0"
    else:
        identity["isolation"] = _isolation_identity()
    second, _, _ = _harbor_cohort(tmp_path / "second", cache_path, identity)
    try:
        second.reserve()
        assert all(row["status"] == "miss" for row in second.decisions().values())
    finally:
        second.close()


@pytest.mark.parametrize(
    "failure",
    [
        "running",
        "digest",
        "residuals",
        "unknown-cleanup",
        "runtime",
        "bare-protocol",
        "resource-limit",
        "derived-image",
        "launch-pin",
        "observed-version",
    ],
)
def test_harbor_incomplete_or_mismatched_delivery_never_seeds_cache(
    tmp_path: Path, failure: str
) -> None:
    cache_path = tmp_path / "cache"
    cache, tasks, records = _harbor_cohort(tmp_path / "first", cache_path)
    receipt_path = cache.root / "attempts/baseline-1/handoff/artifacts/runtime-isolation.json"
    receipt = json.loads(receipt_path.read_text())
    if failure == "running":
        receipt["export"]["stopped"] = False
    elif failure == "digest":
        receipt["export"]["snapshot_digest"] = "sha256:" + "e" * 64
    elif failure == "residuals":
        receipt["cleanup"]["residuals"]["containers"] = ["leftover"]
    elif failure == "unknown-cleanup":
        del receipt["cleanup"]["residuals"]
    elif failure == "runtime":
        receipt["container"]["docker_engine"]["id"] = "wrong-daemon"
    elif failure == "bare-protocol":
        coding = cache.contexts["answer"]["coding"]
        assert isinstance(coding, dict)
        coding["isolation"] = {"protocol": "harbor-docker-v1"}
        receipt["container"] = coding["isolation"]
    elif failure == "resource-limit":
        receipt["export"]["resources"]["memory_mb"] = 2048
    elif failure == "derived-image":
        receipt["export"]["derived_image_id"] = "unresolved-image"
    elif failure == "launch-pin":
        invocation = receipt_path.with_name("invocation.json")
        launch = json.loads(invocation.read_text())
        del launch["harness_version"]
        invocation.write_text(json.dumps(launch))
    else:
        execution_path = receipt_path.parent.parent / "execution.json"
        execution = json.loads(execution_path.read_text())
        execution["startup"] = [
            {"type": "session_meta", "payload": {"cli_version": "0.143.0"}},
            {"type": "session_meta", "payload": {"cli_version": "0.144.0"}},
        ]
        execution_path.write_text(json.dumps(execution))
    receipt_path.write_text(json.dumps(receipt))
    try:
        cache.reserve().publish(records, tasks)
    finally:
        cache.close()
    assert not list(cache_path.glob("*/manifest.json"))


@pytest.mark.parametrize("source", ["invocation", "export"])
@pytest.mark.parametrize("fault", ["foreign-image", "missing-field"])
def test_harbor_actual_image_mismatch_never_seeds_cache(
    tmp_path: Path, source: str, fault: str
) -> None:
    cache_path = tmp_path / "cache"
    cache, tasks, records = _harbor_cohort(tmp_path / "first", cache_path)
    artifacts = cache.root / "attempts/baseline-1/handoff/artifacts"
    receipt_path = artifacts / (
        "invocation.json" if source == "invocation" else "runtime-isolation.json"
    )
    receipt = json.loads(receipt_path.read_text())
    image = (
        receipt["image_identity"]
        if source == "invocation"
        else receipt["export"]["base_image_identity"]
    )
    if fault == "foreign-image":
        image["image_id"] = "sha256:" + "d" * 64
    else:
        del image["workdir"]
    receipt_path.write_text(json.dumps(receipt))
    try:
        cache.reserve().publish(records, tasks)
    finally:
        cache.close()
    assert not list(cache_path.glob("*/manifest.json"))


@pytest.mark.parametrize("source", ["invocation", "export"])
@pytest.mark.parametrize("fault", ["different-content", "version-only"])
def test_harbor_toolchain_content_mismatch_never_seeds_cache(
    tmp_path: Path, source: str, fault: str
) -> None:
    cache_path = tmp_path / "cache"
    cache, tasks, records = _harbor_cohort(tmp_path / "first", cache_path)
    artifacts = cache.root / "attempts/baseline-1/handoff/artifacts"
    receipt_path = artifacts / (
        "invocation.json" if source == "invocation" else "runtime-isolation.json"
    )
    receipt = json.loads(receipt_path.read_text())
    delivery = receipt if source == "invocation" else receipt["export"]
    if fault == "different-content":
        delivery["toolchain_identity"]["image_id"] = "sha256:" + "f" * 64
    else:
        delivery["toolchain_identity"] = {"harness_version": "0.143.0"}
    receipt_path.write_text(json.dumps(receipt))
    try:
        cache.reserve().publish(records, tasks)
    finally:
        cache.close()
    assert not list(cache_path.glob("*/manifest.json"))


@pytest.mark.parametrize(
    "fault",
    [
        "cli-hash",
        "plugin-executable",
        "plugin-version",
        "missing-proof",
        "malformed-json",
        "missing-field",
        "relative-alias",
        "malformed-hash",
    ],
)
def test_harbor_actual_private_docker_client_mismatch_never_seeds_cache(
    tmp_path: Path, fault: str
) -> None:
    cache_path = tmp_path / "cache"
    cache, tasks, records = _harbor_cohort(tmp_path / "first", cache_path)
    receipt_path = cache.root / "attempts/baseline-1/handoff/artifacts/docker-client.json"
    client = json.loads(receipt_path.read_text())
    if fault == "missing-proof":
        receipt_path.unlink()
    elif fault == "malformed-json":
        receipt_path.write_text('{"truncated":')
    else:
        if fault == "cli-hash":
            client["docker_cli"]["sha256"] = "9" * 64
        elif fault == "plugin-executable":
            client["docker_plugins"]["compose"]["resolved_path"] = "/tools/foreign/docker-compose"
        elif fault == "plugin-version":
            client["docker_plugins"]["buildx"]["version"] = "v0.99.0"
        elif fault == "missing-field":
            del client["docker_plugins"]["compose"]["resolved_path"]
        elif fault == "relative-alias":
            client["docker_cli"]["path"] = "relative/docker"
        else:
            client["docker_cli"]["sha256"] = "unknown"
        receipt_path.write_text(json.dumps(client))
    try:
        cache.reserve().publish(records, tasks)
    finally:
        cache.close()
    assert not list(cache_path.glob("*/manifest.json"))


@pytest.mark.parametrize(
    "fault",
    [
        "cleanup",
        "invocation-image",
        "export-image",
        "toolchain-content",
        "version-only",
        "docker-client",
        "missing-client-proof",
        "missing-state-proof",
        "state-running",
        "unknown-oom",
        "missing-container-id",
        "rebound-owner",
        "state-resources",
        "state-image",
        "state-file-missing",
        "state-worker-mismatch",
        "invocation-agent-budget",
        "invocation-build-budget",
        "invocation-missing-agent-budget",
        "invocation-missing-build-budget",
    ],
)
def test_cached_harbor_delivery_tampering_is_rejected_before_import(
    tmp_path: Path, fault: str
) -> None:
    cache_path = tmp_path / "cache"
    first, tasks, records = _harbor_cohort(tmp_path / "first", cache_path)
    try:
        first.reserve().publish(records, tasks)
    finally:
        first.close()
    manifest_path = next(cache_path.glob("*/manifest.json"))
    filename = (
        "container-state.json"
        if fault.startswith("state-")
        or fault
        in {
            "missing-state-proof",
            "state-running",
            "unknown-oom",
            "missing-container-id",
            "rebound-owner",
        }
        else "invocation.json"
        if fault.startswith("invocation-")
        else "docker-client.json"
        if fault in {"docker-client", "missing-client-proof"}
        else "runtime-isolation.json"
    )
    receipt_path = next(manifest_path.parent.glob(f"attempts/*/handoff/artifacts/{filename}"))
    receipt = json.loads(receipt_path.read_text())
    if fault == "cleanup":
        receipt["cleanup"]["residuals"]["networks"] = ["leaked-network"]
    elif fault == "invocation-image":
        receipt["image_identity"]["platform"] = "linux/amd64"
    elif fault == "invocation-agent-budget":
        receipt["timeout_seconds"] = 30
    elif fault == "invocation-build-budget":
        receipt["build_timeout_seconds"] = 300
    elif fault == "invocation-missing-agent-budget":
        del receipt["timeout_seconds"]
    elif fault == "invocation-missing-build-budget":
        del receipt["build_timeout_seconds"]
    elif fault == "export-image":
        receipt["export"]["base_image_identity"]["workdir"] = "/foreign-repo"
    elif fault == "toolchain-content":
        receipt["export"]["toolchain_identity"]["image_id"] = "sha256:" + "f" * 64
    elif fault == "version-only":
        del receipt["export"]["toolchain_identity"]
    elif fault == "docker-client":
        receipt["docker_plugins"]["compose"]["sha256"] = "9" * 64
    elif fault == "missing-state-proof":
        del receipt["state"]
    elif fault == "state-running":
        receipt["state"]["Running"] = True
    elif fault == "unknown-oom":
        receipt["state"]["OOMKilled"] = None
    elif fault == "missing-container-id":
        del receipt["container_id"]
    elif fault == "rebound-owner":
        receipt["owner"] = "f" * 32
    elif fault == "state-resources":
        receipt["resources"]["memory_mb"] = 2048
    elif fault == "state-image":
        receipt["derived_image_id"] = "sha256:" + "0" * 64
    elif fault == "state-worker-mismatch":
        worker = receipt_path.with_name("worker.json")
        worker.write_text(json.dumps({"session_id": "another-trial"}))
    if fault in {"missing-client-proof", "state-file-missing"}:
        receipt_path.unlink()
    else:
        receipt_path.write_text(json.dumps(receipt))
    manifest = json.loads(manifest_path.read_text())
    manifest["inventory"] = baselines._inventory(manifest_path.parent)
    manifest_path.write_text(json.dumps(manifest))
    second, _, _ = _harbor_cohort(tmp_path / "second", cache_path)
    reason = (
        "immutable.*toolchain" if fault == "version-only" else "complete isolated baseline evidence"
    )
    with pytest.raises(ValueError, match=reason):
        second.reserve()
