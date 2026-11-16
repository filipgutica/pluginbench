from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Evaluations share caches under XDG_CACHE_HOME; tests never touch the user's real one."""
    cache = tmp_path_factory.mktemp("xdg-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


@pytest.fixture(autouse=True)
def isolated_host_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The host execution lock lives here, never in the user's real state directory."""
    from pluginbench import run_lock

    state = tmp_path_factory.mktemp("host-state")
    monkeypatch.setattr(run_lock, "state_root", lambda: state)
    return state
