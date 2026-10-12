from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pluginbench import pier_backend, study


def _catalog(tmp_path: Path) -> Path:
    entries = []
    for name in ("first", "second"):
        directory = tmp_path / "tasks" / name
        (directory / "tests").mkdir(parents=True)
        (directory / "instruction.md").write_text(f"Implement {name}.\n")
        (directory / "task.toml").write_text(
            '[metadata]\nbase_commit_hash="'
            + "a" * 40
            + '"\nrepository_url="https://example.com/repo"\n'
            '[environment]\ndocker_image="image@sha256:' + "b" * 64 + '"\n'
            '[verifier]\nenvironment_mode="separate"\n'
        )
        (directory / "tests/Dockerfile").write_text("FROM image@sha256:" + "b" * 64 + "\n")
        entries.append({"id": name, "path": f"tasks/{name}"})
    catalog = tmp_path / "tasks.yaml"
    catalog.write_text(
        yaml.safe_dump({"schema_version": 1, "source": {"revision": "c" * 40}, "tasks": entries})
    )
    return catalog


def _recipe(tmp_path: Path) -> study.ResolvedStudy:
    _catalog(tmp_path)
    (tmp_path / "guidance.md").write_text("Reuse existing helpers.\n")
    recipe = tmp_path / "study.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "Real task smoke",
                "backend": "pier",
                "tasks": "tasks.yaml",
                "guidance": {"engineering": ["guidance.md"]},
                "repeats": 1,
            }
        )
    )
    return study.load_study(recipe)


def test_pinned_catalog_and_freeze_bind_all_task_bytes(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    tasks = pier_backend.load_tasks(catalog)
    frozen = pier_backend.freeze_tasks(tasks, tmp_path / "frozen")
    assert frozen[0].digest == tasks[0].digest
    (tasks[0].directory / "tests/Dockerfile").write_text("FROM image:latest\n")
    with pytest.raises(ValueError, match="changed before freezing"):
        pier_backend.freeze_tasks(tasks, tmp_path / "another")
    with pytest.raises(ValueError, match="pinned by digest"):
        pier_backend.load_tasks(catalog)


def test_catalog_rejects_symlink_ancestors_and_unpinned_source(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "tasks", target_is_directory=True)
    data = yaml.safe_load(catalog.read_text())
    data["tasks"][0]["path"] = "alias/first"
    catalog.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="without symlinks"):
        pier_backend.load_tasks(catalog)
    data["source"]["revision"] = "main"
    catalog.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="exact source revision"):
        pier_backend.load_tasks(catalog)


def test_pier_rejects_repairs_before_launch(tmp_path: Path) -> None:
    resolved = _recipe(tmp_path)
    data = yaml.safe_load(resolved.source.read_text())
    data["max_repairs"] = 1
    resolved.source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="max_repairs: 0"):
        study.load_study(resolved.source)


def test_retired_pier_execution_stops_before_output_or_model_calls(tmp_path: Path) -> None:
    resolved = _recipe(tmp_path)
    target = tmp_path / "new-run"
    with pytest.raises(ValueError, match="Pier execution is retired"):
        study.run_study(resolved, target)
    assert not target.exists()


def test_legacy_pier_report_reads_frozen_tasks_without_execution(tmp_path: Path) -> None:
    resolved = _recipe(tmp_path)
    root = tmp_path / "saved-run"
    root.mkdir()
    frozen = study._freeze(resolved, root)
    plan = study.plan_study(frozen)
    study._write(root / "plan.json", plan)
    study._write(
        root / "runtime-preflight.json", {"execution_backend": "pier", "pier_version": "0.3.1"}
    )
    records = [
        {
            **{key: plan["entries"][0][key] for key in ("id", "task_id", "arm", "attempt")},
            "model": None,
            "stages": [{"id": "handoff", "snapshot": None, "work": None}],
        }
    ]
    manifest = {
        "schema_version": 1,
        "name": resolved.recipe.name,
        "records": records,
        "standards": "frozen/standards.yaml",
    }
    study._checkpoint(root, frozen, manifest, {records[0]["id"]: "complete"}, status="complete")
    report, _ = study.report_study(root)
    assert report.is_file()
    assert json.loads(report.read_text())["attempts"][0]["stages"][0]["snapshot_digest"] is None
