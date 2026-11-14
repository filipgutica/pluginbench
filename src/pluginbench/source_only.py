"""Source-only correctness: the native verifier again, over the submission without its tests.

A DeepSWE verifier replaces only the test files its hidden `test.patch` touches. Every other
test file the agent wrote stays and is built or collected with the hidden suite, so an agent
test that calls a helper from a replaced file, or redeclares a hidden name, fails a correct
solution. This grade leaves every test file out of the saved `model.patch`, using the same
classifier as the test-line metrics, and replays the unchanged native verifier in the frozen
grader image. The native grade stays as it is; this one is recorded beside it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from pluginbench import harbor_backend, study, submissions
from pluginbench.code_diff import without_test_files
from pluginbench.datasets import TaskSpec, load_tasks, native_identity

METHOD = "native verifier over model.patch without test files"
DIRECTORY = "source-only"


def _stage_grade(
    *,
    evidence: Path,
    attempt_id: str,
    stage_id: str,
    run: dict[str, Any],
    task: TaskSpec,
    target: Path,
) -> None:
    """Write the stage's grade to `target/grade.json`; only a complete grade takes that path."""
    execution_path = study._bound_path(evidence, run["executions"][attempt_id][stage_id])
    execution = study._bound_document(evidence, run["executions"][attempt_id][stage_id])
    native = study._bound_document(evidence, run["verifier_results"][attempt_id][stage_id])
    artifacts = execution_path.parent / "artifacts"
    hashes = execution.get("native_artifact_hashes")
    grade: dict[str, Any] = {
        "schema_version": 1,
        "method": METHOD,
        "attempt_id": attempt_id,
        "stage": stage_id,
        "native_verifier_result": None,
        "reward": None,
        "model_calls": 0,
    }
    if not isinstance(hashes, dict) or submissions.PATCH_ARTIFACT not in hashes:
        reason = "the attempt has no hash-bound model.patch to grade without its tests"
        study._write(target / "grade.json", {**grade, "status": "unavailable", "reason": reason})
        return
    patch_bytes = (artifacts / "harbor/trial/artifacts" / submissions.PATCH_ARTIFACT).read_bytes()
    native_patch = hashlib.sha256(patch_bytes).hexdigest()
    if native_patch != hashes[submissions.PATCH_ARTIFACT] or native.get("patch_sha256") not in (
        None,
        native_patch,
    ):
        raise ValueError(f"{attempt_id}: the graded model.patch changed after grading")
    grade["native_patch_sha256"] = native_patch
    try:
        filtered, removed = without_test_files(patch_bytes.decode("utf-8"))
    except ValueError as error:
        study._write(
            target / "grade.json", {**grade, "status": "unavailable", "reason": str(error)}
        )
        return
    filtered_bytes = filtered.encode("utf-8")
    hidden = submissions._patch_paths((task.native_task / "tests/test.patch").read_text())
    grade.update(
        status="complete",
        filtered_patch_sha256=hashlib.sha256(filtered_bytes).hexdigest(),
        removed_test_files=removed,
        nothing_removed=not removed,
        # Paths the hidden tests still overwrite, which the grade therefore does not see.
        trusted_test_overlap=sorted(submissions._patch_paths(filtered) & hidden),
    )
    if not removed:
        # The same bytes would grade the same way, so the native result is reused, not rerun.
        verifier = native.get("native_verifier_result")
        grade.update(
            native_result_reused=True,
            native_verifier_result=verifier,
            reward=harbor_backend.binary_native_reward(verifier),
        )
        study._write(target / "grade.json", grade)
        return
    # A try that fails keeps its files; only a complete one takes the stage's name.
    staging = target.with_name(f"{stage_id}.try-{uuid4().hex[:12]}")
    trial = staging / "source/harbor/trial"
    shutil.copytree(artifacts / "harbor/trial", trial, symlinks=True)
    (trial / "artifacts" / submissions.PATCH_ARTIFACT).write_bytes(filtered_bytes)
    regraded = harbor_backend.regrade_native(
        artifacts=staging / "source",
        task_dir=task.native_task,
        identity=native_identity(task),
        directory=staging / "regrade",
        # regrade_native proves the copy equals the graded trial but for the filtered patch.
        artifact_hashes={**hashes, submissions.PATCH_ARTIFACT: grade["filtered_patch_sha256"]},
    )
    verifier = regraded.get("verifier_result") if regraded.get("exception_info") is None else None
    grade.update(
        native_result_reused=False,
        native_verifier_result=verifier,
        reward=harbor_backend.binary_native_reward(verifier),
        regrade=regraded,
    )
    study._write(staging / "grade.json", grade)
    staging.rename(target)


def record_source_only_grades(
    evidence: Path, *, progress: Callable[[str], None] | None = None
) -> dict[str, str]:
    """Grade every graded stage of a finished run source-only; return the bound index.

    A stage graded by an earlier, interrupted call keeps its saved grade.
    """
    run = json.loads((evidence / "run.json").read_text())
    tasks = {task.task_id: task for task in load_tasks(evidence / "frozen/tasks.json")}
    entries = {
        entry["id"]: entry for entry in study._bound_document(evidence, run["plan"])["entries"]
    }
    records: dict[str, dict[str, dict[str, str]]] = {}
    for attempt_id, stages in sorted(run.get("verifier_results", {}).items()):
        for stage_id in sorted(stages):
            target = evidence / DIRECTORY / attempt_id / stage_id
            path = target / "grade.json"
            if not path.is_file():
                task = tasks[entries[attempt_id]["task_id"]]
                if progress:
                    progress(f"Grading {task.task_id} attempt {attempt_id} source-only")
                _stage_grade(
                    evidence=evidence,
                    attempt_id=attempt_id,
                    stage_id=stage_id,
                    run=run,
                    task=task,
                    target=target,
                )
            records.setdefault(attempt_id, {})[stage_id] = study._reference(evidence, path)
    index = evidence / DIRECTORY / "index.json"
    study._write(index, {"schema_version": 1, "method": METHOD, "records": records})
    return study._reference(evidence, index)


def attach_source_only_grades(
    evidence: Path, index: object, attempts: list[dict[str, Any]]
) -> None:
    """Add each stage's bound source-only grade to report attempts, beside its native grade."""
    records = study._bound_document(evidence, index)["records"]
    for attempt in attempts:
        for stage in attempt["stages"]:
            reference = records.get(attempt["id"], {}).get(stage["id"])
            if reference is not None:
                stage["source_only_grading"] = study._bound_document(evidence, reference)
