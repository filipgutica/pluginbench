"""Canonical source for explicitly reviewed, patch-grading native contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from pluginbench.assessment_metrics import EXCLUDED_NAMES, inspect_snapshot
from pluginbench.code_diff import _file_changes

SOURCE_POLICY = "reviewed-native-patch-source-v2"
GRADER_STARTUP_POLICY = "reviewed-native-grader-startup-v1"
_GRADER_ENVIRONMENT = {
    "APP_DIR": "/app",
    "TESTS_DIR": "/tests",
    "ARTIFACTS_DIR": "/logs/artifacts",
    "VERIFIER_DIR": "/logs/verifier",
}
_GRADER_SHA256 = "47cc9eaadf21e636323c360ec4fa786f0733ec9fd1d21ea5a5717ff9f8c4077c"
# Reviewed complete verifier trees, including task entrypoint, config and hidden patch.
# A shared grader filename or even its fingerprint alone is insufficient admission.
_REVIEWED_TESTS = {
    "ac4aadf99cfbda751fb12f9faef03cc6a3ccbeed8cf1ccda9441c811ef247acc": "d8fbebc75de4991a32354518beff1abf628d0b07",
    "9caf86b1898a4e1f9077e7ea9ed7f7e46c16bec89526916cf56a1d50187acfb5": "88e3d965c0b1628642a30a841745b410d6835052",
    "0eaf29b862c00fe04c8d5f76b63297fd15e8193da13b548f9a68e57b8ccf276d": "a691069fcadf9131e5f7a5a130a022dc678f3e1d",
    "5712990cdc674f4a167fc86ac9104daa7070cadbd051eeb329589a0fa1b174db": "c981f9c7aa91b83cc9bd33a09ecee9e751b06e8d",
    # happy-dom-abort-pending-body-reads and testem-per-launcher-reports at DeepSWE 0b9fabbb.
    "9d5f5c01aeb6935be963a7858beeacc97f231b92ccd34925a94ba5a46454673c": "82a0888cb2c87a6123e05424b528f8e8c9b3e426",
    "eda69e4b08e0976c74e59f4556e97ac0721b0ff0e154f58b6c3ec20b52249f10": "158f61ea91c9613d2011c41ee9be40ada1d7a307",
}
PATCH_ARTIFACT = "logs/artifacts/model.patch"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def portable_qualification(proof: object) -> dict[str, object]:
    """Bind source semantics without container IDs, cleanup timestamps or run locations."""
    if not isinstance(proof, dict):
        return {"status": "unknown", "reason": "legacy preparation"}
    cleanup = proof.get("cleanup")
    return {
        **{
            key: value
            for key, value in proof.items()
            if key not in {"base_tree", "export", "cleanup"}
        },
        "cleanup_status": cleanup.get("status") if isinstance(cleanup, dict) else "unavailable",
    }


def grader_startup(*, config: object, image_id: str) -> dict[str, object]:
    """Admit the startup semantics Harbor actually inherits from a prebuilt grader."""
    if not isinstance(config, dict):
        raise ValueError("native grader image configuration unavailable")
    if config.get("Entrypoint") not in (None, []):
        raise ValueError("native grader image inherited entrypoint is unsupported")
    if config.get("WorkingDir") != "/app":
        raise ValueError("native grader image workdir differs from reviewed /app contract")
    variables = config.get("Env")
    if variables is None:
        variables = []
    if not isinstance(variables, list) or not all(isinstance(value, str) for value in variables):
        raise ValueError("native grader image environment unavailable")
    environment: dict[str, str] = {}
    for variable in variables:
        key, separator, value = variable.partition("=")
        if key in _GRADER_ENVIRONMENT:
            if not separator or key in environment or value != _GRADER_ENVIRONMENT[key]:
                raise ValueError("native grader image redirects reviewed environment: " + key)
            environment[key] = value
    return {
        "policy": GRADER_STARTUP_POLICY,
        "image_id": image_id,
        "workdir": "/app",
        "entrypoint": [],
        "environment": environment,
    }


def reviewed_contract(task_dir: Path) -> dict[str, object]:
    """Admit only the reviewed verifier and exact artifact collection semantics."""
    from pluginbench.harbor_backend import native_task_assets

    native_task_assets(task_dir)
    tests = task_dir / "tests"
    tests_digest = native_task_assets(tests)
    base = _REVIEWED_TESTS.get(tests_digest)
    if base is None:
        raise ValueError("unsupported native patch grading contract: verifier tree unreviewed")
    if hashlib.sha256((tests / "grader.py").read_bytes()).hexdigest() != _GRADER_SHA256:
        raise ValueError("unsupported native patch grading contract: grader changed")
    config = json.loads((tests / "config.json").read_text())
    if config.get("base_commit") != base:
        raise ValueError("reviewed verifier base commit changed")
    task = tomllib.loads((task_dir / "task.toml").read_text())
    verifier = task.get("verifier", {})
    collection = [
        {
            "command": "cd /app && mkdir -p /logs/artifacts && git config --global --add "
            "safe.directory /app && git diff --binary "
            + base
            + " HEAD > /logs/artifacts/model.patch",
            "service": "main",
            "timeout_sec": 300.0,
        }
    ]
    collect = verifier.get("collect")
    if isinstance(collect, list) and all(isinstance(entry, dict) for entry in collect):
        # Harbor runs a collect command in the main service when the task names none.
        collect = [{"service": "main", **entry} for entry in collect]
    if (
        task.get("artifacts") != ["/logs/artifacts/model.patch"]
        or verifier.get("environment_mode") != "separate"
        or verifier.get("network_mode") != "no-network"
        or collect != collection
        or verifier.get("env", {}) != {}
        or verifier.get("environment", {}).get("env", {}) != {}
    ):
        raise ValueError(
            "unsupported native patch grading contract: collection or environment changed"
        )
    payload = {
        name: hashlib.sha256((tests / name).read_bytes()).hexdigest()
        for name in ("test.sh", "test.patch", "grader.py", "config.json")
    }
    return {
        "base_commit": base,
        "tests_sha256": tests_digest,
        "collection": collection,
        "grader_sha256": _GRADER_SHA256,
        "patch_artifact": PATCH_ARTIFACT,
        "verifier_payload": payload,
    }


def base_tree_manifest(output: str) -> dict[str, dict[str, str]]:
    """Parse finite git ls-tree -r -z BASE output without losing blob identities."""
    entries: dict[str, dict[str, str]] = {}
    for record in output.split("\0"):
        if not record:
            continue
        header, separator, path = record.partition("\t")
        match = re.fullmatch(r"([0-7]{6}) (blob|commit) ([0-9a-f]{40})", header)
        if not separator or match is None or path in entries:
            raise ValueError("invalid or duplicate native base tree entry")
        _safe_path(path)
        entries[path] = {"mode": match[1], "type": match[2], "blob": match[3]}
    if not entries:
        raise ValueError("native base tree inventory is empty")
    return entries


def _safe_path(value: str, *, included: bool = False) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
        or any(ord(char) < 32 for char in value)
        or (included and any(part in EXCLUDED_NAMES for part in path.parts))
    ):
        raise ValueError("unsafe or excluded native patch path: " + value)


def _patch_paths(patch: str) -> set[str]:
    # The reviewed grader does not reliably parse Git's C-quoted paths.
    if any(line.startswith("diff --git ") and '"' in line for line in patch.splitlines()):
        raise ValueError("unsupported quoted native patch path")
    for line in patch.splitlines():
        if re.match(r"(?:old mode|new mode|new file mode|deleted file mode) ", line):
            if line.rsplit(" ", 1)[-1] not in {"100644", "100755"}:
                raise ValueError("unsupported native patch file mode")
    changes = _file_changes(patch)
    paths = {
        path
        for change in changes
        for path in (change.old_path, change.new_path)
        if path is not None
    }
    sections = iter(changes)
    current = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = next(sections)
            header = _file_changes(line)[0]
            for effective, declared in (
                (current.old_path, header.old_path),
                (current.new_path, header.new_path),
            ):
                if effective is not None and effective != declared:
                    raise ValueError("inconsistent native patch header and marker paths")
        elif current is not None:
            for prefix, expected in (
                ("rename from ", current.old_path),
                ("rename to ", current.new_path),
                ("copy from ", current.old_path),
                ("copy to ", current.new_path),
            ):
                if line.startswith(prefix):
                    path = line.removeprefix(prefix)
                    _safe_path(path, included=True)
                    if path != expected:
                        raise ValueError("inconsistent native patch rename or copy paths")
                    paths.add(path)
    for path in paths:
        _safe_path(path, included=True)
    return paths


def _proof(*, before: Path, task_dir: Path, identity: dict[str, object]) -> dict[str, Any]:
    from pluginbench.harbor_backend import native_task_assets

    if native_task_assets(task_dir) != identity.get("task_assets_sha256"):
        raise ValueError("frozen native task assets changed")
    contract = reviewed_contract(task_dir)
    proof = identity.get("source_qualification")
    if not isinstance(proof, dict) or proof.get("status") != "complete":
        raise ValueError("native patch source qualification unavailable or incomplete")
    if (
        identity.get("source_policy") != SOURCE_POLICY
        or proof.get("source_policy") != SOURCE_POLICY
    ):
        raise ValueError("native patch source policy incompatible")
    if proof.get("contract") != contract or proof.get("grader_head") != contract["base_commit"]:
        raise ValueError("native patch source qualification contract changed")
    if proof.get("grader_tests_sha256") != _digest(contract["verifier_payload"]):
        raise ValueError("native grader image verifier payload unqualified")
    startup = proof.get("grader_startup")
    grader_image = identity.get("grader_image_identity")
    if not isinstance(startup, dict) or not isinstance(grader_image, dict):
        raise ValueError("native grader startup qualification unavailable")
    environment = startup.get("environment")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ValueError("native grader startup environment qualification unavailable")
    admitted_startup = grader_startup(
        config={
            "Entrypoint": startup.get("entrypoint"),
            "WorkingDir": startup.get("workdir"),
            "Env": [key + "=" + value for key, value in environment.items()],
        },
        image_id=str(grader_image.get("image_id")),
    )
    if startup != admitted_startup:
        raise ValueError("native grader startup qualification changed")
    for key, proof_key in (
        ("image_identity", "coding_image_id"),
        ("grader_image_identity", "grader_image_id"),
    ):
        image = identity.get(key)
        if not isinstance(image, dict) or image.get("image_id") != proof.get(proof_key):
            raise ValueError("native patch source qualification image changed")
    if (
        inspect_snapshot(before)["digest"] != proof.get("fixture_digest")
        or native_task_assets(before) != proof.get("fixture_metadata_digest")
        or identity.get("source_digest") != proof.get("fixture_digest")
    ):
        raise ValueError("frozen native patch fixture changed")
    tree = proof.get("base_tree")
    if not isinstance(tree, dict) or not tree or _digest(tree) != proof.get("base_tree_sha256"):
        raise ValueError("native patch base inventory changed or unavailable")
    cleanup = proof.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
        raise ValueError("native patch qualification cleanup incomplete")
    return proof


def reconstruct(
    *,
    before: Path,
    task_dir: Path,
    identity: dict[str, object],
    artifacts: Path,
    execution: dict[str, Any],
    destination: Path,
) -> dict[str, object]:
    """Reconstruct the admitted hash-bound artifact; never substitute a stopped export."""
    from pluginbench.harbor_backend import native_task_assets

    if destination.exists() or destination.is_symlink():
        raise ValueError("canonical native destination already exists")
    if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("canonical native destination is linked")
    proof = _proof(before=before, task_dir=task_dir, identity=identity)
    hashes = execution.get("native_artifact_hashes")
    if not isinstance(hashes, dict) or not isinstance(hashes.get(PATCH_ARTIFACT), str):
        raise ValueError("admitted native graded patch hash unavailable")
    patch = artifacts / "harbor/trial/artifacts" / PATCH_ARTIFACT
    if (
        not patch.is_file()
        or patch.is_symlink()
        or any(parent.is_symlink() for parent in patch.parents)
    ):
        raise ValueError("native graded patch unavailable or linked")
    patch_bytes = patch.read_bytes()
    digest = hashlib.sha256(patch_bytes).hexdigest()
    if digest != hashes[PATCH_ARTIFACT]:
        raise ValueError("native graded patch changed")
    text = patch_bytes.decode("utf-8", errors="strict")
    paths = _patch_paths(text)
    tree = proof["base_tree"]
    for path in paths:
        preimage = before / path
        entry = tree.get(path)
        if entry is None:
            if preimage.exists():
                raise ValueError("native patch creation collides with frozen fixture: " + path)
            continue
        if (
            not isinstance(entry, dict)
            or entry.get("type") != "blob"
            or entry.get("mode") not in {"100644", "100755"}
            or not preimage.is_file()
            or preimage.is_symlink()
        ):
            raise ValueError("native patch preimage unavailable or unsupported: " + path)
        content = preimage.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        mode = "100755" if preimage.stat().st_mode & 0o111 else "100644"
        if blob != entry.get("blob") or mode != entry["mode"]:
            raise ValueError("native patch preimage differs from verifier base: " + path)
    hidden = _patch_paths((task_dir / "tests/test.patch").read_text())
    with tempfile.TemporaryDirectory(prefix="pluginbench-canonical-patch-") as temporary:
        source = Path(temporary) / "source"
        shutil.copytree(before, source)
        try:
            applied = subprocess.run(
                [
                    "git",
                    "apply",
                    "--binary",
                    "--allow-empty",
                    "--whitespace=nowarn",
                    str(patch.resolve()),
                ],
                cwd=source,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
                env={
                    "PATH": os.environ["PATH"],
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CEILING_DIRECTORIES": temporary,
                },
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("graded patch reconstruction timed out after 60 seconds") from error
        if applied.returncode:
            raise ValueError("graded patch could not be safely reconstructed: " + applied.stderr)
        source_digest = inspect_snapshot(source)["digest"]
        metadata_digest = native_task_assets(source)
        # Copy only after all validation, and never leave a partially admitted destination.
        try:
            shutil.copytree(source, destination)
        except BaseException:
            if destination.is_dir():
                shutil.rmtree(destination)
            raise
    return {
        "status": "complete",
        "source_policy": SOURCE_POLICY,
        "binding": "reviewed model.patch over qualified frozen fixture",
        "snapshot_digest": source_digest,
        "source_metadata_digest": metadata_digest,
        "patch_artifact": PATCH_ARTIFACT,
        "patch_sha256": digest,
        "preparation_proof_sha256": _digest(portable_qualification(proof)),
        "trusted_test_overlap": sorted(paths & hidden),
    }


def validate(
    *,
    before: Path,
    task_dir: Path,
    identity: dict[str, object],
    artifacts: Path,
    execution: dict[str, Any],
    snapshot: Path,
) -> dict[str, object]:
    """Independently reconstruct and compare the complete canonical snapshot metadata."""
    from pluginbench.harbor_backend import native_task_assets

    with tempfile.TemporaryDirectory(prefix="pluginbench-validate-patch-") as temporary:
        receipt = reconstruct(
            before=before,
            task_dir=task_dir,
            identity=identity,
            artifacts=artifacts,
            execution=execution,
            destination=Path(temporary).resolve() / "source",
        )
    if (
        inspect_snapshot(snapshot)["digest"] != receipt["snapshot_digest"]
        or native_task_assets(snapshot) != receipt["source_metadata_digest"]
    ):
        raise ValueError("canonical graded source changed")
    return receipt


def validate_export(
    *, diagnostic: Path, artifacts: Path, execution: dict[str, Any]
) -> dict[str, object]:
    """Validate stopped-source diagnostics separately from the canonical submission."""
    from pluginbench.harbor_backend import native_task_assets, _native_artifact_hashes

    receipt_path = artifacts / "submission-source.json"
    for path in (diagnostic, receipt_path, artifacts / "harbor/trial/artifacts"):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("native stopped export evidence is linked")
    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict):
        raise ValueError("native stopped export receipt must be an object")
    digest = inspect_snapshot(diagnostic)["digest"]
    if digest != receipt.get("snapshot_digest") or digest != execution.get(
        "submission_export_digest"
    ):
        raise ValueError("native stopped export changed")
    if native_task_assets(diagnostic) != receipt.get("source_metadata_digest"):
        raise ValueError("native stopped export metadata changed")
    artifact_dir = artifacts / "harbor/trial/artifacts"
    native_task_assets(artifact_dir)
    if _native_artifact_hashes(artifact_dir) != execution.get("native_artifact_hashes"):
        raise ValueError("native graded artifacts changed")
    return receipt
