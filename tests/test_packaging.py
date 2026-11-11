"""The package metadata agrees with the code it ships."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pluginbench
from pluginbench import harbor_backend

ROOT = Path(__file__).parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_the_package_version_is_the_same_in_every_place() -> None:
    # `pluginbench --version` reads the module, so it is right when the source runs uninstalled.
    # The release pull request updates all three; a hand edit must do the same.
    released = json.loads((ROOT / ".release-please-manifest.json").read_text())["."]
    assert PROJECT["version"] == pluginbench.__version__ == released


def test_the_harbor_pin_is_the_version_runs_require() -> None:
    assert f"harbor=={harbor_backend.HARBOR_VERSION}" in PROJECT["dependencies"]
