"""Harbor execution with a stopped-container submission boundary.

PluginBench freezes the installed agent image; Harbor runs the agent. The
workspace is copied only after Docker proves the container is stopped, then
the native separate verifier grades collected artifacts and owned resources
are removed. Source and graded-artifact evidence remain distinct.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pluginbench import codex, isolation, submissions, native_treatments
from pluginbench.treatments import Treatment
from pluginbench.native_treatments import NativeCapabilities
from pluginbench.code_diff import _is_source_path, _is_test_path
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.assessment_metrics import EXCLUDED_NAMES
from pluginbench.processes import run_command

HARBOR_VERSION = "0.24.0"
PROTOCOL = "harbor-docker-v1"
_OWNER_LABEL = "org.pluginbench.trial"
_BASE_TOOLS = "curl bash node npm"
_DOCKER_TIMEOUT = 60
_CODEX_TRANSPORT_VERSION = "codex-stdout-stderr-v1"
_CODEX_RUN_SHA256 = "5b14fbcc889a398f80c7b5f7e2a621efc1337ba72e748fafefbab2ad09ad86bd"
_CODEX_EXEC_PREFIX = (
    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
    "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --model "
)
_CODEX_EXEC_SUFFIX = "2>&1 </dev/null | tee /logs/agent/codex.txt"
NATIVE_PROVIDER_HOSTS = {
    "codex": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
    "claude": ["api.anthropic.com", "console.anthropic.com", "claude.ai"],
}
NATIVE_TRANSPORT_POLICY = "native-cli-provider-hosts-v1"
_PREPARATION_POLICY = "native-task-and-qualified-patch-source-v3"


@dataclass(frozen=True)
class _NativePreparation:
    admission: dict[str, object]
    runtime: dict[str, object]
    base: dict[str, str]
    grader: dict[str, str]


def _assert_codex_transport_source() -> str:
    if importlib.metadata.version("harbor") != HARBOR_VERSION:
        raise ValueError("Codex transport requires the pinned Harbor version")
    agent = importlib.import_module("harbor.agents.installed.codex").Codex
    digest = hashlib.sha256(inspect.getsource(agent.run).encode()).hexdigest()
    if digest != _CODEX_RUN_SHA256:
        raise ValueError("Harbor Codex.run changed; refusing the native transport adapter")
    return digest


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return {key: item for key, item in value.items()}


def _docker(
    *arguments: str,
    timeout: int = _DOCKER_TIMEOUT,
    expected_runtime: dict[str, object] | None = None,
    receipt: Path | None = None,
) -> str:
    argv = ["docker"]
    env = dict(os.environ)
    if expected_runtime is not None:
        cli = _object(expected_runtime.get("docker_cli"), "frozen Docker CLI")
        argv = [_frozen_executable(cli, "CLI")["resolved_path"]]
        engine = _object(expected_runtime.get("docker_engine"), "frozen Docker engine")
        endpoint = engine.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
            raise ValueError("Frozen Docker endpoint must be a local Unix socket")
        argv.extend(["--host", endpoint])
        env.pop("DOCKER_CONTEXT", None)
        env.pop("DOCKER_HOST", None)
    command = [*argv, *arguments]
    try:
        result = run_command(command, cwd=Path.cwd(), env=env, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if receipt is not None:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "command": command,
                        "returncode": None,
                        "timed_out": True,
                        "stdout": (error.output or b"").decode("utf-8", errors="replace"),
                        "stderr": (error.stderr or b"").decode("utf-8", errors="replace"),
                    },
                    sort_keys=True,
                )
            )
        raise
    if receipt is not None:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "command": command,
                    "returncode": result.returncode,
                    "timed_out": False,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                sort_keys=True,
            )
        )
    if result.returncode:
        raise ValueError(f"docker {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _assert_daemon(expected_runtime: dict[str, object]) -> None:
    engine = _object(expected_runtime.get("docker_engine"), "frozen Docker engine")
    actual = _object(
        json.loads(_docker("info", "--format", "{{json .}}", expected_runtime=expected_runtime)),
        "Docker daemon",
    )
    if not engine.get("id") or actual.get("ID") != engine["id"]:
        raise ValueError("Docker daemon changed; cannot accept export or cleanup proof")


def _engine() -> tuple[dict[str, Any], str]:
    info = _object(json.loads(_docker("info", "--format", "{{json .}}")), "Docker info")
    contexts = json.loads(_docker("context", "inspect"))
    if not isinstance(contexts, list) or len(contexts) != 1:
        raise ValueError("Docker must have one selected context")
    context = _object(contexts[0], "Docker context")
    endpoints = _object(context.get("Endpoints"), "Docker endpoints")
    docker = _object(endpoints.get("docker"), "Docker endpoint")
    endpoint = (
        docker.get("Host")
        if os.environ.get("DOCKER_CONTEXT")
        else os.environ.get("DOCKER_HOST") or docker.get("Host")
    )
    if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
        raise ValueError("Harbor execution currently requires a local Unix Docker endpoint")
    return info, endpoint


def _executable_identity(path: Path) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    if not path.is_absolute() or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Docker executables must be readable absolute executable files")
    with resolved.open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    return {"path": str(path), "resolved_path": str(resolved), "sha256": digest}


def _docker_plugins(info: dict[str, Any]) -> dict[str, dict[str, str]]:
    client = _object(info.get("ClientInfo"), "Docker client metadata")
    plugins = client.get("Plugins")
    if not isinstance(plugins, list):
        raise ValueError("Docker did not report its CLI plugins")
    result: dict[str, dict[str, str]] = {}
    for name in ("compose", "buildx"):
        selected = [item for item in plugins if isinstance(item, dict) and item.get("Name") == name]
        if len(selected) != 1:
            raise ValueError(f"Docker must report one usable {name} plugin")
        plugin = selected[0]
        path, version = plugin.get("Path"), plugin.get("Version")
        if not isinstance(path, str) or not isinstance(version, str) or not version:
            raise ValueError(f"Docker did not report the {name} executable and version")
        result[name] = {**_executable_identity(Path(path)), "version": version}
    return result


def _docker_cli(info: dict[str, Any]) -> dict[str, str]:
    client = _object(info.get("ClientInfo"), "Docker client metadata")
    version, path = client.get("Version"), shutil.which("docker")
    if not path or not isinstance(version, str) or not version:
        raise ValueError("Docker did not report its client executable and version")
    return {**_executable_identity(Path(path)), "version": version}


def _frozen_executable(identity: dict[str, Any], label: str) -> dict[str, str]:
    try:
        actual = _executable_identity(Path(identity["path"]))
    except (OSError, ValueError) as error:
        raise isolation.RuntimeDriftError(f"Cannot verify frozen Docker {label}") from error
    if any(identity.get(key) != value for key, value in actual.items()):
        raise isolation.RuntimeDriftError(f"Docker {label} changed since preparation")
    return actual


def _private_docker_config(
    *, private: Path, env: dict[str, str], expected_runtime: dict[str, object]
) -> dict[str, object]:
    """Expose only frozen plugin binaries, never the operator's Docker settings."""
    plugins = _object(expected_runtime.get("docker_plugins"), "frozen Docker plugins")
    config = private / "docker"
    directory = config / "cli-plugins"
    directory.mkdir(parents=True, mode=0o700)
    cli = _object(expected_runtime.get("docker_cli"), "frozen Docker CLI")
    actual_cli = _frozen_executable(cli, "CLI")
    binaries = private / "docker-bin"
    binaries.mkdir(mode=0o700)
    (binaries / "docker").symlink_to(actual_cli["resolved_path"])
    env["PATH"] = str(binaries) + os.pathsep + env.get("PATH", os.defpath)
    for name in ("compose", "buildx"):
        plugin = _object(plugins.get(name), f"frozen {name} plugin")
        actual = _frozen_executable(plugin, name + " plugin")
        (directory / ("docker-" + name)).symlink_to(actual["resolved_path"])
    env["DOCKER_CONFIG"] = str(config)
    try:
        selected_cli = shutil.which("docker", path=env["PATH"])
        if selected_cli is None:
            raise ValueError("Private Docker executable is unavailable")
        observed_cli = _executable_identity(Path(selected_cli))
        discovery = run_command(
            ["docker", "info", "--format", "{{json .ClientInfo}}"],
            cwd=private,
            env=env,
            timeout=_DOCKER_TIMEOUT,
        )
        if discovery.returncode:
            raise ValueError("Private Docker client discovery failed")
        client = _object(json.loads(discovery.stdout), "isolated Docker client")
        observed = _docker_plugins({"ClientInfo": client})
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise isolation.RuntimeDriftError(
            "Cannot verify isolated Docker plugin discovery"
        ) from error
    if (
        client.get("Version") != cli["version"]
        or any(observed_cli[key] != cli[key] for key in ("resolved_path", "sha256"))
        or any(
            observed[name][key] != plugins[name][key]
            for name in ("compose", "buildx")
            for key in ("resolved_path", "sha256", "version")
        )
    ):
        raise isolation.RuntimeDriftError(
            "Isolated Docker executable identity differs from preparation"
        )
    for name in ("compose", "buildx"):
        argv = ["docker", name, "version"] + (["--short"] if name == "compose" else [])
        try:
            result = run_command(argv, cwd=private, env=env, timeout=_DOCKER_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise isolation.RuntimeDriftError(f"Cannot verify isolated Docker {name}") from error
        version = plugins[name]["version"]
        if result.returncode or version.removeprefix("v") not in {
            token.removeprefix("v") for token in result.stdout.split()
        }:
            raise isolation.RuntimeDriftError(
                f"Isolated Docker {name} differs from its frozen version"
            )
    return {
        "docker_cli": {**observed_cli, "version": client["Version"]},
        "docker_plugins": observed,
    }


def runtime_identity(*, cpus: int = 1, memory_mb: int = 2048) -> dict[str, object]:
    """Fingerprint the framework, wrapper, selected daemon and enforced limits."""
    try:
        version = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError as error:
        raise ValueError(f"Harbor {HARBOR_VERSION} is required for new coding runs") from error
    if version != HARBOR_VERSION:
        raise ValueError(f"Harbor {HARBOR_VERSION} is required; installed version is {version}")
    if cpus < 1 or memory_mb < 128:
        raise ValueError("Container limits require at least one CPU and 128 MB memory")
    info, endpoint = _engine()
    required = ("ID", "ServerVersion", "OSType", "Architecture")
    if not all(isinstance(info.get(key), str) and info[key] for key in required):
        raise ValueError("Docker did not report a complete daemon identity")
    if info["OSType"] != "linux":
        raise ValueError("Harbor execution currently requires Linux containers")
    capacity = {"cpus": info.get("NCPU"), "memory_bytes": info.get("MemTotal")}
    if not all(isinstance(value, int) and value > 0 for value in capacity.values()):
        raise ValueError("Docker did not report usable CPU and memory capacity")
    return {
        "protocol": PROTOCOL,
        "harbor_version": version,
        "submission_receipt_version": 2,
        "codex_transport": {
            "version": _CODEX_TRANSPORT_VERSION,
            "harbor_run_sha256": _assert_codex_transport_source(),
        },
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "docker_cli": _docker_cli(info),
        "docker_plugins": _docker_plugins(info),
        "docker_engine": {
            "id": info["ID"],
            "version": info["ServerVersion"],
            "platform": info["OSType"] + "/" + info["Architecture"],
            "endpoint": endpoint,
            "capacity": capacity,
        },
        "resources": {
            "cpus": cpus,
            "memory_mb": memory_mb,
            "cpu_enforcement": "limit",
            "memory_enforcement": "limit",
        },
    }


def assert_runtime(expected: dict[str, object]) -> None:
    resources = _object(expected.get("resources"), "container resources")
    cpus, memory = resources.get("cpus"), resources.get("memory_mb")
    if not isinstance(cpus, int) or not isinstance(memory, int):
        raise ValueError("Frozen container resource limits are missing")
    try:
        actual = runtime_identity(cpus=cpus, memory_mb=memory)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise isolation.RuntimeDriftError(
            "Cannot verify the frozen Harbor and Docker runtime"
        ) from error
    if actual != expected:
        raise isolation.RuntimeDriftError(
            "Harbor or Docker runtime changed since evaluation preparation"
        )


def capacity_jobs(*, cpus: int = 1, memory_mb: int = 2048) -> int:
    """Bound coding concurrency by the actual Docker allocation, leaving headroom."""
    if cpus < 1 or memory_mb < 128:
        raise ValueError("Container limits require at least one CPU and 128 MB memory")
    info, _ = _engine()
    cpu_capacity, memory_capacity = info.get("NCPU"), info.get("MemTotal")
    if not isinstance(cpu_capacity, int) or not isinstance(memory_capacity, int):
        raise ValueError("Docker did not report CPU and memory capacity")
    jobs = min(4, cpu_capacity // cpus, int(memory_capacity * 0.8) // (memory_mb * 1024**2))
    if jobs < 1:
        raise ValueError(
            f"One coding container ({cpus} CPUs, {memory_mb} MB) does not fit within Docker capacity ({cpu_capacity} CPUs, {memory_capacity // 1024**2} MB; 80% memory budget)"
        )
    return jobs


def preflight(*, jobs: int = 1, cpus: int = 1, memory_mb: int = 2048) -> dict[str, Any]:
    """Check local capabilities and capacity without installing or calling a model."""
    runtime = runtime_identity(cpus=cpus, memory_mb=memory_mb)
    info, _ = _engine()
    if jobs < 1 or jobs * cpus > info.get("NCPU", 0):
        raise ValueError("Requested concurrent container CPU limits exceed Docker capacity")
    if jobs * memory_mb * 1024**2 > info.get("MemTotal", 0) * 0.8:
        raise ValueError("Requested concurrent container memory exceeds 80% of Docker capacity")
    compose = _docker("compose", "version", "--short").strip()
    return {"runtime": runtime, "compose_version": compose, "paid_calls": 0}


def _inspect_image(
    image: str, *, pull: bool, expected_runtime: dict[str, object] | None = None
) -> dict[str, Any]:
    try:
        inspected = _docker("image", "inspect", image, expected_runtime=expected_runtime)
    except ValueError as error:
        if not pull or "no such image" not in str(error).lower():
            raise
        _docker("pull", image, timeout=600, expected_runtime=expected_runtime)
        inspected = _docker("image", "inspect", image, expected_runtime=expected_runtime)
    images = json.loads(inspected)
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Docker did not identify one image")
    return _object(images[0], "Docker image")


def image_identity(
    image: str, *, pull: bool = False, expected_runtime: dict[str, object] | None = None
) -> dict[str, str]:
    """Bind an immutable base image to the local platform that will run it."""
    if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("Agent images must use an immutable sha256 registry digest")
    image_info = _inspect_image(image, pull=pull, expected_runtime=expected_runtime)
    config = _object(image_info.get("Config"), "Docker image config")
    workdir = config.get("WorkingDir")
    if not isinstance(workdir, str) or re.fullmatch(r"/[A-Za-z0-9_-]+", workdir) is None:
        raise ValueError("Base image must have one explicit project directory below /")
    if workdir in {"/root", "/home", "/tmp", "/usr", "/bin", "/etc", "/var", "/logs"}:
        raise ValueError(f"Unsupported base-image project directory: {workdir}")
    return {
        "image": image,
        "image_id": str(image_info["Id"]),
        "platform": str(image_info["Os"]) + "/" + str(image_info["Architecture"]),
        "workdir": workdir,
    }


def resolve_image(image: str) -> dict[str, str]:
    """Resolve a task's image tag once during preparation, then freeze its digest."""
    if not image or any(character.isspace() for character in image):
        raise ValueError("Task image must be a nonempty Docker image reference")
    if "@" in image:
        return {**image_identity(image, pull=True), "original_image": image}
    original = _inspect_image(image, pull=True)
    repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    digests = original.get("RepoDigests")
    if not isinstance(digests, list):
        raise ValueError("Task image has no immutable repository digest")
    matches = sorted(
        digest
        for digest in digests
        if isinstance(digest, str) and digest.startswith(repository + "@sha256:")
    )
    if not matches:
        raise ValueError("Task image has no immutable digest for the requested repository")
    resolved = image_identity(matches[0])
    if resolved["image_id"] != original.get("Id"):
        raise ValueError("Task image changed while resolving its immutable digest")
    return {**resolved, "original_image": image}


def _normalize_version(version: str) -> str:
    return version.removeprefix("codex-cli ").split()[0]


def _required_tools(harness: str) -> str:
    support = "rg" if harness == "codex" else "ps"
    return f'for tool in {_BASE_TOOLS} {support}; do command -v "$tool" >/dev/null; done'


def _cli_guard(harness: str, version: str) -> str:
    if (
        harness not in {"codex", "claude"}
        or re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) is None
    ):
        raise ValueError("Toolchains require a supported harness and exact CLI version")
    tools = _required_tools(harness) + "; "
    if harness == "codex":
        return tools + f'test "$(codex --version)" = "codex-cli {version}"'
    return tools + f'actual="$(claude --version)"; test "${{actual%% *}}" = "{version}"'


async def run_trial(config: dict[str, Any]) -> dict[str, Any]:
    """Run one native trial, without queue retries or Harbor resume behavior."""
    # Harbor's wheel is untyped. Keep its dynamic API at this single boundary,
    # without making reporting import its execution dependencies.
    config = dict(config)
    boundary = config.pop("pluginbench_boundary", None)
    agent = _object(config.get("agent"), "native agent configuration")
    separate_codex = agent.get("name") == "codex" and config.get("source_trial") is None
    if separate_codex:
        _assert_codex_transport_source()
    trial_config = importlib.import_module("harbor.models.trial.config").TrialConfig
    trial_type = importlib.import_module("harbor.trial.trial").Trial
    trial = await trial_type.create(trial_config.model_validate(config))
    if boundary is not None:
        install_native_hooks(trial, _native_object(boundary, "native boundary"))
    if separate_codex:
        original_exec = trial.agent.exec_as_agent
        treatment_installed = False

        async def separated_exec(
            environment: object,
            command: str,
            env: dict[str, str] | None = None,
            cwd: str | None = None,
            timeout_sec: int | None = None,
        ) -> object:
            nonlocal treatment_installed
            if "codex exec" in command:
                if not command.startswith(_CODEX_EXEC_PREFIX) or not command.endswith(
                    _CODEX_EXEC_SUFFIX
                ):
                    raise ValueError(
                        "Native Codex invocation differs from the pinned transport contract"
                    )
                if boundary is not None and "treatment" in boundary:
                    if treatment_installed:
                        raise ValueError("native treatment was already used; use a fresh attempt")
                    if env is None:
                        raise ValueError("native coding command has no private environment")
                    await native_treatments.install(
                        environment=environment,
                        treatment=Treatment.model_validate(boundary["treatment"]),
                        env=env,
                        cwd=str(boundary["image"]["workdir"]),
                        artifacts=Path(str(boundary["area"])) / "artifacts",
                    )
                    treatment_installed = True
                command = command.removesuffix(_CODEX_EXEC_SUFFIX) + (
                    "2>/logs/agent/codex.stderr.log </dev/null | tee /logs/agent/codex.txt"
                )
            result: object = await original_exec(
                environment,
                command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
            return result

        trial.agent.exec_as_agent = separated_exec
    result = await trial.run()
    return {
        "result": result.model_dump(mode="json"),
        "session_id": trial.agent_environment.session_id,
    }


def _project_ids(kind: str, project: str, expected_runtime: dict[str, object]) -> list[str]:
    arguments = [kind, "ls", "--quiet"] if kind != "container" else ["ps", "-aq"]
    output = _docker(
        *arguments,
        "--filter",
        f"label=com.docker.compose.project={project}",
        expected_runtime=expected_runtime,
    )
    return output.split()


def stopped_container_state(value: object) -> dict[str, object]:
    """Retain engine failure evidence independently of native agent completion."""
    state = _object(value, "container state")
    if state.get("Running") is not False or state.get("Status") not in {"exited", "dead"}:
        raise ValueError("Submission container is not stopped; refusing a live snapshot")
    if (
        type(state.get("OOMKilled")) is not bool
        or type(state.get("ExitCode")) is not int
        or state["ExitCode"] < 0
    ):
        raise ValueError("Docker did not report complete stopped-container failure evidence")
    if not all(isinstance(state.get(key), str) for key in ("Error", "StartedAt", "FinishedAt")):
        raise ValueError("Docker did not report stopped-container error and timestamps")
    return {
        key: state[key]
        for key in (
            "Running",
            "Status",
            "OOMKilled",
            "ExitCode",
            "Error",
            "StartedAt",
            "FinishedAt",
        )
    }


def cleanup(*, project: str, owner: str, expected_runtime: dict[str, object]) -> dict[str, object]:
    """Remove only this trial's labelled resources and verify that none remain."""
    _assert_daemon(expected_runtime)
    containers = _project_ids("container", project, expected_runtime)
    if containers:
        _docker("rm", "--force", "--volumes", *containers, expected_runtime=expected_runtime)
    networks = _project_ids("network", project, expected_runtime)
    if networks:
        _docker("network", "rm", *networks, expected_runtime=expected_runtime)
    images = _docker(
        "image",
        "ls",
        "-q",
        "--filter",
        f"label={_OWNER_LABEL}={owner}",
        expected_runtime=expected_runtime,
    ).split()
    if images:
        _docker("image", "rm", *images, expected_runtime=expected_runtime)
    residuals = {
        "containers": _project_ids("container", project, expected_runtime),
        "networks": _project_ids("network", project, expected_runtime),
        "images": _docker(
            "image",
            "ls",
            "-q",
            "--filter",
            f"label={_OWNER_LABEL}={owner}",
            expected_runtime=expected_runtime,
        ).split(),
    }
    if any(residuals.values()):
        raise ValueError(f"Owned Harbor resources remain after cleanup: {residuals}")
    _assert_daemon(expected_runtime)
    return {"status": "complete", "residuals": residuals}


def _native_evidence(
    *,
    area: Path,
    host: str,
    effort: str,
    transport: str | None = None,
    include_descendants: bool = False,
) -> dict[str, Any]:
    codex.validate_artifacts(area / "artifacts")
    agent = area / "artifacts/harbor/trial/agent"
    raw = agent / ("codex.txt" if host == "codex" else "claude-code.txt")

    stdout = raw.read_text(errors="replace") if raw.is_file() else ""
    sessions = agent / "sessions"
    traces = [path.read_text(errors="replace") for path in sorted(sessions.rglob("*.jsonl"))]
    artifacts = area / "artifacts"
    if host == "codex":
        if transport != _CODEX_TRANSPORT_VERSION:
            raise ValueError("Unsupported frozen Codex transport version")
        stderr = agent / "codex.stderr.log"
        if not raw.is_file() or not stderr.is_file():
            raise ValueError("Native Codex separate stdout/stderr capture is incomplete")
        shutil.copyfile(stderr, artifacts / "codex.stderr.log")
    (artifacts / "stdout.jsonl").write_text(stdout)
    for index, trace in enumerate(traces):
        (artifacts / f"host-trace-{index}.jsonl").write_text(trace)
    return codex.parse_events(
        stdout, traces, host, requested_effort=effort, include_descendants=include_descendants
    )


def _native_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return {key: item for key, item in value.items()}


def _native_asset_inventory(task_dir: Path, *, exclude: str | None = None) -> dict[str, str]:
    files: dict[str, str] = {}
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise ValueError("Native task must be a directory, not a link")
    for path in sorted(task_dir.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"Unsupported native task asset: {path}")
        if path.relative_to(task_dir).as_posix() == exclude:
            continue
        if path.is_file():
            files[path.relative_to(task_dir).as_posix()] = hashlib.sha256(
                str(path.stat().st_mode & 0o777).encode() + b"\0" + path.read_bytes()
            ).hexdigest()
        else:
            files[path.relative_to(task_dir).as_posix() + "/"] = "directory:" + str(
                path.stat().st_mode & 0o777
            )
    return files


def native_task_assets(task_dir: Path) -> str:
    """Bind all asset bytes, directories and modes without following links."""
    return hashlib.sha256(
        json.dumps(_native_asset_inventory(task_dir), sort_keys=True).encode()
    ).hexdigest()


def validate_native_task(task_dir: Path) -> dict[str, object]:
    """Admit standard tasks by execution topology, never by task name."""
    native_task_assets(task_dir)
    task = importlib.import_module("harbor.models.task.task").Task(task_dir)
    modes = importlib.import_module("harbor.models.task.verifier_mode")
    if task.has_steps or modes.resolve_task_verifier_mode(task.config).value != "separate":
        raise ValueError("Native execution requires a single-step task with a separate verifier")
    definition = modes.resolve_verifier_environment_definition(task.config, task.paths)
    if definition is None:
        raise ValueError("Native separate verifier definition is missing")
    for directory, configuration in (
        (task.paths.environment_dir, task.config.environment),
        (definition.directory, definition.config),
    ):
        if configuration.os.value != "linux" or configuration.gpus or configuration.tpu:
            raise ValueError("Native execution supports Linux CPU tasks only")
        if any(
            key in configuration.env
            for key in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
        ):
            raise ValueError(
                "Custom native provider endpoints or agent configuration directories are unsupported"
            )
        if (directory / "docker-compose.yaml").exists() or (
            directory / "docker-compose.yml"
        ).exists():
            raise ValueError(
                "Task-authored Docker Compose is unsupported: host mounts and service topology are not admitted"
            )
        if not configuration.docker_image and not (directory / "Dockerfile").is_file():
            raise ValueError("Native environment needs a Docker image or Dockerfile")
    return {
        "task_assets_sha256": native_task_assets(task_dir),
        "configuration": task.config.model_dump(mode="json"),
        "verifier_directory": str(definition.directory),
        "verifier_bundled_tests": definition.bundled_tests,
        "verifier_configuration": definition.config.model_dump(mode="json"),
    }


def _native_image(
    *, directory: Path, image: str | None, expected: dict[str, object]
) -> dict[str, str]:
    if image:
        description = _inspect_image(image, pull=True, expected_runtime=expected)
    else:
        description = _build_native_image(directory=directory, expected=expected)
    image_id = description.get("Id")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("Native environment did not resolve to an immutable image ID")
    config = _native_object(description.get("Config"), "native image config")
    return {
        "image": _native_image_reference(image_id=image_id, expected=expected),
        "image_id": image_id,
        "platform": str(description["Os"]) + "/" + str(description["Architecture"]),
        "workdir": str(config.get("WorkingDir") or "/"),
    }


def _build_native_image(*, directory: Path, expected: dict[str, object]) -> dict[str, object]:
    tag = "pluginbench-native-build:" + uuid4().hex
    try:
        _inspect_image(tag, pull=False, expected_runtime=expected)
    except ValueError as error:
        if "no such image" not in str(error).lower():
            raise
    else:
        raise ValueError("Native temporary build reference already exists")
    image_id: str | None = None
    failure: BaseException | None = None
    try:
        # Tagged export loads an inspectable image; raw quiet output may be a config digest.
        _docker(
            "build",
            "--quiet",
            "--provenance=false",
            "--tag",
            tag,
            str(directory),
            timeout=1800,
            expected_runtime=expected,
        )
        description = _native_object(
            _inspect_image(tag, pull=False, expected_runtime=expected), "built native image"
        )
        candidate = description.get("Id")
        if (
            not isinstance(candidate, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", candidate) is None
        ):
            raise ValueError("Native build did not expose its immutable image ID")
        image_id = candidate
        _native_image_reference(image_id=image_id, expected=expected)
        return description
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            remaining = _inspect_image(tag, pull=False, expected_runtime=expected)
        except ValueError as error:
            if "no such image" not in str(error).lower():
                if failure is None:
                    raise
                failure.add_note("Native build cleanup inspection failed: " + str(error))
        else:
            try:
                if image_id is None or remaining.get("Id") != image_id:
                    raise ValueError(
                        "Cannot prove temporary native build ownership; reference retained: " + tag
                    )
                _native_image_reference(image_id=image_id, expected=expected)
                _docker("image", "rm", "--no-prune", tag, expected_runtime=expected)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                if failure is None:
                    raise
                failure.add_note("Native build cleanup incomplete: " + str(error))


def _native_image_reference(*, image_id: str, expected: dict[str, object]) -> str:
    """BuildKit needs a named local reference; verify its immutable target."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("Native image must have a complete immutable ID")
    reference = "pluginbench-native:sha256-" + image_id.removeprefix("sha256:")
    try:
        image = _inspect_image(reference, pull=False, expected_runtime=expected)
    except ValueError as error:
        if "no such image" not in str(error).lower():
            raise
        _docker("tag", image_id, reference, expected_runtime=expected)
        image = _inspect_image(reference, pull=False, expected_runtime=expected)
    if image.get("Id") != image_id:
        raise ValueError("Native image reference was rebound to different content")
    return reference


def _bundle_native_tests(
    *, tests: Path, grader: dict[str, str], expected: dict[str, object]
) -> dict[str, str]:
    """Move the native verifier's exact uploaded tests into its frozen image."""
    with tempfile.TemporaryDirectory(prefix="pluginbench-native-tests-") as temporary:
        context = Path(temporary)
        shutil.copytree(tests, context / "tests")
        (context / "Dockerfile").write_text("FROM " + grader["image"] + "\nCOPY tests/ /tests/\n")
        return _native_image(directory=context, image=None, expected=expected)


def _assert_native_image(identity: dict[str, object], expected: dict[str, object]) -> None:
    for reference in (identity["image_id"], identity["image"]):
        if (
            _inspect_image(str(reference), pull=False, expected_runtime=expected).get("Id")
            != identity["image_id"]
        ):
            raise ValueError("Frozen native image is unavailable or its reference was rebound")


def _native_container(
    *, project: str, trial_dir: Path, expected: dict[str, object], image_id: str | None = None
) -> dict[str, object]:
    _assert_daemon(expected)
    ids = _project_ids("container", project, expected)
    descriptions = json.loads(_docker("inspect", *ids, expected_runtime=expected)) if ids else []
    mains = [
        item
        for item in descriptions
        if _object(_object(item, "native container").get("Config"), "native config")
        .get("Labels", {})
        .get("com.docker.compose.service")
        == "main"
    ]
    if len(mains) != 1:
        raise ValueError("Native project must have exactly one main container")
    container = _native_object(mains[0], "native main")
    config = _native_object(container.get("Config"), "native container config")
    labels = _native_object(config.get("Labels"), "native container labels")
    if labels.get("com.docker.compose.project") != project or not re.fullmatch(
        r"[0-9a-f]{64}", str(container.get("Id"))
    ):
        raise ValueError("Native container ownership does not match the trial identity")
    if image_id is not None and container.get("Image") != image_id:
        raise ValueError("Native container differs from the frozen image")
    actual = _native_object(container.get("HostConfig"), "native resources")
    limits = _native_object(expected.get("resources"), "native limits")
    if (
        actual.get("NanoCpus") != int(str(limits["cpus"])) * 10**9
        or actual.get("Memory") != int(str(limits["memory_mb"])) * 1024**2
    ):
        raise ValueError("Native container resource limits differ from the frozen settings")
    if (
        actual.get("Privileged")
        or actual.get("CapAdd")
        or actual.get("PidMode") == "host"
        or actual.get("NetworkMode") == "host"
    ):
        raise ValueError("Native container has unsupported privileged or host access")
    allowed = {
        "/logs/agent": trial_dir / "agent",
        "/logs/verifier": trial_dir / "verifier",
        "/logs/artifacts": trial_dir / "artifacts/logs/artifacts",
    }
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        raise ValueError("Native container did not report its mounts")
    for item in mounts:
        mount = _native_object(item, "native mount")
        target = mount.get("Destination")
        if (
            not isinstance(target, str)
            or target not in allowed
            or mount.get("Type") != "bind"
            or Path(str(mount.get("Source"))).resolve() != allowed[target].resolve()
        ):
            raise ValueError("Native container has an unapproved host mount")
    return container


def export_native_source(
    *,
    destination: Path,
    project: str,
    trial_dir: Path,
    workdir: str,
    expected: dict[str, object],
    image_id: str | None = None,
    resume: bool = False,
    fresh_source: bool = False,
) -> dict[str, object]:
    """Freeze actual stopped source before handing it to another phase."""
    if (
        re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/?)+", workdir) is None
        or workdir in {"/root", "/home", "/tmp", "/usr", "/etc", "/var", "/logs"}
        or ".." in Path(workdir).parts
    ):
        raise ValueError("Native image must declare a safe explicit repository workdir")
    container = _native_container(
        project=project, trial_dir=trial_dir, expected=expected, image_id=image_id
    )
    container_id = str(container["Id"])
    if resume:
        _docker("stop", "--time", "10", container_id, expected_runtime=expected)
    container = _native_container(
        project=project, trial_dir=trial_dir, expected=expected, image_id=image_id
    )
    state = stopped_container_state(container.get("State"))
    destination.mkdir(parents=True, exist_ok=False)
    excluded_runtime_paths: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix="pluginbench-native-export-", dir=destination.parent
    ) as temporary:
        raw = Path(temporary)
        _docker(
            "cp", f"{container_id}:{workdir}/.", str(raw), timeout=300, expected_runtime=expected
        )

        def copy(directory: Path, target: Path) -> None:
            for path in sorted(directory.iterdir()):
                if path.name in EXCLUDED_NAMES:
                    continue
                if path.is_symlink():
                    raise ValueError(f"Native source contains a symlink: {path.relative_to(raw)}")
                if fresh_source and path.name == ".pytest_cache" and path.is_dir():
                    excluded_runtime_paths.append(path.relative_to(raw).as_posix())
                    continue
                output = target / path.name
                if path.is_dir():
                    output.mkdir()
                    copy(path, output)
                    shutil.copystat(path, output, follow_symlinks=False)
                elif path.is_file():
                    shutil.copy2(path, output)
                else:
                    raise ValueError("Native source contains a nonregular file")

        copy(raw, destination)
    snapshot = inspect_snapshot(destination)
    receipt = {
        "status": "complete",
        "stopped": True,
        "container_id": container_id,
        "project": project,
        "state": state,
        "snapshot_digest": snapshot["digest"],
        "source_metadata_digest": native_task_assets(destination),
        "capture_policy": "fresh-source-v1" if fresh_source else "exact-replay-v1",
        "excluded_runtime_paths": excluded_runtime_paths,
        "image_id": container["Image"],
        "workdir": workdir,
        "resources": {
            key: value
            for key, value in _native_object(
                expected.get("resources", {}), "native export resources"
            ).items()
            if key in {"cpus", "memory_mb"}
        },
    }
    if resume:
        _docker("start", container_id, expected_runtime=expected)
    return receipt


def install_native_hooks(trial: object, boundary: dict[str, object]) -> None:
    """Attach public Harbor hooks without changing native verifier semantics."""
    events = importlib.import_module("harbor.trial.hooks").TrialEvent
    area = Path(str(boundary["area"]))
    expected = _native_object(boundary["runtime"], "native runtime")
    image = _native_object(boundary["image"], "native image")
    # Harbor's runtime objects remain at this untyped SDK boundary.
    trial_name = getattr(getattr(trial, "config"), "trial_name")
    add_hook = getattr(trial, "add_hook")
    project = trial_name + "__env"
    trial_dir = area / "artifacts/harbor" / trial_name

    async def freeze_before(event: object) -> None:
        receipt = export_native_source(
            destination=area / "before",
            project=project,
            trial_dir=trial_dir,
            workdir=str(image["workdir"]),
            expected=expected,
            image_id=str(image["image_id"]),
            resume=True,
            fresh_source=True,
        )
        codex.write_json(area / "artifacts/before-source.json", receipt)
        if receipt["snapshot_digest"] != boundary["source_digest"]:
            raise ValueError(
                "Native starting source differs from the prepared fixture; model call refused"
            )
        metadata = boundary.get("source_metadata_digest")
        if metadata is not None and receipt.get("source_metadata_digest") != metadata:
            raise ValueError("Native starting source metadata changed; model call refused")

    async def freeze_after(event: object) -> None:
        receipt = export_native_source(
            destination=area / "workspace",
            project=project,
            trial_dir=trial_dir,
            workdir=str(image["workdir"]),
            expected=expected,
            image_id=str(image["image_id"]),
            fresh_source=True,
        )
        codex.write_json(area / "artifacts/submission-source.json", receipt)

    agent = getattr(trial, "agent")
    if boundary.get("install_only"):
        original_install = getattr(agent, "install")

        async def qualified_install(environment: object) -> None:
            await original_install(environment)
            result = await getattr(agent, "exec_as_agent")(
                environment,
                command=_native_cli_qualification(
                    str(boundary["harness"]), str(boundary["version"])
                ),
            )
            if getattr(result, "return_code") != 0:
                raise ValueError("Native CLI qualification failed during setup")
            command = (
                "set -eu; rm -rf /tmp/codex-home /tmp/codex-secrets /root/.codex /root/.claude /root/.agents /root/.config /root/.claude.json /AGENTS.md /AGENTS.override.md /CLAUDE.md; find "
                + shlex.quote(str(image["workdir"]))
            )
            command += (
                " -mindepth 1 \\( -name .git -o -name node_modules \\) -prune -o \\( "
                + " -o ".join("-name " + shlex.quote(name) for name in sorted(codex.GUIDANCE_NAMES))
                + " \\) -prune -exec rm -rf -- {} +"
            )
            result = await getattr(agent, "exec_as_root")(environment, command=command)
            if getattr(result, "return_code") != 0:
                raise ValueError("Native source sanitation failed during setup")

        setattr(agent, "install", qualified_install)
    else:
        add_hook(events.AGENT_START, freeze_before)
        add_hook(events.VERIFICATION_START, freeze_after)

        async def verify_installed(environment: object) -> None:
            execute = getattr(agent, "exec_as_agent")
            result = await execute(
                environment,
                _native_cli_qualification(str(boundary["harness"]), str(boundary["version"])),
            )
            if getattr(result, "return_code") != 0:
                raise ValueError("Frozen native CLI changed; model call refused")

        setattr(agent, "install", verify_installed)


def _native_cli_qualification(harness: str, version: str) -> str:
    prelude = _CODEX_EXEC_PREFIX.partition("codex exec")[0] if harness == "codex" else ""
    return prelude + "set -eu; " + _cli_guard(harness, version)


def _native_config(
    *,
    task_dir: Path,
    area: Path,
    owner: str,
    harness: str,
    version: str,
    limits: dict[str, object],
    timeout: int,
    model: str | None = None,
    effort: str | None = None,
    prompt: str = "",
    treatment: Treatment | None = None,
    capabilities: NativeCapabilities | None = None,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "version": version,
        "config": {"features": {"code_mode": True, "code_mode_only": True, "multi_agent": False}}
        if harness == "codex"
        else {},
    }
    if effort is not None:
        kwargs["reasoning_effort"] = effort
    if treatment is not None:
        native_treatments.admit(treatment, harness)
        if prompt:
            raise ValueError("native treatments cannot be appended to the task prompt")
        if capabilities is None or model is None or effort is None:
            raise ValueError("native treatment execution requires a fixed capability/model policy")
        kwargs["config"] = capabilities.codex_config(model=model, effort=effort)
        kwargs["dangerously_bypass_hook_trust"] = True
    return {
        "task": {"path": str(task_dir.resolve())},
        "trial_name": "pluginbench-" + owner,
        "trials_dir": str((area / "artifacts/harbor").resolve()),
        "agent": {
            "name": "codex" if harness == "codex" else "claude-code",
            "model_name": model,
            "override_timeout_sec": timeout,
            "override_setup_timeout_sec": 600,
            "kwargs": kwargs,
            "skills": [str(path) for path in treatment.skills] if treatment is not None else [],
            "mcp_servers": [],
            "extra_allowed_hosts": NATIVE_PROVIDER_HOSTS[harness],
        },
        "environment": {
            "type": "docker",
            "delete": True,
            "override_cpus": limits["cpus"],
            "override_memory_mb": limits["memory_mb"],
            "cpu_enforcement_policy": "limit",
            "memory_enforcement_policy": "limit",
            "kwargs": {"keep_containers": True},
        },
        "verifier": {"disable": False},
        "extra_instructions": [prompt] if prompt else [],
    }


def _native_worker_budget(*, task_dir: Path, agent_seconds: int = 0, prepare: bool = False) -> int:
    task = importlib.import_module("harbor.models.task.task").Task(task_dir)
    build = int(task.config.environment.build_timeout_sec)
    if prepare:
        return build + 600 + 360
    collect = sum(int(item.timeout_sec or 300) for item in task.config.verifier.collect)
    return agent_seconds + 600 + 2 * build + int(task.config.verifier.timeout_sec) + collect + 720


def _native_process(
    *,
    area: Path,
    config: dict[str, object],
    expected: dict[str, object],
    harness: str,
    timeout: int,
    install_only: bool,
    auth_file: Path | None = None,
) -> dict[str, object]:
    private = area / "private"
    artifacts = area / "artifacts"
    secrets: tuple[str, ...] = ()
    if install_only:
        private.mkdir(mode=0o700)
        home = private / "home"
        home.mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
            }
        }
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    else:
        env, secrets = codex.runtime_environment(private, harness, auth_file=auth_file)
        if harness == "codex":
            env["CODEX_AUTH_JSON_PATH"] = str(private / "codex/auth.json")
    engine = _native_object(expected["docker_engine"], "native engine")
    env.update(
        DOCKER_HOST=str(engine["endpoint"]),
        PYTHON_DOTENV_DISABLED="1",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        PYTHONDONTWRITEBYTECODE="1",
    )
    try:
        codex.write_json(
            artifacts / "docker-client.json",
            _private_docker_config(private=private, env=env, expected_runtime=expected),
        )
        codex.write_json(private / "trial.json", config)
        result = run_command(
            [
                sys.executable,
                "-m",
                "pluginbench.harbor_backend",
                str(private / "trial.json"),
                str(artifacts / "worker.json"),
            ],
            cwd=private,
            env=env,
            timeout=timeout,
        )
        (artifacts / "harbor-worker.stdout").write_text(result.stdout)
        (artifacts / "harbor-worker.stderr").write_text(result.stderr)
        if result.returncode:
            raise ValueError(f"Native Harbor worker failed (exit {result.returncode})")
        return _native_object(json.loads((artifacts / "worker.json").read_text()), "native worker")
    finally:
        shutil.rmtree(private)
        codex.scrub_artifacts(artifacts, secrets)


def _qualify_native_source(
    *,
    task_dir: Path,
    fixture: Path,
    coding_image: dict[str, str],
    grader: dict[str, str],
    expected: dict[str, object],
    destination: Path,
) -> dict[str, object]:
    """No-model proof of the reviewed grader's base inventory and initial source parity."""
    proof: dict[str, object] = {"status": "unknown", "source_policy": submissions.SOURCE_POLICY}
    try:
        contract = submissions.reviewed_contract(task_dir)
    except (OSError, ValueError) as error:
        proof["reason"] = str(error)
        return proof
    destination.mkdir(parents=True, exist_ok=False)
    owner = uuid4().hex
    project = "pluginbench-source-proof-" + owner
    limits = _native_object(expected["resources"], "source proof resources")
    container_id = ""
    try:
        base = str(contract["base_commit"])
        workdir = grader["workdir"]
        if workdir != "/app" or coding_image["workdir"] != "/app":
            raise ValueError("reviewed native grader requires the /app workdir")
        frozen_grader = _inspect_image(grader["image_id"], pull=False, expected_runtime=expected)
        if frozen_grader.get("Id") != grader["image_id"]:
            raise ValueError("native grader image identity changed during source qualification")
        startup = submissions.grader_startup(
            config=frozen_grader.get("Config"), image_id=grader["image_id"]
        )
        command = (
            "set -eu; git -c safe.directory=/app rev-parse --verify HEAD; "
            "git -c safe.directory=/app ls-tree -r -z --full-tree " + base
        )
        container_id = _docker(
            "create",
            "--label",
            "com.docker.compose.project=" + project,
            "--label",
            "com.docker.compose.service=main",
            "--label",
            _OWNER_LABEL + "=" + owner,
            "--cpus",
            str(limits["cpus"]),
            "--memory",
            str(limits["memory_mb"]) + "m",
            "--network",
            "none",
            "--workdir",
            workdir,
            "--entrypoint",
            "bash",
            grader["image_id"],
            "-c",
            command,
            expected_runtime=expected,
        ).strip()
        _docker("start", "--attach", container_id, timeout=60, expected_runtime=expected)
        if _docker("wait", container_id, expected_runtime=expected).strip() != "0":
            raise ValueError("native grader base inventory command failed")
        output = _docker("logs", container_id, expected_runtime=expected)
        (destination / "base-tree.raw").write_text(output)
        head, separator, inventory = output.partition("\n")
        if not separator or head != base:
            raise ValueError("native grader HEAD differs from reviewed verifier base")
        tree = submissions.base_tree_manifest(inventory)
        exported = export_native_source(
            destination=destination / "fixture",
            project=project,
            trial_dir=destination,
            workdir=workdir,
            expected=expected,
            image_id=grader["image_id"],
            fresh_source=True,
        )
        if exported["snapshot_digest"] != inspect_snapshot(fixture)["digest"] or exported[
            "source_metadata_digest"
        ] != native_task_assets(fixture):
            raise ValueError("native grader initial source differs from frozen coding fixture")
        payload_dir = destination / "tests"
        payload_dir.mkdir()
        _docker(
            "cp",
            container_id + ":/tests/.",
            str(payload_dir),
            expected_runtime=expected,
            timeout=60,
        )
        _native_asset_inventory(payload_dir)
        payload = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in payload_dir.iterdir()
            if path.is_file()
        }
        if (
            payload != contract["verifier_payload"]
            or any(not path.is_file() for path in payload_dir.iterdir())
            or not (payload_dir / "test.sh").stat().st_mode & 0o111
        ):
            raise ValueError("native grader image verifier differs from reviewed frozen tests")
        proof.update(
            status="complete",
            contract=contract,
            grader_head=head,
            base_tree=tree,
            base_tree_sha256=hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest(),
            fixture_digest=exported["snapshot_digest"],
            fixture_metadata_digest=exported["source_metadata_digest"],
            coding_image_id=coding_image["image_id"],
            grader_image_id=grader["image_id"],
            grader_tests_sha256=hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
            grader_startup=startup,
            export=exported,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        proof.update(status="unknown", reason=str(error))
    finally:
        try:
            proof["cleanup"] = cleanup(project=project, owner=owner, expected_runtime=expected)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            proof["cleanup"] = {"status": "incomplete", "error": str(error)}
        if _native_object(proof["cleanup"], "source proof cleanup").get("status") != "complete":
            proof.update(status="unknown", reason="native source qualification cleanup incomplete")
        codex.write_json(destination / "qualification.json", proof)
    return proof


def _prepare_native_task(
    *,
    task_dir: Path,
    destination: Path,
    harness: str,
    version: str,
    limits: dict[str, int],
    resolved: _NativePreparation | None = None,
) -> dict[str, object]:
    """Freeze native task, installed CLI, starting source and separate grader once."""
    admission = resolved.admission if resolved else validate_native_task(task_dir)
    version = _normalize_version(version)
    _cli_guard(harness, version)
    expected = (
        resolved.runtime
        if resolved
        else runtime_identity(cpus=limits["cpus"], memory_mb=limits["memory_mb"])
    )
    destination.mkdir(parents=True, exist_ok=False)
    artifacts = destination / "artifacts"
    artifacts.mkdir()
    frozen = destination / "task"
    shutil.copytree(task_dir, frozen)
    if native_task_assets(frozen) != admission["task_assets_sha256"]:
        raise ValueError("Native task changed while freezing preparation inputs")
    task_type = importlib.import_module("harbor.models.task.task").Task
    task = task_type(frozen)
    modes = importlib.import_module("harbor.models.task.verifier_mode")
    definition = modes.resolve_verifier_environment_definition(task.config, task.paths)
    base = (
        resolved.base
        if resolved
        else _native_image(
            directory=task.paths.environment_dir,
            image=task.config.environment.docker_image,
            expected=expected,
        )
    )
    grader = (
        resolved.grader
        if resolved
        else _native_image(
            directory=definition.directory, image=definition.config.docker_image, expected=expected
        )
    )
    test_delivery = "native-image"
    if not definition.bundled_tests:
        grader = _bundle_native_tests(tests=task.paths.tests_dir, grader=grader, expected=expected)
        test_delivery = "native-upload-frozen-into-image"
    task.config.environment.docker_image = base["image"]
    verifier_config = definition.config.model_copy(update={"docker_image": grader["image"]})
    task.config.verifier.environment = verifier_config
    (frozen / "task.toml").write_text(task.config.model_dump_toml())
    owner = uuid4().hex
    project = "pluginbench-" + owner + "__env"
    config = _native_config(
        task_dir=frozen,
        area=destination,
        owner=owner,
        harness=harness,
        version=version,
        limits=dict(limits),
        timeout=limits.get("setup_timeout", 600),
    )
    config["install_only"] = True
    config["pluginbench_boundary"] = {
        "area": str(destination.resolve()),
        "runtime": expected,
        "image": base,
        "harness": harness,
        "version": version,
        "install_only": True,
    }
    failure: BaseException | None = None
    receipt: dict[str, object] = {"status": "incomplete", "cleanup": {"status": "incomplete"}}
    try:
        worker = _native_process(
            area=destination,
            config=config,
            expected=expected,
            harness=harness,
            timeout=_native_worker_budget(task_dir=frozen, prepare=True),
            install_only=True,
        )
        result = _native_object(worker["result"], "native installation result")
        if worker.get("session_id") != project or result.get("exception_info") is not None:
            raise ValueError(
                f"Native agent installation did not complete: {result.get('exception_info')}"
            )
        container = _native_container(
            project=project,
            trial_dir=artifacts / "harbor" / ("pluginbench-" + owner),
            expected=expected,
            image_id=base["image_id"],
        )
        container_id = str(container["Id"])
        fixture = destination / "fixture"
        export = export_native_source(
            destination=fixture,
            project=project,
            trial_dir=artifacts / "harbor" / ("pluginbench-" + owner),
            workdir=base["workdir"],
            expected=expected,
            image_id=base["image_id"],
            fresh_source=True,
        )
        installed_id = _docker("commit", container_id, expected_runtime=expected).strip()
        installed = _native_image(
            directory=frozen / "environment", image=installed_id, expected=expected
        )
        task.config.environment.docker_image = installed["image"]
        (frozen / "task.toml").write_text(task.config.model_dump_toml())
        qualification = _qualify_native_source(
            task_dir=frozen,
            fixture=fixture,
            coding_image=installed,
            grader=grader,
            expected=expected,
            destination=artifacts / "source-qualification",
        )
        receipt["source_qualification"] = qualification
        qualification_cleanup = qualification.get("cleanup")
        if (
            isinstance(qualification_cleanup, dict)
            and qualification_cleanup.get("status") != "complete"
        ):
            raise ValueError("Native source qualification cleanup incomplete")
        receipt.update(
            status="complete",
            task_dir=str(frozen.resolve()),
            fixture_dir=str(fixture.resolve()),
            image_identity=installed,
            toolchain_identity={
                "image_id": installed["image_id"],
                "harness": harness,
                "harness_version": version,
                "platform": installed["platform"],
                "install_policy": "harbor-native-install-only-v1",
                "base_image_identity": base,
            },
            grader_image_identity=grader,
            verifier_test_delivery=test_delivery,
            trusted_tests_sha256=native_task_assets(task.paths.tests_dir),
            workdir=base["workdir"],
            source_digest=export["snapshot_digest"],
            task_assets_sha256=native_task_assets(frozen),
            original_task_assets_sha256=admission["task_assets_sha256"],
            resources=limits,
            isolation=expected,
            transport_policy={
                "name": NATIVE_TRANSPORT_POLICY,
                "provider_hosts": NATIVE_PROVIDER_HOSTS[harness],
            },
            preparation_policy=_PREPARATION_POLICY,
            source_policy=submissions.SOURCE_POLICY,
            source_qualification=qualification,
        )
    except BaseException as error:
        failure = error
        receipt["error"] = str(error)
    finally:
        try:
            receipt["cleanup"] = cleanup(project=project, owner=owner, expected_runtime=expected)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            receipt["cleanup"] = {"status": "incomplete", "error": str(error)}
            if failure is None:
                failure = error
            else:
                failure.add_note("Native preparation cleanup failed: " + str(error))
        codex.write_json(artifacts / "native-preparation.json", receipt)
    if failure is not None:
        raise failure
    return receipt


def prepare_native_task(
    *,
    task_dir: Path,
    destination: Path,
    harness: str,
    version: str,
    limits: dict[str, int],
    cache: Path | None = None,
) -> dict[str, object]:
    """Reuse verified prepared source/images across evaluations with identical inputs."""
    version = _normalize_version(version)
    _cli_guard(harness, version)
    if cache is None:
        return _prepare_native_task(
            task_dir=task_dir,
            destination=destination,
            harness=harness,
            version=version,
            limits=limits,
        )
    admission = validate_native_task(task_dir)
    expected = runtime_identity(cpus=limits["cpus"], memory_mb=limits["memory_mb"])
    task = importlib.import_module("harbor.models.task.task").Task(task_dir)
    modes = importlib.import_module("harbor.models.task.verifier_mode")
    definition = modes.resolve_verifier_environment_definition(task.config, task.paths)
    base = _native_image(
        directory=task.paths.environment_dir,
        image=task.config.environment.docker_image,
        expected=expected,
    )
    grader = _native_image(
        directory=definition.directory, image=definition.config.docker_image, expected=expected
    )
    agent_module = importlib.import_module(
        "harbor.agents.installed.codex"
        if harness == "codex"
        else "harbor.agents.installed.claude_code"
    )
    agent_type = getattr(agent_module, "Codex" if harness == "codex" else "ClaudeCode")
    inputs: dict[str, object] = {
        "task_assets_sha256": admission["task_assets_sha256"],
        "base_image": base,
        "grader_base_image": grader,
        "harness": harness,
        "version": _normalize_version(version),
        "resources": limits,
        "runtime": expected,
        "install_sha256": hashlib.sha256(
            inspect.getsource(agent_type.install).encode()
        ).hexdigest(),
        "policy": _PREPARATION_POLICY,
        "source_policy": submissions.SOURCE_POLICY,
        "transport_policy": {
            "name": NATIVE_TRANSPORT_POLICY,
            "provider_hosts": NATIVE_PROVIDER_HOSTS[harness],
        },
    }
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    if cache.is_symlink():
        raise ValueError("Native preparation cache must not follow symlinks")
    cache.mkdir(parents=True, exist_ok=True)
    lock_path = cache / (key + ".lock")
    cached = cache / key
    if lock_path.is_symlink() or cached.is_symlink():
        raise ValueError("Native preparation cache entries must not follow symlinks")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "Native preparation inputs are in use by another evaluation"
            ) from error
        if not cached.exists():
            with tempfile.TemporaryDirectory(prefix=".native-prepare-", dir=cache) as temporary:
                prepared = Path(temporary) / "prepared"
                try:
                    _prepare_native_task(
                        task_dir=task_dir,
                        destination=prepared,
                        harness=harness,
                        version=version,
                        limits=limits,
                        resolved=_NativePreparation(admission, expected, base, grader),
                    )
                except BaseException as error:
                    try:
                        destination.mkdir(parents=True, exist_ok=False)
                        if (prepared / "artifacts").exists():
                            shutil.copytree(
                                prepared / "artifacts", destination / "artifacts", symlinks=True
                            )
                        codex.write_json(
                            destination / "artifacts/preparation-failure.json",
                            {
                                "status": "failed",
                                "error": str(error),
                                "preparation_key": key,
                                "inputs": inputs,
                            },
                        )
                    except (OSError, ValueError) as evidence_error:
                        error.add_note(
                            "Could not preserve native preparation failure evidence: "
                            + str(evidence_error)
                        )
                    raise
                manifest = _native_asset_inventory(prepared)
                codex.write_json(prepared / "cache.json", {"inputs": inputs, "files": manifest})
                prepared.rename(cached)
        actual = _native_asset_inventory(cached, exclude="cache.json")
        packet = _native_object(
            json.loads((cached / "cache.json").read_text()), "native cache packet"
        )
        if packet["inputs"] != inputs:
            raise ValueError("Native preparation cache inputs changed")
        files = _native_object(packet["files"], "native cache files")
        if actual != files:
            raise ValueError("Native preparation cache evidence changed")
        receipt = _native_object(
            json.loads((cached / "artifacts/native-preparation.json").read_text()),
            "cached native preparation",
        )
        if (
            receipt["status"] != "complete"
            or _native_object(receipt["cleanup"], "cached cleanup")["status"] != "complete"
            or receipt["isolation"] != expected
            or inspect_snapshot(cached / "fixture")["digest"] != receipt["source_digest"]
            or native_task_assets(cached / "task") != receipt["task_assets_sha256"]
        ):
            raise ValueError("Native preparation cache has incomplete or invalid proof")
        for name in ("image_identity", "grader_image_identity"):
            image = _native_object(receipt[name], "cached native image")
            _assert_native_image(image, expected)
        shutil.copytree(cached, destination)
        receipt.update(
            task_dir=str((destination / "task").resolve()),
            fixture_dir=str((destination / "fixture").resolve()),
            preparation_key=key,
        )
        receipt["preparation_identity"] = {
            name: value
            for name, value in receipt.items()
            if name not in {"task_dir", "fixture_dir", "cleanup", "preparation_identity"}
        }
        codex.write_json(destination / "artifacts/native-preparation.json", receipt)
        return receipt


def _native_cleanup_projects(
    *, name: str, owner: str, expected: dict[str, object]
) -> dict[str, object]:
    projects: dict[str, object] = {}
    for project in (name + "__env", name + "__verifier__trial"):
        try:
            projects[project] = cleanup(project=project, owner=owner, expected_runtime=expected)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            projects[project] = {"status": "incomplete", "error": str(error)}
    return {
        "status": "complete"
        if all(
            _native_object(value, "native cleanup")["status"] == "complete"
            for value in projects.values()
        )
        else "incomplete",
        "projects": projects,
    }


def _native_submission(
    *, area: Path, name: str, identity: dict[str, object], expected: dict[str, object]
) -> dict[str, object]:
    receipt_path = area / "artifacts/submission-source.json"
    if receipt_path.is_file():
        receipt = _native_object(json.loads(receipt_path.read_text()), "native submission receipt")
    else:
        image = _native_object(identity["image_identity"], "native coding image")
        container = _native_container(
            project=name + "__env",
            trial_dir=area / "artifacts/harbor" / name,
            expected=expected,
            image_id=str(image["image_id"]),
        )
        _docker("stop", "--time", "10", str(container["Id"]), expected_runtime=expected)
        receipt = export_native_source(
            destination=area / "workspace",
            project=name + "__env",
            trial_dir=area / "artifacts/harbor" / name,
            workdir=str(identity["workdir"]),
            expected=expected,
            image_id=str(image["image_id"]),
            fresh_source=True,
        )
    if inspect_snapshot(area / "workspace")["digest"] != receipt["snapshot_digest"]:
        raise ValueError("Frozen native submission changed before measurement")
    if (
        receipt.get("source_metadata_digest") is not None
        and native_task_assets(area / "workspace") != receipt["source_metadata_digest"]
    ):
        raise ValueError("Frozen native submission metadata changed before measurement")
    if identity.get("preparation_policy") == _PREPARATION_POLICY and not isinstance(
        receipt.get("source_metadata_digest"), str
    ):
        raise ValueError("Frozen native submission metadata proof unavailable")
    return receipt


def _native_completion(
    *,
    area: Path,
    name: str,
    execution: dict[str, object],
    native_result: dict[str, object],
    receipt: dict[str, object],
    launch: dict[str, object],
    failure: BaseException | None,
    started: float,
) -> None:
    artifacts = area / "artifacts"
    host = str(launch["harness"]).removesuffix("-cli")
    trial_dir = artifacts / "harbor" / name
    if trial_dir.exists():
        trial_dir.rename(artifacts / "harbor/trial")
    if _native_object(receipt["cleanup"], "native cleanup")["status"] == "complete":
        try:
            execution.update(
                _native_evidence(
                    area=area,
                    host=host,
                    effort=str(launch["requested_effort"]),
                    transport=_CODEX_TRANSPORT_VERSION if host == "codex" else None,
                    include_descendants=bool(
                        _native_object(launch.get("capabilities", {}), "coding capabilities").get("subagents")
                    ),
                )
            )
        except (OSError, ValueError) as error:
            execution["evidence_error"] = str(error)
    native_error = native_result.get("exception_info")
    if native_error is not None:
        execution["error"] = native_error
    execution.update(
        native_result=native_result,
        native_verifier_result=native_result.get("verifier_result"),
        native_artifact_hashes=_native_artifact_hashes(artifacts / "harbor/trial/artifacts"),
        timed_out=isinstance(failure, subprocess.TimeoutExpired)
        or (
            isinstance(native_error, dict)
            and native_error.get("exception_type") == "AgentTimeoutError"
        ),
        elapsed_seconds=time.monotonic() - started,
        private_runtime_removed=not (area / "private").exists(),
        launch=launch,
        launch_harness_version=codex.observed_cli_version(execution.get("startup"), host=host),
    )
    if launch.get("delivery") == native_treatments.DELIVERY_POLICY:
        try:
            treatment_receipt = _native_object(
                json.loads((artifacts / "native-treatment.json").read_text()), "native treatment receipt"
            )
            if (treatment_receipt.get("status") != "verified"
                    or treatment_receipt.get("inventory") != launch.get("treatment_inventory")):
                raise ValueError("native treatment installation was not verified for this attempt")
            execution["native_treatment"] = treatment_receipt
            # Command hooks may execute subprocesses outside native session logging.
            # Preserve native observed usage, but never present it as complete total cost.
            if _native_object(treatment_receipt.get("hooks"), "native hooks").get("hooks"):
                execution["native_session_usage"] = execution.get("usage")
                execution["usage"] = None
                execution["usage_accounting"] = {
                    "status": "incomplete",
                    "reason": "command-hook subprocess usage is not independently metered",
                    "native_sessions": execution.get("usage_accounting"),
                }
        except (OSError, ValueError) as error:
            execution["treatment_error"] = str(error)
            execution["successful_native_completion"] = False
    if failure is not None or native_error is not None:
        execution["successful_native_completion"] = False
    execution["exit_code"] = 0 if execution["successful_native_completion"] else 1
    codex.write_json(artifacts / "runtime-isolation.json", receipt)
    codex.write_json(artifacts / "execution.json", execution)


def invoke_native(
    *,
    area: Path,
    task_dir: Path,
    entry: dict[str, object],
    protocol: dict[str, object],
    prompt: str,
    timeout: int,
) -> dict[str, object]:
    """Run the frozen standard task and its native separate verifier."""
    identity = _native_object(protocol.get("native_task_identity"), "prepared native task")
    host = str(entry["host"])
    treatment = Treatment.model_validate(protocol.get("treatment", {}))
    capabilities = NativeCapabilities.model_validate(protocol.get("capabilities", {}))
    native_treatments.admit(treatment, host)
    if prompt:
        raise ValueError("native coding task prompts must not contain treatment text")
    toolchain = _native_object(identity["toolchain_identity"], "native toolchain")
    if (
        toolchain["harness"] != host
        or toolchain["harness_version"] != _normalize_version(str(protocol["harness_version"]))
        or identity.get("transport_policy")
        != {"name": NATIVE_TRANSPORT_POLICY, "provider_hosts": NATIVE_PROVIDER_HOSTS.get(host)}
    ):
        raise ValueError(
            "Native task was prepared for different harness or model transport settings"
        )
    if native_task_assets(task_dir) != identity["task_assets_sha256"]:
        raise ValueError("Frozen native task assets changed")
    validate_native_task(task_dir)
    expected = _native_object(protocol["isolation"], "native runtime")
    assert_runtime(expected)
    image = _native_object(identity["image_identity"], "native coding image")
    grader = _native_object(identity["grader_image_identity"], "native grader image")
    for frozen_image in (image, grader):
        _assert_native_image(frozen_image, expected)
    artifacts = area / "artifacts"
    artifacts.mkdir(exist_ok=True)
    if (artifacts / "invocation.json").exists():
        raise ValueError("Native invocation already started; use a fresh area")
    # Existing study setup copies the fixture; hooks supply the actual before/after.
    for name in ("before", "workspace"):
        path = area / name
        if path.exists():
            if inspect_snapshot(path)["digest"] != identity["source_digest"]:
                raise ValueError("Native trial area does not match the prepared fixture")
            shutil.rmtree(path)
    owner = uuid4().hex
    name = "pluginbench-" + owner
    effort = str(protocol["reasoning"])
    version = _normalize_version(str(protocol["harness_version"]))
    models = _native_object(protocol["models"], "native models")
    limits = _native_object(expected["resources"], "native resources")
    config = _native_config(
        task_dir=task_dir,
        area=area,
        owner=owner,
        harness=host,
        version=version,
        limits=limits,
        timeout=timeout,
        model=str(models[host]),
        effort=effort,
        prompt=prompt,
        treatment=treatment,
        capabilities=capabilities,
    )
    config["pluginbench_boundary"] = {
        "treatment": treatment.model_dump(mode="json"),
        "capabilities": capabilities.model_dump(mode="json"),
        "area": str(area.resolve()),
        "runtime": expected,
        "image": image,
        "source_digest": identity["source_digest"],
        "source_metadata_digest": _native_object(
            identity.get("source_qualification", {}), "source qualification"
        ).get("fixture_metadata_digest"),
        "harness": host,
        "version": version,
    }
    launch = {
        "harness": host + "-cli",
        "requested_model": models[host],
        "requested_effort": effort,
        "harness_version": version,
        "backend": PROTOCOL,
        "delivery": native_treatments.DELIVERY_POLICY,
        "capabilities": capabilities.model_dump(mode="json"),
        "treatment_inventory": native_treatments.admit(treatment, host),
    }
    codex.write_json(
        artifacts / "invocation.json",
        {
            **launch,
            "task_assets_sha256": identity["task_assets_sha256"],
            "native_task_identity": identity,
            "timeout_seconds": timeout,
            "delivery": native_treatments.DELIVERY_POLICY,
            "capabilities": capabilities.model_dump(mode="json"),
            "treatment_inventory": native_treatments.admit(treatment, host),
        },
    )
    (artifacts / "prompt.txt").write_text(prompt)
    started = time.monotonic()
    execution: dict[str, object] = {
        **codex.parse_events("", [], host, requested_effort=effort),
        "submission_collected": False,
        "successful_native_completion": False,
    }
    receipt: dict[str, object] = {
        "export": {"status": "incomplete"},
        "cleanup": {"status": "incomplete"},
    }
    native_result: dict[str, object] = {}
    failure: BaseException | None = None
    try:
        worker = _native_process(
            area=area,
            config=config,
            expected=expected,
            harness=host,
            timeout=_native_worker_budget(task_dir=task_dir, agent_seconds=timeout),
            install_only=False,
            auth_file=Path(str(protocol["auth_file"])) if protocol.get("auth_file") else None,
        )
        if worker.get("session_id") != name + "__env":
            raise ValueError("Native worker returned a foreign project identity")
        native_result = _native_object(worker["result"], "native trial result")
        if native_result.get("verifier_result") is not None:
            verifier_container = _native_container(
                project=name + "__verifier__trial",
                trial_dir=artifacts / "harbor" / name,
                expected=expected,
                image_id=str(grader["image_id"]),
            )
            receipt["verifier"] = {
                "container_id": verifier_container["Id"],
                "image_id": verifier_container["Image"],
                "state": stopped_container_state(verifier_container.get("State")),
                "resources": verifier_container["HostConfig"],
            }
    except BaseException as error:
        failure = error
        execution["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            receipt["export"] = _native_submission(
                area=area, name=name, identity=identity, expected=expected
            )
            execution.update(
                submission_collected=True,
                submission_export_digest=_native_object(receipt["export"], "native export")[
                    "snapshot_digest"
                ],
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            execution["submission_error"] = str(error)
        receipt["cleanup"] = _native_cleanup_projects(name=name, owner=owner, expected=expected)
        if _native_object(receipt["cleanup"], "native cleanup")["status"] != "complete":
            execution["submission_collected"] = False
            cleanup_error = ValueError(
                "Native cleanup incomplete: " + json.dumps(receipt["cleanup"])
            )
            if failure is None:
                failure = cleanup_error
            else:
                failure.add_note(str(cleanup_error))
        _native_completion(
            area=area,
            name=name,
            execution=execution,
            native_result=native_result,
            receipt=receipt,
            launch=launch,
            failure=failure,
            started=started,
        )
    if failure is not None:
        raise failure
    return execution


def run_native_command(
    *,
    snapshot: Path,
    task_identity: dict[str, object],
    destination: Path,
    command: list[str],
    report_path: str,
    timeout: int,
) -> dict[str, object]:
    """Run explicitly configured measurement commands in a fresh frozen image."""
    relative = Path(report_path)
    if (
        not command
        or any(not isinstance(item, str) or not item for item in command)
        or relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or timeout < 1
    ):
        raise ValueError(
            "Native measurement requires argv, a safe relative report path and a positive timeout"
        )
    original = inspect_snapshot(snapshot)
    original_inventory = _native_asset_inventory(snapshot)
    if relative.as_posix() in original_inventory or _is_source_path(relative.as_posix()):
        raise ValueError(
            "Native measurement report must not overwrite original inputs or source paths"
        )
    expected = _native_object(task_identity["isolation"], "measurement runtime")
    assert_runtime(expected)
    image = _native_object(task_identity["image_identity"], "measurement image")
    workdir = str(task_identity["workdir"])
    limits = _native_object(expected["resources"], "measurement resources")
    owner = uuid4().hex
    project = "pluginbench-measure-" + owner
    destination.mkdir(parents=True, exist_ok=True)
    container_id = ""
    receipt: dict[str, object] = {
        "exit_code": None,
        "timed_out": False,
        "report_collected": False,
        "before_digest": original["digest"],
        "image_id": image["image_id"],
        "resources": limits,
        "command": command,
        "report_path": report_path,
        "cleanup": {"status": "incomplete"},
    }
    failure: BaseException | None = None

    def docker(*arguments: str, limit: int = _DOCKER_TIMEOUT) -> str:
        return _docker(*arguments, timeout=limit, expected_runtime=expected)

    try:
        container_id = docker(
            "create",
            "--label",
            "com.docker.compose.project=" + project,
            "--label",
            "com.docker.compose.service=main",
            "--label",
            _OWNER_LABEL + "=" + owner,
            "--cpus",
            str(limits["cpus"]),
            "--memory",
            str(limits["memory_mb"]) + "m",
            "--network",
            "none",
            "--workdir",
            workdir,
            "--entrypoint",
            "bash",
            str(image["image_id"]),
            "-c",
            "sleep infinity",
        ).strip()
        docker("start", container_id)
        excluded = " -o ".join("-name " + shlex.quote(name) for name in sorted(EXCLUDED_NAMES))
        docker(
            "exec",
            container_id,
            "bash",
            "-c",
            "set -eu; find "
            + shlex.quote(workdir)
            + " -mindepth 1 \\( "
            + excluded
            + " \\) -prune -o \\( -type f -o -type l \\) -exec rm -f -- {} +; find "
            + shlex.quote(workdir)
            + " -mindepth 1 \\( "
            + excluded
            + ' \\) -prune -o -type d -print0 | sort -zr | while IFS= read -r -d \'\' directory; do if [ -z "$(ls -A -- "$directory")" ]; then rmdir -- "$directory"; fi; done',
        )
        docker("cp", str(snapshot) + "/.", container_id + ":" + workdir + "/", limit=300)
        before = export_native_source(
            destination=destination / "before",
            project=project,
            trial_dir=destination,
            workdir=workdir,
            expected=expected,
            image_id=str(image["image_id"]),
            resume=True,
        )
        if before["snapshot_digest"] != original["digest"]:
            raise ValueError("Native measurement source differs from the selected frozen snapshot")
        if _native_asset_inventory(destination / "before") != original_inventory:
            raise ValueError(
                "Native measurement source metadata differs from the selected frozen snapshot"
            )
        # A stale report must never be accepted as output of the requested command.
        target = workdir.rstrip("/") + "/" + report_path
        docker("exec", container_id, "bash", "-c", "rm -f -- " + shlex.quote(target))
        try:
            argv = [
                "docker",
                "--host",
                str(_native_object(expected["docker_engine"], "measurement engine")["endpoint"]),
                "exec",
                "--workdir",
                workdir,
                container_id,
                *command,
            ]
            cli = _object(expected["docker_cli"], "measurement Docker CLI")
            argv[0] = _frozen_executable(cli, "CLI")["resolved_path"]
            result = run_command(
                argv,
                cwd=destination,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key not in {"DOCKER_HOST", "DOCKER_CONTEXT"}
                },
                timeout=timeout,
            )
            receipt.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired as error:
            receipt.update(
                timed_out=True,
                stdout=(error.output or b"").decode(errors="replace"),
                stderr=(error.stderr or b"").decode(errors="replace"),
            )
        docker("stop", "--time", "10", container_id)
        after = export_native_source(
            destination=destination / "after",
            project=project,
            trial_dir=destination,
            workdir=workdir,
            expected=expected,
            image_id=str(image["image_id"]),
        )
        receipt["after_digest"] = after["snapshot_digest"]
        _assert_measurement_source(
            original_inventory=original_inventory,
            after=destination / "after",
            report_path=report_path,
        )
        receipt["source_integrity"] = "verified"
        if not receipt["timed_out"]:
            report = destination / "after" / relative
            # Included-source export rejects all symlinks before reaching this read.
            if report.is_file():
                shutil.copyfile(report, destination / "report.raw")
                receipt.update(
                    report_collected=True,
                    report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                )
    except BaseException as error:
        failure = error
        receipt["error"] = str(error)
        receipt["source_integrity"] = "failed"
    finally:
        try:
            receipt["cleanup"] = cleanup(project=project, owner=owner, expected_runtime=expected)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            receipt["cleanup"] = {"status": "incomplete", "error": str(error)}
            if failure is None:
                failure = error
            else:
                failure.add_note("Native measurement cleanup failed: " + str(error))
        if (
            inspect_snapshot(snapshot)["digest"] != original["digest"]
            or _native_asset_inventory(snapshot) != original_inventory
        ):
            failure = ValueError("Frozen measurement input changed during execution")
        codex.write_json(destination / "execution.json", receipt)
    if failure is not None:
        raise failure
    return receipt


def _assert_measurement_source(
    *, original_inventory: dict[str, str], after: Path, report_path: str
) -> None:
    actual = _native_asset_inventory(after)
    for name, digest in original_inventory.items():
        if actual.get(name) != digest:
            raise ValueError(
                "Native measurement changed selected source bytes or metadata: " + name
            )
    parent = Path(report_path).parent
    report_tree = parent.as_posix() + "/"
    generated_tree = parent != Path(".") and not any(
        name.startswith(report_tree) and (_is_source_path(name) or _is_test_path(name))
        for name in original_inventory
        if not name.endswith("/")
    )
    for name in actual.keys() - original_inventory.keys():
        if (
            name != report_path
            and not name.endswith("/")
            and not (generated_tree and name.startswith(report_tree))
            and (_is_source_path(name) or _is_test_path(name))
        ):
            raise ValueError("Native measurement introduced production or test source: " + name)


def _native_artifact_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _native_regrade_task(
    *, task_dir: Path, source: Path, directory: Path, artifact_hashes: dict[str, str]
) -> tuple[Path, dict[str, object]]:
    """Remove only redundant declarations whose bytes the convention parent captured."""
    if _native_artifact_hashes(source / "artifacts") != artifact_hashes:
        raise ValueError("Recorded native artifacts differ from their frozen hashes")
    task = importlib.import_module("harbor.models.task.task").Task(task_dir)
    helpers = importlib.import_module("harbor.models.task.artifacts")
    host_path = importlib.import_module("harbor.trial.artifact_handler").artifact_host_path
    convention = helpers.convention_source_for_os(task.config.environment.os)
    declared = helpers.normalize_artifact_entries(task.config.artifacts)
    parent = next(
        entry
        for entry in helpers.with_convention_entry(declared, convention_source=convention)
        if helpers.is_convention_entry(entry, convention)
    )
    kept = []
    removed: list[dict[str, object]] = []
    for entry in declared:
        suffix = (
            PurePosixPath(entry.source).relative_to(convention)
            if (PurePosixPath(convention) in PurePosixPath(entry.source).parents)
            else None
        )
        child = host_path(source / "artifacts", entry)
        if (
            suffix is None
            or parent.exclude
            or parent.destination is not None
            or entry.exclude
            or entry.destination is not None
            or helpers.effective_artifact_service(entry)
            != helpers.effective_artifact_service(parent)
        ):
            kept.append(entry)
            continue
        if child != host_path(source / "artifacts", parent).joinpath(*suffix.parts):
            raise ValueError("Native regrade child artifact is not at its canonical parent path")
        if not child.is_file() or child.is_symlink():
            raise ValueError(
                "Native regrade redundant child artifact is missing or not a regular file"
            )
        removed.append(
            {
                "declaration": entry.model_dump(mode="json"),
                "destination": child.relative_to(source).as_posix(),
                "sha256": artifact_hashes[child.relative_to(source / "artifacts").as_posix()],
            }
        )
    proof: dict[str, object] = {
        "original_task_assets_sha256": native_task_assets(task_dir),
        "removed_declarations": removed,
    }
    derived = task_dir
    if removed:
        proof["parent"] = _native_regrade_parent(
            source=source, parent=_native_object(parent.model_dump(mode="json"), "replay parent")
        )
        proof["manifest_sha256"] = artifact_hashes["manifest.json"]
        derived = directory / "regrade-task" / task_dir.name
        shutil.copytree(task_dir, derived)
        task.config.artifacts = kept
        (derived / "task.toml").write_text(task.config.model_dump_toml())
    proof["derived_task_assets_sha256"] = native_task_assets(derived)
    return derived, proof


def _native_regrade_parent(*, source: Path, parent: dict[str, object]) -> dict[str, object]:
    helpers = importlib.import_module("harbor.models.task.artifacts")
    host_path = importlib.import_module("harbor.trial.artifact_handler").artifact_host_path
    manifest = json.loads((source / "artifacts/manifest.json").read_text())
    if not isinstance(manifest, list):
        raise ValueError("Native regrade artifact manifest must be a list")
    entry_type = importlib.import_module(
        "harbor.models.trial.artifact_manifest"
    ).ArtifactManifestEntry
    entries = [
        _native_object(
            entry_type.model_validate(entry).model_dump(mode="json"), "native artifact entry"
        )
        for entry in manifest
    ]
    artifact_type = importlib.import_module("harbor.models.task.config").ArtifactConfig
    artifact = artifact_type.model_validate(parent)
    parent_path = host_path(source / "artifacts", artifact)
    matching = [
        entry
        for entry in entries
        if (
            str(entry["source"]).rstrip("/") == artifact.source.rstrip("/")
            and (entry.get("service") or "main") == helpers.effective_artifact_service(artifact)
            and entry.get("destination") == parent_path.relative_to(source).as_posix()
        )
    ]
    if (
        len(matching) != 1
        or matching[0].get("status") != "ok"
        or matching[0].get("type") != "directory"
        or matching[0].get("exclude") != []
        or not parent_path.is_dir()
    ):
        raise ValueError("Native regrade requires one collected, unfiltered convention parent")
    return matching[0]


def regrade_native(
    *,
    artifacts: Path,
    task_dir: Path,
    identity: dict[str, object],
    directory: Path,
    artifact_hashes: dict[str, str],
) -> dict[str, object]:
    """Replay only the standard native verifier over recorded immutable artifacts."""
    source = artifacts / "harbor/trial"
    source_digest = native_task_assets(source)
    if native_task_assets(task_dir) != identity["task_assets_sha256"]:
        raise ValueError("Native regrade task assets differ from the frozen task")
    validate_native_task(task_dir)
    expected = _native_object(identity["isolation"], "native regrade runtime")
    assert_runtime(expected)
    grader = _native_object(identity["grader_image_identity"], "native regrade grader")
    _assert_native_image(grader, expected)
    toolchain = _native_object(identity["toolchain_identity"], "native regrade toolchain")
    directory.mkdir(parents=True, exist_ok=True)
    replay_task, normalization = _native_regrade_task(
        task_dir=task_dir, source=source, directory=directory, artifact_hashes=artifact_hashes
    )
    output = directory / "artifacts"
    output.mkdir(exist_ok=False)
    owner = uuid4().hex
    name = "pluginbench-" + owner
    config = _native_config(
        task_dir=replay_task,
        area=directory,
        owner=owner,
        harness=str(toolchain["harness"]),
        version=str(toolchain["harness_version"]),
        limits=_native_object(expected["resources"], "native regrade limits"),
        timeout=1,
    )
    config["source_trial"] = {"action": "regrade", "type": "local", "path": str(source.resolve())}
    receipt: dict[str, object] = {
        "source_sha256": source_digest,
        "task_assets_sha256": identity["task_assets_sha256"],
        "grader_image_identity": grader,
        "observer_identity": expected,
        "artifact_hashes": artifact_hashes,
        "task_normalization": normalization,
        "model_calls": 0,
        "cleanup": {"status": "incomplete"},
    }
    result: dict[str, object] = {}
    failure: BaseException | None = None
    try:
        worker = _native_process(
            area=directory,
            config=config,
            expected=expected,
            harness=str(toolchain["harness"]),
            timeout=_native_worker_budget(task_dir=task_dir),
            install_only=True,
        )
        if worker.get("session_id") != name + "__env":
            raise ValueError("Native regrade returned a foreign trial identity")
        result = _native_object(worker["result"], "native regrade result")
        if result.get("verifier_result") is not None:
            container = _native_container(
                project=name + "__verifier__trial",
                trial_dir=output / "harbor" / name,
                expected=expected,
                image_id=str(grader["image_id"]),
            )
            receipt["verifier"] = {
                "container_id": container["Id"],
                "image_id": container["Image"],
                "state": stopped_container_state(container.get("State")),
            }
    except BaseException as error:
        failure = error
        receipt["error"] = str(error)
    finally:
        receipt["cleanup"] = _native_cleanup_projects(name=name, owner=owner, expected=expected)
        if _native_object(receipt["cleanup"], "native regrade cleanup")["status"] != "complete":
            cleanup_error = ValueError("Native regrade cleanup incomplete")
            if failure is None:
                failure = cleanup_error
            else:
                failure.add_note(str(cleanup_error))
        if native_task_assets(source) != source_digest:
            failure = ValueError("Recorded native source trial changed during regrade")
        if native_task_assets(task_dir) != identity["task_assets_sha256"]:
            failure = ValueError("Frozen native task changed during regrade")
        if native_task_assets(replay_task) != normalization["derived_task_assets_sha256"]:
            failure = ValueError("Derived native replay task changed during regrade")
        codex.write_json(directory / "regrade-execution.json", receipt)
        codex.write_json(directory / "regrade-result.json", result)
    if failure is not None:
        raise failure
    return {
        "native_result": result,
        "verifier_result": result.get("verifier_result"),
        "exception_info": result.get("exception_info"),
        "receipt": receipt,
    }


if __name__ == "__main__":
    # Disable .env discovery before any Harbor imports and use only the parent
    # supplied private environment, not the operator's agent settings.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    payload = _object(json.loads(Path(sys.argv[1]).read_text()), "native trial config")
    codex.write_json(Path(sys.argv[2]), asyncio.run(run_trial(payload)))
