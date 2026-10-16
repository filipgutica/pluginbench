from pathlib import Path
import json
import shutil
import stat

import pytest
import yaml

from pluginbench.datasets import load_tasks
from native_support import task_entry


def _task_catalog(root: Path) -> Path:
    tasks = [task_entry(root, name) for name in ("task-a", "task-b")]
    config = root / "tasks.yaml"
    config.write_text(yaml.safe_dump({"schema_version": 1, "tasks": tasks}))
    return config


def test_loads_selected_native_tasks_and_rejects_changed_trusted_assets(tmp_path: Path) -> None:
    config = _task_catalog(tmp_path)
    tasks = load_tasks(config, ["task-b", "task-a"])
    assert [task.task_id for task in tasks] == ["task-b", "task-a"]
    assert all(task.digest.startswith("sha256:") for task in tasks)
    assert not tasks[0].native_task.is_relative_to(tasks[0].fixture)
    (tasks[0].native_task / "tests/test.sh").write_text("exit 1\n")
    with pytest.raises(ValueError, match="native task assets changed"):
        load_tasks(config)


def test_task_digest_is_portable_across_run_folders_and_cleanup_nonces(tmp_path: Path) -> None:
    original = tmp_path / "one"
    original.mkdir()
    config = _task_catalog(original)
    before = load_tasks(config)[0].digest
    copied = tmp_path / "two"
    shutil.copytree(original, copied)
    document = yaml.safe_load((copied / "tasks.yaml").read_text())
    for entry in document["tasks"]:
        identity = json.loads(entry["provenance"]["native_task_identity"])
        identity.update(
            task_dir=str(copied / entry["native_task"]), fixture_dir=str(copied / entry["fixture"])
        )
        identity["cleanup"]["nonce"] = "another setup"
        entry["provenance"]["native_task_identity"] = json.dumps(identity)
    (copied / "tasks.yaml").write_text(yaml.safe_dump(document))
    assert load_tasks(copied / "tasks.yaml")[0].digest == before


def test_task_digest_tracks_executable_modes_and_empty_directories(tmp_path: Path) -> None:
    config = _task_catalog(tmp_path)
    file = tmp_path / "fixtures/task-a/src/answer.py"
    before = load_tasks(config)[0].digest
    file.chmod(file.stat().st_mode | stat.S_IXUSR)
    executable = load_tasks(config)[0].digest
    (file.parent / "empty").mkdir()
    assert before != executable
    assert load_tasks(config)[0].digest != executable


@pytest.mark.parametrize("fault", ["inside", "escape", "link", "source"])
def test_rejects_unbound_or_unsafe_native_inputs(tmp_path: Path, fault: str) -> None:
    config = _task_catalog(tmp_path)
    document = yaml.safe_load(config.read_text())
    entry = document["tasks"][0]
    if fault == "inside":
        entry["native_task"] = entry["fixture"]
    elif fault == "escape":
        entry["fixture"] = "../outside"
    elif fault == "link":
        (tmp_path / "link").symlink_to(tmp_path / entry["native_task"], target_is_directory=True)
        entry["native_task"] = "link"
    else:
        (tmp_path / entry["fixture"] / "src/answer.py").write_text("tampered\n")
    config.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError):
        load_tasks(config)
