"""Native task admission, frozen snapshots and verifier lifecycle contracts."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pluginbench import harbor_backend as harbor
from pluginbench.assessment_metrics import inspect_snapshot


def native_task(root: Path, *, bundled: bool = True, mode: str = "separate") -> Path:
    task = root / "task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "instruction.md").write_text("Fix the requested behavior.\n")
    (task / "environment/Dockerfile").write_text("FROM python:3.12\nWORKDIR /app\n")
    (task / "tests/test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    (task / "tests/test.sh").chmod(0o755)
    (task / "task.toml").write_text(
        'version = "1.0"\n[environment]\ncpus=1\nmemory_mb=256\n[verifier]\nenvironment_mode="'
        + mode
        + '"\n'
        + (
            '[verifier.environment]\ndocker_image="grader:1"\n'
            if bundled and mode == "separate"
            else ""
        )
    )
    return task


@pytest.mark.parametrize("bundled", [True, False])
def test_native_admission_uses_standard_topology_not_task_allowlist(
    tmp_path: Path, bundled: bool
) -> None:
    task = native_task(tmp_path, bundled=bundled)
    receipt = harbor.validate_native_task(task)
    assert receipt["verifier_bundled_tests"] is bundled
    assert receipt["task_assets_sha256"] == harbor.native_task_assets(task)


@pytest.mark.parametrize("unsafe", ["shared", "compose", "symlink"])
def test_unsupported_native_topology_fails_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    task = native_task(tmp_path, mode="shared" if unsafe == "shared" else "separate")
    if unsafe == "compose":
        (task / "environment/docker-compose.yaml").write_text(
            "services:\n  main:\n    privileged: true\n"
        )
    elif unsafe == "symlink":
        (task / "tests/linked.sh").symlink_to("test.sh")

    def docker(*arguments: str, **kwargs: object) -> str:
        pytest.fail("admission must precede Docker execution")

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError):
        harbor.validate_native_task(task)


def test_native_asset_hash_binds_test_permissions_and_bytes(tmp_path: Path) -> None:
    task = native_task(tmp_path)
    original = harbor.native_task_assets(task)
    (task / "tests/test.sh").chmod(0o644)
    assert harbor.native_task_assets(task) != original
    altered_mode = harbor.native_task_assets(task)
    (task / "tests/test.sh").write_text("different trusted checks")
    assert harbor.native_task_assets(task) != altered_mode


def test_native_verifier_bundling_preserves_exact_tests_and_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = native_task(tmp_path, bundled=False)
    observed: list[str] = []

    def image(*, directory: Path, image: str | None, expected: dict[str, object]) -> dict[str, str]:
        assert image is None
        observed.append((directory / "Dockerfile").read_text())
        assert (directory / "tests/test.sh").read_bytes() == (task / "tests/test.sh").read_bytes()
        assert (directory / "tests/test.sh").stat().st_mode == (
            task / "tests/test.sh"
        ).stat().st_mode
        return {"image_id": "sha256:grader"}

    monkeypatch.setattr(harbor, "_native_image", image)
    assert harbor._bundle_native_tests(
        tests=task / "tests",
        grader={"image": "verified-base", "image_id": "sha256:base"},
        expected={},
    ) == {"image_id": "sha256:grader"}
    assert observed == ["FROM verified-base\nCOPY tests/ /tests/\n"]


@pytest.mark.parametrize("changed", [False, True])
def test_public_hooks_freeze_before_model_and_before_native_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    (tmp_path / "artifacts").mkdir()
    events = importlib.import_module("harbor.trial.hooks").TrialEvent
    callbacks: dict[object, object] = {}

    def add_hook(event: object, callback: object) -> None:
        callbacks[event] = callback

    trial = SimpleNamespace(
        config=SimpleNamespace(trial_name="pluginbench-owned"),
        agent=SimpleNamespace(),
        add_hook=add_hook,
    )
    exports: list[dict[str, object]] = []

    def export(**kwargs: object) -> dict[str, object]:
        exports.append(kwargs)
        return {"snapshot_digest": "changed" if changed else "source", "stopped": True}

    monkeypatch.setattr(harbor, "export_native_source", export)
    harbor.install_native_hooks(
        trial,
        {
            "area": str(tmp_path),
            "runtime": {},
            "image": {"image_id": "frozen", "workdir": "/app"},
            "source_digest": "source",
            "harness": "codex",
            "version": "0.208.0",
        },
    )

    async def exercise() -> None:
        before = callbacks[events.AGENT_START]
        assert callable(before)
        if changed:
            with pytest.raises(ValueError, match="model call refused"):
                await before(None)
            assert events.VERIFICATION_START in callbacks
            return
        await before(None)
        after = callbacks[events.VERIFICATION_START]
        assert callable(after)
        await after(None)

    asyncio.run(exercise())
    assert exports[0]["destination"] == tmp_path / "before"
    assert exports[0]["resume"] is True
    assert exports[0]["fresh_source"] is True
    assert exports[0]["project"] == "pluginbench-owned__env"
    if not changed:
        assert exports[1]["destination"] == tmp_path / "workspace"
        assert exports[1]["fresh_source"] is True
        assert "resume" not in exports[1]
        assert (
            json.loads((tmp_path / "artifacts/submission-source.json").read_text())["stopped"]
            is True
        )


@pytest.mark.parametrize("fresh_source", [False, True])
@pytest.mark.parametrize("linked_cache", [False, True])
def test_native_export_stops_then_copies_original_repo_and_excludes_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_source: bool, linked_cache: bool
) -> None:
    calls: list[str] = []
    state = {
        "Running": False,
        "Status": "exited",
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
        "StartedAt": "start",
        "FinishedAt": "finish",
    }

    def container(**kwargs: object) -> dict[str, object]:
        calls.append("inspect")
        return {"Id": "id", "Image": "image", "State": state}

    def docker(*arguments: str, **kwargs: object) -> str:
        calls.append(arguments[0])
        if arguments[0] == "cp":
            assert arguments[1] == "id:/app/."
            raw = Path(arguments[2])
            (raw / "code.py").write_text("print('source')\n")
            (raw / "node_modules").mkdir()
            (raw / "node_modules/linked").symlink_to("/outside")
            if linked_cache:
                (raw / ".pytest_cache").symlink_to(tmp_path, target_is_directory=True)
            else:
                (raw / ".pytest_cache").mkdir()
                (raw / ".pytest_cache/nodeids").write_text("saved cache bytes\n")
            (raw / "data").mkdir()
            (raw / "data/.pytest_cache").write_text("ordinary source file\n")
        return ""

    monkeypatch.setattr(harbor, "_native_container", container)
    monkeypatch.setattr(harbor, "_docker", docker)
    destination = tmp_path / "before"
    if linked_cache:
        with pytest.raises(ValueError, match="symlink"):
            harbor.export_native_source(
                destination=destination,
                project="owned",
                trial_dir=tmp_path,
                workdir="/app",
                expected={},
                fresh_source=fresh_source,
            )
        return
    receipt = harbor.export_native_source(
        destination=destination,
        project="owned",
        trial_dir=tmp_path,
        workdir="/app",
        expected={},
        resume=True,
        fresh_source=fresh_source,
    )
    assert calls == ["inspect", "stop", "inspect", "cp", "start"]
    assert receipt["snapshot_digest"] == inspect_snapshot(destination)["digest"]
    assert (destination / "code.py").read_text() == "print('source')\n"
    assert (destination / "data/.pytest_cache").read_text() == "ordinary source file\n"
    assert not (destination / "node_modules").exists()
    assert receipt["capture_policy"] == ("fresh-source-v1" if fresh_source else "exact-replay-v1")
    assert receipt["excluded_runtime_paths"] == ([".pytest_cache"] if fresh_source else [])
    if fresh_source:
        assert not (destination / ".pytest_cache").exists()
    else:
        assert (destination / ".pytest_cache/nodeids").read_text() == "saved cache bytes\n"


@pytest.mark.parametrize(
    "mount",
    [
        {"Type": "bind", "Source": "/user/secrets", "Destination": "/app"},
        {"Type": "volume", "Destination": "/app"},
    ],
)
def test_native_container_rejects_unapproved_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mount: dict[str, str]
) -> None:
    description = {
        "Id": "a" * 64,
        "Image": "sha256:image",
        "Config": {
            "Labels": {"com.docker.compose.project": "owned", "com.docker.compose.service": "main"}
        },
        "HostConfig": {"NanoCpus": 10**9, "Memory": 256 * 1024**2},
        "Mounts": [mount],
    }
    monkeypatch.setattr(harbor, "_assert_daemon", lambda expected: None)
    monkeypatch.setattr(harbor, "_project_ids", lambda kind, project, expected: ["container"])
    monkeypatch.setattr(harbor, "_docker", lambda *arguments, **kwargs: json.dumps([description]))
    with pytest.raises(ValueError, match="unapproved host mount"):
        harbor._native_container(
            project="owned",
            trial_dir=tmp_path,
            expected={"resources": {"cpus": 1, "memory_mb": 256}},
        )


@pytest.mark.parametrize("bundled", [True, False])
@pytest.mark.parametrize("qualification_cleanup", ["complete", "incomplete"])
def test_native_preparation_freezes_both_images_and_source_without_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundled: bool, qualification_cleanup: str
) -> None:
    task = native_task(tmp_path, bundled=bundled)
    base = {
        "image": "sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64,
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    grader = {**base, "image": "sha256:" + "b" * 64, "image_id": "sha256:" + "b" * 64}
    installed = {**base, "image": "sha256:" + "c" * 64, "image_id": "sha256:" + "c" * 64}
    images = iter([base, grader, installed])
    monkeypatch.setattr(
        harbor, "runtime_identity", lambda **kwargs: {"resources": {"cpus": 1, "memory_mb": 256}}
    )
    monkeypatch.setattr(harbor, "_native_image", lambda **kwargs: next(images))
    monkeypatch.setattr(harbor, "_bundle_native_tests", lambda **kwargs: grader)

    def process(**kwargs: object) -> dict[str, object]:
        config = kwargs["config"]
        assert isinstance(config, dict)
        assert config["install_only"] is True
        agent = config["agent"]
        assert isinstance(agent, dict)
        assert agent["model_name"] is None
        return {
            "session_id": str(config["trial_name"]) + "__env",
            "result": {"exception_info": None},
        }

    monkeypatch.setattr(harbor, "_native_process", process)
    monkeypatch.setattr(harbor, "_native_container", lambda **kwargs: {"Id": "container"})
    monkeypatch.setattr(
        harbor,
        "_docker",
        lambda *args, **kwargs: installed["image_id"] if args[0] == "commit" else "",
    )

    def export(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        destination.mkdir()
        (destination / "code.py").write_text("source\n")
        return {"snapshot_digest": inspect_snapshot(destination)["digest"]}

    monkeypatch.setattr(harbor, "export_native_source", export)
    projects: list[str] = []

    def cleanup(
        *, project: str, owner: str, expected_runtime: dict[str, object]
    ) -> dict[str, object]:
        projects.append(project)
        return {"status": "complete"}

    monkeypatch.setattr(harbor, "cleanup", cleanup)
    if qualification_cleanup == "incomplete":
        monkeypatch.setattr(
            harbor,
            "_qualify_native_source",
            lambda **kwargs: {"status": "unknown", "cleanup": {"status": "incomplete"}},
        )
        with pytest.raises(ValueError, match="qualification cleanup incomplete"):
            harbor.prepare_native_task(
                task_dir=task,
                destination=tmp_path / "prepared",
                harness="codex",
                version="0.208.0",
                limits={"cpus": 1, "memory_mb": 256},
            )
        failed = json.loads((tmp_path / "prepared/artifacts/native-preparation.json").read_text())
        assert failed["status"] == "incomplete"
        assert failed["source_qualification"]["cleanup"]["status"] == "incomplete"
        assert failed["cleanup"]["status"] == "complete"
        return
    result = harbor.prepare_native_task(
        task_dir=task,
        destination=tmp_path / "prepared",
        harness="codex",
        version="0.208.0",
        limits={"cpus": 1, "memory_mb": 256},
    )
    assert result["image_identity"] == installed
    assert result["grader_image_identity"] == grader
    frozen = Path(str(result["task_dir"]))
    assert result["task_assets_sha256"] == harbor.native_task_assets(frozen)
    assert (frozen / "tests/test.sh").read_bytes() == (task / "tests/test.sh").read_bytes()
    assert result["source_digest"] == inspect_snapshot(Path(str(result["fixture_dir"])))["digest"]
    assert result["verifier_test_delivery"] == (
        "native-image" if bundled else "native-upload-frozen-into-image"
    )
    assert len(projects) == 1 and projects[0].endswith("__env")
    qualification = result["source_qualification"]
    assert isinstance(qualification, dict)
    assert qualification["status"] == "unknown"


@pytest.mark.parametrize(
    "failure",
    [None, "head", "source", "payload", "cleanup", "environment", "entrypoint", "workdir"],
)
def test_source_qualification_binds_grader_base_payload_and_coding_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    from pluginbench import submissions

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "code.py").write_text("value = 1\n")
    base = "a" * 40
    payload = {
        "test.sh": b"#!/bin/sh\n",
        "test.patch": b"",
        "grader.py": b"grader\n",
        "config.json": b"{}\n",
    }
    contract = {
        "base_commit": base,
        "verifier_payload": {
            name: hashlib.sha256(content).hexdigest() for name, content in payload.items()
        },
    }
    monkeypatch.setattr(submissions, "reviewed_contract", lambda task_dir: contract)
    image_config: dict[str, object] = {"WorkingDir": "/app", "Entrypoint": [], "Env": []}
    if failure == "environment":
        image_config["Env"] = ["APP_DIR=/other"]
    elif failure == "entrypoint":
        image_config["Entrypoint"] = ["/bin/sh", "-c", 'touch /app/inherited.txt; exec "$@"']
    elif failure == "workdir":
        image_config["WorkingDir"] = "/other"
    monkeypatch.setattr(
        harbor, "_inspect_image", lambda *args, **kwargs: {"Id": "grader", "Config": image_config}
    )
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, **kwargs: object) -> str:
        calls.append(arguments)
        if arguments[0] == "create":
            return "container"
        if arguments[0] == "wait":
            return "0"
        if arguments[0] == "logs":
            return (
                ("b" * 40 if failure == "head" else base)
                + "\n100644 blob "
                + "c" * 40
                + "\tcode.py\0"
            )
        if arguments[0] == "cp":
            target = Path(arguments[2])
            for name, content in payload.items():
                (target / name).write_bytes(content)
            (target / "test.sh").chmod(0o755)
            if failure == "payload":
                (target / "grader.py").write_text("different grader\n")
        return ""

    def export(**kwargs: object) -> dict[str, object]:
        import shutil

        target = kwargs["destination"]
        assert isinstance(target, Path)
        shutil.copytree(fixture, target)
        if failure == "source":
            (target / "code.py").chmod(0o755)
        return {
            "snapshot_digest": inspect_snapshot(target)["digest"],
            "source_metadata_digest": harbor.native_task_assets(target),
        }

    monkeypatch.setattr(harbor, "_docker", docker)
    monkeypatch.setattr(harbor, "export_native_source", export)
    cleanup_calls: list[object] = []

    def cleanup(**kwargs: object) -> dict[str, object]:
        cleanup_calls.append(kwargs)
        return {"status": "incomplete" if failure == "cleanup" else "complete"}

    monkeypatch.setattr(harbor, "cleanup", cleanup)
    proof = harbor._qualify_native_source(
        task_dir=tmp_path,
        fixture=fixture,
        coding_image={"image_id": "coding", "workdir": "/app"},
        grader={"image_id": "grader", "workdir": "/app"},
        expected={"resources": {"cpus": 1, "memory_mb": 256}},
        destination=tmp_path / "proof",
    )
    assert proof["status"] == ("complete" if failure is None else "unknown")
    assert len(cleanup_calls) == 1
    if failure in {"environment", "entrypoint", "workdir"}:
        assert calls == []
        assert "image" in str(proof["reason"])
        return
    create = calls[0]
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--cpus") + 1] == "1"
    assert create[create.index("--memory") + 1] == "256m"
    assert "ls-tree -r -z --full-tree " + base in create[-1]
    if failure is None:
        assert proof["base_tree"] == {
            "code.py": {"mode": "100644", "type": "blob", "blob": "c" * 40}
        }
        assert proof["fixture_metadata_digest"] == harbor.native_task_assets(fixture)
    assert (
        json.loads((tmp_path / "proof/qualification.json").read_text())["status"] == proof["status"]
    )


@pytest.mark.parametrize("result_kind", ["success", "failure", "timeout", "host-metadata"])
def test_native_measurement_uses_frozen_source_and_preserves_missing_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_kind: str
) -> None:
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("code\n")
    identity: dict[str, object] = {
        "isolation": {
            "resources": {"cpus": 1, "memory_mb": 256},
            "docker_engine": {"endpoint": "unix:///frozen.sock"},
            "docker_cli": {},
        },
        "image_identity": {"image_id": "sha256:frozen"},
        "workdir": "/app",
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(
        harbor, "_frozen_executable", lambda identity, label: {"resolved_path": "/frozen/docker"}
    )
    docker_calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, **kwargs: object) -> str:
        docker_calls.append(arguments)
        return "container" if arguments[0] == "create" else ""

    monkeypatch.setattr(harbor, "_docker", docker)

    def export(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        destination.mkdir()
        (destination / "code.py").write_text("code\n")
        if destination.name == "after" and result_kind == "success":
            (destination / "coverage.info").write_text("SF:code.py\nDA:1,1\nend_of_record\n")
        return {"snapshot_digest": inspect_snapshot(destination)["digest"]}

    monkeypatch.setattr(harbor, "export_native_source", export)

    def command(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["/frozen/docker", "--host", "unix:///frozen.sock"]
        assert argv[-3:] == ["python", "-m", "tests"]
        if result_kind == "timeout":
            raise subprocess.TimeoutExpired(
                argv, timeout, output=b"partial stdout", stderr=b"partial stderr"
            )
        if result_kind == "host-metadata":
            (source / "code.py").chmod(0o755)
        return subprocess.CompletedProcess(
            argv, 0 if result_kind == "success" else 1, "stdout", "stderr"
        )

    monkeypatch.setattr(harbor, "run_command", command)
    cleanups: list[str] = []

    def cleanup(
        *, project: str, owner: str, expected_runtime: dict[str, object]
    ) -> dict[str, object]:
        cleanups.append(project)
        return {"status": "complete"}

    monkeypatch.setattr(harbor, "cleanup", cleanup)
    output = tmp_path / "measurement"
    original = inspect_snapshot(source)["digest"]
    if result_kind == "host-metadata":
        with pytest.raises(ValueError, match="Frozen measurement input changed"):
            harbor.run_native_command(
                snapshot=source,
                task_identity=identity,
                destination=output,
                command=["python", "-m", "tests"],
                report_path="coverage.info",
                timeout=10,
            )
        assert inspect_snapshot(source)["digest"] == original
        assert json.loads((output / "execution.json").read_text())["cleanup"] == {
            "status": "complete"
        }
        return
    receipt = harbor.run_native_command(
        snapshot=source,
        task_identity=identity,
        destination=output,
        command=["python", "-m", "tests"],
        report_path="coverage.info",
        timeout=10,
    )
    assert receipt["before_digest"] == original
    assert inspect_snapshot(source)["digest"] == original
    assert receipt["report_collected"] is (result_kind == "success")
    assert (output / "report.raw").exists() is (result_kind == "success")
    assert receipt["timed_out"] is (result_kind == "timeout")
    assert len(cleanups) == 1
    assert (
        docker_calls[0][0] == "create"
        and "--network" in docker_calls[0]
        and "none" in docker_calls[0]
    )
    assert json.loads((output / "execution.json").read_text())["cleanup"] == {"status": "complete"}


def saved_native_artifacts(root: Path) -> tuple[Path, dict[str, str]]:
    artifacts = root / "saved-artifacts"
    source = artifacts / "harbor/trial"
    (source / "artifacts/logs/artifacts").mkdir(parents=True)
    (source / "result.json").write_text('{"task_name":"task"}')
    (source / "artifacts/logs/artifacts/output.patch").write_text("native artifact\n")
    (source / "artifacts/manifest.json").write_text(
        json.dumps(
            [
                {
                    "source": "/logs/artifacts",
                    "destination": "artifacts/logs/artifacts",
                    "type": "directory",
                    "status": "ok",
                    "service": None,
                    "exclude": [],
                },
                {
                    "source": "/logs/artifacts/output.patch",
                    "destination": "artifacts/logs/artifacts/output.patch",
                    "type": "file",
                    "status": "skipped",
                    "service": None,
                    "exclude": [],
                },
            ]
        )
    )
    hashes = {
        path.relative_to(source / "artifacts").as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in (source / "artifacts").rglob("*")
        if path.is_file()
    }
    return artifacts, hashes


@pytest.mark.parametrize("redundant", [False, True])
def test_native_regrade_replays_verifier_without_agent_or_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redundant: bool
) -> None:
    task = native_task(tmp_path)
    if redundant:
        task_config = task / "task.toml"
        task_config.write_text(
            'artifacts=["/logs/artifacts/output.patch"]\n' + task_config.read_text()
        )
    artifacts, hashes = saved_native_artifacts(tmp_path)
    source = artifacts / "harbor/trial"
    source_digest = harbor.native_task_assets(source)
    identity: dict[str, object] = {
        "isolation": {"resources": {"cpus": 1, "memory_mb": 256}},
        "task_assets_sha256": harbor.native_task_assets(task),
        "grader_image_identity": {"image": "sha256:grader", "image_id": "sha256:grader"},
        "toolchain_identity": {"harness": "codex", "harness_version": "0.208.0"},
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(harbor, "_inspect_image", lambda *args, **kwargs: {"Id": "sha256:grader"})

    def process(**kwargs: object) -> dict[str, object]:
        assert (
            kwargs["install_only"] is True
        )  # Private no-auth environment; TrialConfig stays regrade.
        assert "auth_file" not in kwargs
        config = kwargs["config"]
        assert isinstance(config, dict)
        trial_config = importlib.import_module("harbor.models.trial.config").TrialConfig
        native = trial_config.model_validate(config)
        assert native.source_trial.type == "local" and native.source_trial.path == source
        assert native.is_regrade
        replay = importlib.import_module("harbor.models.task.task").Task(native.task.path)
        original = importlib.import_module("harbor.models.task.task").Task(task)
        assert replay.name == original.name == "task"
        assert replay.task_dir.name == task.name
        original_config = original.config.model_dump(mode="json")
        replay_config = replay.config.model_dump(mode="json")
        assert replay_config.pop("artifacts") == []
        original_config.pop("artifacts")
        assert replay_config == original_config
        assert (replay.task_dir / "tests/test.sh").read_bytes() == (
            task / "tests/test.sh"
        ).read_bytes()
        assert (replay.task_dir / "tests/test.sh").stat().st_mode == (
            task / "tests/test.sh"
        ).stat().st_mode
        assert "install_only" not in config
        agent = config["agent"]
        assert isinstance(agent, dict) and agent["model_name"] is None
        return {
            "session_id": str(config["trial_name"]) + "__env",
            "result": {"verifier_result": {"rewards": {"reward": 1}}, "exception_info": None},
        }

    monkeypatch.setattr(harbor, "_native_process", process)
    state = {
        "Running": False,
        "Status": "exited",
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
        "StartedAt": "start",
        "FinishedAt": "finish",
    }
    monkeypatch.setattr(
        harbor,
        "_native_container",
        lambda **kwargs: {"Id": "verifier", "Image": "sha256:grader", "State": state},
    )
    projects: list[str] = []

    def cleanup(
        *, project: str, owner: str, expected_runtime: dict[str, object]
    ) -> dict[str, object]:
        projects.append(project)
        return {"status": "complete"}

    monkeypatch.setattr(harbor, "cleanup", cleanup)
    result = harbor.regrade_native(
        artifacts=artifacts,
        task_dir=task,
        identity=identity,
        directory=tmp_path / "regrade",
        artifact_hashes=hashes,
    )
    assert result["verifier_result"] == {"rewards": {"reward": 1}}
    assert harbor.native_task_assets(source) == source_digest
    assert harbor.native_task_assets(task) == identity["task_assets_sha256"]
    assert len(projects) == 2
    assert projects[0].endswith("__env") and projects[1].endswith("__verifier__trial")
    receipt = result["receipt"]
    assert isinstance(receipt, dict) and receipt["model_calls"] == 0
    assert receipt["source_sha256"] == source_digest
    assert receipt["observer_identity"] == identity["isolation"]
    proof = receipt["task_normalization"]
    assert isinstance(proof, dict)
    removed = proof["removed_declarations"]
    assert isinstance(removed, list) and len(removed) == int(redundant)
    assert proof["original_task_assets_sha256"] == identity["task_assets_sha256"]
    if redundant:
        assert proof["manifest_sha256"] == hashes["manifest.json"]
        assert proof["derived_task_assets_sha256"] != proof["original_task_assets_sha256"]


def test_native_regrade_normalization_satisfies_real_sdk_artifact_coverage(tmp_path: Path) -> None:
    task = native_task(tmp_path)
    config = task / "task.toml"
    config.write_text('artifacts=["/logs/artifacts/output.patch"]\n' + config.read_text())
    artifacts, hashes = saved_native_artifacts(tmp_path)
    source = artifacts / "harbor/trial"
    derived, _ = harbor._native_regrade_task(
        task_dir=task, source=source, directory=tmp_path / "observer", artifact_hashes=hashes
    )
    task_type = importlib.import_module("harbor.models.task.task").Task
    regrade = importlib.import_module("harbor.trial.regrade")
    observer = SimpleNamespace(
        agent_env_paths=SimpleNamespace(artifacts_dir=Path("/logs/artifacts"))
    )
    entries = regrade.read_artifact_manifest(source)
    arguments = {
        "source_artifacts_dir": source / "artifacts",
        "source_record_dir": source,
        "entries": entries,
    }
    assert regrade.RegradeTrial._artifact_coverage_problems(
        observer, **arguments, declared_artifacts=task_type(task).config.artifacts
    ) == [
        "/logs/artifacts/output.patch: skipped in the source trial (host-path collision); "
        "the recorded bytes belong to a different artifact"
    ]
    assert (
        regrade.RegradeTrial._artifact_coverage_problems(
            observer, **arguments, declared_artifacts=task_type(derived).config.artifacts
        )
        == []
    )
    assert entries[1].status == "skipped"


@pytest.mark.parametrize(
    "unproven", ["missing", "changed", "manifest", "filtered", "empty", "wrong-destination"]
)
def test_native_regrade_refuses_unproven_parent_or_child(tmp_path: Path, unproven: str) -> None:
    task = native_task(tmp_path)
    config = task / "task.toml"
    config.write_text('artifacts=["/logs/artifacts/output.patch"]\n' + config.read_text())
    artifacts, hashes = saved_native_artifacts(tmp_path)
    source = artifacts / "harbor/trial"
    child = source / "artifacts/logs/artifacts/output.patch"
    if unproven == "missing":
        child.unlink()
    elif unproven == "changed":
        child.write_text("tampered")
    else:
        manifest = source / "artifacts/manifest.json"
        entries = json.loads(manifest.read_text())
        if unproven == "filtered":
            entries[0]["exclude"] = ["*.patch"]
        elif unproven == "empty":
            entries[0]["status"] = "empty"
        elif unproven == "wrong-destination":
            entries[0]["destination"] = "artifacts/other"
        else:
            entries[0]["status"] = "failed"
        manifest.write_text(json.dumps(entries))
        # A genuinely frozen incompatible manifest is still not proof of captured bytes.
        if unproven != "manifest":
            hashes["manifest.json"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="frozen hashes|unfiltered convention parent"):
        harbor._native_regrade_task(
            task_dir=task, source=source, directory=tmp_path / "observer", artifact_hashes=hashes
        )
    assert not (tmp_path / "observer/regrade-task").exists()


@pytest.mark.parametrize(
    "declaration",
    [
        '{source="/logs/artifacts/output.patch", service="helper"}',
        '{source="/logs/artifacts/output.patch", destination="other.patch"}',
        '{source="/logs/artifacts/output.patch", exclude=["*.patch"]}',
        '{source="/logs/artifacts", exclude=["*.patch"]}, "/logs/artifacts/output.patch"',
        '{source="/logs/artifacts", destination="other"}, "/logs/artifacts/output.patch"',
    ],
)
def test_native_regrade_retains_nonredundant_artifact_policies(
    tmp_path: Path, declaration: str
) -> None:
    task = native_task(tmp_path)
    config = task / "task.toml"
    config.write_text("artifacts=[" + declaration + "]\n" + config.read_text())
    artifacts, hashes = saved_native_artifacts(tmp_path)
    original = harbor.native_task_assets(task)
    derived, proof = harbor._native_regrade_task(
        task_dir=task,
        source=artifacts / "harbor/trial",
        directory=tmp_path / "observer",
        artifact_hashes=hashes,
    )
    assert derived == task
    assert proof["removed_declarations"] == []
    assert harbor.native_task_assets(task) == original


def test_native_preparation_cache_reuses_exact_image_source_and_portable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    task = native_task(tmp_path)
    image: dict[str, str] = {
        "image": "sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64,
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    expected: dict[str, object] = {"resources": {"cpus": 1, "memory_mb": 256}}
    monkeypatch.setattr(harbor, "runtime_identity", lambda **kwargs: expected)
    monkeypatch.setattr(harbor, "_native_image", lambda **kwargs: image)
    monkeypatch.setattr(harbor, "_inspect_image", lambda reference, **kwargs: {"Id": reference})
    preparations: list[Path] = []

    def prepare(
        *,
        task_dir: Path,
        destination: Path,
        harness: str,
        version: str,
        limits: dict[str, int],
        resolved: object = None,
    ) -> dict[str, object]:
        preparations.append(destination)
        (destination / "artifacts").mkdir(parents=True)
        frozen = destination / "task"
        shutil.copytree(task_dir, frozen)
        fixture = destination / "fixture"
        fixture.mkdir()
        (fixture / "code.py").write_text("starting source\n")
        receipt: dict[str, object] = {
            "status": "complete",
            "cleanup": {"status": "complete", "project": "nonce"},
            "task_dir": str(frozen),
            "fixture_dir": str(fixture),
            "isolation": expected,
            "source_digest": inspect_snapshot(fixture)["digest"],
            "task_assets_sha256": harbor.native_task_assets(frozen),
            "image_identity": image,
            "grader_image_identity": image,
        }
        (destination / "artifacts/native-preparation.json").write_text(json.dumps(receipt))
        return receipt

    monkeypatch.setattr(harbor, "_prepare_native_task", prepare)

    def run(destination: Path) -> dict[str, object]:
        return harbor.prepare_native_task(
            task_dir=task,
            destination=destination,
            harness="codex",
            version="0.208.0",
            limits={"cpus": 1, "memory_mb": 256},
            cache=tmp_path / "cache",
        )

    first = run(tmp_path / "first")
    second = run(tmp_path / "second")
    assert len(preparations) == 1
    assert first["preparation_key"] == second["preparation_key"]
    assert first["preparation_identity"] == second["preparation_identity"]
    assert first["image_identity"] == second["image_identity"]
    assert first["task_dir"] != second["task_dir"]
    cached = tmp_path / "cache" / str(first["preparation_key"])
    (cached / "fixture/code.py").write_text("tampered source\n")
    with pytest.raises(ValueError, match="evidence changed"):
        run(tmp_path / "rejected")
    assert len(preparations) == 1 and not (tmp_path / "rejected").exists()


def test_native_model_transport_allowlist_preserves_task_network_policy(tmp_path: Path) -> None:
    task_dir = native_task(tmp_path)
    config_path = task_dir / "task.toml"
    config_path.write_text(config_path.read_text() + '[agent]\nnetwork_mode="no-network"\n')
    task_type = importlib.import_module("harbor.models.task.task").Task
    task = task_type(task_dir)
    config_type = importlib.import_module("harbor.models.trial.config").TrialConfig
    config = config_type.model_validate(
        harbor._native_config(
            task_dir=task_dir,
            area=tmp_path,
            owner="owned",
            harness="codex",
            version="0.208.0",
            limits={"cpus": 1, "memory_mb": 256},
            timeout=600,
        )
    )
    policy = importlib.import_module("harbor.trial.network_policy")
    baseline = policy.resolve_agent_env_baseline(task.config, config.environment)
    effective = policy.resolve_agent_phase_policy(task.config, config.agent, baseline)
    assert task.config.agent.network_mode.value == "no-network"
    assert effective.network_mode.value == "allowlist"
    assert effective.allowed_hosts == harbor.NATIVE_PROVIDER_HOSTS["codex"]
    assert "github.com" not in effective.allowed_hosts


@pytest.mark.parametrize("rebound", [False, True])
def test_native_build_reference_preserves_immutable_image_and_rejects_rebinding(
    monkeypatch: pytest.MonkeyPatch, rebound: bool
) -> None:
    image_id = "sha256:" + "a" * 64
    reference = "pluginbench-native:sha256-" + "a" * 64
    aliases: dict[str, str] = {reference: "sha256:" + "b" * 64} if rebound else {}
    calls: list[tuple[str, ...]] = []

    def inspect(
        reference: str, *, pull: bool, expected_runtime: dict[str, object]
    ) -> dict[str, object]:
        assert pull is False
        if reference not in aliases:
            raise ValueError("No such image")
        return {"Id": aliases[reference]}

    def docker(*arguments: str, **kwargs: object) -> str:
        calls.append(arguments)
        assert arguments == ("tag", image_id, reference)
        aliases[reference] = image_id
        return ""

    monkeypatch.setattr(harbor, "_inspect_image", inspect)
    monkeypatch.setattr(harbor, "_docker", docker)
    if rebound:
        with pytest.raises(ValueError, match="rebound"):
            harbor._native_image_reference(image_id=image_id, expected={})
        assert calls == []
    else:
        assert harbor._native_image_reference(image_id=image_id, expected={}) == reference
        assert calls == [("tag", image_id, reference)]
        assert harbor._native_image_reference(image_id=image_id, expected={}) == reference
        assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["modify", "new-source", "new-test", "chmod"])
def test_measurement_rejects_mutated_container_source_before_accepting_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("selected source\n")
    identity: dict[str, object] = {
        "isolation": {
            "resources": {"cpus": 1, "memory_mb": 256},
            "docker_engine": {"endpoint": "unix:///frozen.sock"},
            "docker_cli": {},
        },
        "image_identity": {"image_id": "sha256:frozen"},
        "workdir": "/app",
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(
        harbor, "_frozen_executable", lambda identity, label: {"resolved_path": "/frozen/docker"}
    )
    monkeypatch.setattr(
        harbor, "_docker", lambda *args, **kwargs: "container" if args[0] == "create" else ""
    )

    def export(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        destination.mkdir()
        (destination / "code.py").write_text("selected source\n")
        if destination.name == "after":
            (destination / "coverage.info").write_text("SF:code.py\nDA:1,1\nend_of_record\n")
            if mutation == "modify":
                (destination / "code.py").write_text("different code measured\n")
            elif mutation == "new-source":
                (destination / "new.py").write_text("new production code measured\n")
            elif mutation == "new-test":
                (destination / "new.test.py").write_text("new tests measured\n")
            else:
                (destination / "code.py").chmod(0o755)
        return {"snapshot_digest": inspect_snapshot(destination)["digest"]}

    monkeypatch.setattr(harbor, "export_native_source", export)
    monkeypatch.setattr(
        harbor,
        "run_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(harbor, "cleanup", lambda **kwargs: {"status": "complete"})
    destination = tmp_path / "measurement"
    with pytest.raises(ValueError, match="measurement.*source"):
        harbor.run_native_command(
            snapshot=source,
            task_identity=identity,
            destination=destination,
            command=["test"],
            report_path="coverage.info",
            timeout=10,
        )
    assert not (destination / "report.raw").exists()
    assert json.loads((destination / "execution.json").read_text())["report_collected"] is False


def test_install_only_qualifies_and_sanitizes_while_native_network_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []
    alive = {"main": True, "egress": True}

    async def original_install(environment: object) -> None:
        assert alive["main"] and alive["egress"]
        steps.append("native-install")

    async def agent_exec(environment: object, *, command: str) -> object:
        assert alive["main"] and alive["egress"]
        assert "codex-cli 0.208.0" in command
        assert command.index(". ~/.nvm/nvm.sh") < command.index("set -eu")
        assert "find /app" not in command
        steps.append("qualify-as-agent")
        return SimpleNamespace(return_code=0)

    async def root_exec(environment: object, *, command: str) -> object:
        assert alive["main"] and alive["egress"]
        assert "codex --version" not in command and "find /app" in command
        assert "AGENTS.md" in command
        steps.append("sanitize-as-root")
        return SimpleNamespace(return_code=0)

    def hook(event: object, callback: object) -> None:
        pytest.fail("Install-only must not register model or verification hooks")

    agent = SimpleNamespace(
        install=original_install, exec_as_root=root_exec, exec_as_agent=agent_exec
    )
    trial = SimpleNamespace(
        config=SimpleNamespace(trial_name="pluginbench-owned"), agent=agent, add_hook=hook
    )
    monkeypatch.setattr(
        harbor,
        "_docker",
        lambda *args, **kwargs: pytest.fail(
            "Native setup must not restart stopped network namespaces"
        ),
    )
    harbor.install_native_hooks(
        trial,
        {
            "area": str(tmp_path),
            "runtime": {},
            "image": {"workdir": "/app"},
            "harness": "codex",
            "version": "0.208.0",
            "install_only": True,
        },
    )
    asyncio.run(agent.install(object()))
    assert steps == ["native-install", "qualify-as-agent", "sanitize-as-root"]


def test_failed_preparation_preserves_evidence_without_publishing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = native_task(tmp_path)
    image = {
        "image": "image",
        "image_id": "sha256:image",
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    monkeypatch.setattr(harbor, "runtime_identity", lambda **kwargs: {})
    monkeypatch.setattr(harbor, "_native_image", lambda **kwargs: image)

    def fail(**kwargs: object) -> dict[str, object]:
        prepared = kwargs["destination"]
        assert isinstance(prepared, Path)
        (prepared / "artifacts").mkdir(parents=True)
        (prepared / "artifacts/native-preparation.json").write_text(
            '{"status":"failed","cleanup":{"status":"complete"}}'
        )
        (prepared / "artifacts/harbor-worker.stderr").write_text("setup failure evidence")
        raise ValueError("native setup failed")

    monkeypatch.setattr(harbor, "_prepare_native_task", fail)
    output, cache = tmp_path / "failed", tmp_path / "cache"
    with pytest.raises(ValueError, match="native setup failed"):
        harbor.prepare_native_task(
            task_dir=task,
            destination=output,
            harness="codex",
            version="0.208.0",
            limits={"cpus": 1, "memory_mb": 256},
            cache=cache,
        )
    assert (output / "artifacts/harbor-worker.stderr").read_text() == "setup failure evidence"
    assert (
        json.loads((output / "artifacts/native-preparation.json").read_text())["cleanup"]["status"]
        == "complete"
    )
    assert (
        json.loads((output / "artifacts/preparation-failure.json").read_text())["status"]
        == "failed"
    )
    assert not any(path.is_dir() for path in cache.iterdir())
    assert not (output / "private").exists()


def test_actual_native_invocation_does_not_publish_submission_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = native_task(tmp_path)
    area = tmp_path / "attempt"
    area.mkdir()
    identity: dict[str, object] = {
        "task_assets_sha256": harbor.native_task_assets(task_dir),
        "source_digest": "prepared-source",
        "workdir": "/app",
        "image_identity": {"image": "coding", "image_id": "coding"},
        "grader_image_identity": {"image": "grader", "image_id": "grader"},
        "toolchain_identity": {"harness": "codex", "harness_version": "0.208.0"},
        "transport_policy": {
            "name": harbor.NATIVE_TRANSPORT_POLICY,
            "provider_hosts": harbor.NATIVE_PROVIDER_HOSTS["codex"],
        },
    }
    protocol: dict[str, object] = {
        "native_task_identity": identity,
        "isolation": {"resources": {"cpus": 1, "memory_mb": 256}},
        "harness_version": "0.208.0",
        "reasoning": "low",
        "models": {"codex": "fake-model"},
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(harbor, "_assert_native_image", lambda image, expected: None)

    def process(**kwargs: object) -> dict[str, object]:
        config = kwargs["config"]
        assert isinstance(config, dict)
        return {
            "session_id": str(config["trial_name"]) + "__env",
            "result": {"verifier_result": None, "exception_info": None},
        }

    monkeypatch.setattr(harbor, "_native_process", process)
    monkeypatch.setattr(
        harbor,
        "_native_submission",
        lambda **kwargs: {"status": "complete", "snapshot_digest": "frozen-submission"},
    )
    monkeypatch.setattr(
        harbor,
        "cleanup",
        lambda **kwargs: {"status": "incomplete", "residuals": ["owned-container"]},
    )
    with pytest.raises(ValueError, match="cleanup incomplete"):
        harbor.invoke_native(
            area=area,
            task_dir=task_dir,
            entry={"host": "codex"},
            protocol=protocol,
            prompt="",
            timeout=600,
        )
    execution = json.loads((area / "artifacts/execution.json").read_text())
    isolation = json.loads((area / "artifacts/runtime-isolation.json").read_text())
    assert execution["submission_collected"] is False
    assert execution["successful_native_completion"] is False
    assert execution["submission_export_digest"] == "frozen-submission"
    assert isolation["export"]["status"] == "complete"
    assert isolation["cleanup"]["status"] == "incomplete"


def test_measurement_report_cannot_overwrite_original_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.ts").write_text("original code\n")
    monkeypatch.setattr(
        harbor,
        "assert_runtime",
        lambda expected: pytest.fail("unsafe report must be rejected before Docker admission"),
    )
    with pytest.raises(ValueError, match="overwrite original inputs"):
        harbor.run_native_command(
            snapshot=source,
            task_identity={},
            destination=tmp_path / "measurement",
            command=["coverage"],
            report_path="index.ts",
            timeout=10,
        )


@pytest.mark.parametrize("report", ["coverage/lcov.info", "lcov.info"])
def test_measurement_allows_companion_assets_only_in_empty_configured_report_subtree(
    tmp_path: Path, report: str
) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "index.ts").write_text("original code\n")
    (after / "index.ts").write_text("original code\n")
    (after / "test").write_text("# TLS secrets log file, generated by OpenSSL / Python\n")
    (after / "coverage/lcov-report").mkdir(parents=True)
    (after / "coverage/lcov-report/prettify.js").write_text("generated coverage assets\n")
    inventory = harbor._native_asset_inventory(before)
    if report == "lcov.info":
        with pytest.raises(ValueError, match="introduced production or test source"):
            harbor._assert_measurement_source(
                original_inventory=inventory, after=after, report_path=report
            )
    else:
        harbor._assert_measurement_source(
            original_inventory=inventory, after=after, report_path=report
        )
        (before / "coverage").mkdir()
        (before / "coverage/original.ts").write_text("original source here\n")
        (after / "coverage/original.ts").write_text("original source here\n")
        with pytest.raises(ValueError, match="introduced production or test source"):
            harbor._assert_measurement_source(
                original_inventory=harbor._native_asset_inventory(before),
                after=after,
                report_path=report,
            )


def test_measurement_reconstructs_removed_directories_and_preserves_dependency_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import shutil

    source = tmp_path / "selected"
    source.mkdir()
    (source / "code.py").write_text("selected\n")
    (source / "with-deps").mkdir()
    image_source = tmp_path / "container-tree"
    (image_source / "removed/nested").mkdir(parents=True)
    (image_source / "stale.py").write_text("stale source\n")
    (image_source / "with-deps/node_modules/pkg").mkdir(parents=True)
    (image_source / "with-deps/node_modules/pkg/index.js").write_text("installed dependency\n")
    identity: dict[str, object] = {
        "isolation": {
            "resources": {"cpus": 1, "memory_mb": 256},
            "docker_engine": {"endpoint": "unix:///frozen.sock"},
            "docker_cli": {},
        },
        "image_identity": {"image_id": "sha256:frozen"},
        "workdir": "/app",
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(
        harbor, "_frozen_executable", lambda identity, label: {"resolved_path": "/frozen/docker"}
    )

    def docker(*arguments: str, **kwargs: object) -> str:
        if arguments[:4] == ("exec", "container", "bash", "-c"):
            command = arguments[4].replace("/app", str(image_source))
            completed = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
            if completed.returncode:
                raise ValueError(completed.stderr)
        if arguments[0] == "cp":
            shutil.copytree(source, image_source, dirs_exist_ok=True)
        return "container" if arguments[0] == "create" else ""

    monkeypatch.setattr(harbor, "_docker", docker)

    def export(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        shutil.copytree(image_source, destination, ignore=shutil.ignore_patterns("node_modules"))
        return {"snapshot_digest": inspect_snapshot(destination)["digest"]}

    monkeypatch.setattr(harbor, "export_native_source", export)

    def command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        (image_source / "coverage.info").write_text("SF:code.py\nDA:1,1\nend_of_record\n")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(harbor, "run_command", command)
    monkeypatch.setattr(harbor, "cleanup", lambda **kwargs: {"status": "complete"})
    result = harbor.run_native_command(
        snapshot=source,
        task_identity=identity,
        destination=tmp_path / "measurement",
        command=["test"],
        report_path="coverage.info",
        timeout=10,
    )
    assert result["report_collected"] is True
    assert not (image_source / "removed").exists()
    assert not (image_source / "stale.py").exists()
    assert (
        image_source / "with-deps/node_modules/pkg/index.js"
    ).read_text() == "installed dependency\n"


def test_codex_cli_qualification_loads_agent_nvm_before_strict_shell_flags(tmp_path: Path) -> None:
    import subprocess

    (tmp_path / ".nvm").mkdir()
    (tmp_path / "nvm-bin").mkdir()
    (tmp_path / ".nvm/nvm.sh").write_text(
        'export PATH="$HOME/nvm-bin:$PATH"\nif [ "$NVM_TEST_UNSET_VARIABLE" = x ]; then :; fi\n'
    )
    for name in ("curl", "bash", "node", "npm", "rg", "codex"):
        binary = tmp_path / "nvm-bin" / name
        binary.write_text(
            "#!/bin/sh\n" + ('echo "codex-cli 0.208.0"\n' if name == "codex" else "exit 0\n")
        )
        binary.chmod(0o755)
    command = harbor._native_cli_qualification("codex", "0.208.0")
    result = subprocess.run(
        ["bash", "-c", command],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_repeated_native_builds_keep_exact_inspected_identity_and_remove_temporary_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Quiet output can name an uninspectable config digest; inspect the loaded tag instead.
    builds: list[tuple[str, ...]] = []
    references: dict[str, str] = {}
    stable_id = "sha256:" + "a" * 64
    quiet_output = "sha256:" + "f" * 64
    inspections: list[str] = []

    def docker(*arguments: str, **kwargs: object) -> str:
        if arguments[0] == "build":
            builds.append(arguments)
            tag = arguments[arguments.index("--tag") + 1]
            references[tag] = (
                stable_id
                if "--provenance=false" in arguments
                else "sha256:" + str(len(builds)) * 64
            )
            return quiet_output
        if arguments[0] == "tag":
            references[arguments[2]] = arguments[1]
            return ""
        assert arguments[:3] == ("image", "rm", "--no-prune")
        tag = arguments[3]
        assert tag.startswith("pluginbench-native-build:")
        del references[tag]
        return ""

    def inspect(
        image: str, *, pull: bool, expected_runtime: dict[str, object]
    ) -> dict[str, object]:
        inspections.append(image)
        if image not in references:
            raise ValueError("No such image")
        return {
            "Id": references[image],
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {"WorkingDir": "/app"},
        }

    monkeypatch.setattr(harbor, "_docker", docker)
    monkeypatch.setattr(harbor, "_inspect_image", inspect)
    first = harbor._native_image(directory=tmp_path, image=None, expected={})
    second = harbor._native_image(directory=tmp_path, image=None, expected={})
    assert first == second
    assert first["image_id"] == stable_id
    assert len(builds) == 2
    assert quiet_output not in inspections
    assert not any(name.startswith("pluginbench-native-build:") for name in references)


@pytest.mark.parametrize("foreign_reference", [False, True])
def test_failed_native_build_never_removes_unqualified_or_foreign_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, foreign_reference: bool
) -> None:
    owned_tag: list[str] = []
    calls: list[tuple[str, ...]] = []

    def inspect(
        image: str, *, pull: bool, expected_runtime: dict[str, object]
    ) -> dict[str, object]:
        if owned_tag and foreign_reference:
            return {"Id": "sha256:" + "e" * 64}
        raise ValueError("No such image")

    def docker(*arguments: str, **kwargs: object) -> str:
        calls.append(arguments)
        assert arguments[0] == "build"
        owned_tag.append(arguments[arguments.index("--tag") + 1])
        raise ValueError("build failed")

    monkeypatch.setattr(harbor, "_inspect_image", inspect)
    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="build failed") as caught:
        harbor._native_image(directory=tmp_path, image=None, expected={})
    assert len(calls) == 1
    if foreign_reference:
        assert any("ownership" in note for note in caught.value.__notes__)
