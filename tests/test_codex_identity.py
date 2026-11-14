"""Codex launches are byte-identical to main before the Claude launch contract (315540a).

`tests/fixtures/codex-launch-golden.json` was computed by `codex_launch_snapshot` with the
source tree of origin/main at 315540a, so this test compares against main, not this branch.
The only later change is the vanilla config's `features.multi_agent_v2` table, which hides
V2's spawn model and effort fields; it was added to the golden by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pluginbench import components
from pluginbench import harbor_backend as harbor
from pluginbench.native_treatments import NativeCapabilities
from pluginbench.treatments import Treatment

GOLDEN = Path(__file__).parent / "fixtures/codex-launch-golden.json"


def codex_launch_snapshot(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What a Codex launch is built from, with the Docker engine replaced by a fixed one."""
    info = {
        "ID": "engine", "ServerVersion": "29.0.0", "OSType": "linux", "Architecture": "x86_64",
        "NCPU": 8, "MemTotal": 16 * 1024**3,
    }
    monkeypatch.setattr(harbor, "_engine", lambda: (info, "unix:///var/run/docker.sock"))
    monkeypatch.setattr(
        harbor, "_docker_cli",
        lambda info: {"path": "/usr/bin/docker", "sha256": "d" * 64, "version": "29.0.0"},
    )
    monkeypatch.setattr(harbor, "_docker_plugins", lambda info: {})
    identity = harbor.runtime_identity(cpus=2, memory_mb=4096)
    # The file's own hash is recorded provenance, never compared.
    identity.pop("implementation_sha256")
    limits = {"cpus": 2, "memory_mb": 4096}
    common: dict[str, Any] = {
        "task_dir": Path("/task"), "area": Path("/area"), "owner": "o", "harness": "codex",
        "version": "0.167.0", "limits": limits, "timeout": 600,
    }
    return {
        "runtime_identity": identity,
        "config_vanilla": harbor._native_config(
            **common, model="gpt-6.1-sol", effort="high", treatment=Treatment(),
            capabilities=NativeCapabilities(),
        ),
        "config_prepare": harbor._native_config(**common),
        "qualification": harbor._native_cli_qualification("codex", "0.167.0"),
        "guard": harbor._cli_guard("codex", "0.167.0"),
        "launcher_files_codex": list(components.launcher_files("codex")),
        "exec_prefix": harbor._CODEX_EXEC_PREFIX,
        "exec_suffix": harbor._CODEX_EXEC_SUFFIX,
        "codex_run_sha256": harbor._CODEX_RUN_SHA256,
    }


def test_codex_launch_identity_is_byte_identical_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    golden = json.loads(GOLDEN.read_text())["snapshot"]
    current = json.loads(json.dumps(codex_launch_snapshot(monkeypatch)))
    # Key order too: trial and invocation receipts are written in insertion order.
    assert json.dumps(current) == json.dumps(golden)
    # Saved runs bind the launcher by version, which stays 1.
    assert components.VERSIONS["launcher"] == 1
