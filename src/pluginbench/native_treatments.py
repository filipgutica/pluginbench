"""Install declared treatments through the native Codex CLI in an owned runtime.

Harbor owns skill upload, configuration and execution. This adapter runs after
Harbor's configuration/skill setup and before its first coding command. The
companion Node program checks the actual installation and native discovery.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pluginbench.treatments import Treatment, resolve_treatment, treatment_inventory

DELIVERY_POLICY = "native-instructions-skills-plugins-v1"
MARKETPLACE = "pluginbench-frozen"
REMOTE_ROOT = "/tmp/pluginbench-treatments"
MAX_GUIDANCE_BYTES = 32768


class NativeCapabilities(BaseModel):
    """One capability policy for vanilla and every treatment in a study."""

    model_config = ConfigDict(extra="forbid")

    subagents: bool = True
    max_threads: int = Field(default=4, strict=True, ge=1, le=16)
    max_depth: int = Field(
        default=1, strict=True, ge=1, le=2,
        description="Native V1 depth preference; ignored by model-selected V2 agents.",
    )

    def codex_config(self, *, model: str, effort: str) -> dict[str, object]:
        return {
            "features": {
                "code_mode": True,
                "code_mode_only": True,
                "plugins": True,
                "remote_plugin": False,
                "apps": False,
                "hooks": True,
                "multi_agent": self.subagents,
            },
            "project_doc_max_bytes": MAX_GUIDANCE_BYTES,
            "agents": {
                # Unlike feature flags, this also disables model-selected V2 agents.
                "enabled": self.subagents,
                "max_threads": self.max_threads,
                "max_depth": self.max_depth,
                "default_subagent_model": model,
                "default_subagent_reasoning_effort": effort,
            },
        }


def admit(treatment: Treatment, harness: str) -> dict[str, Any]:
    """Reject unsupported inputs before container preparation or any model call."""
    if harness != "codex":
        raise ValueError("native treatment execution is currently qualified for Codex only")
    resolved = resolve_treatment(treatment, Path.cwd(), harness)
    inventory = treatment_inventory(resolved)
    guidance = b"\n\n".join(path.read_bytes() for path in resolved.guidance)
    if len(guidance) > MAX_GUIDANCE_BYTES:
        raise ValueError(
            f"native guidance exceeds {MAX_GUIDANCE_BYTES} bytes; refusing silent truncation"
        )
    return inventory


def stage(treatment: Treatment, directory: Path) -> dict[str, Any]:
    """Build a local marketplace from complete, already-frozen packages."""
    inventory = admit(treatment, "codex")
    directory.mkdir(parents=True, exist_ok=True)
    guidance = b"\n\n".join(path.read_bytes() for path in treatment.guidance)
    (directory / "AGENTS.md").write_bytes(guidance)
    market = directory / "marketplace"
    manifest_dir = market / ".agents/plugins"
    manifest_dir.mkdir(parents=True)
    packages = []
    for source, item in zip(treatment.plugins, inventory["plugins"], strict=True):
        name = item["name"]
        shutil.copytree(source, market / "plugins" / name, ignore=shutil.ignore_patterns(".git"))
        packages.append({
            "name": name,
            "source": {"source": "local", "path": "./plugins/" + name},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        })
    (manifest_dir / "marketplace.json").write_text(
        json.dumps({"name": MARKETPLACE, "plugins": packages}) + "\n"
    )
    payload = {
        "delivery": DELIVERY_POLICY,
        "inventory": inventory,
        "guidance_sha256": hashlib.sha256(guidance).hexdigest(),
        "guidance_bytes": len(guidance),
        "marketplace": MARKETPLACE,
    }
    (directory / "treatment.json").write_text(json.dumps(payload, sort_keys=True) + "\n")
    shutil.copy2(Path(__file__).with_name("native_treatment_runtime.js"), directory / "install.cjs")
    return payload


async def install(
    *, environment: object, treatment: Treatment,
    env: dict[str, str], cwd: str, artifacts: Path,
) -> dict[str, Any]:
    """Use the same environment/configuration as the upcoming native coding CLI."""
    if env.get("CODEX_HOME") != "/tmp/codex-home":
        raise ValueError("native treatment installation requires Harbor's private CODEX_HOME")
    with tempfile.TemporaryDirectory(prefix="pluginbench-treatment-") as temporary:
        local = Path(temporary).resolve()
        payload = stage(treatment, local)
        await getattr(environment, "upload_dir")(source_dir=local, target_dir=REMOTE_ROOT)
        result = await getattr(environment, "exec")(
            command=(
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                f"node {REMOTE_ROOT}/install.cjs {REMOTE_ROOT} {shlex.quote(cwd)}"
                " 2>/logs/agent/native-treatment.stderr"
            ),
            env=env,
            cwd=cwd,
            timeout_sec=120,
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
                "reason": "native installation or discovery failed; coding was not started",
            }, indent=2) + "\n")
            raise ValueError("native treatment installation/discovery failed; model call refused")
        try:
            receipt = json.loads(stdout)
        except ValueError as error:
            raise ValueError("native treatment discovery returned invalid evidence") from error
        if not isinstance(receipt, dict) or receipt.get("status") != "verified":
            raise ValueError("native treatment discovery did not verify installation")
        if receipt.get("inventory") != payload["inventory"]:
            raise ValueError("native treatment receipt describes different package contents")
        receipt_file.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return receipt
