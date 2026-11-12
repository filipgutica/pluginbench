"""Project current token validation without changing saved raw evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pluginbench import claude_usage, codex


def revalidate_usage(execution: dict[str, Any], receipt: Path) -> dict[str, Any]:
    """Keep the original observation beside a fresh, fail-closed usage result."""
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
    try:
        codex.validate_artifacts(artifacts)
        traces = sorted(artifacts.glob("host-trace-*.jsonl"))
        if not traces:
            # Older Harbor layouts may retain sessions without the flattened copies.
            traces = sorted((artifacts / "harbor/trial/agent/sessions").rglob("*.jsonl"))
        if launch.get("harness") == "claude-cli":
            parsed = _claude_usage(artifacts, launch)
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
            if parsed["usage"] != original["recorded_usage"]:
                reasons.append("revalidated usage disagrees with recorded usage")
    except (OSError, ValueError) as error:
        reasons.append(f"saved usage evidence is unreadable: {error}")
    # Native session accounting cannot meter subprocesses launched by command hooks.
    hooks = execution.get("native_treatment", {}).get("hooks", {}).get("hooks")
    if hooks:
        reasons.append("command-hook subprocess usage is not independently metered")
    usage = parsed.get("usage") if not reasons else None
    return {
        **execution,
        "usage": usage,
        "partial_usage": parsed.get("partial_usage", []),
        "usage_accounting": {
            **parsed.get("usage_accounting", {}),
            "status": "complete" if usage is not None else "incomplete",
            "reasons": reasons,
        },
        "usage_validation": {
            **original,
            "status": "validated" if usage is not None else "unavailable",
            "validated_usage": parsed.get("usage"),
            "reasons": reasons,
        },
    }


def _claude_usage(artifacts: Path, launch: dict[str, Any]) -> dict[str, Any]:
    """Re-parse a Claude attempt's saved stream and transcripts with the attempt's own rules."""
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
