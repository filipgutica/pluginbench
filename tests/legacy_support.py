"""Saved state from PluginBench 942f12c, written by tests/fixtures/legacy-942f12c/generate.py."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from pluginbench import components

LEGACY = Path(__file__).parent / "fixtures/legacy-942f12c"


def legacy_json(name: str) -> dict[str, Any]:
    value = json.loads((LEGACY / name).read_text())
    assert isinstance(value, dict)
    return value


def current_runtime(saved: dict[str, Any]) -> dict[str, Any]:
    """The same runtime as this build reports it, after Docker's memory moved by 28 KiB."""
    current = copy.deepcopy(saved)
    current["launcher_version"] = components.VERSIONS["launcher"]
    current["implementation_sha256"] = components.source_sha256("harbor_backend.py")
    current["docker_engine"]["capacity"]["memory_bytes"] -= 28 * 1024
    return current


def copy_legacy(name: str, destination: Path) -> Path:
    """Copy a fixture directory, restoring the file modes a vanilla manifest recorded.

    Git keeps only the executable bit, and a vanilla cache inventory binds every mode bit.
    """
    shutil.copytree(LEGACY / name, destination)
    for manifest in destination.glob("*/*/manifest.json"):
        entry = manifest.parent
        for relative, metadata in json.loads(manifest.read_text())["inventory"].items():
            (entry / relative).chmod(metadata["mode"])
    return destination
