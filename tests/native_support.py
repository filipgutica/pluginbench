"""Offline native boundary fixtures shared by study and cache contract tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from pluginbench import harbor_backend, submissions
from pluginbench.assessment_metrics import inspect_snapshot


def synthetic_contract(task_dir: Path) -> dict[str, object]:
    """Mock admission only; these fixtures do not prove a trusted grader contract."""
    return {
        "base_commit": "a" * 40,
        "tests_sha256": harbor_backend.native_task_assets(task_dir / "tests"),
        "collection": [],
        "grader_sha256": "offline fixture",
        "patch_artifact": submissions.PATCH_ARTIFACT,
        "verifier_payload": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((task_dir / "tests").iterdir())
            if path.is_file()
        },
    }


def qualify_synthetic_source(identity: dict[str, object]) -> None:
    """Supply the preparation boundary while exercising real patch reconstruction."""
    source = Path(str(identity["fixture_dir"]))
    tree = {}
    for path in source.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            tree[path.relative_to(source).as_posix()] = {
                "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                "type": "blob",
                "blob": hashlib.sha1(
                    b"blob " + str(len(content)).encode() + b"\0" + content
                ).hexdigest(),
            }
    image, grader = identity["image_identity"], identity["grader_image_identity"]
    assert isinstance(image, dict) and isinstance(grader, dict)
    contract = synthetic_contract(Path(str(identity["task_dir"])))
    identity["source_digest"] = inspect_snapshot(source)["digest"]
    identity["source_policy"] = submissions.SOURCE_POLICY
    identity["source_qualification"] = {
        "status": "complete",
        "source_policy": submissions.SOURCE_POLICY,
        "contract": contract,
        "grader_tests_sha256": hashlib.sha256(
            json.dumps(contract["verifier_payload"], sort_keys=True).encode()
        ).hexdigest(),
        "grader_head": "a" * 40,
        "coding_image_id": image["image_id"],
        "grader_image_id": grader["image_id"],
        "grader_startup": {
            "policy": "reviewed-native-grader-startup-v1",
            "image_id": grader["image_id"],
            "workdir": "/app",
            "entrypoint": [],
            "environment": {},
        },
        "fixture_digest": identity["source_digest"],
        "fixture_metadata_digest": harbor_backend.native_task_assets(source),
        "base_tree": tree,
        "base_tree_sha256": hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest(),
        "cleanup": {"status": "complete"},
    }


def runtime_identity() -> dict[str, object]:
    def executable(name: str, digest: str) -> dict[str, str]:
        return {
            "path": "/tools/" + name,
            "resolved_path": "/tools/" + name,
            "sha256": digest * 64,
            "version": "1.2.3",
        }

    return {
        "protocol": harbor_backend.PROTOCOL,
        "harbor_version": "0.24.0",
        "implementation_sha256": "a" * 64,
        "submission_receipt_version": 2,
        "docker_cli": executable("docker", "1"),
        "docker_plugins": {
            "compose": executable("compose", "2"),
            "buildx": executable("buildx", "3"),
        },
        "docker_engine": {
            "id": "daemon-one",
            "version": "29.5.3",
            "platform": "linux/arm64",
            "endpoint": "unix:///var/run/docker.sock",
        },
        "resources": {"cpus": 1, "memory_mb": 2048},
    }


def task_entry(
    root: Path, name: str, *, answer_path: str = "src/answer.py", version: str = "1.2.3"
) -> dict[str, object]:
    source = root / "fixtures" / name
    answer = source / answer_path
    answer.parent.mkdir(parents=True)
    answer.write_text("answer = 0\n")
    task = root / "native" / name
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Set the answer to one.\n")
    (task / "task.toml").write_text(
        '[environment]\ndocker_image = "example@sha256:' + "b" * 64 + '"\n'
    )
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "tests/test.patch").write_text("")
    image = {
        "image": "sha256:" + "c" * 64,
        "image_id": "sha256:" + "c" * 64,
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    identity = {
        "status": "complete",
        "preparation_key": "6" * 64,
        "preparation_policy": "native-task-and-clean-guidance-source-v1",
        "transport_policy": {
            "name": harbor_backend.NATIVE_TRANSPORT_POLICY,
            "provider_hosts": harbor_backend.NATIVE_PROVIDER_HOSTS["codex"],
        },
        "cleanup": {"status": "complete", "nonce": name},
        "task_dir": str(task),
        "fixture_dir": str(source),
        "source_digest": inspect_snapshot(source)["digest"],
        "task_assets_sha256": harbor_backend.native_task_assets(task),
        "original_task_assets_sha256": "d" * 64,
        "image_identity": image,
        "toolchain_identity": {
            "image_id": image["image_id"],
            "harness": "codex",
            "harness_version": version,
            "platform": "linux/arm64",
            "install_policy": "harbor-native-install-only-v1",
            "base_image_identity": image,
        },
        "grader_image_identity": {**image, "image_id": "sha256:" + "e" * 64},
        "trusted_tests_sha256": harbor_backend.native_task_assets(task / "tests"),
        "verifier_test_delivery": "native-image",
        "workdir": "/app",
        "resources": {"cpus": 1, "memory_mb": 2048},
        "isolation": runtime_identity(),
    }
    qualify_synthetic_source(identity)
    return {
        "id": name,
        "prompt": f"Set the answer to one for {name}.",
        "fixture": f"fixtures/{name}",
        "native_task": f"native/{name}",
        "provenance": {"native_task_identity": json.dumps(identity)},
    }


def execution(
    area: Path,
    identity: dict[str, object],
    *,
    answer_path: str = "src/answer.py",
    reward: int = 1,
    timeout: int = 600,
    version: str = "1.2.3",
    diagnostic_files: dict[str, str] | None = None,
) -> dict[str, object]:
    artifacts = area / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    patch_dir = artifacts / "harbor/trial/artifacts/logs/artifacts"
    patch_dir.mkdir(parents=True)
    patch = patch_dir / "model.patch"
    before = area / "before"
    if not before.exists():
        shutil.copytree(Path(str(identity["fixture_dir"])), before)
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--binary", "--", str(before), str(area / "workspace")],
        capture_output=True,
        text=True,
        check=False,
    )
    text = diff.stdout.replace("a" + str(before) + "/", "a/").replace(
        "b" + str(area / "workspace") + "/", "b/"
    )
    patch.write_text(text)
    for relative, content in (diagnostic_files or {}).items():
        path = area / "workspace" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    runtime = identity["isolation"]
    assert isinstance(runtime, dict)
    (artifacts / "docker-client.json").write_text(
        json.dumps({key: runtime[key] for key in ("docker_cli", "docker_plugins")})
    )
    state = {
        "Running": False,
        "Status": "exited",
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
        "StartedAt": "2026-10-13T01:00:00Z",
        "FinishedAt": "2026-10-13T01:01:00Z",
    }
    image = identity["image_identity"]
    assert isinstance(image, dict)
    export = {
        "status": "complete",
        "stopped": True,
        "container_id": "f" * 64,
        "project": "pluginbench-" + "f" * 32 + "__env",
        "state": state,
        "snapshot_digest": inspect_snapshot(area / "workspace")["digest"],
        "source_metadata_digest": harbor_backend.native_task_assets(area / "workspace"),
        "image_id": image["image_id"],
        "workdir": "/app",
        "resources": runtime["resources"],
    }
    (artifacts / "submission-source.json").write_text(json.dumps(export))
    (artifacts / "before-source.json").write_text(
        json.dumps({**export, "snapshot_digest": identity["source_digest"]})
    )
    cleanup = {
        "status": "complete",
        "projects": {
            "coding": {
                "status": "complete",
                "residuals": {"containers": [], "networks": [], "images": []},
            }
        },
    }
    grader = identity["grader_image_identity"]
    assert isinstance(grader, dict)
    (artifacts / "runtime-isolation.json").write_text(
        json.dumps(
            {
                "export": export,
                "cleanup": cleanup,
                "verifier": {
                    "container_id": "a" * 64,
                    "image_id": grader["image_id"],
                    "state": state,
                    "resources": {"NanoCpus": 1_000_000_000, "Memory": 2048 * 1024 * 1024},
                },
            }
        )
    )
    # Synthetic native delivery proof for cache admission tests; actual discovery
    # is exercised separately through the real CLI in test_native_treatments.
    inventory: dict[str, list[object]] = {"guidance": [], "skills": [], "plugins": []}
    capabilities = {"subagents": True, "max_threads": 4, "max_depth": 1}
    treatment = {
        "status": "verified", "delivery": "native-instructions-skills-plugins-v1",
        "inventory": inventory,
        "effective_config": '''project_doc_max_bytes = 32768
[features]
code_mode = true
code_mode_only = true
plugins = true
remote_plugin = false
apps = false
hooks = true
multi_agent = true
multi_agent_v2 = false
[agents]
max_threads = 4
max_depth = 1
default_subagent_model = "gpt-6.1-sol"
default_subagent_reasoning_effort = "medium"
''',
    }
    (artifacts / "native-treatment.json").write_text(json.dumps(treatment))
    (artifacts / "prompt.txt").write_text("")
    (artifacts / "invocation.json").write_text(
        json.dumps(
            {
                "delivery": treatment["delivery"],
                "capabilities": capabilities,
                "treatment_inventory": inventory,
                "backend": harbor_backend.PROTOCOL,
                "harness": "codex-cli",
                "harness_version": version,
                "requested_model": "gpt-6.1-sol",
                "requested_effort": "medium",
                "timeout_seconds": timeout,
                "task_assets_sha256": identity["task_assets_sha256"],
                "native_task_identity": identity,
            }
        )
    )
    return {
        "native_treatment": treatment,
        "submission_collected": True,
        "successful_native_completion": True,
        "submission_export_digest": export["snapshot_digest"],
        "private_runtime_removed": True,
        "native_verifier_result": {"rewards": {"reward": reward}},
        "native_artifact_hashes": {
            submissions.PATCH_ARTIFACT: hashlib.sha256(patch.read_bytes()).hexdigest()
        },
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "elapsed_seconds": 1.0,
        "actual_primary_models": ["gpt-6.1-sol"],
        "runtime_effort_evidence": ["medium"],
        "startup": [{"type": "session_meta", "payload": {"cli_version": version}}],
    }
