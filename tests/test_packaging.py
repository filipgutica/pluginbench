"""The package metadata agrees with the code it ships."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pluginbench
from pluginbench import harbor_backend

PROJECT = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]


def test_the_package_version_is_the_same_in_both_places() -> None:
    # `pluginbench --version` reads the module, so it is right when the source runs uninstalled.
    assert PROJECT["version"] == pluginbench.__version__


def test_the_harbor_pin_is_the_version_runs_require() -> None:
    assert f"harbor=={harbor_backend.HARBOR_VERSION}" in PROJECT["dependencies"]
