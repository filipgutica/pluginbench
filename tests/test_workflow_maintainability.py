from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pluginbench.config import DatasetConfig
from pluginbench.datasets import load_tasks


ROOT = Path(__file__).parents[1] / "benchmarks" / "workflow-maintainability"
TASK_IDS = ["metadata-summary", "selection-count", "grid-version-bump"]


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
        timeout=30,
        check=False,
    )
    report: dict[str, object] = json.loads(result.stdout)
    assert result.returncode == (0 if report["pass"] else 1), result.stderr
    return report


def apply_reference(directory: Path, task_id: str) -> None:
    if task_id == "metadata-summary":
        (directory / "src/worktree-summary.ts").write_text(
            "import type { Worktree } from './types.ts';\n"
            "import { formatAge, formatSize } from './formatting.ts';\n"
            "export const formatWorktreeSummary = ({ worktree, now }: "
            "{ worktree: Worktree; now: Date }): string =>\n"
            "  `${worktree.branch} · ${formatAge(worktree.updatedAt, now)} · "
            "${formatSize(worktree.bytes)}`;\n"
        )
    elif task_id == "selection-count":
        source = directory / "src/selection-state.ts"
        source.write_text(
            source.read_text().replace(
                "  has(path:", "  get count(): number { return this.paths.size; }\n\n  has(path:"
            )
        )
    else:
        for name in [
            "packages/grid-widget/package.json",
            "packages/direct-app/package.json",
            "package-lock.json",
        ]:
            path = directory / name
            path.write_text(path.read_text().replace("1.4.0", "1.4.1"))


def failed_check(report: dict[str, object], name: str) -> bool:
    return any(check["name"] == name and not check["pass"] for check in report_checks(report))


def report_checks(report: dict[str, object]) -> list[dict[str, object]]:
    checks = report["checks"]
    assert isinstance(checks, list)
    rows: list[dict[str, object]] = []
    for check in checks:
        assert isinstance(check, dict)
        assert all(isinstance(key, str) for key in check)
        rows.append(check)
    return rows


def test_catalog_uses_native_local_tasks() -> None:
    tasks = load_tasks(
        DatasetConfig(
            name="workflow-maintainability",
            version="2026-10-10",
            source=ROOT / "tasks.yaml",
            task_ids=TASK_IDS,
        )
    )
    assert [task.task_id for task in tasks] == TASK_IDS
    assert all(
        task.verifier is not None and task.verifier.script == ROOT / "verifier.mjs"
        for task in tasks
    )


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_seed_tests_pass_but_missing_work_fails_verification(tmp_path: Path, task_id: str) -> None:
    directory = workspace(tmp_path, task_id)
    tests = sorted(str(path.relative_to(directory)) for path in directory.rglob("*.test.ts"))
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--test", *tests],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = verify(directory, task_id)
    assert failed_check(report, "hidden behavioral contract")
    assert not failed_check(report, "preservation and scope")


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_patch_passes(tmp_path: Path, task_id: str) -> None:
    directory = workspace(tmp_path, task_id)
    apply_reference(directory, task_id)
    assert verify(directory, task_id)["pass"]


def test_summary_regression_fails_composition_contract(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "metadata-summary")
    apply_reference(directory, "metadata-summary")
    source = directory / "src/worktree-summary.ts"
    source.write_text(source.read_text().replace("formatAge(worktree.updatedAt, now)", "'unknown'"))
    assert failed_check(verify(directory, "metadata-summary"), "hidden behavioral contract")


@pytest.mark.parametrize("mutation", ["clear", "replace", "writable"])
def test_selection_regressions_fail_runtime_contract(tmp_path: Path, mutation: str) -> None:
    directory = workspace(tmp_path, "selection-count")
    apply_reference(directory, "selection-count")
    source = directory / "src/selection-state.ts"
    text = source.read_text()
    if mutation == "clear":
        text = text.replace("this.paths.clear();", "void this.paths;")
    elif mutation == "replace":
        text = text.replace(
            "this.paths = new Set(paths);", "for (const path of paths) this.paths.add(path);"
        )
    else:
        text = text.replace("get count(): number { return this.paths.size; }", "count = 0;")
    source.write_text(text)
    assert not verify(directory, "selection-count")["pass"]


@pytest.mark.parametrize("mistake", ["indirect-dependency", "stylesheet", "category", "stale-lock"])
def test_grid_bump_rejects_ownership_and_lock_regressions(tmp_path: Path, mistake: str) -> None:
    directory = workspace(tmp_path, "grid-version-bump")
    apply_reference(directory, "grid-version-bump")
    if mistake == "indirect-dependency":
        path = directory / "packages/analytics-app/package.json"
        manifest = json.loads(path.read_text())
        manifest["dependencies"]["@kong-ui-public/table-data-grid"] = "1.4.1"
        path.write_text(json.dumps(manifest))
    elif mistake == "stylesheet":
        path = directory / "packages/direct-app/src/dashboard.ts"
        path.write_text("import '@kong-ui-public/table-data-grid/style.css';\n" + path.read_text())
    elif mistake == "category":
        path = directory / "packages/grid-widget/package.json"
        manifest = json.loads(path.read_text())
        manifest["dependencies"] = manifest.pop("devDependencies")
        path.write_text(json.dumps(manifest))
    else:
        path = directory / "package-lock.json"
        lock = json.loads(path.read_text())
        lock["packages"]["node_modules/@kong-ui-public/table-data-grid"]["version"] = "1.4.0"
        path.write_text(json.dumps(lock))
    report = verify(directory, "grid-version-bump")
    assert not report["pass"]
    if mistake == "stylesheet":
        assert report["functional_pass"]
        assert not report["preservation_scope_pass"]


@pytest.mark.parametrize("mutation", ["delete", "skip"])
def test_original_test_preservation_is_independent_of_pass_counts(
    tmp_path: Path, mutation: str
) -> None:
    directory = workspace(tmp_path, "metadata-summary")
    apply_reference(directory, "metadata-summary")
    path = directory / "test/formatting.test.ts"
    if mutation == "delete":
        path.unlink()
    else:
        path.write_text(path.read_text().replace("it(", "it.skip("))
    assert failed_check(verify(directory, "metadata-summary"), "preservation and scope")


def test_new_nested_test_is_executed(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "metadata-summary")
    apply_reference(directory, "metadata-summary")
    nested = directory / "test/summary"
    nested.mkdir()
    (nested / "regression.test.ts").write_text(
        "import { it } from 'node:test';\n"
        "it('new regression', () => { throw new Error('deliberate new test failure'); });\n"
    )
    report = verify(directory, "metadata-summary")
    assert failed_check(report, "repository test: test/summary/regression.test.ts")


def test_unsupported_added_test_is_rejected_instead_of_silently_skipped(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "metadata-summary")
    apply_reference(directory, "metadata-summary")
    (directory / "test/failing.test.js").write_text(
        "throw new Error('must not be silently ignored');\n"
    )
    report = verify(directory, "metadata-summary")
    assert not report["pass"]
    assert not report["preservation_scope_pass"]
    assert failed_check(report, "preservation and scope")


@pytest.mark.parametrize("target", ["protected-file", "mutable-owner"])
def test_grading_cannot_change_the_submitted_candidate(tmp_path: Path, target: str) -> None:
    directory = workspace(tmp_path, "metadata-summary")
    apply_reference(directory, "metadata-summary")
    if target == "protected-file":
        file = "notes.md"
        content = "changed during grading\n"
    else:
        file = "src/worktree-summary.ts"
        content = (directory / file).read_text()
        shutil.copy2(ROOT / "fixtures/metadata-summary" / file, directory / file)
    (directory / "test/mutating.test.ts").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "import { it } from 'node:test';\n"
        f"it('changes workspace during grading', () => writeFileSync({json.dumps(file)}, "
        f"{json.dumps(content)}));\n"
    )
    report = verify(directory, "metadata-summary")
    assert report["functional_pass"]
    assert not report["pass"]
    assert not report["preservation_scope_pass"]
    assert not report["grading_snapshot_unchanged"]
    assert failed_check(report, "grading leaves submitted workspace unchanged")
    if target == "protected-file":
        assert failed_check(report, "preservation and scope after grading")


@pytest.mark.parametrize("where", ["production", "new-test"])
def test_early_successful_exit_is_not_a_completed_check(tmp_path: Path, where: str) -> None:
    directory = workspace(tmp_path, "metadata-summary")
    apply_reference(directory, "metadata-summary")
    if where == "production":
        source = directory / "src/worktree-summary.ts"
        source.write_text("process.exit(0);\n" + source.read_text())
        name = "hidden behavioral contract"
    else:
        (directory / "test/early-exit.test.ts").write_text("process.exit(0);\n")
        name = "repository test: test/early-exit.test.ts"
    report = verify(directory, "metadata-summary")
    assert failed_check(report, name)
    assert any(
        isinstance(error := check.get("error"), str) and "missing completion receipt" in error
        for check in report_checks(report)
    )


def test_unrelated_edits_and_unremoved_scratch_fail_scope(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "selection-count")
    apply_reference(directory, "selection-count")
    (directory / "notes.md").write_text("unrelated change\n")
    assert failed_check(verify(directory, "selection-count"), "preservation and scope")
    shutil.copy2(ROOT / "fixtures/selection-count/notes.md", directory / "notes.md")
    (directory / ".pilot-scratch").mkdir()
    assert failed_check(verify(directory, "selection-count"), "preservation and scope")


def test_verifier_requires_framework_owned_task_identity(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "selection-count")
    assert failed_check(verify(directory, "not-a-task"), "task identity")
