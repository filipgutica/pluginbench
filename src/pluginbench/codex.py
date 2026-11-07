"""Native CLI execution and evidence for isolated reviews and Harbor submissions."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

from pluginbench import isolation
from pluginbench.codex_events import admit_version as admit_version
from pluginbench.codex_events import observed_cli_version as observed_cli_version
from pluginbench.codex_events import parse_events as parse_events
from pluginbench.processes import terminate_process_group


GUIDANCE_NAMES = {
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    ".agents",
    ".claude",
    ".codex",
    ".claude-plugin",
}


def reject_guidance(
    workspace: Path, *, include_ancestors: bool = True, reject_git: bool = False
) -> None:
    """Reject undeclared instruction discovery surfaces in task source."""
    root = workspace.resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    contaminants = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts and not reject_git:
            continue
        if path.is_symlink():
            contaminants.append(str(path))
        elif path.name in GUIDANCE_NAMES or (reject_git and path.name == ".git"):
            contaminants.append(str(path))
    if include_ancestors:
        contaminants.extend(
            str(parent / name)
            for parent in root.parents
            for name in GUIDANCE_NAMES
            if (parent / name).exists()
        )
    if contaminants:
        raise ValueError(
            "custom guidance or unsupported fixture paths: " + ", ".join(sorted(contaminants))
        )


def preflight() -> dict[str, Any]:
    """Check native runtime capabilities locally without invoking a model."""
    outputs = {}
    for key, arguments in (("version", ["--version"]), ("help", ["exec", "--help"])):
        result = subprocess.run(
            ["codex", *arguments], capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode:
            raise ValueError(f"codex {key} check failed (exit {result.returncode})")
        outputs[key] = result.stdout
    required = (
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--model",
        "--config",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--cd",
    )
    missing = [flag for flag in required if flag not in outputs["help"]]
    if missing:
        raise ValueError("native codex lacks required isolation flags: " + ", ".join(missing))
    return {
        "harness": "codex-cli",
        "harness_version": outputs["version"].strip(),
        "help": outputs["help"],
        "required_flags": list(required),
        "paid_calls": 0,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def credential_values(auth_file: Path) -> tuple[str, ...]:
    """Collect credential scalars for redaction without exposing them in receipts."""
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str) and len(value) > 20:
            values.append(value)

    collect(json.loads(auth_file.read_text()))
    return tuple(values)


def validate_artifacts(directory: Path) -> None:
    """Accept only ordinary directories and files before reading native evidence."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("native artifact root must be an ordinary directory")

    def failed(error: OSError) -> None:
        raise error

    for root, names, files in os.walk(directory, followlinks=False, onerror=failed):
        for name in [*names, *files]:
            path = Path(root) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"unsupported native artifact: {path.relative_to(directory)}")


def copy_artifacts(source: Path, target: Path) -> str | None:
    """Preserve unsupported evidence as an inert archive, without following links."""
    try:
        validate_artifacts(source)
    except (OSError, ValueError) as error:
        target.mkdir(parents=True)
        with tarfile.open(target / "unsupported-artifacts.tar", "w", dereference=False) as archive:
            archive.add(source, arcname="artifacts")
        write_json(target / "unsupported-artifacts.json", {"error": str(error)})
        return str(error)
    shutil.copytree(source, target, symlinks=True)
    return None


def scrub_artifacts(directory: Path, secrets: tuple[str, ...]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        return
    for root, names, files in os.walk(directory, followlinks=False):
        names[:] = [name for name in names if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            original = path.read_bytes()
            content = original
            for secret in secrets:
                content = content.replace(secret.encode(), b"<redacted-credential>")
            if content != original:
                path.write_bytes(content)


def _native_traces(private: Path, artifacts: Path, secrets: tuple[str, ...]) -> list[str]:
    sessions = private / "codex/sessions"
    for path in (private, private / "codex", sessions):
        if path.is_symlink():
            copy_artifacts(path, artifacts / "unsupported-native-traces")
            raise ValueError("unsupported native session ancestor link")
        if path.exists() and not path.is_dir():
            raise ValueError("native session ancestors must be ordinary directories")
    if not sessions.exists():
        return []
    try:
        validate_artifacts(sessions)
    except (OSError, ValueError):
        scrub_artifacts(sessions, secrets)
        copy_artifacts(sessions, artifacts / "unsupported-native-traces")
        raise
    return [path.read_text() for path in sorted(sessions.rglob("*.jsonl"))]


def runtime_environment(
    private: Path, host: str, *, auth_file: Path | None = None
) -> tuple[dict[str, str], tuple[str, ...]]:
    if host == "claude":
        from pluginbench import claude

        return claude.runtime_environment(private, auth_file=auth_file)
    private.mkdir(mode=0o700)
    home = private / "home"
    home.mkdir(mode=0o700)
    temporary = private / "tmp"
    temporary.mkdir(mode=0o700)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        }
    }
    env.update(
        HOME=str(home), TMPDIR=str(temporary), XDG_CONFIG_HOME=str(home / ".config"), NO_COLOR="1"
    )
    secrets: list[str] = []
    if host == "codex":
        config = private / "codex"
        config.mkdir(mode=0o700)
        auth = auth_file if auth_file is not None else Path.home() / ".codex/auth.json"
        shutil.copy2(auth, config / "auth.json")
        (config / "auth.json").chmod(0o600)

        secrets.extend(credential_values(auth))
        env["CODEX_HOME"] = str(config)
    else:
        raise ValueError(f"unsupported native host: {host}")
    return env, tuple(secrets)


def command(
    *,
    host: str,
    model: str,
    workspace: Path,
    effort: str = "medium",
) -> list[str]:
    if host == "claude":
        from pluginbench import claude

        return claude.command(model=model, effort=effort)
    if host == "codex":
        return [
            "codex",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "-m",
            model,
            "-c",
            f"model_reasoning_effort={json.dumps(effort)}",
            "-c",
            'web_search="disabled"',
            "-c",
            "features.multi_agent=false",
            "--enable",
            "code_mode",
            "--enable",
            "code_mode_only",
            # The mandatory outer OS wrapper owns read-only scope. Nested Seatbelt fails.
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C",
            str(workspace),
            "-",
        ]
    raise ValueError(f"unsupported native host: {host}")


def validated_claude_launch(execution: dict[str, Any], model: str, effort: str) -> bool:
    """Bind a requested effort to an isolated launch without claiming effective effort."""
    launch = execution.get("launch")
    if not isinstance(launch, dict) or execution.get("actual_primary_models") != [model]:
        return False
    argv = launch.get("argv")
    return (
        bool(
            launch.get("harness") == "claude-cli"
            and launch.get("requested_model") == model
            and launch.get("requested_effort") == effort
            and launch.get("harness_version")
            and isinstance(argv, list)
            and ["--model", model] == argv[argv.index("--model") : argv.index("--model") + 2]
            and ["--effort", effort] == argv[argv.index("--effort") : argv.index("--effort") + 2]
            and "--safe-mode" in argv
            and execution.get("runtime_effort_evidence") in ([], [effort])
        )
        if isinstance(argv, list) and "--model" in argv and "--effort" in argv
        else False
    )


def invoke(
    *,
    area: Path,
    entry: dict[str, Any],
    protocol: dict[str, Any],
    prompt: str,
    timeout: int,
    output_schema: Path | None = None,
) -> dict[str, Any]:
    """Invoke a local reviewer with an OS-enforced read-only source packet."""
    artifacts = area / "artifacts"
    artifacts.mkdir(exist_ok=True)
    if any((artifacts / name).exists() for name in ("invocation.json", "execution.json")):
        raise ValueError("native invocation artifacts already exist; use a fresh trial area")
    private = area / "private"
    effort = protocol.get("reasoning", "medium")
    if not isinstance(effort, str) or not effort:
        raise ValueError("reasoning must be a nonempty effort string")
    argv = command(
        host=entry["host"],
        model=protocol["models"][entry["host"]],
        workspace=area / "workspace",
        effort=effort,
    )
    if output_schema is not None:
        if entry["host"] == "claude":
            argv.extend(["--json-schema", json.dumps(json.loads(output_schema.read_text()))])
        else:
            argv[-1:-1] = ["--output-schema", str(output_schema)]
    launch = {
        "harness": entry["host"] + "-cli",
        "requested_model": protocol["models"][entry["host"]],
        "requested_effort": effort,
        "argv": argv,
    }
    if entry["host"] == "claude":
        from pluginbench import claude

        launch["harness_version"] = claude.preflight()["harness_version"]
    write_json(
        artifacts / "invocation.json",
        {**launch, "timeout_seconds": timeout},
    )
    (artifacts / "prompt.txt").write_text(prompt)
    secrets: tuple[str, ...] = ()
    started = time.monotonic()
    timed_out = False
    proc = None
    traces = []
    failure: BaseException | None = None
    try:
        auth_file = protocol.get("auth_file")
        env, secrets = runtime_environment(
            private,
            entry["host"],
            **({"auth_file": Path(auth_file).expanduser()} if auth_file is not None else {}),
        )
        argv, filesystem = isolation.command(
            argv,
            writable=[private],
            readable=[area / "workspace", *([output_schema] if output_schema else [])],
            expected=protocol.get("isolation"),
        )
        write_json(
            artifacts / "runtime-isolation.json",
            {
                "environment_keys": sorted(env),
                "private_startup_files": sorted(
                    str(p.relative_to(private)) for p in private.rglob("*") if p.is_file()
                ),
                "fresh_home": not any((private / "home").iterdir())
                if (private / "home").is_dir()
                else None,
                "configuration_source": "private auth only; user/project setting sources disabled",
                "native_builtin_instructions": "retained",
                "filesystem": filesystem,
            },
        )
        with (
            (artifacts / "stdout.jsonl").open("w") as stdout,
            (artifacts / "stderr.log").open("w") as stderr,
        ):
            proc = subprocess.Popen(
                argv,
                cwd=area / "workspace",
                env=env,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
            try:
                proc.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup_error: OSError | subprocess.TimeoutExpired | None = None
        trace_error: OSError | ValueError | None = None
        try:
            if proc is not None:
                terminate_process_group(proc)
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup_error = error
            if failure is not None:
                failure.add_note(f"Process-group cleanup failed: {error}")
        try:
            traces = _native_traces(private, artifacts, secrets)
            for index, trace in enumerate(traces):
                (artifacts / f"host-trace-{index}.jsonl").write_text(trace)
        except (OSError, ValueError) as error:
            trace_error = error
            if failure is not None:
                failure.add_note(f"Native session evidence rejected: {error}")
        finally:
            try:
                if private.is_symlink():
                    private.unlink()
                elif private.exists():
                    shutil.rmtree(private)
            finally:
                try:
                    scrub_artifacts(artifacts, secrets)
                finally:
                    write_json(
                        artifacts / "execution.json",
                        {
                            "exit_code": proc.returncode if proc is not None else None,
                            "timed_out": timed_out,
                            "elapsed_seconds": time.monotonic() - started,
                            "private_runtime_removed": not private.exists(),
                            "cleanup_error": str(cleanup_error)
                            if cleanup_error is not None
                            else None,
                            "trace_error": str(trace_error) if trace_error is not None else None,
                            "successful_native_completion": False
                            if trace_error is not None
                            else not timed_out and proc is not None and proc.returncode == 0,
                        },
                    )
        if cleanup_error is not None and failure is None:
            raise cleanup_error
        if trace_error is not None and failure is None:
            raise trace_error
    validate_artifacts(artifacts)
    traces = [p.read_text() for p in sorted(artifacts.glob("host-trace-*.jsonl"))]
    parsed = parse_events(
        (artifacts / "stdout.jsonl").read_text(), traces, entry["host"], requested_effort=effort
    )
    if timed_out or proc is None or proc.returncode != 0:
        parsed["successful_native_completion"] = False
    parsed["launch"] = launch
    parsed["launch_harness_version"] = launch.get("harness_version")
    write_json(artifacts / "host-evidence.json", parsed)
    return {**json.loads((artifacts / "execution.json").read_text()), **parsed}
