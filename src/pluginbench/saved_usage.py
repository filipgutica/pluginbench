"""Project current token validation without changing saved raw evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pluginbench import claude_usage, codex

HOOK_REASON = "command-hook subprocess usage is not independently metered"


class AuditedHook(BaseModel):
    """A command hook audited to make no model call and start no model CLI."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    plugin_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Pins the audited plugin's files by the sha256 that its treatment inventory records.",
    )
    hook: str = Field(
        min_length=1,
        description="Identifies the hook: Codex's currentHash, or the sha256: identity PluginBench names for a Claude hook.",
    )
    note: str = Field(
        min_length=1,
        description="Records the audit: the hook makes no model call and starts no model CLI.",
    )


def _hook_identity(hook: dict[str, Any]) -> tuple[str, str, str | None]:
    """The hook's plugin name, event and identity; None when the receipt cannot identify it."""
    if "pluginId" in hook:
        # Codex hooks/list hashes each handler and its source as "sha256:<a>/sha256:<b>".
        plugin, event, identity = hook["pluginId"], hook.get("eventName"), hook.get("currentHash")
    else:
        # Claude's hook listing has no hash, so its event, matcher and command name the hook.
        # The plugin's sha256 pins the files that command runs.
        plugin, event = hook.get("pluginName"), hook.get("event")
        command = hook.get("commandText")
        identity = (
            "sha256:" + hashlib.sha256(
                json.dumps([event, hook.get("matcher") or "", command]).encode()
            ).hexdigest()
            if isinstance(command, str)
            else None
        )
    name = str(plugin).rpartition("@")[0] or str(plugin)
    return name, str(event), identity if isinstance(identity, str) else None


def hook_rule(
    treatment: dict[str, Any], audited: Sequence[AuditedHook]
) -> tuple[str | None, list[dict[str, str]]]:
    """Why command hooks leave native usage incomplete, or None with the audits that applied.

    Every hook in the receipt counts as one that runs. A Claude receipt marks its hooks
    `disabled` only because discovery runs with disableAllHooks; the coding run executes them.
    """
    hooks = (treatment.get("hooks") or {}).get("hooks")
    if not hooks:
        return None, []
    if not audited:
        return HOOK_REASON, []
    plugins = {
        str(plugin.get("name")): str(plugin.get("sha256"))
        for plugin in (treatment.get("inventory") or {}).get("plugins", [])
    }
    listed = {(entry.plugin_sha256, entry.hook) for entry in audited}
    applied: list[dict[str, str]] = []
    unaudited = []
    for hook in hooks:
        name, event, identity = _hook_identity(hook)
        sha256 = plugins.get(name)
        if sha256 is not None and identity is not None and (sha256, identity) in listed:
            applied.append({"plugin_sha256": sha256, "hook": identity})
        else:
            unaudited.append(f"{name} {event} (plugin_sha256 {sha256}, hook {identity})")
    if unaudited:
        return f"{HOOK_REASON}; unaudited hooks: {'; '.join(unaudited)}", []
    return None, applied


def revalidate_usage(
    execution: dict[str, Any], receipt: Path, audited_hooks: Sequence[AuditedHook] = ()
) -> dict[str, Any]:
    """Keep the original observation beside a fresh, fail-closed usage result.

    `audited_hooks` is the run's frozen list, so the hook rule decides as it did at launch.
    """
    original = execution.get("usage_validation") or {
        "recorded_usage": execution.get("usage"),
        "recorded_partial_usage": execution.get("partial_usage", []),
        "recorded_usage_accounting": execution.get("usage_accounting"),
        # Historical receipts lack a recording timestamp; retain the file time explicitly.
        "recorded_at": datetime.fromtimestamp(receipt.stat().st_mtime, timezone.utc).isoformat(),
        "recorded_at_source": "execution receipt file modification time",
    }
    artifacts = receipt.parent / "artifacts"
    parsed: dict[str, Any] = {}
    reasons = []
    launch = execution.get("launch") or {}
    # Native session accounting cannot meter subprocesses launched by unaudited command hooks.
    hook_reason, audited = hook_rule(execution.get("native_treatment") or {}, audited_hooks)
    recorded = original["recorded_usage"]
    # A fresh Claude attempt whose built-ins differ from the cached vanilla it is compared with.
    mixed = execution.get("mixed_builtins")
    if "native_session_usage" in execution:
        # The hook and mixed built-ins rules keep the session usage there before they clear
        # `usage`. An attempt whose hooks were all audited kept `usage` and has no such key.
        recorded = execution["native_session_usage"]
    try:
        codex.validate_artifacts(artifacts)
        traces = sorted(artifacts.glob("host-trace-*.jsonl"))
        if not traces:
            # Older Harbor layouts may retain sessions without the flattened copies.
            traces = sorted((artifacts / "harbor/trial/agent/sessions").rglob("*.jsonl"))
        if launch.get("harness") == "claude-cli":
            parsed = claude_attempt_usage(artifacts, launch)
        elif not traces:
            reasons.append("legacy usage has no saved session evidence to validate")
        else:
            stdout = artifacts / "stdout.jsonl"
            if not stdout.is_file():
                stdout = artifacts / "harbor/trial/agent/codex.txt"
            parsed = codex.parse_events(
                stdout.read_text() if stdout.is_file() else "",
                [path.read_text() for path in traces],
                "codex",
                include_descendants=True,
            )
        if parsed:
            reasons.extend(parsed["usage_accounting"]["reasons"])
            if parsed["usage"] != recorded:
                reasons.append("revalidated usage disagrees with recorded usage")
    except (OSError, ValueError) as error:
        reasons.append(f"saved usage evidence is unreadable: {error}")
    if hook_reason:
        reasons.append(hook_reason)
    if mixed:
        reasons.append(str(mixed))
    usage = parsed.get("usage") if not reasons else None
    return {
        **execution,
        "usage": usage,
        "partial_usage": parsed.get("partial_usage", []),
        "usage_accounting": {
            **parsed.get("usage_accounting", {}),
            "status": "complete" if usage is not None else "incomplete",
            "reasons": reasons,
            **({"audited_hooks": audited} if audited else {}),
        },
        "usage_validation": {
            **original,
            "status": "validated" if usage is not None else "unavailable",
            "validated_usage": parsed.get("usage"),
            "reasons": reasons,
        },
    }


def claude_attempt_usage(artifacts: Path, launch: dict[str, Any]) -> dict[str, Any]:
    """Parse a Claude attempt's saved stream and transcripts with the attempt's own rules.

    A fresh attempt and every later reuse or report run this same function.
    """
    agent = artifacts / "harbor/trial/agent"
    stdout = artifacts / "stdout.jsonl"
    if not stdout.is_file():
        stdout = agent / "claude-code.txt"
    return claude_usage.coding_usage(
        stdout.read_text(errors="replace") if stdout.is_file() else "",
        agent / "sessions",
        runner_model=str(launch.get("requested_model")),
        cli_version=str(launch.get("harness_version")),
    )
