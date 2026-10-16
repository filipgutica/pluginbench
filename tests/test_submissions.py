"""Canonical patch reconstruction and explicitly reviewed contract admission."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pluginbench import submissions
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.harbor_backend import native_task_assets


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    before = tmp_path / "before"
    before.mkdir()
    (before / "code.py").write_text("value = 1\n")
    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    (task / "tests/test.patch").write_text("")
    tree = {
        "code.py": {
            "mode": "100644",
            "type": "blob",
            "blob": hashlib.sha1(b"blob 10\0value = 1\n").hexdigest(),
        }
    }
    contract = {"base_commit": "a" * 40, "verifier_payload": {}}
    # Reconstruction is exercised in production. Only contract admission is replaced:
    # this fixture's artificial verifier has no production trust.
    monkeypatch.setattr(submissions, "reviewed_contract", lambda task_dir: contract)
    proof = {
        "status": "complete",
        "source_policy": submissions.SOURCE_POLICY,
        "contract": contract,
        "grader_head": contract["base_commit"],
        "fixture_digest": inspect_snapshot(before)["digest"],
        "fixture_metadata_digest": native_task_assets(before),
        "base_tree": tree,
        "base_tree_sha256": _hash(tree),
        "coding_image_id": "coding",
        "grader_image_id": "grader",
        "grader_tests_sha256": _hash(contract["verifier_payload"]),
        "grader_startup": {
            "policy": submissions.GRADER_STARTUP_POLICY,
            "image_id": "grader",
            "workdir": "/app",
            "entrypoint": [],
            "environment": {},
        },
        "cleanup": {"status": "complete"},
    }
    identity = {
        "task_assets_sha256": native_task_assets(task),
        "source_digest": proof["fixture_digest"],
        "source_policy": submissions.SOURCE_POLICY,
        "source_qualification": proof,
        "image_identity": {"image_id": "coding"},
        "grader_image_identity": {"image_id": "grader"},
    }
    artifacts = tmp_path / "artifacts"
    patch = artifacts / "harbor/trial/artifacts/logs/artifacts/model.patch"
    patch.parent.mkdir(parents=True)
    return {
        "before": before,
        "task_dir": task,
        "identity": identity,
        "artifacts": artifacts,
        "patch": patch,
        "execution": {},
        "destination": tmp_path / "canonical",
    }


def _write_patch(inputs: dict[str, Any], text: str) -> dict[str, Any]:
    inputs["patch"].write_text(text)
    inputs["execution"]["native_artifact_hashes"] = {
        submissions.PATCH_ARTIFACT: hashlib.sha256(text.encode()).hexdigest()
    }
    return {key: value for key, value in inputs.items() if key != "patch"}


_EDIT = "diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"


def test_reconstruct_measures_patch_and_preserves_modes_not_exported_generated_files(
    inputs: dict[str, Any],
    tmp_path: Path,
) -> None:
    diagnostic = tmp_path / "stopped-export"
    shutil.copytree(inputs["before"], diagnostic)
    (diagnostic / "dist").mkdir()
    (diagnostic / "dist/generated.js").write_text("generated();\n")
    patch = (
        _EDIT
        + "diff --git a/authored.js b/authored.js\nnew file mode 100755\n--- /dev/null\n+++ b/authored.js\n@@ -0,0 +1 @@\n+authored();\n"
    )
    kwargs = _write_patch(inputs, patch)
    receipt = submissions.reconstruct(**kwargs)
    canonical = inputs["destination"]
    assert (canonical / "code.py").read_text() == "value = 2\n"
    assert (canonical / "authored.js").read_text() == "authored();\n"
    assert (canonical / "authored.js").stat().st_mode & 0o111 == 0o111
    assert not (canonical / "dist").exists()
    assert (diagnostic / "dist/generated.js").read_text() == "generated();\n"
    kwargs.pop("destination")
    assert submissions.validate(**kwargs, snapshot=canonical) == receipt
    (canonical / "authored.js").chmod(0o644)
    with pytest.raises(ValueError, match="canonical graded source changed"):
        submissions.validate(**kwargs, snapshot=canonical)


def test_empty_patch_and_mode_only_change(inputs: dict[str, Any]) -> None:
    kwargs = _write_patch(inputs, "")
    receipt = submissions.reconstruct(**kwargs)
    assert receipt["snapshot_digest"] == inspect_snapshot(inputs["before"])["digest"]
    shutil.rmtree(inputs["destination"])
    kwargs = _write_patch(
        inputs, "diff --git a/code.py b/code.py\nold mode 100644\nnew mode 100755\n"
    )
    submissions.reconstruct(**kwargs)
    assert (inputs["destination"] / "code.py").stat().st_mode & 0o111 == 0o111


def test_binary_patch_and_rename_are_preserved(inputs: dict[str, Any], tmp_path: Path) -> None:
    original = b"\0old\n"
    (inputs["before"] / "code.py").write_bytes(original)
    proof = inputs["identity"]["source_qualification"]
    proof["fixture_digest"] = inputs["identity"]["source_digest"] = inspect_snapshot(
        inputs["before"]
    )["digest"]
    proof["fixture_metadata_digest"] = native_task_assets(inputs["before"])
    proof["base_tree"]["code.py"]["blob"] = hashlib.sha1(b"blob 5\0" + original).hexdigest()
    proof["base_tree_sha256"] = _hash(proof["base_tree"])
    expected = tmp_path / "expected"
    expected.mkdir()
    (expected / "code.py").write_bytes(b"\0new\n")
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--binary", "--", "before/code.py", "expected/code.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert diff.returncode == 1
    patch = diff.stdout.replace("a/before/", "a/").replace("b/expected/", "b/")
    submissions.reconstruct(**_write_patch(inputs, patch))
    assert (inputs["destination"] / "code.py").read_bytes() == b"\0new\n"
    shutil.rmtree(inputs["destination"])
    rename = "diff --git a/code.py b/renamed.py\nsimilarity index 100%\nrename from code.py\nrename to renamed.py\n"
    (inputs["task_dir"] / "tests/test.patch").write_text(_EDIT)
    inputs["identity"]["task_assets_sha256"] = native_task_assets(inputs["task_dir"])
    receipt = submissions.reconstruct(**_write_patch(inputs, rename))
    assert (inputs["destination"] / "renamed.py").read_bytes() == original
    assert not (inputs["destination"] / "code.py").exists()
    assert receipt["trusted_test_overlap"] == ["code.py"]


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
def test_apply_failure_does_not_publish_partial_or_overwrite_existing_source(
    inputs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    working_sources: list[Path] = []
    if failure == "timeout":

        def timed_out(command: list[str], *, cwd: Path, **options: object) -> None:
            working_sources.append(cwd)
            (cwd / "code.py").write_text("partially applied\n")
            raise subprocess.TimeoutExpired(
                command, 60, output=b"partial output", stderr=b"timeout details"
            )

        monkeypatch.setattr(subprocess, "run", timed_out)
    kwargs = _write_patch(
        inputs, _EDIT if failure == "timeout" else _EDIT.replace("-value = 1", "-value = 3")
    )
    expected_reason = (
        "reconstruction timed out" if failure == "timeout" else "could not be safely reconstructed"
    )
    with pytest.raises(ValueError, match=expected_reason) as rejected:
        submissions.reconstruct(**kwargs)
    assert not inputs["destination"].exists()
    if failure == "timeout":
        cause = rejected.value.__cause__
        assert isinstance(cause, subprocess.TimeoutExpired)
        assert cause.stderr == b"timeout details"
        assert working_sources and all(not source.exists() for source in working_sources)
        assert (inputs["before"] / "code.py").read_text() == "value = 1\n"
    inputs["destination"].mkdir()
    (inputs["destination"] / "keep").write_text("existing\n")
    with pytest.raises(ValueError, match="already exists"):
        submissions.reconstruct(**kwargs)
    assert (inputs["destination"] / "keep").read_text() == "existing\n"


@pytest.mark.parametrize("operation", ["rename", "copy"])
@pytest.mark.parametrize("destination", ["node_modules/copied.py", "hidden-test.py"])
def test_extended_patch_headers_cannot_redirect_to_unchecked_paths(
    inputs: dict[str, Any],
    operation: str,
    destination: str,
) -> None:
    if destination == "hidden-test.py":
        (inputs["task_dir"] / "tests/test.patch").write_text(
            _EDIT.replace("code.py", "hidden-test.py")
        )
        inputs["identity"]["task_assets_sha256"] = native_task_assets(inputs["task_dir"])
    patch = (
        "diff --git a/code.py b/copied.py\nsimilarity index 100%\n"
        + operation
        + " from code.py\n"
        + operation
        + " to "
        + destination
        + "\n"
    )
    with pytest.raises(ValueError, match="excluded|inconsistent"):
        submissions.reconstruct(**_write_patch(inputs, patch))
    assert not inputs["destination"].exists()


def test_patch_marker_disagreement_cannot_bypass_base_preimage_check(
    inputs: dict[str, Any],
) -> None:
    patch = _EDIT.replace("--- a/code.py", "--- a/other.py")
    with pytest.raises(ValueError, match="header and marker"):
        submissions.reconstruct(**_write_patch(inputs, patch))


@pytest.mark.parametrize("path", ["../escape.py", ".git/config", "node_modules/module.js"])
def test_unsafe_and_excluded_patch_paths_are_rejected(inputs: dict[str, Any], path: str) -> None:
    patch = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+bad\n"
    with pytest.raises(ValueError, match="unsafe or excluded"):
        submissions.reconstruct(**_write_patch(inputs, patch))
    assert not inputs["destination"].exists()


def test_symlink_patch_modes_and_linked_artifacts_are_rejected(inputs: dict[str, Any]) -> None:
    patch = "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+code.py\n"
    with pytest.raises(ValueError, match="file mode"):
        submissions.reconstruct(**_write_patch(inputs, patch))
    kwargs = _write_patch(inputs, _EDIT)
    destination = inputs["destination"]
    linked_parent = destination.parent / "linked-output"
    actual_parent = destination.parent / "outside-output"
    actual_parent.mkdir()
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="destination is linked"):
        submissions.reconstruct(**{**kwargs, "destination": linked_parent / "source"})
    assert not list(actual_parent.iterdir())
    inputs["patch"].rename(inputs["patch"].with_suffix(".saved"))
    inputs["patch"].symlink_to(inputs["patch"].with_suffix(".saved"))
    with pytest.raises(ValueError, match="linked"):
        submissions.reconstruct(**kwargs)


@pytest.mark.parametrize(
    "tamper",
    ["artifact", "fixture", "base", "image", "cleanup", "proof", "startup", "startup-missing"],
)
def test_tampered_or_incomplete_inputs_are_rejected(inputs: dict[str, Any], tamper: str) -> None:
    kwargs = _write_patch(inputs, _EDIT)
    proof = inputs["identity"]["source_qualification"]
    if tamper == "artifact":
        inputs["patch"].write_text(_EDIT + "\n")
    elif tamper == "fixture":
        (inputs["before"] / "code.py").write_text("value = 0\n")
    elif tamper == "base":
        proof["base_tree"]["code.py"]["blob"] = "b" * 40
        proof["base_tree_sha256"] = _hash(proof["base_tree"])
    elif tamper == "image":
        inputs["identity"]["grader_image_identity"]["image_id"] = "different"
    elif tamper == "cleanup":
        proof["cleanup"]["status"] = "incomplete"
    elif tamper == "startup":
        proof["grader_startup"]["environment"]["APP_DIR"] = "/other"
    elif tamper == "startup-missing":
        proof.pop("grader_startup")
    else:
        inputs["identity"].pop("source_qualification")
    with pytest.raises(ValueError):
        submissions.reconstruct(**kwargs)
    assert not inputs["destination"].exists()


def test_hidden_test_overlap_remains_visible(inputs: dict[str, Any]) -> None:
    (inputs["task_dir"] / "tests/test.patch").write_text(_EDIT)
    inputs["identity"]["task_assets_sha256"] = native_task_assets(inputs["task_dir"])
    receipt = submissions.reconstruct(**_write_patch(inputs, _EDIT))
    assert receipt["trusted_test_overlap"] == ["code.py"]
    assert (inputs["destination"] / "code.py").read_text() == "value = 2\n"


def test_arbitrary_model_patch_verifier_has_no_production_admission(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/grader.py").write_text("# model.patch\n")
    with pytest.raises(ValueError, match="verifier tree unreviewed"):
        submissions.reviewed_contract(tmp_path)


@pytest.mark.parametrize(
    "task_name",
    [
        "true-myth-iterable-collection-combinators",
        "csstree-shorthand-expansion-compression",
        "adaptix-name-mapping-aliases",
        "dateutil-rfc5545-timezone-interop",
    ],
)
def test_reviewed_real_contract_requires_exact_collection(task_name: str, tmp_path: Path) -> None:
    source = (
        Path(__file__).parents[1] / "runs/extended-guidance-20261013-v1/inputs" / task_name / "task"
    )
    if not source.is_dir():
        pytest.skip("saved diagnostic cohort is unavailable; no production trust substitution")
    task = tmp_path / "task"
    shutil.copytree(source, task)
    admitted = submissions.reviewed_contract(task)
    assert admitted["patch_artifact"] == "logs/artifacts/model.patch"
    config = task / "task.toml"
    config.write_text(config.read_text().replace("git diff --binary", "git diff --stat"))
    with pytest.raises(ValueError, match="collection or environment changed"):
        submissions.reviewed_contract(task)


def test_base_inventory_retains_full_modes_and_blob_ids() -> None:
    manifest = submissions.base_tree_manifest("100755 blob " + "a" * 40 + "\tscript.sh\0")
    assert manifest == {"script.sh": {"mode": "100755", "type": "blob", "blob": "a" * 40}}
    with pytest.raises(ValueError, match="invalid or duplicate"):
        submissions.base_tree_manifest("100755 blob abc\tscript.sh\0")


def test_portable_qualification_binds_source_proof_without_run_specific_container_data(
    inputs: dict[str, Any],
) -> None:
    proof = inputs["identity"]["source_qualification"]
    identity = submissions.portable_qualification(proof)
    proof["export"] = {"container_id": "another", "project": "another"}
    proof["cleanup"] = {"status": "complete", "removed_containers": ["another"]}
    assert submissions.portable_qualification(proof) == identity
    proof["base_tree_sha256"] = "changed"
    assert submissions.portable_qualification(proof) != identity


@pytest.mark.parametrize("tamper", [None, "metadata", "bytes", "artifact", "link"])
def test_stopped_export_validation_preserves_independent_diagnostic_integrity(
    inputs: dict[str, Any],
    tamper: str | None,
) -> None:
    _write_patch(inputs, _EDIT)
    diagnostic = inputs["before"]
    receipt = {
        "snapshot_digest": inspect_snapshot(diagnostic)["digest"],
        "source_metadata_digest": native_task_assets(diagnostic),
    }
    (inputs["artifacts"] / "submission-source.json").write_text(json.dumps(receipt))
    inputs["execution"]["submission_export_digest"] = receipt["snapshot_digest"]
    if tamper == "metadata":
        (diagnostic / "code.py").chmod(0o755)
    elif tamper == "bytes":
        (diagnostic / "code.py").write_text("different\n")
    elif tamper == "artifact":
        inputs["patch"].write_text("different patch\n")
    elif tamper == "link":
        (inputs["patch"].parent / "escape").symlink_to(diagnostic, target_is_directory=True)
    kwargs = {
        "diagnostic": diagnostic,
        "artifacts": inputs["artifacts"],
        "execution": inputs["execution"],
    }
    if tamper is None:
        assert submissions.validate_export(**kwargs) == receipt
    else:
        with pytest.raises(ValueError):
            submissions.validate_export(**kwargs)
