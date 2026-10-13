from pathlib import Path
import stat

import pytest

from pluginbench.datasets import load_tasks


def _task_catalog(root: Path) -> Path:
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "README.md").write_text("Create answer.txt.\n")
    (root / "verify.py").write_text(
        "from pathlib import Path\nassert Path('answer.txt').exists()\n"
    )
    (root / "tasks.yaml").write_text("""schema_version: 1
tasks:
  - id: task-a
    prompt: Create answer.txt.
    fixture: fixture
    verifier:
      command: [python3]
      script: verify.py
  - id: task-b
    prompt: Create answer.txt.
    fixture: fixture
    verifier:
      command: [python3]
      script: verify.py
""")
    return root / "tasks.yaml"


def test_loads_selected_local_tasks_with_content_digests(tmp_path: Path) -> None:
    config = _task_catalog(tmp_path)

    tasks = load_tasks(config, ["task-b", "task-a"])

    assert [task.task_id for task in tasks] == ["task-b", "task-a"]
    assert all(task.digest.startswith("sha256:") for task in tasks)
    assert tasks[0].verifier is not None
    assert tasks[0].fixture is not None
    assert tasks[0].verifier.command == ("python3", str(tmp_path / "verify.py"))
    assert not Path(tasks[0].verifier.command[-1]).is_relative_to(tasks[0].fixture)


def test_verifier_content_changes_task_digest(tmp_path: Path) -> None:
    config = _task_catalog(tmp_path)
    before = load_tasks(config)[0].digest

    (tmp_path / "verify.py").write_text("raise SystemExit(0)\n")

    assert load_tasks(config)[0].digest != before


def test_task_digest_tracks_executable_modes_and_empty_directories(tmp_path: Path) -> None:
    config = _task_catalog(tmp_path)
    fixture_file = tmp_path / "fixture" / "helper.sh"
    fixture_file.write_text("#!/bin/sh\n")
    original = load_tasks(config)[0].digest

    fixture_file.chmod(fixture_file.stat().st_mode | stat.S_IXUSR)
    executable = load_tasks(config)[0].digest
    (tmp_path / "fixture" / "empty").mkdir()
    with_empty_directory = load_tasks(config)[0].digest

    assert original != executable
    assert executable != with_empty_directory


def test_rejects_verifier_inside_writable_fixture(tmp_path: Path) -> None:
    config = _task_catalog(tmp_path)
    catalog = config
    catalog.write_text(catalog.read_text().replace("script: verify.py", "script: fixture/check.py"))
    (tmp_path / "fixture" / "check.py").write_text("raise SystemExit(0)\n")

    with pytest.raises(ValueError, match="outside the writable fixture"):
        load_tasks(config)
