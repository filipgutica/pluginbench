"""Claude Code capabilities and evidence for isolated read-only reviews and coding launches."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any


REQUIRED_FLAGS = (
    "--safe-mode",
    "--setting-sources",
    "--strict-mcp-config",
    "--mcp-config",
    "--settings",
    "--disable-slash-commands",
    "--tools",
    "--model",
    "--effort",
    "--output-format",
    "--verbose",
    "--no-session-persistence",
    "--no-chrome",
    "--restricted",
    "--permission-mode",
    "--permission-prompts",
    "--json-schema",
)
AUTH_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
_NO_CREDENTIAL = (
    "Claude authentication unavailable: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY "
    "is set in the environment of this process. A service, such as a systemd unit, does not "
    "inherit a variable that a shell profile exports; set one in the service's environment"
)
READ_TOOLS = "Read,Glob,Grep"


def preflight() -> dict[str, Any]:
    """Check installed flags without authenticating or invoking a model."""
    outputs = {}
    for name in ("version", "help"):
        result = subprocess.run(
            ["claude", f"--{name}"], capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode:
            raise ValueError(f"claude {name} check failed (exit {result.returncode})")
        outputs[name] = result.stdout
    missing = [flag for flag in REQUIRED_FLAGS if flag not in outputs["help"]]
    if missing:
        raise ValueError("native claude lacks required isolation flags: " + ", ".join(missing))
    return {
        "harness": "claude-cli",
        "harness_version": outputs["version"].strip(),
        "help": outputs["help"],
        "required_flags": list(REQUIRED_FLAGS),
        "paid_calls": 0,
        "effective_effort_status": "unknown unless reported by native session events",
        "managed_policy": "safe mode does not bypass managed settings",
    }


def command(
    *,
    model: str,
    effort: str = "medium",
) -> list[str]:
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError(f"unsupported Claude effort: {effort}")
    # cwd is set by the shared runner, not a prompt or --add-dir discovery surface.
    return [
        "claude",
        "--print",
        "--safe-mode",
        "--model",
        model,
        "--effort",
        effort,
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        '{"disableAllHooks":true,"autoMemoryEnabled":false}',
        "--disable-slash-commands",
        "--tools",
        READ_TOOLS,
        "--restricted",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--no-chrome",
    ]


EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The pinned launch bypasses permission prompts, which print mode would turn into denials.
# It needs no CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: that forces `default` mode and sandboxes Bash
# (CLI 2.1.322), and the subscription token reaches the CLI as a file, not through the env.
PERMISSION_MODE = "bypassPermissions"
# Settings every Claude coding launch passes, identical in vanilla and treatment arms.
PINNED_SETTINGS: dict[str, object] = {}
# Discovery adds only this: it lists hooks without running them.
DISCOVERY_SETTINGS: dict[str, object] = {"disableAllHooks": True}
DISALLOWED_TOOLS = ("RemoteTrigger", "CronCreate", "ScheduleWakeup", "SendMessage", "Workflow")
# Routes, credentials and limits that would change which model serves the run, whose
# account pays for it, or how it is metered. Coding authenticates with the operator's
# subscription token only; an API key would silently outrank it in print mode.
REFUSED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "DISABLE_PROMPT_CACHING",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)


# The subscription token reaches the CLI only as this file, linked into its private
# configuration from a secrets folder outside the downloaded /logs; it is never in the
# environment of a command in the container. CLI 2.1.322 accepts this shape for a
# setup-token (offline check): `scopes` must grant inference; no refresh token or expiry,
# so the CLI never rewrites the file.
SECRETS_DIR = "/tmp/claude-secrets"
CREDENTIALS_FILE = ".credentials.json"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def credentials(token: str) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token, "scopes": ["user:inference"]}})


@dataclass(frozen=True)
class CodingRun:
    """The run settings a Claude coding launch, and its discovery, are built from."""

    model: str
    effort: str
    subagents: bool
    timeout_seconds: int


def _settings(settings: dict[str, object]) -> str:
    return json.dumps(settings, sort_keys=True, separators=(",", ":"))


def coding_launch(run: CodingRun) -> tuple[list[str], dict[str, str]]:
    """The argv and pinned environment of a coding attempt; the prompt arrives on stdin."""
    if run.effort not in EFFORTS:
        raise ValueError(f"unsupported Claude effort: {run.effort}")
    disallowed = [*DISALLOWED_TOOLS, *([] if run.subagents else ["Agent"])]
    argv = [
        "claude",
        "--verbose",
        "--output-format=stream-json",
        "--settings",
        _settings(PINNED_SETTINGS),
        "--model",
        run.model,
        "--effort",
        run.effort,
        f"--permission-mode={PERMISSION_MODE}",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--include-hook-events",
        "--no-chrome",
        "--disallowed-tools",
        ",".join(disallowed),
        "--print",
    ]
    env = {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_UPDATES": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # Subagents run in the foreground, so one run is one init and one result; without
        # it, CLI 2.1.322 starts an Agent call in the background unless the model asks not to.
        # It also removes Bash's run_in_background, in every arm alike.
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_SUBAGENT_MODEL": run.model,
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": str(run.timeout_seconds * 1000),
    }
    return argv, env


STDOUT_LOG = "/logs/agent/claude-code.txt"
STDERR_LOG = "/logs/agent/claude-code.stderr.log"
# The coding command Harbor 0.24.0's ClaudeCode.run builds when PluginBench passes it only
# `version` and `reasoning_effort`: no settings file, no other flag. The prompt travels in a
# one-use environment variable whose name Harbor draws at random.
_HARBOR_CODING = re.compile(
    r'export PATH="\$HOME/\.local/bin:\$PATH"; '
    r'(?P<var>harbor_claude_code_instruction_[0-9a-f]{32})="\$(?P<env>[0-9A-Z_]+)"; '
    r'unset (?P=env); printf "%s" "\$(?P=var)" \| '
    r"(?P<cli>.*) 2>&1 \| tee " + re.escape(STDOUT_LOG)
)


def contract_command(command: str, run: CodingRun) -> str:
    """Harbor's coding command with the pinned argv in place of Harbor's own flags.

    Only the exact command Harbor builds for this run is accepted. The pinned command keeps
    stderr out of the stream-json stdout, and Harbor's `set -o pipefail` keeps the CLI's exit.
    """
    match = _HARBOR_CODING.fullmatch(command)
    harbor = (
        "claude --verbose --output-format=stream-json "
        f"--effort {run.effort} --permission-mode=bypassPermissions --print"
    )
    if match is None or match["env"] != match["var"].upper() or match["cli"] != harbor:
        raise ValueError("Harbor's Claude coding command differs from the pinned launch contract")
    argv = coding_launch(run)[0]
    return command[: match.start("cli")] + f"{shlex.join(argv)} 2>{STDERR_LOG} | tee {STDOUT_LOG}"


def refuse_env(env: Mapping[str, str]) -> None:
    refused = sorted(set(env) & set(REFUSED_ENV))
    if refused:
        raise ValueError(f"Claude coding refuses {', '.join(refused)}; model call refused")


def refuse_container_env(env: Mapping[str, str]) -> None:
    """A command in the container: no refused variable, and no token, which is a file there."""
    refuse_env(env)
    if TOKEN_ENV in env:
        raise ValueError(f"Claude coding refuses {TOKEN_ENV} in the container; model call refused")


def check_coding_env(env: Mapping[str, str], run: CodingRun) -> None:
    """The environment a coding command runs with: pinned, with no credential in it."""
    refuse_container_env(env)
    unpinned = sorted(name for name, value in coding_launch(run)[1].items() if env.get(name) != value)
    if unpinned:
        raise ValueError(f"Claude coding requires the pinned {', '.join(unpinned)}")


def discovery_launch(run: CodingRun) -> tuple[list[str], dict[str, str]]:
    """The coding launch with only the discovery delta, so both load the same treatment.

    Discovery sends control requests on stdin instead of the prompt, keeps no session, and
    lists hooks without running them. The environment is unchanged.
    """
    argv, env = coding_launch(run)
    argv[argv.index("--settings") + 1] = _settings({**PINNED_SETTINGS, **DISCOVERY_SETTINGS})
    argv[argv.index("--print") : argv.index("--print")] = [
        "--input-format", "stream-json", "--no-session-persistence",
    ]
    return argv, env


def runtime_environment(
    private: Path, host: str = "claude", *, auth_file: Path | None = None
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Use a fresh config/home and pass only selected authentication scalars."""
    if host != "claude":
        raise ValueError(f"unsupported Claude host: {host}")
    if auth_file is not None:
        selected = json.loads(auth_file.read_text())
        if not isinstance(selected, dict) or set(selected) - set(AUTH_NAMES):
            raise ValueError("Claude auth file must contain only a supported credential variable")
    else:
        selected = {name: os.environ[name] for name in AUTH_NAMES if os.environ.get(name)}
        if not selected:
            # Retain the existing native macOS subscription-auth path, not its settings.
            try:
                credentials = subprocess.run(
                    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except OSError as error:
                raise ValueError(_NO_CREDENTIAL) from error
            if credentials.returncode:
                raise ValueError(_NO_CREDENTIAL)
            try:
                token = json.loads(credentials.stdout)["claudeAiOauth"]["accessToken"]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Claude Keychain OAuth access token unavailable") from error
            selected = {"CLAUDE_CODE_OAUTH_TOKEN": token}
    if len(selected) != 1 or not all(
        isinstance(value, str) and value.strip() for value in selected.values()
    ):
        raise ValueError("Select exactly one nonempty Claude OAuth token or API key")
    private.mkdir(mode=0o700)
    home, temporary, config = (private / name for name in ("home", "tmp", "claude"))
    for directory in (home, temporary, config):
        directory.mkdir(mode=0o700)
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        }
    }
    env.update(
        HOME=str(home),
        TMPDIR=str(temporary),
        XDG_CONFIG_HOME=str(home / ".config"),
        NO_COLOR="1",
        CLAUDE_CONFIG_DIR=str(config),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_SUBPROCESS_ENV_SCRUB="1",
    )
    env.update(selected)
    return env, tuple(selected.values())


def coding_environment(
    private: Path, *, auth_file: Path | None = None
) -> tuple[dict[str, str], tuple[str, ...]]:
    """The worker environment of a coding run: the operator's subscription token only.

    Harbor forwards an API key over the token and never drops one, so a refused variable in
    the operator's environment stops the run before any container starts.
    """
    present = sorted(name for name in REFUSED_ENV if name in os.environ)
    if present:
        raise ValueError(
            "Claude coding authenticates with CLAUDE_CODE_OAUTH_TOKEN only; unset "
            + ", ".join(present)
        )
    if auth_file is not None:
        raise ValueError("Claude coding takes CLAUDE_CODE_OAUTH_TOKEN, not an auth file")
    return runtime_environment(private)


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage(result: dict[str, Any]) -> tuple[dict[str, int] | None, str]:
    # modelUsage covers nested/auxiliary pipeline calls; usage covers the main loop.
    models = result.get("modelUsage")
    if isinstance(models, dict) and models:
        entries = list(models.values())
        fields = {
            "input_tokens": ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"),
            "output_tokens": ("outputTokens",),
            "cached_input_tokens": ("cacheReadInputTokens",),
            "cache_creation_input_tokens": ("cacheCreationInputTokens",),
        }
        source = "native modelUsage; all query-pipeline models; cached input included"
    else:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None, "unknown"
        entries = [usage]
        fields = {
            "input_tokens": (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ),
            "output_tokens": ("output_tokens",),
            "cached_input_tokens": ("cache_read_input_tokens",),
            "cache_creation_input_tokens": ("cache_creation_input_tokens",),
        }
        source = "native usage; main loop only; cached input included"
    measured = {}
    for key, native_keys in fields.items():
        if all(
            isinstance(entry, dict) and all(_count(entry.get(k)) for k in native_keys)
            for entry in entries
        ):
            measured[key] = sum(entry[k] for entry in entries for k in native_keys)
    return measured or None, source


def observed_cli_version(startup: object) -> str | None:
    if not isinstance(startup, list):
        return None
    versions = [
        event.get("claude_code_version", event.get("cli_version"))
        for event in startup
        if isinstance(event, dict)
        and event.get("type") == "system"
        and event.get("subtype") == "init"
    ]
    if not versions or not all(
        isinstance(version, str) and version.strip() for version in versions
    ):
        return None
    return versions[0] if len(set(versions)) == 1 else None


# Claude Code versions that coding evidence (discovery control requests, session transcripts,
# and token reconciliation) is qualified against. Other versions run with a warning.
QUALIFIED_VERSIONS = frozenset({"2.1.322"})
# The tools a qualified CLI offers the pinned coding launch with subagents on, as `system/init`
# lists them; tests/fixtures/claude-2.1.322/init-tools.json holds the captures. From the paid
# 2.1.322 probe (subscription login): an offline, fake-token start omits WebFetch.
# `system/init` names the subagent tool Task; `--disallowed-tools Agent` removes it.
SUBAGENT_TOOL = "Task"
QUALIFIED_TOOLS: dict[str, frozenset[str]] = {
    "2.1.322": frozenset({
        "Bash", "CronDelete", "CronList", "Edit", "EnterWorktree", "ExitWorktree", "ListAgents",
        "Monitor", "NotebookEdit", "Read", "ReportFindings", "Skill", "Task", "TaskStop",
        "ToolSearch", "WebFetch", "WebSearch", "Write",
    }),
}


@cache
def admit_version(version: str) -> None:
    """Accept every version; warn once per unqualified version in this process."""
    if version not in QUALIFIED_VERSIONS:
        warnings.warn(
            f"Claude Code CLI {version} is not qualified for PluginBench: its discovery control "
            "requests, session transcript format, and token reconciliation have not been "
            "checked against this version. Qualified versions: "
            + ", ".join(sorted(QUALIFIED_VERSIONS)),
            stacklevel=2,
        )


def _builtin_plugin(plugin: object) -> bool:
    """A plugin the CLI ships and loads by itself; no setting of the run selects it.

    Its `path` is the word `builtin`, where an installed plugin has a directory. The set is
    part of the CLI version, which the run records, and stays in the saved startup event.
    """
    return (
        isinstance(plugin, dict)
        and plugin.get("path") == "builtin"
        and isinstance(plugin.get("name"), str)
        and plugin.get("source") == f"{plugin['name']}@builtin"
    )


def parse_events(
    stdout: str,
    traces: list[str],
    host: str = "claude",
    *,
    requested_effort: str = "medium",
    coding: bool = False,
) -> dict[str, Any]:
    """Normalize native receipts; never infer effective effort from the launch flag.

    An isolated review refuses plugins, hooks and subagents. A coding attempt may run its
    treatment's plugins and hooks, and subagents; its receipt checks those instead.
    """
    if host != "claude":
        raise ValueError(f"unsupported Claude host: {host}")
    errors: list[Any] = []
    startup: list[dict[str, Any]] = []
    models: set[str] = set()
    efforts: set[str] = set()
    commands: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    messages: list[str] = []
    pending: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            errors.append("malformed stdout JSONL")
            continue
        if not isinstance(event, dict):
            errors.append("stdout JSONL event is not an object")
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            startup.append(event)
            if isinstance(event.get("effort"), str) and event["effort"].strip():
                efforts.add(event["effort"])
            plugins = event.get("plugins") or []
            if (
                not isinstance(plugins, list)
                or not (coding or all(_builtin_plugin(plugin) for plugin in plugins))
                or any(
                    event.get(key) for key in ("mcp_servers", "plugin_errors", "mcp_server_errors")
                )
            ):
                errors.append("custom plugin or MCP configuration present in native startup")
        if (
            not coding
            and event.get("type") == "system"
            and str(event.get("subtype", "")).startswith("hook_")
        ):
            errors.append("native hook ran during isolated invocation")
        if event.get("type") == "result":
            result = event
        if event.get("type") not in {"assistant", "user"}:
            continue
        if not coding and event.get("parent_tool_use_id") is not None:
            errors.append("delegated work appeared in isolated native invocation")
        message = event.get("message")
        if (
            isinstance(message, dict)
            and event.get("type") == "user"
            and isinstance(message.get("content"), str)
        ):
            continue
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            errors.append("malformed native message")
            continue
        if (
            event.get("type") == "assistant"
            and isinstance(message.get("model"), str)
            and message["model"].strip()
        ):
            models.add(message["model"])
        for content in message["content"]:
            if not isinstance(content, dict):
                errors.append("malformed native content block")
            elif content.get("type") == "text" and isinstance(content.get("text"), str):
                if event.get("type") == "assistant":
                    messages.append(content["text"])
            elif content.get("type") == "tool_use":
                tools.append(content)
                if isinstance(content.get("id"), str):
                    pending[content["id"]] = content
            elif content.get("type") == "tool_result":
                tool_id = content.get("tool_use_id")
                call = pending.get(tool_id, {}) if isinstance(tool_id, str) else {}
                commands.append(
                    {
                        "id": content.get("tool_use_id"),
                        "command": call,
                        "status": "completed",
                        "exit_code": 1 if content.get("is_error") else 0,
                        "aggregated_output": content.get("content"),
                    }
                )
    if not models:
        models.update(
            event["model"]
            for event in startup
            if isinstance(event.get("model"), str) and event["model"].strip()
        )
    usage, token_format = _usage(result)
    structured = result.get("structured_output")
    final = (
        json.dumps(structured) if isinstance(structured, (dict, list)) else result.get("result", "")
    )
    if not isinstance(final, str):
        errors.append("invalid native result text")
        final = ""
    if result.get("is_error") or result.get("errors"):
        errors.append(result)
    if not startup:
        errors.append("native initialization evidence missing")
    dollars = result.get("total_cost_usd")
    if (
        not isinstance(dollars, (int, float))
        or isinstance(dollars, bool)
        or not math.isfinite(dollars)
        or dollars < 0
    ):
        dollars = None
    return {
        "command_items": commands,
        "actual_primary_models": sorted(models),
        "usage": usage,
        "partial_usage": [],
        "token_format": token_format,
        "native_dollars": dollars,
        "dollars_status": "reported estimate" if dollars is not None else "unknown",
        "requested_effort": requested_effort,
        "runtime_effort_evidence": sorted(efforts),
        "effort_evidence_source": "native session event"
        if efforts
        else "requested launch setting; effective effort unverified",
        "startup": startup,
        "observed_cli_version": observed_cli_version(startup),
        "other_tools": tools,
        "stream_errors": errors,
        "final": final,
        "agent_messages": messages,
        "successful_native_completion": result.get("subtype") == "success" and not errors,
    }
