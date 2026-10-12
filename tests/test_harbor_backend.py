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
import tomllib
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from pluginbench import codex
from pluginbench import harbor_backend as harbor
from pluginbench import isolation
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.processes import run_command


def base_image() -> dict[str, str]:
    return {
        "image": "repo@sha256:" + "a" * 64,
        "image_id": "sha256:" + "a" * 64,
        "platform": "linux/arm64",
        "workdir": "/app",
    }


def toolchain(*, base: dict[str, str] | None = None, harness: str = "codex") -> dict[str, Any]:
    base = base or base_image()
    return {
        "image_id": "sha256:" + "c" * 64,
        "platform": base["platform"],
        "harness": harness,
        "harness_version": "0.208.0" if harness == "codex" else "2.1.115",
        "install_policy": "base-runtime-frozen-support-exact-npm-cli-v1",
        "base_image_identity": base,
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
                "org.pluginbench.base-image": base_image()["image"],
                "org.pluginbench.base-image-id": base_image()["image_id"],
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


@pytest.mark.parametrize("native_toolchain", [None, toolchain()])
@pytest.mark.parametrize("oom_killed", [False, True])
def test_export_copies_stopped_container_and_hashes_every_source_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_toolchain: dict[str, Any] | None,
    oom_killed: bool,
) -> None:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/old.py").write_text("old")
    calls = []
    description = container()
    description["State"]["OOMKilled"] = oom_killed
    if native_toolchain is not None:
        description["Config"]["Labels"].update(harbor._toolchain_labels(native_toolchain))
        description["Config"]["Labels"]["org.pluginbench.toolchain-image-id"] = native_toolchain[
            "image_id"
        ]

    def docker(*arguments: str, **kwargs: Any) -> str:
        if arguments[:2] == ("image", "inspect"):
            return json.dumps([{"Config": description["Config"]}])
        reply = control_reply(arguments)
        if reply is not None:
            return reply
        calls.append(arguments)
        if arguments[0] == "ps":
            return "owned-id\n"
        if arguments[0] == "inspect":
            return json.dumps([description])
        assert arguments[:2] == ("cp", "owned-id:/workspace/.")
        destination = Path(arguments[2])
        (destination / "new.py").write_text("complete submission\n")
        (destination / "nested").mkdir()
        (destination / "nested/other.py").write_text("more\n")
        return ""

    monkeypatch.setattr(harbor, "_docker", docker)
    receipt = harbor.collect_submission(
        area=tmp_path,
        project="owned",
        owner="owner",
        expected_runtime=runtime(),
        base_image=base_image(),
        toolchain=native_toolchain,
    )
    assert receipt == {
        "status": "complete",
        "stopped": True,
        "snapshot_digest": inspect_snapshot(tmp_path / "workspace")["digest"],
        "resources": {"cpus": 1, "memory_mb": 2048},
        "derived_image_id": "sha256:" + "b" * 64,
        "base_image_identity": base_image(),
        **({"toolchain_identity": native_toolchain} if native_toolchain is not None else {}),
    }
    assert not (tmp_path / "workspace/old.py").exists()
    assert json.loads((tmp_path / "artifacts/container-state.json").read_text()) == {
        "container_id": description["Id"],
        "owner": "owner",
        "state": description["State"],
        "resources": receipt["resources"],
        "derived_image_id": receipt["derived_image_id"],
    }
    assert len(calls) == 3


@pytest.mark.parametrize(
    "description",
    [
        container(running=True),
        container(project="foreign"),
        container(mounts=[{"Destination": "/workspace"}]),
    ],
)
def test_export_rejects_live_foreign_or_shared_workspace_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description: dict[str, Any]
) -> None:
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        reply = control_reply(arguments)
        if reply is not None:
            return reply
        calls.append(arguments[0])
        return "owned-id" if arguments[0] == "ps" else json.dumps([description])

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError):
        harbor.collect_submission(
            area=tmp_path,
            project="owned",
            owner="owner",
            expected_runtime=runtime(),
            base_image=base_image(),
        )
    assert "cp" not in calls


def test_failed_copy_never_replaces_initial_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "workspace").mkdir()
    original = tmp_path / "workspace/initial.py"
    original.write_text("initial")

    def docker(*arguments: str, **kwargs: Any) -> str:
        reply = control_reply(arguments)
        if reply is not None:
            return reply
        if arguments[0] == "ps":
            return "owned-id"
        if arguments[0] == "inspect":
            return json.dumps([container()])
        (Path(arguments[2]) / "partial.py").write_text("partial")
        raise ValueError("copy interrupted")

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="copy interrupted"):
        harbor.collect_submission(
            area=tmp_path,
            project="owned",
            owner="owner",
            expected_runtime=runtime(),
            base_image=base_image(),
        )
    assert original.read_text() == "initial"
    assert (tmp_path / "artifacts/container-submission/partial.py").exists()
    assert (
        json.loads((tmp_path / "artifacts/container-state.json").read_text())["state"]
        == container()["State"]
    )


def test_unsupported_export_retains_link_evidence_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "artifacts").mkdir()

    def docker(*arguments: str, **kwargs: Any) -> str:
        reply = control_reply(arguments)
        if reply is not None:
            return reply
        if arguments[0] == "ps":
            return "owned-id"
        if arguments[0] == "inspect":
            return json.dumps([container()])
        (Path(arguments[2]) / "external").symlink_to("/etc/passwd")
        return ""

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="symlink"):
        harbor.collect_submission(
            area=tmp_path,
            project="owned",
            owner="owner",
            expected_runtime=runtime(),
            base_image=base_image(),
        )
    assert (tmp_path / "artifacts/unsupported-submission/external").is_symlink()


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
    result = harbor._native_evidence(area=tmp_path, host="codex", effort="high")
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


def test_native_codex_separates_known_cli_prelude_without_changing_raw_output(
    tmp_path: Path,
) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    raw = agent / "codex.txt"
    output = _CODEX_PRELUDE + _CODEX_COMPLETED
    raw.write_text(output)
    assert codex.parse_events(output, [], "codex")["successful_native_completion"] is False

    result = harbor._native_evidence(area=tmp_path, host="codex", effort="medium")

    assert result["successful_native_completion"] is True
    assert result["stream_errors"] == []
    assert result["usage"] == {"input_tokens": 43035, "output_tokens": 199}
    assert result["final"] == "Updated."
    assert raw.read_text() == output
    assert (tmp_path / "artifacts/stdout.jsonl").read_text() == _CODEX_COMPLETED
    assert (tmp_path / "artifacts/codex-prelude.log").read_text() == _CODEX_PRELUDE


@pytest.mark.parametrize(
    "output,error",
    [
        ("Unexpected CLI failure\n" + _CODEX_COMPLETED, "malformed stdout JSONL"),
        (_CODEX_COMPLETED + _CODEX_PRELUDE, "malformed stdout JSONL"),
        (_CODEX_PRELUDE + '{"type":\n' + _CODEX_COMPLETED, "malformed stdout JSONL"),
        (_CODEX_PRELUDE + "[]\n" + _CODEX_COMPLETED, "stdout JSONL event is not an object"),
        (
            _CODEX_PRELUDE + '{"type":"turn.failed","error":"timeout"}\n' + _CODEX_COMPLETED,
            {"type": "turn.failed", "error": "timeout"},
        ),
        (
            _CODEX_PRELUDE + '{"type":"error","message":"failed"}\n' + _CODEX_COMPLETED,
            {"type": "error", "message": "failed"},
        ),
    ],
)
def test_native_codex_prelude_does_not_hide_protocol_errors(
    tmp_path: Path, output: str, error: str | dict[str, str]
) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    (agent / "codex.txt").write_text(output)

    result = harbor._native_evidence(area=tmp_path, host="codex", effort="medium")

    assert result["successful_native_completion"] is False
    assert error in result["stream_errors"]


@pytest.mark.parametrize("failure_kind", ["native", "worker", "cleanup", "cleanup-drift"])
def test_invoke_preserves_failed_submission_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("original\n")
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    image = {
        "image": "image@sha256:" + "a" * 64,
        "image_id": "id",
        "platform": "linux/arm64",
        "workdir": "/app",
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(harbor, "image_identity", lambda value, **kwargs: image)
    monkeypatch.setattr(harbor, "_assert_toolchain", lambda identity, **kwargs: None)
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CODEX_MODEL", "unexpected-model")
    calls = []
    expected_runtime = {**runtime(), "docker_plugins": docker_plugins(tmp_path / "plugins")}
    expected_runtime["docker_cli"] = docker_cli(tmp_path / "plugins")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        config_path = Path(kwargs["env"]["DOCKER_CONFIG"])
        assert config_path == tmp_path / "private/docker"
        assert not (config_path / "config.json").exists()
        executable = Path(kwargs["env"]["PATH"].split(":")[0]) / "docker"
        assert str(executable.resolve()) == expected_runtime["docker_cli"]["resolved_path"]
        for name, identity in expected_runtime["docker_plugins"].items():
            link = config_path / "cli-plugins" / ("docker-" + name)
            assert link.is_symlink() and str(link.resolve()) == identity["resolved_path"]
        if argv[:2] == ["docker", "compose"]:
            return subprocess.CompletedProcess(argv, 0, "5.4.0\n", "")
        if argv[:2] == ["docker", "buildx"]:
            return subprocess.CompletedProcess(
                argv, 0, "github.com/docker/buildx v0.34.1 Homebrew\n", ""
            )
        if argv[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "Version": "29.5.0",
                        "Plugins": [
                            {
                                "Name": name,
                                "Path": str(config_path / "cli-plugins" / ("docker-" + name)),
                                "Version": identity["version"],
                            }
                            for name, identity in expected_runtime["docker_plugins"].items()
                        ],
                    }
                ),
                "",
            )
        calls.append(argv)
        config = json.loads(Path(argv[-2]).read_text())
        assert config["verifier"] == {"disable": True}
        assert "source_trial" not in config and "retries" not in config
        assert config["agent"]["kwargs"] == {
            "version": "0.208.0",
            "reasoning_effort": "high",
            "config": {},
        }
        assert config["agent"]["skills"] == [] and config["agent"]["mcp_servers"] == []
        assert not {"CLAUDE_CODE_EFFORT_LEVEL", "CODEX_MODEL"} & kwargs["env"].keys()
        assert kwargs["env"]["PYTHON_DOTENV_DISABLED"] == "1"
        assert kwargs["env"]["DOCKER_DEFAULT_PLATFORM"] == image["platform"]
        agent = Path(config["trials_dir"]) / config["trial_name"] / "agent"
        agent.mkdir(parents=True)
        (agent / "codex.txt").write_text('{"type":"turn.failed","error":"timeout"}\n')
        (agent / "sessions").mkdir()
        (agent / "sessions/rollout.jsonl").write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 3}},
                    },
                }
            )
            + "\n"
        )
        Path(argv[-1]).write_text(
            json.dumps(
                {
                    "session_id": config["trial_name"] + "__env",
                    "result": {"exception_info": {"exception_type": "AgentTimeoutError"}},
                }
            )
        )
        return subprocess.CompletedProcess(argv, 1 if failure_kind == "worker" else 0, "", "")

    def collect(**kwargs: Any) -> dict[str, object]:
        (workspace / "source.py").write_text("failed but submitted\n")
        return {
            "status": "complete",
            "stopped": True,
            "snapshot_digest": inspect_snapshot(workspace)["digest"],
        }

    monkeypatch.setattr(harbor, "run_command", run)
    monkeypatch.setattr(harbor, "collect_submission", collect)

    def clean(**kwargs: Any) -> dict[str, object]:
        if failure_kind == "cleanup-drift":
            raise isolation.RuntimeDriftError("Docker CLI changed during trial")
        if failure_kind == "cleanup":
            raise ValueError("cleanup failed")
        return {
            "status": "complete",
            "residuals": {"containers": [], "networks": [], "images": []},
        }

    monkeypatch.setattr(harbor, "cleanup", clean)
    protocol = {
        "models": {"codex": "model"},
        "reasoning": "high",
        "harness_version": "codex-cli 0.208.0",
        "isolation": expected_runtime,
        "agent_image": image["image"],
        "image_identity": image,
        "toolchain_identity": toolchain(base=image),
        "auth_file": str(auth),
    }
    if failure_kind == "native":
        result = harbor.invoke(
            area=tmp_path, entry={"host": "codex"}, protocol=protocol, prompt="task", timeout=1
        )
    else:
        with pytest.raises((ValueError, isolation.RuntimeDriftError)):
            harbor.invoke(
                area=tmp_path, entry={"host": "codex"}, protocol=protocol, prompt="task", timeout=1
            )
        result = json.loads((tmp_path / "artifacts/execution.json").read_text())
    assert result["submission_collected"] is (failure_kind == "native")
    assert result["successful_native_completion"] is False
    assert result["usage"] is None
    assert result["launch_harness_version"] is None
    assert result["launch"]["harness_version"] == "0.208.0"
    invocation = json.loads((tmp_path / "artifacts/invocation.json").read_text())
    assert invocation["toolchain_identity"] == toolchain(base=image)
    if failure_kind != "worker":
        assert result["submission_export_digest"] == inspect_snapshot(workspace)["digest"]
    if failure_kind not in {"cleanup", "cleanup-drift"}:
        assert result["partial_usage"][0]["tokens"] == {"input_tokens": 10, "output_tokens": 3}
    else:
        assert result["partial_usage"] == []
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "harbor-task").exists()
    with pytest.raises(ValueError, match="already started"):
        harbor.invoke(
            area=tmp_path, entry={"host": "codex"}, protocol=protocol, prompt="task", timeout=1
        )
    assert len(calls) == 1


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
        harbor._native_evidence(area=tmp_path, host="codex", effort="high")
    assert not (tmp_path / "artifacts/stdout.jsonl").exists()


@pytest.mark.parametrize("field", ["NanoCpus", "Memory"])
def test_actual_limits_must_match_before_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    description = container()
    description["HostConfig"][field] = 0

    def docker(*arguments: str, **kwargs: Any) -> str:
        reply = control_reply(arguments)
        if reply is not None:
            return reply
        assert arguments[0] != "cp"
        return "owned-id" if arguments[0] == "ps" else json.dumps([description])

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="actual CPU or memory"):
        harbor.collect_submission(
            area=tmp_path,
            project="owned",
            owner="owner",
            expected_runtime=runtime(),
            base_image=base_image(),
        )


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


def test_staged_task_has_empty_home_and_canonical_source_only(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/source.py").write_text("canonical source\n")
    task = harbor._stage_task(
        area=tmp_path,
        prompt="the same task",
        owner="owned",
        timeout=60,
        image=base_image(),
        cpus=2,
        memory_mb=8192,
        build_timeout=1800,
    )
    dockerfile = (task / "environment/Dockerfile").read_text()
    assert "mv /app /workspace" in dockerfile
    assert "! -name node_modules -exec rm -rf {} +" in dockerfile
    assert "rm -rf /pluginbench-home; mkdir /pluginbench-home" in dockerfile
    assert "ENV HOME=/pluginbench-home" in dockerfile
    assert (task / "environment/fixture/source.py").read_text() == "canonical source\n"
    assert (task / "instruction.md").read_text() == "the same task"
    limits = tomllib.loads((task / "task.toml").read_text())["environment"]
    assert (limits["cpus"], limits["memory_mb"], limits["build_timeout_sec"]) == (2, 8192, 1800)


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
    exported = harbor.collect_submission(
        area=tmp_path,
        project="owned",
        owner="owner",
        expected_runtime=runtime(),
        base_image=base_image(),
    )
    assert exported["snapshot_digest"] == inspect_snapshot(tmp_path / "workspace")["digest"]
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


@pytest.mark.parametrize("changed", ["container", "image"])
def test_changed_base_image_evidence_is_rejected_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    description = container()
    derived_config = container()["Config"]
    target = description["Config"] if changed == "container" else derived_config
    target["Labels"]["org.pluginbench.base-image-id"] = "sha256:" + "c" * 64

    def docker(*arguments: str, **kwargs: Any) -> str:
        if arguments[0] == "info":
            return json.dumps({"ID": "daemon"})
        if arguments[0] == "ps":
            return "ours"
        if arguments[0] == "inspect":
            return json.dumps([description])
        assert arguments[:2] == ("image", "inspect")
        return json.dumps([{"Config": derived_config}])

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="frozen base image"):
        harbor.collect_submission(
            area=tmp_path,
            project="owned",
            owner="owner",
            expected_runtime=runtime(),
            base_image=base_image(),
        )


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("failure", ["missing-tool", "wrong-version", "missing-support", None])
def test_toolchain_preparation_freezes_support_and_rejects_invalid_base_or_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, failure: str | None
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    npm_calls = tmp_path / "npm-calls"
    cli_calls = tmp_path / "cli-calls"
    apt_calls = tmp_path / "apt-calls"
    support = "rg" if harness == "codex" else "ps"
    for name in ("curl", "bash", "node", "npm", "rg", "ps", harness):
        if name == "node" and failure == "missing-tool":
            continue
        if name == support and failure == "missing-support":
            continue
        script = "#!/bin/sh\n"
        if name == "npm":
            script += f"printf '%s\\n' \"$@\" >> '{npm_calls}'\n"
        if name == harness:
            version = "0.208.0" if harness == "codex" else "2.1.115"
            version = "0.0.0" if failure == "wrong-version" else version
            output = f"codex-cli {version}" if harness == "codex" else f"{version} (Claude Code)"
            script += f"printf called >> '{cli_calls}'\nprintf '%s\\n' '{output}'\n"
        binary = binaries / name
        binary.write_text(script)
        binary.chmod(0o755)
    apt = binaries / "apt-get"
    apt.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> '{apt_calls}'\n"
        f"if [ \"$1\" = install ]; then printf '#!/bin/sh\\n' > '{binaries / support}'; "
        f"/bin/chmod +x '{binaries / support}'; fi\n"
    )
    apt.chmod(0o755)
    # Keep the real shell evaluation from deleting host package-manager paths.
    remove = binaries / "rm"
    remove.write_text("#!/bin/sh\nexit 0\n")
    remove.chmod(0o755)
    monkeypatch.setattr(harbor, "runtime_identity", lambda **kwargs: runtime())
    monkeypatch.setattr(harbor, "image_identity", lambda image, **kwargs: base_image())
    built: list[str] = []
    prepared = toolchain(harness=harness)
    prepared_image = {
        "Id": prepared["image_id"],
        "Os": "linux",
        "Architecture": "arm64",
        "Config": {"Labels": harbor._toolchain_labels(prepared)},
    }

    def docker(*arguments: str, **kwargs: Any) -> str:
        if arguments[0] == "info":
            return json.dumps({"ID": "daemon"})
        if arguments[:2] == ("image", "inspect"):
            if not built:
                raise ValueError("No such image")
            return json.dumps([prepared_image])
        if arguments[0] == "tag":
            assert arguments[1] == prepared["image_id"]
            assert arguments[2] == "pluginbench-toolchain:sha256-" + "c" * 64
            return ""
        assert arguments[0] == "build"
        assert arguments[1:3] == ("--platform", base_image()["platform"])
        dockerfile = (Path(arguments[-1]) / "Dockerfile").read_text()
        build_script = next(line[4:] for line in dockerfile.splitlines() if line.startswith("RUN "))
        build_script = build_script.replace(
            "/tmp/pluginbench-npm-userconfig", str(tmp_path / "npm-userconfig")
        )
        checked = subprocess.run(
            ["/bin/sh", "-c", build_script],
            env={"PATH": str(binaries)},
            text=True,
            capture_output=True,
        )
        if checked.returncode:
            raise ValueError("Toolchain image build rejected: " + checked.stderr)
        built.append(dockerfile)
        return ""

    monkeypatch.setattr(harbor, "_docker", docker)
    version = "codex-cli 0.208.0" if harness == "codex" else "2.1.115 (Claude Code)"
    if failure in {"missing-tool", "wrong-version"}:
        with pytest.raises(ValueError, match="Toolchain image build rejected"):
            harbor.prepare_toolchain(base_image(), harness=harness, version=version)
        assert not built
        assert npm_calls.exists() is (failure == "wrong-version")
        assert cli_calls.exists() is (failure == "wrong-version")
        assert not apt_calls.exists()
    else:
        result = harbor.prepare_toolchain(base_image(), harness=harness, version=version)
        assert result == prepared
        package = (
            "@openai/codex@0.208.0" if harness == "codex" else "@anthropic-ai/claude-code@2.1.115"
        )
        assert package in npm_calls.read_text().splitlines()
        assert "--registry=https://registry.npmjs.org" in npm_calls.read_text().splitlines()
        configs = [
            option.partition("=")[2]
            for option in npm_calls.read_text().splitlines()
            if option.startswith(("--userconfig=", "--globalconfig="))
        ]
        assert len(configs) == 2 and configs[0] != configs[1]
        assert (tmp_path / "npm-userconfig").read_text() == ""
        assert "nvm" not in built[0]
        if failure == "missing-support":
            assert apt_calls.read_text().splitlines() == [
                "update",
                "install",
                "--yes",
                "--no-install-recommends",
                "ripgrep" if harness == "codex" else "procps",
            ]
        else:
            assert not apt_calls.exists()
        # Reusing this prepared content does not install again, even though
        # npm's transitive resolution could have changed since preparation.
        assert harbor.prepare_toolchain(base_image(), harness=harness, version=version) == result
        assert len(built) == 1


def test_trial_uses_frozen_toolchain_and_checks_cli_before_native_start(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir()
    native = toolchain()
    task = harbor._stage_task(
        area=tmp_path,
        prompt="task",
        image=base_image(),
        owner="owner",
        timeout=60,
        toolchain=native,
        cpus=1,
        memory_mb=2048,
    )
    dockerfile = (task / "environment/Dockerfile").read_text()
    assert dockerfile.startswith(
        "FROM --platform=linux/arm64 pluginbench-toolchain:sha256-" + "c" * 64 + "\n"
    )
    assert f'LABEL org.pluginbench.toolchain-image-id="{native["image_id"]}"' in dockerfile
    guard = json.loads(dockerfile.splitlines()[-1].removeprefix("RUN "))
    assert guard[:2] == ["bash", "-c"]
    assert 'test "$(codex --version)" = "codex-cli 0.208.0"' in guard[2]
    assert dockerfile.splitlines()[-2] == "ENV HOME=/pluginbench-home"
    assert "npm install" not in dockerfile


@pytest.mark.parametrize("alias_state", ["missing", "changed"])
def test_native_admission_rejects_missing_or_rebound_content_tag(
    monkeypatch: pytest.MonkeyPatch, alias_state: str
) -> None:
    native = toolchain()
    calls = []

    def docker(*arguments: str, **kwargs: Any) -> str:
        calls.append(arguments)
        assert arguments[:2] == ("image", "inspect")
        image_id = native["image_id"]
        if arguments[2] != image_id:
            assert arguments[2] == "pluginbench-toolchain:sha256-" + "c" * 64
            if alias_state == "missing":
                raise ValueError("No such image")
            image_id = "sha256:" + "d" * 64
        return json.dumps(
            [
                {
                    "Id": image_id,
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Labels": harbor._toolchain_labels(native)},
                }
            ]
        )

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError):
        harbor._assert_toolchain(native, expected_runtime=runtime())
    assert len(calls) == 2


@pytest.mark.parametrize("changed", ["container", "image"])
def test_changed_toolchain_evidence_rejects_export_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    native = toolchain()
    description = container()
    derived_config = container()["Config"]
    for config in (description["Config"], derived_config):
        config["Labels"].update(harbor._toolchain_labels(native))
        config["Labels"]["org.pluginbench.toolchain-image-id"] = native["image_id"]
    target = description["Config"] if changed == "container" else derived_config
    target["Labels"]["org.pluginbench.toolchain-image-id"] = "sha256:" + "d" * 64

    def docker(*arguments: str, **kwargs: Any) -> str:
        if arguments[0] == "info":
            return json.dumps({"ID": "daemon"})
        if arguments[0] == "ps":
            return "ours"
        if arguments[0] == "inspect":
            return json.dumps([description])
        assert arguments[:2] == ("image", "inspect")
        return json.dumps([{"Config": derived_config}])

    monkeypatch.setattr(harbor, "_docker", docker)
    with pytest.raises(ValueError, match="frozen base image or toolchain"):
        harbor.collect_submission(
            area=tmp_path,
            project="owned",
            owner="owner",
            expected_runtime=runtime(),
            base_image=base_image(),
            toolchain=native,
        )


@pytest.mark.parametrize("changed", [False, True])
def test_prepared_task_stage_binds_source_and_preserves_nested_dependencies(
    tmp_path: Path, changed: bool
) -> None:
    fixture = tmp_path / "workspace"
    fixture.mkdir()
    (fixture / "package.json").write_text("{}")
    native = toolchain()
    native["task_preparation"] = {"fixture_digest": inspect_snapshot(fixture)["digest"]}
    if changed:
        (fixture / "package.json").write_text('{"changed":true}')
        with pytest.raises(ValueError, match="fixture"):
            harbor._stage_task(
                area=tmp_path,
                prompt="task",
                image=base_image(),
                owner="owner",
                timeout=60,
                cpus=2,
                memory_mb=8192,
                toolchain=native,
            )
        assert not (tmp_path / "harbor-task").exists()
    else:
        staged = harbor._stage_task(
            area=tmp_path,
            prompt="task",
            image=base_image(),
            owner="owner",
            timeout=60,
            cpus=2,
            memory_mb=8192,
            toolchain=native,
        )
        dockerfile = (staged / "environment/Dockerfile").read_text()
        assert "mv /app /workspace" not in dockerfile
        assert "find /workspace -mindepth" not in dockerfile
        assert "COPY fixture/" not in dockerfile
        assert "COREPACK_HOME=/pluginbench-corepack" in dockerfile


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "install",
        "visible-check",
        "source-integrity",
        "cleanup",
        "cache-missing",
        "cache-rebound",
        "create-timeout",
        "create-malformed",
        "stop",
        "pnpm10",
        "image-configuration",
        "image-cleanup",
        "image-cleanup-check",
        "late-writer",
        "excluded-guidance",
    ],
)
def test_task_preparation_requires_success_and_preserves_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "package.json").write_text("{}")
    pnpm = failure == "pnpm10"
    failure = None if pnpm else failure
    manager = "pnpm@10.20.0" if pnpm else "npm@10.9.2"
    (fixture / ("pnpm-lock.yaml" if pnpm else "package-lock.json")).write_text("{}")
    recipe = {
        "package_manager": manager,
        "install_arguments": ["install", "--frozen-lockfile", "--ignore-scripts"]
        if pnpm
        else ["ci", "--include=dev", "--ignore-scripts"],
        "visible_check": [
            "node",
            "/workspace/node_modules/mocha/bin/mocha.js",
            "tests/base.js",
            "--reporter=json",
            "--reporter-option=output=/tmp/pluginbench-visible-check.json",
        ],
        "framework": "mocha",
        "test_cwd": ".",
        "lock_file": "pnpm-lock.yaml" if pnpm else "package-lock.json",
    }
    artifacts = tmp_path / "artifacts"
    calls: list[tuple[str, ...]] = []
    shared_image = toolchain()["image_id"]
    images: dict[str, dict[str, Any]] = {shared_image: {"Id": shared_image}}
    owner = ""
    container_name = ""
    removed = False
    native_workspace = tmp_path / "native-workspace"
    monkeypatch.setattr(harbor, "runtime_identity", lambda **kwargs: runtime())
    monkeypatch.setattr(harbor, "_assert_daemon", lambda expected: None)
    monkeypatch.setattr(harbor, "_assert_toolchain", lambda identity, **kwargs: None)

    def docker(*args: str, **kwargs: Any) -> str:
        nonlocal owner, container_name, removed
        calls.append(args)
        receipt = kwargs.get("receipt")
        if receipt is not None:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps({"returncode": 0, "timed_out": False, "stdout": "", "stderr": ""})
            )
        if args[:2] == ("image", "inspect"):
            if args[2] not in images:
                raise ValueError("No such image")
            return json.dumps([images[args[2]]])
        if args[:2] == ("image", "rm"):
            assert args == ("image", "rm", "sha256:" + "e" * 64)
            if failure == "image-cleanup":
                raise ValueError("Image cleanup failed")
            if failure != "image-cleanup-check":
                images.pop(args[2])
            return ""
        if args[0] == "create":
            owner = next(
                value.split("=", 1)[1]
                for value in args
                if value.startswith(harbor._OWNER_LABEL + "=")
            )
            assert "--cpus" in args and args[args.index("--cpus") + 1] == "2"
            assert args[args.index("--memory") + 1] == "8192m"
            assert not any("mount" in value or "volume" in value for value in args)
            container_name = args[args.index("--name") + 1]
            if failure == "create-timeout":
                raise subprocess.TimeoutExpired("docker create", 60)
            if failure == "create-malformed":
                return "truncated-id"
            return "a" * 64
        if args[0] == "ps":
            return (
                ""
                if removed or (receipt is not None and receipt.stem == "cleanup-check")
                else "a" * 64
            )
        if args[0] == "rm" and failure != "cleanup":
            removed = True
        if receipt is not None and receipt.stem == failure and args[0] in {"exec", "rm", "stop"}:
            receipt.write_text(
                json.dumps({"returncode": 137, "stdout": "partial", "stderr": "failed"})
            )
            raise ValueError(f"{failure} failed")
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "a" * 64,
                        "Name": "/" + container_name,
                        "Config": {"Labels": {harbor._OWNER_LABEL: owner}},
                        "HostConfig": {"NanoCpus": 2_000_000_000, "Memory": 8192 * 1024**2},
                        "Mounts": [],
                        "State": {
                            "Running": failure == "stop",
                            "Status": "running" if failure == "stop" else "exited",
                            "OOMKilled": failure
                            in {"install", "visible-check", "source-integrity"},
                            "ExitCode": 137 if failure else 0,
                            "Error": "",
                            "StartedAt": "start",
                            "FinishedAt": "end",
                        },
                    }
                ]
            )
        if receipt is not None and receipt.stem == "versions":
            return json.dumps(
                {
                    "node": "v24.12.0",
                    "manager": manager.split("@", 1)[1],
                    "cli": "codex-cli 0.208.0",
                }
            )
        if receipt is not None and receipt.stem == "source-integrity":
            return json.dumps(inspect_snapshot(fixture)["files"])
        if receipt is not None and receipt.stem == "copy-source":
            shutil.copytree(fixture, native_workspace)
        if receipt is not None and receipt.stem == "final-reset" and failure == "late-writer":
            (native_workspace / "package.json").write_text('{"written_after_check":true}')
        if receipt is not None and receipt.stem == "final-reset" and failure == "excluded-guidance":
            (native_workspace / ".agents").mkdir()
            (native_workspace / ".agents/private.md").write_text(
                "unexpected inherited instructions"
            )
        if receipt is not None and receipt.stem == "frozen-source-copy":
            assert any(call[0] == "stop" for call in calls)
            shutil.copytree(native_workspace, Path(args[-1]), dirs_exist_ok=True)
            (Path(args[-1]) / "packages/widget/node_modules/library").mkdir(parents=True)
            (Path(args[-1]) / "packages/widget/node_modules/library/index.js").write_text(
                "dependency"
            )
        if receipt is not None and receipt.stem == "copy-report":
            Path(args[-1]).write_text(
                json.dumps(
                    {
                        "tests": [{"fullTitle": "base"}, {"fullTitle": "pending"}],
                        "passes": [{"fullTitle": "base"}],
                        "failures": [],
                        "pending": [{"fullTitle": "pending"}],
                    }
                )
            )
        if args[0] == "tag":
            images[args[2]] = images[args[1]]
        if args[0] == "commit":
            assert any(call[0] == "stop" for call in calls)
            assert 'ENTRYPOINT ["/usr/bin/env"]' in args
            assert 'CMD ["sh", "-c", "sleep infinity"]' in args
            labels = {}
            for index, value in enumerate(args):
                if value == "--change" and args[index + 1].startswith("LABEL "):
                    name, _, encoded = args[index + 1].removeprefix("LABEL ").partition("=")
                    labels[name] = json.loads(encoded)
            images["sha256:" + "e" * 64] = {
                "Id": "sha256:" + "e" * 64,
                "Os": "linux",
                "Architecture": "arm64",
                "Config": {
                    "Labels": labels,
                    "WorkingDir": "/workspace",
                    "Entrypoint": ["bash"]
                    if failure in {"image-configuration", "image-cleanup", "image-cleanup-check"}
                    else ["/usr/bin/env"],
                    "Cmd": ["sh", "-c", "sleep infinity"],
                },
            }
            return "sha256:" + "e" * 64
        return ""

    monkeypatch.setattr(harbor, "_docker", docker)
    if failure and not failure.startswith("cache-"):
        match = (
            "Docker did not report"
            if failure == "create-malformed"
            else "docker create"
            if failure == "create-timeout"
            else "frozen content or install settings"
            if failure in {"image-configuration", "image-cleanup", "image-cleanup-check"}
            else "Stopped preparation source"
            if failure in {"late-writer", "excluded-guidance"}
            else failure
        )
        with pytest.raises((ValueError, subprocess.TimeoutExpired), match=match):
            harbor.prepare_task_image(
                fixture=fixture,
                toolchain=toolchain(),
                recipe=recipe,
                cache=tmp_path / "cache",
                artifacts=artifacts,
                cpus=2,
                memory_mb=8192,
                timeout=60,
            )
        assert any(call[0] == "commit" for call in calls) is (
            failure in {"cleanup", "image-configuration", "image-cleanup", "image-cleanup-check"}
        )
        assert not any(call[0] == "tag" for call in calls)
        if failure == "stop":
            assert json.loads((artifacts / "container-state.json").read_text())["state"] is None
        elif failure not in {"create-timeout", "create-malformed"}:
            assert json.loads((artifacts / "container-state.json").read_text())["state"][
                "OOMKilled"
            ] is (failure in {"install", "visible-check", "source-integrity"})
        if failure not in {
            "create-timeout",
            "create-malformed",
            "image-configuration",
            "image-cleanup",
            "image-cleanup-check",
            "late-writer",
            "excluded-guidance",
        }:
            assert (
                json.loads((artifacts / "evidence" / "steps" / (failure + ".json")).read_text())[
                    "returncode"
                ]
                == 137
            )
        assert json.loads((artifacts / "preparation.json").read_text())["status"] == "failed"
        assert json.loads((artifacts / "preparation.json").read_text())["cleanup"]["status"] == (
            "incomplete"
            if failure in {"cleanup", "image-cleanup", "image-cleanup-check"}
            else "complete"
        )
        committed = any(call[0] == "commit" for call in calls)
        assert any(call[:2] == ("image", "rm") for call in calls) is committed
        if failure in {"image-cleanup", "image-cleanup-check"}:
            assert "sha256:" + "e" * 64 in images
            failed = json.loads((artifacts / "preparation.json").read_text())
            assert "sha256:" + "e" * 64 in failed["cleanup"]["residuals"]
        else:
            assert "sha256:" + "e" * 64 not in images
    else:
        result = harbor.prepare_task_image(
            fixture=fixture,
            toolchain=toolchain(),
            recipe=recipe,
            cache=tmp_path / "cache",
            artifacts=artifacts,
            cpus=2,
            memory_mb=8192,
            timeout=60,
        )
        assert result["image_id"] == "sha256:" + "e" * 64
        prep = result["task_preparation"]
        assert prep["parent_image_id"] == toolchain()["image_id"]
        assert prep["fixture_digest"] == inspect_snapshot(fixture)["digest"]
        assert prep["recipe"] == recipe
        assert prep["resources"] == {"cpus": 2, "memory_mb": 8192}
        validation = json.loads(Path(prep["receipt_path"]).read_text())
        assert validation["visible_tests"] == {"total": 2, "passed": 1, "failed": 0, "pending": 1}
        initial_calls = list(calls)
        receipt_path = Path(result["task_preparation"]["receipt_path"])
        if failure == "cache-missing":
            receipt_path.unlink()
        elif failure == "cache-rebound":
            data = json.loads(receipt_path.read_text())
            data["image_id"] = "sha256:" + "f" * 64
            receipt_path.write_text(json.dumps(data))
        if failure is not None:
            with pytest.raises(ValueError, match="receipt"):
                harbor.prepare_task_image(
                    fixture=fixture,
                    toolchain=toolchain(),
                    recipe=recipe,
                    cache=tmp_path / "cache",
                    artifacts=tmp_path / "reuse",
                    cpus=2,
                    memory_mb=8192,
                    timeout=60,
                )
        else:
            cached = harbor.prepare_task_image(
                fixture=fixture,
                toolchain=toolchain(),
                recipe=recipe,
                cache=tmp_path / "cache",
                artifacts=tmp_path / "reuse",
                cpus=2,
                memory_mb=8192,
                timeout=60,
            )
            assert cached == result
        assert not any(
            call[0] in {"create", "exec", "commit"} for call in calls[len(initial_calls) :]
        )
    assert any(call[0] == "rm" for call in calls)
    assert shared_image in images


@pytest.mark.parametrize("changed", [False, True])
def test_native_source_guard_removes_generated_state_and_preserves_all_dependency_trees(
    tmp_path: Path, changed: bool
) -> None:
    node = shutil.which("node")
    assert node is not None
    workspace = tmp_path / "workspace"
    source = workspace / "packages/widget/source.js"
    source.parent.mkdir(parents=True)
    source.write_text("export const value = 1;")
    snapshot = inspect_snapshot(workspace)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(snapshot["files"]))
    for relative in (
        "node_modules/library/index.js",
        "packages/widget/node_modules/library/index.js",
    ):
        dependency = workspace / relative
        dependency.parent.mkdir(parents=True)
        dependency.write_text("dependency")
    cache = workspace / "packages/widget/node_modules/.vite/results.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("cached test state")
    generated = workspace / "packages/widget/generated-report.json"
    generated.write_text("test output")
    if changed:
        source.write_text("export const value = 2;")
    script = harbor._PREPARATION_SOURCE_GUARD.replace(
        "'/tmp/pluginbench-source-manifest.json'", json.dumps(str(manifest))
    ).replace("'/workspace'", json.dumps(str(workspace)))
    checked = subprocess.run([node, "-e", script], text=True, capture_output=True)
    if changed:
        assert checked.returncode != 0 and "Starting source changed" in checked.stderr
        assert generated.exists()
    else:
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout) == snapshot["files"]
        assert not generated.exists() and not cache.exists()
        assert (workspace / "node_modules/library/index.js").read_text() == "dependency"
        assert (
            workspace / "packages/widget/node_modules/library/index.js"
        ).read_text() == "dependency"


@pytest.mark.parametrize(
    "configuration", ["valid", "preparation-entrypoint", "preparation-command"]
)
def test_prepared_image_must_start_with_harbor_command_override(
    monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    native = toolchain()
    native["task_preparation"] = {
        "input_key": "a" * 64,
        "validation_policy": harbor._TASK_PREPARATION_POLICY,
    }
    monkeypatch.setattr(harbor, "_assert_task_preparation", lambda identity: None)
    image = {
        "Id": native["image_id"],
        "Os": "linux",
        "Architecture": "arm64",
        "Config": {
            "Labels": harbor._toolchain_labels(native),
            "WorkingDir": "/workspace",
            "Entrypoint": ["bash"]
            if configuration == "preparation-entrypoint"
            else ["/usr/bin/env"],
            "Cmd": ["-c", "sleep infinity"]
            if configuration == "preparation-command"
            else ["sh", "-c", "sleep infinity"],
        },
    }
    monkeypatch.setattr(harbor, "_docker", lambda *args, **kwargs: json.dumps([image]))
    if configuration == "valid":
        harbor._assert_toolchain(native, expected_runtime=runtime())
    else:
        with pytest.raises(ValueError, match="frozen content or install settings"):
            harbor._assert_toolchain(native, expected_runtime=runtime())


@pytest.mark.parametrize(
    "case",
    ["stderr", "malformed", "turn.failed", "error", "exit-nonzero", "mismatch", "source-drift"],
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
    if case == "source-drift":
        monkeypatch.setattr(harbor, "_CODEX_RUN_SHA256", "changed", raising=False)
    if case in {"mismatch", "source-drift", "exit-nonzero"}:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(harbor.run_trial({"agent": {"name": "codex"}}))
        if case in {"mismatch", "source-drift"}:
            assert not counter.exists()
        if case == "source-drift":
            assert not created
        return
    result = asyncio.run(harbor.run_trial({"agent": {"name": "codex"}}))
    assert result["session_id"] == "paid-free"
    assert (agent_directory / "codex.stderr.log").is_file()
    assert diagnostic not in (agent_directory / "codex.txt").read_text()
    evidence = harbor._native_evidence(
        area=tmp_path, host="codex", effort="medium", transport="codex-stdout-stderr-v1"
    )
    assert evidence["successful_native_completion"] is (case == "stderr")
    assert (agent_directory / "codex.stderr.log").read_text() == diagnostic + "\n"
    assert (tmp_path / "artifacts/codex.stderr.log").read_text() == diagnostic + "\n"
    assert commands[0] == "set -o pipefail; printf setup"
    assert "--model gpt-6.1-sol --json --enable unified_exec -- 'task'" in commands[1]
    assert counter.read_text() == "called"


@pytest.mark.parametrize("capture", ["missing", "empty", "prelude", "unknown"])
def test_native_codex_capture_version_never_falls_back_to_legacy(
    tmp_path: Path, capture: str
) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    (agent / "codex.txt").write_text(
        (_CODEX_PRELUDE if capture == "prelude" else "") + _CODEX_COMPLETED
    )
    if capture != "missing":
        (agent / "codex.stderr.log").write_text("")
    transport = "unknown" if capture == "unknown" else "codex-stdout-stderr-v1"
    if capture in {"missing", "unknown"}:
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


def test_legacy_codex_midstream_stderr_stays_a_failure_even_with_separate_file(
    tmp_path: Path,
) -> None:
    agent = tmp_path / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    output = _CODEX_COMPLETED.replace(
        '{"type":"turn.completed"',
        'ERROR codex_core::tools::router: exec command rejected\n{"type":"turn.completed"',
    )
    (agent / "codex.txt").write_text(output)
    (agent / "codex.stderr.log").write_text("new filename does not upgrade old capture\n")
    result = harbor._native_evidence(area=tmp_path, host="codex", effort="medium")
    assert result["successful_native_completion"] is False
    assert "malformed stdout JSONL" in result["stream_errors"]
    assert (agent / "codex.txt").read_text() == output
    assert not (tmp_path / "artifacts/codex.stderr.log").exists()


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
