"""Optional upstream Pier execution, adapted to PluginBench's attempt contract."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import os
import signal
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MethodType
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pluginbench import codex
from pluginbench.assessment_metrics import inspect_snapshot

PIER_VERSION = "0.3.1"


class PierTaskEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    path: Path


class PierCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    source: dict[str, str]
    tasks: list[PierTaskEntry] = Field(min_length=1)


@dataclass(frozen=True)
class PierTask:
    task_id: str
    prompt: str
    directory: Path
    base_commit: str
    digest: str
    provenance: dict[str, str]


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _tree_digest(root: Path) -> str:
    rows = []
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ValueError(f"Pier task contains an unsupported symlink: {file}")
        if file.is_file():
            rows.append(
                (
                    file.relative_to(root).as_posix(),
                    file.stat().st_mode & 0o777,
                    hashlib.sha256(file.read_bytes()).hexdigest(),
                )
            )
    return "sha256:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _pinned_task(directory: Path) -> tuple[str, dict[str, str]]:
    data = tomllib.loads((directory / "task.toml").read_text())
    metadata = data["metadata"]
    base = metadata["base_commit_hash"]
    if not isinstance(base, str) or re.fullmatch(r"[a-f0-9]{40}", base) is None:
        raise ValueError("Pier task requires an exact base_commit_hash")
    image = data["environment"]["docker_image"]
    if re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image) is None:
        raise ValueError("Pier task agent docker_image must be pinned by digest")
    verifier = data["verifier"]
    if verifier.get("environment_mode") != "separate":
        raise ValueError("Pier studies require separate verifier environments")
    if data["environment"].get("mcp_servers") or data["environment"].get("skills_dir"):
        raise ValueError("Pier studies supply text only, not task MCP servers or skills")
    dockerfile = (directory / "tests/Dockerfile").read_text()
    from_images = re.findall(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", dockerfile, re.MULTILINE)
    if not from_images or any(
        re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", item) is None for item in from_images
    ):
        raise ValueError("Pier verifier Dockerfile FROM images must be pinned by digest")
    return base, {
        "repository_url": metadata["repository_url"],
        "base_commit": base,
        "agent_image": image,
        "verifier_images": ",".join(from_images),
    }


def load_tasks(path: Path) -> tuple[PierTask, ...]:
    catalog = PierCatalog.model_validate(yaml.safe_load(path.read_text()))
    if re.fullmatch(r"[a-f0-9]{40}", catalog.source.get("revision", "")) is None:
        raise ValueError("Pier catalog requires an exact source revision")
    if len({entry.id for entry in catalog.tasks}) != len(catalog.tasks):
        raise ValueError("Pier task IDs must be unique")
    tasks = []
    for entry in catalog.tasks:
        candidate = path.parent / entry.path
        if (
            entry.path.is_absolute()
            or ".." in entry.path.parts
            or any(parent.is_symlink() for parent in (candidate, *candidate.parents))
            or not candidate.resolve().is_relative_to(path.parent.resolve())
        ):
            raise ValueError("Pier task directories must be relative, without symlinks")
        directory = candidate.resolve()
        base, metadata = _pinned_task(directory)
        tasks.append(
            PierTask(
                entry.id,
                (directory / "instruction.md").read_text(),
                directory,
                base,
                _tree_digest(directory),
                {**{f"catalog_{key}": value for key, value in catalog.source.items()}, **metadata},
            )
        )
    return tuple(tasks)


def freeze_tasks(tasks: tuple[PierTask, ...], directory: Path) -> tuple[PierTask, ...]:
    frozen = []
    for index, task in enumerate(tasks):
        if _tree_digest(task.directory) != task.digest:
            raise ValueError("Pier source task changed before freezing")
        target = directory / str(index)
        shutil.copytree(task.directory, target)
        frozen.append(
            PierTask(
                task.task_id,
                task.prompt,
                target,
                task.base_commit,
                _tree_digest(target),
                task.provenance,
            )
        )
    return tuple(frozen)


def preflight(python: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata; from pier.trial.trial import Trial; "
            "print(importlib.metadata.version('datacurve-pier'))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    version = result.stdout.strip()
    if result.returncode or version != PIER_VERSION:
        raise ValueError(
            f"Pier {PIER_VERSION} is required in {python}; install datacurve-pier=={PIER_VERSION}"
        )
    return {
        "harness": "codex-cli",
        "execution_backend": "pier",
        "pier_version": version,
        "python": str(python),
        "paid_calls": 0,
    }


def invoke(
    *,
    target: Path,
    task: PierTask,
    prompt: str,
    model: str,
    reasoning: str,
    auth_file: Path,
    codex_version: str,
    python: Path,
    timeout: int,
) -> dict[str, Any]:
    target.mkdir(parents=True)
    request = {
        "task_dir": str(task.directory),
        "base_commit": task.base_commit,
        "target": str(target),
        "prompt": prompt,
        "model": model,
        "reasoning": reasoning,
        "auth_file": str(auth_file),
        "codex_version": codex_version,
        "timeout": timeout,
    }
    return _execute_request(target, request, python)


def _execute_request(target: Path, request: dict[str, Any], python: Path) -> dict[str, Any]:
    # The auth path is an invocation input; credential contents are never serialized.
    _write(target / "request.json", request)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_DEFAULT_PLATFORM",
        }
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    startup_error = None
    result_code = None
    process = None
    try:
        with (
            (target / "backend-stdout.log").open("w") as stdout,
            (target / "backend-stderr.log").open("w") as stderr,
        ):
            try:
                process = subprocess.Popen(
                    [
                        str(python),
                        "-m",
                        "pluginbench.pier_backend",
                        "--request",
                        str(target / "request.json"),
                    ],
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                result_code = process.wait()
            except OSError as exc:
                startup_error = f"Cannot start Pier backend: {exc}"
            finally:
                if process is not None and process.poll() is None:
                    # asyncio.run handles SIGINT by cancelling Trial.run, whose finally
                    # owns Docker cleanup. SIGTERM would bypass that owner.
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    _write(
                        target / "cancellation-cleanup.json", _stop_attempt_containers(target, env)
                    )
    finally:
        if request.get("auth_file"):
            _redact_attempt(target, Path(request["auth_file"]))
    if startup_error:
        return _failure(startup_error, target)
    if not (target / "backend-result.json").is_file():
        return _failure(f"Pier backend exited {result_code} without a result", target)
    document: dict[str, Any] = json.loads((target / "backend-result.json").read_text())
    return document


def _stop_attempt_containers(target: Path, env: dict[str, str]) -> dict[str, Any]:
    """Stop only containers whose Compose inputs belong to this exact attempt."""
    removed = []
    try:
        listing = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        for container in listing.stdout.splitlines():
            if not re.fullmatch(r"[a-f0-9]{12,64}", container):
                raise ValueError("unexpected Docker container identifier")
            receipt = subprocess.run(
                ["docker", "inspect", "--format", "{{json .Config.Labels}}", container],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            labels = json.loads(receipt.stdout)
            if not isinstance(labels, dict):
                raise ValueError("Docker labels are not an object")
            config_files = labels.get("com.docker.compose.project.config_files", "")
            if not isinstance(config_files, str):
                raise ValueError("Docker Compose input paths are not a string")
            configs = config_files.split(",")
            owned = any(
                Path(path).resolve().is_relative_to(target.resolve() / "pier/native")
                for path in configs
                if path.startswith("/")
            )
            if owned:
                subprocess.run(
                    ["docker", "rm", "-f", container],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                removed.append(container)
        return {
            "status": "complete",
            "removed_owned_containers": removed,
            "scope": "exact attempt Compose input paths",
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "status": "unknown",
            "removed_owned_containers": removed,
            "error": f"{type(exc).__name__}: container cleanup could not be verified",
        }


def _failure(message: str, target: Path | None = None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    before = None
    harness_version = None
    if target is not None:
        raw = target / "pier/native/agent"
        traces = _session_traces(raw)
        stream = raw / "codex.txt"
        parsed = codex.parse_events(stream.read_text() if stream.is_file() else "", traces, "codex")
        if _validated_export(target / "before"):
            before = str(target / "before")
        version = target / "runtime-version.json"
        if version.is_file():
            harness_version = json.loads(version.read_text()).get("harness_version")
    return {
        "execution": {
            **parsed,
            "successful_native_completion": False,
            "usage": parsed.get("usage"),
            "error": message,
        },
        "checks": {"behavior": None, "delivery_complete": None},
        "before": before,
        "handoff": None,
        "harness_version": harness_version,
    }


def _session_traces(raw: Path) -> list[str]:
    # Live sessions remain on the mounted log volume if Docker becomes unreachable.
    traces: dict[str, str] = {}
    for directory in (raw / "live-sessions", raw / "sessions"):
        for path in sorted(directory.rglob("*.jsonl")):
            if path.is_file():
                content = path.read_text()
                traces.setdefault(content, content)
    return list(traces.values())


def _redact_attempt(target: Path, auth_file: Path) -> None:
    secrets = codex.credential_values(auth_file)
    for name in ("before", "handoff"):
        snapshot = target / name
        if snapshot.is_dir() and any(
            secret.encode() in file.read_bytes()
            for file in snapshot.rglob("*")
            if file.is_file() and not file.is_symlink()
            for secret in secrets
        ):
            shutil.rmtree(snapshot)
            (target / f"{name}-export.json").unlink(missing_ok=True)
            (target / "expected-model.patch").unlink(missing_ok=True)
            (target / "pier/native/artifacts/model.patch").unlink(missing_ok=True)
            _write(
                target / "credential-rejection.json",
                {"snapshot": name, "reason": "credential found in submitted source"},
            )
    codex.scrub_artifacts(target, secrets)


def calibrate(tasks: tuple[PierTask, ...], target: Path, python: Path) -> dict[str, Any]:
    """Prove frozen verifiers reject nop and accept oracle before paid calls."""
    receipts = []
    for index, task in enumerate(tasks):
        directory = target / str(index)
        directory.mkdir(parents=True)
        result = _execute_request(
            directory,
            {"mode": "calibration", "target": str(directory), "task_dir": str(task.directory)},
            python,
        )
        receipt = {"task_id": task.task_id, "task_digest": task.digest, **result}
        receipts.append(receipt)
        _write(target / "calibration.json", {"paid_calls": 0, "tasks": receipts})
        if result.get("passed") is not True:
            raise ValueError(
                f"Frozen Pier verifier calibration failed for {task.task_id}; no paid attempts launched"
            )
    return {"paid_calls": 0, "tasks": receipts}


async def _checked(environment: Any, command: str) -> Any:
    result = await environment.exec(command=command, timeout_sec=120)
    if result.return_code:
        raise RuntimeError(f"Pier adapter command failed ({result.return_code}): {command}")
    result.stdout = result.stdout or ""
    return result


async def _export(environment: Any, revision: str, target: Path) -> None:
    archive = f"/tmp/pluginbench-{target.name}.tar"
    # Read object bytes directly: checkout filters and archive attributes change delivery.
    script = """import io, subprocess, sys, tarfile
entries = []
tree = subprocess.check_output(["git", "-C", "/app", "ls-tree", "-rz", sys.argv[1]])
for row in tree.split(b"\\0"):
    if not row:
        continue
    metadata, name = row.split(b"\\t", 1)
    mode, kind, oid = metadata.split()
    if mode not in (b"100644", b"100755") or kind != b"blob":
        raise ValueError("unsupported Git tree entry: " + name.decode("utf-8", "replace"))
    entries.append((mode, oid, name))
objects = subprocess.check_output(["git", "-C", "/app", "cat-file", "--batch"], input=b"\\n".join(oid for _, oid, _ in entries) + b"\\n") if entries else b""
stream = io.BytesIO(objects)
with tarfile.open(sys.argv[2], "w") as bundle:
    for mode, oid, name in entries:
        observed, kind, size = stream.readline().split()
        if observed != oid or kind != b"blob":
            raise ValueError("Git blob export mismatch")
        contents = stream.read(int(size))
        if len(contents) != int(size) or stream.read(1) != b"\\n":
            raise ValueError("truncated Git blob")
        member = tarfile.TarInfo(name.decode("utf-8", "surrogateescape"))
        member.mode = int(mode, 8)
        member.size = len(contents)
        bundle.addfile(member, io.BytesIO(contents))
"""
    await _checked(
        environment, f"python3 -c {shlex.quote(script)} {shlex.quote(revision)} {archive}"
    )
    with tempfile.TemporaryDirectory(prefix="pluginbench-pier-export-") as temporary:
        local = Path(temporary) / "source.tar"
        await environment.download_file(archive, local)
        staged = Path(temporary) / "source"
        staged.mkdir()
        with tarfile.open(local) as bundle:
            bundle.extractall(staged, filter="data")
        snapshot = inspect_snapshot(staged)
        shutil.move(str(staged), target)
        _write(
            target.parent / f"{target.name}-export.json",
            {"revision": revision, "snapshot_digest": snapshot["digest"]},
        )


def _validated_export(target: Path) -> bool:
    receipt = target.parent / f"{target.name}-export.json"
    if not receipt.is_file() or receipt.is_symlink():
        return False
    try:
        saved = json.loads(receipt.read_text())
        return bool(inspect_snapshot(target)["digest"] == saved["snapshot_digest"])
    except (ValueError, OSError, KeyError):
        return False


def build_agent(*, logs_dir: Path, model_name: str, request: dict[str, Any], **kwargs: Any) -> Any:
    """Pier's import-path factory; wrap its Codex object instead of copying its runner."""
    upstream = importlib.import_module("pier.agents.installed.codex").Codex
    agent = upstream(logs_dir=logs_dir, model_name=model_name, **kwargs)
    agent._REMOTE_CODEX_HOME = PurePosixPath("/home/pluginbench/.codex")
    if agent.skills_dir or agent.mcp_servers:
        raise ValueError("PluginBench Pier agent accepts text guidance only")
    original_run = agent.run
    original_exec = agent.exec_as_agent
    original_flags = agent.build_cli_flags
    original_allowlist = agent.network_allowlist
    capture = Path(request["target"])

    def flags(self: Any) -> str:
        return (
            str(original_flags())
            + ' --ignore-user-config --ignore-rules -c web_search="disabled" -c features.multi_agent=false'
        )

    def allowlist(self: Any) -> Any:
        result = original_allowlist()
        result.domains = sorted(set(result.domains) | {"chatgpt.com", "auth.openai.com"})
        return result

    async def execute(
        self: Any,
        environment: Any,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        native = "codex exec " in command
        if native:
            marker = "2>&1 </dev/null | tee "
            if marker not in command:
                raise ValueError("Unsupported Pier Codex command framing")
            command = command.replace(marker, "2>/logs/agent/codex-stderr.log </dev/null | tee ", 1)
            command += (
                "; pluginbench_native_status=${PIPESTATUS[0]}; "
                'printf "%s\\n" "$pluginbench_native_status" > /logs/agent/native-exit-code.txt; '
                'exit "$pluginbench_native_status"'
            )
        try:
            result = await original_exec(
                environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec
            )
            if native:
                _write(capture / "native-exit.json", {"exit_code": result.return_code})
            return result
        except BaseException as exc:
            if native:
                code = re.search(r"Command failed \(exit (\d+)\)", str(exc))
                _write(
                    capture / "native-exit.json",
                    {
                        "exit_code": int(code.group(1)) if code else None,
                        "error": type(exc).__name__,
                    },
                )
            raise

    async def run(self: Any, instruction: str, environment: Any, context: Any) -> None:
        head = (await _checked(environment, "git -C /app rev-parse HEAD")).stdout.strip()
        if head != request["base_commit"]:
            raise ValueError("Pier image HEAD differs from the declared base commit")
        initial_status = await _checked(
            environment, "git -C /app status --porcelain=v1 --untracked-files=all"
        )
        if initial_status.stdout.strip():
            raise ValueError("Pier image repository is not clean at its base commit")
        guidance = "AGENTS.md CLAUDE.md .agents .claude .codex .claude-plugin"
        inventory = await _checked(
            environment,
            "find /app -type d \\( -name .git -o -name node_modules \\) -prune -o "
            "\\( -name AGENTS.md -o -name CLAUDE.md -o -name .agents -o -name .claude "
            "-o -name .codex -o -name .claude-plugin \\) -print; "
            # The installer may populate its own HOME; the model uses a fresh private HOME.
            f"for p in / /tmp /home /home/pluginbench; do for n in {guidance}; do "
            'if [ -e "$p/$n" ]; then printf \'%s\\n\' "$p/$n"; fi; done; done',
        )
        _write(
            capture / "guidance-inventory.json", {"unexpected_paths": inventory.stdout.splitlines()}
        )
        if inventory.stdout.strip():
            raise ValueError("Pier image or repository contains custom guidance")
        setup_path = 'if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh"; fi; '
        native_path = (await _checked(environment, setup_path + "command -v codex")).stdout.strip()
        node_path = (await _checked(environment, setup_path + "command -v node")).stdout.strip()
        image_path = (await _checked(environment, 'printf "%s" "$PATH"')).stdout.strip()
        node_version = (await _checked(environment, setup_path + "node --version")).stdout.strip()
        if not native_path.startswith("/") or not node_path.startswith("/"):
            raise ValueError("Cannot resolve installed native Codex and Node executables")
        await _checked(environment, "mkdir -m 700 /home/pluginbench /tmp/pluginbench-tmp")
        agent._extra_env.update(
            HOME="/home/pluginbench",
            TMPDIR="/tmp/pluginbench-tmp",
            XDG_CONFIG_HOME="/home/pluginbench/.config",
            PATH=":".join(
                dict.fromkeys(
                    (
                        str(PurePosixPath(native_path).parent),
                        str(PurePosixPath(node_path).parent),
                        image_path,
                    )
                )
            ),
        )
        fresh_env = {"CODEX_HOME": agent._REMOTE_CODEX_HOME.as_posix()}
        version = await original_exec(environment, "codex --version", env=fresh_env, timeout_sec=30)
        if version.return_code:
            raise ValueError("Container Codex version preflight failed")
        version_lines = re.findall(r"^codex-cli \d+\.\d+\.\d+$", version.stdout, re.MULTILINE)
        if version_lines != [f"codex-cli {request['codex_version']}"]:
            raise ValueError("Container Codex version differs from its requested pin")
        _write(
            capture / "runtime-version.json",
            {"harness_version": version_lines[0], "output": version.stdout},
        )
        help_result = await original_exec(
            environment, "codex exec --help", env=fresh_env, timeout_sec=30
        )
        required_flags = ["--ignore-user-config", "--ignore-rules", "--json", "--model", "--config"]
        if help_result.return_code or any(
            flag not in help_result.stdout for flag in required_flags
        ):
            raise ValueError("Container Codex lacks required isolation flags")
        _write(
            capture / "runtime-preflight.json",
            {
                "help": help_result.stdout,
                "required_flags": required_flags,
                "paid_calls": 0,
                "fresh_home": True,
                "native_path": native_path,
                "node_version": node_version,
            },
        )
        await _checked(
            environment,
            "mkdir -p /home/pluginbench/.codex /logs/agent/live-sessions; "
            "ln -s /logs/agent/live-sessions /home/pluginbench/.codex/sessions",
        )
        await _export(environment, head, capture / "before")
        _write(capture / "prompt.txt.json", {"prompt": request["prompt"]})
        primary_failure = None
        try:
            await original_run(request["prompt"], environment, context)
        except BaseException as exc:
            primary_failure = exc
            _write(capture / "native-error.json", {"type": type(exc).__name__, "error": str(exc)})
        try:
            status = await _checked(
                environment, "git -C /app status --porcelain=v1 --untracked-files=all"
            )
            final_head = (await _checked(environment, "git -C /app rev-parse HEAD")).stdout.strip()
            _write(
                capture / "submission.json",
                {
                    "base_commit": head,
                    "final_head": final_head,
                    "git_status": status.stdout,
                    "delivery_complete": not status.stdout.strip(),
                    "graded_source": "base commit to final HEAD; uncommitted changes are omitted",
                },
            )
            expected = await _checked(environment, f"git -C /app diff --binary {head} HEAD")
            (capture / "expected-model.patch").write_text(expected.stdout)
            await _export(environment, final_head, capture / "handoff")
        except Exception as exc:
            _write(
                capture / "collection-error.json", {"type": type(exc).__name__, "error": str(exc)}
            )
            if primary_failure is None:
                raise
        if primary_failure is not None:
            raise primary_failure

    agent.build_cli_flags = MethodType(flags, agent)
    agent.network_allowlist = MethodType(allowlist, agent)
    agent.exec_as_agent = MethodType(execute, agent)
    agent.run = MethodType(run, agent)
    return agent


async def _run(request: dict[str, Any]) -> dict[str, Any]:
    trial_module = importlib.import_module("pier.trial.trial")
    config_module = importlib.import_module("pier.models.trial.config")
    target = Path(request["target"])
    config = config_module.TrialConfig(
        task={"path": request["task_dir"]},
        trial_name="native",
        trials_dir=target / "pier",
        agent={
            "import_path": "pluginbench.pier_backend:build_agent",
            "model_name": request["model"],
            "override_timeout_sec": request["timeout"],
            "env": {"CODEX_AUTH_JSON_PATH": request["auth_file"]},
            "kwargs": {
                "request": request,
                "version": request["codex_version"],
                "reasoning_effort": request["reasoning"],
            },
        },
        environment={"type": "docker", "delete": True},
    )
    started = time.monotonic()
    trial = await trial_module.Trial.create(config)

    _protect_verifier(trial, target / "pier/native/verifier", trial_module.TrialEvent)
    result = await trial.run()
    _redact_attempt(target, Path(request["auth_file"]))
    raw = target / "pier/native/agent"
    traces = _session_traces(raw)
    stream = raw / "codex.txt"
    parsed = codex.parse_events(
        stream.read_text() if stream.exists() else "",
        traces,
        "codex",
        requested_effort=request["reasoning"],
    )
    native = (
        json.loads((target / "native-exit.json").read_text())
        if (target / "native-exit.json").exists()
        else {"exit_code": None}
    )
    exit_receipt = raw / "native-exit-code.txt"
    if exit_receipt.is_file() and re.fullmatch(r"\d+\s*", exit_receipt.read_text()):
        native["exit_code"] = int(exit_receipt.read_text())
    failure = result.exception_info.model_dump(mode="json") if result.exception_info else None
    rewards = result.verifier_result.rewards if result.verifier_result else None
    reward = rewards.get("reward") if rewards else None
    behavior = (
        reward == 1
        if isinstance(reward, (int, float)) and not isinstance(reward, bool) and reward in (0, 1)
        else None
    )
    submission = (
        json.loads((target / "submission.json").read_text())
        if (target / "submission.json").exists()
        else {}
    )
    patch = target / "pier/native/artifacts/model.patch"
    expected = target / "expected-model.patch"
    captured = (
        patch.is_file()
        and expected.is_file()
        and patch.read_bytes() == expected.read_bytes()
        and _validated_export(target / "handoff")
    )
    return {
        "execution": {
            **parsed,
            **native,
            "elapsed_seconds": time.monotonic() - started,
            "successful_native_completion": parsed["successful_native_completion"]
            and native.get("exit_code") == 0
            and failure is None,
            "exception": failure,
            "pier_version": importlib.metadata.version("datacurve-pier"),
            "timed_out": bool(failure and "Timeout" in failure["exception_type"]),
        },
        "checks": {
            "behavior": behavior if captured else None,
            "delivery_complete": submission.get("delivery_complete"),
            "submission_collected": captured,
        },
        "rewards": rewards,
        "submission": submission,
        "before": str(target / "before") if _validated_export(target / "before") else None,
        "handoff": str(target / "handoff") if captured else None,
        "harness_version": json.loads((target / "runtime-version.json").read_text()).get(
            "harness_version"
        )
        if (target / "runtime-version.json").exists()
        else None,
    }


def _protect_verifier(trial: Any, directory: Path, events: Any) -> None:
    async def clear_verifier_logs(event: Any) -> None:
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

    trial.add_hook(events.VERIFICATION_START, clear_verifier_logs)


async def _calibration(request: dict[str, Any]) -> dict[str, Any]:
    trial_module = importlib.import_module("pier.trial.trial")
    config_module = importlib.import_module("pier.models.trial.config")
    target = Path(request["target"])
    results = {}
    for agent_name, expected in (("nop", 0), ("oracle", 1)):
        config = config_module.TrialConfig(
            task={"path": request["task_dir"]},
            trial_name=agent_name,
            trials_dir=target / "pier",
            agent={"name": agent_name},
            environment={"type": "docker", "delete": True},
        )
        trial = await trial_module.Trial.create(config)
        _protect_verifier(trial, target / "pier" / agent_name / "verifier", trial_module.TrialEvent)
        result = await trial.run()
        rewards = result.verifier_result.rewards if result.verifier_result else None
        failure = result.exception_info.model_dump(mode="json") if result.exception_info else None
        reward = rewards.get("reward") if rewards else None
        results[agent_name] = {
            "reward": reward,
            "expected": expected,
            "exception": failure,
            "passed": failure is None and type(reward) in (int, float) and reward == expected,
        }
    return {
        "passed": all(item["passed"] for item in results.values()),
        "checks": results,
        "paid_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    request = json.loads(arguments.request.read_text())
    try:
        result = asyncio.run(
            _calibration(request) if request.get("mode") == "calibration" else _run(request)
        )
    except Exception as exc:
        result = _failure(f"{type(exc).__name__}: {exc}", Path(request["target"]))
    _write(Path(request["target"]) / "backend-result.json", result)


if __name__ == "__main__":
    main()
