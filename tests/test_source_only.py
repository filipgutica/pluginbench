from __future__ import annotations

import os
from pathlib import Path

import pytest

from pluginbench import source_only, submissions

# The pinned DeepSWE task copies of the eval batch, kept outside the repository.
PINNED_TASKS = Path(
    os.environ.get(
        "PLUGINBENCH_PINNED_TASKS",
        Path(__file__).parents[1] / "runs/claude-harness/eval-batch/tasks",
    )
)


def _section(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"


def test_no_pinned_reference_solution_loses_a_file_it_changes() -> None:
    solutions = sorted(PINNED_TASKS.glob("*/solution/solution.patch"))
    if not solutions:
        pytest.skip("pinned DeepSWE tasks are unavailable; set PLUGINBENCH_PINNED_TASKS")
    lost = {}
    for solution in solutions:
        _, reference = source_only._task_paths(solution.parents[1])
        filtered, _ = source_only._without_tests(solution.read_text(), reference)
        if missing := reference - submissions._patch_paths(filtered):
            lost[solution.parents[1].name] = sorted(missing)

    # bandit's core/test_set.py, expr's vm/vm_test.go and meriyah's snapshot are among them.
    assert lost == {}


def test_reference_solution_files_stay_and_every_conftest_leaves() -> None:
    reference = {"bandit/core/test_set.py", "vm/vm_test.go"}
    kept = [
        "bandit/core/test_set.py", "vm/vm_test.go", "pyproject.toml", "checkers/testdata/a.go",
        "fixtures/b.css", "examples/c.py",
    ]
    removed = ["benchmarks/x/conftest.py", "conftest.py", "skrub/conftest.py", "vm/run_test.go"]
    patch = "".join(_section(path) for path in kept + removed)

    filtered, left_out = source_only._without_tests(patch, reference)

    assert left_out == removed
    assert filtered == "".join(_section(path) for path in kept)
