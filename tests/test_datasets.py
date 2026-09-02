from pathlib import Path
import stat

import pytest

from conftest import load_evaluation_config, write_swebench_config

from pluginbench.config import load_config
from pluginbench.datasets import load_tasks


def test_loads_selected_local_tasks_with_content_digests(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)

    tasks = load_tasks(config.dataset)

    assert [task.task_id for task in tasks] == ["task-a", "task-b"]
    assert all(task.digest.startswith("sha256:") for task in tasks)
    assert tasks[0].verifier is not None
    assert tasks[0].fixture is not None
    assert tasks[0].verifier.command == ("python3", str(tmp_path / "verify.py"))
    assert not Path(tasks[0].verifier.command[-1]).is_relative_to(tasks[0].fixture)


def test_verifier_content_changes_task_digest(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    before = load_tasks(config.dataset)[0].digest

    (tmp_path / "verify.py").write_text("raise SystemExit(0)\n")

    assert load_tasks(config.dataset)[0].digest != before


def test_task_digest_tracks_executable_modes_and_empty_directories(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    fixture_file = tmp_path / "fixture" / "helper.sh"
    fixture_file.write_text("#!/bin/sh\n")
    original = load_tasks(config.dataset)[0].digest

    fixture_file.chmod(fixture_file.stat().st_mode | stat.S_IXUSR)
    executable = load_tasks(config.dataset)[0].digest
    (tmp_path / "fixture" / "empty").mkdir()
    with_empty_directory = load_tasks(config.dataset)[0].digest

    assert original != executable
    assert executable != with_empty_directory


def test_rejects_verifier_inside_writable_fixture(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    catalog = Path(config.dataset.source)
    catalog.write_text(catalog.read_text().replace("script: verify.py", "script: fixture/check.py"))
    (tmp_path / "fixture" / "check.py").write_text("raise SystemExit(0)\n")

    with pytest.raises(ValueError, match="outside the writable fixture"):
        load_tasks(config.dataset)


def test_loads_pinned_swebench_snapshot(tmp_path: Path) -> None:
    config = load_config(write_swebench_config(tmp_path / "eval.yaml"))

    tasks = load_tasks(config.dataset)

    assert [task.task_id for task in tasks] == ["sympy__sympy-20590"]
    assert tasks[0].fixture is None
    assert tasks[0].swebench is not None
    assert tasks[0].swebench.repo == "sympy/sympy"
    assert tasks[0].swebench.base_commit == "cffd4e0f86fefd4802349a9f9b19ed70934ea354"
    assert tasks[0].digest.startswith("sha256:")


def test_starting_patch_changes_prompt_and_task_digest(tmp_path: Path) -> None:
    patch = tmp_path / "candidate.patch"
    patch.write_text("diff --git a/a b/a\n")
    config = load_config(write_swebench_config(tmp_path / "eval.yaml", starting_patch=patch))

    task = load_tasks(config.dataset)[0]

    assert task.starting_patch is not None
    assert task.starting_patch.path == patch.resolve()
    assert task.starting_patch.expected_score == 0
    assert task.starting_patch.digest.startswith("sha256:")
    assert "proposed implementation is already applied" in task.prompt
    original_digest = task.digest

    patch.write_text("diff --git a/a b/a\nchanged\n")

    assert load_tasks(config.dataset)[0].digest != original_digest


def test_rejects_missing_starting_patch(tmp_path: Path) -> None:
    patch = tmp_path / "missing.patch"
    config = load_config(write_swebench_config(tmp_path / "eval.yaml", starting_patch=patch))

    with pytest.raises(ValueError, match="starting patch does not exist"):
        load_tasks(config.dataset)


def test_rejects_mutable_swebench_evaluator_image(tmp_path: Path) -> None:
    config = load_config(write_swebench_config(tmp_path / "eval.yaml"))
    snapshot = Path(config.dataset.source)
    snapshot.write_text(
        snapshot.read_text().replace(
            "@sha256:" + "0" * 64,
            ":latest",
        )
    )

    with pytest.raises(ValueError, match="image"):
        load_tasks(config.dataset)


def test_live_swebench_still_requires_its_native_harness(tmp_path: Path) -> None:
    config = load_evaluation_config(tmp_path)
    dataset = config.dataset.model_copy(update={"adapter": "live-swe-bench"})

    with pytest.raises(ValueError, match="native benchmark harness"):
        load_tasks(dataset)
