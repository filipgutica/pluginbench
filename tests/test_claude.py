from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import IO, Literal, TypedDict, Unpack

import pytest

import pluginbench.claude as claude


class NativeProcessArguments(TypedDict):
    cwd: Path
    env: dict[str, str]
    stdin: int
    stdout: IO[str]
    stderr: IO[str]
    text: Literal[True]
    start_new_session: bool


def stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def init(**extra: object) -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "init",
        "model": "requested-alias",
        "plugins": [],
        "mcp_servers": [],
        **extra,
    }


def result(**extra: object) -> dict[str, object]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "final answer",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5,
        },
        **extra,
    }


def test_preflight_checks_local_capabilities_without_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def native(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="2.1.295 (Claude Code)"
            if argv[-1] == "--version"
            else " ".join(claude.REQUIRED_FLAGS),
        )

    monkeypatch.setattr(subprocess, "run", native)
    receipt = claude.preflight()
    assert calls == [["claude", "--version"], ["claude", "--help"]]
    assert receipt["paid_calls"] == 0
    assert receipt["harness_version"] == "2.1.295 (Claude Code)"
    assert "unknown" in receipt["effective_effort_status"]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="old cli"),
    )
    with pytest.raises(ValueError, match="lacks required isolation flags"):
        claude.preflight()


def test_read_only_command_removes_shell_writes_and_delegation(tmp_path: Path) -> None:
    argv = claude.command(
        model="claude-sonnet-5-5", effort="high", workspace=tmp_path, read_only=True
    )
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--restricted" in argv and "--safe-mode" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert not any(flag in argv for flag in ("--plugin-dir", "--agents", "--system-prompt"))
    with pytest.raises(ValueError, match="text guidance"):
        claude.command(model="test", workspace=tmp_path, plugin=tmp_path)


def test_selected_auth_file_ignores_inherited_auth_guidance_and_model_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unselected-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "unselected-oauth")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/host/plugins")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://host.invalid")
    monkeypatch.setenv("ANTHROPIC_MODEL", "unselected-model")
    auth = tmp_path / "selected.json"
    auth.write_text(json.dumps({"CLAUDE_CODE_OAUTH_TOKEN": "selected-oauth"}))
    private = tmp_path / "private"
    env, secrets = claude.runtime_environment(private, auth_file=auth)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "selected-oauth"
    assert secrets == ("selected-oauth",)
    assert not any(
        key in env
        for key in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_EFFORT_LEVEL",
            "CLAUDE_CODE_PLUGIN_DIRS",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
        )
    )
    assert Path(env["HOME"]).is_relative_to(private)
    assert Path(env["CLAUDE_CONFIG_DIR"]).is_relative_to(private)
    assert not any(path.is_file() for path in private.rglob("*"))
    assert private.stat().st_mode & 0o777 == 0o700
    assert auth.exists()


@pytest.mark.parametrize(
    "auth",
    [
        {},
        {"CLAUDE_CODE_OAUTH_TOKEN": " "},
        {"ANTHROPIC_API_KEY": False},
        {"ANTHROPIC_API_KEY": "key", "CLAUDE_CODE_OAUTH_TOKEN": "token"},
        {"token": "unsupported"},
        {"ANTHROPIC_API_KEY": "key", "settings": {"hooks": {}}},
        [],
    ],
)
def test_auth_input_does_not_accept_settings_or_ambiguous_credentials(
    tmp_path: Path,
    auth: object,
) -> None:
    selected = tmp_path / "auth.json"
    selected.write_text(json.dumps(auth))
    with pytest.raises(ValueError):
        claude.runtime_environment(tmp_path / "private", auth_file=selected)
    assert not (tmp_path / "private").exists()


def test_oauth_environment_needs_no_keychain_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "explicit-token")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Explicit token must not read Keychain"),
    )
    env, secrets = claude.runtime_environment(tmp_path / "private")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "explicit-token"
    assert secrets == ("explicit-token",)


def test_existing_keychain_auth_copies_only_access_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in claude.AUTH_NAMES:
        monkeypatch.delenv(name, raising=False)
    calls = []

    def keychain(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "claudeAiOauth": {"accessToken": "access", "refreshToken": "refresh"},
                    "unrelated": "discard",
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", keychain)
    env, secrets = claude.runtime_environment(tmp_path / "private")
    assert calls == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "access"
    assert "refresh" not in env.values() and secrets == ("access",)


def test_structured_final_and_usage_include_cached_input_without_invented_effort() -> None:
    parsed = claude.parse_events(
        stream(
            init(),
            {"type": "user", "message": {"role": "user", "content": "task prompt"}},
            {
                "type": "assistant",
                "message": {
                    "model": "actual-model",
                    "content": [
                        {"type": "text", "text": "progress"},
                    ],
                },
            },
            result(structured_output={"issues": []}),
        ),
        [],
        requested_effort="high",
    )
    assert parsed["successful_native_completion"]
    assert parsed["actual_primary_models"] == ["actual-model"]
    assert json.loads(parsed["final"]) == {"issues": []}
    assert parsed["agent_messages"] == ["progress"]
    assert parsed["usage"] == {
        "input_tokens": 35,
        "output_tokens": 4,
        "cached_input_tokens": 20,
        "cache_creation_input_tokens": 5,
    }
    assert parsed["requested_effort"] == "high"
    assert parsed["runtime_effort_evidence"] == []
    assert parsed["observed_cli_version"] is None
    assert "unverified" in parsed["effort_evidence_source"]


def test_latest_model_usage_covers_auxiliary_calls_without_summing_cumulative_results() -> None:
    first = result(
        modelUsage={
            "model-a": {
                "inputTokens": 10,
                "outputTokens": 4,
                "cacheReadInputTokens": 20,
                "cacheCreationInputTokens": 5,
            }
        }
    )
    last = result(
        modelUsage={
            "model-a": {
                "inputTokens": 12,
                "outputTokens": 6,
                "cacheReadInputTokens": 20,
                "cacheCreationInputTokens": 5,
            },
            "compact-model": {
                "inputTokens": 3,
                "outputTokens": 2,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
        }
    )
    parsed = claude.parse_events(stream(init(), first, last), [])
    assert parsed["usage"]["input_tokens"] == 40
    assert parsed["usage"]["output_tokens"] == 8
    assert parsed["actual_primary_models"] == ["requested-alias"]


def test_missing_cache_accounting_keeps_input_tokens_unknown_and_zero_output_known() -> None:
    parsed = claude.parse_events(
        stream(init(), result(usage={"input_tokens": 10, "output_tokens": 0})), []
    )
    assert parsed["usage"] == {"output_tokens": 0}


@pytest.mark.parametrize(
    "bad_event",
    [
        [],
        {"type": "assistant", "message": None},
        {"type": "assistant", "message": {"content": [False]}},
        {"type": "system", "subtype": "hook_started"},
        {"type": "assistant", "parent_tool_use_id": "delegate", "message": {"content": []}},
        init(plugins=[{"name": "unexpected"}]),
        init(mcp_servers=[{"name": "unexpected"}]),
    ],
)
def test_bad_events_or_loaded_customizations_cannot_count_as_success(bad_event: object) -> None:
    stdout = stream(init()) + "\n" + json.dumps(bad_event) + "\n" + stream(result())
    parsed = claude.parse_events(stdout, [])
    assert parsed["stream_errors"] and not parsed["successful_native_completion"]


def test_truncated_and_failed_runs_do_not_report_success() -> None:
    parsed = claude.parse_events(stream(init(), result()) + "\n{truncated", [])
    assert not parsed["successful_native_completion"]
    failed = claude.parse_events(stream(init(), result(is_error=True)), [])
    assert not failed["successful_native_completion"]
    assert not claude.parse_events(stream(init()), [])["successful_native_completion"]


def test_native_read_trace_and_optional_identity_fields_are_preserved() -> None:
    parsed = claude.parse_events(
        stream(
            init(effort="low", cli_version="native-version"),
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-1",
                            "name": "Read",
                            "input": {"file_path": "after/source.ts"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-1",
                            "content": "source",
                        }
                    ]
                },
            },
            result(),
        ),
        [],
    )
    assert parsed["runtime_effort_evidence"] == ["low"]
    assert parsed["observed_cli_version"] == "native-version"
    assert parsed["command_items"][0]["command"]["input"] == {"file_path": "after/source.ts"}
    assert parsed["command_items"][0]["aggregated_output"] == "source"


@pytest.mark.parametrize("version", [None, "", "  ", {}, []])
def test_invalid_native_version_is_unknown(version: object) -> None:
    assert claude.observed_cli_version([init(cli_version=version)]) is None


def test_keychain_unavailable_does_not_create_private_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in claude.AUTH_NAMES:
        monkeypatch.delenv(name, raising=False)

    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("security unavailable")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ValueError, match="export an OAuth token or API key"):
        claude.runtime_environment(tmp_path / "private")
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("exit_code", [0, 2])
def test_shared_invoke_forwards_schema_isolates_and_redacts_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    secret = "fake-secret-that-must-not-remain-in-evidence"
    auth = tmp_path / "selected-auth.json"
    auth.write_text(json.dumps({"CLAUDE_CODE_OAUTH_TOKEN": secret}))
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {"type": "object", "properties": {"issues": {"type": "array"}}, "required": ["issues"]}
        )
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "unselected-model")
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/unselected/plugins")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setattr(
        claude,
        "preflight",
        lambda: {
            "harness_version": "2.1.fixture (Claude Code)",
            "paid_calls": 0,
        },
    )
    native_popen = subprocess.Popen
    launches: list[dict[str, object]] = []
    script = "\n".join(
        [
            "import json,os,pathlib,sys",
            "assert 'ANTHROPIC_MODEL' not in os.environ",
            "assert 'CLAUDE_CODE_PLUGIN_DIRS' not in os.environ",
            "assert 'CLAUDE_CODE_EFFORT_LEVEL' not in os.environ",
            "config=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])",
            "token=os.environ['CLAUDE_CODE_OAUTH_TOKEN']",
            "(config/'secret-file').write_text(token)",
            "assert sys.stdin.read() == 'review task'",
            "print(json.dumps({'type':'system','subtype':'init','model':'actual-model',"
            "'plugins':[],'mcp_servers':[]}))",
            "print(json.dumps({'type':'assistant','message':{'model':'actual-model',"
            "'content':[{'type':'text','text':token}]}}))",
            "print(token,file=sys.stderr)",
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'structured_output':{'issues':[]},'result':'not the JSON response',"
            "'usage':{'input_tokens':10,'output_tokens':3,"
            "'cache_read_input_tokens':20,'cache_creation_input_tokens':5}}))",
            f"sys.exit({exit_code})",
        ]
    )

    def launch(argv: list[str], **kwargs: Unpack[NativeProcessArguments]) -> subprocess.Popen[str]:
        launches.append({"argv": argv, "cwd": kwargs["cwd"]})
        return native_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    execution = claude.invoke(
        area=area,
        entry={"host": "claude", "arm": "baseline"},
        protocol={
            "models": {"claude": "requested-model"},
            "reasoning": "high",
            "auth_file": str(auth),
        },
        prompt="review task",
        timeout=5,
        read_only=True,
        output_schema=schema,
    )
    argv = launches[0]["argv"]
    assert isinstance(argv, list)
    assert json.loads(argv[argv.index("--json-schema") + 1]) == json.loads(schema.read_text())
    assert "--output-schema" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--restricted" in argv and "--safe-mode" in argv
    assert launches[0]["cwd"] == area / "workspace"
    assert execution["successful_native_completion"] is (exit_code == 0)
    assert execution["exit_code"] == exit_code and not execution["timed_out"]
    assert execution["actual_primary_models"] == ["actual-model"]
    assert execution["runtime_effort_evidence"] == []
    assert execution["observed_cli_version"] is None
    assert execution["launch_harness_version"] == "2.1.fixture (Claude Code)"
    assert execution["launch"]["requested_model"] == "requested-model"
    assert execution["launch"]["requested_effort"] == "high"
    assert execution["usage"]["input_tokens"] == 35
    assert execution["usage"]["output_tokens"] == 3
    assert json.loads(execution["final"]) == {"issues": []}
    assert execution["private_runtime_removed"] and not (area / "private").exists()
    assert auth.exists()
    isolation = json.loads((area / "artifacts/runtime-isolation.json").read_text())
    assert isolation["fresh_home"] and isolation["private_startup_files"] == []
    for artifact in (area / "artifacts").iterdir():
        assert secret not in artifact.read_text()
    saved = json.loads((area / "artifacts/host-evidence.json").read_text())
    assert saved["launch"]["harness_version"] == "2.1.fixture (Claude Code)"
    assert saved["agent_messages"] == ["<redacted-credential>"]


def test_shared_invoke_timeout_overrides_completed_stream_and_cleans_private_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-authentication")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude, "preflight", lambda: {"harness_version": "fixture-version"})
    native_popen = subprocess.Popen
    script = "\n".join(
        [
            "import json,time",
            f"print({stream(init(), result())!r}, flush=True)",
            "time.sleep(10)",
        ]
    )

    def launch(argv: list[str], **kwargs: Unpack[NativeProcessArguments]) -> subprocess.Popen[str]:
        return native_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    execution = claude.invoke(
        area=area,
        entry={"host": "claude", "arm": "baseline"},
        protocol={"models": {"claude": "requested-model"}},
        prompt="task",
        timeout=1,
    )
    assert execution["timed_out"] and not execution["successful_native_completion"]
    assert execution["private_runtime_removed"] and not (area / "private").exists()


def test_shared_invoke_missing_selected_auth_preserves_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    area = tmp_path / "trial"
    (area / "workspace").mkdir(parents=True)
    monkeypatch.setattr(claude, "preflight", lambda: {"harness_version": "fixture-version"})
    with pytest.raises(FileNotFoundError):
        claude.invoke(
            area=area,
            entry={"host": "claude", "arm": "baseline"},
            protocol={
                "models": {"claude": "requested-model"},
                "auth_file": str(tmp_path / "missing"),
            },
            prompt="task",
            timeout=1,
        )
    execution = json.loads((area / "artifacts/execution.json").read_text())
    assert execution["exit_code"] is None and execution["private_runtime_removed"]
    assert not (area / "private").exists()
