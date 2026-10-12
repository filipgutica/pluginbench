"""Harbor execution with a stopped-container submission boundary.

PluginBench freezes the installed agent image; Harbor runs the agent. The
workspace is copied only after Docker proves the container is stopped, then
the resources belonging to that trial are removed. Grading is external.
"""

from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import Any
from uuid import uuid4

from pluginbench import codex, isolation
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.processes import run_command
from pluginbench.task_data.deepswe.verify import reported_tests

HARBOR_VERSION = "0.24.0"
PROTOCOL = "harbor-docker-v1"
_OWNER_LABEL = "org.pluginbench.trial"
_BASE_IMAGE_LABEL = "org.pluginbench.base-image"
_BASE_IMAGE_ID_LABEL = "org.pluginbench.base-image-id"
_TOOLCHAIN_IMAGE_LABEL = "org.pluginbench.toolchain-image-id"
_TOOLCHAIN_POLICY = "base-runtime-frozen-support-exact-npm-cli-v1"
_TASK_PREPARATION_POLICY = "locked-native-visible-check-clean-source-v1"
_TASK_INPUT_LABEL = "org.pluginbench.task-preparation-input"
_TASK_POLICY_LABEL = "org.pluginbench.task-preparation-policy"
_BASE_TOOLS = "curl bash node npm"
_DOCKER_TIMEOUT = 60
_CODEX_TRANSPORT_VERSION = "codex-stdout-stderr-v1"
_CODEX_RUN_SHA256 = "5b14fbcc889a398f80c7b5f7e2a621efc1337ba72e748fafefbab2ad09ad86bd"
_CODEX_EXEC_PREFIX = (
    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
    "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --model "
)
_CODEX_EXEC_SUFFIX = "2>&1 </dev/null | tee /logs/agent/codex.txt"


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


def _toolchain_labels(identity: dict[str, Any]) -> dict[str, str]:
    base = _object(identity.get("base_image_identity"), "toolchain base image")
    labels = {
        _BASE_IMAGE_LABEL: base["image"],
        _BASE_IMAGE_ID_LABEL: base["image_id"],
        "org.pluginbench.harness": identity["harness"],
        "org.pluginbench.harness-version": identity["harness_version"],
        "org.pluginbench.toolchain-policy": identity["install_policy"],
    }
    if "task_preparation" in identity:
        preparation = _object(identity["task_preparation"], "task preparation")
        labels[_TASK_INPUT_LABEL] = preparation["input_key"]
        labels[_TASK_POLICY_LABEL] = preparation["validation_policy"]
    return labels


def _toolchain_tag(image_id: object) -> str:
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("Toolchain requires an immutable Docker image ID")
    return "pluginbench-toolchain:sha256-" + image_id.removeprefix("sha256:")


def _assert_toolchain_image(identity: dict[str, Any], image: dict[str, Any]) -> None:
    config = _object(image.get("Config"), "toolchain image config")
    labels = _object(config.get("Labels"), "toolchain image labels")
    if (
        image.get("Id") != identity["image_id"]
        or str(image.get("Os")) + "/" + str(image.get("Architecture")) != identity.get("platform")
        or (
            "task_preparation" in identity
            and (
                config.get("WorkingDir") != "/workspace"
                or config.get("Entrypoint") != ["/usr/bin/env"]
                or config.get("Cmd") != ["sh", "-c", "sleep infinity"]
            )
        )
        or any(labels.get(key) != value for key, value in _toolchain_labels(identity).items())
    ):
        raise ValueError("Toolchain image differs from its frozen content or install settings")


def _assert_toolchain(identity: dict[str, Any], *, expected_runtime: dict[str, object]) -> None:
    image_id = identity.get("image_id", "")
    local_tag = _toolchain_tag(image_id)
    _cli_guard(identity.get("harness", ""), identity.get("harness_version", ""))
    if identity.get("install_policy") != _TOOLCHAIN_POLICY:
        raise ValueError("Toolchain install policy differs from the supported policy")
    for reference in (image_id, local_tag):
        images = json.loads(
            _docker("image", "inspect", reference, expected_runtime=expected_runtime)
        )
        if not isinstance(images, list) or len(images) != 1:
            raise ValueError("Docker did not identify one frozen toolchain image")
        image = _object(images[0], "toolchain image")
        _assert_toolchain_image(identity, image)
    if "task_preparation" in identity:
        _assert_task_preparation(identity)


def prepare_toolchain(base_image: dict[str, str], harness: str, version: str) -> dict[str, Any]:
    """Install once before reservation, then freeze the complete toolchain image."""
    version = _normalize_version(version)
    guard = _cli_guard(harness, version)
    expected = runtime_identity()
    actual_base = image_identity(base_image["image"], expected_runtime=expected)
    if any(base_image.get(key) != value for key, value in actual_base.items()):
        raise ValueError("Base image changed before preparing its agent toolchain")
    identity: dict[str, Any] = {
        "platform": actual_base["platform"],
        "harness": harness,
        "harness_version": version,
        "install_policy": _TOOLCHAIN_POLICY,
        "base_image_identity": actual_base,
    }
    package = "@openai/codex" if harness == "codex" else "@anthropic-ai/claude-code"
    support, support_package = ("rg", "ripgrep") if harness == "codex" else ("ps", "procps")
    labels = "".join(
        f"LABEL {name}={json.dumps(value)}\n" for name, value in _toolchain_labels(identity).items()
    )
    dockerfile = (
        f"FROM --platform={actual_base['platform']} {actual_base['image']}\nUSER root\n{labels}"
        f'RUN set -eu; for tool in {_BASE_TOOLS}; do command -v "$tool" >/dev/null; done; '
        f"if ! command -v {support} >/dev/null; then "
        "command -v apt-get >/dev/null; apt-get update; "
        f"apt-get install --yes --no-install-recommends {support_package}; "
        "rm -rf /var/lib/apt/lists/*; fi; "
        ": > /tmp/pluginbench-npm-userconfig; "
        "npm --userconfig=/tmp/pluginbench-npm-userconfig --globalconfig=/dev/null "
        "--registry=https://registry.npmjs.org "
        f"install --global --no-audit --no-fund {package}@{version}; "
        f"rm -f /tmp/pluginbench-npm-userconfig; {guard}\n"
    )
    key = hashlib.sha256(dockerfile.encode()).hexdigest()
    tag = "pluginbench-toolchain:" + key
    try:
        images = json.loads(_docker("image", "inspect", tag, expected_runtime=expected))
    except ValueError as error:
        if "no such image" not in str(error).lower():
            raise
        with tempfile.TemporaryDirectory(prefix="pluginbench-toolchain-") as temporary:
            context = Path(temporary)
            (context / "Dockerfile").write_text(dockerfile)
            _docker(
                "build",
                "--platform",
                actual_base["platform"],
                "--tag",
                tag,
                str(context),
                timeout=600,
                expected_runtime=expected,
            )
        images = json.loads(_docker("image", "inspect", tag, expected_runtime=expected))
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Docker did not identify one prepared toolchain image")
    identity["image_id"] = _object(images[0], "prepared toolchain").get("Id")
    _docker(
        "tag", identity["image_id"], _toolchain_tag(identity["image_id"]), expected_runtime=expected
    )
    _assert_toolchain(identity, expected_runtime=expected)
    _assert_daemon(expected)
    return identity


def _preparation_key(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _visible_test_counts(report: Path, framework: str) -> dict[str, int]:
    rows = reported_tests(report, framework)
    return {
        "total": len(rows),
        "passed": sum(passed for _, passed, _ in rows),
        "failed": sum(not passed and detail is not None for _, passed, detail in rows),
        "pending": sum(not passed and detail is None for _, passed, detail in rows),
    }


def _assert_prepared_source(snapshot: dict[str, Any], digest: object) -> None:
    excluded = snapshot.get("excluded_paths")
    files = _object(snapshot.get("files"), "stopped preparation source files")
    if (
        not isinstance(excluded, list)
        or any(not isinstance(name, str) or Path(name).name != "node_modules" for name in excluded)
        or snapshot.get("digest") != digest
        or "sha256:" + _preparation_key(files) != digest
    ):
        raise ValueError("Stopped preparation source differs from the starting fixture")


def _assert_task_preparation(identity: dict[str, Any]) -> None:
    preparation = _object(identity["task_preparation"], "task preparation")
    inputs = {
        key: value
        for key, value in preparation.items()
        if key not in {"input_key", "receipt_path", "receipt_sha256"}
    }
    if (
        inputs.get("validation_policy") != _TASK_PREPARATION_POLICY
        or inputs.get("implementation_sha256")
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        or preparation.get("input_key") != _preparation_key(inputs)
    ):
        raise ValueError("Task preparation inputs differ from the supported validation policy")
    path = Path(preparation["receipt_path"])
    if (
        not path.is_file()
        or path.is_symlink()
        or hashlib.sha256(path.read_bytes()).hexdigest() != preparation.get("receipt_sha256")
    ):
        raise ValueError("Task preparation receipt is missing or changed")
    receipt = _object(json.loads(path.read_text()), "task preparation receipt")
    counts = _object(receipt.get("visible_tests"), "native visible test counts")
    state = _object(receipt.get("container"), "preparation stopped container")
    versions = _object(receipt.get("versions"), "native tool versions")
    manager = _object(inputs.get("recipe"), "task preparation recipe").get("package_manager")
    if not isinstance(manager, str):
        raise ValueError("Task preparation package manager is missing")
    if (
        receipt.get("status") != "validated"
        or receipt.get("inputs") != inputs
        or receipt.get("input_key") != preparation["input_key"]
        or receipt.get("image_id") != identity["image_id"]
        or receipt.get("source_inventory_digest") != inputs["fixture_digest"]
        or receipt.get("cleanup") != {"status": "complete", "residuals": []}
        or type(counts.get("passed")) is not int
        or counts["passed"] <= 0
        or counts.get("failed") != 0
        or versions.get("manager") != manager.split("@", 1)[1]
        or not isinstance(versions.get("node"), str)
        or not versions["node"].startswith("v")
        or not isinstance(versions.get("cli"), str)
        or identity["harness_version"] not in versions["cli"].split()
        or re.fullmatch(r"[0-9a-f]{64}", state.get("container_id", "")) is None
        or re.fullmatch(r"[0-9a-f]{32}", state.get("owner", "")) is None
        or state.get("resources") != inputs["resources"]
        or stopped_container_state(state.get("state"))["OOMKilled"] is not False
    ):
        raise ValueError("Task preparation receipt does not qualify this image")
    evidence_files = _object(receipt.get("evidence_files"), "task preparation evidence")
    steps = {
        "create",
        "start",
        "source-reset",
        "copy-source",
        "copy-manifest",
        "install",
        "versions",
        "visible-check",
        "copy-report",
        "source-integrity",
        "final-reset",
        "stop",
        "inspect",
        "commit",
        "cleanup",
        "cleanup-check",
        "discover",
        "owner-inspect",
        "image-qualification",
        "frozen-source-copy",
    }
    report_name = "pluginbench-visible-check." + (
        "xml" if inputs["recipe"]["framework"] == "vitest" else "json"
    )
    required = {"container-state.json", "frozen-source.json", report_name} | {
        "steps/" + step + ".json" for step in steps
    }
    if set(evidence_files) != required:
        raise ValueError("Task preparation command evidence is incomplete")
    for name, digest in evidence_files.items():
        evidence = path.parent / name
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or evidence.is_symlink()
            or not evidence.is_file()
            or hashlib.sha256(evidence.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("Task preparation command evidence is missing or changed")
        if name.startswith("steps/"):
            step = _object(json.loads(evidence.read_text()), "task preparation command")
            if step.get("returncode") != 0 or step.get("timed_out") is not False:
                raise ValueError("Task preparation command did not complete successfully")
    _assert_prepared_source(
        _object(
            json.loads((path.parent / "frozen-source.json").read_text()),
            "frozen preparation source",
        ),
        inputs["fixture_digest"],
    )
    if _visible_test_counts(path.parent / report_name, inputs["recipe"]["framework"]) != counts:
        raise ValueError("Task preparation visible test report differs from its counts")


# Verify original files before removing generated output; leave every dependency tree intact.
_PREPARATION_SOURCE_GUARD = """
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const expected = JSON.parse(fs.readFileSync('/tmp/pluginbench-source-manifest.json', 'utf8'));
const root = '/workspace';
for (const [name, digest] of Object.entries(expected)) {
  const file = path.join(root, name);
  if (!fs.existsSync(file) || !fs.lstatSync(file).isFile() ||
      'sha256:' + crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') !== digest)
    throw new Error('Starting source changed: ' + name);
}
const actual = {};
function clean(directory) {
  for (const entry of fs.readdirSync(directory, {withFileTypes:true})) {
    if (entry.name === 'node_modules') {
      for (const cache of ['.vite', '.vitest', '.cache'])
        fs.rmSync(path.join(directory, entry.name, cache), {recursive:true, force:true});
      continue;
    }
    const file = path.join(directory, entry.name), name = path.relative(root, file);
    if (entry.isDirectory()) {
      clean(file);
      if (fs.readdirSync(file).length === 0) fs.rmdirSync(file);
    } else if (!(name in expected)) fs.rmSync(file, {recursive:true, force:true});
    else actual[name] = 'sha256:' + crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  }
}
clean(root);
console.log(JSON.stringify(actual));
"""


def prepare_task_image(
    *,
    fixture: Path,
    toolchain: dict[str, Any],
    recipe: dict[str, Any],
    artifacts: Path,
    cache: Path,
    cpus: int,
    memory_mb: int,
    timeout: int,
) -> dict[str, Any]:
    """Qualify one native dependency image before reserving any paid coding work."""
    snapshot = inspect_snapshot(fixture)
    if snapshot["excluded_paths"]:
        raise ValueError(
            "Task preparation fixture must contain only source, without excluded paths"
        )
    fields = {
        "package_manager",
        "install_arguments",
        "visible_check",
        "test_cwd",
        "lock_file",
        "framework",
    }
    manager, install, check = (
        recipe.get("package_manager"),
        recipe.get("install_arguments"),
        recipe.get("visible_check"),
    )
    cwd, lock, framework = recipe.get("test_cwd"), recipe.get("lock_file"), recipe.get("framework")
    if (
        set(recipe) != fields
        or manager not in {"npm@10.9.2", "pnpm@10.20.0", "pnpm@10.28.1"}
        or not isinstance(install, list)
        or not all(isinstance(arg, str) for arg in install)
        or "--ignore-scripts" not in install
        or (manager.startswith("npm@") and (not install or install[0] != "ci"))
        or (
            manager.startswith("pnpm@")
            and (not install or install[0] != "install" or "--frozen-lockfile" not in install)
        )
        or not isinstance(check, list)
        or not all(isinstance(arg, str) for arg in check)
        or not isinstance(cwd, str)
        or Path(cwd).is_absolute()
        or ".." in Path(cwd).parts
        or not (fixture / cwd).is_dir()
        or lock not in {"package-lock.json", "pnpm-lock.yaml"}
        or not (fixture / lock).is_file()
        or framework not in {"vitest", "mocha"}
        or type(cpus) is not int
        or cpus <= 0
        or type(memory_mb) is not int
        or memory_mb < 128
        or type(timeout) is not int
        or timeout <= 0
    ):
        raise ValueError("Unsupported locked native task preparation recipe or resource budget")
    executable = "/workspace/node_modules/" + (
        "vitest/vitest.mjs" if framework == "vitest" else "mocha/bin/mocha.js"
    )
    report_name = "pluginbench-visible-check." + ("xml" if framework == "vitest" else "json")
    report_option = (
        ("--outputFile=" if framework == "vitest" else "--reporter-option=output=")
        + "/tmp/"
        + report_name
    )
    if check[:2] != ["node", executable] or report_option not in check:
        raise ValueError("Visible check must use the pinned native test executable and report path")
    expected = runtime_identity(cpus=cpus, memory_mb=memory_mb)
    _assert_daemon(expected)
    _assert_toolchain(toolchain, expected_runtime=expected)
    inputs: dict[str, Any] = {
        "fixture_digest": snapshot["digest"],
        "lock_digest": hashlib.sha256((fixture / lock).read_bytes()).hexdigest(),
        "recipe": recipe,
        "parent_image_id": toolchain["image_id"],
        "platform": toolchain["platform"],
        "resources": {"cpus": cpus, "memory_mb": memory_mb},
        "timeout_seconds": timeout,
        "validation_policy": _TASK_PREPARATION_POLICY,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    key = _preparation_key(inputs)
    tag = "pluginbench-prepared:" + key
    evidence = cache / key
    receipt_path = evidence / "receipt.json"
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        cached = _inspect_image(tag, pull=False, expected_runtime=expected)
    except ValueError as error:
        if "no such image" not in str(error).lower():
            raise
        if evidence.exists():
            raise ValueError(
                "Task preparation cache has evidence without its frozen image"
            ) from error
    else:
        result = {
            **toolchain,
            "image_id": cached["Id"],
            "task_preparation": {
                **inputs,
                "input_key": key,
                "receipt_path": str(receipt_path.resolve()),
                "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()
                if receipt_path.is_file()
                else "",
            },
        }
        _assert_task_preparation(result)
        _assert_toolchain(result, expected_runtime=expected)
        shutil.copytree(evidence, artifacts / "evidence")
        (artifacts / "preparation.json").write_text(
            json.dumps({"reused": True, "identity": result}, sort_keys=True)
        )
        return result
    evidence.mkdir(parents=True, exist_ok=False)
    owner = uuid4().hex
    name = "pluginbench-prepare-" + owner
    container_id = ""
    owned_container = False
    receipt: dict[str, Any] = {
        "input_key": key,
        "inputs": inputs,
        "cleanup": {"status": "incomplete"},
    }
    failure: BaseException | None = None

    def docker(step: str, *arguments: str, limit: int = _DOCKER_TIMEOUT) -> str:
        return _docker(
            *arguments,
            timeout=limit,
            expected_runtime=expected,
            receipt=evidence / "steps" / (step + ".json"),
        )

    try:
        container_id = docker(
            "create",
            "create",
            "--name",
            name,
            "--label",
            _OWNER_LABEL + "=" + owner,
            "--platform",
            toolchain["platform"],
            "--cpus",
            str(cpus),
            "--memory",
            str(memory_mb) + "m",
            "--env",
            "HOME=/pluginbench-home",
            "--env",
            "COREPACK_HOME=/pluginbench-corepack",
            "--env",
            "npm_config_userconfig=/pluginbench-home/.npmrc",
            "--env",
            "npm_config_globalconfig=/dev/null",
            "--workdir",
            "/workspace",
            "--entrypoint",
            "bash",
            toolchain["image_id"],
            "-c",
            "sleep infinity",
        ).strip()
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise ValueError("Docker did not report a complete preparation container ID")
        docker("start", "start", container_id)
        base = _object(toolchain.get("base_image_identity"), "toolchain base image")
        reset = f"rm -rf {shlex.quote(base['workdir'])} /workspace /pluginbench-home /pluginbench-corepack; "
        reset += "mkdir /workspace /pluginbench-home /pluginbench-corepack; rm -rf /root/.codex /root/.claude /root/.agents /root/.config /root/.npm /root/.npmrc /root/.netrc /root/.ssh /AGENTS.md /AGENTS.override.md /CLAUDE.md /.agents /.claude /.codex; "
        reset += "find /home \\( -name .agents -o -name .claude -o -name .codex \\) -prune -exec rm -rf {} +"
        docker("source-reset", "exec", container_id, "bash", "-c", "set -eu; " + reset)
        docker("copy-source", "cp", str(fixture) + "/.", container_id + ":/workspace/")
        with tempfile.TemporaryDirectory(prefix="pluginbench-source-manifest-") as temporary:
            manifest = Path(temporary) / "manifest.json"
            manifest.write_text(json.dumps(snapshot["files"]))
            docker(
                "copy-manifest",
                "cp",
                str(manifest),
                container_id + ":/tmp/pluginbench-source-manifest.json",
            )
        docker("install", "exec", container_id, "corepack", manager, *install, limit=timeout)
        receipt["versions"] = json.loads(
            docker(
                "versions",
                "exec",
                container_id,
                "bash",
                "-c",
                "set -eu; "
                + _cli_guard(toolchain["harness"], toolchain["harness_version"])
                + "; "
                + 'printf \'{"node":"%s","manager":"%s","cli":"%s"}\\n\' '
                + f'"$(node --version)" "$(corepack {manager} --version)" "$({toolchain["harness"]} --version)"',
            )
        )
        if receipt["versions"]["manager"] != manager.split("@", 1)[1]:
            raise ValueError("Native package manager version differs from the pinned recipe")
        docker(
            "visible-check",
            "exec",
            "--workdir",
            "/workspace" + ("/" + cwd if cwd != "." else ""),
            container_id,
            *check,
            limit=timeout,
        )
        docker(
            "copy-report", "cp", container_id + ":/tmp/" + report_name, str(evidence / report_name)
        )
        counts = _visible_test_counts(evidence / report_name, framework)
        receipt["visible_tests"] = counts
        if not counts["passed"] or counts["failed"]:
            raise ValueError("Native visible check has no passing tests or has failures")
        inventory = json.loads(
            docker(
                "source-integrity", "exec", container_id, "node", "-e", _PREPARATION_SOURCE_GUARD
            )
        )
        if inventory != snapshot["files"]:
            raise ValueError("Native preparation source inventory differs from the fixture")
        receipt["live_source_inventory_digest"] = snapshot["digest"]
        docker(
            "final-reset",
            "exec",
            container_id,
            "bash",
            "-c",
            "set -eu; rm -rf /pluginbench-home /tmp/* /tmp/.[!.]* /tmp/..?* /root/.npm; mkdir /pluginbench-home; test -x /usr/bin/env; "
            + _cli_guard(toolchain["harness"], toolchain["harness_version"]),
        )
    except BaseException as error:
        failure = error
    finally:
        try:
            _assert_daemon(expected)
            ids = docker("discover", "ps", "-aq", "--filter", "name=^/" + name + "$").split()
            if len(ids) > 1:
                raise ValueError("Preparation name identifies multiple containers")
            if ids:
                descriptions = json.loads(docker("owner-inspect", "inspect", ids[0]))
                if not isinstance(descriptions, list) or len(descriptions) != 1:
                    raise ValueError("Preparation name did not identify one container")
                description = _object(descriptions[0], "preparation container")
                config = _object(description.get("Config"), "preparation container config")
                discovered_id = description.get("Id")
                if (
                    description.get("Name") != "/" + name
                    or not isinstance(discovered_id, str)
                    or re.fullmatch(r"[0-9a-f]{64}", discovered_id) is None
                    or _object(config.get("Labels"), "preparation container labels").get(
                        _OWNER_LABEL
                    )
                    != owner
                    or (
                        re.fullmatch(r"[0-9a-f]{64}", container_id) is not None
                        and discovered_id != container_id
                    )
                ):
                    raise ValueError("Preparation container ownership changed")
                container_id = discovered_id
                owned_container = True
                receipt["container"] = {
                    "container_id": container_id,
                    "owner": owner,
                    "resources": inputs["resources"],
                    "state": None,
                }
            elif failure is None:
                raise ValueError("Preparation container disappeared before qualification")
            else:
                receipt["cleanup"] = {"status": "complete", "residuals": []}
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note("Preparation ownership recovery failed: " + str(error))
        if owned_container:
            try:
                docker("stop", "stop", "--time", "10", container_id)
            except BaseException as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note("Preparation stop failed: " + str(error))
            try:
                descriptions = json.loads(docker("inspect", "inspect", container_id))
                description = _object(descriptions[0], "preparation container")
                resources = _object(
                    description.get("HostConfig"), "preparation container resources"
                )
                if (
                    description.get("Mounts") != []
                    or resources.get("NanoCpus") != cpus * 10**9
                    or resources.get("Memory") != memory_mb * 1024**2
                ):
                    raise ValueError("Preparation container isolation settings changed")
                state = stopped_container_state(description.get("State"))
                receipt["container"]["state"] = state
                if failure is None:
                    if state["OOMKilled"]:
                        raise ValueError("Native preparation container was OOM killed")
                    with tempfile.TemporaryDirectory(
                        prefix="pluginbench-prepared-snapshot-"
                    ) as temporary:
                        exported = Path(temporary) / "workspace"
                        exported.mkdir()
                        docker(
                            "frozen-source-copy",
                            "cp",
                            container_id + ":/workspace/.",
                            str(exported),
                            limit=timeout,
                        )
                        frozen_source = inspect_snapshot(exported)
                    (evidence / "frozen-source.json").write_text(
                        json.dumps(frozen_source, sort_keys=True)
                    )
                    _assert_prepared_source(frozen_source, snapshot["digest"])
                    if frozen_source["files"] != snapshot["files"]:
                        raise ValueError(
                            "Stopped preparation source files differ from the starting fixture"
                        )
                    receipt["source_inventory_digest"] = frozen_source["digest"]
                    identity = {**toolchain, "task_preparation": {**inputs, "input_key": key}}
                    changes = [
                        item
                        for label, value in _toolchain_labels(identity).items()
                        for item in ("--change", "LABEL " + label + "=" + json.dumps(value))
                    ]
                    changes += [
                        "--change",
                        "LABEL " + _OWNER_LABEL + '=""',
                        "--change",
                        "WORKDIR /workspace",
                        "--change",
                        'ENTRYPOINT ["/usr/bin/env"]',
                        "--change",
                        'CMD ["sh", "-c", "sleep infinity"]',
                    ]
                    receipt["image_id"] = docker("commit", "commit", *changes, container_id).strip()
                    _toolchain_tag(receipt["image_id"])
                    committed_images = json.loads(
                        docker("image-qualification", "image", "inspect", receipt["image_id"])
                    )
                    if not isinstance(committed_images, list) or len(committed_images) != 1:
                        raise ValueError("Docker did not identify one committed task image")
                    _assert_toolchain_image(
                        {**identity, "image_id": receipt["image_id"]},
                        _object(committed_images[0], "committed task image"),
                    )
            except BaseException as error:
                receipt["container"]["state_error"] = str(error)
                if failure is None:
                    failure = error
                else:
                    failure.add_note("Preparation finalization failed: " + str(error))
            finally:
                (evidence / "container-state.json").write_text(
                    json.dumps(receipt["container"], sort_keys=True)
                )
            try:
                _assert_daemon(expected)
                docker("cleanup", "rm", "--force", "--volumes", container_id)
                remaining = docker(
                    "cleanup-check", "ps", "-aq", "--filter", "label=" + _OWNER_LABEL + "=" + owner
                ).split()
                if remaining:
                    raise ValueError("Owned preparation container cleanup is incomplete")
                receipt["cleanup"] = {"status": "complete", "residuals": []}
            except BaseException as error:
                if failure is None:
                    failure = error
                else:
                    failure.add_note("Preparation cleanup failed: " + str(error))
        committed_image = receipt.get("image_id")
        if failure is not None and committed_image is not None:
            try:
                _toolchain_tag(committed_image)
                docker("image-cleanup", "image", "rm", committed_image)
                try:
                    docker("image-cleanup-check", "image", "inspect", committed_image)
                except ValueError as error:
                    if "no such image" not in str(error).lower():
                        raise
                else:
                    raise ValueError("Owned preparation image cleanup is incomplete")
            except BaseException as error:
                receipt["cleanup"] = {
                    "status": "incomplete",
                    "residuals": [*receipt["cleanup"].get("residuals", []), committed_image],
                }
                failure.add_note("Preparation image cleanup failed: " + str(error))
        receipt["status"] = "failed" if failure else "validated"
        receipt["error"] = str(failure) if failure else None
        receipt["evidence_files"] = {
            path.relative_to(evidence).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in evidence.rglob("*")
            if path.is_file()
        }
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))
        shutil.copytree(evidence, artifacts / "evidence")
        if "container" in receipt:
            (artifacts / "container-state.json").write_text(
                json.dumps(receipt["container"], sort_keys=True)
            )
        (artifacts / "preparation.json").write_text(json.dumps(receipt, sort_keys=True))
    if failure is not None:
        raise failure
    result = {
        **toolchain,
        "image_id": receipt["image_id"],
        "task_preparation": {
            **inputs,
            "input_key": key,
            "receipt_path": str(receipt_path.resolve()),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        },
    }
    _assert_task_preparation(result)
    _assert_daemon(expected)
    _docker("tag", result["image_id"], tag, expected_runtime=expected)
    _docker(
        "tag", result["image_id"], _toolchain_tag(result["image_id"]), expected_runtime=expected
    )
    _assert_toolchain(result, expected_runtime=expected)
    _assert_daemon(expected)
    return result


def _stage_task(
    *,
    area: Path,
    prompt: str,
    image: dict[str, str],
    owner: str,
    timeout: int,
    cpus: int,
    memory_mb: int,
    build_timeout: int = 300,
    toolchain: dict[str, Any] | None = None,
) -> Path:
    prepared = toolchain is not None and "task_preparation" in toolchain
    if prepared:
        assert toolchain is not None
        preparation = _object(toolchain["task_preparation"], "task preparation")
        if inspect_snapshot(area / "workspace")["digest"] != preparation.get("fixture_digest"):
            raise ValueError("Prepared image fixture differs from the starting task fixture")
    task = area / "harbor-task"
    environment = task / "environment"
    environment.mkdir(parents=True)
    if not prepared:
        shutil.copytree(area / "workspace", environment / "fixture")
    workdir = image["workdir"]
    move = (
        f"rm -rf /workspace; mv {workdir} /workspace; "
        if not prepared and workdir != "/workspace"
        else ""
    )
    source = _toolchain_tag(toolchain["image_id"]) if toolchain is not None else image["image"]
    toolchain_label = (
        f"LABEL {_TOOLCHAIN_IMAGE_LABEL}={json.dumps(toolchain['image_id'])}\n"
        if toolchain is not None
        else ""
    )
    guard = (
        "RUN "
        + json.dumps(
            [
                "bash",
                "-c",
                "set -eu; " + _cli_guard(toolchain["harness"], toolchain["harness_version"]),
            ]
        )
        + "\n"
        if toolchain is not None
        else ""
    )
    source_reset = (
        ""
        if prepared
        else f"RUN set -eu; {move}"
        "find /workspace -mindepth 1 -maxdepth 1 ! -name node_modules -exec rm -rf {} +; "
        "rm -rf /root/.agents /root/.claude /root/.codex /root/.config "
        "/AGENTS.md /AGENTS.override.md /CLAUDE.md /.agents /.claude /.codex; "
        "find /home \\( -name .agents -o -name .claude -o -name .codex \\) "
        "-prune -exec rm -rf {} +; "
        "rm -rf /pluginbench-home; mkdir /pluginbench-home\n"
        "COPY fixture/ /workspace/\n"
    )
    # Prepared images already contain the exact clean source and all nested dependencies.
    (environment / "Dockerfile").write_text(
        f"FROM --platform={image['platform']} {source}\nUSER root\n"
        f"LABEL {_OWNER_LABEL}={json.dumps(owner)}\n"
        f"LABEL {_BASE_IMAGE_LABEL}={json.dumps(image['image'])}\n"
        f"LABEL {_BASE_IMAGE_ID_LABEL}={json.dumps(image['image_id'])}\n"
        f"{toolchain_label}"
        f"{source_reset}WORKDIR /workspace\n"
        + ("ENV COREPACK_HOME=/pluginbench-corepack\n" if prepared else "")
        + "ENV HOME=/pluginbench-home\n"
        + f"{guard}"
    )
    (task / "instruction.md").write_text(prompt)
    (task / "task.toml").write_text(
        'version = "1.0"\n'
        f"[agent]\ntimeout_sec = {timeout}\n"
        f"[environment]\nbuild_timeout_sec = {build_timeout}\ncpus = {cpus}\nmemory_mb = {memory_mb}\n"
        'network_mode = "public"\n'
    )
    return task


async def run_trial(config: dict[str, Any]) -> dict[str, Any]:
    """Run one native trial, without queue retries or Harbor resume behavior."""
    # Harbor's wheel is untyped. Keep its dynamic API at this single boundary,
    # without making old report readers import its execution dependencies.
    agent = _object(config.get("agent"), "native agent configuration")
    separate_codex = agent.get("name") == "codex"
    if separate_codex:
        _assert_codex_transport_source()
    trial_config = importlib.import_module("harbor.models.trial.config").TrialConfig
    trial_type = importlib.import_module("harbor.trial.trial").Trial
    trial = await trial_type.create(trial_config.model_validate(config))
    if separate_codex:
        original_exec = trial.agent.exec_as_agent

        async def separated_exec(
            environment: object,
            command: str,
            env: dict[str, str] | None = None,
            cwd: str | None = None,
            timeout_sec: int | None = None,
        ) -> object:
            if "codex exec" in command:
                if not command.startswith(_CODEX_EXEC_PREFIX) or not command.endswith(
                    _CODEX_EXEC_SUFFIX
                ):
                    raise ValueError(
                        "Native Codex invocation differs from the pinned transport contract"
                    )
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


def collect_submission(
    *,
    area: Path,
    project: str,
    owner: str,
    expected_runtime: dict[str, object],
    base_image: dict[str, str],
    toolchain: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Copy only from the one stopped main container; no live snapshot is accepted."""
    _assert_daemon(expected_runtime)
    ids = _project_ids("container", project, expected_runtime)
    if len(ids) != 1:
        raise ValueError(f"Expected one owned main container, found {len(ids)}")
    descriptions = json.loads(_docker("inspect", ids[0], expected_runtime=expected_runtime))
    container = _object(descriptions[0], "owned container")
    config = _object(container.get("Config"), "container config")
    labels = _object(config.get("Labels"), "container labels")
    state = stopped_container_state(container.get("State"))
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "main"
        or labels.get(_OWNER_LABEL) != owner
    ):
        raise ValueError("Submission container does not belong to the expected main service")
    container_id = container.get("Id")
    if not isinstance(container_id, str) or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise ValueError("Docker did not report the stopped container ID")
    image_id = container.get("Image")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("Container did not report its immutable derived image ID")
    derived_images = json.loads(
        _docker("image", "inspect", image_id, expected_runtime=expected_runtime)
    )
    if not isinstance(derived_images, list) or len(derived_images) != 1:
        raise ValueError("Docker did not identify the owned derived image")
    derived = _object(derived_images[0], "derived image")
    derived_config = _object(derived.get("Config"), "derived image config")
    derived_labels = _object(derived_config.get("Labels"), "derived image labels")
    required_labels = {
        _OWNER_LABEL: owner,
        _BASE_IMAGE_LABEL: base_image["image"],
        _BASE_IMAGE_ID_LABEL: base_image["image_id"],
    }
    if toolchain is not None:
        required_labels.update(_toolchain_labels(toolchain))
        required_labels[_TOOLCHAIN_IMAGE_LABEL] = toolchain["image_id"]
    if any(
        labels.get(key) != value or derived_labels.get(key) != value
        for key, value in required_labels.items()
    ):
        raise ValueError(
            "Derived submission image does not match its frozen base image or toolchain"
        )
    limits = _object(expected_runtime.get("resources"), "frozen container limits")
    actual = _object(container.get("HostConfig"), "container runtime limits")
    if (
        actual.get("NanoCpus") != limits["cpus"] * 10**9
        or actual.get("Memory") != limits["memory_mb"] * 1024**2
    ):
        raise ValueError("Container's actual CPU or memory limit differs from the frozen settings")
    if any(
        mount.get("Destination", "").startswith("/workspace")
        for mount in container.get("Mounts", [])
    ):
        raise ValueError("Submission workspace must not be a shared mount")
    proof = {
        "container_id": container_id,
        "owner": owner,
        "state": state,
        "resources": {"cpus": actual["NanoCpus"] / 10**9, "memory_mb": actual["Memory"] / 1024**2},
        "derived_image_id": image_id,
    }
    artifacts = area / "artifacts"
    artifacts.mkdir(exist_ok=True)
    codex.write_json(artifacts / "container-state.json", proof)
    exported = artifacts / "container-submission"
    exported.mkdir()
    _docker(
        "cp",
        f"{ids[0]}:/workspace/.",
        str(exported),
        timeout=300,
        expected_runtime=expected_runtime,
    )
    _assert_daemon(expected_runtime)
    try:
        snapshot = inspect_snapshot(exported)
    except ValueError:
        shutil.move(exported, area / "artifacts/unsupported-submission")
        raise
    shutil.rmtree(area / "workspace")
    exported.rename(area / "workspace")
    return {
        "status": "complete",
        "stopped": True,
        "snapshot_digest": snapshot["digest"],
        "resources": proof["resources"],
        "derived_image_id": proof["derived_image_id"],
        "base_image_identity": base_image,
        **({"toolchain_identity": toolchain} if toolchain is not None else {}),
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
) -> dict[str, Any]:
    codex.validate_artifacts(area / "artifacts")
    agent = area / "artifacts/harbor/trial/agent"
    raw = agent / ("codex.txt" if host == "codex" else "claude-code.txt")

    stdout = raw.read_text(errors="replace") if raw.is_file() else ""
    sessions = agent / "sessions"
    traces = [path.read_text(errors="replace") for path in sorted(sessions.rglob("*.jsonl"))]
    artifacts = area / "artifacts"
    if host == "codex" and transport == _CODEX_TRANSPORT_VERSION:
        stderr = agent / "codex.stderr.log"
        if not raw.is_file() or not stderr.is_file():
            raise ValueError("Native Codex separate stdout/stderr capture is incomplete")
        shutil.copyfile(stderr, artifacts / "codex.stderr.log")
    elif host == "codex":
        if transport not in {None, "merged-stderr-v1"}:
            raise ValueError("Unsupported frozen Codex transport version")
        # Legacy Harbor merges stderr into codex.txt. Separate only observed CLI prelude
        # diagnostics; unexpected text and diagnostics inside JSONL still fail parsing.
        diagnostics = {
            "WARNING: proceeding, even though we could not create PATH aliases: Refusing to create "
            'helper binaries under temporary dir "/tmp" '
            '(codex_home: AbsolutePathBuf("/tmp/codex-home"))',
            "Reading additional input from stdin...",
        }
        lines = stdout.splitlines(keepends=True)
        prefix = 0
        while prefix < len(lines) and lines[prefix].rstrip("\r\n") in diagnostics:
            prefix += 1
        (artifacts / "codex-prelude.log").write_text("".join(lines[:prefix]))
        stdout = "".join(lines[prefix:])
    (artifacts / "stdout.jsonl").write_text(stdout)
    for index, trace in enumerate(traces):
        (artifacts / f"host-trace-{index}.jsonl").write_text(trace)
    return codex.parse_events(stdout, traces, host, requested_effort=effort)


def invoke(
    *, area: Path, entry: dict[str, Any], protocol: dict[str, Any], prompt: str, timeout: int
) -> dict[str, Any]:
    artifacts = area / "artifacts"
    artifacts.mkdir(exist_ok=True)
    if (artifacts / "invocation.json").exists():
        raise ValueError("Harbor invocation already started; use a fresh trial area")
    host, effort = entry["host"], protocol["reasoning"]
    version = _normalize_version(protocol["harness_version"])
    if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) is None or host not in {
        "codex",
        "claude",
    }:
        raise ValueError("Harbor requires an exact supported native CLI version")
    expected = _object(protocol.get("isolation"), "frozen Harbor runtime")
    assert_runtime(expected)
    image = image_identity(protocol["agent_image"], expected_runtime=expected)
    frozen_image = _object(protocol.get("image_identity"), "frozen base image")
    if any(value != frozen_image.get(key) for key, value in image.items()):
        raise ValueError("Agent image changed since evaluation preparation")
    toolchain = _object(protocol.get("toolchain_identity"), "frozen agent toolchain")
    if (
        toolchain.get("base_image_identity") != image
        or toolchain.get("platform") != image["platform"]
        or toolchain.get("harness") != host
        or toolchain.get("harness_version") != version
    ):
        raise ValueError("Toolchain does not match the frozen base image and agent settings")
    _assert_toolchain(toolchain, expected_runtime=expected)
    codex.reject_guidance(area / "workspace", include_ancestors=False)
    owner = uuid4().hex
    build_timeout = protocol.get("build_timeout_seconds", 300)
    if type(build_timeout) is not int or build_timeout <= 0:
        raise ValueError("Build timeout must be a positive whole number")
    project = f"pluginbench-{owner}__env"
    task = _stage_task(
        area=area,
        prompt=prompt,
        image=image,
        owner=owner,
        timeout=timeout,
        toolchain=toolchain,
        cpus=expected["resources"]["cpus"],
        memory_mb=expected["resources"]["memory_mb"],
        build_timeout=build_timeout,
    )
    private = area / "private"
    started = time.monotonic()
    secrets: tuple[str, ...] = ()
    receipt: dict[str, Any] = {
        "container": expected,
        "export": {"status": "incomplete"},
        "cleanup": {"status": "incomplete"},
    }
    execution: dict[str, Any] = {
        **codex.parse_events("", [], host, requested_effort=effort),
        "submission_collected": False,
        "successful_native_completion": False,
    }
    failure: BaseException | None = None
    native_result: dict[str, Any] = {}
    launch = {
        "harness": host + "-cli",
        "requested_model": protocol["models"][host],
        "requested_effort": effort,
        "harness_version": version,
        "backend": PROTOCOL,
    }
    codex.write_json(
        artifacts / "invocation.json",
        {
            **launch,
            "timeout_seconds": timeout,
            "build_timeout_seconds": build_timeout,
            "image_identity": image,
            "toolchain_identity": toolchain,
        },
    )
    (artifacts / "prompt.txt").write_text(prompt)
    try:
        env, secrets = codex.runtime_environment(
            private,
            host,
            auth_file=Path(protocol["auth_file"]) if protocol.get("auth_file") else None,
        )
        engine = _object(expected["docker_engine"], "Docker engine")
        env.update(
            DOCKER_HOST=engine["endpoint"],
            DOCKER_DEFAULT_PLATFORM=image["platform"],
            PYTHON_DOTENV_DISABLED="1",
            PYTHONPATH=str(Path(__file__).resolve().parents[1]),
            PYTHONDONTWRITEBYTECODE="1",
        )
        if host == "codex":
            env["CODEX_AUTH_JSON_PATH"] = str(private / "codex/auth.json")
        docker_client = _private_docker_config(private=private, env=env, expected_runtime=expected)
        codex.write_json(artifacts / "docker-client.json", docker_client)
        config = {
            "task": {"path": str(task.resolve())},
            "trial_name": f"pluginbench-{owner}",
            "trials_dir": str((artifacts / "harbor").resolve()),
            "agent": {
                "name": "codex" if host == "codex" else "claude-code",
                "model_name": protocol["models"][host],
                "override_timeout_sec": timeout,
                "override_setup_timeout_sec": 300,
                "kwargs": {"version": version, "reasoning_effort": effort, "config": {}},
                "skills": [],
                "mcp_servers": [],
            },
            "environment": {
                "type": "docker",
                "delete": True,
                "override_cpus": expected["resources"]["cpus"],
                "override_memory_mb": expected["resources"]["memory_mb"],
                "cpu_enforcement_policy": "limit",
                "memory_enforcement_policy": "limit",
                "kwargs": {"keep_containers": True},
            },
            "verifier": {"disable": True},
        }
        # A fixed trial directory keeps the native logs easy to locate; the
        # container's compose project still has a fresh per-invocation identity.
        config_path = private / "trial.json"
        codex.write_json(config_path, config)
        result = run_command(
            [
                sys.executable,
                "-m",
                "pluginbench.harbor_backend",
                str(config_path),
                str(artifacts / "worker.json"),
            ],
            cwd=private,
            env=env,
            timeout=timeout + build_timeout + 360,
        )
        (artifacts / "harbor-worker.stdout").write_text(result.stdout)
        (artifacts / "harbor-worker.stderr").write_text(result.stderr)
        if result.returncode:
            raise ValueError(f"Harbor worker failed (exit {result.returncode})")
        worker = _object(
            json.loads((artifacts / "worker.json").read_text()), "Harbor worker result"
        )
        session = worker.get("session_id")
        if session != f"pluginbench-{owner}__env":
            raise ValueError("Harbor returned an unexpected environment identity")
        native_result = _object(worker.get("result"), "Harbor result")
        receipt["export"] = collect_submission(
            area=area,
            project=project,
            owner=owner,
            expected_runtime=expected,
            base_image=image,
            toolchain=toolchain,
        )
        execution.update(
            submission_collected=True,
            submission_export_digest=receipt["export"]["snapshot_digest"],
            timed_out=False,
        )
    except BaseException as error:
        failure = error
        execution["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            receipt["cleanup"] = cleanup(project=project, owner=owner, expected_runtime=expected)
        except (
            OSError,
            ValueError,
            isolation.RuntimeDriftError,
            subprocess.TimeoutExpired,
        ) as error:
            receipt["cleanup"] = {"status": "incomplete", "error": str(error)}
            execution.update(submission_collected=False, successful_native_completion=False)
            if failure is not None:
                failure.add_note(f"Harbor cleanup failed: {error}")
            else:
                failure = error
        # Read agent-controlled logs only after every owned writer is gone.
        try:
            native_dir = artifacts / "harbor" / f"pluginbench-{owner}"
            if native_dir.exists():
                native_dir.rename(artifacts / "harbor/trial")
            if receipt["cleanup"]["status"] == "complete":
                transport = (
                    _object(expected["codex_transport"], "frozen Codex transport")["version"]
                    if "codex_transport" in expected
                    else None
                )
                execution.update(
                    _native_evidence(area=area, host=host, effort=effort, transport=transport)
                )
        except (OSError, ValueError) as error:
            execution["error"] = str(error)
            if failure is None:
                failure = error
        native_error = native_result.get("exception_info")
        if failure is not None or native_error is not None:
            execution["successful_native_completion"] = False
        if native_error is not None:
            execution["error"] = native_error
        execution["exit_code"] = (
            (0 if execution["successful_native_completion"] else 1) if native_result else None
        )
        execution["timed_out"] = isinstance(failure, subprocess.TimeoutExpired) or (
            isinstance(native_error, dict)
            and native_error.get("exception_type") == "AgentTimeoutError"
        )
        try:
            for path in (private, task):
                try:
                    if path.exists():
                        shutil.rmtree(path)
                except OSError as error:
                    receipt["cleanup"].update(status="incomplete", error=str(error))
                    execution.update(submission_collected=False, successful_native_completion=False)
                    if failure is None:
                        failure = error
                    else:
                        failure.add_note(f"Owned temporary directory cleanup failed: {error}")
        finally:
            codex.scrub_artifacts(artifacts, secrets)
            execution.update(
                elapsed_seconds=time.monotonic() - started,
                private_runtime_removed=not private.exists(),
                launch=launch,
                launch_harness_version=codex.observed_cli_version(
                    execution.get("startup"), host=host
                ),
            )
            codex.write_json(artifacts / "runtime-isolation.json", receipt)
            codex.write_json(artifacts / "execution.json", execution)
        if failure is not None:
            raise failure
    return execution


if __name__ == "__main__":
    # Disable .env discovery before any Harbor imports and use only the parent
    # supplied private environment, not the operator's agent settings.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    payload = _object(json.loads(Path(sys.argv[1]).read_text()), "native trial config")
    codex.write_json(Path(sys.argv[2]), asyncio.run(run_trial(payload)))
