from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pluginbench.run_lock import ExecutionLock


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


@pytest.fixture
def host_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ExecutionLock]:
    """Hold the host execution lock, as the CLI does around recovery after a crash.

    A test's killed child leaves nothing running, but a call killed before its child was
    recorded cannot be checked, so the process check passes here; test_control_store covers it.
    """
    from pluginbench import control_store, run_lock

    monkeypatch.setattr(control_store, "verify_invocation", nothing_left_running)

    owner = run_lock.ExecutionOwner(kind="run", eval=str(tmp_path), root=str(tmp_path))
    with control_store.execute(owner) as lock:
        yield lock


def nothing_left_running(item: dict[str, Any]) -> dict[str, Any]:
    """A cleanup check for a test whose crashed calls left no process."""
    return {"id": item["id"], "checked": "test"}
