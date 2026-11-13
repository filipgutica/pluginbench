from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from pluginbench import (
    baselines,
    claude,
    claude_launch,
    claude_treatments,
    evaluation,
    harbor_backend,
    native_treatments,
    saved_usage,
    study,
    submissions,
    trial_containers,
)
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.datasets import TaskSpec, native_identity, portable_native_identity
from pluginbench.native_treatments import NativeCapabilities
from native_support import (
    task_entry,
    runtime_identity,
    execution as native_execution,
    synthetic_contract,
)


@pytest.fixture(autouse=True)
def synthetic_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)


def _study(
    directory: Path, *, guidance: str = "candidate", runner: dict[str, str] | None = None
) -> study.ResolvedStudy:
    directory.mkdir()
    version = (runner or {}).get("harness_version", "0.143.0")
    entry = task_entry(directory, "answer", answer_path="answer.py", version=version)
    if runner:
        provenance = entry["provenance"]
        assert isinstance(provenance, dict)
        identity = json.loads(provenance["native_task_identity"])
        identity["toolchain_identity"]["harness"] = runner["harness"]
        provenance["native_task_identity"] = json.dumps(identity)
    (directory / "tasks.yaml").write_text(yaml.safe_dump({"schema_version": 1, "tasks": [entry]}))
    (directory / "guidance.md").write_text(f"Follow {guidance}.\n")
    recipe = directory / "recipe.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": guidance,
                "tasks": "tasks.yaml",
                "repeats": 2,
                "harness_version": "0.143.0",
                "timeout_seconds": 1800,
                "guidance": {guidance: ["guidance.md"]},
                **(runner or {}),
            }
        )
    )
    return study.load_study(recipe)


def _coding(
    tasks: tuple[TaskSpec, ...], runner: dict[str, str] | None = None
) -> dict[str, object]:
    return {
        "delivery": "native-instructions-skills-plugins-v1",
        "capabilities": {"subagents": True, "max_threads": 4, "max_depth": 1},
        "isolation": runtime_identity(),
        "harness_version": (runner or {}).get("harness_version", "0.143.0"),
        "timeout_seconds": 1800,
        "study": {
            "harness": "codex", "model": "gpt-6.1-sol", "reasoning": "medium",
            **{key: value for key, value in (runner or {}).items() if key != "harness_version"},
        },
        "task_environment": {
            task.task_id: portable_native_identity(native_identity(task)) for task in tasks
        },
    }


def _cohort(
    directory: Path, cache_path: Path, runner: dict[str, str] | None = None
) -> tuple[baselines.BaselineCache, tuple[TaskSpec, ...], list[dict[str, Any]]]:
    directory.mkdir()
    tasks = _study(directory / "config", runner=runner).tasks
    identity = _coding(tasks)
    root = directory / "run"
    records = []
    for repeat in (1, 2):
        identifier = f"baseline-{repeat}"
        target = root / "attempts" / identifier / "handoff"
        snapshot = target / "snapshot"
        shutil.copytree(tasks[0].fixture, snapshot)
        (snapshot / "answer.py").write_text("answer = 1\n")
        # Generate authentic native receipt contracts, then preserve their frozen paths.
        area = directory / f"area-{repeat}"
        shutil.copytree(snapshot, area / "workspace")
        execution = native_execution(
            area,
            native_identity(tasks[0]),
            answer_path="answer.py",
            timeout=1800,
            version="0.143.0",
            diagnostic_files={"dist/temporary.js": "generated after patch collection\n"},
        )
        shutil.copytree(area / "workspace", target / "diagnostic-source")
        shutil.move(str(area / "artifacts"), target / "artifacts")
        execution.update(
            successful_native_completion=repeat == 2,
            actual_primary_models=[],
            runtime_effort_evidence=[],
        )
        (target / "execution.json").write_text(json.dumps(execution))
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
                        "snapshot": {
                            "path": str(snapshot.relative_to(root)),
                            "sha256": inspect_snapshot(snapshot)["digest"],
                        },
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
    return (
        baselines.BaselineCache(
            path=cache_path, root=root, tasks=tasks, entries=records, identity=identity
        ),
        tasks,
        records,
    )


def _publish(cache: baselines.BaselineCache, records: list[dict[str, Any]]) -> list[str | None]:
    """Publish each record; returns why each one was not cached, or None."""
    try:
        cache.reserve()
        return [cache.publish(record) for record in records]
    finally:
        cache.close()


def test_native_cache_reuses_failed_attempts_and_unknown_usage_without_observers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    _publish(first, records)
    second, _, entries = _cohort(tmp_path / "second", path)
    shutil.rmtree(second.root)
    second.root.mkdir()
    try:
        second.reserve()
        assert all(row["status"] == "hit" for row in second.decisions().values())
        imported = [second.import_attempt(entry) for entry in entries]
    finally:
        second.close()
    assert imported == records
    assert imported[0] is not None
    assert imported[0]["stages"][0]["work"]["tokens"] is None
    execution = second.root / "attempts/baseline-1/handoff/execution.json"
    assert json.loads(execution.read_text())["successful_native_completion"] is False
    assert list(second.root.glob("baseline-evidence/*/origin.json"))


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "reasoning",
        "jobs",
        "timeout_seconds",
        "harness_version",
        "grader_image",
        "source",
        "task_assets",
        "engine-id",
        "platform",
        "toolchain",
        "trusted_tests",
        "source_policy",
        "delivery",
        "capabilities",
        "unchanged",
    ],
)
def test_changed_native_coding_identity_misses(tmp_path: Path, field: str) -> None:
    # Lookups scan saved contexts, so a different key alone does not prove a miss: reserve.
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    _publish(first, records)
    other, _, _ = _cohort(tmp_path / "second", path)
    context = json.loads(json.dumps(other.contexts["answer"]))
    coding = context["coding"]
    native = coding["native_task_identity"]
    if field == "unchanged":
        pass
    elif field == "source_policy":
        context["source_policy"] = "old working tree source policy"
    elif field in {"model", "reasoning", "jobs"}:
        coding["study"][field] = "different"
    elif field in {"harness_version", "timeout_seconds", "delivery", "capabilities"}:
        coding[field] = "different"
    elif field == "grader_image":
        native["grader_image_identity"]["image_id"] = "sha256:" + "9" * 64
    elif field == "engine-id":
        # Recorded, not compared: the engine was restarted or replaced.
        coding["isolation"]["docker_engine"]["id"] = "new daemon"
    elif field == "platform":
        coding["isolation"]["docker_engine"]["platform"] = "linux/riscv64"
    elif field == "toolchain":
        native["toolchain_identity"]["image_id"] = "sha256:" + "9" * 64
    else:
        native[
            {
                "source": "source_digest",
                "task_assets": "task_assets_sha256",
                "trusted_tests": "trusted_tests_sha256",
            }[field]
        ] = "9" * 64
    other.contexts["answer"] = context
    other.contracts["answer"] = baselines._cohort_contract(context)
    other.keys["answer"] = baselines._cohort_key(context, other.tasks["answer"])
    other.reserve()
    try:
        statuses = [row["status"] for row in other.decisions().values()]
    finally:
        other.close()
    assert statuses == ["hit" if field in {"unchanged", "engine-id"} else "miss"] * 2


@pytest.mark.parametrize(
    "fault",
    [
        "running",
        "digest",
        "before",
        "cleanup",
        "residuals",
        "runtime",
        "private",
        "image",
        "toolchain",
        "version",
        "budget",
        "model",
        "effort",
        "source-proof",
        "client-proof",
        "cli-hash",
        "plugin-hash",
        "plugin-version",
        "linked-client",
        "relative-client",
        "nonbinary-state",
        "container-id",
        "project",
        "grader-image",
        "grader-running",
        "grader-resources",
        "grader-proof",
        "coding-resources",
        "diagnostic-bytes",
        "diagnostic-mode",
        "canonical-bytes",
        "canonical-mode",
        "native-missing-delivery",
        "native-wrong-delivery",
        "native-capabilities",
        "native-missing-receipt",
        "native-unverified-receipt",
        "native-execution-receipt",
        "native-nonempty-inventory",
        "native-effective-config",
        "native-spawn-overrides-shown",
        "native-prompt",
        "descendant-model",
        "descendant-effort",
    ],
)
def test_incomplete_or_mismatched_native_delivery_cannot_seed_cache(
    tmp_path: Path, fault: str
) -> None:
    path = tmp_path / "cache"
    cache, tasks, records = _cohort(tmp_path / "first", path)
    target = cache.root / "attempts/baseline-1/handoff"
    artifacts = target / "artifacts"
    if fault.startswith("native-"):
        invocation_path = artifacts / "invocation.json"
        invocation = json.loads(invocation_path.read_text())
        receipt_path = artifacts / "native-treatment.json"
        treatment = json.loads(receipt_path.read_text())
        execution_path = target / "execution.json"
        execution = json.loads(execution_path.read_text())
        if fault == "native-missing-delivery":
            invocation.pop("delivery")
        elif fault == "native-wrong-delivery":
            invocation["delivery"] = "forced-text"
        elif fault == "native-capabilities":
            invocation["capabilities"]["subagents"] = False
        elif fault == "native-missing-receipt":
            receipt_path.unlink()
        elif fault == "native-unverified-receipt":
            treatment["status"] = "failed"
        elif fault == "native-execution-receipt":
            execution.pop("native_treatment")
        elif fault == "native-nonempty-inventory":
            inventory = {"guidance": [{"name": "extra.md"}], "skills": [], "plugins": []}
            invocation["treatment_inventory"] = inventory
            treatment["inventory"] = inventory
        elif fault == "native-effective-config":
            treatment["effective_config"] = treatment["effective_config"].replace(
                "multi_agent = true", "multi_agent = false"
            )
        elif fault == "native-spawn-overrides-shown":
            # Launched before V2's spawn model fields were hidden: another tool schema.
            treatment["effective_config"] = treatment["effective_config"].replace(
                "[features.multi_agent_v2]\nexpose_spawn_agent_model_overrides = false\n", ""
            )
        else:
            (artifacts / "prompt.txt").write_text("Treatment injected into prompt")
        if fault != "native-execution-receipt":
            execution["native_treatment"] = treatment
        invocation_path.write_text(json.dumps(invocation))
        execution_path.write_text(json.dumps(execution))
        if fault != "native-missing-receipt":
            receipt_path.write_text(json.dumps(treatment))
    elif fault.startswith(("diagnostic-", "canonical-")):
        directory = "diagnostic-source" if fault.startswith("diagnostic-") else "snapshot"
        file = target / directory / "answer.py"
        if fault.endswith("bytes"):
            file.write_text("tampered = True\n")
        else:
            file.chmod(0o755)
    elif fault in {"client-proof", "source-proof"}:
        (
            artifacts / ("docker-client.json" if fault == "client-proof" else "before-source.json")
        ).unlink()
    elif fault in {"cli-hash", "plugin-hash", "plugin-version", "relative-client", "linked-client"}:
        file = artifacts / "docker-client.json"
        client = json.loads(file.read_text())
        if fault == "cli-hash":
            client["docker_cli"]["sha256"] = "9" * 64
        elif fault == "plugin-hash":
            client["docker_plugins"]["compose"]["sha256"] = "9" * 64
        elif fault == "plugin-version":
            client["docker_plugins"]["buildx"]["version"] = "different"
        elif fault == "relative-client":
            client["docker_cli"]["path"] = "relative/docker"
        else:
            # Filesystem inventory refuses the link even when its bytes are valid.
            original = file.with_suffix(".original")
            file.rename(original)
            file.symlink_to(original)
        if fault != "linked-client":
            file.write_text(json.dumps(client))
    elif fault in {"image", "toolchain", "version", "budget", "model", "effort", "runtime"}:
        file = artifacts / "invocation.json"
        invocation = json.loads(file.read_text())
        native = invocation["native_task_identity"]
        if fault == "image":
            native["image_identity"]["image_id"] = "sha256:" + "9" * 64
        elif fault == "toolchain":
            native["toolchain_identity"] = {"harness_version": "0.143.0"}
        elif fault == "runtime":
            native["isolation"]["docker_engine"]["id"] = "foreign daemon"
        else:
            invocation[
                {
                    "version": "harness_version",
                    "budget": "timeout_seconds",
                    "model": "requested_model",
                    "effort": "requested_effort",
                }[fault]
            ] = "wrong"
        file.write_text(json.dumps(invocation))
    elif fault in {"descendant-model", "descendant-effort"}:
        # A child the runner could not lock ran on another model or effort.
        file = target / "execution.json"
        execution = json.loads(file.read_text())
        if fault == "descendant-model":
            execution["actual_descendant_models"] = ["another-model"]
        else:
            execution["actual_descendant_efforts"] = ["low"]
        file.write_text(json.dumps(execution))
    elif fault == "private":
        file = target / "execution.json"
        execution = json.loads(file.read_text())
        execution["private_runtime_removed"] = False
        file.write_text(json.dumps(execution))
    else:
        file = artifacts / "runtime-isolation.json"
        receipt = json.loads(file.read_text())
        if fault == "grader-proof":
            del receipt["verifier"]
        elif fault == "grader-image":
            receipt["verifier"]["image_id"] = "sha256:" + "9" * 64
        elif fault == "grader-running":
            receipt["verifier"]["state"]["Running"] = True
        elif fault == "grader-resources":
            receipt["verifier"]["resources"]["Memory"] = 4096
        elif fault == "cleanup":
            receipt["cleanup"]["status"] = "incomplete"
        elif fault == "residuals":
            receipt["cleanup"]["projects"]["coding"]["residuals"]["containers"] = ["leftover"]
        elif fault == "before":
            before = artifacts / "before-source.json"
            data = json.loads(before.read_text())
            data["snapshot_digest"] = "sha256:" + "9" * 64
            before.write_text(json.dumps(data))
        else:
            exported = receipt["export"]
            if fault == "coding-resources":
                exported["resources"]["memory_mb"] = 4096
            elif fault == "running":
                exported["stopped"] = False
            elif fault == "digest":
                exported["snapshot_digest"] = "sha256:" + "9" * 64
            elif fault == "container-id":
                exported["container_id"] = "unknown"
            elif fault == "project":
                exported["project"] = "another project"
            else:
                exported["state"]["OOMKilled"] = "unknown"
            (artifacts / "submission-source.json").write_text(json.dumps(exported))
        file.write_text(json.dumps(receipt))
    if fault == "linked-client":
        with pytest.raises(ValueError, match="unsupported artifact"):
            _publish(cache, records)
    else:
        _publish(cache, records)
        # An intact repeat is cached on its own.
        assert (path / cache.keys["answer"] / "2/manifest.json").is_file()
    # The faulty repeat seeds nothing.
    assert not list(path.glob("*/1"))


def test_cached_artifact_tampering_is_rejected_before_import(tmp_path: Path) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    _publish(first, records)
    cohort = path / first.keys["answer"] / "1"
    (cohort / "attempts/baseline-1/handoff/artifacts/submission-source.json").write_text("tampered")
    second, _, _ = _cohort(tmp_path / "second", path)
    with pytest.raises(ValueError, match="cohort changed"):
        second.reserve()


def test_session_that_never_reached_the_model_is_not_published(tmp_path: Path) -> None:
    # Runs from before the coding pass stopped on such a session recorded it as complete.
    path = tmp_path / "cache"
    cache, _, records = _cohort(tmp_path / "run", path)
    receipt = cache.root / "attempts/baseline-1/handoff/execution.json"
    execution = json.loads(receipt.read_text())
    execution.update(
        usage=None,
        timed_out=False,
        stream_errors=[{"type": "turn.failed", "error": {"message": "backend unreachable"}}],
    )
    receipt.write_text(json.dumps(execution))
    _publish(cache, records)
    assert [entry.name for entry in (path / cache.keys["answer"]).iterdir()] == ["2"]


def test_cohort_lock_and_each_published_repeat_is_its_own_hit(tmp_path: Path) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    second, _, _ = _cohort(tmp_path / "second", path)
    try:
        first.reserve()
        with pytest.raises(ValueError, match="cohort is in use"):
            second.reserve()
        # A run that stops after one repeat still caches that repeat.
        first.publish(records[0])
        assert [entry.name for entry in (path / first.keys["answer"]).iterdir()] == ["1"]
    finally:
        first.close()
    try:
        second.reserve()
        decisions = second.decisions()
        assert decisions["baseline-1"]["status"] == "hit"
        assert decisions["baseline-1"]["origin_cohort"].endswith(f"{second.keys['answer']}/1")
        assert decisions["baseline-2"]["status"] == "miss"
    finally:
        second.close()


@pytest.mark.parametrize("unsupported_patch", [False, True])
def test_reuse_regrades_without_replaying_models_and_retains_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported_patch: bool
) -> None:
    calls = []
    regrades = []
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime_identity())
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        result = native_execution(
            area,
            kwargs["protocol"]["native_task_identity"],
            answer_path="answer.py",
            timeout=1800,
            version="0.143.0",
            diagnostic_files={"dist/temporary.js": "generated after patch collection\n"},
        )
        result.update(
            successful_native_completion=not (
                kwargs["entry"]["arm"] == "baseline" and kwargs["entry"]["attempt"] == 1
            ),
            usage=None if kwargs["entry"]["attempt"] == 1 else {"input_tokens": 13},
            elapsed_seconds=7.0,
        )
        if (
            unsupported_patch
            and kwargs["entry"]["arm"] == "baseline"
            and kwargs["entry"]["attempt"] == 2
        ):
            artifacts = area / "artifacts/harbor/trial/artifacts"
            (artifacts / submissions.PATCH_ARTIFACT).write_text("invalid patch\n")
            result["native_artifact_hashes"] = harbor_backend._native_artifact_hashes(artifacts)
        return result

    def regrade(**kwargs: Any) -> dict[str, object]:
        regrades.append(kwargs)
        return {
            "verifier_result": {"rewards": {"reward": 1}},
            "exception_info": None,
            "receipt": {"model_calls": 0},
        }

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    monkeypatch.setattr(harbor_backend, "regrade_native", regrade)
    one = _study(tmp_path / "one")
    first = study.run_study(
        one,
        tmp_path / "first",
        baseline_cache=tmp_path / "cache",
        baseline_identity=_coding(one.tasks),
    )
    two = _study(tmp_path / "two", guidance="another")
    recipe = yaml.safe_load(two.source.read_text())
    recipe["guidance"]["third"] = ["guidance.md"]
    recipe["guidance"]["fourth"] = ["guidance.md"]
    two.source.write_text(yaml.safe_dump(recipe))
    two = study.load_study(two.source)
    second = study.run_study(
        two,
        tmp_path / "second",
        baseline_cache=tmp_path / "cache",
        baseline_identity=_coding(two.tasks),
    )
    assert len(calls) == 10 and len(regrades) == (1 if unsupported_patch else 2)
    assert all(call["entry"]["arm"] in {"another", "third", "fourth"} for call in calls[4:])
    run = json.loads((second / "run.json").read_text())
    assert run["actual_calls"] == 6 and len(run["baseline_origin"]) == 2
    records = json.loads((second / "assessment-input.json").read_text())["records"]
    baseline = sorted(
        (row for row in records if row["arm"] == "baseline"), key=lambda row: row["attempt"]
    )
    assert baseline[0]["stages"][0]["work"]["tokens"] is None
    assert baseline[1]["stages"][0]["work"]["tokens"] is None
    validation = json.loads(
        (second / "attempts" / baseline[1]["id"] / "handoff/execution-current.json").read_text()
    )["usage_validation"]
    assert validation["recorded_usage"] == {"input_tokens": 13}
    assert validation["status"] == "unavailable"
    correctness = second / baseline[0]["stages"][0]["correctness"]["path"]
    assert json.loads(correctness.read_text())["checks"]["native_completion"] is False
    # Origins name their cohort relative to the cache root, never the origin run's path.
    assert all(
        item["origin_cohort"] == f"cache/{item['cache_key']}/{item['original_record']['attempt']}"
        and "origin_run" not in item
        for item in run["baseline_origin"].values()
    )
    for record in baseline:
        if unsupported_patch and record["attempt"] == 2:
            assert record["stages"][0]["snapshot"] is None
            assert record["stages"][0]["correctness"] is None
            target = second / "attempts" / record["id"] / "handoff"
            assert (target / "diagnostic-source/dist/temporary.js").is_file()
            original_id = run["baseline_origin"][record["id"]]["original_record"]["id"]
            assert original_id != record["id"]
            assert (target / "unsupported-submission.json").read_bytes() == (
                first / "attempts" / original_id / "handoff/unsupported-submission.json"
            ).read_bytes()
            continue
        snapshot = second / record["stages"][0]["snapshot"]["path"]
        assert not (snapshot / "dist/temporary.js").exists()
        assert (snapshot.parent / "diagnostic-source/dist/temporary.js").is_file()


def test_moved_cache_and_run_keep_their_cache_decisions_readable(tmp_path: Path) -> None:
    first, tasks, records = _cohort(tmp_path / "first", tmp_path / "cache-a/vanilla")
    _publish(first, records)
    (tmp_path / "cache-a").rename(tmp_path / "cache-b")
    second, _, entries = _cohort(tmp_path / "second", tmp_path / "cache-b/vanilla")
    shutil.rmtree(second.root)
    second.root.mkdir()
    try:
        second.reserve()
        decisions = second.decisions()
        for entry in entries:
            second.import_attempt(entry)
    finally:
        second.close()
    moved = tmp_path / "moved-run"
    second.root.rename(moved)
    for identifier, decision in decisions.items():
        assert decision["status"] == "hit"
        assert not any(
            isinstance(value, str) and value.startswith("/") for value in decision.values()
        )
        cohort = tmp_path / "cache-b" / str(decision["origin_cohort"])
        assert (cohort / "attempts" / str(decision["origin_attempt"])).is_dir()
        origin = json.loads((moved / "baseline-evidence" / identifier / "origin.json").read_text())
        assert {key: origin[key] for key in decision} == decision
        assert all((moved / row["path"]).is_file() for row in origin["historical_evidence"])


def _hits(cache: baselines.BaselineCache) -> list[str]:
    try:
        cache.reserve()
        return [row["status"] for row in cache.decisions().values()]
    finally:
        cache.close()


def test_runtime_captures_that_differ_by_memory_jitter_still_publish_and_admit(
    tmp_path: Path,
) -> None:
    # `check` captures the runtime at planning and again at preparation; Docker can report a
    # few KiB less memory between them. Every cohort that run published was never admitted.
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    planned = runtime_identity()
    engine = planned["docker_engine"]
    assert isinstance(engine, dict)
    engine["capacity"] = {"cpus": 8, "memory_bytes": 32 * 1024**3 - 28 * 1024}
    jittered = baselines.BaselineCache(
        path=path,
        root=first.root,
        tasks=tasks,
        entries=records,
        identity={**_coding(tasks), "isolation": planned},
    )
    _publish(jittered, records)
    assert len(list(path.glob("*/*/manifest.json"))) == 2
    second, _, _ = _cohort(tmp_path / "second", path)
    assert _hits(second) == ["hit", "hit"]


def test_an_attempt_that_ran_with_an_updated_docker_cli_is_still_cached(tmp_path: Path) -> None:
    # Each attempt uses the Docker CLI installed when it starts and records that runtime.
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    updated = runtime_identity()
    updated["docker_cli"] = {**updated["docker_cli"], "sha256": "9" * 64}  # type: ignore[dict-item]
    for record in records:
        artifacts = first.root / "attempts" / record["id"] / "handoff/artifacts"
        client = json.loads((artifacts / "docker-client.json").read_text())
        client["docker_cli"] = updated["docker_cli"]
        (artifacts / "docker-client.json").write_text(json.dumps(client))
        invocation = json.loads((artifacts / "invocation.json").read_text())
        (artifacts / "invocation.json").write_text(json.dumps({**invocation, "runtime": updated}))
    _publish(first, records)
    second, _, _ = _cohort(tmp_path / "second", path)
    assert _hits(second) == ["hit", "hit"]


@pytest.mark.parametrize("evidence", ["valid", "sessions", "ordinal", "subset", "disagrees", "legacy"])
@pytest.mark.parametrize("boundary", ["report", "reuse"])
def test_saved_usage_is_revalidated_on_report_and_cache_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str, boundary: str
) -> None:
    from test_codex import native_session, stream

    calls = []
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime_identity())
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    monkeypatch.setattr(
        harbor_backend, "regrade_native",
        lambda **kwargs: {"verifier_result": {"rewards": {"reward": 1}}, "exception_info": None},
    )
    recorded = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        execution = native_execution(
            area, kwargs["protocol"]["native_task_identity"],
            answer_path="answer.py", timeout=1800, version="0.143.0",
        )
        artifacts = area / "artifacts"
        counts = dict(recorded)
        if evidence == "subset":
            counts["cached_input_tokens"] = 11
        if evidence == "disagrees":
            counts.update(input_tokens=11, total_tokens=16)
        (artifacts / "stdout.jsonl").write_text(
            stream({"type": "turn.completed", "usage": counts})
        )
        if evidence != "legacy":
            events = [json.loads(line) for line in native_session("root", [counts]).splitlines()]
            if evidence == "ordinal":
                for index, event in enumerate(events):
                    event["ordinal"] = index + (1 if index > 1 else 0)
            trace = artifacts / "host-trace-0.jsonl"
            if evidence == "sessions":
                trace = artifacts / "harbor/trial/agent/sessions/rollout.jsonl"
                trace.parent.mkdir(parents=True)
                (trace.parent.parent / "codex.txt").write_text(
                    (artifacts / "stdout.jsonl").read_text()
                )
                (artifacts / "stdout.jsonl").unlink()
            trace.write_text(stream(*events))
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    one = _study(tmp_path / "one")
    cache = tmp_path / "cache"
    first = study.run_study(one, tmp_path / "first", baseline_cache=cache,
                            baseline_identity=_coding(one.tasks))
    originals = {
        path: path.read_bytes()
        for path in first.glob("attempts/*/handoff/execution.json")
    }
    # Rebuilding a report must project the same result without changing its evidence.
    study.report_study(first)
    root = first
    if boundary == "reuse":
        two = _study(tmp_path / "two", guidance="another")
        root = study.run_study(two, tmp_path / "second", baseline_cache=cache,
                               baseline_identity=_coding(two.tasks))
        assert len(calls) == 6  # Two cached baselines, no model replay.
        run = json.loads((root / "run.json").read_text())
        for attempt_id in run["baseline_origin"]:
            reference = run["executions"][attempt_id]["handoff"]
            assert reference["path"] == f"attempts/{attempt_id}/handoff/execution-current.json"
            assert study._bound_document(root, reference)["usage_validation"]
        manifest = json.loads((root / "assessment-input.json").read_text())
        for row in manifest["records"]:
            if row["arm"] == "baseline":
                assert row["stages"][0]["work"]["tokens"] == (
                    recorded if evidence in {"valid", "sessions"} else None
                )
    expected_reason = {
        "ordinal": "noncontiguous rollout ordinals",
        "subset": "invalid cumulative usage",
        "disagrees": "disagrees with recorded usage",
        "legacy": "no saved session evidence",
    }
    report = json.loads((root / "report/assessment-report.json").read_text())
    for row in report["attempts"]:
        work = row["stages"][0]["work"]
        assert work["tokens"] == (recorded if evidence in {"valid", "sessions"} else None)
        validation = work["usage_validation"]
        assert validation["recorded_usage"] == recorded
        assert validation["recorded_at"]
        if evidence in {"valid", "sessions"}:
            assert work["tokens"] == recorded
            assert validation["status"] == "validated"
        else:
            assert work["tokens"] is None
            assert validation["status"] == "unavailable"
            assert any(expected_reason[evidence] in reason for reason in validation["reasons"])
    from pluginbench.evaluation_report import evaluation_data

    results = evaluation_data(report, {"measurements": ["tokens"]})
    observations = results["observations"]
    assert isinstance(observations, list)
    token_rows = [row for row in observations if row["metric"] == "Input tokens"]
    assert all(row["value"] == (10 if evidence in {"valid", "sessions"} else None)
               for row in token_rows)
    assert all(row["provenance"]["usage_validation"]["recorded_usage"] == recorded
               for row in token_rows)
    if evidence in expected_reason:
        assert all(expected_reason[evidence] in row["reason"] for row in token_rows)
    assert all(path.read_bytes() == content for path, content in originals.items())
    # Imported execution receipts remain the original, even after regrading.
    for path in root.glob("attempts/*/handoff/execution-current.json"):
        assert json.loads(path.with_name("execution.json").read_text())["usage"] == recorded


CLAUDE_RUNNER = {"harness": "claude", "model": "claude-opus-5-5", "harness_version": "2.1.322"}
CLAUDE_FIXTURES = Path(__file__).parent / "fixtures/claude-2.1.322"


def _claude_identity(
    tasks: tuple[TaskSpec, ...], *, subagents: bool = True
) -> dict[str, object]:
    """The vanilla identity `check` and `run` give a Claude run of these tasks."""
    config = evaluation.Evaluation.model_construct(
        runner=evaluation.Runner(
            harness="claude", model=CLAUDE_RUNNER["model"], reasoning="medium",
            cli_version=CLAUDE_RUNNER["harness_version"],
        ),
        capabilities=NativeCapabilities(subagents=subagents),
        # As the recipes of _study set them.
        timeout_seconds=1800,
        jobs=1,
        seed=42,
    )
    comparison: dict[str, object] = {
        "tasks": {task.task_id: "d" * 64 for task in tasks},
        "runner": config.runner.model_dump(mode="json", exclude={"cli_version"}),
        "isolation": runtime_identity(),
        # As plan_evaluation records them.
        "delivery": study.delivery_policy("claude"),
        "capabilities": claude_launch.recorded_capabilities(config.capabilities, "claude"),
    }
    return evaluation._vanilla_identity(
        config,
        comparison,
        CLAUDE_RUNNER["harness_version"],
        {task.task_id: portable_native_identity(native_identity(task)) for task in tasks},
    )


def _claude_evidence(
    artifacts: Path, execution: dict[str, Any], *, subagents: bool = True
) -> dict[str, Any]:
    """Turn a native fixture attempt into the receipts invoke_native writes for Claude."""
    run = claude.CodingRun(CLAUDE_RUNNER["model"], "medium", subagents, 1800)
    inventory: dict[str, list[object]] = {"guidance": [], "skills": [], "plugins": []}
    launch = {
        "harness": "claude-cli",
        "requested_model": CLAUDE_RUNNER["model"],
        "requested_effort": "medium",
        "harness_version": CLAUDE_RUNNER["harness_version"],
        "backend": harbor_backend.PROTOCOL,
        "delivery": claude_treatments.DELIVERY_POLICY,
        "capabilities": claude_launch.capabilities(subagents),
        "treatment_inventory": inventory,
        "argv": claude.coding_launch(run)[0],
    }
    invocation = json.loads((artifacts / "invocation.json").read_text())
    (artifacts / "invocation.json").write_text(json.dumps({**invocation, **launch}))
    builtins = json.loads((CLAUDE_FIXTURES / "subscription-builtins.json").read_text())
    receipt = {
        "status": "verified",
        "delivery": claude_treatments.DELIVERY_POLICY,
        "inventory": inventory,
        "cli_version": CLAUDE_RUNNER["harness_version"],
        "account": {"subscriptionType": "max", "apiProvider": "firstParty"},
        "commands": [],
        "skills": [],
        "builtins": builtins["builtins"],
        "plugin_installs": [],
        "hooks": {"hooks": [], "policy": {"allDisabled": True}},
        "settings": {"disableAllHooks": True},
        "discovery": {"argv": claude.discovery_launch(run)[0], "exit_code": 0},
    }
    (artifacts / "native-treatment.json").write_text(json.dumps(receipt))
    tools = json.loads((CLAUDE_FIXTURES / "init-tools.json").read_text())["signed_in"]
    return {
        **execution,
        "launch": launch,
        "native_treatment": receipt,
        "claude_init_tools": tools if subagents else [tool for tool in tools if tool != "Task"],
        "claude_permission_denials": [],
        "actual_primary_models": [CLAUDE_RUNNER["model"]],
        "runtime_effort_evidence": [],
    }


@pytest.mark.parametrize("evidence", ["valid", "orphan"])
@pytest.mark.parametrize("boundary", ["report", "reuse"])
def test_saved_claude_usage_is_revalidated_on_report_and_cache_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str, boundary: str
) -> None:
    fixture = CLAUDE_FIXTURES / "coding/one-subagent"
    stream = (fixture / "stream.jsonl").read_text()
    recorded = claude.parse_events(stream, [])["usage"]
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime_identity())
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    monkeypatch.setattr(
        harbor_backend, "regrade_native",
        lambda **kwargs: {"verifier_result": {"rewards": {"reward": 1}}, "exception_info": None},
    )
    calls = []

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        execution = native_execution(
            area, kwargs["protocol"]["native_task_identity"],
            answer_path="answer.py", timeout=1800, version=CLAUDE_RUNNER["harness_version"],
        )
        artifacts = area / "artifacts"
        (artifacts / "stdout.jsonl").write_text(stream)
        sessions = artifacts / "harbor/trial/agent/sessions"
        shutil.copytree(fixture / "sessions", sessions)
        if evidence == "orphan":
            (subagent,) = sessions.rglob("agent-*.meta.json")
            subagent.write_text(json.dumps({"toolUseId": "toolu_orphan"}))
        return {**_claude_evidence(artifacts, execution), "usage": recorded}

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    # The treatment is shaped as Codex packages; this test needs only saved runs.
    monkeypatch.setattr(study, "admit", lambda treatment, harness: native_treatments.admit(
        treatment, "codex"
    ))
    one = _study(tmp_path / "one", runner=CLAUDE_RUNNER)
    cache = tmp_path / "cache"
    root = study.run_study(one, tmp_path / "first", baseline_cache=cache,
                           baseline_identity=_claude_identity(one.tasks))
    study.report_study(root)
    if boundary == "reuse":
        two = _study(tmp_path / "two", guidance="another", runner=CLAUDE_RUNNER)
        # `check` previews with the identity the run binds from its frozen recipe.
        preview = baselines.preview(
            path=cache, tasks=two.tasks, repeats=2, identity=_claude_identity(two.tasks)
        )
        assert [row["status"] for row in preview] == ["hit", "hit"]
        root = study.run_study(two, tmp_path / "second", baseline_cache=cache,
                               baseline_identity=_claude_identity(two.tasks))
        assert len(calls) == 6  # Two cached baselines, no model replay.
        run = json.loads((root / "run.json").read_text())
        assert len(run["baseline_origin"]) == 2
        manifest = json.loads((root / "assessment-input.json").read_text())
        baseline = [row for row in manifest["records"] if row["arm"] == "baseline"]
        assert [row["stages"][0]["work"]["tokens"] for row in baseline] == [
            recorded if evidence == "valid" else None
        ] * 2
    report = json.loads((root / "report/assessment-report.json").read_text())
    for row in report["attempts"]:
        work = row["stages"][0]["work"]
        validation = work["usage_validation"]
        assert validation["recorded_usage"] == recorded
        if evidence == "valid":
            assert work["tokens"] == recorded and validation["status"] == "validated"
        else:
            assert work["tokens"] is None and validation["status"] == "unavailable"
            assert "a subagent transcript has no matching Agent tool call" in validation["reasons"]


def _claude_cohort(
    directory: Path, cache_path: Path
) -> tuple[baselines.BaselineCache, tuple[TaskSpec, ...], list[dict[str, Any]]]:
    """Two Claude vanilla repeats, coded and ready to publish under the Claude identity."""
    codex_shaped, tasks, records = _cohort(directory, cache_path, CLAUDE_RUNNER)
    for record in records:
        record["runtime"]["harness"] = "claude-cli"
        target = codex_shaped.root / "attempts" / record["id"] / "handoff"
        execution = json.loads((target / "execution.json").read_text())
        execution = _claude_evidence(target / "artifacts", execution)
        (target / "execution.json").write_text(json.dumps(execution))
    cache = baselines.BaselineCache(
        path=cache_path, root=codex_shaped.root, tasks=tasks, entries=records,
        identity=_claude_identity(tasks),
    )
    return cache, tasks, records


def _statuses(cache: baselines.BaselineCache) -> list[str]:
    try:
        cache.reserve()
        return [row["status"] for row in cache.decisions().values()]
    finally:
        cache.close()


def test_a_claude_vanilla_attempt_is_cached_and_reused_on_the_same_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache"
    first, _, records = _claude_cohort(tmp_path / "first", path)
    _publish(first, records)
    second, _, entries = _claude_cohort(tmp_path / "second", path)
    assert second.keys == first.keys
    shutil.rmtree(second.root)
    second.root.mkdir()
    try:
        second.reserve()
        assert [row["status"] for row in second.decisions().values()] == ["hit", "hit"]
        imported = [second.import_attempt(entry) for entry in entries]
    finally:
        second.close()
    assert imported == records


@pytest.mark.parametrize(
    "field",
    [
        "unchanged",
        "model",
        "reasoning",
        "cli_version",
        "subagents",
        "delivery",
        "coding_argv",
        "coding_env",
        "refused_env",
        "discovery_argv",
        "discovery_settings",
        "init_tools",
        "builtins",
        "harbor",
        "task_assets",
    ],
)
def test_changed_claude_coding_identity_misses(tmp_path: Path, field: str) -> None:
    # Lookups scan saved contexts, so a different key alone does not prove a miss: reserve.
    path = tmp_path / "cache"
    first, _, records = _claude_cohort(tmp_path / "first", path)
    _publish(first, records)
    other, _, _ = _claude_cohort(tmp_path / "second", path)
    context = json.loads(json.dumps(other.contexts["answer"]))
    coding = context["coding"]
    pinned = coding["claude"]
    if field in {"model", "reasoning"}:
        coding["study"][field] = "different"
    elif field == "cli_version":
        coding["harness_version"] = "2.1.323"
    elif field == "subagents":
        coding["capabilities"]["subagents"] = False
    elif field == "delivery":
        coding["delivery"] = "claude-native-v2"
    elif field in {"coding_argv", "refused_env", "discovery_argv", "init_tools"}:
        pinned[field] = [*pinned[field], "different"]
    elif field in {"coding_env", "discovery_settings"}:
        pinned[field] = {**pinned[field], "DIFFERENT": "1"}
    elif field == "builtins":
        pinned[field] = {**pinned[field], "agents": ["claude-code-guide"]}
    elif field == "harbor":
        coding["isolation"]["harbor_version"] = "0.25.0"
    elif field == "task_assets":
        coding["native_task_identity"]["task_assets_sha256"] = "9" * 64
    other.contexts["answer"] = context
    other.contracts["answer"] = baselines._cohort_contract(context)
    other.keys["answer"] = baselines._cohort_key(context, other.tasks["answer"])
    assert _statuses(other) == ["hit" if field == "unchanged" else "miss"] * 2


@pytest.mark.parametrize(
    "fault",
    [
        "authentic",
        "timeout-unknown-usage",
        "never-reached-model",
        "builtins",
        "init-tools",
        "no-init",
        "permission-denial",
        "argv",
        "launch",
        "capabilities",
        "settings",
        "discovery-argv",
        "receipt-version",
        "unverified-receipt",
        "execution-receipt",
        "codex-receipt",
        "treatment-inventory",
        "prompt",
    ],
)
def test_claude_evidence_that_differs_from_its_identity_cannot_seed_the_cache(
    tmp_path: Path, fault: str
) -> None:
    path = tmp_path / "cache"
    cache, _, records = _claude_cohort(tmp_path / "first", path)
    for record in records:
        target = cache.root / "attempts" / record["id"] / "handoff"
        artifacts = target / "artifacts"
        execution = json.loads((target / "execution.json").read_text())
        receipt = json.loads((artifacts / "native-treatment.json").read_text())
        invocation = json.loads((artifacts / "invocation.json").read_text())
        if fault == "timeout-unknown-usage":
            # A timeout leaves no result event: usage is unknown, and the attempt is scored.
            execution.update(usage=None, timed_out=True)
        elif fault == "never-reached-model":
            execution.update(usage=None, timed_out=False)
        elif fault == "builtins":
            receipt["builtins"]["agents"].append("claude-code-guide")
        elif fault == "init-tools":
            execution["claude_init_tools"].remove("WebFetch")
        elif fault == "no-init":
            execution["claude_init_tools"] = None
        elif fault == "permission-denial":
            execution["claude_permission_denials"] = ["Bash"]
        elif fault == "argv":
            invocation["argv"] = [*invocation["argv"], "--fork-session"]
            execution["launch"]["argv"] = invocation["argv"]
        elif fault == "launch":
            execution["launch"]["requested_model"] = "claude-fable-5-2"
        elif fault == "capabilities":
            invocation["capabilities"] = claude_launch.capabilities(False)
            execution["launch"]["capabilities"] = invocation["capabilities"]
        elif fault == "settings":
            receipt["settings"] = {"disableAllHooks": True, "permissions": {"allow": ["Bash"]}}
        elif fault == "discovery-argv":
            receipt["discovery"]["argv"] = receipt["discovery"]["argv"][:-1]
        elif fault == "receipt-version":
            receipt["cli_version"] = "2.1.323"
        elif fault == "unverified-receipt":
            receipt["status"] = "failed"
        elif fault == "treatment-inventory":
            invocation["treatment_inventory"] = {"guidance": ["CLAUDE.md"], "skills": [], "plugins": []}
            execution["launch"]["treatment_inventory"] = invocation["treatment_inventory"]
        elif fault == "prompt":
            (artifacts / "prompt.txt").write_text("Follow the guidance.\n")
        elif fault == "codex-receipt":
            receipt = execution["native_treatment"] = json.loads(
                json.dumps({**receipt, "delivery": native_treatments.DELIVERY_POLICY})
            )
        if fault != "execution-receipt":
            execution["native_treatment"] = receipt
        (target / "execution.json").write_text(json.dumps(execution))
        (artifacts / "native-treatment.json").write_text(json.dumps(receipt))
        (artifacts / "invocation.json").write_text(json.dumps(invocation))
    if fault == "execution-receipt":
        # The receipt file differs from the one the attempt's checks accepted.
        for target in cache.root.glob("attempts/*/handoff/artifacts/native-treatment.json"):
            saved = json.loads(target.read_text())
            target.write_text(json.dumps({**saved, "commands": ["undeclared"]}))
    reasons = _publish(cache, records)
    second, _, _ = _claude_cohort(tmp_path / "second", path)
    hit = fault in {"authentic", "timeout-unknown-usage"}
    assert _statuses(second) == ["hit" if hit else "miss"] * 2
    assert reasons == [
        None
        if hit
        else "its session never reached the model"
        if fault == "never-reached-model"
        else "its saved evidence does not match the vanilla cache identity"
    ] * 2


def test_the_claude_vanilla_identity_binds_subagents_to_the_runner_model() -> None:
    # A vanilla attempt coded before subagents were forced onto the runner model never hits.
    run = claude.CodingRun(CLAUDE_RUNNER["model"], "medium", True, 1800)
    env = baselines.claude_identity(run, CLAUDE_RUNNER["harness_version"])["coding_env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == CLAUDE_RUNNER["model"]
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"


def test_a_claude_cli_version_without_pinned_built_ins_and_tools_is_never_cached(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache"
    run = claude.CodingRun(CLAUDE_RUNNER["model"], "medium", True, 1800)
    unqualified = baselines.claude_identity(run, "2.1.399")
    assert unqualified["init_tools"] is None and unqualified["builtins"] is None
    caches = []
    for name in ("first", "second"):
        cache, _, records = _claude_cohort(tmp_path / name, path)
        # Everything else unchanged: only the qualification of the version is missing.
        cache.contexts["answer"]["coding"]["claude"].update(init_tools=None, builtins=None)
        cache.contracts["answer"] = baselines._cohort_contract(cache.contexts["answer"])
        cache.keys["answer"] = baselines._cohort_key(cache.contexts["answer"], cache.tasks["answer"])
        caches.append((cache, records))
    assert _publish(*caches[0]) == ["Claude Code 2.1.322 has no pinned built-ins and init tools"] * 2
    assert not any(path.glob("*/*/manifest.json"))
    assert _statuses(caches[1][0]) == ["miss", "miss"]


@pytest.mark.parametrize("cached", ["claude", "codex"])
def test_a_vanilla_entry_never_satisfies_the_other_harness(tmp_path: Path, cached: str) -> None:
    path = tmp_path / "cache"
    cohorts = {"claude": _claude_cohort, "codex": _cohort}
    other = "codex" if cached == "claude" else "claude"
    first, _, records = cohorts[cached](tmp_path / "first", path)
    _publish(first, records)
    same, _, _ = cohorts[cached](tmp_path / "same", path)
    assert _statuses(same) == ["hit", "hit"]
    lookup, _, _ = cohorts[other](tmp_path / "other", path)
    assert _statuses(lookup) == ["miss", "miss"]


PLAIN = CLAUDE_FIXTURES / "coding/plain"


def _claude_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    drifted: Callable[[dict[str, Any]], bool] = lambda entry: False,
    interrupt_at: int | None = None,
    session_mismatch: bool = False,
) -> list[dict[str, Any]]:
    """Claude attempts whose usage validates; `drifted` ones report other built-ins.

    With `session_mismatch`, an attempt is recorded as invoke_native records a session that
    differs from its receipt: failed, with the receipt file only.
    """
    stream = (PLAIN / "stream.jsonl").read_text()
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime_identity())
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    monkeypatch.setattr(
        harbor_backend, "regrade_native",
        lambda **kwargs: {"verifier_result": {"rewards": {"reward": 1}}, "exception_info": None},
    )
    # A resume looks for containers a stopped session left running; there are none.
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])
    # The treatment is shaped as Codex packages; these tests need only saved runs.
    monkeypatch.setattr(study, "admit", lambda treatment, harness: native_treatments.admit(
        treatment, "codex"
    ))
    calls: list[dict[str, Any]] = []

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["entry"])
        if len(calls) == interrupt_at:
            raise KeyboardInterrupt("offline interrupt")
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        execution = native_execution(
            area, kwargs["protocol"]["native_task_identity"],
            answer_path="answer.py", timeout=1800, version=CLAUDE_RUNNER["harness_version"],
        )
        artifacts = area / "artifacts"
        (artifacts / "stdout.jsonl").write_text(stream)
        shutil.copytree(PLAIN / "sessions", artifacts / "harbor/trial/agent/sessions")
        coded: dict[str, Any] = {
            **_claude_evidence(artifacts, execution),
            "usage": claude.parse_events(stream, [])["usage"],
        }
        if drifted(kwargs["entry"]):
            # The same CLI, as another plan or account would see it.
            receipt = coded["native_treatment"]
            receipt["builtins"] = {
                **receipt["builtins"], "agents": [*receipt["builtins"]["agents"], "claude-code-guide"],
            }
            (artifacts / "native-treatment.json").write_text(json.dumps(receipt))
        if session_mismatch:
            del coded["native_treatment"]
            coded.update(
                treatment_error="the Claude coding session differs from its discovery receipt",
                successful_native_completion=False,
            )
        return coded

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    return calls


def _attempt_json(root: Path, attempt_id: str, name: str) -> dict[str, Any]:
    value = json.loads((root / "attempts" / attempt_id / "handoff" / name).read_text())
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("recorded", ["receipt", "session-mismatch"])
def test_a_fresh_claude_attempt_with_other_built_ins_stops_a_run_that_reuses_vanilla(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str
) -> None:
    cache = tmp_path / "cache"
    _claude_backend(monkeypatch)
    one = _study(tmp_path / "one", runner=CLAUDE_RUNNER)
    study.run_study(one, tmp_path / "first", baseline_cache=cache,
                    baseline_identity=_claude_identity(one.tasks))
    (cohort,) = [path for path in cache.iterdir() if not path.name.startswith(".")]
    calls = _claude_backend(
        monkeypatch, drifted=lambda entry: True, session_mismatch=recorded == "session-mismatch"
    )
    two = _study(tmp_path / "two", guidance="another", runner=CLAUDE_RUNNER)
    root = tmp_path / "second"
    with pytest.raises(study.BuiltinsDiffer) as raised:
        study.run_study(two, root, baseline_cache=cache,
                        baseline_identity=_claude_identity(two.tasks))
    message = str(raised.value)
    assert "is not scored and the run stops: this run reuses the cached vanilla attempts" in message
    assert f"(cache/{cohort.name}/" in message
    assert "Resume the run once Claude Code reports the pinned built-ins again" in message
    assert f"move cache/{cohort.name} out of the PluginBench cache root and start a new run" in (
        message
    )
    # The first fresh attempt stops the run; no other attempt starts.
    [attempt] = [entry["id"] for entry in calls]
    assert json.loads((root / "run.json").read_text())["attempt_states"][attempt] == "interrupted"
    execution = _attempt_json(root, attempt, "execution.json")
    assert execution["successful_native_completion"] is False
    assert execution["usage"] is None
    assert "reuses the cached vanilla" in execution["treatment_error"]
    revalidated = saved_usage.revalidate_usage(
        execution, root / "attempts" / attempt / "handoff/execution.json"
    )
    assert revalidated["usage_validation"]["status"] == "unavailable"
    assert any("reuses the cached vanilla" in reason
               for reason in revalidated["usage_validation"]["reasons"])


def test_a_fully_fresh_claude_run_with_other_built_ins_still_scores_and_says_why_nothing_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing cached is mixed in, so the run is comparable within itself.
    _claude_backend(monkeypatch, drifted=lambda entry: True)
    cache = tmp_path / "cache"
    progress: list[str] = []
    one = _study(tmp_path / "one", runner=CLAUDE_RUNNER)
    root = study.run_study(one, tmp_path / "run", baseline_cache=cache, progress=progress.append,
                           baseline_identity=_claude_identity(one.tasks))
    report = json.loads((root / "report/assessment-report.json").read_text())
    for row in report["attempts"]:
        assert _attempt_json(root, row["id"], "correctness.json")["checks"]["native_completion"]
        assert row["stages"][0]["work"]["usage_validation"]["status"] == "validated"
    assert not list(cache.glob("*/*/manifest.json"))
    skipped = [line for line in progress if " was not cached in " in line]
    assert len(skipped) == 2
    assert all(line.endswith("its saved evidence does not match the vanilla cache identity")
               for line in skipped)


def test_a_resumed_claude_run_with_attempts_under_other_built_ins_reuses_no_cached_vanilla(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first session misses the cache and codes under other built-ins; it stops midway.
    cache = tmp_path / "cache"
    _claude_backend(monkeypatch, drifted=lambda entry: True, interrupt_at=3)
    one = _study(tmp_path / "one", runner=CLAUDE_RUNNER)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        study.run_study(one, root, baseline_cache=cache,
                        baseline_identity=_claude_identity(one.tasks))
    states = json.loads((root / "run.json").read_text())["attempt_states"]
    arms = {entry["id"]: entry["arm"] for entry in json.loads((root / "plan.json").read_text())[
        "entries"
    ]}
    assert any(state == "complete" for state in states.values())
    assert any(arms[attempt] == "baseline" and state != "complete"
               for attempt, state in states.items())
    # Meanwhile another run caches both vanilla repeats under the pinned built-ins.
    _claude_backend(monkeypatch)
    seed = _study(tmp_path / "seed", guidance="seed", runner=CLAUDE_RUNNER)
    study.run_study(seed, tmp_path / "seed-run", baseline_cache=cache,
                    baseline_identity=_claude_identity(seed.tasks))
    assert len(list(cache.glob("*/*/manifest.json"))) == 2
    progress: list[str] = []
    study.resume_study(root, baseline_cache=cache, progress=progress.append,
                       baseline_identity=_claude_identity(study._frozen_study(root).tasks))
    assert json.loads((root / "run.json").read_text())["baseline_origin"] == {}
    assert any(line.startswith("Cached vanilla attempts are not reused") for line in progress)


def test_a_resumed_claude_run_whose_own_vanilla_is_cached_keeps_caching_its_vanilla(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The plan codes vanilla repeat 2, a treatment, the other treatment, then vanilla repeat 1.
    # The first session caches repeat 2 under the pinned built-ins, codes a treatment under
    # other ones, and stops; nothing it codes was reused, so nothing is mixed.
    cache = tmp_path / "cache"
    _claude_backend(monkeypatch, drifted=lambda entry: entry["arm"] != "baseline", interrupt_at=3)
    one = _study(tmp_path / "one", runner=CLAUDE_RUNNER)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        study.run_study(one, root, baseline_cache=cache,
                        baseline_identity=_claude_identity(one.tasks))
    assert len(list(cache.glob("*/*/manifest.json"))) == 1
    progress: list[str] = []
    _claude_backend(monkeypatch)
    study.resume_study(root, baseline_cache=cache, progress=progress.append,
                       baseline_identity=_claude_identity(study._frozen_study(root).tasks))
    # Repeat 2's entry is the run's own, not a reuse: repeat 1 is cached too.
    assert not any(line.startswith("Cached vanilla attempts are not reused") for line in progress)
    assert len(list(cache.glob("*/*/manifest.json"))) == 2


def test_a_claude_run_prepared_before_the_claude_identity_resumes_into_the_claude_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Decided: the run binds the Claude identity from its frozen recipe, as a new run does,
    # so its vanilla repeats are cached under the key a fresh `check` computes.
    cache = tmp_path / "cache"
    one = _study(tmp_path / "one", runner=CLAUDE_RUNNER)
    # inputs/vanilla-identity.json as fed4eba saved it for a Claude run, which `_code` passes
    # to run_study and resume_study verbatim.
    saved = _claude_identity(one.tasks)
    saved.pop("claude")
    saved.update(
        delivery=native_treatments.DELIVERY_POLICY,
        capabilities=NativeCapabilities().model_dump(mode="json"),
    )
    _claude_backend(monkeypatch, interrupt_at=3)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        study.run_study(one, root, baseline_cache=cache, baseline_identity=saved)
    _claude_backend(monkeypatch)
    study.resume_study(root, baseline_cache=cache, baseline_identity=saved)
    assert len(list(cache.glob("*/*/manifest.json"))) == 2
    two = _study(tmp_path / "two", guidance="another", runner=CLAUDE_RUNNER)
    preview = baselines.preview(
        path=cache, tasks=two.tasks, repeats=2, identity=_claude_identity(two.tasks)
    )
    assert [row["status"] for row in preview] == ["hit", "hit"]
    decisions = json.loads((root / "plan.json").read_text())["baseline_cache"]
    assert {decision["cache_key"] for decision in decisions.values()} == {
        row["cache_key"] for row in preview
    }


def test_a_claude_attempt_reports_its_pinned_launch_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pluginbench import claude, claude_launch, claude_treatments

    fixture = Path(__file__).parent / "fixtures/claude-2.1.322/coding/plain"
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime_identity())
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    monkeypatch.setattr(
        harbor_backend, "regrade_native",
        lambda **kwargs: {"verifier_result": {"rewards": {"reward": 1}}, "exception_info": None},
    )
    # What invoke_native records for a Claude launch; test_claude_launch.py proves it does.
    launch = {
        "harness": "claude-cli", "requested_model": "claude-opus-5-5", "requested_effort": "medium",
        "harness_version": CLAUDE_RUNNER["harness_version"],
        "delivery": claude_treatments.DELIVERY_POLICY,
        "capabilities": claude_launch.capabilities(True),
        "argv": claude.coding_launch(claude.CodingRun("claude-opus-5-5", "medium", True, 1800))[0],
    }

    def invoke(**kwargs: Any) -> dict[str, Any]:
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        execution = native_execution(
            area, kwargs["protocol"]["native_task_identity"],
            answer_path="answer.py", timeout=1800, version=CLAUDE_RUNNER["harness_version"],
        )
        (area / "artifacts/stdout.jsonl").write_text((fixture / "stream.jsonl").read_text())
        execution.update(
            claude.parse_events((fixture / "stream.jsonl").read_text(), [], coding=True),
            launch=launch,
        )
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    monkeypatch.setattr(study, "admit", lambda treatment, harness: native_treatments.admit(
        treatment, "codex"
    ))
    root = study.run_study(_study(tmp_path / "one", runner=CLAUDE_RUNNER), tmp_path / "run")
    study.report_study(root)
    report = json.loads((root / "report/assessment-report.json").read_text())
    for row in report["attempts"]:
        provenance = row["provenance"]
        assert provenance["guidance_delivery"] == "claude-native-v1"
        assert provenance["comparison_reasoning_effort"] == "medium"
        assert provenance["launch_harness_version"] == "2.1.322"
        assert provenance["capabilities"] == {
            "subagents": True, "max_threads": "not_enforced", "max_depth": "not_enforced",
        }
