from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path
from typing import IO, Any, Literal, TypedDict, Unpack

import pytest

import pluginbench.claude as claude
from pluginbench import codex, evaluation


class NativeProcessArguments(TypedDict):
    cwd: Path
    env: dict[str, str]
    stdin: int
    stdout: IO[str]
    stderr: IO[str]
    text: Literal[True]
    start_new_session: bool


@pytest.fixture(autouse=True)
def _clean_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer shell may export a refused variable; tests set the ones they need.
    for name in claude.REFUSED_ENV:
        monkeypatch.delenv(name, raising=False)


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


def test_unqualified_version_is_admitted_and_warns_once() -> None:
    claude.admit_version.cache_clear()
    with pytest.warns(UserWarning, match="Claude Code CLI 2.1.9001 is not qualified") as caught:
        assert claude.admit_version("2.1.9001") is None
        assert claude.admit_version("2.1.9001") is None
    assert len(caught) == 1
    message = str(caught[0].message)
    for unqualified in (
        "discovery control requests",
        "session transcript format",
        "token reconciliation",
        "plugin skills that set `paths`",
    ):
        assert unqualified in message
    assert "Qualified versions: 2.1.322" in message


def test_qualified_version_is_admitted_silently() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        claude.admit_version("2.1.322")


def test_review_command_removes_shell_writes_and_delegation() -> None:
    argv = claude.command(model="claude-sonnet-5-5", effort="high")
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--restricted" in argv and "--safe-mode" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert not any(flag in argv for flag in ("--plugin-dir", "--agents", "--system-prompt"))


def test_coding_launch_is_the_pinned_contract() -> None:
    run = claude.CodingRun(
        model="claude-opus-5-5", effort="high", subagents=False, timeout_seconds=1800
    )
    assert claude.coding_launch(run) == (
        [
            "claude", "--verbose", "--output-format=stream-json", "--settings", "{}",
            "--model", "claude-opus-5-5", "--effort", "high",
            "--permission-mode=bypassPermissions", "--setting-sources", "user",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--include-hook-events", "--no-chrome", "--disallowed-tools",
            "RemoteTrigger,CronCreate,ScheduleWakeup,SendMessage,Workflow,Agent", "--print",
        ],
        {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_UPDATES": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5-5",
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1800000",
        },
    )


def test_a_coding_env_without_the_forced_subagent_model_is_refused() -> None:
    # Without it, an Agent call's `model` or an agent's frontmatter picks another model.
    run = claude.CodingRun("claude-opus-5-5", "high", True, 1800)
    env = dict(claude.coding_launch(run)[1])
    claude.check_coding_env(env, run)
    del env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"]
    with pytest.raises(ValueError, match="pinned CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
        claude.check_coding_env(env, run)


@pytest.mark.parametrize("subagents", [False, True])
def test_discovery_differs_from_coding_by_exactly_the_discovery_delta(subagents: bool) -> None:
    run = claude.CodingRun(
        model="claude-opus-5-5", effort="medium", subagents=subagents, timeout_seconds=600
    )
    coding, coding_env = claude.coding_launch(run)
    discovery, discovery_env = claude.discovery_launch(run)
    expected = list(coding)
    settings = json.loads(coding[coding.index("--settings") + 1])
    expected[coding.index("--settings") + 1] = json.dumps(
        {**settings, "disableAllHooks": True}, sort_keys=True, separators=(",", ":")
    )
    printing = coding.index("--print")
    expected[printing:printing] = ["--input-format", "stream-json", "--no-session-persistence"]
    assert discovery == expected
    assert discovery_env == coding_env


def test_selected_auth_file_ignores_inherited_auth_guidance_and_model_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A refused variable, such as ANTHROPIC_API_KEY, stops the launch instead of being ignored.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "unselected-oauth")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/host/plugins")
    monkeypatch.setenv("ANTHROPIC_MODEL", "unselected-model")
    auth = tmp_path / "selected.json"
    auth.write_text(json.dumps({"CLAUDE_CODE_OAUTH_TOKEN": "selected-oauth"}))
    private = tmp_path / "private"
    env, secrets = claude.runtime_environment(private, auth_file=auth)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "selected-oauth"
    assert secrets == ("selected-oauth",)
    assert not any(
        key in env
        for key in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_PLUGIN_DIRS", "ANTHROPIC_MODEL")
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
        {"ANTHROPIC_API_KEY": "key"},
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


# Each refused case fails on 0.7.1: an API key alone was accepted, and a provider variable
# beside the token was ignored.
REFUSED_OPERATOR_ENVS = [
    pytest.param({"ANTHROPIC_API_KEY": "sk-ant-unwanted"}, "ANTHROPIC_API_KEY", id="api-key"),
    pytest.param(
        {"CLAUDE_CODE_OAUTH_TOKEN": "token", "CLAUDE_CODE_USE_BEDROCK": "1"},
        "CLAUDE_CODE_USE_BEDROCK",
        id="provider",
    ),
]


@pytest.mark.parametrize(("operator_env", "refused"), REFUSED_OPERATOR_ENVS)
def test_claude_review_preflight_refuses_an_api_key_or_provider_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator_env: dict[str, str], refused: str
) -> None:
    # The run's review phase and `pluginbench review` pass this check before any review call.
    for name in (claude.TOKEN_ENV, *claude.REFUSED_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in operator_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(claude, "preflight", lambda: {"harness_version": "fixture-version"})
    monkeypatch.setattr(
        subprocess, "Popen", lambda *args, **kwargs: pytest.fail("model call started")
    )
    with pytest.raises(ValueError, match=f"CLAUDE_CODE_OAUTH_TOKEN only; unset {refused}$"):
        evaluation._review_preflight({"reviewer": {"harness": "claude"}})


def test_claude_review_preflight_accepts_the_subscription_token_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in claude.REFUSED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(claude.TOKEN_ENV, "token")
    monkeypatch.setattr(claude, "preflight", lambda: {"harness_version": "fixture-version"})
    evaluation._review_preflight({"reviewer": {"harness": "claude"}})


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
    monkeypatch.delenv(claude.TOKEN_ENV, raising=False)
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


def _answered(startup: dict[str, object]) -> dict[str, Any]:
    return claude.parse_events(
        stream(
            startup,
            {"type": "assistant", "message": {"model": "actual-model", "content": []}},
            result(),
        ),
        [],
    )


def _builtin(name: str) -> dict[str, str]:
    # The shape Claude Code 2.1.322 reports for the plugins it ships and loads by itself.
    return {"name": name, "path": "builtin", "source": f"{name}@builtin"}


def test_plugins_built_into_the_cli_are_not_custom_configuration() -> None:
    plugins = [_builtin("cc-plugin-agents-md"), _builtin("cc-plugin-privacy")]
    parsed = _answered(init(plugins=plugins))
    assert parsed["stream_errors"] == []
    assert parsed["successful_native_completion"]
    assert parsed["startup"][0]["plugins"] == plugins


@pytest.mark.parametrize(
    "startup",
    [
        {"plugins": [{"name": "mine", "path": "/plugins/mine", "source": "mine@market"}]},
        {"plugins": [{"name": "mine", "path": "/plugins/mine", "source": "mine@builtin"}]},
        {"plugins": [{"name": "mine", "path": "builtin", "source": "mine@market"}]},
        {"plugins": [_builtin("cc-plugin-privacy"), "mine"]},
        {"plugins": {"cc-plugin-privacy": "builtin"}},
        {"mcp_servers": [{"name": "files", "status": "connected"}]},
        {"plugins": [_builtin("cc-plugin-privacy")], "plugin_errors": ["failed to load"]},
        {"plugins": [_builtin("cc-plugin-privacy")], "mcp_server_errors": ["failed to start"]},
    ],
)
def test_any_other_plugin_or_mcp_server_at_startup_fails_the_call(
    startup: dict[str, object],
) -> None:
    parsed = _answered(init(**startup))
    assert parsed["stream_errors"] == [
        "custom plugin or MCP configuration present in native startup"
    ]
    assert not parsed["successful_native_completion"]


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
    monkeypatch.delenv(claude.TOKEN_ENV, raising=False)

    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("security unavailable")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ValueError) as refused:
        claude.runtime_environment(tmp_path / "private")
    # The message names the variable and the case that hides it: a service's environment.
    assert claude.TOKEN_ENV in str(refused.value)
    assert "systemd unit" in str(refused.value)
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
    execution = codex.invoke(
        area=area,
        entry={"host": "claude", "arm": "baseline"},
        protocol={
            "models": {"claude": "requested-model"},
            "reasoning": "high",
            "auth_file": str(auth),
        },
        prompt="review task",
        timeout=5,
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
    execution = codex.invoke(
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
        codex.invoke(
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
