"""The container boundary must fail closed without another model invocation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import os
import shlex
import sys
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from pluginbench import harbor_backend as harbor
from pluginbench import isolation, native_treatments
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.processes import run_command


def base_image() -> dict[str, str]:
    return {
        "image": "repo@sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64,
        "platform": "linux/arm64",
        "workdir": "/app",
    }


def control_reply(arguments: tuple[str, ...]) -> str | None:
    if arguments[0] == "info":
        return json.dumps({"ID": "daemon"})
    if arguments[:2] == ("image", "inspect"):
        return json.dumps([{"Config": container()["Config"]}])
    return None


def runtime() -> dict[str, Any]:
    return {
        "protocol": harbor.PROTOCOL,
        "harbor_version": harbor.HARBOR_VERSION,
        "codex_transport": {"version": "codex-stdout-stderr-v1", "harbor_run_sha256": "e" * 64},
        "implementation_sha256": "a" * 64,
        "docker_cli": {
            "path": "/frozen/docker",
            "resolved_path": "/frozen/docker",
            "version": "29.5.0",
            "sha256": "d" * 64,
        },
        "docker_engine": {
            "id": "daemon",
            "version": "29.5",
            "platform": "linux/arm64",
            "endpoint": "unix:///private/docker.sock",
        },
        "resources": {
            "cpus": 1,
            "memory_mb": 2048,
            "cpu_enforcement": "limit",
            "memory_enforcement": "limit",
        },
    }


def docker_plugins(directory: Path) -> dict[str, dict[str, str]]:
    directory.mkdir()
    result = {}
    for name, version in (("compose", "5.4.0"), ("buildx", "v0.34.1")):
        path = directory / ("docker-" + name)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        result[name] = {
            "path": str(path),
            "resolved_path": str(path.resolve()),
            "version": version,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return result


def docker_cli(directory: Path) -> dict[str, str]:
    path = directory / "docker"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "version": "29.5.0",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def container(
    *, running: bool = False, project: str = "owned", mounts: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "Id": "1" * 64,
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "main",
                "org.pluginbench.trial": "owner",
            }
        },
        "State": {
            "Running": running,
            "Status": "running" if running else "exited",
            "OOMKilled": False,
            "ExitCode": 137,
            "Error": "",
            "StartedAt": "2026-10-11T19:00:00Z",
            "FinishedAt": "2026-10-11T19:30:00Z",
        },
        "HostConfig": {"NanoCpus": 10**9, "Memory": 2048 * 1024**2},
        "Image": "sha256:" + "b" * 64,
        "Mounts": mounts or [],
    }


def test_cleanup_selects_only_owned_labels_and_proves_no_residuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = {"containers": ["ours"], "networks": ["network"], "images": ["image"]}
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        reply = control_reply(arguments)
        if reply is not None:
            return reply
        calls.append(arguments)
        if arguments[0] == "ps":
            assert arguments[-1] == "label=com.docker.compose.project=owned"
            return "\n".join(remaining["containers"])
        if arguments[:2] == ("network", "ls"):
            assert arguments[-1] == "label=com.docker.compose.project=owned"
            return "\n".join(remaining["networks"])
        if arguments[:2] == ("image", "ls"):
            assert arguments[-1] == "label=org.pluginbench.trial=unique-owner"
            return "\n".join(remaining["images"])
        key = "containers" if arguments[0] == "rm" else arguments[0] + "s"
        assert arguments[-1] == remaining[key][0]
        remaining[key] = []
        return ""

    monkeypatch.setattr(harbor, "_docker", docker)
    assert harbor.cleanup(project="owned", owner="unique-owner", expected_runtime=runtime()) == {
        "status": "complete",
        "residuals": remaining,
    }
    assert not any("prune" in call for call in calls)


def test_cleanup_query_failure_cannot_become_an_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def docker(*arguments: str, **kwargs: Any) -> str:
        raise ValueError("daemon unavailable")

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="daemon unavailable"):
        harbor.cleanup(project="owned", owner="owner", expected_runtime=runtime())


def test_capacity_uses_docker_vm_not_host_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        harbor, "_engine", lambda: ({"NCPU": 2, "MemTotal": 6 * 1024**3}, "unix:///docker")
    )
    assert harbor.capacity_jobs() == 2
    assert harbor.capacity_jobs(memory_mb=4096) == 1
    with pytest.raises(ValueError, match="does not fit"):
        harbor.capacity_jobs(memory_mb=8192)


def test_runtime_drift_rejects_before_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        harbor, "runtime_identity", lambda **kwargs: {**runtime(), "harbor_version": "other"}
    )
    with pytest.raises(isolation.RuntimeDriftError, match="changed"):
        harbor.assert_runtime(runtime())


def test_unverifiable_runtime_also_stops_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(**kwargs: Any) -> dict[str, object]:
        raise ValueError("Harbor version changed")

    monkeypatch.setattr(harbor, "runtime_identity", unavailable)
    with pytest.raises(isolation.RuntimeDriftError, match="Cannot verify"):
        harbor.assert_runtime(runtime())


def test_runtime_freezes_selected_docker_cli_and_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugins = docker_plugins(tmp_path / "plugins")
    cli = docker_cli(tmp_path / "plugins")
    info = {
        "ID": "daemon",
        "ServerVersion": "29.5.0",
        "OSType": "linux",
        "Architecture": "arm64",
        "NCPU": 2,
        "MemTotal": 6 * 1024**3,
        "ClientInfo": {
            "Version": cli["version"],
            "Plugins": [
                {"Name": name, "Path": identity["path"], "Version": identity["version"]}
                for name, identity in plugins.items()
            ],
        },
    }
    monkeypatch.setattr(harbor, "_engine", lambda: (info, "unix:///docker.sock"))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: harbor.HARBOR_VERSION)
    monkeypatch.setattr(shutil, "which", lambda name: cli["path"])
    monkeypatch.setattr(harbor, "_assert_codex_transport_source", lambda: harbor._CODEX_RUN_SHA256)
    actual = harbor.runtime_identity()
    assert actual["codex_transport"] == {
        "version": harbor._CODEX_TRANSPORT_VERSION,
        "harbor_run_sha256": harbor._CODEX_RUN_SHA256,
    }
    assert actual["docker_cli"] == cli
    assert actual["docker_plugins"] == plugins
    Path(plugins["compose"]["path"]).write_text("#!/bin/sh\nexit 1\n")
    assert harbor.runtime_identity()["docker_plugins"] != actual["docker_plugins"]
    with pytest.raises(isolation.RuntimeDriftError, match="changed"):
        harbor.assert_runtime(actual)


@pytest.mark.parametrize("component", ["docker", "compose", "buildx"])
@pytest.mark.parametrize("fault", ["missing", "changed"])
def test_changed_or_missing_frozen_docker_executable_stops_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str, fault: str
) -> None:
    plugins = docker_plugins(tmp_path / "plugins")
    cli = docker_cli(tmp_path / "plugins")
    selected = cli if component == "docker" else plugins[component]
    if fault == "missing":
        Path(selected["path"]).unlink()
    else:
        Path(selected["path"]).write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setattr(
        harbor, "run_command", lambda *args, **kwargs: pytest.fail("unexpected process launch")
    )
    with pytest.raises(isolation.RuntimeDriftError):
        harbor._private_docker_config(
            private=tmp_path / "private",
            env={"PATH": str(tmp_path / "plugins")},
            expected_runtime={**runtime(), "docker_plugins": plugins, "docker_cli": cli},
        )


def test_private_docker_discovery_rejects_different_plugin_bytes_despite_matching_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugins = docker_plugins(tmp_path / "plugins")
    cli = docker_cli(tmp_path / "plugins")
    foreign = tmp_path / "foreign-compose"
    foreign.write_text("#!/bin/sh\nexit 1\n")
    foreign.chmod(0o755)
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv == ["docker", "info", "--format", "{{json .ClientInfo}}"]
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "Version": cli["version"],
                    "Plugins": [
                        {
                            "Name": name,
                            "Path": str(foreign) if name == "compose" else identity["path"],
                            "Version": identity["version"],
                        }
                        for name, identity in plugins.items()
                    ],
                }
            ),
            "",
        )

    monkeypatch.setattr(harbor, "run_command", run)
    with pytest.raises(isolation.RuntimeDriftError, match="identity differs"):
        harbor._private_docker_config(
            private=tmp_path / "private",
            env={"PATH": str(tmp_path / "plugins")},
            expected_runtime={**runtime(), "docker_plugins": plugins, "docker_cli": cli},
        )
    assert len(calls) == 1


@pytest.mark.parametrize("image", ["repo:latest", "repo@sha256:short"])
def test_mutable_image_is_rejected_without_docker(
    image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harbor, "_docker", lambda *args, **kwargs: pytest.fail("unexpected Docker call")
    )
    with pytest.raises(ValueError, match="immutable"):
        harbor.image_identity(image)


def test_missing_native_usage_stays_unknown(tmp_path: Path) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    (agent / "codex.txt").write_text('{"type":"turn.failed","error":"timeout"}\n')
    (agent / "codex.stderr.log").write_text("")
    result = harbor._native_evidence(
        area=tmp_path, host="codex", effort="high", transport="codex-stdout-stderr-v1"
    )
    assert result["usage"] is None
    assert result["successful_native_completion"] is False


_CODEX_PRELUDE = (
    "WARNING: proceeding, even though we could not create PATH aliases: Refusing to create "
    'helper binaries under temporary dir "/tmp" '
    '(codex_home: AbsolutePathBuf("/tmp/codex-home"))\n'
    "Reading additional input from stdin...\n"
)
_CODEX_COMPLETED = (
    '{"type":"thread.started","thread_id":"canary"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"Updated."}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":43035,"output_tokens":199}}\n'
)


@pytest.mark.parametrize("link_name", ["codex.txt", "unrelated.bin", "sessions/unrelated.bin"])
def test_native_log_link_is_rejected_without_reading_external_file(
    tmp_path: Path, link_name: str
) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("never read")
    link = agent / link_name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="unsupported native artifact"):
        harbor._native_evidence(
            area=tmp_path, host="codex", effort="high", transport="codex-stdout-stderr-v1"
        )
    assert not (tmp_path / "artifacts/stdout.jsonl").exists()


def test_existing_image_with_unsupported_project_directory_does_not_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        calls.append(arguments)
        return json.dumps([{"Config": {"WorkingDir": "/"}}])

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="project directory"):
        harbor.image_identity("repo@sha256:" + "a" * 64, pull=True)
    assert len(calls) == 1


def test_context_switch_cannot_redirect_export_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/before.py").write_text("before")
    remaining = {"containers": ["ours"], "networks": ["network"], "images": ["image"]}
    seen = []
    monkeypatch.setenv("DOCKER_CONTEXT", "other-empty-daemon")
    monkeypatch.setenv("DOCKER_HOST", "unix:///other.sock")
    monkeypatch.setattr(harbor, "_frozen_executable", lambda identity, label: identity)

    def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["/frozen/docker", "--host", runtime()["docker_engine"]["endpoint"]]
        assert "DOCKER_CONTEXT" not in kwargs["env"] and "DOCKER_HOST" not in kwargs["env"]
        args = argv[3:]
        seen.append(args)
        reply = control_reply(tuple(args))
        if reply is None:
            if args[0] == "ps":
                reply = "\n".join(remaining["containers"])
            elif args[0] == "inspect":
                reply = json.dumps([container()])
            elif args[0] == "cp":
                (Path(args[2]) / "submitted.py").write_text("submitted")
                reply = ""
            elif args[1] == "ls":
                reply = "\n".join(remaining[args[0] + "s"])
            else:
                key = "containers" if args[0] == "rm" else args[0] + "s"
                remaining[key] = []
                reply = ""
        return subprocess.CompletedProcess(argv, 0, reply, "")

    monkeypatch.setattr(harbor, "run_command", command)
    exported = harbor.export_native_source(
        destination=tmp_path / "submission",
        project="owned",
        trial_dir=tmp_path,
        workdir="/app",
        expected=runtime(),
    )
    assert exported["snapshot_digest"] == inspect_snapshot(tmp_path / "submission")["digest"]
    assert (
        harbor.cleanup(project="owned", owner="owner", expected_runtime=runtime())["status"]
        == "complete"
    )
    assert remaining == {"containers": [], "networks": [], "images": []}
    assert any(args[0] == "cp" for args in seen) and any(args[0] == "rm" for args in seen)


def test_replacement_daemon_cannot_supply_an_empty_cleanup_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(harbor, "_frozen_executable", lambda identity, label: identity)

    def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["/frozen/docker", "--host", runtime()["docker_engine"]["endpoint"]]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ID": "replacement-daemon"}), "")

    monkeypatch.setattr(harbor, "run_command", command)
    with pytest.raises(ValueError, match="daemon changed"):
        harbor.cleanup(project="owned", owner="owner", expected_runtime=runtime())
    assert len(calls) == 1 and calls[0][3] == "info"


def test_parent_docker_proof_uses_frozen_binary_even_when_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "original"
    directory.mkdir()
    cli = docker_cli(directory)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    docker_cli(foreign)
    monkeypatch.setenv("PATH", str(foreign))
    expected = {**runtime(), "docker_cli": cli}
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv == [cli["resolved_path"], "--host", "unix:///private/docker.sock", "info"]
        return subprocess.CompletedProcess(argv, 0, "original daemon proof", "")

    monkeypatch.setattr(harbor, "run_command", run)
    assert harbor._docker("info", expected_runtime=expected) == "original daemon proof"
    assert len(calls) == 1
    Path(cli["path"]).write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(isolation.RuntimeDriftError, match="changed"):
        harbor._docker("info", expected_runtime=expected)
    assert len(calls) == 1


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize(
    "repository", ["public.ecr.aws/team/task", "registry.local:5000/team/task"]
)
def test_preparation_resolves_image_tag_once_to_immutable_content(
    monkeypatch: pytest.MonkeyPatch, missing: bool, repository: str
) -> None:
    tag = repository + ":v1"
    digest = repository + "@sha256:" + "a" * 64
    image = {
        "Id": "sha256:" + "a" * 64,
        "RepoDigests": ["unrelated/repo@sha256:" + "c" * 64, digest],
        "Config": {"WorkingDir": "/app"},
        "Os": "linux",
        "Architecture": "arm64",
    }
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        calls.append(arguments)
        if (
            arguments[:2] == ("image", "inspect")
            and arguments[-1] == tag
            and missing
            and len(calls) == 1
        ):
            raise ValueError("no such image")
        return "" if arguments[0] == "pull" else json.dumps([image])

    monkeypatch.setattr(harbor, "_docker", docker)
    result = harbor.resolve_image(tag)
    assert result == {
        "image": digest,
        "original_image": tag,
        "image_id": image["Id"],
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    assert [call for call in calls if call[0] == "pull"] == ([("pull", tag)] if missing else [])


def test_cached_tag_without_repository_digest_is_not_pulled_or_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        calls.append(arguments)
        return json.dumps([{"Id": "sha256:" + "a" * 64, "RepoDigests": []}])

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="no immutable digest"):
        harbor.resolve_image("registry/task:v1")
    assert len(calls) == 1


def test_image_inspection_error_does_not_trigger_a_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        calls.append(arguments)
        raise ValueError("Docker daemon unavailable")

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="daemon unavailable"):
        harbor.resolve_image("registry/task:v1")
    assert calls == [("image", "inspect", "registry/task:v1")]


@pytest.mark.parametrize(
    "case",
    ["stderr", "malformed", "turn.failed", "error", "exit-nonzero", "mismatch", "source-drift", "native-success", "native-failure"],
)
def test_codex_transport_delegates_to_sdk_pipefail_and_preserves_separate_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    pytest.importorskip("harbor")
    sdk_codex = importlib.import_module("harbor.agents.installed.codex").Codex
    sdk_result = importlib.import_module("harbor.environments.base").ExecResult
    agent_directory = tmp_path / "artifacts/harbor/trial/agent"
    agent_directory.mkdir(parents=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    counter = tmp_path / "inference-counter"
    first = tmp_path / "first.jsonl"
    rest = tmp_path / "rest.jsonl"
    first.write_text(_CODEX_COMPLETED.splitlines(keepends=True)[0])
    remainder = "".join(_CODEX_COMPLETED.splitlines(keepends=True)[1:])
    if case == "malformed":
        remainder = "unexpected stdout text\n" + remainder
    elif case == "error":
        remainder = '{"type":"error","message":"native error"}\n' + remainder
    elif case == "turn.failed":
        remainder = '{"type":"turn.failed","error":"native failure"}\n' + remainder
    rest.write_text(remainder)
    diagnostic = "ERROR codex_core::tools::router: exec command rejected"
    binary = binaries / "codex"
    binary.write_text(
        "#!/bin/sh\n"
        + f"printf called >> {shlex.quote(str(counter))}\n"
        + f"cat {shlex.quote(str(first))}\n"
        + f"printf '%s\\n' {shlex.quote(diagnostic)} >&2\n"
        + f"cat {shlex.quote(str(rest))}\n"
        + ("exit 7\n" if case == "exit-nonzero" else "exit 0\n")
    )
    binary.chmod(0o755)
    commands: list[str] = []
    created: list[object] = []

    class Environment:
        session_id = "paid-free"

        async def exec(
            self,
            command: str,
            user: str | int | None = None,
            env: dict[str, str] | None = None,
            cwd: str | None = None,
            timeout_sec: int | None = None,
        ) -> object:
            commands.append(command)
            mapped = command.replace("/logs/agent/", str(agent_directory) + "/")
            assert cwd == "/workspace" and timeout_sec == 5 and user is None
            assert env == ({"SETUP": "unchanged"} if len(commands) == 1 else {"TASK": "unchanged"})
            result = run_command(
                ["bash", "-c", mapped],
                cwd=tmp_path,
                timeout=5,
                env={
                    **dict(os.environ),
                    "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", os.defpath),
                    "HOME": str(home),
                    **(env or {}),
                },
            )
            return sdk_result(
                stdout=result.stdout, stderr=result.stderr, return_code=result.returncode
            )

    class Trial:
        agent_environment = Environment()

        def __init__(self) -> None:
            self.agent = sdk_codex(logs_dir=agent_directory, model_name="gpt-6.1-sol", config={})

        @classmethod
        async def create(cls, config: object) -> Trial:
            created.append(config)
            return cls()

        async def run(self) -> object:
            await self.agent.exec_as_agent(
                self.agent_environment,
                command="printf setup",
                env={"SETUP": "unchanged"},
                cwd="/workspace",
                timeout_sec=5,
            )
            command = (
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "
                "--model gpt-6.1-sol --json --enable unified_exec -- 'task' "
                "2>&1 </dev/null | tee /logs/agent/codex.txt"
            )
            if case == "mismatch":
                command = command.replace("2>&1 </dev/null", "2>&1")
            await self.agent.exec_as_agent(
                self.agent_environment,
                command=command,
                env={"TASK": "unchanged"},
                cwd="/workspace",
                timeout_sec=5,
            )
            return SimpleNamespace(model_dump=lambda mode: {"exception_info": None})

    monkeypatch.setattr(importlib.import_module("harbor.trial.trial"), "Trial", Trial)
    monkeypatch.setattr(
        importlib.import_module("harbor.models.trial.config"),
        "TrialConfig",
        SimpleNamespace(model_validate=lambda config: config),
    )
    trial_config: dict[str, Any] = {"agent": {"name": "codex"}}
    if case.startswith("native-"):
        trial_config["pluginbench_boundary"] = {
            "treatment": {}, "image": {"workdir": "/workspace"}, "area": str(tmp_path),
        }
        # Source-export hooks are unrelated to this transport-ordering boundary.
        monkeypatch.setattr(harbor, "install_native_hooks", lambda *args: None)

        async def install(**kwargs: Any) -> dict[str, object]:
            assert commands == ["set -o pipefail; printf setup"]
            assert not counter.exists()
            await kwargs["environment"].exec(
                command="printf native-installed",
                env=kwargs["env"], cwd=kwargs["cwd"], timeout_sec=5,
            )
            if case == "native-failure":
                raise ValueError("native discovery rejected the package")
            return {"status": "verified"}

        monkeypatch.setattr(native_treatments, "install", install)
    if case == "source-drift":
        monkeypatch.setattr(harbor, "_CODEX_RUN_SHA256", "changed", raising=False)
    if case in {"mismatch", "source-drift", "exit-nonzero", "native-failure"}:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(harbor.run_trial(trial_config))
        if case in {"mismatch", "source-drift", "native-failure"}:
            assert not counter.exists()
        if case == "source-drift":
            assert not created
        return
    result = asyncio.run(harbor.run_trial(trial_config))
    assert result["session_id"] == "paid-free"
    assert (agent_directory / "codex.stderr.log").is_file()
    assert diagnostic not in (agent_directory / "codex.txt").read_text()
    evidence = harbor._native_evidence(
        area=tmp_path, host="codex", effort="medium", transport="codex-stdout-stderr-v1"
    )
    assert evidence["successful_native_completion"] is (case in {"stderr", "native-success"})
    assert (agent_directory / "codex.stderr.log").read_text() == diagnostic + "\n"
    assert (tmp_path / "artifacts/codex.stderr.log").read_text() == diagnostic + "\n"
    assert commands[0] == "set -o pipefail; printf setup"
    assert "--model gpt-6.1-sol --json --enable unified_exec -- 'task'" in commands[-1]
    if case == "native-success":
        assert commands[1] == "printf native-installed"
    assert counter.read_text() == "called"


@pytest.mark.parametrize(
    "capture", ["missing", "empty", "prelude", "unknown", "unversioned", "merged"]
)
def test_native_codex_requires_current_transport_and_separate_streams(
    tmp_path: Path, capture: str
) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    (agent / "codex.txt").write_text(
        (_CODEX_PRELUDE if capture == "prelude" else "") + _CODEX_COMPLETED
    )
    if capture != "missing":
        (agent / "codex.stderr.log").write_text("")
    transport = {"unknown": "unknown", "unversioned": None, "merged": "merged-stderr-v1"}.get(
        capture, "codex-stdout-stderr-v1"
    )
    if capture in {"missing", "unknown", "unversioned", "merged"}:
        with pytest.raises(ValueError):
            harbor._native_evidence(
                area=tmp_path, host="codex", effort="medium", transport=transport
            )
    else:
        result = harbor._native_evidence(
            area=tmp_path, host="codex", effort="medium", transport=transport
        )
        assert result["successful_native_completion"] is (capture == "empty")
        assert not (tmp_path / "artifacts/codex-prelude.log").exists()


def test_report_and_backend_import_without_optional_harbor_sdk(tmp_path: Path) -> None:
    script = """
import builtins
original = builtins.__import__
def without_harbor(name, *args, **kwargs):
    if name == 'harbor' or name.startswith('harbor.'):
        raise ImportError('optional Harbor unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = without_harbor
import pluginbench.harbor_backend
import pluginbench.cli
print('loaded')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, text=True, capture_output=True, check=True
    )
    assert result.stdout.strip() == "loaded"
