"""Validate a Claude Code coding attempt's token usage against its session transcripts.

The headline is `result.modelUsage`, which counts subagents. The cross-check is every session
transcript Claude writes under `projects/`: one root transcript, `<session>.jsonl`, and one
`<session>/subagents/agent-<id>.jsonl` per subagent. A subagent's `agent-<id>.meta.json` names
the `Agent` tool call that started it (`toolUseId`). Any failed rule leaves usage unvalidated.

The stream is the least durable record: a rule never relaxes on the stream's word alone. The
launched CLI version picks the rule, and compaction counts only when the root transcript
records it too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pluginbench import claude

SUBAGENT_TOOL = "Agent"
_MODEL_USAGE = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
_MESSAGE_USAGE = (
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
)
# Derived from the 2.1.322 probe's one manual `/compact` (the `compaction` fixture): the
# untranscribed compaction call read the boundary's `preTokens` (15,343, the context before
# compaction) plus a 1,995-token compaction prompt, and wrote the summary (421 tokens), which
# is part of the context the boundary's `postTokens` (4,799) counts. Each compaction may
# therefore add at most `preTokens` plus this prompt allowance (about twice the observed
# prompt) of input and `postTokens` of output. The prompt is fixed text, so the allowance is a
# constant rather than a multiple of the context. Recalibrate when qualifying another version.
COMPACTION_PROMPT_TOKENS = 4000


def coding_usage(
    stream: str, sessions: Path, *, runner_model: str, cli_version: str
) -> dict[str, Any]:
    """Usage and its accounting; usage is None when any accounting rule fails.

    `stream` is the CLI's stream-json output, `sessions` its `CLAUDE_CONFIG_DIR`, and
    `cli_version` the version the attempt was launched and checked with.
    """
    reasons: list[str] = []
    events = _stream_events(stream, reasons)
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    results = [e for e in events if e.get("type") == "result"]
    # A coding attempt sends one prompt, so one result; a timeout leaves none (no lower bound).
    if len(inits) != 1 or len(results) != 1:
        reasons.append("the stream needs exactly one system/init and one result event")
    init = inits[0] if inits else {}
    result = results[0] if results else {}
    session = init.get("session_id")
    if not isinstance(session, str) or not session or result.get("session_id") != session:
        reasons.append("the init and result session ids disagree")
    if result.get("is_error") is not False or result.get("subtype") != "success":
        reasons.append("the result is an error or not a success")
    model_usage = _model_usage(result.get("modelUsage"))
    if model_usage is None:
        reasons.append("a modelUsage count is missing or ill-typed")
    if init.get("model") != runner_model:
        reasons.append("the primary model differs from the runner model")
    version = claude.observed_cli_version(inits)
    if version != cli_version:
        reasons.append("the init CLI version is missing or differs from the launched version")
    transcripts = _transcript_usage(sessions / "projects", session, reasons)
    transcribed = transcripts["models"]
    if _agent_calls(events) != transcripts["calls"]:
        reasons.append("the stream's Agent calls differ from the transcripts'")
    compacted = any(
        e.get("type") == "system" and e.get("subtype") == "compact_boundary" for e in events
    )
    if compacted != transcripts["compacted"]:
        reasons.append("the stream and the root transcript disagree on compaction")
    bound = transcripts["compaction_bound"]
    if cli_version not in claude.QUALIFIED_VERSIONS:
        rule = "one-sided"
    else:
        rule = "compaction" if compacted and transcripts["compacted"] else "equal"
    if rule == "compaction" and bound is None:
        reasons.append("a compact boundary's preTokens or postTokens is missing or ill-typed")

    per_model: dict[str, dict[str, int]] = {}
    untranscribed: dict[str, dict[str, int]] = {}
    for model in sorted(set(model_usage or {}) | set(transcribed)):
        reported = (model_usage or {}).get(model, dict.fromkeys(_MODEL_USAGE, 0))
        seen = transcribed.get(model, dict.fromkeys(_MESSAGE_USAGE, 0))
        reported_input = sum(reported[key] for key in _MODEL_USAGE[:3])
        seen_input = sum(seen[key] for key in _MESSAGE_USAGE[:3])
        per_model[model] = {
            "input_tokens": reported_input,
            "output_tokens": reported["outputTokens"],
            "cached_input_tokens": reported["cacheReadInputTokens"],
            "cache_creation_input_tokens": reported["cacheCreationInputTokens"],
            "transcript_input_tokens": seen_input,
            "transcript_output_tokens": seen["output_tokens"],
        }
        if model_usage is None:
            continue
        if model in transcribed and model not in model_usage:
            reasons.append(f"transcript model {model} is missing from modelUsage")
            continue
        excess = {
            "input_tokens": reported_input - seen_input,
            "output_tokens": reported["outputTokens"] - seen["output_tokens"],
        }
        if rule == "equal":
            # Calibrated on 2.1.322: without compaction every call is transcribed with its
            # final usage, so any difference is lost or invented usage.
            if any(excess.values()):
                reasons.append(f"transcripts differ from modelUsage for {model}")
            continue
        # Per-step output counts are placeholders on unqualified versions; compare input only.
        compared = excess if rule == "compaction" else {"input_tokens": excess["input_tokens"]}
        if model != runner_model:
            # Only the main loop compacts; any other model is transcribed in full.
            if any(compared.values()):
                reasons.append(f"transcripts differ from modelUsage for {model}")
            continue
        if any(value < 0 for value in compared.values()):
            reasons.append(f"transcripts exceed modelUsage for {model}")
        if any(value > 0 for value in compared.values()):
            # Calls Claude makes but never transcribes, such as a compaction summary.
            untranscribed[model] = {key: max(value, 0) for key, value in compared.items()}
        if rule == "compaction" and bound is not None and any(
            compared[key] > bound[key] for key in bound
        ):
            reasons.append(f"untranscribed usage for {model} exceeds its compactions' bound")

    usage = claude._usage(result)[0] if not reasons else None
    return {
        "usage": usage,
        "usage_accounting": {
            "status": "complete" if usage is not None else "incomplete",
            "reasons": reasons,
            "rule": rule,
            "cli_version": version,
            "per_model": per_model,
            "untranscribed": untranscribed,
            # Report only: nesting is neither limited nor refused here.
            "observed_subagent_depth": transcripts["depth"],
        },
    }


def _stream_events(stream: str, reasons: list[str]) -> list[dict[str, Any]]:
    events = []
    for line in stream.splitlines():
        # Harbor tees stderr into the same file; its warning lines are not stream events.
        if not line.startswith("{"):
            # The CLI does not write "type" first in every event, so any embedded object counts.
            if '{"' in line:
                reasons.append("a stream line hides an event behind other output")
            continue
        try:
            event = json.loads(line)
        except ValueError:
            reasons.append("the stream has a malformed JSON event")
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _agent_calls(events: list[dict[str, Any]]) -> set[str]:
    """The ids of the Agent tool calls in assistant messages."""
    calls: set[str] = set()
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if event.get("type") != "assistant" or not isinstance(content, list):
            continue
        calls.update(
            block["id"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == SUBAGENT_TOOL
            and isinstance(block.get("id"), str)
        )
    return calls


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _model_usage(value: object) -> dict[str, dict[str, int]] | None:
    if not isinstance(value, dict) or not value:
        return None
    counts = {}
    for model, entry in value.items():
        if not isinstance(entry, dict) or not all(_count(entry.get(k)) for k in _MODEL_USAGE):
            return None
        counts[model] = {key: entry[key] for key in _MODEL_USAGE}
    return counts


def _compaction_bound(boundaries: list[dict[str, Any]]) -> dict[str, int] | None:
    """The most untranscribed usage these compactions explain, summed over them; None when a
    boundary lacks well-typed token counts. See COMPACTION_PROMPT_TOKENS."""
    bound = {"input_tokens": 0, "output_tokens": 0}
    for boundary in boundaries:
        meta = boundary.get("compactMetadata")
        if not isinstance(meta, dict) or not all(
            _count(meta.get(key)) for key in ("preTokens", "postTokens")
        ):
            return None
        bound["input_tokens"] += meta["preTokens"] + COMPACTION_PROMPT_TOKENS
        bound["output_tokens"] += meta["postTokens"]
    return bound


def _transcript_usage(projects: Path, session: object, reasons: list[str]) -> dict[str, Any]:
    """Per-model usage of one session's transcripts, their Agent calls, whether the root
    transcript records a compaction and the bound its compactions set, and the deepest
    subagent's depth."""
    found: dict[str, Any] = {
        "models": {}, "calls": set(), "compacted": False, "compaction_bound": None, "depth": None,
    }
    files = sorted(projects.rglob("*.jsonl")) if projects.is_dir() else []
    roots = [path for path in files if len(path.relative_to(projects).parts) == 2]
    subagents = [path for path in files if path not in roots]
    if len(roots) != 1:
        reasons.append("the sessions need exactly one root transcript")
        return found
    root = roots[0]
    if root.stem != session:
        reasons.append("the root transcript is not the stream's session")
        return found
    expected = root.with_suffix("") / "subagents"
    if any(path.parent != expected or not path.name.startswith("agent-") for path in subagents):
        reasons.append("a transcript lies outside its session's subagents directory")
        return found
    # Each transcript's Agent calls, and the transcript that answers each call.
    calls: dict[Path, set[str]] = {}
    answers: dict[str, Path] = {}
    messages: dict[str, tuple[str, dict[str, int]]] = {}
    for path in [root, *subagents]:
        events = _read_transcript(path, session, messages, reasons)
        calls[path] = _agent_calls(events)
        if path == root:
            boundaries = [
                e for e in events
                if e.get("type") == "system" and e.get("subtype") == "compact_boundary"
            ]
            found["compacted"] = bool(boundaries)
            found["compaction_bound"] = _compaction_bound(boundaries)
            continue
        try:
            meta = json.loads(path.with_suffix(".meta.json").read_text())
        except (OSError, ValueError):
            meta = None
        call = meta.get("toolUseId") if isinstance(meta, dict) else None
        if not isinstance(call, str) or call in answers:
            reasons.append("a subagent transcript has no matching Agent tool call")
            continue
        answers[call] = path
    found["calls"] = set[str]().union(*calls.values())
    if found["calls"] - set(answers):
        reasons.append("an Agent tool call has no subagent transcript")
    if set(answers) - found["calls"]:
        reasons.append("a subagent transcript has no matching Agent tool call")
    for model, usage in messages.values():
        totals = found["models"].setdefault(model, dict.fromkeys(_MESSAGE_USAGE, 0))
        for key in _MESSAGE_USAGE:
            totals[key] += usage[key]
    parent = {
        path: caller
        for call, path in answers.items()
        for caller, made_calls in calls.items()
        if call in made_calls
    }
    depths = {root: 0}
    while ready := {
        path: depths[caller] + 1
        for path, caller in parent.items()
        if caller in depths and path not in depths
    }:
        depths.update(ready)
    found["depth"] = max(depths.values())
    return found


def _read_transcript(
    path: Path,
    session: object,
    messages: dict[str, tuple[str, dict[str, int]]],
    reasons: list[str],
) -> list[dict[str, Any]]:
    """Record each assistant message's usage once; return the file's events."""
    events = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            reasons.append(f"transcript {path.name} has a malformed JSON line")
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if "sessionId" in event and event["sessionId"] != session:
            reasons.append(f"transcript {path.name} belongs to another session")
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage, model, key = message.get("usage"), message.get("model"), message.get("id")
        if not isinstance(usage, dict) or not all(_count(usage.get(k)) for k in _MESSAGE_USAGE):
            reasons.append(f"transcript {path.name} has a message without well-typed usage")
            continue
        if not any(usage[k] for k in _MESSAGE_USAGE):
            # A message the CLI writes itself, such as `<synthetic>` after an API error,
            # made no model call.
            continue
        if not isinstance(key, str) or not isinstance(model, str):
            reasons.append(f"transcript {path.name} has a message without an id or model")
            continue
        entry = (model, {k: usage[k] for k in _MESSAGE_USAGE})
        # One API call may span several lines (one per content block), or several files.
        if messages.setdefault(key, entry) != entry:
            reasons.append("a message id repeats with different usage")
    return events
