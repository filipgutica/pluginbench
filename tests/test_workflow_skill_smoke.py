import shutil
import subprocess
import sys
from pathlib import Path

from pluginbench.config import DatasetConfig
from pluginbench.datasets import load_tasks


VALID_PLAN = """# Order idempotency

## Context

Source: [REQUEST.md](REQUEST.md)

## Goal

Add idempotency to `src/orders/api.py` and `src/orders/service.py`.

## Non-goals

Do not add dependencies.

## Success criteria

Repeated keys return the original order.

## Bounded subtasks

Update `src/orders/repository.py` and add tests in `tests/test_orders.py`.

## Files touched

- `src/orders/api.py`
- `src/orders/service.py`
- `src/orders/repository.py`
- `tests/test_orders.py`

## Verification commands

`PYTHONPATH=src python3 -m unittest discover -s tests -v`

## Risks / assumptions / open questions

The current repository stores data in memory.
"""


def test_workflow_skill_smoke_catalog_has_three_failing_start_states() -> None:
    root = Path(__file__).parents[1] / "benchmarks" / "workflow-skill-smoke"
    tasks = load_tasks(
        DatasetConfig(
            name="workflow-skill-smoke",
            version="2026-08-31.2",
            source=root / "tasks.yaml",
            task_ids=[
                "debug-profile-cache",
                "triage-pricing-review",
                "plan-order-idempotency",
            ],
        )
    )

    assert [task.task_id for task in tasks] == [
        "debug-profile-cache",
        "triage-pricing-review",
        "plan-order-idempotency",
    ]
    expected_failures = {
        "debug-profile-cache": "user IDs still collide when their case differs",
        "triage-pricing-review": "discount -1 must raise ValueError",
        "plan-order-idempotency": "PLAN.md does not exist",
    }
    for task in tasks:
        assert task.verifier is not None
        assert task.fixture is not None
        result = subprocess.run(
            task.verifier.command,
            cwd=task.fixture,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, task.task_id
        assert expected_failures[task.task_id] in f"{result.stdout}\n{result.stderr}"


def test_plan_verifier_accepts_repository_aware_test_command(tmp_path: Path) -> None:
    root = Path(__file__).parents[1] / "benchmarks" / "workflow-skill-smoke"
    fixture = root / "fixtures" / "plan-order-idempotency"
    workspace = tmp_path / "workspace"
    shutil.copytree(fixture, workspace)
    (workspace / "PLAN.md").write_text(VALID_PLAN)

    result = subprocess.run(
        [sys.executable, str(root / "verifiers" / "plan_order_idempotency.py")],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
