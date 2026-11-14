"""Synthetic M4 traces; no real prompts or sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pluginbench.iterations import masked, measure_attempt, verification_command


@pytest.mark.parametrize(
    "command",
    [
        "go test ./...",
        "go build ./...",
        "go vet ./...",
        "py.test tests",
        "python3 -m unittest",
        "python -m mypy src",
        "mypy src",
        "ruff check .",
        "jest",
        "mocha",
        "vitest run",
        "tsc --noEmit",
        "eslint src",
        "npm test",
        "pnpm test",
        "yarn test",
        "npm run typecheck",
        "pnpm run prebuild",
        "yarn run lint:fix",
        "pnpm check",
        "yarn build",
        "make test-unit",
        "make clean build",
        "cd /app && pytest",
        "false || pytest",
        "echo hello; pytest",
        "echo hello\npytest",
        "cat file | pytest",
        "X=y env time timeout 30 npx jest",
        "env X=y uv run python -m pytest",
        "poetry run pytest",
        "pnpm exec vitest",
        "yarn exec eslint src",
        "/bin/bash -lc 'pytest'",
        "bash -lc 'cd /app && env X=y timeout 5 uv run pytest'",
        "env X=y bash -c 'npm run test:unit'",
        "bash -c \"bash -lc 'go test ./...'\"",
    ],
)
def test_frozen_verification_commands(command: str) -> None:
    assert verification_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "python - <<PY\nprint('ok')\nPY",
        "python /tmp/check.py",
        "npm run serve",
        "npm run",
        "npm install",
        "pnpm install",
        "make clean",
        "make -C build",
        "make -f tests.mk",
        "make TYPE=test",
        "ruff format .",
        "echo 'pytest && go test'",
        "git diff",
        "python3 -m compileall",
        "go version",
    ],
)
def test_commands_outside_frozen_list(command: str) -> None:
    assert not verification_command(command)


def _write(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def _tool(
    identity: str,
    name: str,
    inputs: dict[str, object],
    *,
    agent: str | None = None,
    error: bool = False,
    output: str = "ok",
) -> list[dict[str, object]]:
    return [
        {
            "type": "assistant",
            "agent_id": agent,
            "message": {
                "content": [
                    {"type": "tool_use", "id": identity, "name": name, "input": inputs},
                ]
            },
        },
        {
            "type": "user",
            "agent_id": agent,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": identity,
                        "content": output,
                        "is_error": error,
                    },
                ]
            },
        },
    ]


def test_claude_iterations_are_per_agent_and_stream_copies_are_not_added(tmp_path: Path) -> None:
    root = [
        *_tool("a", "Bash", {"command": "pytest"}, error=True, output="Exit code 1\nfailure"),
        *_tool("b", "Edit", {"file_path": "/app/src/a.py"}),
        *_tool("c", "Bash", {"command": "pytest && ruff check ."}),
        *_tool("d", "Bash", {"command": "pytest"}),
        *_tool("e", "Write", {"file_path": "/app/README.md"}),
        *_tool("f", "Bash", {"command": "pytest"}),
    ]
    root.extend(_tool("spawn", "Agent", {}))
    child = [
        *_tool("g", "Bash", {"command": "go test ./..."}, agent="child"),
        *_tool("h", "MultiEdit", {"file_path": "/app/tests/a_test.go"}, agent="child"),
        *_tool("i", "Bash", {"command": "go test ./..."}, agent="child"),
    ]
    _write(tmp_path / "stdout.jsonl", [*root[:2], *child[:2], *root[2:4], *child[2:], *root[4:]])
    _write(tmp_path / "harbor/trial/agent/sessions/projects/app/root.jsonl", root)
    _write(
        tmp_path / "harbor/trial/agent/sessions/projects/app/root/subagents/agent-child.jsonl",
        child,
    )
    (
        tmp_path / "harbor/trial/agent/sessions/projects/app/root/subagents/agent-child.meta.json"
    ).write_text(json.dumps({"toolUseId": "spawn"}))
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "complete", result
    assert result["iterations"] == 2
    assert (
        result["verification_commands"] == 6
    )  # one shell command, even with two matching segments
    assert result["failed_verification_commands"] == 1


def test_interleaved_stream_does_not_use_another_agents_edits(tmp_path: Path) -> None:
    _write(
        tmp_path / "stdout.jsonl",
        [
            *_tool("a", "Bash", {"command": "pytest"}),
            *_tool("b", "Write", {"file_path": "src/a.py"}, agent="child"),
            *_tool("c", "Bash", {"command": "pytest"}),
            *_tool("d", "Bash", {"command": "pytest"}, agent="child"),
            *_tool("e", "NotebookEdit", {"notebook_path": "tests/test.ipynb"}, agent="child"),
            *_tool("f", "Bash", {"command": "pytest"}, agent="child"),
        ],
    )
    result = measure_attempt(tmp_path, claude=True)
    assert result["iterations"] == 1, result


@pytest.mark.parametrize(
    ("command", "is_masked"),
    [
        ("pytest | tee log", True),
        ("pytest; echo done", True),
        ("pytest || true", True),
        ("pytest || echo failed", False),
        ("pytest && ruff check .", False),
        ("set -o pipefail; pytest | tee log", False),
        ("bash -lc 'set -o pipefail; pytest | tee log'", False),
    ],
)
def test_failure_masking(command: str, is_masked: bool) -> None:
    assert masked(command) is is_masked


def test_claude_permission_denials_cancellations_and_failed_edits(tmp_path: Path) -> None:
    _write(
        tmp_path / "stdout.jsonl",
        [
            *_tool("a", "Bash", {"command": "pytest"}, error=True, output="Permission denied"),
            *_tool("b", "Edit", {"file_path": "src/a.py"}, error=True, output="Cancelled"),
            *_tool("c", "Bash", {"command": "pytest"}, error=True, output="Cancelled"),
            *_tool("d", "Bash", {"command": "pytest | tee log"}, error=True, output="Exit code 1"),
            *_tool(
                "e",
                "Bash",
                {"command": "set -o pipefail; pytest | tee log"},
                error=True,
                output="Exit code 1",
            ),
        ],
    )
    result = measure_attempt(tmp_path, claude=True)
    assert result["iterations"] == 0
    assert result["verification_commands"] == 4
    assert result["failed_verification_commands"] == 1
    assert result["masked_commands"] == 1


def _command(identity: str, command: str, code: int | None = 0) -> list[dict[str, object]]:
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": identity,
                "arguments": json.dumps({"cmd": command}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": identity,
                "output": json.dumps({"exit_code": code}),
            },
        },
    ]


def _patch(identity: str, path: str) -> list[dict[str, object]]:
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": identity,
                "input": f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": identity,
                "output": f"Success. Updated the following files:\nM {path}",
            },
        },
    ]


def test_codex_native_sessions_missing_exit_code_and_duplicate_copies(tmp_path: Path) -> None:
    root: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "root"}},
        *_command("a", "pytest", 1),
        *_patch("b", "src/a.py"),
        *_command("c", "pytest", None),
    ]
    child: list[dict[str, object]] = [
        {"type": "session_meta", "payload": {"id": "child", "parent_thread_id": "root"}},
        *_command("d", "go test ./..."),
        *_patch("e", "tests/a_test.go"),
        *_command("f", "go test ./...", 2),
    ]
    sessions = tmp_path / "harbor/trial/agent/sessions"
    _write(sessions / "root.jsonl", root)
    _write(sessions / "child.jsonl", child)
    _write(sessions / "copy.jsonl", root)
    _write(
        tmp_path / "stdout.jsonl",
        [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest", "exit_code": 1},
            }
        ],
    )
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == 4
    assert result["iterations"] == 2
    assert result["failed_verification_commands"] is None
    assert result["known_failed_verification_commands"] == 2
    assert result["unknown_exit_commands"] == 1


def test_codex_stream_command_and_file_change_items(tmp_path: Path) -> None:
    _write(
        tmp_path / "stdout.jsonl",
        [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest", "exit_code": 1},
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "status": "completed",
                    "changes": [{"path": "tests/test_a.py"}],
                },
            },
            {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest"}},
        ],
    )
    result = measure_attempt(tmp_path, claude=False)
    assert result["iterations"] == 1
    assert result["failed_verification_commands"] is None


@pytest.mark.parametrize("claude", [True, False])
@pytest.mark.parametrize(
    "trace", ['{"type": "assistant"}\nnot-json', '{"type":"assistant"}', "[]", ""]
)
def test_unparseable_traces_fail_closed(tmp_path: Path, claude: bool, trace: str) -> None:
    (tmp_path / "stdout.jsonl").write_text(trace)
    result = measure_attempt(tmp_path, claude=claude)
    assert result["status"] == "incomplete"
    assert result["reason"]
    assert result["iterations"] is None
    assert result["verification_commands"] is None


def test_missing_trace_is_unknown_not_zero(tmp_path: Path) -> None:
    assert measure_attempt(tmp_path, claude=False)["iterations"] is None


def test_codex_inherited_context_does_not_count_as_child_work(tmp_path: Path) -> None:
    from test_codex import inherited_native_child

    inherited = inherited_native_child()
    child = inherited[:4] + [
        *_command("a", "pytest"),
        *_patch("b", "src/a.py"),
        *_command("c", "pytest"),
    ]
    child = [{**event, "ordinal": ordinal} for ordinal, event in enumerate(child)]
    sessions = tmp_path / "harbor/trial/agent/sessions"
    _write(sessions / "child.jsonl", child)
    _write(sessions / "root.jsonl", [{"type": "session_meta", "payload": {"id": "root"}}])
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "complete", result
    assert result["iterations"] == 1
    assert result["verification_commands"] == 2


def test_missing_claude_subagent_transcript_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path / "harbor/trial/agent/sessions/projects/app/root.jsonl",
        _tool("spawn", "Agent", {}),
    )
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "incomplete"
    assert "subagent transcripts" in str(result["reason"])


def test_missing_codex_subagent_trace_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path / "harbor/trial/agent/sessions/root.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            {
                "type": "event_msg",
                "payload": {"type": "collab_agent_spawn_end", "new_thread_id": "lost"},
            },
        ],
    )
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "incomplete"
    assert "subagent session missing" in str(result["reason"])


def test_valid_empty_trace_counts_zero_and_bad_shell_syntax_is_incomplete(tmp_path: Path) -> None:
    _write(tmp_path / "stdout.jsonl", _tool("a", "Bash", {"command": "echo hello"}))
    result = measure_attempt(tmp_path, claude=True)
    assert (
        result["status"] == "complete"
        and result["iterations"] == result["verification_commands"] == 0
    )
    _write(
        tmp_path / "stdout.jsonl", _tool("a", "Bash", {"command": "pytest && echo 'unterminated"})
    )
    assert measure_attempt(tmp_path, claude=True)["status"] == "incomplete"


def test_codex_stream_orders_verification_at_command_start(tmp_path: Path) -> None:
    _write(
        tmp_path / "stdout.jsonl",
        [
            {
                "type": "item.started",
                "item": {"id": "a", "type": "command_execution", "command": "pytest"},
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "status": "completed",
                    "changes": [{"path": "src/a.py"}],
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "a",
                    "type": "command_execution",
                    "command": "pytest",
                    "exit_code": 1,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "b",
                    "type": "command_execution",
                    "command": "pytest",
                    "exit_code": 0,
                },
            },
        ],
    )
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == 2
    assert result["iterations"] == result["failed_verification_commands"] == 1


def test_claude_stream_only_requires_successful_subagents(tmp_path: Path) -> None:
    _write(tmp_path / "stdout.jsonl", _tool("spawn", "Agent", {}))
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "incomplete"
    assert "subagent stream missing" in str(result["reason"])
    _write(
        tmp_path / "stdout.jsonl",
        _tool("spawn", "Agent", {}, error=True, output="Permission denied"),
    )
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "complete" and result["verification_commands"] == 0
