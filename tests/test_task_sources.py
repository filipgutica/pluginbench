from __future__ import annotations

import json
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from pluginbench import cli, evaluation, harbor_backend, task_sources as sources

NAMES = ["testem-per-launcher-reports", "true-myth-iterable-collection-combinators"]
NONPILOT = "prometheus-typed-label-sorting"
PIN = "sha256:" + "a" * 64


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths = {}
    for name in [*NAMES, NONPILOT]:
        path = tmp_path / "downloads" / name
        for directory in ("environment", "tests", "solution"):
            (path / directory).mkdir(parents=True)
        (path / "task.toml").write_text(
            "schema_version='1.4'\n[metadata]\nrepository_url='https://github.com/example/repo'\n"
            f"language='{'go' if name == NONPILOT else 'javascript'}'\n"
            "base_commit_hash='base'\n[agent]\ntimeout_sec=10800\n"
            "[environment]\ncpus=2\nmemory_mb=8192\nbuild_timeout_sec=1800\n"
            "docker_image='fixture:pinned'\n"
            "[verifier]\nenvironment_mode='separate'\n"
            "[verifier.environment]\ndocker_image='fixture:grader'\n"
            "cpus=2\nmemory_mb=8192\n"
        )
        (path / "instruction.md").write_text("Implement the requested behavior.\n")
        (path / "tests/test.patch").write_text("Trusted assertions.\n")
        (path / "tests/test.sh").write_text("#!/bin/sh\nprintf 1 > /logs/verifier/reward.txt\n")
        (path / "tests/test.sh").chmod(0o755)
        (path / "solution/solution.patch").write_text("Known reference.\n")
        paths[name] = path
    references = [{"org": "datacurve", "name": name, "ref": PIN} for name in NAMES]
    references.append({"org": "datacurve", "name": NONPILOT, "ref": PIN})
    monkeypatch.setattr(
        sources, "dataset_catalog", lambda *args: ("datacurve/deep-swe-1-1", PIN, references)
    )

    def download(refs: list[dict[str, str]]) -> list[tuple[Path, dict[str, str]]]:
        return [
            (
                paths[sources.task_name(ref)],
                {**ref, "git_commit_id": "b" * 40} if "git_url" in ref else ref,
            )
            for ref in refs
        ]

    monkeypatch.setattr(sources, "download_tasks", download)
    return paths


def picker(
    monkeypatch: pytest.MonkeyPatch, *, indexes: tuple[int, ...] = (0, 1), code: int = 0
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/fzf")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    def run(argv: list[str], *, input: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "--multi" in argv and "--preview" in argv
        lines = input.splitlines()
        return subprocess.CompletedProcess(argv, code, "\n".join(lines[index] for index in indexes))

    monkeypatch.setattr(subprocess, "run", run)


def test_picker_yaml_can_be_used_by_the_actual_evaluation_planner(
    assets: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker(monkeypatch)
    block = sources.pick_tasks("datacurve/deep-swe-1-1")
    assert block is not None
    tasks = yaml.safe_load(block)["tasks"]
    assert tasks == {
        "dataset": "datacurve/deep-swe-1-1",
        "version": PIN,
        "ids": [f"datacurve/{name}" for name in NAMES],
    }
    guidance = tmp_path / "guidance.md"
    guidance.write_text("Reuse the owning implementation.\n")
    config = tmp_path / "evaluation.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "runner": {"harness": "codex", "model": "gpt-6-luna", "reasoning": "low"},
                "guidance": {"candidate": [str(guidance)]},
                "tasks": tasks,
                "measurements": ["tokens", "lines"],
                "repeats": 1,
            }
        )
    )
    monkeypatch.setattr(harbor_backend, "capacity_jobs", lambda **kwargs: 1)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: {"fixture": True})
    resolved = evaluation.load_evaluation(config)
    plan = evaluation.plan_evaluation(resolved)
    assert plan["tasks"] == NAMES and plan["coding_calls"] == 4
    assert resolved.tasks.version == PIN and len(resolved.tasks.references) == 2
    # Reading saved settings never re-resolves a dataset or invokes Harbor.
    monkeypatch.setattr(sources, "dataset_catalog", lambda *args: pytest.fail("offline settings"))
    assert evaluation._saved_evaluation(resolved.model_dump(mode="json")).tasks.version == PIN


@pytest.mark.parametrize("code", [1, 130])
def test_cancel_produces_no_yaml_or_selected_task_download(
    assets: dict[str, Path], monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    picker(monkeypatch, indexes=(), code=code)
    monkeypatch.setattr(
        sources, "download_tasks", lambda *args: pytest.fail("cancel must not download")
    )
    assert sources.pick_tasks("datacurve/deep-swe-1-1") is None


def test_nonpilot_task_selection_emits_usable_yaml_without_a_name_gate(
    assets: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    picker(monkeypatch, indexes=(2,))
    block = sources.pick_tasks("datacurve/deep-swe-1-1")
    assert block is not None
    selected = yaml.safe_load(block)["tasks"]
    assert selected == {
        "dataset": "datacurve/deep-swe-1-1",
        "version": PIN,
        "ids": [f"datacurve/{NONPILOT}"],
    }
    tasks = sources.Tasks.model_validate(selected)
    root = tasks.resolve()
    assert harbor_backend.native_task_assets(
        root / "tasks" / NONPILOT
    ) == harbor_backend.native_task_assets(assets[NONPILOT])


@pytest.mark.parametrize(
    "mutation", ["prompt", "tests", "reference", "metadata", "mode", "origins", "symlink"]
)
def test_reusing_a_selection_revalidates_its_assets_and_provenance(
    assets: dict[str, Path], mutation: str
) -> None:
    task = sources.Tasks(dataset="datacurve/deep-swe-1-1", ids=[NAMES[0]])
    root = task.resolve()
    path = root / "tasks" / NAMES[0]
    targets = {
        "prompt": path / "instruction.md",
        "tests": path / "tests/test.patch",
        "reference": path / "solution/solution.patch",
        "metadata": path / "task.toml",
        "origins": root / ".pluginbench-source.json",
    }
    if mutation == "symlink":
        (path / "instruction.md").unlink()
        (path / "instruction.md").symlink_to(assets[NAMES[0]] / "instruction.md")
    elif mutation == "mode":
        verifier = path / "tests/test.sh"
        verifier.chmod(0o644)
    elif mutation == "origins":
        provenance = targets["origins"]
        receipt = json.loads(provenance.read_text())
        receipt["references"][0]["org"] = "other-origin"
        provenance.write_text(json.dumps(receipt))
    elif mutation == "metadata":
        manifest = path / "task.toml"
        manifest.write_text(manifest.read_text().replace("fixture:pinned", "fixture:changed"))
    else:
        target = targets[mutation]
        target.write_text(target.read_text() + "\nchanged\n")
    with pytest.raises(ValueError):
        task.resolve()


def test_grouped_download_failure_is_a_cli_diagnostic_without_yaml_or_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    error = ExceptionGroup(
        "downloads", [ExceptionGroup("lookup", [ValueError("task unavailable")])]
    )

    class Client:
        async def download_tasks(self, tasks: object, *, output_dir: Path, overwrite: bool) -> None:
            assert overwrite is True
            raise error

    identifier = SimpleNamespace(model_validate=lambda ref: ref)
    modules = {
        "harbor.models.task.id": SimpleNamespace(PackageTaskId=identifier),
        "harbor.tasks.client": SimpleNamespace(TaskClient=Client),
    }
    original_import = importlib.import_module
    monkeypatch.setattr(
        importlib, "import_module", lambda name: modules.get(name) or original_import(name)
    )

    def failing_picker(*args: object) -> None:
        sources.download_tasks([{"org": "example", "name": "task", "ref": PIN}])

    monkeypatch.setattr(sources, "pick_tasks", failing_picker)
    monkeypatch.setattr(
        evaluation, "run_evaluation", lambda *args, **kwargs: pytest.fail("must not evaluate")
    )
    result = CliRunner().invoke(cli.app, ["tasks", "pick", "--dataset", "example/tasks"])
    assert result.exit_code == 2 and result.stdout == ""
    assert "task unavailable" in result.stderr
    with pytest.raises(ValueError, match="task unavailable") as caught:
        sources.download_tasks([{"org": "example", "name": "task", "ref": PIN}])
    assert caught.value.__cause__ is error


def test_execution_budget_and_package_metadata_are_part_of_the_native_asset_hash(
    assets: dict[str, Path],
) -> None:
    path = assets[NAMES[0]]
    original = harbor_backend.native_task_assets(path)
    manifest = path / "task.toml"
    manifest.write_text(manifest.read_text().replace("10800", "5400"))
    changed_budget = harbor_backend.native_task_assets(path)
    assert changed_budget != original
    manifest.write_text(manifest.read_text() + "\n[task]\nname='datacurve/example'\n")
    assert harbor_backend.native_task_assets(path) not in {original, changed_budget}


def test_git_task_links_are_pinned_before_execution(
    assets: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("harbor.registry.client.git_repo")
    setattr(
        module,
        "resolve_repo_source",
        lambda url: SimpleNamespace(
            git_url="https://github.com/datacurve-ai/deep-swe",
            ref="main",
            subdir=f"tasks/{NAMES[0]}",
        ),
    )
    original_import = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: module if name == "harbor.registry.client.git_repo" else original_import(name),
    )
    task = sources.Tasks(
        urls=[f"https://github.com/datacurve-ai/deep-swe/tree/main/tasks/{NAMES[0]}"]
    )
    source = task.resolve()
    assert source.exists() and f"/tree/{'b' * 40}/" in task.urls[0]
    assert task.references[0]["git_commit_id"] == "b" * 40


@pytest.mark.parametrize(
    "raw",
    [
        {"source": ".", "dataset": "datacurve/deep-swe-1-1", "ids": [NAMES[0]]},
        {"dataset": "datacurve/deep-swe-1-1", "ids": [NAMES[0], f"datacurve/{NAMES[0]}"]},
        {"urls": [f"https://github.com/a/b/tree/main/tasks/{NAMES[0]}"] * 2},
        {"urls": [f"https://github.com/a/b/tree/--upload-pack/tasks/{NAMES[0]}"]},
        {"source": ".", "ids": [NAMES[0]], "version": PIN},
    ],
)
def test_ambiguous_or_invalid_sources_are_rejected_without_network(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        sources.Tasks.model_validate(raw)


def test_preview_downloads_only_the_focused_task_and_excludes_hidden_tests(
    assets: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = tmp_path / "packet.json"
    refs = [{"org": "datacurve", "name": name, "ref": PIN} for name in NAMES]
    packet.write_text(json.dumps(refs))
    original = sources.download_tasks
    seen = []

    def download(selected: list[dict[str, str]]) -> list[tuple[Path, dict[str, str]]]:
        seen.extend(selected)
        return original(selected)

    monkeypatch.setattr(sources, "download_tasks", download)
    text = sources.preview_task(packet, 1)
    assert seen == [refs[1]]
    assert "Implement the requested behavior" in text and "javascript" in text
    assert "Trusted assertions" not in text and "Known reference" not in text


@pytest.mark.parametrize("clipboard_fails", [False, True])
def test_cli_prints_exact_yaml_and_copies_only_when_requested(
    monkeypatch: pytest.MonkeyPatch, clipboard_fails: bool
) -> None:
    block = "tasks:\n  dataset: example/tasks\n  version: pinned\n  ids: [example]\n"
    monkeypatch.setattr(sources, "pick_tasks", lambda *args: block)
    copied = []

    def copy(value: str) -> None:
        copied.append(value)
        if clipboard_fails:
            raise ValueError("clipboard unavailable")

    monkeypatch.setattr(sources, "copy_yaml", copy)
    result = CliRunner().invoke(cli.app, ["tasks", "pick", "--dataset", "example/tasks", "--copy"])
    assert result.stdout == block and copied == [block]
    assert result.exit_code == (2 if clipboard_fails else 0)
    copied.clear()
    result = CliRunner().invoke(cli.app, ["tasks", "pick", "--dataset", "example/tasks"])
    assert result.stdout == block and not copied and result.exit_code == 0
