"""Deliver declared treatments through the native Claude Code CLI in an owned runtime.

The Claude counterpart of native_treatments.py, under delivery policy `claude-native-v1`.
Harbor copies loose skills into the attempt's private CLAUDE_CONFIG_DIR. This adapter runs
after that copy and before the first coding command: its companion Node program writes the
guidance, installs plugins from a private marketplace, and proves through a discovery run
that sends no user message that exactly the declared treatment loaded.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pluginbench import claude
from pluginbench.native_treatments import MARKETPLACE, MAX_GUIDANCE_BYTES, REMOTE_ROOT
from pluginbench.treatments import Treatment, resolve_treatment, treatment_inventory

DELIVERY_POLICY = "claude-native-v1"
# Harbor's claude-code agent keeps its private configuration in its logs directory.
CONFIG_DIR = "/logs/agent/sessions"
# Offline, each plugin command took about 18 s and discovery about 20 s, so this admits
# about 25 plugins. The runtime keeps its own per-command limits.
INSTALL_TIMEOUT_SECONDS = 600


def admit(treatment: Treatment) -> dict[str, Any]:
    """The inventory of a treatment Claude can deliver, under the Codex guidance cap."""
    resolved = resolve_treatment(treatment, Path.cwd(), "claude")
    inventory = treatment_inventory(resolved, "claude")
    guidance = b"\n\n".join(path.read_bytes() for path in resolved.guidance)
    if len(guidance) > MAX_GUIDANCE_BYTES:
        raise ValueError(
            f"native guidance exceeds {MAX_GUIDANCE_BYTES} bytes; refusing silent truncation"
        )
    return inventory


def stage(
    treatment: Treatment, directory: Path, *, run: claude.CodingRun, cli_version: str
) -> dict[str, Any]:
    """Build the guidance, a private marketplace and the discovery launch for the runtime."""
    inventory = admit(treatment)
    directory.mkdir(parents=True, exist_ok=True)
    guidance = b"\n\n".join(path.read_bytes() for path in treatment.guidance)
    (directory / "CLAUDE.md").write_bytes(guidance)
    market = directory / "marketplace"
    manifest_dir = market / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    packages = []
    for source, item in zip(treatment.plugins, inventory["plugins"], strict=True):
        name = item["name"]
        shutil.copytree(source, market / "plugins" / name, ignore=shutil.ignore_patterns(".git"))
        packages.append({"name": name, "source": "./plugins/" + name})
    (manifest_dir / "marketplace.json").write_text(json.dumps({
        "name": MARKETPLACE, "owner": {"name": "pluginbench"}, "plugins": packages,
    }) + "\n")
    discovery_argv, pinned_env = claude.discovery_launch(run)
    payload = {
        "delivery": DELIVERY_POLICY,
        "inventory": inventory,
        "guidance_sha256": hashlib.sha256(guidance).hexdigest(),
        "guidance_bytes": len(guidance),
        "marketplace": MARKETPLACE,
        "cli_version": cli_version,
        "discovery_argv": discovery_argv,
        # The runtime checks its own environment against these, since the CLI inherits it.
        "pinned_env": pinned_env,
        "refused_env": list(claude.REFUSED_ENV),
    }
    (directory / "treatment.json").write_text(json.dumps(payload, sort_keys=True) + "\n")
    shutil.copy2(Path(__file__).with_name("claude_treatment_runtime.js"), directory / "install.cjs")
    return payload


async def install(
    *, environment: object, treatment: Treatment, env: dict[str, str], cwd: str,
    artifacts: Path, run: claude.CodingRun, cli_version: str,
) -> dict[str, Any]:
    """Deliver and discover with the same environment as the upcoming coding command."""
    if env.get("CLAUDE_CONFIG_DIR") != CONFIG_DIR:
        raise ValueError(
            "Claude treatment installation requires Harbor's private CLAUDE_CONFIG_DIR"
        )
    pinned = claude.discovery_launch(run)[1]
    if any(env.get(name) != value for name, value in pinned.items()):
        raise ValueError("Claude treatment installation requires the pinned launch environment")
    refused = sorted(set(env) & set(claude.REFUSED_ENV))
    if refused:
        raise ValueError(f"Claude treatment installation refuses {', '.join(refused)}")
    with tempfile.TemporaryDirectory(prefix="pluginbench-treatment-") as temporary:
        local = Path(temporary).resolve()
        payload = stage(treatment, local, run=run, cli_version=cli_version)
        await getattr(environment, "upload_dir")(source_dir=local, target_dir=REMOTE_ROOT)
        result = await getattr(environment, "exec")(
            # The coding command puts $HOME/.local/bin first too, so both run the same CLI.
            command=(
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                f"node {REMOTE_ROOT}/install.cjs {REMOTE_ROOT} {shlex.quote(cwd)}"
                " 2>/logs/agent/native-treatment.stderr"
            ),
            env=env,
            cwd=cwd,
            timeout_sec=INSTALL_TIMEOUT_SECONDS,
        )
        receipt_file = artifacts / "native-treatment.json"
        stdout = str(getattr(result, "stdout", ""))
        (artifacts / "native-treatment.stdout").write_text(stdout)
        await getattr(environment, "download_file")(
            source_path="/logs/agent/native-treatment.stderr",
            target_path=artifacts / "native-treatment.stderr",
        )
        if getattr(result, "return_code") != 0:
            receipt_file.write_text(json.dumps({
                "status": "failed", "delivery": DELIVERY_POLICY,
                "inventory": payload["inventory"],
                "reason": "Claude installation or discovery failed; coding was not started",
            }, indent=2) + "\n")
            raise ValueError("Claude treatment installation/discovery failed; model call refused")
        try:
            receipt = json.loads(stdout)
        except ValueError as error:
            raise ValueError("Claude treatment discovery returned invalid evidence") from error
        if not isinstance(receipt, dict) or receipt.get("status") != "verified":
            raise ValueError("Claude treatment discovery did not verify installation")
        if receipt.get("inventory") != payload["inventory"]:
            raise ValueError("Claude treatment receipt describes different package contents")
        discovery = receipt.get("discovery")
        if not isinstance(discovery, dict) or discovery.get("argv") != payload["discovery_argv"]:
            raise ValueError("Claude treatment discovery did not use the pinned launch")
        receipt_file.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return receipt
