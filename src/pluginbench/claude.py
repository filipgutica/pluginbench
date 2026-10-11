"""Claude Code capabilities and evidence for the shared native runner.

Treatment guidance is prompt text; safe mode preserves native instructions while
disabling discovered customizations. Managed policy can still apply.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
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
    "--dangerously-skip-permissions",
)
AUTH_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
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
    host: str = "claude",
    model: str,
    workspace: Path,
    plugin: Path | None = None,
    effort: str = "medium",
    read_only: bool = False,
) -> list[str]:
    if host != "claude":
        raise ValueError(f"unsupported Claude host: {host}")
    if plugin is not None:
        raise ValueError("Claude evaluates text guidance, not native plugin execution")
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError(f"unsupported Claude effort: {effort}")
    # cwd is set by the shared runner, not a prompt or --add-dir discovery surface.
    tools = READ_TOOLS if read_only else "Bash,Read,Edit,Write,Glob,Grep"
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
        tools,
        *(
            ["--restricted", "--permission-mode", "dontAsk"]
            if read_only
            else ["--dangerously-skip-permissions"]
        ),
        "--permission-prompts",
        "none",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--no-chrome",
    ]


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
                raise ValueError(
                    "Claude authentication unavailable; export an OAuth token or API key"
                ) from error
            if credentials.returncode:
                raise ValueError(
                    "Claude authentication unavailable; export an OAuth token or API key"
                )
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


def parse_events(
    stdout: str, traces: list[str], host: str = "claude", *, requested_effort: str = "medium"
) -> dict[str, Any]:
    """Normalize native receipts; never infer effective effort from the launch flag."""
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
            if any(
                event.get(key)
                for key in ("plugins", "mcp_servers", "plugin_errors", "mcp_server_errors")
            ):
                errors.append("custom plugin or MCP configuration present in native startup")
        if event.get("type") == "system" and str(event.get("subtype", "")).startswith("hook_"):
            errors.append("native hook ran during isolated invocation")
        if event.get("type") == "result":
            result = event
        if event.get("type") not in {"assistant", "user"}:
            continue
        if event.get("parent_tool_use_id") is not None:
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


def invoke(**kwargs: Any) -> dict[str, Any]:
    """Use the process, timeout, cleanup, and redaction owner shared with Codex."""
    from . import codex

    return codex.invoke(**kwargs)
