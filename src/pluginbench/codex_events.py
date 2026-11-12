"""Codex session event and rollout parsing: usage, models, effort and version evidence.

Kept out of `codex.py` so the vanilla cache's launcher hash covers only how attempts are
launched. A parser-only change re-reads saved traces; it does not change how a model was called.
"""

from __future__ import annotations

import json
import warnings
from functools import cache
from typing import Any


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
    startup = []
    errors: list[Any] = []
    tools = []
    tool_startup_failures = []
    final = []
    successful_completion = False
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
        if not isinstance(event.get("type"), str):
            errors.append("invalid stdout event type")
            continue
        stdout_events.append(event)
        item = event.get("item", {})
        if not isinstance(item, dict):
            errors.append("stdout item is not an object")
            continue
        if item.get("type") is not None and not isinstance(item["type"], str):
            errors.append("invalid stdout item type")
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
            if (
                not isinstance(reported, dict)
                or _token_usage(reported) is None
                or not {"input_tokens", "output_tokens"}.issubset(reported)
            ):
                errors.append("invalid turn usage")
            successful_completion = True
        if event.get("type") in {"error", "turn.failed"}:
            errors.append(event)
        if item.get("type") not in {None, "command_execution", "agent_message", "reasoning"}:
            tools.append(item)
    effort = set()
    trace_events: list[list[dict[str, Any]]] = []
    for trace in traces:
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
            if not isinstance(event.get("type"), str):
                errors.append("invalid trace event type")
                continue
            decoded_trace.append(event)
            payload = event.get("payload")
            if (
                event.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") in {"function_call_output", "custom_tool_call_output"}
                and isinstance(output := payload.get("output"), str)
            ):
                message = output.strip().lower()
                # Inspect tool errors only, never task/source text or the judge's explanation.
                if (
                    message.startswith(("error", "failed", "could not", "unable"))
                    and any(
                        helper in message
                        for helper in (
                            "codex-code-mode-host",
                            "codex-exec-server",
                            "code mode host",
                            "code-mode host",
                        )
                    )
                    and any(
                        failure in message
                        for failure in (
                            "failed to start",
                            "failed to spawn",
                            "could not start",
                            "no such file",
                            "not found",
                            "missing",
                            "permission denied",
                        )
                    )
                ):
                    tool_startup_failures.append(output)
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
    # Validate trace identity and cumulative usage even when subagents are disabled.
    accounting = _descendant_usage(stdout_events, trace_events, errors)
    result = {
        "command_items": commands,
        "actual_primary_models": sorted(models),
        "usage": accounting["usage"],
        "partial_usage": accounting["partial_usage"],
        "usage_accounting": accounting["usage_accounting"],
        # Evidence that a model turn occurred survives an unavailable token measure.
        "observed_token_usage": any(
            event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)
            or (
                event.get("type") in {"item.started", "item.updated", "item.completed"}
                and isinstance(item := event.get("item"), dict)
                and item.get("type") in (
                    "agent_message", "reasoning", "command_execution", "file_change",
                    "mcp_tool_call", "collab_tool_call", "web_search", "todo_list",
                )
            )
            for event in stdout_events
        ) or any(_trace_model_evidence(events) for events in trace_events),
        "native_dollars": None,
        "dollars_status": "unknown",
        "requested_effort": requested_effort,
        "runtime_effort_evidence": sorted(effort),
        "startup": startup,
        "observed_cli_version": observed_cli_version(startup),
        "other_tools": tools,
        "tool_startup_failures": tool_startup_failures,
        "stream_errors": errors,
        "final": final[-1] if final else "",
        "agent_messages": final,
        "successful_native_completion": successful_completion and not errors,
    }
    if include_descendants:
        result.update(accounting)
    return result


def _trace_model_evidence(events: list[dict[str, object]]) -> bool:
    """Recognize positive model evidence; other records do not prove a call happened."""
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        # session/mod.rs:5320-5338 emits token_count after sampling. The presence of
        # non-null info is evidence even when numeric accounting cannot validate it.
        if event.get("type") == "event_msg":
            if kind == "token_count" and payload.get("info") is not None:
                return True
            # EventMsg uses these names in all three verified versions. Deltas and
            # failed completions can survive even when no response item or usage did.
            if kind in (
                "agent_message", "agent_reasoning", "agent_reasoning_raw_content",
                "agent_reasoning_section_break", "agent_message_content_delta", "plan_delta",
                "reasoning_content_delta", "reasoning_raw_content_delta",
            ):
                return True
            # TTFT is an Option<i64>; zero still means that a model token arrived.
            if kind in ("task_complete", "turn_complete") and (
                payload.get("time_to_first_token_ms") is not None
                or payload.get("last_agent_message") is not None
            ):
                return True
            if kind == "item_completed" and isinstance(item := payload.get("item"), dict):
                if item.get("type") in (
                    "AgentMessage", "Reasoning", "Plan", "FunctionCallOutput", "CommandExecution",
                    "DynamicToolCall", "CollabAgentToolCall", "WebSearch", "ImageView",
                    "ImageGeneration", "FileChange", "McpToolCall", "ContextCompaction",
                ):
                    return True
                if item.get("type") == "SubAgentActivity" and item.get("kind") == "completed":
                    return True
                if item.get("type") == "Extension" and item.get("kind") in (
                    "image_gen.generation", "web.search", "clock.sleep",
                ):
                    return True
                if item.get("type") == "ExitedReviewMode" and item.get("review_output") is not None:
                    return True
        # protocol/models.rs:1062-1340 defines these output items (including the
        # compaction_summary alias). User messages and request controls are inputs.
        if event.get("type") == "response_item" and (
            (kind == "message" and payload.get("role") == "assistant")
            or kind in (
                "agent_message", "reasoning", "function_call", "function_call_output",
                "custom_tool_call", "custom_tool_call_output", "local_shell_call",
                "tool_search_call", "tool_search_output", "web_search_call", "image_generation_call",
                "compaction", "compaction_summary", "context_compaction",
            )
        ):
            return True
    return False


def failed_before_first_turn(execution: dict[str, Any]) -> str | None:
    """The error of a Codex session that failed before any model turn, else None.

    Such a session, typically a network or backend outage, never let the agent work, so its
    empty submission is not an outcome. A timeout, or a failure after any usage, command, or
    agent message, is the agent's attempt and stays scored.
    """
    if (
        execution.get("timed_out") is True
        or execution.get("usage") is not None
        or execution.get("observed_token_usage") is True
        or any(execution.get(key) for key in ("partial_usage", "command_items", "agent_messages"))
    ):
        return None
    for error in execution.get("stream_errors") or []:
        if isinstance(error, dict) and error.get("type") == "turn.failed":
            detail = error.get("error")
            message = detail.get("message") if isinstance(detail, dict) else None
            return message if isinstance(message, str) and message else "turn.failed"
    return None


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
    # Codex maps cache read/write and reasoning counts from input/output details (rust-v0.167.1,
    # codex-api/src/sse/responses.rs:131-143). These are subsets, not extra tokens.
    for subset, parent in (
        ("cached_input_tokens", "input_tokens"),
        ("cache_write_input_tokens", "input_tokens"),
        ("reasoning_output_tokens", "output_tokens"),
    ):
        if subset in value and (parent not in value or value[subset] > value[parent]):
            return None
    # Exact response totals sum input and output. protocol.rs:2554-2567 also emits
    # synthetic context-window totals; those cannot establish exact usage or a lower bound.
    if "total_tokens" in value and (
        not {"input_tokens", "output_tokens"}.issubset(value)
        or value["total_tokens"] != value["input_tokens"] + value["output_tokens"]
    ):
        return None
    return value


# Codex versions checked against real traces or equivalent upstream rollout source.
# rust-v0.167.1 keeps rust-v0.167.0's protocol, rollout writer, and fork/history code.
_VERIFIED_TRACE_VERSIONS = frozenset({"0.162.1", "0.167.0", "0.167.1"})


@cache
def admit_version(version: str) -> None:
    """Accept every version; warn once per unverified version in this process."""
    if version not in _VERIFIED_TRACE_VERSIONS:
        warnings.warn(
            f"Codex CLI {version} has not been checked against PluginBench's trace parser; "
            "token usage is verified per attempt and reported as unavailable if a trace "
            "does not match. Verified versions: " + ", ".join(sorted(_VERIFIED_TRACE_VERSIONS)),
            stacklevel=2,
        )


def _session_history(
    events: list[dict[str, Any]], reasons: set[str]
) -> tuple[dict[str, Any], list[dict[str, Any]], bool] | None:
    metadata = [event.get("payload") for event in events if event.get("type") == "session_meta"]
    first = events[0].get("payload") if events and events[0].get("type") == "session_meta" else None
    inherited = isinstance(first, dict) and (
        first.get("forked_from_id") is not None
        or first.get("history_base") is not None
        or first.get("subagent_history_start_ordinal") is not None
    )
    if isinstance(first, dict) and (
        first.get("history_mode") == "paginated" or any("ordinal" in event for event in events)
    ) and not all(
        isinstance(event.get("ordinal"), int)
        and not isinstance(event.get("ordinal"), bool)
        and event["ordinal"] == ordinal
        for ordinal, event in enumerate(events)
    ):
        reasons.add(f"noncontiguous rollout ordinals for session {first.get('id')}")
        return None
    if not inherited:
        if len(metadata) != 1 or not isinstance(first, dict):
            reasons.add("trace lacks one unambiguous session identity")
            return None
        return first, events, False

    # 0.162.1 protocol.rs defines this ordinal as the first own-history record.
    # Earlier records are inherited model context, including other session headers.
    # 0.167.0 and 0.167.1 keep the same header fields, boundary, and ordinals.
    assert isinstance(first, dict)
    version = first.get("cli_version")
    if not isinstance(version, str) or version not in _VERIFIED_TRACE_VERSIONS:
        label = version if isinstance(version, str) else "unknown"
        reasons.add(f"inherited-history layout of Codex {label} is unverified")
        return None
    boundary = first.get("subagent_history_start_ordinal")
    source = first.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawned = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    parent = first.get("parent_thread_id")
    path = first.get("agent_path")
    valid = (
        first.get("history_mode") == "paginated"
        and first.get("history_base") is None
        and isinstance(parent, str)
        and bool(parent)
        and first.get("forked_from_id") == parent
        and isinstance(first.get("session_id"), str)
        and isinstance(path, str)
        and bool(path)
        and isinstance(spawned, dict)
        and spawned.get("parent_thread_id") == parent
        and spawned.get("agent_path") == path
        and isinstance(boundary, int)
        and not isinstance(boundary, bool)
        and 1 <= boundary < len(events)
        and not any(event.get("type") == "session_meta" for event in events[boundary:])
    )
    if not valid:
        reasons.add(f"inherited history is unsupported for session {first.get('id')}")
        return None
    return first, [events[0], *events[boundary:]], True


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
        history = _session_history(events, reasons)
        if history is None:
            continue
        meta, events, inherited = history
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
        sessions[identity] = {
            "events": events,
            "parent_thread_id": parent,
            "inherited": inherited,
            "agent_path": meta.get("agent_path"),
            "root_session_id": meta.get("session_id"),
        }

    identity_safe = not reasons and "invalid turn usage" not in stream_errors
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
    expected_agent_paths: set[tuple[str, str]] = set()
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
        if session["inherited"] and session["root_session_id"] != root:
            reasons.add(f"inherited root identity does not match session {identity}")
            identity_safe = False
        total: dict[str, int] | None = None
        complete = False
        failed = False
        pending_spawns: set[str] = set()
        named_spawns: dict[str, str | None] = {}
        own_turn: str | None = None
        for event in session["events"]:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                reasons.add(f"invalid trace payload for session {identity}")
                continue
            kind = payload.get("type") if event.get("type") == "event_msg" else None
            if event.get("type") == "event_msg" and not isinstance(kind, str):
                reasons.add(f"invalid event message type for session {identity}")
                continue
            if event.get("type") == "response_item":
                call_id = payload.get("call_id")
                if (
                    payload.get("type") == "function_call"
                    and payload.get("namespace") == "collaboration"
                    and payload.get("name") == "spawn_agent"
                ):
                    if isinstance(call_id, str) and call_id:
                        if call_id in named_spawns:
                            reasons.add(f"duplicate named spawn for session {identity}")
                        named_spawns[call_id] = None
                    else:
                        reasons.add(f"unidentified named spawn for session {identity}")
                if (
                    payload.get("type") == "function_call_output"
                    and isinstance(call_id, str)
                    and call_id in named_spawns
                ):
                    try:
                        output = (
                            json.loads(payload["output"])
                            if isinstance(payload.get("output"), str)
                            else None
                        )
                    except ValueError:
                        output = None
                    path = output.get("task_name") if isinstance(output, dict) else None
                    if not isinstance(path, str) or not path:
                        reasons.add(f"unresolved named spawn for session {identity}")
                    else:
                        if named_spawns[call_id] not in (None, path):
                            reasons.add(f"conflicting named spawn result for session {identity}")
                        named_spawns[call_id] = path
                        expected_agent_paths.add((identity, path))
            if event.get("type") == "turn_context":
                complete = False
                if session["inherited"] and (
                    own_turn is None or payload.get("turn_id") != own_turn
                ):
                    reasons.add(f"unverified own turn for session {identity}")
                    identity_safe = False
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
                own_turn = (
                    payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
                )
            if kind in {"task_complete", "turn_complete"}:
                complete = True
                if session["inherited"] and (
                    own_turn is None or payload.get("turn_id") != own_turn
                ):
                    reasons.add(f"unverified own turn for session {identity}")
                    identity_safe = False
            if kind in {"error", "turn_aborted", "turn_failed"}:
                failed = True
            if kind == "token_count":
                info = payload.get("info")
                observed = (
                    _token_usage(info.get("total_token_usage")) if isinstance(info, dict) else None
                )
                if observed is not None:
                    if not {"input_tokens", "output_tokens"}.issubset(observed):
                        reasons.add(f"invalid cumulative usage for session {identity}")
                        identity_safe = False
                    if session["inherited"] and own_turn is None:
                        reasons.add(f"unverified own turn for session {identity}")
                        identity_safe = False
                    if session["inherited"] and observed != total:
                        last = (
                            _token_usage(info.get("last_token_usage"))
                            if isinstance(info, dict)
                            else None
                        )
                        if last is None or any(
                            last.get(key) != value - (total.get(key, 0) if total else 0)
                            for key, value in observed.items()
                        ):
                            reasons.add(f"unverified cumulative origin for session {identity}")
                            identity_safe = False
                    if total is not None and any(
                        key not in observed or observed[key] < value for key, value in total.items()
                    ):
                        reasons.add(f"nonmonotonic cumulative usage for session {identity}")
                        identity_safe = False
                    total = observed
                elif info is not None:
                    reasons.add(f"invalid cumulative usage for session {identity}")
                    identity_safe = False
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
        if any(path is None for path in named_spawns.values()):
            reasons.add(f"unresolved named spawn for session {identity}")
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
    for parent, path in sorted(expected_agent_paths):
        matching = [
            identity
            for identity, session in sessions.items()
            if session["parent_thread_id"] == parent and session["agent_path"] == path
        ]
        if not matching:
            reasons.add(f"missing descendant agent {path} from session {parent}")
        elif len(matching) != 1:
            reasons.add(f"conflicting descendant identities for agent {path} from session {parent}")
            identity_safe = False
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
