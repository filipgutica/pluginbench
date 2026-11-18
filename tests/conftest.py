from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import operation_support
import resume_support
from fake_systemd import HANDLERS, register_handlers
from pluginbench import control_store, invocation_runtime, run_lock
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
    state = tmp_path_factory.mktemp("host-state")
    monkeypatch.setattr(run_lock, "state_root", lambda: state)
    # Recovery's two container inventories need not be a real second apart in tests.
    monkeypatch.setattr(control_store, "INVENTORY_INTERVAL", 0.0)
    return state


@pytest.fixture(autouse=True)
def guidance_outside_tests_is_ignored(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guidance a host keeps in /tmp or above cannot fail tests, here or in their children.

    Temporary workspaces PluginBench makes itself go below pytest's base directory too, so the
    ancestor check still covers everything a test creates.
    """
    if request.node.get_closest_marker("real_guidance_check"):
        return  # The guard's own tests check it as production runs it.
    base = tmp_path_factory.getbasetemp().resolve()
    temporary = base / "tempfile"
    temporary.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv(resume_support.GUIDANCE_CEILING, str(base))
    resume_support.bound_guidance(monkeypatch)


@pytest.fixture
def host_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ExecutionLock]:
    """Hold the host execution lock, as the CLI does around recovery after a crash.

    A test's killed child leaves nothing running, but a call killed before its child was
    recorded cannot be checked, so the process check passes here; test_control_store covers it.
    """
    monkeypatch.setattr(control_store, "verify_invocation", nothing_left_running)
    owner = run_lock.ExecutionOwner(kind="run", eval=str(tmp_path), root=str(tmp_path))
    with control_store.execute(owner) as lock:
        yield lock


def nothing_left_running(check: dict[str, Any]) -> dict[str, Any]:
    """A cleanup check for a test whose crashed calls left no process."""
    return {"id": check["id"], "checked": "test"}


@pytest.fixture(autouse=True)
def operations_in_place(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operations run in the test's own process (`operation_support.in_place`) on a host that
    can contain them. Tests marked `real_operations` run them in real or fake systemd scopes."""
    if not request.node.get_closest_marker("real_operations"):
        operation_support.in_place(monkeypatch)


@pytest.fixture
def user_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real systemd user manager, for tests marked `systemd`. Without one the test skips,
    unless PLUGINBENCH_REQUIRE_SYSTEMD_TESTS=1 (as in CI) makes that a failure. Scope children
    load the test handlers from this checkout."""
    try:
        invocation_runtime.require_user_manager()
    except invocation_runtime.HostUnavailable as exc:
        if os.environ.get("PLUGINBENCH_REQUIRE_SYSTEMD_TESTS") == "1":
            pytest.fail(f"a systemd user manager is required: {exc}")
        pytest.skip(f"no systemd user manager: {exc.reason}")
    source = str(Path(invocation_runtime.__file__).parents[1])
    monkeypatch.setenv("PYTHONPATH", f"{HANDLERS}{os.pathsep}{source}")
    register_handlers(monkeypatch)
