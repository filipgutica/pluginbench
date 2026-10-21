from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from pluginbench import baselines, harbor_backend, study, submissions
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.datasets import TaskSpec, native_identity, portable_native_identity
from native_support import (
    task_entry,
    runtime_identity,
    execution as native_execution,
    synthetic_contract,
)


@pytest.fixture(autouse=True)
def synthetic_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)


def _study(directory: Path, *, guidance: str = "candidate") -> study.ResolvedStudy:
    directory.mkdir()
    entry = task_entry(directory, "answer", answer_path="answer.py", version="0.143.0")
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
            }
        )
    )
    return study.load_study(recipe)


def _coding(tasks: tuple[TaskSpec, ...]) -> dict[str, object]:
    return {
        "delivery": "native-instructions-skills-plugins-v1",
        "capabilities": {"subagents": True, "max_threads": 4, "max_depth": 1},
        "isolation": runtime_identity(),
        "harness_version": "0.143.0",
        "timeout_seconds": 1800,
        "study": {"harness": "codex", "model": "gpt-6.1-sol", "reasoning": "medium"},
        "task_environment": {
            task.task_id: portable_native_identity(native_identity(task)) for task in tasks
        },
    }


def _cohort(
    directory: Path, cache_path: Path
) -> tuple[baselines.BaselineCache, tuple[TaskSpec, ...], list[dict[str, Any]]]:
    directory.mkdir()
    tasks = _study(directory / "config").tasks
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


def _publish(
    cache: baselines.BaselineCache, tasks: tuple[TaskSpec, ...], records: list[dict[str, Any]]
) -> None:
    try:
        cache.reserve().publish(records, tasks)
    finally:
        cache.close()


def test_native_cache_reuses_failed_attempts_and_unknown_usage_without_observers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    _publish(first, tasks, records)
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
        "runtime",
        "toolchain",
        "trusted_tests",
        "source_policy",
        "delivery",
        "capabilities",
    ],
)
def test_changed_native_coding_identity_misses(tmp_path: Path, field: str) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    _publish(first, tasks, records)
    other, _, _ = _cohort(tmp_path / "second", path)
    context = json.loads(json.dumps(other.contexts["answer"]))
    coding = context["coding"]
    native = coding["native_task_identity"]
    if field == "source_policy":
        context["source_policy"] = "old working tree source policy"
    elif field in {"model", "reasoning", "jobs"}:
        coding["study"][field] = "different"
    elif field in {"harness_version", "timeout_seconds", "delivery", "capabilities"}:
        coding[field] = "different"
    elif field == "grader_image":
        native["grader_image_identity"]["image_id"] = "sha256:" + "9" * 64
    elif field == "runtime":
        coding["isolation"]["docker_engine"]["id"] = "new daemon"
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
    assert baselines._hash(context) != first.keys["answer"]


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
        "native-prompt",
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
            _publish(cache, tasks, records)
    else:
        _publish(cache, tasks, records)
    assert not list(path.glob("*/manifest.json"))


def test_cached_artifact_tampering_is_rejected_before_import(tmp_path: Path) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    _publish(first, tasks, records)
    cohort = path / first.keys["answer"]
    (cohort / "attempts/baseline-1/handoff/artifacts/submission-source.json").write_text("tampered")
    second, _, _ = _cohort(tmp_path / "second", path)
    with pytest.raises(ValueError, match="cohort changed"):
        second.reserve()


def test_missing_cohort_lock_and_partial_publication_preserve_full_repeats(tmp_path: Path) -> None:
    path = tmp_path / "cache"
    first, tasks, records = _cohort(tmp_path / "first", path)
    second, _, _ = _cohort(tmp_path / "second", path)
    try:
        first.reserve()
        with pytest.raises(ValueError, match="cohort is in use"):
            second.reserve()
        with pytest.raises(ValueError):
            first.publish(records[:1], tasks)
        assert not list(path.glob("*/manifest.json"))
    finally:
        first.close()
    try:
        second.reserve()
        assert all(row["status"] == "miss" for row in second.decisions().values())
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
    assert baseline[1]["stages"][0]["work"]["tokens"] == {"input_tokens": 13}
    correctness = second / baseline[0]["stages"][0]["correctness"]["path"]
    assert json.loads(correctness.read_text())["checks"]["native_completion"] is False
    assert all(item["origin_run"] == str(first) for item in run["baseline_origin"].values())
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
