"""The `systemd` marker: its tests use the real runtime, and CI cannot let them skip.

No module-level `real_operations` marker here, on purpose: a `systemd` test must run real
operations without it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from conftest import REQUIRE_SYSTEMD
from pluginbench import invocation_runtime, run_lock
from pluginbench.invocation_runtime import InvocationSpec, OperationRequest

_TESTS = Path(__file__).parent
_SOURCE = Path(invocation_runtime.__file__).parents[1]


@pytest.mark.systemd
def test_a_systemd_test_runs_its_operation_in_a_real_scope_without_real_operations(
    user_manager: None, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    request = OperationRequest(
        operation="echo",
        arguments={"value": 1, "marker": str(marker)},
        timeout=30.0,
        spec=InvocationSpec(uuid4().hex, None),
    )
    owner = run_lock.ExecutionOwner(kind="run", eval=str(tmp_path), root=str(tmp_path))
    with run_lock.hold_execution(owner):
        assert invocation_runtime.run_operation(request) == 1

    # In place, the handler would have run in this process.
    assert int(marker.read_text()) != os.getpid()


_SKIPPING = """
import pytest

@pytest.fixture
def manager():
    pytest.skip("no user manager")

@pytest.mark.systemd
def test_skips_in_its_body():
    pytest.skip("no user manager")

@pytest.mark.systemd
def test_skips_in_a_fixture(manager):
    pass

@pytest.mark.systemd
@pytest.mark.xfail(reason="expected")
def test_expected_to_fail():
    raise AssertionError
"""


@pytest.mark.parametrize("required", [True, False])
def test_a_skipped_systemd_test_fails_only_when_systemd_tests_are_required(
    tmp_path: Path, required: bool
) -> None:
    (tmp_path / "test_skipping.py").write_text(_SKIPPING)
    env = {key: value for key, value in os.environ.items() if key != REQUIRE_SYSTEMD}
    env["PYTHONPATH"] = os.pathsep.join((str(_TESTS), str(_SOURCE)))
    if required:
        env[REQUIRE_SYSTEMD] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider", "-rA",
         "-o", "markers=systemd: needs systemd",
         "--rootdir", str(tmp_path), str(tmp_path / "test_skipping.py")],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120, check=False,
    )

    output = result.stdout + result.stderr
    if required:
        # A skip in the body fails the call; one in a fixture fails setup, reported as an error.
        assert result.returncode == 1, output
        assert "1 failed, 1 xfailed, 1 error" in output
        assert output.count(f"a systemd test skipped although {REQUIRE_SYSTEMD}=1") >= 2
    else:
        assert result.returncode == 0, output
        assert "2 skipped, 1 xfailed" in output
