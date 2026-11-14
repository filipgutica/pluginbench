"""Source-only correctness: the native verifier again, over the submission without its tests.

A DeepSWE verifier replaces only the test files its hidden `test.patch` touches. Every other
test file the agent wrote stays and is built or collected with the hidden suite, so an agent
test that calls a helper from a replaced file, or redeclares a hidden name, fails a correct
solution. This grade leaves the agent's test files out of the saved `model.patch` and replays
the unchanged native verifier in the frozen grader image. The native grade stays as it is; this
one is recorded beside it.

A file the task's reference solution changes, and its hidden tests do not, is kept even when it
looks like a test: such a file is production code (bandit's `core/test_set.py`) or a test edit a
correct solution needs (an existing test that must follow a changed signature).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pluginbench import harbor_backend, isolation, study, submissions
from pluginbench.code_diff import _is_test_path, without_test_files
from pluginbench.datasets import TaskSpec, load_tasks, native_identity

METHOD = "native verifier over model.patch without test files"
DIRECTORY = "source-only"
# Bump whenever what the filter leaves out changes. A run graded by another revision is graded
# again into its own directory; earlier revisions stay as evidence.
#   1: the shared test-file classifier, in `source-only/` itself.
#   2: also keeps the reference solution's non-test-patch files, and leaves out every conftest.py.
FILTER_REVISION = 2
# Kept, but listed in the grade, so a source-only pass that changed them is visible.
TEST_SETUP_DIRECTORIES = frozenset({"testdata", "fixtures", "examples"})
TEST_SETUP_FILENAMES = ("pyproject.toml", "package.json", "jest.config.*", "pytest.ini", "tox.ini")


def _directory(evidence: Path) -> Path:
    return evidence / DIRECTORY / f"filter-{FILTER_REVISION}"


@dataclass(frozen=True)
class _Prepared:
    """A stage's grade so far, and the filtered patch when only a replay can finish it."""

    grade: dict[str, Any]
    replay: bytes | None = None


def _looks_like_test(path: str) -> bool:
    return _is_test_path(path) or PurePosixPath(path).name == "conftest.py"


def _task_paths(native_task: Path) -> tuple[set[str], set[str]]:
    """The paths the hidden tests replace, and the other paths the reference solution changes."""
    hidden = submissions._patch_paths((native_task / "tests/test.patch").read_text())
    reference = submissions._patch_paths((native_task / "solution/solution.patch").read_text())
    return hidden, reference - hidden


def _without_tests(patch: str, reference: set[str]) -> tuple[str, list[str]]:
    """Leave out each test-looking file but those in `reference`; see `without_test_files`."""
    return without_test_files(
        patch, is_test=lambda path: path not in reference and _looks_like_test(path)
    )


def _is_test_setup(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in TEST_SETUP_DIRECTORIES for part in parts[:-1]) or any(
        fnmatch.fnmatchcase(parts[-1], pattern) for pattern in TEST_SETUP_FILENAMES
    )


def _prepare(
    *, evidence: Path, attempt_id: str, stage_id: str, run: dict[str, Any], task: TaskSpec
) -> _Prepared:
    """Filter a stage's patch; the grade is final unless `replay` holds a patch to grade."""
    execution_path = study._bound_path(evidence, run["executions"][attempt_id][stage_id])
    execution = study._bound_document(evidence, run["executions"][attempt_id][stage_id])
    native = study._bound_document(evidence, run["verifier_results"][attempt_id][stage_id])
    hashes = execution.get("native_artifact_hashes")
    grade: dict[str, Any] = {
        "schema_version": 1,
        "source_only_filter": FILTER_REVISION,
        "method": METHOD,
        "attempt_id": attempt_id,
        "stage": stage_id,
        "native_verifier_result": None,
        "reward": None,
        "model_calls": 0,
    }

    def unavailable(reason: str) -> _Prepared:
        return _Prepared({**grade, "status": "unavailable", "reason": reason})

    if not isinstance(hashes, dict) or submissions.PATCH_ARTIFACT not in hashes:
        return unavailable("the attempt has no hash-bound model.patch to grade without its tests")
    patch_path = execution_path.parent / "artifacts/harbor/trial/artifacts"
    patch_bytes = (patch_path / submissions.PATCH_ARTIFACT).read_bytes()
    native_patch = hashlib.sha256(patch_bytes).hexdigest()
    if native_patch != hashes[submissions.PATCH_ARTIFACT] or native.get("patch_sha256") not in (
        None,
        native_patch,
    ):
        raise ValueError(f"{attempt_id}: the graded model.patch changed after grading")
    grade["native_patch_sha256"] = native_patch
    if not (task.native_task / "solution/solution.patch").is_file():
        return unavailable("task ships no solution/solution.patch to tell its files from tests")
    hidden, reference = _task_paths(task.native_task)
    try:
        filtered, removed = _without_tests(patch_bytes.decode("utf-8"), reference)
    except ValueError as error:
        return unavailable(str(error))
    filtered_bytes = filtered.encode("utf-8")
    kept = submissions._patch_paths(filtered)
    grade.update(
        status="complete",
        filtered_patch_sha256=hashlib.sha256(filtered_bytes).hexdigest(),
        removed_test_files=removed,
        nothing_removed=not removed,
        # Test-looking files kept because the reference solution changes them.
        kept_reference_solution_files=sorted(filter(_looks_like_test, kept & reference)),
        kept_test_setup_files=sorted(path for path in kept if _is_test_setup(path)),
        # Paths the hidden tests still overwrite, which the grade therefore does not see.
        trusted_test_overlap=sorted(kept & hidden),
    )
    if removed:
        return _Prepared(grade, filtered_bytes)
    # The same bytes would grade the same way, so the native result is reused, not rerun.
    verifier = native.get("native_verifier_result")
    grade.update(
        native_result_reused=True,
        native_verifier_result=verifier,
        reward=harbor_backend.binary_native_reward(verifier),
    )
    return _Prepared(grade)


def _replay_unavailable(identity: dict[str, object]) -> str | None:
    """Why the frozen Harbor runtime or grader image cannot replay the verifier now, if so."""
    try:
        expected = harbor_backend.assert_runtime(
            harbor_backend._native_object(identity["isolation"], "native regrade runtime"),
            launcher=False,
        )
    except isolation.RuntimeDriftError as error:
        return str(error)
    grader = harbor_backend._native_object(identity["grader_image_identity"], "grader")
    try:
        harbor_backend._assert_native_image(grader, expected)
    except ValueError as error:  # Docker reports a missing or rebound image as a ValueError.
        return str(error)
    return None


def _replay(
    *, grade: dict[str, Any], patch: bytes, execution: Path, task: TaskSpec, target: Path
) -> None:
    """Grade the filtered patch; only a complete replay takes `target`, and older tries go."""
    artifacts = execution.parent / "artifacts"
    hashes = json.loads(execution.read_text())["native_artifact_hashes"]
    staging = target.with_name(f"{target.name}.try-{uuid4().hex[:12]}")
    trial = staging / "source/harbor/trial"
    shutil.copytree(artifacts / "harbor/trial", trial, symlinks=True)
    (trial / "artifacts" / submissions.PATCH_ARTIFACT).write_bytes(patch)
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
    for failed in target.parent.glob(f"{target.name}.try-*"):
        shutil.rmtree(failed)  # Tries that failed earlier; the complete grade replaces them.


def _stages(evidence: Path) -> Iterator[tuple[str, str, dict[str, Any], TaskSpec]]:
    run = json.loads((evidence / "run.json").read_text())
    tasks = {task.task_id: task for task in load_tasks(evidence / "frozen/tasks.json")}
    entries = {
        entry["id"]: entry for entry in study._bound_document(evidence, run["plan"])["entries"]
    }
    for attempt_id, stages in sorted(run.get("verifier_results", {}).items()):
        for stage_id in sorted(stages):
            yield attempt_id, stage_id, run, tasks[entries[attempt_id]["task_id"]]


def needs_grading(evidence: Path, index: object) -> bool:
    """Whether a run's recorded grades are missing, from another filter, or still pending."""
    if index is None:
        return True
    document = study._bound_document(evidence, index)
    return document.get("source_only_filter", 1) != FILTER_REVISION or bool(document.get("pending"))


def pending_replays(evidence: Path, index: object) -> int:
    """Verifier replays that recording the grades would start now; no Docker is used to count."""
    if not needs_grading(evidence, index):
        return 0
    return sum(
        _prepare(evidence=evidence, attempt_id=attempt, stage_id=stage, run=run, task=task).replay
        is not None
        for attempt, stage, run, task in _stages(evidence)
        if not (_directory(evidence) / attempt / stage / "grade.json").is_file()
    )


def record_source_only_grades(
    evidence: Path, *, progress: Callable[[str], None] | None = None
) -> dict[str, str]:
    """Grade every graded stage of a finished run source-only; return the bound index.

    A stage graded by an earlier call in this filter revision keeps its saved grade. A stage
    whose replay the frozen runtime or grader image cannot run now is left pending, with its
    reason, and graded on a later call.
    """
    directory = _directory(evidence)
    records: dict[str, dict[str, dict[str, str]]] = {}
    pending: dict[str, dict[str, str]] = {}
    for attempt_id, stage_id, run, task in _stages(evidence):
        target = directory / attempt_id / stage_id
        path = target / "grade.json"
        if not path.is_file():
            prepared = _prepare(
                evidence=evidence, attempt_id=attempt_id, stage_id=stage_id, run=run, task=task
            )
            if prepared.replay is None:
                study._write(path, prepared.grade)
            elif (reason := _replay_unavailable(native_identity(task))) is not None:
                pending.setdefault(attempt_id, {})[stage_id] = reason
                continue
            else:
                if progress:
                    progress(f"Grading {task.task_id} attempt {attempt_id} source-only")
                _replay(
                    grade=prepared.grade,
                    patch=prepared.replay,
                    execution=study._bound_path(evidence, run["executions"][attempt_id][stage_id]),
                    task=task,
                    target=target,
                )
        records.setdefault(attempt_id, {})[stage_id] = study._reference(evidence, path)
    index = directory / "index.json"
    study._write(
        index,
        {
            "schema_version": 1,
            "source_only_filter": FILTER_REVISION,
            "method": METHOD,
            "records": records,
            "pending": pending,
        },
    )
    return study._reference(evidence, index)


def attach_source_only_grades(
    evidence: Path, index: object, attempts: list[dict[str, Any]]
) -> None:
    """Add each stage's bound source-only grade to report attempts, beside its native grade."""
    document = study._bound_document(evidence, index)
    records, pending = document["records"], document.get("pending", {})
    for attempt in attempts:
        for stage in attempt["stages"]:
            reference = records.get(attempt["id"], {}).get(stage["id"])
            reason = pending.get(attempt["id"], {}).get(stage["id"])
            if reference is not None:
                stage["source_only_grading"] = study._bound_document(evidence, reference)
            elif reason is not None:
                stage["source_only_grading"] = {
                    "source_only_filter": document["source_only_filter"],
                    "status": "unavailable",
                    "reason": reason,
                    "reward": None,
                }
