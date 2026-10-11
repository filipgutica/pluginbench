from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pluginbench import codex


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
