"""Caches saved by PluginBench 942f12c are reused after an upgrade, and only when equivalent."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from legacy_support import LEGACY, copy_legacy, current_runtime, legacy_json
from native_support import synthetic_contract
from pluginbench import baselines, evaluation, harbor_backend, submissions
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.datasets import TaskSpec, load_tasks, native_identity, portable_native_identity

PREPARED_KEY = next(path.name for path in (LEGACY / "prepared").iterdir())
VANILLA_KEY = next(path.name for path in (LEGACY / "vanilla").iterdir())
IMAGE = {
    "image": "sha256:" + "a" * 64,
    "image_id": "sha256:" + "a" * 64,
    "platform": "linux/amd64",
    "workdir": "/app",
}


@pytest.fixture(autouse=True)
def synthetic_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture tasks are synthetic, as in test_baselines; admission itself runs for real.
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)


# Prepared images ---------------------------------------------------------------------------


class _Preparation:
    """A copy of the 942f12c prepared-image cache, with Docker stubbed as it was then."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cache = copy_legacy("prepared", tmp_path / "cache")
        self.task = copy_legacy("prepared-task", tmp_path / "task")
        self.limits = legacy_json("prepared-limits.json")
        packet = json.loads((self.cache / PREPARED_KEY / "cache.json").read_text())
        self.saved = packet["inputs"]["runtime"]
        self.runtime = current_runtime(self.saved)
        self.image = dict(IMAGE)
        self.preparations: list[Path] = []
        self.destinations = 0
        monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: self.runtime)
        monkeypatch.setattr(harbor_backend, "_native_image", lambda **kwargs: self.image)
        monkeypatch.setattr(
            harbor_backend, "_inspect_image", lambda reference, **kwargs: {"Id": reference}
        )
        monkeypatch.setattr(harbor_backend, "_prepare_native_task", self._prepare)

    def _prepare(self, *, task_dir: Path, destination: Path, **kwargs: Any) -> dict[str, object]:
        self.preparations.append(destination)
        (destination / "artifacts").mkdir(parents=True)
        shutil.copytree(task_dir, destination / "task")
        (destination / "fixture").mkdir()
        (destination / "fixture/code.py").write_text('starting = "source"\n')
        receipt: dict[str, object] = {
            "status": "complete",
            "cleanup": {"status": "complete"},
            "isolation": self.runtime,
            "source_digest": inspect_snapshot(destination / "fixture")["digest"],
            "task_assets_sha256": harbor_backend.native_task_assets(destination / "task"),
            "image_identity": self.image,
            "grader_image_identity": self.image,
        }
        (destination / "artifacts/native-preparation.json").write_text(json.dumps(receipt))
        return receipt

    def run(self, *, version: str = "0.208.0", limits: dict[str, int] | None = None) -> Any:
        self.destinations += 1
        return harbor_backend.prepare_native_task(
            task_dir=self.task,
            destination=self.cache.parent / f"run-{self.destinations}",
            harness="codex",
            version=version,
            limits=limits or self.limits,
            cache=self.cache,
        )


def test_a_942f12c_prepared_entry_and_its_reference_agents_are_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preparation = _Preparation(tmp_path, monkeypatch)
    receipt = preparation.run()
    assert preparation.preparations == []
    assert receipt["preparation_key"] == PREPARED_KEY
    # Docker work for this run uses the current runtime; the entry's stays as provenance.
    assert receipt["isolation"] == preparation.runtime
    assert receipt["preparation_isolation"] == preparation.saved
    cache = copy_legacy("reference-agents", tmp_path / "reference-agents")
    monkeypatch.setattr(
        harbor_backend,
        "run_reference_agents",
        lambda *args: pytest.fail("the 942f12c oracle and nop result must be reused"),
    )
    rows = evaluation._reference_agents(receipt, tmp_path / "check", cache)
    assert {name: row["state"] for name, row in rows.items()} == {"oracle": "ready", "nop": "ready"}


@pytest.mark.parametrize(
    "change", ["base-image", "harness-version", "memory-limit", "setup-timeout", "corrupt-file"]
)
def test_a_942f12c_prepared_entry_from_different_inputs_is_prepared_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    preparation = _Preparation(tmp_path, monkeypatch)
    version, limits = "0.208.0", dict(preparation.limits)
    if change == "base-image":
        preparation.image = {
            **IMAGE,
            "image": "sha256:" + "f" * 64,
            "image_id": "sha256:" + "f" * 64,
        }
    elif change == "harness-version":
        version = "0.209.0"
    elif change == "memory-limit":
        limits["memory_mb"] = 512
    elif change == "setup-timeout":
        limits["setup_timeout"] = 601
    else:
        (preparation.cache / PREPARED_KEY / "fixture/code.py").write_text("tampered = 1\n")
    receipt = preparation.run(version=version, limits=limits)
    assert len(preparation.preparations) == 1
    assert receipt["preparation_key"] != PREPARED_KEY


def test_the_first_valid_of_several_equivalent_prepared_entries_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preparation = _Preparation(tmp_path, monkeypatch)
    # Two saved entries share a contract; the one that sorts first no longer validates.
    stale = preparation.cache / ("0" * 64)
    shutil.copytree(preparation.cache / PREPARED_KEY, stale)
    (stale / "fixture/code.py").write_text("tampered = 1\n")
    receipt = preparation.run()
    assert preparation.preparations == []
    assert receipt["preparation_key"] == PREPARED_KEY


def test_a_prepared_entry_whose_images_were_pruned_does_not_hide_a_later_usable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preparation = _Preparation(tmp_path, monkeypatch)
    # Both entries are intact and equivalent; the engine pruned the images of the first.
    pruned = preparation.cache / ("0" * 64)
    shutil.copytree(preparation.cache / PREPARED_KEY, pruned)
    receipt_path = pruned / "artifacts/native-preparation.json"
    receipt = json.loads(receipt_path.read_text())
    gone = {**IMAGE, "image": "sha256:" + "b" * 64, "image_id": "sha256:" + "b" * 64}
    receipt.update(image_identity=gone, grader_image_identity=gone)
    receipt_path.write_text(json.dumps(receipt))
    packet = json.loads((pruned / "cache.json").read_text())
    packet["files"] = harbor_backend._native_asset_inventory(pruned, exclude="cache.json")
    (pruned / "cache.json").write_text(json.dumps(packet))

    def inspect(reference: str, **kwargs: Any) -> dict[str, object]:
        if reference == gone["image_id"]:
            raise ValueError("No such image: " + reference)
        return {"Id": reference}

    monkeypatch.setattr(harbor_backend, "_inspect_image", inspect)
    reused = preparation.run()
    assert preparation.preparations == []
    assert reused["preparation_key"] == PREPARED_KEY


def test_a_prepared_entry_whose_images_the_engine_lost_names_the_entry_and_both_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preparation = _Preparation(tmp_path, monkeypatch)
    preparation.runtime["docker_engine"]["id"] = "replacement-engine"

    def missing(reference: str, **kwargs: Any) -> dict[str, object]:
        raise ValueError("No such image: " + reference)

    monkeypatch.setattr(harbor_backend, "_inspect_image", missing)
    with pytest.raises(ValueError) as raised:
        preparation.run()
    message = str(raised.value)
    assert f"Prepared cache entry {preparation.cache / PREPARED_KEY}" in message
    assert "Docker engine fixture-engine-at-preparation" in message
    assert "the current engine replacement-engine does not hold" in message
    assert preparation.preparations == []


# Vanilla attempts --------------------------------------------------------------------------


class _Vanilla:
    """A copy of the 942f12c vanilla cohort, and the identity this build gives the same run.

    After the upgrade the run's tasks carry the current runtime, as a reused prepared entry
    gives them, and Docker reports 28 KiB less memory than when the cohort was cached.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.cache = copy_legacy("vanilla", tmp_path / "cache")
        self.config = copy_legacy("vanilla-task", tmp_path / "config")
        self.root = tmp_path / "run"
        self.saved = legacy_json("vanilla-identity.json")
        catalog = yaml.safe_load((self.config / "tasks.yaml").read_text())
        for entry in catalog["tasks"]:
            identity = json.loads(entry["provenance"]["native_task_identity"])
            identity["isolation"] = current_runtime(identity["isolation"])
            entry["provenance"]["native_task_identity"] = json.dumps(identity)
        self.catalog = catalog
        self.identity = copy.deepcopy(self.saved)
        self.identity.update(
            isolation=current_runtime(self.saved["isolation"]),
            launcher_version=1,
            launcher_sha256=evaluation._launcher_sha256("codex"),
        )
        self.entries = [
            {"id": f"baseline-{repeat}", "task_id": "answer", "attempt": repeat, "arm": "baseline"}
            for repeat in (1, 2)
        ]

    def tasks(self) -> tuple[TaskSpec, ...]:
        (self.config / "tasks.yaml").write_text(yaml.safe_dump(self.catalog))
        tasks = tuple(load_tasks(self.config / "tasks.yaml"))
        self.identity["task_environment"] = {
            task.task_id: portable_native_identity(native_identity(task)) for task in tasks
        }
        return tasks

    def reserve(self) -> baselines.BaselineCache:
        cache = baselines.BaselineCache(
            path=self.cache,
            root=self.root,
            tasks=self.tasks(),
            entries=self.entries,
            identity=self.identity,
        )
        return cache.reserve()


def test_a_942f12c_vanilla_cohort_is_admitted_and_imported_after_the_upgrade(
    tmp_path: Path,
) -> None:
    vanilla = _Vanilla(tmp_path)
    cache = vanilla.reserve()
    try:
        decisions = cache.decisions()
        imported = [cache.import_attempt(entry) for entry in vanilla.entries]
    finally:
        cache.close()
    assert [row["status"] for row in decisions.values()] == ["hit", "hit"]
    assert {row["cache_key"] for row in decisions.values()} == {VANILLA_KEY}
    assert [record and record["id"] for record in imported] == ["baseline-1", "baseline-2"]
    preview = baselines.preview(
        path=vanilla.cache, tasks=vanilla.tasks(), repeats=2, identity=vanilla.identity
    )
    assert [(row["status"], row["cache_key"]) for row in preview] == [("hit", VANILLA_KEY)] * 2


def _changed(vanilla: _Vanilla, change: str) -> None:
    entry = vanilla.catalog["tasks"][0]
    if change == "prompt":
        entry["prompt"] += " Now."
    elif change == "fixture-mode":
        (vanilla.config / "fixtures/answer/answer.py").chmod(0o755)
    elif change == "provenance":
        entry["provenance"]["source"] = "another catalog"
    elif change == "model":
        vanilla.identity["study"]["model"] = "another-model"
    elif change == "harness-version":
        vanilla.identity["harness_version"] = "0.144.0"
    elif change == "limits":
        vanilla.identity["isolation"]["resources"]["memory_mb"] = 4096
    elif change == "jobs":
        vanilla.identity["jobs"] = 3
    elif change in {"unknown-launcher-hash", "partial-launcher-hashes"}:
        for manifest in vanilla.cache.glob("*/*/manifest.json"):
            saved = json.loads(manifest.read_text())
            hashes = saved["context"]["coding"]["launcher_sha256"]
            if change == "unknown-launcher-hash":
                hashes["codex.py"] = "f" * 64
            else:
                # Every hash but one dropped: not a declaration of the launcher that coded it.
                saved["context"]["coding"]["launcher_sha256"] = {
                    "harbor_backend.py": hashes["harbor_backend.py"]
                }
            manifest.write_text(json.dumps(saved))


@pytest.mark.parametrize(
    "change",
    [
        "prompt",
        "fixture-mode",
        "provenance",
        "model",
        "harness-version",
        "limits",
        "jobs",
        "unknown-launcher-hash",
        "partial-launcher-hashes",
    ],
)
def test_a_942f12c_vanilla_cohort_for_a_different_run_is_a_miss_not_an_error(
    tmp_path: Path, change: str
) -> None:
    vanilla = _Vanilla(tmp_path)
    _changed(vanilla, change)
    cache = vanilla.reserve()
    try:
        assert [row["status"] for row in cache.decisions().values()] == ["miss", "miss"]
    finally:
        cache.close()


@pytest.mark.parametrize("second", ["own-key", "another-saved-cohort"])
def test_a_cohort_split_across_directories_is_resolved_per_repeat(
    tmp_path: Path, second: str
) -> None:
    # A 942f12c run stopped after repeat 1; repeat 2 was cached later, under this build's
    # key or under another 942f12c key whose runtime differed only in recorded fields.
    vanilla = _Vanilla(tmp_path)
    first = baselines.BaselineCache(
        path=vanilla.cache,
        root=vanilla.root,
        tasks=vanilla.tasks(),
        entries=vanilla.entries,
        identity=vanilla.identity,
    )
    other = first.keys["answer"] if second == "own-key" else "0" * 64
    (vanilla.cache / other).mkdir()
    (vanilla.cache / VANILLA_KEY / "2").rename(vanilla.cache / other / "2")
    cache = vanilla.reserve()
    try:
        decisions = cache.decisions()
        imported = [cache.import_attempt(entry) for entry in vanilla.entries]
    finally:
        cache.close()
    assert [(row["status"], row["cache_key"]) for row in decisions.values()] == [
        ("hit", VANILLA_KEY),
        ("hit", other),
    ]
    assert all(imported)
    preview = baselines.preview(
        path=vanilla.cache, tasks=vanilla.tasks(), repeats=2, identity=vanilla.identity
    )
    assert [row["cache_key"] for row in preview] == [VANILLA_KEY, other]


@pytest.mark.parametrize("malformed", ["isolation-null", "launcher-hashes-string"])
def test_a_malformed_unrelated_cohort_does_not_stop_cache_lookups(
    tmp_path: Path, malformed: str
) -> None:
    vanilla = _Vanilla(tmp_path)
    manifest = json.loads((vanilla.cache / VANILLA_KEY / "1/manifest.json").read_text())
    coding = manifest["context"]["coding"]
    if malformed == "isolation-null":
        coding["isolation"] = None
    else:
        coding["launcher_sha256"] = "broken"
    broken = vanilla.cache / ("0" * 64) / "1"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text(json.dumps(manifest))
    preview = baselines.preview(
        path=vanilla.cache, tasks=vanilla.tasks(), repeats=2, identity=vanilla.identity
    )
    assert [(row["status"], row["cache_key"]) for row in preview] == [("hit", VANILLA_KEY)] * 2
    cache = vanilla.reserve()
    try:
        assert [row["status"] for row in cache.decisions().values()] == ["hit", "hit"]
    finally:
        cache.close()


def test_an_unreadable_saved_repeat_does_not_hide_the_valid_repeats_after_it(
    tmp_path: Path,
) -> None:
    vanilla = _Vanilla(tmp_path)
    (vanilla.cache / VANILLA_KEY / "1/manifest.json").write_text("{")
    preview = baselines.preview(
        path=vanilla.cache, tasks=vanilla.tasks(), repeats=2, identity=vanilla.identity
    )
    assert [row["status"] for row in preview] == ["miss", "hit"]
    cache = vanilla.reserve()
    try:
        decisions = cache.decisions()
    finally:
        cache.close()
    assert [(row["status"], row["cache_key"]) for row in decisions.values()][1] == (
        "hit",
        VANILLA_KEY,
    )
    assert [row["status"] for row in decisions.values()][0] == "miss"
