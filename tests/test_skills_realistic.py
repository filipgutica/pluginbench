from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "benchmarks" / "skills-realistic"
TASK_IDS = ["branch-exclusions", "bounded-collection"]


def workspace(tmp_path: Path, task_id: str) -> Path:
    destination = tmp_path / task_id
    shutil.copytree(ROOT / "fixtures" / task_id, destination)
    return destination


def verify(directory: Path, task_id: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", str(ROOT / "verifier.mjs")],
        cwd=directory,
        env={**os.environ, "PLUGINBENCH_TASK_ID": task_id},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    report: dict[str, object] = json.loads(result.stdout)
    assert result.returncode == (0 if report["pass"] else 1), result.stderr
    return report


def apply_reference(directory: Path, task_id: str) -> None:
    result = subprocess.run(
        ["git", "apply", str(ROOT / "references" / f"{task_id}.patch")],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_realistic_seed_preserves_existing_behavior_but_fails_new_contract(
    tmp_path: Path, task_id: str
) -> None:
    report = verify(workspace(tmp_path, task_id), task_id)
    assert report["submitted_tests_pass"]
    assert report["preservation_scope_pass"]
    assert not report["behavior_pass"]
    assert not report["pass"]
    assert report["grading_snapshot_unchanged"]


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_external_reference_patch_satisfies_existing_and_new_contracts(
    tmp_path: Path, task_id: str
) -> None:
    directory = workspace(tmp_path, task_id)
    apply_reference(directory, task_id)
    assert verify(directory, task_id)["pass"]


def test_original_test_can_be_extended_without_hash_based_rejection(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "branch-exclusions")
    apply_reference(directory, "branch-exclusions")
    test = directory / "test/query.test.ts"
    test.write_text(
        test.read_text()
        + "\nit('new case', () => assert.deepEqual(queryWorktrees({worktrees: []}), []));\n"
    )
    assert verify(directory, "branch-exclusions")["pass"]


def test_trusted_existing_behavior_survives_submission_test_weakening(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "branch-exclusions")
    apply_reference(directory, "branch-exclusions")
    source = directory / "src/query.ts"
    source.write_text(source.read_text().replace("options.includeMain ?? true", "false"))
    test = directory / "test/query.test.ts"
    test.write_text("import {it} from 'node:test'; it('no assertions', () => {});\n")
    report = verify(directory, "branch-exclusions")
    assert not report["preservation_scope_pass"]
    assert not report["pass"]


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_added_nested_tests_are_executed(tmp_path: Path, task_id: str) -> None:
    directory = workspace(tmp_path, task_id)
    apply_reference(directory, task_id)
    nested = directory / "test/added"
    nested.mkdir()
    (nested / "failure.test.ts").write_text(
        "import {it} from 'node:test'; it('added regression', () => {throw Error('failure');});\n"
    )
    report = verify(directory, task_id)
    assert not report["submitted_tests_pass"]
    assert not report["pass"]


def test_failure_that_is_undefined_is_still_rejected(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "bounded-collection")
    apply_reference(directory, "bounded-collection")
    (directory / "test/undefined-error.test.ts").write_text(
        "import assert from 'node:assert/strict'; import {it} from 'node:test';\n"
        "import {mapLimit} from '../src/concurrency.ts';\n"
        "it('rejects undefined mapper failure', async () => {\n"
        "  let rejected = false;\n"
        "  await mapLimit([1], 1, async () => {throw undefined;}).catch((error) => {\n"
        "    rejected = true; assert.equal(error, undefined);\n"
        "  }); assert.equal(rejected, true);\n"
        "});\n"
    )
    assert verify(directory, "bounded-collection")["pass"]


def test_early_successful_exit_does_not_pass_external_contract(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "branch-exclusions")
    apply_reference(directory, "branch-exclusions")
    source = directory / "src/query.ts"
    source.write_text("process.exit(0);\n" + source.read_text())
    report = verify(directory, "branch-exclusions")
    assert not report["behavior_pass"]
    assert not report["preservation_scope_pass"]
    assert not report["pass"]
