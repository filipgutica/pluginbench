from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pluginbench import codex, processes, isolation


def test_codex_final_message_excludes_earlier_progress_for_structured_review() -> None:
    stdout = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "I will inspect the source."},
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"issues": []}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}},
        )
    )
    parsed = codex.parse_events(stdout, [], "codex")
    assert json.loads(parsed["final"]) == {"issues": []}
    assert parsed["agent_messages"] == ["I will inspect the source.", '{"issues": []}']
    assert parsed["usage"] == {"input_tokens": 20, "output_tokens": 10}


def stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_cli_version_is_observed_from_consistent_native_session_metadata() -> None:
    traces = [
        stream(
            {"type": "session_meta", "payload": {"cli_version": "0.134.0"}},
            {"type": "turn_context", "payload": {"cli_version": "not-session-evidence"}},
        ),
        stream({"type": "session_meta", "payload": {"cli_version": "0.134.0"}}),
    ]
    parsed = codex.parse_events("", traces, "codex")
    assert parsed["observed_cli_version"] == "0.134.0"
    archived = json.loads(json.dumps(parsed))
    assert codex.observed_cli_version(archived["startup"]) == "0.134.0"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"cli_version": ""},
        {"cli_version": "  "},
        {"cli_version": None},
        {"cli_version": 134},
        {"cli_version": "0.135.0"},
        False,
    ],
)
def test_missing_or_conflicting_session_cli_version_stays_unknown(payload: object) -> None:
    parsed = codex.parse_events(
        "",
        [
            stream(
                {"type": "session_meta", "payload": {"cli_version": "0.134.0"}},
                {"type": "session_meta", "payload": payload},
            )
        ],
        "codex",
    )
    assert parsed["observed_cli_version"] is None
    assert codex.observed_cli_version(parsed["startup"]) is None


@pytest.mark.parametrize(
    "startup",
    [
        None,
        {},
        [],
        [{"type": "turn_context", "payload": {"cli_version": "0.134.0"}}],
        [{"type": "system", "subtype": "init", "cli_version": "0.134.0"}],
        [{"harness_version": "codex-cli 0.134.0"}],
    ],
)
def test_cli_version_without_native_session_metadata_is_unknown(startup: object) -> None:
    assert codex.observed_cli_version(startup) is None
    assert codex.parse_events("", [], "codex")["observed_cli_version"] is None


def test_turn_usage_is_summed_and_missing_fields_stay_unknown() -> None:
    parsed = codex.parse_events(
        stream(
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
            {"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}},
        ),
        [],
        "codex",
        requested_effort="high",
    )
    assert parsed["usage"] == {"input_tokens": 14, "output_tokens": 5}
    assert parsed["actual_primary_models"] == []
    assert parsed["runtime_effort_evidence"] == []
    assert parsed["requested_effort"] == "high"
    missing = codex.parse_events(
        stream(
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
            {"type": "turn.completed", "usage": {"input_tokens": 4}},
        ),
        [],
        "codex",
    )
    assert missing["usage"] == {"input_tokens": 14}


@pytest.mark.parametrize(
    "bad_trace",
    [
        "{broken",
        "[]",
        '{"type":"turn_context","payload":false}',
        '{"type":"turn_context","payload":{"model":42}}',
    ],
)
def test_malformed_trace_cannot_count_as_success(bad_trace: str) -> None:
    parsed = codex.parse_events(
        stream({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        [bad_trace],
        "codex",
    )
    assert parsed["stream_errors"]
    assert not parsed["successful_native_completion"]


@pytest.mark.parametrize(
    "name", ["AGENTS.md", "AGENTS.override.md", "CLAUDE.md", ".agents", ".claude", ".codex"]
)
def test_fixture_guidance_is_rejected_even_when_nested(tmp_path: Path, name: str) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "nested").mkdir(parents=True)
    (workspace / "nested" / name).touch()
    with pytest.raises(ValueError, match="custom guidance"):
        codex.reject_guidance(workspace, include_ancestors=False, reject_git=True)


@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md"])
def test_guidance_guard_checks_ancestors_and_rejects_git_or_symlinks(
    tmp_path: Path, name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / name).touch()
    codex.reject_guidance(workspace, include_ancestors=False)
    with pytest.raises(ValueError, match=name):
        codex.reject_guidance(workspace)
    (tmp_path / name).unlink()
    (workspace / ".git").mkdir()
    codex.reject_guidance(workspace, include_ancestors=False)
    with pytest.raises(ValueError, match=".git"):
        codex.reject_guidance(workspace, include_ancestors=False, reject_git=True)
    (workspace / ".git").rmdir()
    (workspace / "linked").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="linked"):
        codex.reject_guidance(workspace, include_ancestors=False)


def test_preflight_checks_help_without_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    required = (
        "--ignore-user-config --ignore-rules --json --model --config "
        "--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --cd"
    )
    calls = []

    def native(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="codex-cli test" if argv == ["codex", "--version"] else required
        )

    monkeypatch.setattr(subprocess, "run", native)
    receipt = codex.preflight()
    assert receipt["harness_version"] == "codex-cli test" and receipt["paid_calls"] == 0
    assert calls == [["codex", "--version"], ["codex", "exec", "--help"]]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="old cli"),
    )
    with pytest.raises(ValueError, match="lacks required isolation flags"):
        codex.preflight()


@pytest.mark.parametrize("exit_code", [0, 2])
def test_invoke_uses_explicit_auth_and_effort_and_removes_private_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    token = "fake-token-which-must-never-remain-in-evidence"
    auth = tmp_path / "selected-auth.json"
    auth.write_text(json.dumps({"token": token}))
    script = (
        "import json,os,pathlib,sys; "
        "home=pathlib.Path(os.environ['CODEX_HOME']); "
        "token=json.loads((home/'auth.json').read_text())['token']; "
        "(home/'sessions').mkdir(); "
        "(home/'sessions/test.jsonl').write_text(json.dumps({'type':'session_meta',"
        "'payload':{'cli_version':'0.134.0'}})+'\\n'+json.dumps({'type':'turn_context',"
        "'payload':{'model':'observed-model','effort':'low'}})); "
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':token}})); "
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3}})); "
        f"sys.exit({exit_code})"
    )
    observed = {}

    def command(**kwargs: object) -> list[str]:
        observed.update(kwargs)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(codex, "command", command)
    result = codex.invoke(
        area=area,
        entry={"host": "codex", "arm": "baseline"},
        protocol={
            "models": {"codex": "requested-model"},
            "reasoning": "high",
            "auth_file": str(auth),
        },
        prompt="task",
        timeout=5,
    )
    assert observed["effort"] == "high"
    assert result["actual_primary_models"] == ["observed-model"]
    assert result["observed_cli_version"] == "0.134.0"
    evidence = json.loads((area / "artifacts/host-evidence.json").read_text())
    assert evidence["observed_cli_version"] == "0.134.0"
    assert codex.observed_cli_version(evidence["startup"]) == "0.134.0"
    assert result["runtime_effort_evidence"] == ["low"]
    assert result["requested_effort"] == "high"
    assert result["successful_native_completion"] is (exit_code == 0)
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 3}
    assert result["private_runtime_removed"] and not (area / "private").exists()
    for artifact in (area / "artifacts").iterdir():
        assert token not in artifact.read_text()
    assert auth.exists()


def test_missing_auth_removes_private_runtime(tmp_path: Path) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        codex.invoke(
            area=area,
            entry={"host": "codex", "arm": "baseline"},
            protocol={"models": {"codex": "test"}, "auth_file": str(tmp_path / "missing")},
            prompt="task",
            timeout=1,
        )
    assert not (area / "private").exists()
    assert json.loads((area / "artifacts/execution.json").read_text())["private_runtime_removed"]


@pytest.mark.parametrize("link_kind", ["file", "sessions", "codex"])
def test_native_session_links_never_copy_host_data_and_still_remove_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_kind: str
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    outside = tmp_path / "operator-private"
    (outside / "sessions").mkdir(parents=True)
    secret = outside / "sessions/private.jsonl"
    secret.write_text("outside-job-secret must not be read by the host launcher")
    token = "fake-token-that-must-not-be-archived"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"token": token}))
    setup = {
        "file": f"sessions.mkdir(); (sessions/'trace.jsonl').symlink_to({str(secret)!r})",
        "sessions": f"sessions.symlink_to({str(outside / 'sessions')!r})",
        "codex": f"shutil.rmtree(home); home.symlink_to({str(outside)!r})",
    }[link_kind]
    script = (
        "import os,pathlib,shutil; "
        "home=pathlib.Path(os.environ['CODEX_HOME']); sessions=home/'sessions'; " + setup
    )
    monkeypatch.setattr(codex, "command", lambda **kwargs: [sys.executable, "-c", script])
    with pytest.raises(ValueError, match="artifact|session"):
        codex.invoke(
            area=area,
            entry={"host": "codex", "arm": "baseline"},
            protocol={"models": {"codex": "test"}, "auth_file": str(auth)},
            prompt="task",
            timeout=5,
        )
    assert not (area / "private").exists()
    assert secret.read_text() == "outside-job-secret must not be read by the host launcher"
    receipt = json.loads((area / "artifacts/execution.json").read_text())
    assert receipt["private_runtime_removed"]
    for artifact in (area / "artifacts").rglob("*"):
        if artifact.is_file() and not artifact.is_symlink():
            assert secret.read_bytes() not in artifact.read_bytes()
            assert token.encode() not in artifact.read_bytes()


@pytest.mark.parametrize("native_failure", [False, True])
def test_cleanup_failure_still_removes_auth_scrubs_evidence_and_preserves_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_failure: bool
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    token = "fake-credential-on-cleanup-error"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"token": token}))
    script = "import os; from pathlib import Path; print(Path(os.environ['CODEX_HOME'], 'auth.json').read_text())"
    monkeypatch.setattr(codex, "command", lambda **kwargs: [sys.executable, "-c", script])
    cleanup = processes.terminate_process_group

    def failed_cleanup(proc: subprocess.Popen[str]) -> None:
        cleanup(proc)
        raise PermissionError("cleanup probe")

    monkeypatch.setattr(codex, "terminate_process_group", failed_cleanup)
    if native_failure:
        communicate = subprocess.Popen.communicate

        def failed_native(
            proc: subprocess.Popen[str], input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            communicate(proc, input, timeout)
            raise OSError("native probe")

        monkeypatch.setattr(subprocess.Popen, "communicate", failed_native)
    expected = "native probe" if native_failure else "cleanup probe"
    with pytest.raises(OSError, match=expected) as failure:
        codex.invoke(
            area=area,
            entry={"host": "codex", "arm": "baseline"},
            protocol={"models": {"codex": "test"}, "auth_file": str(auth)},
            prompt="task",
            timeout=5,
        )
    if native_failure:
        assert any("Process-group cleanup failed" in note for note in failure.value.__notes__)
    receipt = json.loads((area / "artifacts/execution.json").read_text())
    assert receipt["cleanup_error"] == "cleanup probe"
    assert receipt["private_runtime_removed"] and not (area / "private").exists()
    assert auth.is_file()
    assert all(token not in path.read_text() for path in (area / "artifacts").iterdir())


def test_changed_runtime_is_fatal_before_native_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    auth = tmp_path / "auth.json"
    auth.write_text('{"token":"fake-auth"}')
    expected = {**isolation.runtime_identity(), "os_release": "different-runtime"}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("cannot launch"))
    with pytest.raises(isolation.RuntimeDriftError, match="runtime changed"):
        codex.invoke(
            area=area,
            entry={"host": "codex", "arm": "candidate"},
            protocol={"models": {"codex": "test"}, "auth_file": str(auth), "isolation": expected},
            prompt="task",
            timeout=5,
        )
    assert not (area / "private").exists()
    assert json.loads((area / "artifacts/execution.json").read_text())["exit_code"] is None


@pytest.mark.skipif(shutil.which("codex") is None, reason="native Codex sandbox probe requires CLI")
def test_readonly_invocation_uses_one_sandbox_and_cannot_escape_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    area = tmp_path / "trial"
    packet = area / "workspace"
    packet.mkdir(parents=True)
    source = packet / "packet.txt"
    source.write_text("bounded packet")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside packet")
    auth = tmp_path / "auth.json"
    auth.write_text('{"token":"fake-auth"}')
    script = f"""import json, os
from pathlib import Path
p = Path({str(source)!r}); assert p.read_text() == 'bounded packet'
for path, operation in ((p, 'write'), (Path({str(outside)!r}), 'read'), (Path({str(outside)!r}), 'write')):
    try:
        path.read_text() if operation == 'read' else path.write_text('changed')
    except OSError:
        continue
    raise AssertionError('escaped packet boundary')
Path(os.environ['TMPDIR'], 'scratch.txt').write_text('private storage allowed')
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':0,'output_tokens':0}}}}))
"""
    def probe(**kwargs: object) -> list[str]:
        # Exercise the CLI's actual sandbox entry without invoking any model.
        return [
            "codex",
            "sandbox",
            "-P",
            ":danger-full-access",
            "-C",
            str(packet),
            "--",
            sys.executable,
            "-c",
            script,
        ]

    monkeypatch.setattr(codex, "command", probe)
    result = codex.invoke(
        area=area,
        entry={"host": "codex", "arm": "baseline"},
        protocol={"models": {"codex": "setup-only"}, "auth_file": str(auth)},
        prompt="No model calls: sandbox boundary probe only.",
        timeout=15,
    )
    assert result["exit_code"] == 0, (area / "artifacts/stderr.log").read_text()
    assert source.read_text() == "bounded packet"
    assert outside.read_text() == "outside packet"
    assert result["private_runtime_removed"] and not (area / "private").exists()


def test_invocation_cannot_overwrite_existing_execution_evidence(tmp_path: Path) -> None:
    area = tmp_path / "trial"
    artifacts = area / "artifacts"
    artifacts.mkdir(parents=True)
    existing = artifacts / "execution.json"
    existing.write_text('{"exit_code":0}')
    with pytest.raises(ValueError, match="fresh trial area"):
        codex.invoke(
            area=area,
            entry={"host": "codex", "arm": "baseline"},
            protocol={"models": {"codex": "test"}},
            prompt="task",
            timeout=1,
        )
    assert existing.read_text() == '{"exit_code":0}'


def test_truncated_trace_reports_usage_lower_bound_without_completed_usage() -> None:
    trace = (
        stream(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 3}},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 15, "output_tokens": 5}},
                },
            },
        )
        + "\n{truncated"
    )
    parsed = codex.parse_events("", [trace], "codex")
    assert parsed["usage"] is None
    assert not parsed["successful_native_completion"]
    assert parsed["partial_usage"][0]["tokens"] == {"input_tokens": 15, "output_tokens": 5}
    assert parsed["partial_usage"][0]["trace_sha256"].startswith("sha256:")


def native_session(
    identity: str,
    totals: list[dict[str, int]],
    *,
    parent: str | None = None,
    completed: bool = True,
    extra: list[dict[str, object]] | None = None,
) -> str:
    # Shapes come from Codex rust-v0.162.1 protocol/src/protocol.rs.
    metadata: dict[str, object] = {"id": identity, "cli_version": "0.162.1", "source": "exec"}
    if parent is not None:
        metadata["source"] = {
            "subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}
        }
    events: list[dict[str, object]] = [
        {"type": "session_meta", "payload": metadata},
        {
            "type": "turn_context",
            "payload": {
                "model": "primary" if parent is None else "child-model",
                "effort": "high" if parent is None else "low",
            },
        },
    ]
    events.extend(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": total},
            },
        }
        for total in totals
    )
    events.extend(extra or [])
    if completed:
        events.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    return stream(*events)


def test_native_descendant_usage_counts_session_totals_once_and_keeps_primary_identity() -> None:
    root = native_session(
        "root",
        [
            {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
            {"input_tokens": 30, "cached_input_tokens": 5, "output_tokens": 8},
        ],
    )
    child = native_session(
        "child",
        [
            {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 4},
        ],
        parent="root",
    )
    stdout = stream(
        {"type": "thread.started", "thread_id": "root"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 30,
                "cached_input_tokens": 5,
                "output_tokens": 8,
            },
        },
    )
    parsed = codex.parse_events(
        stdout, [root, child, child, child.rsplit("\n", 1)[0]], "codex", include_descendants=True
    )
    assert parsed["usage"] == {"input_tokens": 37, "cached_input_tokens": 7, "output_tokens": 12}
    assert parsed["actual_primary_models"] == ["primary"]
    assert parsed["actual_descendant_models"] == ["child-model"]
    assert parsed["runtime_effort_evidence"] == ["high"]
    assert parsed["usage_accounting"]["status"] == "complete"
    assert len(parsed["usage_accounting"]["sessions"]) == 2
    assert parsed["successful_native_completion"]


@pytest.mark.parametrize("spawn_evidence", ["trace", "stdout"])
def test_native_missing_descendant_keeps_observed_root_as_lower_bound(spawn_evidence: str) -> None:
    spawn: dict[str, object] = {
        "type": "event_msg",
        "payload": {
            "type": "collab_agent_spawn_end",
            "sender_thread_id": "root",
            "new_thread_id": "missing",
        },
    }
    stdout: list[dict[str, object]] = [
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}
    ]
    if spawn_evidence == "stdout":
        stdout.append(
            {
                "type": "item.completed",
                "item": {
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "receiver_thread_ids": ["missing"],
                },
            }
        )
    root = native_session(
        "root",
        [{"input_tokens": 10, "output_tokens": 3}],
        extra=[spawn] if spawn_evidence == "trace" else [],
    )
    parsed = codex.parse_events(stream(*stdout), [root], "codex", include_descendants=True)
    assert parsed["usage"] is None
    accounting = parsed["usage_accounting"]
    assert accounting["observed_usage_lower_bound"] == {"input_tokens": 10, "output_tokens": 3}
    assert "missing descendant session missing" in accounting["reasons"]
    assert accounting["status"] == "incomplete"


def test_native_failed_descendant_keeps_partial_tokens_without_claiming_complete_usage() -> None:
    root = native_session("root", [{"input_tokens": 10, "output_tokens": 3}])
    child = native_session(
        "child",
        [{"input_tokens": 8, "output_tokens": 2}],
        parent="root",
        completed=False,
        extra=[
            {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}},
        ],
    )
    parsed = codex.parse_events(
        stream({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}),
        [root, child],
        "codex",
        include_descendants=True,
    )
    assert parsed["usage"] is None
    accounting = parsed["usage_accounting"]
    assert accounting["observed_usage_lower_bound"] == {"input_tokens": 18, "output_tokens": 5}
    assert "incomplete or failed session child" in accounting["reasons"]
    assert accounting["sessions"][0]["status"] == "incomplete"


def test_native_multiple_roots_and_conflicting_session_copies_are_not_complete() -> None:
    root = native_session("root", [{"input_tokens": 10, "output_tokens": 3}])
    other = native_session("other", [{"input_tokens": 20, "output_tokens": 5}])
    conflict = native_session("root", [{"input_tokens": 11, "output_tokens": 3}])
    parsed = codex.parse_events("", [root, other, conflict], "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert parsed["actual_primary_models"] == []
    assert "expected exactly one root session" in parsed["usage_accounting"]["reasons"]
    assert "conflicting traces for session root" in parsed["usage_accounting"]["reasons"]


def test_native_completed_child_without_usage_is_unknown_instead_of_zero() -> None:
    root = native_session("root", [{"input_tokens": 10, "output_tokens": 3}])
    child = native_session("child", [], parent="root")
    parsed = codex.parse_events("", [root, child], "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert "missing usage for session child" in parsed["usage_accounting"]["reasons"]
    assert parsed["usage_accounting"]["sessions"][0]["observed_usage"] is None


@pytest.mark.parametrize(
    "extra, reason",
    [
        ([{"type": "event_msg", "payload": {"type": "task_started"}}], "incomplete or failed"),
        (
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "collab_agent_spawn_begin",
                        "call_id": "unresolved",
                    },
                }
            ],
            "unresolved spawns",
        ),
        (
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": -1}},
                    },
                }
            ],
            "invalid cumulative usage",
        ),
    ],
)
def test_native_incomplete_activity_after_completion_is_not_complete_usage(
    extra: list[dict[str, object]], reason: str
) -> None:
    root = native_session("root", [{"input_tokens": 10, "output_tokens": 3}])
    parsed = codex.parse_events(
        "", [root + "\n" + stream(*extra)], "codex", include_descendants=True
    )
    assert parsed["usage"] is None
    assert any(reason in message for message in parsed["usage_accounting"]["reasons"])


def test_native_stdout_identity_mismatch_and_inherited_history_cannot_supply_lower_bound() -> None:
    root = native_session("root", [{"input_tokens": 10, "output_tokens": 3}])
    events = [json.loads(line) for line in root.splitlines()]
    events[0]["payload"]["forked_from_id"] = "previous-session"
    parsed = codex.parse_events(
        stream({"type": "thread.started", "thread_id": {"invalid": "identity"}}),
        [stream(*events)],
        "codex",
        include_descendants=True,
    )
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["observed_usage_lower_bound"] is None
    assert (
        "inherited history is unsupported for session root" in parsed["usage_accounting"]["reasons"]
    )
    assert "invalid stdout root identity" in parsed["usage_accounting"]["reasons"]
