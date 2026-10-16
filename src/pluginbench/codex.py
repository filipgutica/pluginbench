"""Native CLI execution and evidence for isolated reviews and Harbor submissions."""

from __future__ import annotations

import hashlib
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


def observed_cli_version(startup: object, *, host: str = "codex") -> str | None:
    """Return the exact agreed version from native session metadata, or unknown."""
    if not isinstance(startup, list):
        return None
    if host == "claude":
        from pluginbench import claude

        return claude.observed_cli_version(startup)
    versions: set[str] = set()
    for event in startup:
        if not isinstance(event, dict) or event.get("type") != "session_meta":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        version = payload.get("cli_version")
        if not isinstance(version, str) or not version.strip():
            return None
        versions.add(version)
    return next(iter(versions)) if len(versions) == 1 else None


def parse_events(
    stdout: str,
    traces: list[str],
    host: str,
    *,
    requested_effort: str = "medium",
    include_descendants: bool = False,
) -> dict[str, Any]:
    if host == "claude":
        from pluginbench import claude

        return claude.parse_events(stdout, traces, requested_effort=requested_effort)
    if host != "codex":
        raise ValueError(f"unsupported native host: {host}")
    commands: list[dict[str, Any]] = []
    models = set()
    usage = None
    startup = []
    errors: list[Any] = []
    tools = []
    final = []
    successful_completion = False
    turn_usage: list[dict[str, int]] = []
    stdout_events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            errors.append("malformed stdout JSONL")
            continue
        if not isinstance(event, dict):
            errors.append("stdout JSONL event is not an object")
            continue
        stdout_events.append(event)
        item = event.get("item", {})
        if not isinstance(item, dict):
            errors.append("stdout item is not an object")
            continue
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            commands.append(item)
        if item.get("type") == "agent_message" and event.get("type") == "item.completed":
            message_text = item.get("text", "")
            if isinstance(message_text, str):
                final.append(message_text)
            else:
                errors.append("invalid agent message text")
        if event.get("type") == "turn.completed":
            reported = event.get("usage")
            if isinstance(reported, dict) and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in reported.values()
            ):
                turn_usage.append(reported)
            else:
                errors.append("invalid turn usage")
            successful_completion = True
        if event.get("type") in {"error", "turn.failed"}:
            errors.append(event)
        if item.get("type") not in {None, "command_execution", "agent_message", "reasoning"}:
            tools.append(item)
    effort = set()
    partial_usage = []
    trace_events: list[list[dict[str, Any]]] = []
    for trace in traces:
        last_observed_usage = None
        decoded_trace = []
        for line in trace.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                errors.append("malformed trace JSONL")
                continue
            if not isinstance(event, dict):
                errors.append("trace JSONL event is not an object")
                continue
            decoded_trace.append(event)
            if event.get("type") == "event_msg":
                payload = event.get("payload", {})
                info = payload.get("info") if isinstance(payload, dict) else None
                observed = info.get("total_token_usage") if isinstance(info, dict) else None
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "token_count"
                    and isinstance(observed, dict)
                ):
                    if observed and all(
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in observed.values()
                    ):
                        last_observed_usage = observed
            if event.get("type") in {"session_meta", "turn_context"}:
                payload = event.get("payload", {})
                startup.append({"type": event["type"], "payload": payload})
                if not isinstance(payload, dict):
                    errors.append("trace payload is not an object")
                    continue
                if event["type"] == "turn_context":
                    if isinstance(payload.get("model"), str):
                        models.add(payload["model"])
                    elif payload.get("model") is not None:
                        errors.append("invalid trace model")
                    if isinstance(payload.get("effort"), str):
                        effort.add(payload["effort"])
                    elif payload.get("effort") is not None:
                        errors.append("invalid trace effort")
        trace_events.append(decoded_trace)
        if last_observed_usage is not None:
            partial_usage.append(
                {
                    "trace_sha256": "sha256:" + hashlib.sha256(trace.encode()).hexdigest(),
                    "tokens": last_observed_usage,
                    "status": "last observed session total; incomplete usage lower bound",
                }
            )
    if turn_usage:
        # Missing usage fields stay unknown instead of being silently undercounted.
        common_keys = set.intersection(*(set(turn) for turn in turn_usage))
        usage = {key: sum(turn[key] for turn in turn_usage) for key in sorted(common_keys)}
    result = {
        "command_items": commands,
        "actual_primary_models": sorted(models),
        "usage": usage,
        "partial_usage": partial_usage if usage is None else [],
        "native_dollars": None,
        "dollars_status": "unknown",
        "requested_effort": requested_effort,
        "runtime_effort_evidence": sorted(effort),
        "startup": startup,
        "observed_cli_version": observed_cli_version(startup),
        "other_tools": tools,
        "stream_errors": errors,
        "final": final[-1] if final else "",
        "agent_messages": final,
        "successful_native_completion": successful_completion and not errors,
    }
    if include_descendants:
        accounting = _descendant_usage(stdout_events, trace_events, errors)
        result.update(accounting)
    return result


def _sum_usage(totals: list[dict[str, int]]) -> dict[str, int] | None:
    if not totals:
        return None
    common_keys = set.intersection(*(set(total) for total in totals))
    return {key: sum(total[key] for total in totals) for key in sorted(common_keys)}


def _token_usage(value: object) -> dict[str, int] | None:
    if (
        not isinstance(value, dict)
        or not value
        or not all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in value.values()
        )
    ):
        return None
    return value


def _descendant_usage(
    stdout: list[dict[str, Any]],
    traces: list[list[dict[str, Any]]],
    stream_errors: list[Any],
) -> dict[str, Any]:
    """Account once per native thread; unproven coverage remains a lower bound.

    Codex 0.162.1 protocol.rs defines session_meta.id as the thread identity,
    source.subagent.thread_spawn as lineage, and token_count as cumulative usage.
    A successful task_complete closes a turn, not a delta to add to that usage.
    """
    reasons: set[str] = set()
    sessions: dict[str, dict[str, Any]] = {}
    for events in traces:
        metadata = [event.get("payload") for event in events if event.get("type") == "session_meta"]
        if len(metadata) != 1 or not isinstance(metadata[0], dict):
            reasons.add("trace lacks one unambiguous session identity")
            continue
        meta = metadata[0]
        identity = meta.get("id")
        if not isinstance(identity, str) or not identity:
            reasons.add("trace lacks one unambiguous session identity")
            continue
        previous = sessions.get(identity)
        if previous is not None:
            old_events = previous["events"]
            if events[: len(old_events)] == old_events:
                previous["events"] = events
            elif old_events[: len(events)] != events:
                reasons.add(f"conflicting traces for session {identity}")
            continue
        source = meta.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawned = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        source_parent = spawned.get("parent_thread_id") if isinstance(spawned, dict) else None
        parent = meta.get("parent_thread_id", source_parent)
        if parent is None:
            parent = source_parent
        if source_parent is not None and parent != source_parent:
            reasons.add(f"conflicting parent identity for session {identity}")
        if parent is not None and (not isinstance(parent, str) or not parent):
            reasons.add(f"invalid parent identity for session {identity}")
            parent = None
        if subagent is not None and parent is None:
            reasons.add(f"unresolved subagent lineage for session {identity}")
        if meta.get("forked_from_id") or meta.get("history_base"):
            reasons.add(f"inherited history is unsupported for session {identity}")
        sessions[identity] = {"events": events, "parent_thread_id": parent}

    identity_safe = not reasons
    roots = sorted(
        identity for identity, session in sessions.items() if session["parent_thread_id"] is None
    )
    root = roots[0] if len(roots) == 1 else None
    if root is None:
        reasons.add("expected exactly one root session")
        identity_safe = False
    stdout_roots = set()
    for event in stdout:
        if event.get("type") == "thread.started":
            if isinstance(event.get("thread_id"), str):
                stdout_roots.add(event["thread_id"])
            else:
                reasons.add("invalid stdout root identity")
                identity_safe = False
    if stdout_roots and stdout_roots != {root}:
        reasons.add("stdout root identity does not match captured sessions")
        identity_safe = False

    expected_children: set[str] = set()
    models: set[str] = set()
    child_models: set[str] = set()
    effort: set[str] = set()
    records = []
    for identity, session in sorted(sessions.items()):
        prior_reasons = set(reasons)
        parent = session["parent_thread_id"]
        ancestors = {identity}
        while parent is not None and parent in sessions and parent not in ancestors:
            ancestors.add(parent)
            parent = sessions[parent]["parent_thread_id"]
        if parent is not None or root not in ancestors:
            reasons.add(f"session {identity} is not linked to the root")
            identity_safe = False
        total: dict[str, int] | None = None
        complete = False
        failed = False
        pending_spawns: set[str] = set()
        for event in session["events"]:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") if event.get("type") == "event_msg" else None
            if event.get("type") == "turn_context":
                complete = False
                model = payload.get("model")
                if isinstance(model, str):
                    if identity == root:
                        models.add(model)
                    elif session["parent_thread_id"] is not None:
                        child_models.add(model)
                if identity == root and isinstance(payload.get("effort"), str):
                    effort.add(payload["effort"])
            if kind in {"task_started", "turn_started"}:
                complete = False
            if kind in {"task_complete", "turn_complete"}:
                complete = True
            if kind in {"error", "turn_aborted", "turn_failed"}:
                failed = True
            if kind == "token_count":
                info = payload.get("info")
                observed = (
                    _token_usage(info.get("total_token_usage")) if isinstance(info, dict) else None
                )
                if observed is not None:
                    if total is not None and any(
                        key not in observed or observed[key] < value for key, value in total.items()
                    ):
                        reasons.add(f"nonmonotonic cumulative usage for session {identity}")
                    total = observed
                elif isinstance(info, dict):
                    reasons.add(f"invalid cumulative usage for session {identity}")
            if kind == "collab_agent_spawn_begin":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    pending_spawns.add(call_id)
                else:
                    reasons.add(f"unidentified spawn for session {identity}")
            if kind == "collab_agent_spawn_end":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    pending_spawns.discard(call_id)
                child = payload.get("new_thread_id")
                if isinstance(child, str) and child:
                    expected_children.add(child)
                    if isinstance(payload.get("model"), str):
                        child_models.add(payload["model"])
                    if child in sessions and sessions[child]["parent_thread_id"] != identity:
                        reasons.add(f"spawn lineage does not match session {child}")
                        identity_safe = False
        has_usage = total is not None and {"input_tokens", "output_tokens"}.issubset(total)
        if not has_usage:
            reasons.add(f"missing usage for session {identity}")
        if pending_spawns:
            reasons.add(f"unresolved spawns for session {identity}")
        if not complete or failed:
            reasons.add(f"incomplete or failed session {identity}")
        session_reasons = reasons - prior_reasons
        records.append(
            {
                "session_id": identity,
                "parent_thread_id": session["parent_thread_id"],
                "observed_usage": total,
                "status": "complete"
                if complete and not failed and has_usage and not session_reasons
                else "incomplete",
                "reasons": sorted(session_reasons),
            }
        )

    # exec's stdout exposes collab tool receivers even if a child rollout was lost.
    for event in stdout:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "collab_tool_call":
            receivers = item.get("receiver_thread_ids")
            if isinstance(receivers, list):
                expected_children.update(value for value in receivers if isinstance(value, str))
    for child in sorted(expected_children - sessions.keys()):
        reasons.add(f"missing descendant session {child}")
    if stream_errors:
        reasons.add("native event stream contains errors")
    root_turns = [
        reported
        for event in stdout
        if event.get("type") == "turn.completed"
        if (reported := _token_usage(event.get("usage"))) is not None
    ]
    root_usage = _sum_usage(root_turns)
    if root is not None and root_usage is not None:
        captured = next(
            record["observed_usage"] for record in records if record["session_id"] == root
        )
        if captured is None or any(captured.get(key) != value for key, value in root_usage.items()):
            reasons.add("root stdout usage does not match cumulative session usage")
    observed_totals = [
        record["observed_usage"] for record in records if record["observed_usage"] is not None
    ]
    observed_usage = _sum_usage(observed_totals)
    # An ambiguous identity/history can include the same work more than once.
    # Retain individual observations, but do not label their sum a lower bound.
    complete = not reasons
    return {
        "usage": observed_usage if complete else None,
        "partial_usage": [
            {
                "session_id": record["session_id"],
                "tokens": record["observed_usage"],
                "status": "last observed session total; incomplete usage lower bound",
            }
            for record in records
            if not complete and identity_safe and record["observed_usage"] is not None
        ],
        "actual_primary_models": sorted(models),
        "actual_descendant_models": sorted(child_models),
        "runtime_effort_evidence": sorted(effort),
        "usage_accounting": {
            "status": "complete" if complete else "incomplete",
            "scope": "root_and_descendants",
            "root_session_id": root,
            "reasons": sorted(reasons),
            "sessions": records,
            "observed_usage_lower_bound": observed_usage if identity_safe else None,
        },
    }


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
