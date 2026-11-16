"""Source-only correctness: the native verifier again, over the submission without its tests.

This is the attempt's one grade (grade contract 1; see `grade.py`). A DeepSWE verifier replaces
only the test files its hidden `test.patch` touches. Every other test file the agent wrote stays
and is built or collected with the hidden suite, so an agent test that calls a helper from a
replaced file, or redeclares a hidden name, fails a correct solution. This grade leaves the
agent's test files out of the saved `model.patch` and replays the unchanged native verifier in
the frozen grader image. The native reward stays beside it as an attribute.

A file the task's reference solution changes, and its hidden tests do not, is kept even when it
looks like a test: such a file is production code (bandit's `core/test_set.py`) or a test edit a
correct solution needs (an existing test that must follow a changed signature).

What this grade cannot decide is infrastructure, held in the index's `pending` and graded again
by the next run: missing or changed evidence, a replay the frozen runtime cannot run now, and a
fail-shaped outcome (a verifier that still crashes, or a policy violation) whose task's nop and
oracle do not requalify.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pluginbench import grade, harbor_backend, isolation, study, submissions
from pluginbench.code_diff import TestRename, _is_test_path, without_test_files
from pluginbench.datasets import TaskSpec, load_tasks, native_identity

METHOD = "native verifier over model.patch without test files"
DIRECTORY = "source-only"
# Bump whenever what the filter leaves out changes. A run graded by another revision is graded
# again into its own directory; earlier revisions stay as evidence.
#   1: the shared test-file classifier, in `source-only/` itself.
#   2: also keeps the reference solution's non-test-patch files, and leaves out every conftest.py.
#   3: paths as Git reads them; grade contract 1 records (passed, fail_reason, attributes).
FILTER_REVISION = 3
# Kept, but listed in the grade, so a source-only pass that changed them is visible.
TEST_SETUP_DIRECTORIES = frozenset({"testdata", "fixtures", "examples"})
TEST_SETUP_FILENAMES = ("pyproject.toml", "package.json", "jest.config.*", "pytest.ini", "tox.ini")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# Why a task's nop and oracle do not requalify now, or None when both do.
Requalify = Callable[[TaskSpec], str | None]


def _directory(evidence: Path) -> Path:
    return evidence / DIRECTORY / f"filter-{FILTER_REVISION}"


class _Infrastructure(Exception):
    """This pass cannot grade the stage; the next run grades it again. Never a result."""


@dataclass(frozen=True)
class _Prepared:
    """A stage's grade so far, and the patch to replay when no reward can be reused."""

    grade: dict[str, Any]
    replay: bytes | None = None


def _looks_like_test(path: str) -> bool:
    return _is_test_path(path) or PurePosixPath(path).name == "conftest.py"


def _task_paths(native_task: Path) -> tuple[set[str], set[str]]:
    """The paths the hidden tests replace, and the other paths the reference solution changes."""
    hidden = submissions.task_patch_paths(native_task / "tests/test.patch")
    reference = submissions.task_patch_paths(native_task / "solution/solution.patch")
    return hidden, reference - hidden


def left_out(path: str, reference: set[str]) -> bool:
    """Whether this grade leaves `path` out: it looks like a test and `reference` lacks it."""
    return path not in reference and _looks_like_test(path)


def _without_tests(patch: bytes, reference: set[str]) -> tuple[bytes, list[str]]:
    """Leave out each test-looking file but those in `reference`; see `without_test_files`."""
    try:
        filtered, removed = without_test_files(
            patch.decode("utf-8", errors="surrogateescape"),
            is_test=lambda path: left_out(path, reference),
            paths=submissions.section_paths(patch),
        )
    except TestRename as error:
        raise submissions.PolicyViolation("test_rename", str(error)) from None
    return filtered.encode("utf-8", errors="surrogateescape"), removed


def _is_test_setup(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in TEST_SETUP_DIRECTORIES for part in parts[:-1]) or any(
        fnmatch.fnmatchcase(parts[-1], pattern) for pattern in TEST_SETUP_FILENAMES
    )


def _record(
    *, attempt_id: str, stage_id: str, execution: dict[str, Any], patch_sha256: object
) -> dict[str, Any]:
    """A grade record's common part: what was graded and the attempt's attributes."""
    return {
        "schema_version": 1,
        "grade_contract": grade.GRADE_CONTRACT,
        "source_only_filter": FILTER_REVISION,
        "method": METHOD,
        "attempt_id": attempt_id,
        "stage": stage_id,
        "native_verifier_result": None,
        "reward": None,
        "model_calls": 0,
        "attributes": {
            "native_reward": harbor_backend.binary_native_reward(
                execution.get("native_verifier_result")
            ),
            "test_overlap": False,
            "empty_patch": patch_sha256 == _EMPTY_SHA256,
            **grade.attributes(execution),
        },
    }


def _prepare(
    *, evidence: Path, attempt_id: str, stage_id: str, run: dict[str, Any], task: TaskSpec
) -> _Prepared:
    """Filter a graded stage's patch; `replay` holds the patch to grade when no reward is reused.

    Raises `_Infrastructure` for missing or changed evidence and `PolicyViolation` for a patch
    this grade cannot filter.
    """
    execution_path = study._bound_path(evidence, run["executions"][attempt_id][stage_id])
    execution = study._bound_document(evidence, run["executions"][attempt_id][stage_id])
    native = study._bound_document(evidence, run["verifier_results"][attempt_id][stage_id])
    hashes = execution.get("native_artifact_hashes")
    if not isinstance(hashes, dict) or submissions.PATCH_ARTIFACT not in hashes:
        raise _Infrastructure("the attempt has no hash-bound model.patch to grade")
    patch_path = execution_path.parent / "artifacts/harbor/trial/artifacts"
    patch_bytes = (patch_path / submissions.PATCH_ARTIFACT).read_bytes()
    native_patch = hashlib.sha256(patch_bytes).hexdigest()
    if native_patch != hashes[submissions.PATCH_ARTIFACT] or native.get("patch_sha256") not in (
        None,
        native_patch,
    ):
        raise _Infrastructure("the graded model.patch changed after grading")
    record = _record(
        attempt_id=attempt_id, stage_id=stage_id, execution=execution, patch_sha256=native_patch
    )
    record["native_patch_sha256"] = native_patch
    if not (task.native_task / "solution/solution.patch").is_file():
        raise _Infrastructure("task ships no solution/solution.patch to tell its files from tests")
    try:
        hidden, reference = _task_paths(task.native_task)
    except (OSError, ValueError) as error:
        raise _Infrastructure(str(error)) from None
    try:
        filtered, removed = _without_tests(patch_bytes, reference)
        kept = submissions.patch_paths(filtered)
    except submissions.PolicyViolation:
        raise
    except (OSError, ValueError) as error:
        raise _Infrastructure(str(error)) from None
    record.update(
        filtered_patch_sha256=hashlib.sha256(filtered).hexdigest(),
        removed_test_files=removed,
        nothing_removed=not removed,
        # Test-looking files kept because the reference solution changes them.
        kept_reference_solution_files=sorted(filter(_looks_like_test, kept & reference)),
        kept_test_setup_files=sorted(path for path in kept if _is_test_setup(path)),
        # Paths the hidden tests still overwrite, which the grade therefore does not see.
        trusted_test_overlap=sorted(kept & hidden),
    )
    record["attributes"]["test_overlap"] = bool(record["trusted_test_overlap"])
    if removed:
        return _Prepared(record, filtered)
    verifier = native.get("native_verifier_result")
    reward = harbor_backend.binary_native_reward(verifier)
    if reward is None:
        # A verifier that gave no 0 or 1, a timed-out attempt's included, is regraded once.
        return _Prepared(record, filtered)
    # The same bytes would grade the same way, so the native result is reused, not rerun.
    record.update(native_result_reused=True, native_verifier_result=verifier, reward=reward)
    return _Prepared(record)


def _policy_violation(
    *, evidence: Path, attempt_id: str, stage_id: str, run: dict[str, Any], task: TaskSpec
) -> tuple[dict[str, Any], str]:
    """The record and violation kind of a stage frozen without a snapshot.

    The kind is found again from the hash-bound evidence, not read from a saved message, so a
    vanilla attempt reused from an earlier run is graded the same way. Any other reason a
    stage has no snapshot is infrastructure.
    """
    execution_path = study._bound_path(evidence, run["executions"][attempt_id][stage_id])
    execution = study._bound_document(evidence, run["executions"][attempt_id][stage_id])
    study._bound_document(evidence, run["submission_errors"][attempt_id][stage_id])
    with tempfile.TemporaryDirectory(prefix="pluginbench-policy-") as temporary:
        try:
            submissions.reconstruct(
                before=task.fixture,
                task_dir=task.native_task,
                identity=native_identity(task),
                artifacts=execution_path.parent / "artifacts",
                execution=execution,
                destination=Path(temporary).resolve() / "source",
            )
        except submissions.PolicyViolation as violation:
            kind, detail = violation.kind, str(violation)
        except (OSError, ValueError) as error:
            raise _Infrastructure(f"the submission could not be frozen: {error}") from None
        else:
            raise _Infrastructure("the submission has no snapshot, yet it can be frozen now")
    hashes = execution.get("native_artifact_hashes")
    patch = hashes.get(submissions.PATCH_ARTIFACT) if isinstance(hashes, dict) else None
    record = _record(
        attempt_id=attempt_id, stage_id=stage_id, execution=execution, patch_sha256=patch
    )
    return {**record, "policy_violation": detail}, kind


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
    *, record: dict[str, Any], patch: bytes, execution: Path, task: TaskSpec, target: Path
) -> Path:
    """Grade `patch` in a staging directory beside `target`; return that directory."""
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
        # regrade_native proves the copy equals the graded trial but for the replayed patch.
        artifact_hashes={**hashes, submissions.PATCH_ARTIFACT: record["filtered_patch_sha256"]},
    )
    verifier = regraded.get("verifier_result") if regraded.get("exception_info") is None else None
    record.update(
        native_result_reused=False,
        native_verifier_result=verifier,
        reward=harbor_backend.binary_native_reward(verifier),
        regrade=regraded,
    )
    return staging


def _keep(staging: Path | None, target: Path, record: dict[str, Any]) -> None:
    """Write a final grade; only a complete grade takes `target`, and older tries go."""
    if staging is None:
        study._write(target / "grade.json", record)
        return
    study._write(staging / "grade.json", record)
    staging.rename(target)
    for failed in target.parent.glob(f"{target.name}.try-*"):
        shutil.rmtree(failed)  # Tries that failed earlier; the complete grade replaces them.


def _stages(evidence: Path) -> Iterator[tuple[str, str, dict[str, Any], TaskSpec]]:
    """Every stage of every complete attempt, with its run and task."""
    run = json.loads((evidence / "run.json").read_text())
    tasks = {task.task_id: task for task in load_tasks(evidence / "frozen/tasks.json")}
    entries = {
        entry["id"]: entry for entry in study._bound_document(evidence, run["plan"])["entries"]
    }
    states = run.get("attempt_states", {})
    for attempt_id, stages in sorted(run.get("executions", {}).items()):
        if states.get(attempt_id, "complete") != "complete":
            continue
        for stage_id in sorted(stages):
            yield attempt_id, stage_id, run, tasks[entries[attempt_id]["task_id"]]


def _graded_run(evidence: Path) -> bool:
    run = json.loads((evidence / "run.json").read_text())
    return study.is_graded_run(study._bound_document(evidence, run["plan"]))


def needs_grading(evidence: Path, index: object) -> bool:
    """Whether a run's grades are missing, from another filter, or have infrastructure errors.

    A legacy run keeps the grades it has: it is never graded under this contract.
    """
    if not _graded_run(evidence):
        return False
    if index is None:
        return True
    document = study._bound_document(evidence, index)
    return document.get("source_only_filter", 1) != FILTER_REVISION or bool(document.get("pending"))


def pending_replays(evidence: Path, index: object) -> int:
    """Verifier replays that grading would start now, at most; no Docker is used to count."""
    if not needs_grading(evidence, index):
        return 0
    count = 0
    for attempt, stage, run, task in _stages(evidence):
        if (_directory(evidence) / attempt / stage / "grade.json").is_file():
            continue
        if stage not in run.get("verifier_results", {}).get(attempt, {}):
            continue  # A policy violation; it needs no replay.
        try:
            prepared = _prepare(
                evidence=evidence, attempt_id=attempt, stage_id=stage, run=run, task=task
            )
        except (_Infrastructure, submissions.PolicyViolation):
            continue
        count += prepared.replay is not None
    return count


def record_source_only_grades(
    evidence: Path,
    *,
    requalify: Requalify,
    progress: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Grade every stage of a finished run's complete attempts; return the bound index.

    A stage graded by an earlier call in this filter revision keeps its grade. A stage this
    call cannot grade is an infrastructure error, held in `pending` with its reason and graded
    again on a later call; `progress` gets one line naming how many wait and why. `requalify`
    runs the task's nop and oracle at most once per task in a call, and only for a fail that
    needs a healthy task.
    """
    directory = _directory(evidence)
    records: dict[str, dict[str, dict[str, str]]] = {}
    pending: dict[str, dict[str, str]] = {}
    health: dict[str, str | None] = {}

    def unhealthy(task: TaskSpec) -> str | None:
        if task.task_id not in health:
            health[task.task_id] = requalify(task)
        return health[task.task_id]

    for attempt_id, stage_id, run, task in _stages(evidence):
        target = directory / attempt_id / stage_id
        path = target / "grade.json"
        if not path.is_file():
            try:
                record, staging = _grade(
                    evidence=evidence,
                    attempt_id=attempt_id,
                    stage_id=stage_id,
                    run=run,
                    task=task,
                    target=target,
                    progress=progress,
                )
                if record.get("fail_reason") is not None:
                    reason = unhealthy(task)
                    if reason is not None:
                        raise _Infrastructure(
                            f"{record['fail_reason']}, but the task's nop and oracle did not "
                            f"requalify: {reason}"
                        )
                _keep(staging, target, record)
            except _Infrastructure as error:
                pending.setdefault(attempt_id, {})[stage_id] = str(error)
                continue
        records.setdefault(attempt_id, {})[stage_id] = study._reference(evidence, path)
    if pending and progress:
        waiting = [reason for stages in pending.values() for reason in stages.values()]
        progress(
            f"{len(waiting)} grades hit an infrastructure error; the next run grades them "
            "again: " + "; ".join(sorted(set(waiting)))
        )
    index = directory / "index.json"
    study._write(
        index,
        {
            "schema_version": 1,
            "grade_contract": grade.GRADE_CONTRACT,
            "source_only_filter": FILTER_REVISION,
            "method": METHOD,
            "records": records,
            "pending": pending,
        },
    )
    return study._reference(evidence, index)


def _grade(
    *,
    evidence: Path,
    attempt_id: str,
    stage_id: str,
    run: dict[str, Any],
    task: TaskSpec,
    target: Path,
    progress: Callable[[str], None] | None,
) -> tuple[dict[str, Any], Path | None]:
    """One stage's grade record and the staging directory a replay left, if any.

    A fail that needs a healthy task carries its `fail_reason`; the caller requalifies it.
    """
    if stage_id not in run.get("verifier_results", {}).get(attempt_id, {}):
        record, kind = _policy_violation(
            evidence=evidence, attempt_id=attempt_id, stage_id=stage_id, run=run, task=task
        )
        return {**record, **_verdict(None, f"{grade.UNSUPPORTED_SUBMISSION}:{kind}")}, None
    try:
        prepared = _prepare(
            evidence=evidence, attempt_id=attempt_id, stage_id=stage_id, run=run, task=task
        )
    except submissions.PolicyViolation as violation:
        execution = study._bound_document(evidence, run["executions"][attempt_id][stage_id])
        hashes = execution.get("native_artifact_hashes") or {}
        record = _record(
            attempt_id=attempt_id,
            stage_id=stage_id,
            execution=execution,
            patch_sha256=hashes.get(submissions.PATCH_ARTIFACT),
        )
        record["policy_violation"] = str(violation)
        fail = f"{grade.UNSUPPORTED_SUBMISSION}:{violation.kind}"
        return {**record, **_verdict(None, fail)}, None
    record, staging = prepared.grade, None
    if prepared.replay is not None:
        if (reason := _replay_unavailable(native_identity(task))) is not None:
            raise _Infrastructure(reason)
        if progress:
            progress(f"Grading {task.task_id} attempt {attempt_id} source-only")
        # A replay with nothing left out regrades a native reward that was not 0 or 1, so it
        # is the one regrade; a filtered patch's first replay gets one regrade of its own.
        tries = 1 if record["nothing_removed"] else 2
        try:
            for _ in range(tries):
                staging = _replay(
                    record=record,
                    patch=prepared.replay,
                    execution=study._bound_path(evidence, run["executions"][attempt_id][stage_id]),
                    task=task,
                    target=target,
                )
                if record["reward"] is not None:
                    break
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise _Infrastructure(f"the verifier replay failed: {error}") from None
    return {**record, **_verdict(record["reward"], grade.VERIFIER_CRASHED)}, staging


def _verdict(reward: bool | None, crashed: str) -> dict[str, Any]:
    """`passed` and `fail_reason` from a binary reward, or the fail-shaped `crashed` reason."""
    if reward is None:
        return {"status": "complete", "passed": False, "fail_reason": crashed}
    return {"status": "complete", "passed": reward, "fail_reason": None}


def attach_source_only_grades(
    evidence: Path, index: object, attempts: list[dict[str, Any]]
) -> None:
    """Add each stage's bound grade, or its infrastructure error, to report attempts."""
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
                    "grade_contract": document.get("grade_contract"),
                    "source_only_filter": document["source_only_filter"],
                    "status": "infra",
                    "reason": reason,
                    "reward": None,
                }
