"""Whether a coding session ended because of its provider or account, not the agent.

Such a session is no result: the run stops instead of grading it, and a resume codes it again.
Each rule reads only the evidence its CLI writes:

- Claude Code 2.1.322 stream-json. An API error that ends the session arrives as a main-loop
  `assistant` event with an `error` category and a `result` with `is_error: true` and
  `api_error_status`. Each retry is a `system/api_retry` event, and each limit update a
  `rate_limit_event`.
- Codex 0.167.2 rollout and stdout. A failed turn ends with an `event_msg` `task_complete` whose
  `error` holds a `codex_error_info` code. Each retry is a stdout `error` event that starts
  "Reconnecting...": "n/5" while retries last, and "waiting for network", without limit, while
  the connection fails.

Kept out of the launcher files (`components.FILES`), like `codex_events.py`: these rules read
saved evidence and do not change how a model is called.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LIMIT = "a usage or rate limit"
AUTH = "an authentication failure"
PROVIDER = "a provider error that retries did not recover"

# The `error` category of a Claude `assistant` event that wraps an API error.
_CLAUDE_ERRORS = {
    "rate_limit": LIMIT,
    "billing_error": LIMIT,
    "authentication_failed": AUTH,
    "oauth_org_not_allowed": AUTH,
    "account_on_hold": AUTH,
    "verification_required": AUTH,
    "cloud_credential_error": AUTH,
    "overloaded": PROVIDER,
    "server_error": PROVIDER,
}

# Codex `codex_error_info` codes. Others, `other` included, keep the existing handling.
_CODEX_ERRORS = {
    "usage_limit_exceeded": LIMIT,
    "rate_limit_exceeded": LIMIT,
    "unauthorized": AUTH,
    "server_overloaded": PROVIDER,
    "internal_server_error": PROVIDER,
    "policy_capacity_unavailable": PROVIDER,
    "response_too_many_failed_attempts": PROVIDER,
    "http_connection_failed": PROVIDER,
    "response_stream_connection_failed": PROVIDER,
}

# A Codex retry notice: "Reconnecting... 3/5 (...)" or "Reconnecting... waiting for network".
_RECONNECTING = "Reconnecting..."
_RECONNECT_COUNT = re.compile(r"Reconnecting\.\.\. (\d+)/(\d+)")

_DETAIL_LIMIT = 300


def _events(text: str) -> list[dict[str, Any]]:
    """The JSON object lines of a stream; other lines are the parsers' concern, not this rule's."""
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _detail(text: object) -> str:
    return text.strip()[:_DETAIL_LIMIT] if isinstance(text, str) else ""


def _status_kind(status: object) -> str | None:
    if type(status) is not int:
        return None
    if status in (401, 403):
        return AUTH
    if status == 429:
        return LIMIT
    return PROVIDER if status >= 500 else None


def _claude_window(info: dict[str, Any]) -> str:
    window = info.get("rateLimitType")
    reset = info.get("resetsAt")
    parts = [f"Claude {window} window" if isinstance(window, str) else "Claude limit rejected"]
    if type(reset) is int:
        resets = datetime.fromtimestamp(reset, timezone.utc)
        parts.append(f"resets {resets:%Y-%m-%d %H:%M} UTC")
    return ", ".join(parts)


def claude_stop(stdout: str) -> str | None:
    """Why a Claude session stopped at its provider or account, else None.

    A `rate_limit_event` with `status: "rejected"` is a stop even when the session then
    succeeds: a limit hit mid-response can leave a short wrap-up, so a success there may still
    be work the limit cut short. A rejected window the session goes on past on extra usage
    (`isUsingOverage`) is no stop. Otherwise only a session without a successful result can be
    a stop: one whose last main-loop API error, or failed result status, names a limit, an
    authentication failure, or a server error, or one cut off while an API call was still being
    retried. Invalid requests, unknown errors and max-output stops stay the agent's attempt.
    """
    events = _events(stdout)
    rejected = [
        event["rate_limit_info"]
        for event in events
        if event.get("type") == "rate_limit_event"
        and isinstance(event.get("rate_limit_info"), dict)
        and event["rate_limit_info"].get("status") == "rejected"
        and event["rate_limit_info"].get("isUsingOverage") is not True
    ]
    results = [event for event in events if event.get("type") == "result"]
    result = results[-1] if results else None
    failed = result is None or result.get("is_error") is True
    # A failed result's text is the CLI's error message; a successful one's is the agent's.
    message = _detail(result.get("result")) if result is not None and failed else ""
    if rejected:
        return f"{LIMIT} ({_claude_window(rejected[-1])})" + (f": {message}" if message else "")
    if not failed:
        return None
    errors = [
        event
        for event in events
        if event.get("type") == "assistant"
        and event.get("parent_tool_use_id") is None
        and isinstance(event.get("error"), str)
    ]
    kind = _CLAUDE_ERRORS.get(errors[-1]["error"]) if errors else None
    if kind is None and result is not None:
        kind = _status_kind(result.get("api_error_status"))
    if kind is not None:
        return kind + (f": {message}" if message else "")
    if result is None:
        # Cut off, as by a timeout, while an API call was still being retried.
        tail = [
            event
            for event in events
            if event.get("type") == "assistant"
            or (event.get("type") == "system" and event.get("subtype") == "api_retry")
        ]
        if tail and tail[-1].get("type") == "system":
            retry = tail[-1]
            category = retry.get("error")
            kind = _CLAUDE_ERRORS.get(category) if isinstance(category, str) else None
            status = retry.get("error_status")
            return f"{kind or PROVIDER}: the session ended while retrying an API error " + (
                f"(status {status}, {category})" if type(status) is int else f"({category})"
            )
    return None


def _codex_code(info: object) -> str | None:
    """A `codex_error_info` is a code, or one code mapped to its details."""
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and len(info) == 1:
        (code,) = info
        return str(code)
    return None


def _codex_retries(events: list[dict[str, Any]]) -> list[str]:
    """The stdout `error` messages since the session's last item of work.

    Warnings are `error` items, as is the one for a transport fallback, so they are no work.
    """
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") in {"item.started", "item.updated", "item.completed"}
            and isinstance(item, dict)
            and item.get("type") != "error"
        ):
            messages = []
        elif event.get("type") == "error" and isinstance(event.get("message"), str):
            messages.append(event["message"])
    return messages


def _exhausted(messages: list[str]) -> str | None:
    """The "n/n" of the last retry notice, when it used up its retries, else None."""
    notices = [message for message in messages if message.startswith(_RECONNECTING)]
    count = _RECONNECT_COUNT.match(notices[-1]) if notices else None
    if count is None or count[1] != count[2]:
        return None
    return f"{count[1]}/{count[2]}"


def codex_stop(stdout: str, traces: list[str]) -> str | None:
    """Why a Codex session stopped at its provider or account, else None.

    Only a session whose turn never completed can be a stop. Its rollout's last turn error
    must carry a limit, authorization or server code, or be `other` right after the stdout
    retries ran out: `other` alone also covers the agent's own failed requests. A session cut
    off, as by a timeout, while still reconnecting is a stop too. Codex retries a failed
    connection without limit, so such a turn writes no error.
    """
    events = _events(stdout)
    if any(event.get("type") == "turn.completed" for event in events):
        return None
    retries = _codex_retries(events)
    stop = None
    for trace in traces:
        for event in _events(trace):
            payload = event.get("payload")
            if (
                event.get("type") != "event_msg"
                or not isinstance(payload, dict)
                or payload.get("type") != "task_complete"
                or not isinstance(payload.get("error"), dict)
            ):
                continue
            code = _codex_code(payload["error"].get("codex_error_info"))
            kind = _CODEX_ERRORS.get(code) if code else None
            exhausted = _exhausted(retries) if code == "other" else None
            if exhausted is not None:
                kind, code = PROVIDER, f"other after {exhausted} reconnects"
            if kind is not None:
                message = _detail(payload["error"].get("message"))
                stop = f"{kind} ({code})" + (f": {message}" if message else "")
    if (
        stop is None
        and retries
        and retries[-1].startswith(_RECONNECTING)
        and not any(event.get("type") == "turn.failed" for event in events)
    ):
        stop = f"{PROVIDER}: the session ended while reconnecting ({_detail(retries[-1])})"
    return stop


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


def saved_stop(artifacts: Path, harness: object) -> str | None:
    """The provider stop that a coding stage's saved artifacts record, else None."""
    stdout = _read(artifacts / "stdout.jsonl")
    if harness == "claude":
        return claude_stop(stdout)
    traces = [_read(path) for path in sorted(artifacts.glob("host-trace-*.jsonl"))]
    return codex_stop(stdout, traces)


def claude_api_retries(stdout: str) -> dict[str, Any]:
    """How often Claude Code retried an API call, and the waits it announced before each.

    `announced_wait_ms` sums each event's `retry_delay_ms`: what the CLI said it would wait,
    not a measured time. A wait on a background rate limit is announced again on an interval
    while it lasts, so the sum can count one wait more than once.
    """
    retries = [
        event
        for event in _events(stdout)
        if event.get("type") == "system" and event.get("subtype") == "api_retry"
    ]
    errors: dict[str, int] = {}
    for event in retries:
        category = event.get("error")
        key = category if isinstance(category, str) else "unknown"
        errors[key] = errors.get(key, 0) + 1
    delays = [event.get("retry_delay_ms") for event in retries]
    return {
        "count": len(retries),
        "announced_wait_ms": sum(
            delay for delay in delays if isinstance(delay, int | float)
            and not isinstance(delay, bool) and delay >= 0
        ),
        "errors": dict(sorted(errors.items())),
    }
