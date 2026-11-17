"""Every container an operation creates carries its exact invocation label from creation.

Harbor trials use Compose and one `docker container run`; direct paths use `harbor_backend._docker`.
Recovery then finds a crashed operation's containers by that label alone, without the frozen
suite or the Compose project. Docker is a fake CLI or a fake daemon: no container starts.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from pluginbench import control_store, harbor_backend, harbor_labels, invocation_runtime
from pluginbench.assessment_metrics import inspect_snapshot
from test_native_harbor import native_task

LABEL = invocation_runtime.LABEL_KEY

_FAKE_CLI = """#!{python}
import json, os, sys
from collections.abc import Iterator
from pathlib import Path
args = sys.argv[1:]
files = {{}}
for index, item in enumerate(args[:-1]):
    if item == "-f":
        files[args[index + 1]] = Path(args[index + 1]).read_text()
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({{"argv": args, "files": files}}) + "\\n")
if args[:1] == ["info"]:
    print("linux")
elif args[:1] == ["version"]:
    print("linux/amd64")
"""


@pytest.fixture
def docker_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A `docker` on PATH that answers like a Linux daemon and logs each argv with its -f files."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    cli = binaries / "docker"
    cli.write_text(_FAKE_CLI.format(python=sys.executable))
    cli.chmod(0o755)
    log = tmp_path / "docker.jsonl"
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    docker._egress_control_kernel_support.cache_clear()
    yield log
    docker._egress_control_kernel_support.cache_clear()


def _start(tmp_path: Path, invocation: str | None, role: str) -> None:
    """Start Harbor's real Docker environment for the native task's agent or separate verifier."""
    # Harbor's wheel is untyped.
    environment_type = importlib.import_module(
        "harbor.environments.docker.docker"
    ).DockerEnvironment
    task_config = importlib.import_module("harbor.models.task.config")
    NetworkMode, NetworkPolicy = task_config.NetworkMode, task_config.NetworkPolicy
    task = importlib.import_module("harbor.models.task.task").Task(native_task(tmp_path))
    paths = importlib.import_module("harbor.models.trial.paths").TrialPaths(
        tmp_path / f"trial-{role}"
    )
    paths.mkdir()
    if role == "agent":
        # An allowlist, as native coding uses, adds the egress sidecar and its kernel probe.
        directory, config = task.paths.environment_dir, task.config.environment
        policy = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["api.example"])
    else:
        directory, config = task.paths.tests_dir, task.config.verifier.environment
        policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)

    async def start() -> None:
        environment = environment_type(
            environment_dir=directory, environment_name=role, session_id=f"session-{role}",
            trial_paths=paths, task_env_config=config, network_policy=policy,
        )
        await environment.start(force_build=False)

    if invocation is None:
        asyncio.run(start())
    else:
        with harbor_labels.labelled(invocation):
            asyncio.run(start())


def _creates(log: Path) -> list[dict[str, Any]]:
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    return [
        entry for entry in entries
        if {"up", "run", "create"} & set(entry["argv"])
    ]


@pytest.mark.parametrize("role", ["agent", "verifier"])
def test_every_container_harbor_creates_for_a_trial_carries_the_label(
    tmp_path: Path, docker_cli: Path, role: str
) -> None:
    invocation = uuid4().hex
    _start(tmp_path, invocation, role)
    creates = _creates(docker_cli)
    assert any("up" in entry["argv"] for entry in creates)
    if role == "agent":
        assert any(entry["argv"][:2] == ["container", "run"] for entry in creates)
    for entry in creates:
        argv = entry["argv"]
        if argv[0] != "compose":
            assert argv[argv.index("run") + 1: argv.index("run") + 3] == [
                "--label", f"{LABEL}={invocation}",
            ]
            continue
        files = [argv[index + 1] for index, item in enumerate(argv) if item == "-f"]
        *original, overlay = files
        services = {
            name
            for path in original
            for name in (yaml.safe_load(entry["files"][path]) or {}).get("services") or {}
        }
        if role == "agent":
            assert {"main", "harbor-docker-egress-control-sidecar"} <= services
        # The overlay is last, so no file before it can override its label.
        assert yaml.safe_load(entry["files"][overlay]) == {
            "services": {name: {"labels": {LABEL: invocation}} for name in services}
        }


def test_the_adapter_ends_with_its_trial(tmp_path: Path, docker_cli: Path) -> None:
    _start(tmp_path, uuid4().hex, "agent")
    docker_cli.unlink()
    _start(tmp_path / "later", None, "agent")
    for entry in _creates(docker_cli):
        assert LABEL not in json.dumps(entry)


@pytest.mark.parametrize("change", ["version", "property"])
def test_a_changed_harbor_starts_nothing_unlabelled(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    if change == "version":
        monkeypatch.setattr(harbor_labels, "HARBOR_VERSION", "0.0.0")
    else:
        monkeypatch.setattr(docker, "_docker_compose_paths", lambda self: [])
    with pytest.raises(ValueError, match="cannot be labelled"):
        with harbor_labels.labelled(uuid4().hex):
            pytest.fail("the trial ran")


def test_an_operations_trial_worker_gets_its_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_native_process` hands the ID to the Harbor worker, whose `run_trial` labels the trial."""
    invocation = uuid4().hex
    monkeypatch.setattr(invocation_runtime, "_current", invocation)
    monkeypatch.setattr(harbor_backend, "_private_docker_config", lambda **kwargs: {})
    configs: list[dict[str, Any]] = []

    def worker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        configs.append(json.loads(Path(argv[3]).read_text()))
        Path(argv[4]).write_text("{}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(harbor_backend, "run_command", worker)
    area = tmp_path / "area"
    (area / "artifacts").mkdir(parents=True)
    harbor_backend._native_process(
        area=area, config={"trial_name": "t"}, harness="codex", timeout=5, install_only=True,
        expected={"docker_engine": {"endpoint": "unix:///fake.sock"}},
    )
    assert configs == [{"trial_name": "t", "pluginbench_invocation": invocation}]

    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    original = docker.__dict__["_docker_compose_paths"]
    seen: list[object] = []

    class Trial:
        agent_environment = SimpleNamespace(session_id="s")

        @classmethod
        async def create(cls, config: object) -> Trial:
            # Created inside the adapter, so lazily created verifier environments are covered.
            seen.append(docker.__dict__["_docker_compose_paths"])
            return cls()

        async def run(self) -> object:
            return SimpleNamespace(model_dump=lambda mode: {})

    monkeypatch.setattr(importlib.import_module("harbor.trial.trial"), "Trial", Trial)
    monkeypatch.setattr(
        importlib.import_module("harbor.models.trial.config"), "TrialConfig",
        SimpleNamespace(model_validate=lambda config: config),
    )
    asyncio.run(harbor_backend.run_trial({**configs[0], "agent": {"name": "other"}}))
    assert seen and seen[0] is not original
    assert docker.__dict__["_docker_compose_paths"] is original


@pytest.mark.parametrize(
    ("arguments", "labelled"),
    [
        (("create", "--name", "x", "image"), True),
        (("ps", "--all"), False),
        (("rm", "--force", "x"), False),
        (("exec", "x", "create"), False),
    ],
)
def test_a_direct_docker_create_inside_an_operation_carries_the_label(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...], labelled: bool
) -> None:
    calls: list[list[str]] = []

    def record(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(harbor_backend, "run_command", record)
    harbor_backend._docker(*arguments)
    invocation = uuid4().hex
    monkeypatch.setattr(invocation_runtime, "_current", invocation)
    harbor_backend._docker(*arguments)
    outside, inside = calls
    assert outside == ["docker", *arguments]
    expected = ["docker", "create", "--label", f"{LABEL}={invocation}", *arguments[1:]]
    assert inside == (expected if labelled else outside)


@pytest.mark.parametrize(
    "arguments",
    [("run", "--rm", "image"), ("container", "run", "image"), ("container", "create", "image")],
)
def test_harbors_container_runs_carry_the_label(arguments: tuple[str, ...]) -> None:
    """Harbor's engine commands go through `with_label` (`harbor_labels`)."""
    invocation = uuid4().hex
    command = 2 if arguments[0] == "container" else 1
    assert invocation_runtime.with_label(arguments, invocation) == (
        *arguments[:command], "--label", f"{LABEL}={invocation}", *arguments[command:],
    )


class _Daemon:
    """A fake Docker daemon behind the frozen CLI: containers by ID, with their labels."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, str]] = {}

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        arguments = command[3:]  # After the CLI and its --host.
        output, status = "", 0
        if arguments[0] == "create":
            key = f"{len(self.containers):064x}"
            self.containers[key] = dict(
                arguments[index + 1].split("=", 1)
                for index, item in enumerate(arguments)
                if item == "--label"
            )
            output = key + "\n"
        elif arguments[0] == "exec":
            status = 1  # The measurement fails, and its process dies before its cleanup.
        elif arguments[0] == "ps":
            name, value = arguments[-1].removeprefix("label=").split("=", 1)
            output = "".join(
                f"{key}\n" for key, labels in self.containers.items() if labels.get(name) == value
            )
        elif arguments[0] == "inspect":
            output = "".join(f"{self.containers[key].get(LABEL, '')}\n" for key in arguments[3:])
        elif arguments[0] == "rm":
            for key in arguments[3:]:
                self.containers.pop(key, None)
        return subprocess.CompletedProcess(command, status, output, "failed" if status else "")


def test_recovery_removes_a_crashed_measurements_container_by_its_label_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measurement container left by a killed operation, with no frozen suite to find it by."""
    daemon = _Daemon()
    daemon.containers["f" * 64] = {LABEL: uuid4().hex}  # Another operation's: never removed.
    runtime: dict[str, object] = {
        "resources": {"cpus": 1, "memory_mb": 256},
        "docker_engine": {"id": "daemon", "endpoint": "unix:///fake.sock"},
        "docker_cli": {},
    }
    monkeypatch.setattr(harbor_backend, "run_command", daemon)
    monkeypatch.setattr(harbor_backend, "assert_runtime", lambda expected, **kwargs: expected)
    monkeypatch.setattr(
        harbor_backend, "_frozen_executable", lambda identity, label: {"resolved_path": "docker"}
    )
    monkeypatch.setattr(harbor_backend, "_assert_daemon", lambda runtime, timeout=0: None)
    # The process dies before its own cleanup runs.
    monkeypatch.setattr(harbor_backend, "cleanup", lambda **kwargs: {"status": "complete"})
    monkeypatch.setattr(control_store, "INVENTORY_INTERVAL", 0.0)
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("code\n")
    invocation = uuid4().hex
    monkeypatch.setattr(invocation_runtime, "_current", invocation)
    with pytest.raises(ValueError, match="docker exec failed"):
        harbor_backend.run_native_command(
            snapshot=source,
            task_identity={
                "isolation": runtime, "image_identity": {"image_id": "sha256:frozen"},
                "workdir": "/app",
            },
            destination=tmp_path / "measure",
            command=["python", "-m", "tests"],
            report_path="coverage.info",
            timeout=5,
        )
    assert inspect_snapshot(source)  # The frozen input is intact; only the container remains.
    left = [key for key, labels in daemon.containers.items() if labels.get(LABEL) == invocation]
    assert len(left) == 1
    # Recovery runs in the next owner, outside any operation.
    monkeypatch.setattr(invocation_runtime, "_current", None)
    spec = invocation_runtime.InvocationSpec(invocation, runtime)
    proof = invocation_runtime.verify_labelled(
        {"id": invocation, "label": spec.label, "docker": runtime},
        deadline=time.monotonic() + 30,
    )
    assert proof["removed"] == left
    assert list(daemon.containers) == ["f" * 64]


@pytest.mark.parametrize(
    "arguments",
    [
        ("create", "--label", f"{LABEL}=other", "image"),
        ("create", f"--label={LABEL}=other", "image"),
        ("create", "-l", f"{LABEL}=other", "image"),
        ("create", f"-l{LABEL}=other", "image"),
        ("create", f"-l={LABEL}=other", "image"),
        ("create", f"-il{LABEL}=other", "image"),
        ("create", f"-il={LABEL}=other", "image"),
        ("create", "--label", f"{LABEL}.note=x", "image"),
        ("create", "--label-file", "labels.txt", "image"),
        ("create", "--label-file=labels.txt", "image"),
    ],
)
def test_an_argument_naming_the_invocation_label_is_refused(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    """Docker keeps the last of repeated labels, in many spellings, so no argument may name it."""
    daemon = _Daemon()
    monkeypatch.setattr(harbor_backend, "run_command", daemon)
    monkeypatch.setattr(invocation_runtime, "_current", uuid4().hex)
    with pytest.raises(ValueError, match="only PluginBench sets"):
        harbor_backend._docker(*arguments)
    assert daemon.containers == {}
    # The same refusal guards Harbor's own `docker container run`.
    with pytest.raises(ValueError, match="only PluginBench sets"):
        invocation_runtime.with_label(("container", "run", *arguments[1:]), uuid4().hex)


_BUILD = ("build", "--quiet", "--provenance=false", "--tag",
          f"pluginbench-native-build:{'0' * 32}", "/context")


@pytest.mark.parametrize("driver", ["docker", "docker-container"])
def test_a_build_inside_an_operation_runs_only_in_the_engines_own_builder(
    monkeypatch: pytest.MonkeyPatch, driver: str
) -> None:
    """An operator's buildx builder may run BuildKit in a container that carries no label."""
    monkeypatch.setenv("BUILDX_BUILDER", "operator-builder")
    monkeypatch.setenv("DOCKER_BUILDKIT", "0")  # The legacy builder's containers are unlabelled.
    calls: list[tuple[list[str], dict[str, str]]] = []

    def record(
        command: list[str], *, env: dict[str, str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, env))
        output = f"Name: default\nDriver: {driver}\n" if command[1:3] == ["buildx", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(harbor_backend, "run_command", record)
    harbor_backend._docker(*_BUILD)
    outside = calls.pop()
    assert outside[1]["BUILDX_BUILDER"] == "operator-builder"  # Outside an operation, unchanged.
    monkeypatch.setattr(invocation_runtime, "_current", uuid4().hex)
    if driver != "docker":
        with pytest.raises(ValueError, match="built-in builder"):
            harbor_backend._docker(*_BUILD)
        assert [command[1] for command, _ in calls] == ["buildx"]  # Never built.
        return
    harbor_backend._docker(*_BUILD)
    (inspect, inspect_env), (build, build_env) = calls
    assert inspect[1:] == ["buildx", "inspect"] and tuple(build[1:]) == _BUILD
    for env in (inspect_env, build_env):
        assert (env["BUILDX_BUILDER"], env["DOCKER_BUILDKIT"]) == ("default", "1")


@pytest.mark.parametrize(
    "arguments",
    [
        ("build", "--builder", "other", *_BUILD[1:]),
        (*_BUILD[:4], "--builder=other", *_BUILD[4:]),
        (*_BUILD, "--builder", "other"),
        ("build", "--tag", "t", "/context"),
        ("image", "build", *_BUILD[1:]),
        ("builder", "build", *_BUILD[1:]),
        ("buildx", "build", "--builder=other", "/context"),
        ("buildx", "--builder", "other", "build", "/context"),
        ("buildx", "--builder", "other", "create", "--use"),
        ("buildx", "create", "--use"),
        ("buildx", "inspect"),
        ("context", "use", "other"),
        ("--context", "other", "create", "image"),
        ("-c", "other", "create", "image"),
        ("-H", "unix:///other.sock", "create", "image"),
        ("--host", "unix:///other.sock", "create", "image"),
        ("compose", "up"),
        ("run", "image"),
    ],
)
def test_an_operation_sends_only_the_docker_commands_pluginbench_issues(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    daemon = _Daemon()
    monkeypatch.setattr(harbor_backend, "run_command", daemon)
    monkeypatch.setattr(invocation_runtime, "_current", uuid4().hex)
    with pytest.raises(ValueError, match="only the commands PluginBench issues"):
        harbor_backend._docker(*arguments)
    assert daemon.containers == {}


def test_every_docker_command_pluginbench_issues_is_admitted() -> None:
    """The allowlist covers each `_docker` call in the source, so routing breaks none of them."""
    source = Path(harbor_backend.__file__).read_text()
    first = set(re.findall(r'\b_?docker\(\s*"([a-z]+)"', source))
    assert first and first <= harbor_backend._OPERATION_COMMANDS | set(
        harbor_backend._OPERATION_EXACT
    )
    for arguments in [_BUILD, ("context", "inspect"), ("compose", "version", "--short")]:
        harbor_backend._admit_operation_command(arguments)


@pytest.mark.parametrize(
    "document",
    [
        {"include": ["other.yaml"], "services": {"main": {"image": "x"}}},
        {"models": {"llm": {"model": "x"}}, "services": {"main": {"image": "x"}}},
        {"services": {"main": {"extends": {"file": "other.yaml", "service": "base"}}}},
        {"services": {"main": {"provider": {"type": "model"}}}},
        {"services": ["main"]},
    ],
)
def test_a_compose_shape_that_could_add_unlabelled_services_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]
) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(yaml.safe_dump(document))
    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    monkeypatch.setattr(docker, "_docker_compose_paths", property(lambda self: [compose]))
    environment = SimpleNamespace(session_id="s")
    with harbor_labels.labelled(uuid4().hex):
        with pytest.raises(ValueError, match="nothing started|does not map"):
            docker._docker_compose_paths.fget(environment)


def test_compose_extension_fields_are_not_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(yaml.safe_dump(
        {"x-common": {"image": "x"}, "networks": {"n": {}}, "services": {"main": {"image": "x"}}}
    ))
    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    monkeypatch.setattr(docker, "_docker_compose_paths", property(lambda self: [compose]))
    invocation = uuid4().hex
    with harbor_labels.labelled(invocation):
        *_, overlay = docker._docker_compose_paths.fget(SimpleNamespace(session_id="s"))
        assert yaml.safe_load(overlay.read_text()) == {
            "services": {"main": {"labels": {LABEL: invocation}}}
        }


@pytest.mark.parametrize("key", ["DOCKER_HOST", "DOCKER_CONFIG", "BUILDX_BUILDER", "COMPOSE_FILE"])
@pytest.mark.parametrize("where", ["environment", "verifier"])
def test_a_native_task_cannot_set_docker_controls(tmp_path: Path, key: str, where: str) -> None:
    task = native_task(tmp_path)
    toml = (task / "task.toml").read_text()
    section = "[environment.env]" if where == "environment" else "[verifier.env]"
    anchor = "[verifier]" if where == "environment" else "[verifier.environment]"
    (task / "task.toml").write_text(
        toml.replace(anchor, f'{section}\n{key}="/elsewhere"\n{anchor}', 1)
    )
    with pytest.raises(ValueError, match=f"must not set Docker controls: {key}"):
        harbor_backend.validate_native_task(task)


@pytest.mark.parametrize("key", ["DOCKER_HOST", "DOCKER_CONFIG", "BUILDX_BUILDER", "COMPOSE_FILE"])
def test_harbors_compose_commands_keep_the_workers_docker_controls(
    tmp_path: Path, docker_cli: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A task variable Harbor merges into Compose's environment cannot redirect it."""
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "private-config"))
    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    original = docker.__dict__["_compose_env_vars"]

    def with_task_env(self: Any, include_os_env: bool = True) -> dict[str, str]:
        env: dict[str, str] = original(self, include_os_env)
        return {**env, key: "/elsewhere"}

    monkeypatch.setattr(docker, "_compose_env_vars", with_task_env)
    with pytest.raises(ValueError, match=f"changes {key}"):
        _start(tmp_path, uuid4().hex, "agent")
    assert not [entry for entry in _creates(docker_cli) if entry["argv"][0] == "compose"]
