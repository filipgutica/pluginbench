"""Provider stops: a session that ended at its provider or account is no result.

The streams are written by hand from the shapes Claude Code 2.1.322 and Codex 0.167.2 emit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pluginbench import provider_stops

PLAIN = Path(__file__).parent / "fixtures/claude-2.1.322/coding/plain/stream.jsonl"
_SESSION = "00000000-0000-4000-8000-000000000024"
_LIMIT_TEXT = "You've hit your limit · resets Nov 15, 4pm (UTC)"


def _lines(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _plain() -> list[dict[str, object]]:
    return [json.loads(line) for line in PLAIN.read_text().splitlines()]


def _api_error(category: str, status: int | None, text: str) -> list[dict[str, object]]:
    """The main-loop assistant event and result that end a Claude session on an API error."""
    init, assistant, _ = _plain()
    return [
        init,
        assistant,
        {
            "type": "assistant", "parent_tool_use_id": None, "session_id": _SESSION,
            "error": category, "is_api_error_message": True, "api_error_status": status,
            "message": {
                "id": "msg_0026", "model": "<synthetic>", "role": "assistant", "type": "message",
                "content": [{"type": "text", "text": text}],
            },
        },
        {
            "type": "result", "subtype": "success", "is_error": True, "num_turns": 2,
            "api_error_status": status, "result": text, "session_id": _SESSION,
        },
    ]


def _rejected(window: str = "seven_day", *, overage: bool = False) -> dict[str, object]:
    """A rejected window; with `overage`, the session goes on drawing on extra usage."""
    return {
        "type": "rate_limit_event", "session_id": _SESSION,
        "rate_limit_info": {
            "status": "rejected", "resetsAt": 1794758400, "rateLimitType": window,
            "utilization": 1.0, "overageStatus": "allowed" if overage else "rejected",
            "isUsingOverage": overage,
        },
    }


def _retry(attempt: int, error: str = "overloaded", status: int | None = 529) -> dict[str, object]:
    return {
        "type": "system", "subtype": "api_retry", "attempt": attempt, "max_retries": 10,
        "retry_delay_ms": 500 * 2 ** (attempt - 1), "error_status": status, "error": error,
        "session_id": _SESSION,
    }


def claude_rate_limited() -> str:
    """Work, then the seven-day window rejects the next request mid-session."""
    events = _api_error("rate_limit", 429, _LIMIT_TEXT)
    return _lines(*events[:2], _rejected(), *events[2:])


def claude_retried() -> str:
    """Two overloaded responses, retried, then the session completes."""
    init, assistant, result = _plain()
    return _lines(init, _retry(1), _retry(2), assistant, result)


# A Codex session that worked, then hit the account's usage limit mid-turn.
CODEX_LIMIT_TEXT = "You’ve hit your usage limit. Try again at Nov 15th, 2026 4:00 PM."
CODEX_LIMITED_STDOUT = _lines(
    {"type": "thread.started", "thread_id": "01a1b94e-9994-7cd0-8426-bf37e4a00687"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {
        "id": "item_1", "type": "command_execution", "command": "ls", "exit_code": 0,
        "status": "completed", "aggregated_output": "src\n",
    }},
    {"type": "error", "message": CODEX_LIMIT_TEXT},
    {"type": "turn.failed", "error": {"message": CODEX_LIMIT_TEXT}},
)


# Warnings are `error` items; Codex writes them at startup and when it changes transport.
_Event = dict[str, object]
_CODEX_WARNING: _Event = {
    "type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "warning"},
}
_CODEX_COMMAND: _Event = {"type": "item.completed", "item": {
    "id": "item_1", "type": "command_execution", "command": "ls", "exit_code": 0,
    "status": "completed", "aggregated_output": "src\n",
}}
_CODEX_WORK: tuple[_Event, ...] = (
    {"type": "thread.started", "thread_id": "01a1b94e-9994-7cd0-8426-bf37e4a00687"},
    _CODEX_WARNING,
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_1", "type": "command_execution",
                                      "command": "ls", "status": "in_progress"}},
    _CODEX_COMMAND,
    {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
                                        "text": "Next I run the tests."}},
)
_DISCONNECTED = "stream disconnected before completion: failed to lookup address information"
_WAITING: _Event = {"type": "error", "message":
            "Reconnecting... waiting for network (Connection failed: error sending request)"}


def _reconnect(attempt: int) -> dict[str, object]:
    return {"type": "error", "message": f"Reconnecting... {attempt}/5 ({_DISCONNECTED})"}


def _turn_failed() -> tuple[dict[str, object], ...]:
    return (
        {"type": "error", "message": _DISCONNECTED},
        {"type": "turn.failed", "error": {"message": _DISCONNECTED}},
    )


# The provider went away mid-session: Codex retried, fell back to HTTPS, then waited for the
# network without limit until the attempt timed out. No turn ended, so no rollout error.
CODEX_RECONNECTING_STDOUT = _lines(
    *_CODEX_WORK,
    *(_reconnect(attempt) for attempt in range(2, 6)),
    {"type": "item.completed", "item": {
        "id": "item_3", "type": "error",
        "message": f"Falling back from WebSockets to HTTPS transport. {_DISCONNECTED}",
    }},
    _WAITING, _WAITING, _WAITING,
)
CODEX_OPEN_ROLLOUT = _lines(
    {"type": "session_meta", "payload": {"cli_version": "0.167.2", "source": "exec"}},
    {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "medium"}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
)


def codex_rollout(code: object, message: str = CODEX_LIMIT_TEXT) -> str:
    """A rollout whose turn completed with an error, as Codex records a failed turn."""
    return _lines(
        {"type": "session_meta", "payload": {"cli_version": "0.167.2", "source": "exec"}},
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "medium"}},
        {"type": "event_msg", "ordinal": 14, "payload": {
            "type": "task_complete", "turn_id": "synthetic-turn", "last_agent_message": None,
            "error": {"message": message, "codex_error_info": code},
        }},
    )


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (claude_rate_limited(), "a usage or rate limit (Claude seven_day window, resets "
                                "2026-11-15 16:00 UTC): You've hit your limit"),
        (_lines(*_api_error("authentication_failed", 401, "Invalid API key")),
         "an authentication failure: Invalid API key"),
        (_lines(*_api_error("server_error", 500, "API Error: 500")),
         "a provider error that retries did not recover: API Error: 500"),
        # Cut off by a timeout while the CLI still retried an overloaded API.
        (_lines(*_plain()[:2], _retry(1), _retry(2)),
         "a provider error that retries did not recover: the session ended while retrying an "
         "API error (status 529, overloaded)"),
        # A limit that cut a session short may still let it wrap up with a success.
        (_lines(*_plain()[:2], _rejected("five_hour"), _plain()[2]),
         "a usage or rate limit (Claude five_hour window, resets 2026-11-15 16:00 UTC)"),
    ],
    ids=["mid-session-limit", "auth", "server-error", "timeout-while-retrying", "wrap-up"],
)
def test_a_claude_session_that_stopped_at_its_provider_is_named(
    stream: str, expected: str
) -> None:
    stop = provider_stops.claude_stop(stream)
    assert stop is not None and stop.startswith(expected)


@pytest.mark.parametrize(
    "stream",
    [
        claude_retried(),
        # The agent's own failures stay its attempt.
        _lines(*_api_error("invalid_request", 400, "API Error: 400 prompt is too long")),
        _lines(*_api_error("max_output_tokens", None, "API Error: output token maximum")),
        _lines(*_plain()[:2], {
            "type": "result", "subtype": "error_max_turns", "is_error": True, "errors": [],
            "session_id": _SESSION,
        }),
        # A subagent's API error returns to its parent, which may recover.
        _lines(*_plain()[:2], {
            **_api_error("rate_limit", 429, _LIMIT_TEXT)[2], "parent_tool_use_id": "toolu_01",
        }, _plain()[2]),
        # A rejected window the session went on past, on extra usage.
        _lines(*_plain()[:2], _rejected(overage=True), _plain()[2]),
    ],
    ids=[
        "retried-then-recovered", "invalid-request", "max-output", "max-turns", "subagent",
        "overage",
    ],
)
def test_a_claude_session_that_ended_on_its_own_is_no_provider_stop(stream: str) -> None:
    assert provider_stops.claude_stop(stream) is None


def test_claude_api_retries_are_counted_with_the_waits_the_cli_announced() -> None:
    assert provider_stops.claude_api_retries(claude_retried()) == {
        "count": 2, "announced_wait_ms": 1500, "errors": {"overloaded": 2},
    }
    assert provider_stops.claude_api_retries(PLAIN.read_text()) == {
        "count": 0, "announced_wait_ms": 0, "errors": {},
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("usage_limit_exceeded", "a usage or rate limit (usage_limit_exceeded): You’ve hit"),
        ("unauthorized", "an authentication failure (unauthorized)"),
        ({"response_too_many_failed_attempts": {"http_status_code": 529}},
         "a provider error that retries did not recover (response_too_many_failed_attempts)"),
    ],
)
def test_a_codex_turn_that_failed_at_its_provider_is_named(code: object, expected: str) -> None:
    stop = provider_stops.codex_stop(CODEX_LIMITED_STDOUT, [codex_rollout(code)])
    assert stop is not None and stop.startswith(expected)


def test_a_codex_turn_that_failed_otherwise_or_completed_is_no_provider_stop() -> None:
    # `other` is the code of a pre-turn outage, which `failed_before_first_turn` handles.
    assert provider_stops.codex_stop(CODEX_LIMITED_STDOUT, [codex_rollout("other")]) is None
    assert provider_stops.codex_stop(
        CODEX_LIMITED_STDOUT, [codex_rollout("context_window_exceeded")]
    ) is None
    # Reconnect notices are `error` events too; a completed turn recovered from them.
    recovered = _lines(
        {"type": "turn.started"},
        {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected)"},
        {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 1}},
    )
    assert provider_stops.codex_stop(
        recovered, [codex_rollout("usage_limit_exceeded")]
    ) is None


def test_a_codex_session_cut_off_while_reconnecting_is_a_provider_stop() -> None:
    expected = (
        "a provider error that retries did not recover: the session ended while reconnecting "
        "(Reconnecting... waiting for network"
    )
    for traces in ([CODEX_OPEN_ROLLOUT], []):
        stop = provider_stops.codex_stop(CODEX_RECONNECTING_STDOUT, traces)
        assert stop is not None and stop.startswith(expected)


def test_a_codex_turn_that_failed_after_exhausted_reconnects_is_a_provider_stop() -> None:
    stdout = _lines(*_CODEX_WORK, *(_reconnect(attempt) for attempt in range(1, 6)),
                    *_turn_failed())
    stop = provider_stops.codex_stop(stdout, [codex_rollout("other", _DISCONNECTED)])
    assert stop == (
        f"a provider error that retries did not recover (other after 5/5 reconnects): "
        f"{_DISCONNECTED}"
    )


def test_a_transport_fallback_notice_is_no_work_between_retries_and_failure() -> None:
    fallback: dict[str, object] = {"type": "item.completed", "item": {
        "id": "item_3", "type": "error",
        "message": f"Falling back from WebSockets to HTTPS transport. {_DISCONNECTED}",
    }}
    stdout = _lines(*_CODEX_WORK, *(_reconnect(attempt) for attempt in range(1, 6)), fallback,
                    *_turn_failed())
    stop = provider_stops.codex_stop(stdout, [codex_rollout("other", _DISCONNECTED)])
    assert stop is not None and "other after 5/5 reconnects" in stop


@pytest.mark.parametrize(
    ("stdout", "traces"),
    [
        # `other` also covers agent-side failures; retries that were not used up name none.
        (_lines(*_CODEX_WORK, _reconnect(2), *_turn_failed()),
         [codex_rollout("other", _DISCONNECTED)]),
        # Work after the exhausted retries means the session recovered from them.
        (_lines(*_CODEX_WORK, _reconnect(5), _CODEX_COMMAND, *_turn_failed()),
         [codex_rollout("other", _DISCONNECTED)]),
        # After the exhausted retries Codex waited for the network; another error failed the turn.
        (_lines(*_CODEX_WORK, _reconnect(5), _WAITING, *_turn_failed()),
         [codex_rollout("other", _DISCONNECTED)]),
        # Reconnected and back at work when the session ended.
        (_lines(*_CODEX_WORK, _reconnect(2), _WAITING, _CODEX_COMMAND), [CODEX_OPEN_ROLLOUT]),
        # The retries ran out, but the turn then failed on an unrelated, agent-side request.
        (_lines(*_CODEX_WORK, *(_reconnect(attempt) for attempt in range(1, 6)),
                *_turn_failed()),
         [codex_rollout("other", "unexpected status 400: invalid_request_error")]),
        # A failed turn right after a notice, on a code that is no stop.
        (_lines(*_CODEX_WORK, _reconnect(2), _turn_failed()[1]),
         [codex_rollout("context_window_exceeded", _DISCONNECTED)]),
        # Exhausted retries, then a failed turn on a code other than `other` that is no stop.
        (_lines(*_CODEX_WORK, *(_reconnect(attempt) for attempt in range(1, 6)),
                _turn_failed()[1]),
         [codex_rollout("context_window_exceeded", _DISCONNECTED)]),
        # Only a fragment of the notice, not its cause.
        (_lines(*_CODEX_WORK, *(_reconnect(attempt) for attempt in range(1, 6)),
                *_turn_failed()),
         [codex_rollout("other", "stream disconnected")]),
    ],
    ids=["other-before-exhausted", "work-after-exhausted", "waiting-then-failed",
         "work-after-reconnect", "unrelated-error-after-exhausted", "no-stop-code",
         "no-stop-code-after-exhausted", "notice-fragment"],
)
def test_a_codex_session_that_reconnected_or_failed_otherwise_is_no_provider_stop(
    stdout: str, traces: list[str]
) -> None:
    assert provider_stops.codex_stop(stdout, traces) is None
