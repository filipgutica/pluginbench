"""Frozen M4 transcript counts. No models, shell execution, or inferred shell edits."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from pluginbench.code_diff import _is_source_path, _is_test_path
from pluginbench.codex_events import _session_history

LABEL = "Verification iterations (transcript count)"
LIMITATIONS = (
    "Shell-command edits, hook-run tests, and ad-hoc scripts are not observed. "
    "Iterations are counted within each agent, then summed; this does not measure self-review."
)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected an object")
    return dict(value)


def _tokens(command: str) -> list[list[str]]:
    # Here-document bodies are data, not additional shell commands.
    lines = []
    delimiters: list[str] = []
    for line in command.splitlines(keepends=True):
        if delimiters:
            if line.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        lines.append(line)
        delimiters.extend(re.findall(r"<<-?\s*['\"]?(\w+)['\"]?", line))
    if delimiters:
        raise ValueError("unterminated shell here-document")
    command = "".join(lines)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and all(char in ";&|\n" for char in token):
            segments.append([])
        else:
            segments[-1].append(token)
    return segments


def _matches(words: list[str]) -> bool:
    while words:
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", words[0]) or words[0] in {
            "env",
            "time",
            "npx",
        }:
            words = words[1:]
        elif words[0] == "timeout" and len(words) >= 2:
            words = words[2:]
        elif words[:2] in (["uv", "run"], ["poetry", "run"], ["pnpm", "exec"], ["yarn", "exec"]):
            words = words[2:]
        else:
            break
    if len(words) >= 2 and Path(words[0]).name == "bash" and words[1] in {"-c", "-lc"}:
        if len(words) < 3:
            raise ValueError("bash wrapper has no command")
        return verification_command(words[2])
    if not words:
        return False
    first = words[0]
    if first in {"pytest", "py.test", "mypy", "jest", "mocha", "vitest", "tsc", "eslint"}:
        return True
    if first == "go" and len(words) > 1 and words[1] in {"test", "build", "vet"}:
        return True
    if words[:2] == ["ruff", "check"]:
        return True
    if first in {"python", "python3"} and len(words) > 2:
        return words[1] == "-m" and words[2] in {"pytest", "unittest", "mypy"}
    if first in {"npm", "pnpm", "yarn"} and len(words) > 1:
        if words[1] == "test":
            return True
        script = (
            words[2]
            if words[1] == "run" and len(words) > 2
            else (words[1] if first in {"pnpm", "yarn"} and words[1] != "run" else "")
        )
        return any(part in script for part in ("test", "build", "lint", "check", "type"))
    if first == "make":
        skip = False
        for word in words[1:]:
            if skip:
                skip = False
            elif word in {
                "-C",
                "--directory",
                "-f",
                "--file",
                "--makefile",
                "-I",
                "--include-dir",
                "-o",
                "--old-file",
                "-W",
                "--what-if",
            }:
                skip = True
            elif (
                not word.startswith("-")
                and "=" not in word
                and any(part in word for part in ("test", "check", "lint", "build"))
            ):
                return True
    return False


def verification_command(command: str) -> bool:
    # Parse every segment even after a match: malformed trailing syntax is not a complete trace.
    return any([_matches(segment) for segment in _tokens(command)])


def masked(command: str) -> bool:
    return "pipefail" not in command and bool(
        re.search(r"(?<!\|)\|(?!\|)|;|\|\|\s*true\b", command)
    )


@dataclass
class Action:
    # Keep invocation order; later result events fill in the outcome of this same action.
    command: str | None = None
    edit: bool = False
    failed: bool | None = None


def _read(path: Path) -> list[dict[str, object]]:
    events = []
    for index, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = _object(json.loads(line))
        except ValueError as error:
            raise ValueError(f"{path.name}:{index}: malformed JSON event") from error
        if not isinstance(event.get("type"), str):
            raise ValueError(f"{path.name}:{index}: missing event type")
        events.append(event)
    if not events:
        raise ValueError(f"{path.name}: empty trace")
    return events


def _edit(path: object) -> bool:
    if not isinstance(path, str) or not path:
        raise ValueError("edit has no file path")
    return _is_test_path(path) or _is_source_path(path)


def _claude_calls(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    calls = {}
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if event["type"] == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls[str(block["id"])] = _object(block)
    return calls


def _claude_spawns(events: list[dict[str, object]]) -> set[str]:
    calls = {
        identity
        for identity, block in _claude_calls(events).items()
        if block["name"] in {"Agent", "Task"}
    }
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if event["type"] == "user" and isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error") is True
                ):
                    calls.discard(str(block.get("tool_use_id")))
    return calls


def _claude(events: list[dict[str, object]]) -> dict[str, list[Action]]:
    agents: dict[str, list[Action]] = {}
    calls: dict[str, tuple[dict[str, object], Action]] = {}
    results: dict[str, dict[str, object]] = {}
    for event in events:
        if event["type"] not in {"assistant", "user"}:
            if event["type"] not in {
                "system",
                "result",
                "stream_event",
                "rate_limit_event",
                "progress",
                "file-history-snapshot",
                "queue-operation",
                "summary",
                "last-prompt",
            }:
                raise ValueError(f"unsupported Claude event: {event['type']}")
            continue
        message = _object(event.get("message"))
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise ValueError("Claude message has no content blocks")
        agent = (
            event.get("agentId")
            or event.get("agent_id")
            or event.get("parent_tool_use_id")
            or "root"
        )
        if not isinstance(agent, str):
            raise ValueError("invalid Claude agent identity")
        for raw in content:
            block = _object(raw)
            kind = block.get("type")
            if kind == "tool_use":
                identity, name = block.get("id"), block.get("name")
                if not isinstance(identity, str) or not isinstance(name, str):
                    raise ValueError("unidentified Claude tool call")
                if identity in calls:
                    if calls[identity][0] != block:
                        raise ValueError("conflicting Claude tool call")
                    continue
                inputs = _object(block.get("input"))
                action = Action()
                if name == "Bash":
                    command = inputs.get("command")
                    if not isinstance(command, str):
                        raise ValueError("Claude Bash has no command")
                    action.command = command
                elif name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
                    action.edit = _edit(
                        inputs.get("notebook_path" if name == "NotebookEdit" else "file_path")
                    )
                calls[identity] = (block, action)
                agents.setdefault(agent, []).append(action)
            elif kind == "tool_result":
                identity = block.get("tool_use_id")
                if not isinstance(identity, str) or identity not in calls:
                    raise ValueError("Claude tool result has no call")
                if identity in results:
                    if results[identity] != block:
                        raise ValueError("conflicting Claude tool results")
                    continue
                results[identity] = block
                action = calls[identity][1]
                is_error = block.get("is_error", False)
                if not isinstance(is_error, bool):
                    raise ValueError("Claude tool result has invalid is_error")
                text = block.get("content", "")
                if isinstance(text, list):
                    text = "\n".join(str(_object(part).get("text", "")) for part in text)
                if not isinstance(text, str):
                    raise ValueError("invalid Claude tool result content")
                action.failed = is_error and text.lstrip().startswith("Exit code")
                if is_error:
                    action.edit = False
            elif kind not in {"text", "thinking", "redacted_thinking", "image", "document"}:
                raise ValueError(f"unsupported Claude content block: {kind}")
    if calls.keys() - results.keys():
        raise ValueError("Claude trace has unresolved tool calls")
    return agents


def _exit(output: object) -> bool | None:
    if isinstance(output, str):
        text = output
        try:
            output = json.loads(text)
        except ValueError:
            match = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", text)
            return int(match[1]) != 0 if match else None
    if isinstance(output, dict):
        code = output.get("exit_code")
        if code is None and isinstance(output.get("metadata"), dict):
            code = output["metadata"].get("exit_code")
        if code is not None and type(code) is not int:
            raise ValueError("Codex exit_code is not an integer")
        return code != 0 if code is not None else None
    return None


def _codex(events: list[dict[str, object]]) -> list[Action]:
    actions: list[Action] = []
    calls: dict[str, Action] = {}
    resolved: set[str] = set()
    stream_commands: dict[str, Action] = {}
    pending_items: set[str] = set()
    for event in events:
        if event["type"] == "response_item":
            item = _object(event.get("payload"))
            kind, name = item.get("type"), item.get("name")
            if kind in {"function_call", "custom_tool_call", "local_shell_call"}:
                action = Action()
                identity = item.get("call_id") or item.get("id")
                if not isinstance(identity, str):
                    raise ValueError("Codex tool call has no id")
                if identity in calls:
                    raise ValueError("duplicate Codex tool call")
                if name in {"exec_command", "shell", "shell_command"} or kind == "local_shell_call":
                    args = item.get("arguments", item.get("action"))
                    args = _object(json.loads(args) if isinstance(args, str) else args)
                    command = args.get("cmd", args.get("command"))
                    if isinstance(command, list) and all(isinstance(word, str) for word in command):
                        command = shlex.join(command)
                    if not isinstance(command, str):
                        raise ValueError("Codex shell call has no command")
                    action.command = command
                elif name == "apply_patch":
                    patch = item.get("input", item.get("arguments"))
                    if not isinstance(patch, str):
                        raise ValueError("Codex apply_patch has no patch")
                    paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M)
                    if not paths:
                        raise ValueError("unparseable Codex apply_patch")
                    action.edit = any(_edit(path) for path in paths)
                elif name in {"exec", "parallel"}:
                    raise ValueError("embedded Codex code-mode tools are not parseable")
                calls[identity] = action
                actions.append(action)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                identity = item.get("call_id")
                if not isinstance(identity, str) or identity not in calls:
                    raise ValueError("Codex output has no tool call")
                resolved.add(identity)
                action = calls[identity]
                action.failed = _exit(item.get("output"))
                if action.edit and action.failed is not False:
                    # apply_patch success uses a textual receipt rather than exit_code.
                    output = item.get("output")
                    if action.failed is True or (
                        isinstance(output, str)
                        and output.startswith(("Error", "Failed", "patch rejected"))
                    ):
                        action.edit = False
                    elif not isinstance(output, str) or not output.startswith(
                        "Success. Updated the following files:"
                    ):
                        raise ValueError("Codex patch outcome is unknown")
            elif kind not in {
                "message",
                "reasoning",
                "compaction",
                "compaction_summary",
                "web_search_call",
                "tool_search_call",
                "tool_search_output",
                "image_generation_call",
            }:
                raise ValueError(f"unsupported Codex response item: {kind}")
        elif event["type"] in {"item.started", "item.updated", "item.completed"}:
            item = _object(event.get("item"))
            if item.get("type") == "command_execution":
                identity = item.get("id")
                if event["type"] == "item.updated":
                    continue
                command = item.get("command")
                if not isinstance(command, str):
                    raise ValueError("Codex command item has no command")
                stream_action = stream_commands.get(str(identity)) if identity is not None else None
                if stream_action is None:
                    stream_action = Action(command=command)
                    actions.append(stream_action)
                    if isinstance(identity, str):
                        stream_commands[identity] = stream_action
                elif stream_action.command != command:
                    raise ValueError("Codex command changed between start and completion")
                if event["type"] == "item.completed":
                    stream_action.failed = _exit(item)
                    pending_items.discard(str(identity))
                elif isinstance(identity, str):
                    pending_items.add(identity)
                else:
                    raise ValueError("Codex started command has no id")
            elif item.get("type") == "file_change" and event["type"] == "item.completed":
                changes = item.get("changes")
                if not isinstance(changes, list):
                    raise ValueError("Codex file change has no paths")
                actions.append(
                    Action(
                        edit=item.get("status") == "completed"
                        and any(_edit(_object(change).get("path")) for change in changes)
                    )
                )
        elif event["type"] not in {
            "session_meta",
            "turn_context",
            "event_msg",
            "compacted",
            "thread.started",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "error",
            "item.started",
            "item.updated",
        }:
            raise ValueError(f"unsupported Codex event: {event['type']}")
    if calls.keys() - resolved or pending_items:
        raise ValueError("Codex trace has unresolved tool calls")
    return actions


def _count(agents: dict[str, list[Action]]) -> dict[str, object]:
    commands = iterations = failures = unknown = masked_count = 0
    for actions in agents.values():
        verified = edited = False
        for action in actions:
            if action.command is not None and verification_command(action.command):
                commands += 1
                iterations += int(verified and edited)
                verified, edited = True, False
                if masked(action.command):
                    masked_count += 1
                elif action.failed is None:
                    unknown += 1
                else:
                    failures += int(action.failed)
            elif action.edit and verified:
                edited = True
    return {
        "status": "complete",
        "reason": None,
        "iterations": iterations,
        "verification_commands": commands,
        "failed_verification_commands": failures if not unknown else None,
        "known_failed_verification_commands": failures,
        "unknown_exit_commands": unknown,
        "masked_commands": masked_count,
        "method": "M4 transcript count v1",
        "limitations": LIMITATIONS,
    }


def _claude_traces(
    paths: list[Path], stream_events: list[dict[str, object]]
) -> dict[str, list[Action]]:
    agents: dict[str, list[Action]] = {}
    # Validate both saved representations, but count their tool calls once.
    if stream_events:
        _claude(stream_events)
    if paths:
        roots = [path for path in paths if path.parent.name != "subagents"]
        if len(roots) != 1:
            raise ValueError("Claude traces need exactly one root transcript")
        inits = [
            event
            for event in stream_events
            if event["type"] == "system" and event.get("subtype") == "init"
        ]
        if inits and any(event.get("session_id") != roots[0].stem for event in inits):
            raise ValueError("Claude root transcript differs from stream session")
        agent_calls: set[str] = set()
        child_calls: set[str] = set()
        saved_calls: dict[str, dict[str, object]] = {}
        for path in paths:
            events = _read(path)
            parsed = _claude(events)
            saved_calls.update(_claude_calls(events))
            agent_calls.update(_claude_spawns(events))
            if path.parent.name == "subagents":
                meta = _object(json.loads(path.with_suffix(".meta.json").read_text()))
                call = meta.get("toolUseId")
                if not isinstance(call, str) or call in child_calls:
                    raise ValueError("Claude subagent has no unique parent tool call")
                child_calls.add(call)
            for identity, actions in parsed.items():
                agents[f"{path}:{identity}"] = actions
        if agent_calls != child_calls:
            raise ValueError("Claude Agent calls and subagent transcripts do not match")
        if any(
            saved_calls.get(identity) != block
            for identity, block in _claude_calls(stream_events).items()
        ):
            raise ValueError("Claude stream calls are missing or differ in transcripts")
    elif stream_events:
        agents = _claude(stream_events)
        children = {event.get("parent_tool_use_id") for event in stream_events}
        if _claude_spawns(stream_events) - children:
            raise ValueError("Claude subagent stream missing")
    else:
        raise ValueError("no saved Claude traces")
    return agents


def _codex_traces(
    paths: list[Path], stream_events: list[dict[str, object]]
) -> dict[str, list[Action]]:
    sessions: dict[str, list[dict[str, object]]] = {}
    parents: dict[str, str | None] = {}
    agent_paths: set[str] = set()
    reasons: set[str] = set()
    for path in paths:
        history = _session_history(_read(path), reasons)
        if history is None or reasons:
            raise ValueError("; ".join(sorted(reasons)))
        meta, events, _ = history
        session_id = meta.get("id")
        if not isinstance(session_id, str):
            raise ValueError("Codex session has no identity")
        source = meta.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawned = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        parent = meta.get("parent_thread_id") or (
            spawned.get("parent_thread_id") if isinstance(spawned, dict) else None
        )
        if parent is not None and not isinstance(parent, str):
            raise ValueError("invalid Codex parent identity")
        parents[session_id] = parent
        if isinstance(meta.get("agent_path"), str):
            agent_paths.add(str(meta["agent_path"]))
        previous = sessions.get(session_id, [])
        if events[: len(previous)] == previous:
            sessions[session_id] = events
        elif previous[: len(events)] != events:
            raise ValueError("conflicting Codex session copies")
    if sessions:
        root_sessions = [identity for identity, parent in parents.items() if parent is None]
        if len(root_sessions) != 1:
            raise ValueError("Codex traces need exactly one root session")
        for identity in sessions:
            seen = {identity}
            parent = parents[identity]
            while parent is not None:
                if parent not in parents or parent in seen:
                    raise ValueError("Codex subagent lineage is incomplete")
                seen.add(parent)
                parent = parents[parent]
        for events in sessions.values():
            spawn_calls: set[str] = set()
            for event in events:
                item = event.get("payload")
                if not isinstance(item, dict):
                    raise ValueError("invalid Codex trace payload")
                if (
                    item.get("type") == "collab_agent_spawn_end"
                    and isinstance(item.get("new_thread_id"), str)
                    and item.get("new_thread_id") not in sessions
                ):
                    raise ValueError("Codex subagent session missing")
                if item.get("name") == "spawn_agent" and isinstance(item.get("call_id"), str):
                    spawn_calls.add(item["call_id"])
                if (
                    item.get("type") == "function_call_output"
                    and item.get("call_id") in spawn_calls
                ):
                    output = item.get("output")
                    output = _object(json.loads(output) if isinstance(output, str) else output)
                    if (
                        output.get("task_name") not in agent_paths
                        and output.get("agent_id", output.get("id")) not in sessions
                    ):
                        if not output.get("error"):
                            raise ValueError("Codex spawned agent trace missing")
        if stream_events:
            _codex(stream_events)
        agents = {identity: _codex(events) for identity, events in sessions.items()}
    elif stream_events:
        agents = {"root": _codex(stream_events)}
        if any(
            _object(event.get("item", {})).get("type") == "collab_tool_call"
            for event in stream_events
        ):
            raise ValueError("Codex subagent sessions missing")
    else:
        raise ValueError("no saved Codex traces")
    return agents


def measure_attempt(artifacts: Path, *, claude: bool) -> dict[str, object]:
    """Prefer native sessions over their duplicate stream; unreadable evidence stays unknown."""
    try:
        agent = artifacts / "harbor/trial/agent"
        paths = sorted((agent / "sessions").rglob("*.jsonl"))
        stream = artifacts / "stdout.jsonl"
        if not stream.is_file():
            stream = agent / ("claude-code.txt" if claude else "codex.txt")
        stream_events = _read(stream) if stream.is_file() else []
        if not claude:
            paths = sorted({*paths, *artifacts.glob("host-trace-*.jsonl")})
        agents = (
            _claude_traces(paths, stream_events) if claude else _codex_traces(paths, stream_events)
        )
        result = _count(agents)
        result["sources"] = {
            str(path.relative_to(artifacts)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [*paths, *([stream] if stream.is_file() else [])]
        }
        return result
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        return {
            "status": "incomplete",
            "reason": str(error),
            "iterations": None,
            "verification_commands": None,
            "failed_verification_commands": None,
            "method": "M4 transcript count v1",
            "limitations": LIMITATIONS,
        }


def measure_run(root: Path) -> dict[str, object]:
    """Read the native bound execution receipts; preserve attempt/stage identities."""
    from pluginbench import study

    run = _object(json.loads((root / "run.json").read_text()))
    measured: dict[str, object] = {}
    for identity, stages in _object(run["executions"]).items():
        results = {}
        for stage, reference in _object(stages).items():
            receipt = study._bound_path(root, reference)
            execution = _object(json.loads(receipt.read_text()))
            launch = _object(execution.get("launch", {}))
            results[stage] = measure_attempt(
                receipt.parent / "artifacts", claude=launch.get("harness") == "claude-cli"
            )
        measured[identity] = results
    return measured


def attach(report: dict[str, object], measured: dict[str, object]) -> None:
    attempts = report.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("report has no attempts")
    for raw in attempts:
        attempt = _object(raw)
        stages = attempt.get("stages")
        if not isinstance(stages, list):
            raise ValueError("attempt has no stages")
        for stage in stages:
            if not isinstance(stage, dict):
                raise ValueError("invalid report stage")
            stage["iterations"] = _object(measured[str(attempt["id"])])[str(stage["id"])]
