from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pluginbench import codex, codex_events, processes, isolation


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
    parsed = codex.parse_events(
        stdout, [native_session("root", [{"input_tokens": 20, "output_tokens": 10}])], "codex"
    )
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


def test_turn_usage_is_checked_against_trace_totals_and_missing_fields_stay_unknown() -> None:
    parsed = codex.parse_events(
        stream(
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
            {"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}},
        ),
        [native_session("root", [{"input_tokens": 14, "output_tokens": 5}])],
        "codex",
        requested_effort="high",
    )
    assert parsed["usage"] == {"input_tokens": 14, "output_tokens": 5}
    assert parsed["actual_primary_models"] == ["primary"]
    assert parsed["runtime_effort_evidence"] == ["high"]
    assert parsed["requested_effort"] == "high"
    missing = codex.parse_events(
        stream(
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
            {"type": "turn.completed", "usage": {"input_tokens": 4}},
        ),
        [native_session("root", [{"input_tokens": 14, "output_tokens": 5}])],
        "codex",
    )
    assert missing["usage"] is None
    assert "invalid turn usage" in missing["stream_errors"]


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
        stream({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}),
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
@pytest.mark.parametrize("persistent_artifacts", [False, True])
def test_invoke_uses_explicit_auth_and_effort_and_removes_private_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, persistent_artifacts: bool
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    token = "fake-token-which-must-never-remain-in-evidence"
    auth = tmp_path / "selected-auth.json"
    auth.write_text(json.dumps({"token": token}))
    trace = native_session("root", [{"input_tokens": 10, "output_tokens": 3}])
    trace = trace.replace('"0.162.1"', '"0.134.0"').replace('"primary"', '"observed-model"')
    trace = trace.replace('"high"', '"low"')
    script = (
        "import json,os,pathlib,sys; "
        "home=pathlib.Path(os.environ['CODEX_HOME']); "
        "token=json.loads((home/'auth.json').read_text())['token']; "
        "(home/'sessions').mkdir(); "
        f"(home/'sessions/test.jsonl').write_text({trace!r}); "
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':token}})); "
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3}})); "
        f"sys.exit({exit_code})"
    )
    observed = {}

    def command(**kwargs: object) -> list[str]:
        observed.update(kwargs)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(codex, "command", command)
    artifacts = tmp_path / "call-1" if persistent_artifacts else area / "artifacts"
    if persistent_artifacts:
        artifacts.mkdir()  # The judge reserves this directory before launching.
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
        **({"artifacts": artifacts} if persistent_artifacts else {}),
    )
    assert observed["effort"] == "high"
    assert result["actual_primary_models"] == ["observed-model"]
    assert result["observed_cli_version"] == "0.134.0"
    evidence = json.loads((artifacts / "host-evidence.json").read_text())
    assert evidence["observed_cli_version"] == "0.134.0"
    assert codex.observed_cli_version(evidence["startup"]) == "0.134.0"
    assert result["runtime_effort_evidence"] == ["low"]
    assert result["requested_effort"] == "high"
    assert result["successful_native_completion"] is (exit_code == 0)
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 3}
    assert result["private_runtime_removed"] and not (area / "private").exists()
    for artifact in artifacts.iterdir():
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


def test_truncated_trace_without_session_identity_cannot_supply_usage_or_lower_bound() -> None:
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
    assert parsed["partial_usage"] == []
    assert "trace lacks one unambiguous session identity" in (
        parsed["usage_accounting"]["reasons"]
    )


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


def test_a_hooked_saved_codex_run_checks_the_session_usage_kept_at_launch(tmp_path: Path) -> None:
    from pluginbench.saved_usage import revalidate_usage

    counts = {"input_tokens": 10, "output_tokens": 5}
    trace = native_session("root", [counts])
    stdout = stream({"type": "turn.completed", "usage": counts})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "stdout.jsonl").write_text(stdout)
    (artifacts / "host-trace-0.jsonl").write_text(trace)
    receipt = tmp_path / "execution.json"
    receipt.write_text("{}")
    # At launch a hooked run keeps the session usage as native_session_usage and clears usage.
    execution: dict[str, object] = {
        "launch": {"harness": "codex-cli"},
        "native_treatment": {"hooks": {"hooks": [{"event": "SessionStart"}]}},
        "native_session_usage": codex.parse_events(stdout, [trace], "codex")["usage"],
        "usage": None,
    }
    revalidated = revalidate_usage(execution, receipt)
    assert revalidated["usage"] is None
    assert revalidated["usage_validation"]["status"] == "unavailable"
    assert revalidated["usage_validation"]["reasons"] == [
        "command-hook subprocess usage is not independently metered"
    ]
    execution["native_session_usage"] = {"input_tokens": 1, "output_tokens": 4}
    revalidated = revalidate_usage(execution, receipt)
    assert revalidated["usage"] is None
    assert revalidated["usage_validation"]["reasons"] == [
        "revalidated usage disagrees with recorded usage",
        "command-hook subprocess usage is not independently metered",
    ]


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
    assert parsed["actual_descendant_efforts"] == ["low"]
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


def inherited_native_child() -> list[dict[str, object]]:
    """Minimal 0.162.1 paginated child history, with an explicit own-history boundary."""
    events: list[dict[str, object]] = [
        {
            "type": "session_meta",
            "payload": {
                "id": "child",
                "session_id": "root",
                "cli_version": "0.162.1",
                "history_mode": "paginated",
                "forked_from_id": "root",
                "parent_thread_id": "root",
                "subagent_history_start_ordinal": 4,
                "agent_path": "/root/check",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "root",
                            "depth": 1,
                            "agent_path": "/root/check",
                        }
                    }
                },
            },
        },
        {"type": "session_meta", "payload": {"id": "root"}},
        {"type": "turn_context", "payload": {"model": "inherited-model", "effort": "low"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 9000, "output_tokens": 100},
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "child-turn"}},
        {
            "type": "turn_context",
            "payload": {
                "model": "child-model",
                "effort": "low",
                "turn_id": "child-turn",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 7, "output_tokens": 2},
                    "last_token_usage": {"input_tokens": 7, "output_tokens": 2},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 11, "output_tokens": 3},
                    "last_token_usage": {"input_tokens": 4, "output_tokens": 1},
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "child-turn"}},
    ]
    return [{**event, "ordinal": ordinal} for ordinal, event in enumerate(events)]


def named_native_spawn() -> list[dict[str, object]]:
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "call_id": "spawn-call",
                "arguments": "{}",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "spawn-call",
                "output": '{"task_name":"/root/check"}',
            },
        },
    ]


@pytest.mark.parametrize("version", ["0.162.1", "0.167.0", "0.167.1"])
def test_native_inherited_child_counts_only_own_history_and_deduplicates_copies(
    version: str,
) -> None:
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    events = inherited_native_child()
    metadata = events[0]["payload"]
    assert isinstance(metadata, dict)
    metadata["cli_version"] = version
    child = stream(*events)
    parsed = codex.parse_events(
        "", [root, child, child, stream(*events[:-1])], "codex", include_descendants=True
    )
    assert parsed["usage"] == {"input_tokens": 31, "output_tokens": 8}
    assert parsed["actual_primary_models"] == ["primary"]
    assert parsed["actual_descendant_models"] == ["child-model"]
    assert parsed["usage_accounting"]["status"] == "complete"
    assert len(parsed["usage_accounting"]["sessions"]) == 2


def test_native_named_spawn_requires_captured_child_even_without_legacy_spawn_events() -> None:
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    parsed = codex.parse_events("", [root], "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert (
        "missing descendant agent /root/check from session root"
        in parsed["usage_accounting"]["reasons"]
    )


def test_native_spawn_override_is_descendant_evidence_without_the_child_trace() -> None:
    spawn = named_native_spawn()
    call = spawn[0]["payload"]
    assert isinstance(call, dict)
    call["arguments"] = '{"model":"other-model","reasoning_effort":"low"}'
    root = native_session("root", [{"input_tokens": 20, "output_tokens": 5}], extra=spawn)
    parsed = codex.parse_events("", [root], "codex", include_descendants=True)
    assert parsed["actual_descendant_models"] == ["other-model"]
    assert parsed["actual_descendant_efforts"] == ["low"]


@pytest.mark.parametrize("boundary", [None, True, -1, 0, 2, 5, 99])
def test_native_inherited_child_rejects_missing_or_invalid_own_history_boundary(
    boundary: object,
) -> None:
    child = inherited_native_child()
    metadata = child[0]["payload"]
    assert isinstance(metadata, dict)
    metadata["subagent_history_start_ordinal"] = boundary
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    parsed = codex.parse_events("", [root, stream(*child)], "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["status"] == "incomplete"


@pytest.mark.parametrize(
    "field, value",
    [
        ("forked_from_id", "other"),
        ("parent_thread_id", "other"),
        ("agent_path", "/root/other"),
        ("session_id", "other-root"),
        ("cli_version", "0.162.2"),
    ],
)
def test_native_inherited_child_requires_consistent_native_metadata(field: str, value: str) -> None:
    child = inherited_native_child()
    metadata = child[0]["payload"]
    assert isinstance(metadata, dict)
    metadata[field] = value
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    parsed = codex.parse_events("", [root, stream(*child)], "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["status"] == "incomplete"


def test_native_named_spawn_conflicting_result_cannot_claim_complete_usage() -> None:
    spawn = named_native_spawn()
    spawn.append(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "spawn-call",
                "output": '{"task_name":"/root/other"}',
            },
        }
    )
    root = native_session("root", [{"input_tokens": 20, "output_tokens": 5}], extra=spawn)
    parsed = codex.parse_events(
        "", [root, stream(*inherited_native_child())], "codex", include_descendants=True
    )
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["status"] == "incomplete"


@pytest.mark.parametrize(
    "corruption", ["extra_header", "ordinal_gap", "inherited_counter", "wrong_turn"]
)
def test_native_inherited_child_rejects_ambiguous_own_suffix(corruption: str) -> None:
    child = inherited_native_child()
    if corruption == "extra_header":
        child.append({"type": "session_meta", "payload": {"id": "other"}, "ordinal": len(child)})
    elif corruption == "ordinal_gap":
        child[-1]["ordinal"] = 50
    else:
        payload = child[6 if corruption == "inherited_counter" else 8]["payload"]
        assert isinstance(payload, dict)
        if corruption == "inherited_counter":
            info = payload["info"]
            assert isinstance(info, dict)
            info["last_token_usage"] = {"input_tokens": 2, "output_tokens": 1}
        else:
            payload["turn_id"] = "inherited-turn"
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    parsed = codex.parse_events("", [root, stream(*child)], "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["observed_usage_lower_bound"] is None


def test_native_named_spawn_rejects_two_session_identities_for_one_child_path() -> None:
    child = inherited_native_child()
    second = inherited_native_child()
    metadata = second[0]["payload"]
    assert isinstance(metadata, dict)
    metadata["id"] = "second-child"
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    parsed = codex.parse_events(
        "", [root, stream(*child), stream(*second)], "codex", include_descendants=True
    )
    assert parsed["usage"] is None
    assert (
        "conflicting descendant identities for agent /root/check from session root"
        in parsed["usage_accounting"]["reasons"]
    )


@pytest.mark.parametrize("version", ["0.162.1", "0.167.0", "0.167.1"])
def test_version_admission_accepts_versions_the_usage_parser_was_checked_against(
    version: str,
) -> None:
    codex.admit_version(version)


@pytest.mark.parametrize("include_descendants", [False, True])
def test_unverified_plain_session_is_measured_when_its_structure_matches(
    include_descendants: bool,
) -> None:
    trace = native_session("root", [{"input_tokens": 20, "output_tokens": 5}])
    trace = trace.replace('"0.162.1"', '"0.168.0"')
    parsed = codex.parse_events("", [trace], "codex", include_descendants=include_descendants)
    assert parsed["usage"] == {"input_tokens": 20, "output_tokens": 5}
    assert parsed["usage_accounting"]["status"] == "complete"


@pytest.mark.parametrize("include_descendants", [False, True])
def test_plain_paginated_gap_cannot_hide_a_missing_spawn(include_descendants: bool) -> None:
    trace = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}],
        extra=[{"type": "event_msg", "payload": {
            "type": "collab_agent_spawn_end", "new_thread_id": "missing-child",
        }}],
    )
    events = [json.loads(line) for line in trace.splitlines()]
    events[0]["payload"]["history_mode"] = "paginated"
    events = [{**event, "ordinal": ordinal} for ordinal, event in enumerate(events)]
    intact = codex.parse_events(
        "", [stream(*events)], "codex", include_descendants=include_descendants
    )
    assert "missing descendant session missing-child" in intact["usage_accounting"]["reasons"]
    del events[3]  # Remove the spawn without renumbering the surviving rollout records.
    damaged = codex.parse_events(
        "", [stream(*events)], "codex", include_descendants=include_descendants
    )
    assert damaged["usage"] is None
    assert damaged["partial_usage"] == []
    assert "noncontiguous rollout ordinals for session root" in (
        damaged["usage_accounting"]["reasons"]
    )


@pytest.mark.parametrize(
    "bad_counts",
    [
        {"cached_input_tokens": 40},
        {"cache_write_input_tokens": 40},
        {"reasoning_output_tokens": 6},
        {"total_tokens": 999999},
        {"cached_input_tokens": 40, "total_tokens": 999999},
    ],
)
def test_contradictory_counters_cannot_supply_exact_usage_or_a_lower_bound(
    bad_counts: dict[str, int],
) -> None:
    counts = {"input_tokens": 20, "output_tokens": 5, **bad_counts}
    parsed = codex.parse_events(
        "", [native_session("root", [counts])], "codex", include_descendants=True
    )
    assert parsed["usage"] is None
    assert parsed["partial_usage"] == []
    assert parsed["usage_accounting"]["observed_usage_lower_bound"] is None
    assert "invalid cumulative usage for session root" in parsed["usage_accounting"]["reasons"]


@pytest.mark.parametrize("channel", ["stdout", "trace"])
def test_invalid_stdout_or_child_counters_cannot_supply_a_lower_bound(channel: str) -> None:
    counts = {"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 40}
    stdout = stream({"type": "turn.completed", "usage": counts}) if channel == "stdout" else ""
    root = native_session("root", [{"input_tokens": 20, "output_tokens": 5}])
    traces = [root]
    if channel == "trace":
        traces.append(native_session("child", [counts], parent="root"))
    parsed = codex.parse_events(stdout, traces, "codex", include_descendants=True)
    assert parsed["usage"] is None
    assert parsed["partial_usage"] == []
    assert parsed["usage_accounting"]["observed_usage_lower_bound"] is None


@pytest.mark.parametrize("include_descendants", [False, True])
def test_unverified_inherited_history_makes_tokens_unavailable(
    include_descendants: bool,
) -> None:
    root = native_session(
        "root", [{"input_tokens": 20, "output_tokens": 5}], extra=named_native_spawn()
    )
    child = stream(*inherited_native_child()).replace('"0.162.1"', '"0.168.0"')
    parsed = codex.parse_events(
        stream({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}}),
        [root, child], "codex", include_descendants=include_descendants,
    )
    assert parsed["usage"] is None
    assert parsed["partial_usage"] == []
    assert "inherited-history layout of Codex 0.168.0 is unverified" in (
        parsed["usage_accounting"]["reasons"]
    )


@pytest.mark.parametrize("include_descendants", [False, True])
@pytest.mark.parametrize(
    "bad_event",
    [
        {"type": [], "payload": {}},
        {"type": "event_msg", "payload": {"type": []}},
        {"type": "event_msg", "payload": False},
        {"type": "event_msg", "payload": {"type": "token_count", "info": []}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 15}},
            },
        },
    ],
)
def test_malformed_session_makes_tokens_unavailable_without_crashing(
    bad_event: dict[str, object], include_descendants: bool,
) -> None:
    trace = native_session("root", [{"input_tokens": 20, "output_tokens": 5}])
    lines = trace.splitlines()
    lines.insert(2, json.dumps(bad_event))
    parsed = codex.parse_events(
        stream({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}}),
        ["\n".join(lines)], "codex", include_descendants=include_descendants,
    )
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["status"] == "incomplete"
    assert parsed["usage_accounting"]["reasons"]


@pytest.mark.parametrize("include_descendants", [False, True])
@pytest.mark.parametrize("header", ["missing", "late"])
def test_session_without_a_leading_identity_cannot_supply_tokens(
    header: str, include_descendants: bool,
) -> None:
    lines = native_session("root", [{"input_tokens": 20, "output_tokens": 5}]).splitlines()
    metadata = lines.pop(0)
    if header == "late":
        lines.insert(1, metadata)
    parsed = codex.parse_events(
        stream({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}}),
        ["\n".join(lines)], "codex", include_descendants=include_descendants,
    )
    assert parsed["usage"] is None
    assert "trace lacks one unambiguous session identity" in (
        parsed["usage_accounting"]["reasons"]
    )


# Minimized from a real attempt during a network outage: Codex retried, then failed the turn.
_OUTAGE: list[dict[str, object]] = [
    {"type": "thread.started", "thread_id": "01a1b94e-9994-7cd0-8426-bf37e4a00687"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "warning"}},
    {"type": "turn.started"},
    {"type": "error", "message": "Reconnecting... 5/5 (workspace routing discovery failed)"},
    {"type": "error", "message": "workspace routing discovery failed"},
    {"type": "turn.failed", "error": {"message": "workspace routing discovery failed"}},
]
_COMMAND: dict[str, object] = {
    "type": "item.completed",
    "item": {"type": "command_execution", "command": "ls"},
}
_USAGE_TRACE = stream(
    {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 9}}},
    }
)


def outage_trace(startup: str, *, version: str = "0.167.0") -> str:
    """Synthetic initial rollout before a model response, including paginated input."""
    events: list[dict[str, object]] = [{"type": "session_meta", "payload": {
        "id": _OUTAGE[0]["thread_id"], "cli_version": version, "source": "exec",
    }}]
    if startup != "header":
        events.append({"type": "turn_context", "payload": {
            "model": "gpt-6.1-sol", "effort": "medium",
        }})
    if startup == "user_input":
        events.extend([
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Synthetic task"}],
            }},
            {"type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": _OUTAGE[0]["thread_id"],
                "turn_id": "synthetic-turn", "item": {
                    "type": "UserMessage", "id": "synthetic-input",
                    "content": [{"type": "text", "text": "Synthetic task", "text_elements": []}],
                },
            }},
        ])
        metadata = events[0]["payload"]
        assert isinstance(metadata, dict)
        metadata["history_mode"] = "paginated"
    if startup == "rate_limit":
        events.append({"type": "event_msg", "payload": {
            "type": "token_count", "info": None, "rate_limits": {"primary": None},
        }})
    if startup == "user_input":
        events = [{**event, "ordinal": ordinal} for ordinal, event in enumerate(events)]
    return stream(*events)


@pytest.mark.parametrize("version", ["0.162.1", "0.167.0", "0.167.1"])
@pytest.mark.parametrize("startup", ["header", "user_input", "rate_limit"])
@pytest.mark.parametrize(
    "extra",
    [
        None,
        {"type": "world_state", "payload": {"full": True, "state": {}}},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "error": {"message": "terminal error"},
            "last_agent_message": None, "time_to_first_token_ms": None,
        }},
        {"type": "new_unknown_record", "payload": {}},
        "{broken",
    ],
)
def test_outage_without_positive_model_evidence_stays_retryable(
    version: str, startup: str, extra: dict[str, object] | str | None,
) -> None:
    trace = outage_trace(startup, version=version)
    if isinstance(extra, dict):
        record = {**extra, **({"ordinal": len(trace.splitlines())} if startup == "user_input" else {})}
        trace += "\n" + stream(record)
    elif extra is not None:
        trace += "\n" + extra
    parsed = codex.parse_events(stream(*_OUTAGE), [trace], "codex")
    assert parsed["usage"] is None
    assert parsed["observed_token_usage"] is False
    assert codex_events.failed_before_first_turn(parsed) == "workspace routing discovery failed"


@pytest.mark.parametrize(
    "item_type",
    [
        "message", "agent_message", "reasoning", "function_call", "function_call_output",
        "custom_tool_call", "custom_tool_call_output", "local_shell_call", "tool_search_call",
        "tool_search_output", "web_search_call", "image_generation_call", "compaction",
        "compaction_summary", "context_compaction",
    ],
)
def test_one_model_output_record_blocks_pre_model_retry(item_type: str) -> None:
    item: dict[str, object] = {"type": item_type}
    if item_type == "message":
        item.update(role="assistant", content=[])
    if item_type == "function_call":
        item["name"] = "shell"
    trace = outage_trace("header") + "\n" + stream({"type": "response_item", "payload": item})
    parsed = codex.parse_events(stream(*_OUTAGE), [trace], "codex")
    assert parsed["observed_token_usage"] is True
    assert codex_events.failed_before_first_turn(parsed) is None


@pytest.mark.parametrize("version", ["0.162.1", "0.167.0", "0.167.1"])
@pytest.mark.parametrize("kind", ["task_complete", "turn_complete"])
@pytest.mark.parametrize(
    "timing, last_message, model_ran",
    [(12, None, True), (0, None, True), (None, "partial output", True),
     (None, "", True), (None, None, False)],
)
def test_failed_completion_retains_partial_stream_evidence(
    version: str, kind: str, timing: int | None, last_message: str | None, model_ran: bool,
) -> None:
    trace = outage_trace("header", version=version) + "\n" + stream({
        "type": "event_msg", "payload": {
            "type": kind, "turn_id": "synthetic-turn",
            "error": {"message": "stream disconnected before response.completed"},
            "time_to_first_token_ms": timing, "last_agent_message": last_message,
        },
    })
    parsed = codex.parse_events(stream(*_OUTAGE), [trace], "codex")
    assert parsed["usage"] is None
    assert parsed["observed_token_usage"] is model_ran
    assert codex_events.failed_before_first_turn(parsed) == (
        None if model_ran else "workspace routing discovery failed"
    )


@pytest.mark.parametrize("version", ["0.162.1", "0.167.0", "0.167.1"])
@pytest.mark.parametrize(
    "kind",
    ["agent_message", "agent_reasoning", "agent_reasoning_raw_content",
     "agent_reasoning_section_break", "agent_message_content_delta", "plan_delta",
     "reasoning_content_delta", "reasoning_raw_content_delta"],
)
def test_stream_output_event_blocks_pre_model_retry(version: str, kind: str) -> None:
    trace = outage_trace("header", version=version) + "\n" + stream({
        "type": "event_msg", "payload": {
            "type": kind, "thread_id": _OUTAGE[0]["thread_id"],
            "turn_id": "synthetic-turn", "item_id": "synthetic-output",
            "message": "partial output", "text": "partial output", "delta": "partial output",
            "summary_index": 0, "content_index": 0,
        },
    })
    parsed = codex.parse_events(stream(*_OUTAGE), [trace], "codex")
    assert parsed["observed_token_usage"] is True
    assert codex_events.failed_before_first_turn(parsed) is None


@pytest.mark.parametrize("version", ["0.162.1", "0.167.0", "0.167.1"])
@pytest.mark.parametrize(
    "item_type",
    ["AgentMessage", "Reasoning", "Plan", "FunctionCallOutput", "CommandExecution",
     "DynamicToolCall", "CollabAgentToolCall", "WebSearch", "ImageView", "ImageGeneration",
     "FileChange", "McpToolCall", "ContextCompaction"],
)
def test_completed_model_output_item_blocks_pre_model_retry(version: str, item_type: str) -> None:
    trace = outage_trace("header", version=version) + "\n" + stream({
        "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": _OUTAGE[0]["thread_id"],
            "turn_id": "synthetic-turn", "item": {"type": item_type, "id": "synthetic-output"},
        },
    })
    parsed = codex.parse_events(stream(*_OUTAGE), [trace], "codex")
    assert parsed["observed_token_usage"] is True
    assert codex_events.failed_before_first_turn(parsed) is None


@pytest.mark.parametrize(
    "item, model_ran",
    [({"type": "SubAgentActivity", "kind": "completed"}, True),
     ({"type": "SubAgentActivity", "kind": "started"}, False),
     ({"type": "Extension", "kind": "image_gen.generation"}, True),
     ({"type": "Extension", "kind": "web.search"}, True),
     ({"type": "Extension", "kind": "clock.sleep"}, True),
     ({"type": "Extension", "kind": "unknown.extension"}, False),
     ({"type": "UserMessage", "content": []}, False),
     ({"type": "HookPrompt", "fragments": []}, False),
     ({"type": "EnteredReviewMode"}, False),
     ({"type": "ExitedReviewMode", "review_output": None}, False),
     ({"type": "ExitedReviewMode", "review_output": {"findings": []}}, True),
     ({"type": "UnknownOutput"}, False)],
)
def test_completed_item_requires_positive_output_evidence(
    item: dict[str, object], model_ran: bool,
) -> None:
    trace = outage_trace("header") + "\n" + stream({
        "type": "event_msg", "payload": {"type": "item_completed", "item": item},
    })
    parsed = codex.parse_events(stream(*_OUTAGE), [trace], "codex")
    assert parsed["observed_token_usage"] is model_ran
    assert codex_events.failed_before_first_turn(parsed) == (
        None if model_ran else "workspace routing discovery failed"
    )


@pytest.mark.parametrize(
    "item_type",
    ["agent_message", "reasoning", "command_execution", "file_change", "mcp_tool_call",
     "collab_tool_call", "web_search", "todo_list"],
)
def test_one_stdout_output_item_blocks_pre_model_retry(item_type: str) -> None:
    stdout = stream(*_OUTAGE, {"type": "item.completed", "item": {"type": item_type}})
    parsed = codex.parse_events(stdout, [], "codex")
    assert parsed["observed_token_usage"] is True
    assert codex_events.failed_before_first_turn(parsed) is None


def test_session_that_failed_before_its_first_model_turn_is_a_backend_failure() -> None:
    execution = codex.parse_events(stream(*_OUTAGE), [], "codex")
    execution["timed_out"] = False
    assert codex_events.failed_before_first_turn(execution) == "workspace routing discovery failed"


@pytest.mark.parametrize(
    "evidence", ["token_count", "response_item", "stdout_usage"]
)
def test_model_call_evidence_does_not_require_valid_accounting(evidence: str) -> None:
    counts = {"input_tokens": 20, "output_tokens": 5, "new_nullable_field": None}
    stdout = stream(*_OUTAGE)
    if evidence == "token_count":
        trace = stream({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": counts},
        }})
    elif evidence == "response_item":
        trace = stream({"type": "response_item", "payload": {"type": "reasoning"}})
    elif evidence == "stdout_usage":
        trace = ""
        stdout += "\n" + stream({"type": "turn.completed", "usage": counts})
    parsed = codex.parse_events(stdout, [trace] if trace else [], "codex")
    assert parsed["usage"] is None
    assert codex_events.failed_before_first_turn(parsed) is None
    assert parsed["observed_token_usage"] is True


@pytest.mark.parametrize("record", [
    "{broken", "[]", '{"type":"new_unknown_event"}',
    '{"type":"item.completed","item":{"type":"new_unknown_item"}}',
])
def test_malformed_or_unknown_stdout_does_not_block_pre_model_retry(record: str) -> None:
    parsed = codex.parse_events(stream(*_OUTAGE) + "\n" + record, [], "codex")
    assert codex_events.failed_before_first_turn(parsed) == "workspace routing discovery failed"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "session_meta", "payload": {
            "id": _OUTAGE[0]["thread_id"], "cli_version": "0.167.0", "source": [],
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "output_text", "text": "x"}],
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": 5}],
        }},
        {"type": "turn_context", "payload": {"model": []}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
        {"type": "event_msg", "payload": {
            "type": "token_count", "info": None, "rate_limits": [],
        }},
        {"type": "event_msg", "payload": {"type": "new_unknown_event"}},
    ],
)
def test_non_model_records_do_not_block_pre_model_retry(
    event: dict[str, object],
) -> None:
    header: dict[str, object] = {"type": "session_meta", "payload": {
        "id": _OUTAGE[0]["thread_id"], "cli_version": "0.167.0", "source": "exec",
    }}
    records = [event] if event["type"] == "session_meta" else [header, event]
    parsed = codex.parse_events(stream(*_OUTAGE), [stream(*records)], "codex")
    assert codex_events.failed_before_first_turn(parsed) == "workspace routing discovery failed"


@pytest.mark.parametrize(
    ("events", "traces", "timed_out"),
    [
        # Reconnected and finished the turn.
        (
            [*_OUTAGE[:4], {"type": "turn.completed", "usage": {"input_tokens": 9}}],
            [],
            False,
        ),
        # A completed turn proves a call even when no trace could validate its token measure.
        (
            [*_OUTAGE, {"type": "turn.completed", "usage": {"input_tokens": 9}}],
            [],
            False,
        ),
        # Timed out while waiting for the network: a scored timeout.
        (_OUTAGE[:4], [], True),
        # Failed after the model had already used tokens.
        (_OUTAGE, [_USAGE_TRACE], False),
        # Failed after the agent ran a command.
        (
            [
                *_OUTAGE[:3],
                _COMMAND,
                *_OUTAGE[3:],
            ],
            [],
            False,
        ),
    ],
)
def test_sessions_where_the_agent_worked_or_timed_out_are_not_backend_failures(
    events: list[dict[str, object]], traces: list[str], timed_out: bool
) -> None:
    execution = codex.parse_events(stream(*events), traces, "codex")
    execution["timed_out"] = timed_out
    assert codex_events.failed_before_first_turn(execution) is None
