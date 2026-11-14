"""Synthetic verification-iteration traces; no real prompts or sessions."""

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
        "bash -lc \"python - <<'PY-EOF'\nprint('ok')\nPY-EOF\npytest\"",
        "cat <<'PY-EOF'\npytest\nPY-EOF\ngo test ./...",
        "pytest <<< 'input'",
        "echo $((1<<3)) && pytest",
        "if pytest; then echo ok; fi",
        "then X=y timeout 5 uv run pytest",
        "elif go test ./...; then true; fi",
        "else pytest",
        "while pytest; do true; done",
        "until pytest; do true; done",
        "do pytest",
        "! pytest",
        "(pytest)",
        "{pytest; }",
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
        "npm run dev",
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
        "cat <<'PY-EOF'\npytest\nPY-EOF",
        "bash -lc \"cat <<'PY-EOF'\npytest\nPY-EOF\"",
        "echo $((1<<3))",
        "cat <<< 'data'",
        "echo 'unterminated",
        "# pytest; go test ./...\necho ok",
        "echo $(pytest)",
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
        ("go test -run 'A|B' ./...", False),
        ("pytest -k 'A;B'", False),
        ("pytest -k 'A||true'", False),
        ("bash -lc \"go test -run 'A|B' ./...\"", False),
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
    assert result["verification_commands"] == 2
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
    if not claude:
        trace = trace.replace('"assistant"', '"response_item"')
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


@pytest.mark.parametrize("payload", [
    {"type": "collab_agent_spawn_end", "new_thread_id": "lost"},
    {"type": "item_completed", "item": {
        "type": "SubAgentActivity", "kind": "started", "agent_thread_id": "lost",
    }},
])
def test_missing_codex_subagent_trace_fails_closed(
    tmp_path: Path, payload: dict[str, object],
) -> None:
    _write(
        tmp_path / "harbor/trial/agent/sessions/root.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "root"}},
            {
                "type": "event_msg",
                "payload": payload,
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


def _native_item(item: dict[str, object], *, completed: bool = True) -> dict[str, object]:
    return {
        "type": "event_msg",
        "payload": {"type": "item_completed" if completed else "item_started", "item": item},
    }


def test_codex_code_mode_uses_native_items_once_per_thread(tmp_path: Path) -> None:
    sessions = tmp_path / "harbor/trial/agent/sessions"
    metadata: list[dict[str, object]] = [
        {"type": kind} for kind in [
            "configuration_update", "world_state", "token_usage_record",
            "inter_agent_communication_metadata",
        ]
    ]
    messages: list[dict[str, object]] = [
        {"type": "response_item", "payload": {"type": kind}} for kind in [
            "additional_tools", "agent_message",
        ]
    ]
    for identity, parent in [("root", None), ("child", "root")]:
        _write(sessions / f"{identity}.jsonl", [
            {"type": "session_meta", "payload": {
                "id": identity, "parent_thread_id": parent, "cwd": "/workspace",
            }},
            *metadata, *messages,
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "exec",
                "input": 'await tools.exec_command({cmd: "pytest"})',
            }},
            _native_item({"type": "CommandExecution", "id": "a",
                          "command": ["bash", "-lc", "pytest"], "exit_code": 1}),
            _native_item({"type": "FileChange", "id": "b", "status": "completed",
                          "changes": {"/tmp/check.py": {"type": "add"}}}),
            _native_item({"type": "CommandExecution", "id": "c",
                          "command": ["bash", "-lc", "pytest"], "exit_code": 0}),
            _native_item({"type": "FileChange", "id": "d", "status": "completed",
                          "changes": {"/workspace/src/a.py": {"type": "update"}}}),
            _native_item({"type": "CommandExecution", "id": "e", "status": "declined",
                          "command": ["bash", "-lc", "pytest"], "exit_code": None}),
            _native_item({"type": "CommandExecution", "id": "f", "status": "cancelled",
                          "command": ["bash", "-lc", "pytest"], "exit_code": None}),
            _native_item({"type": "CommandExecution", "id": "g",
                          "command": ["bash", "-lc", "pytest"], "exit_code": 0}),
            {"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "exec", "output": "ok",
            }},
        ])
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == 6
    assert result["iterations"] == 2
    assert result["failed_verification_commands"] == 2


@pytest.mark.parametrize("claude", [True, False])
def test_unresolved_final_verification_is_counted_with_unknown_outcome(
    tmp_path: Path, claude: bool,
) -> None:
    events = (
        [*_tool("a", "Bash", {"command": "pytest"}),
         *_tool("b", "Edit", {"file_path": "src/a.py"}),
         *_tool("c", "Bash", {"command": "pytest"})[:1]]
        if claude else
        [*_command("a", "pytest"), *_patch("b", "src/a.py"), *_command("c", "pytest")[:1]]
    )
    _write(tmp_path / "stdout.jsonl", events)
    result = measure_attempt(tmp_path, claude=claude)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == 2
    assert result["iterations"] == 1
    assert result["known_failed_verification_commands"] == 0
    assert result["failed_verification_commands"] is None
    assert result["unknown_exit_commands"] == 1


def test_codex_unresolved_native_command_keeps_start_order(tmp_path: Path) -> None:
    _write(tmp_path / "harbor/trial/agent/sessions/root.jsonl", [
        {"type": "session_meta", "payload": {"id": "root", "cwd": "/app"}},
        _native_item({"type": "CommandExecution", "id": "a",
                      "command": ["bash", "-lc", "pytest"]}, completed=False),
    ])
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == result["unknown_exit_commands"] == 1
    assert result["failed_verification_commands"] is None


def test_claude_tool_error_without_exit_status_keeps_the_command_unknown(tmp_path: Path) -> None:
    _write(tmp_path / "stdout.jsonl", _tool(
        "a", "Bash", {"command": "pytest"}, error=True, output="Tool transport error",
    ))
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == result["unknown_exit_commands"] == 1
    assert result["failed_verification_commands"] is None


def test_claude_metadata_unrelated_subagents_and_workspace_boundary(tmp_path: Path) -> None:
    sessions = tmp_path / "harbor/trial/agent/sessions/projects/workspace"
    metadata: list[dict[str, object]] = [
        {"type": kind, "cwd": "/workspace"} for kind in [
            "attachment", "atis-latch", "api-request", "api-request-blob", "api-request-shape",
            "tools-used", "cost-state",
        ]
    ]
    _write(sessions / "root.jsonl", [
        *metadata,
        *_tool("a", "Bash", {"command": "pytest"}),
        *_tool("b", "Write", {"file_path": "/tmp/check.py"}),
        *_tool("c", "Edit", {"file_path": "/workspace-other/a.py"}),
        *_tool("d", "Edit", {"file_path": "../outside/a.py"}),
        *_tool("e", "Bash", {"command": "pytest"}),
        *_tool("f", "Edit", {"file_path": "/workspace/src/a.py"}),
        *_tool("g", "Bash", {"command": "pytest"}),
    ])
    _write(sessions / "root/subagents/agent-unrelated.jsonl", [{"type": "attachment"}])
    _write(tmp_path / "stdout.jsonl", [{"type": "tool_progress"}])
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == 3
    assert result["iterations"] == 1


def test_claude_missing_matched_subagent_metadata_reports_relative_paths(tmp_path: Path) -> None:
    sessions = tmp_path / "harbor/trial/agent/sessions"
    _write(sessions / "root.jsonl", _tool("spawn", "Agent", {}))
    _write(sessions / "root/subagents/agent-child.jsonl", [{"type": "attachment"}])
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "incomplete"
    assert "subagent transcripts" in str(result["reason"])
    assert str(tmp_path) not in str(result["reason"])
    # Exercise an OS error containing an absolute artifact path.
    (sessions / "root.jsonl").unlink()
    (sessions / "root.jsonl").mkdir()
    result = measure_attempt(tmp_path, claude=True)
    assert result["status"] == "incomplete"
    assert str(tmp_path) not in str(result["reason"])
    assert "harbor/trial/agent/sessions/root.jsonl" in str(result["reason"])


def test_older_harbor_trial_directory_is_read(tmp_path: Path) -> None:
    _write(tmp_path / "harbor/task-trial/agent/sessions/root.jsonl", [
        {"type": "session_meta", "payload": {"id": "root"}}, *_command("a", "pytest"),
    ])
    result = measure_attempt(tmp_path, claude=False)
    assert result["status"] == "complete", result
    assert result["verification_commands"] == 1
