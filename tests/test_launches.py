"""The launch protocol: whose launch may claim the host, and how a persisted stop reaches it.

Scopes are fake (`fake_systemd`). No test starts a container or a model.
"""

from __future__ import annotations

import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from fake_systemd import FakeSystemd, members
from fake_systemd import fake_systemd_fixture  # noqa: F401  (the fake_systemd fixture)
from launch_support import in_scope, launches, ticket
from pluginbench import control_store, invocation_runtime, parallel, run_lock, study
from pluginbench.control_store import ControlStore, LaunchFenced
from pluginbench.ledger import LEDGER_NAME, Ledger

pytestmark = pytest.mark.real_operations



def test_a_stale_worker_exits_before_it_touches_another_launch(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        stale = ticket(store, status="fenced")
        current = ticket(store)
    waiting = in_scope(current.scope)
    try:
        owner = run_lock.ExecutionOwner("run", "/run", "/run", launch_id=stale.launch_id)
        with pytest.raises(LaunchFenced, match="is fenced"):
            with control_store.execute(owner):
                pytest.fail("a stale launch ran")
        assert launches(isolated_host_state)[current.launch_id].status == "launching"
        assert members(current.scope)
    finally:
        waiting.kill()
        waiting.wait()


def test_a_command_that_takes_the_host_first_fences_a_launching_worker_and_stops_it(
    fake_systemd: FakeSystemd, isolated_host_state: Path
) -> None:
    with ControlStore.open(isolated_host_state) as store:
        pending = ticket(store)
    waiting = in_scope(pending.scope)
    try:
        with control_store.execute(run_lock.ExecutionOwner("review", "/other", "/other")):
            assert launches(isolated_host_state)[pending.launch_id].status == "finished"
            assert members(pending.scope) == []
        owner = run_lock.ExecutionOwner("run", "/run", "/run", launch_id=pending.launch_id)
        with pytest.raises(LaunchFenced):
            with control_store.execute(owner):
                pytest.fail("a fenced launch ran")
    finally:
        waiting.kill()
        waiting.wait()


def test_a_fenced_launch_is_never_permitted_to_start(
    isolated_host_state: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, status="fenced")
        proc = invocation_runtime.start_gated(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            output=tmp_path / "log",
            admit=partial(store.record_bootstrap, launch.launch_id),
        )
    assert proc is None
    assert not marker.exists()
    assert launches(isolated_host_state)[launch.launch_id].bootstrap is None


def test_a_stop_whose_signal_is_lost_admits_no_further_intent_while_two_jobs_run(
    isolated_host_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wake-up never comes: only the persisted request can stop the run.
    monkeypatch.setattr(control_store, "STOP_POLL", 3600.0)
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, scope="pb-worker-test.scope")
    entries: list[dict[str, Any]] = [{"id": f"a{slot}", "task_id": "t", "arm": "baseline", "attempt": 1}
               for slot in range(6)]
    ledger = Ledger.create(tmp_path / LEDGER_NAME, entries, {e["id"]: "pending" for e in entries})
    both = threading.Barrier(2, timeout=10)

    def operation(slot: int, cancelled: threading.Event, emit: Any) -> None:
        study._on_coordinator(
            emit, partial(ledger.intent, slot=slot, stage="coding", attempt_id=f"a{slot}")
        )
        if slot < 2:
            both.wait()  # Both jobs hold an intent.
            if slot == 0:
                with ControlStore.open(isolated_host_state) as store:
                    store.request_stop(launch.launch_id)
            both.wait()

    owner = run_lock.ExecutionOwner(
        "run", str(tmp_path), str(tmp_path), launch_id=launch.launch_id
    )
    try:
        with pytest.raises(run_lock.StopRequested):
            with control_store.execute(owner):
                parallel.run_jobs(range(6), jobs=2, operation=operation)
        assert sorted(row["slot"] for row in ledger.invocations()) == [0, 1]
    finally:
        ledger.close()
    assert launches(isolated_host_state)[launch.launch_id].status == "finished"


def test_after_a_stop_an_admitted_attempt_s_operations_finish_but_no_new_call_starts(
    fake_systemd: FakeSystemd, isolated_host_state: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As after `stop`'s signal: an attempt past its intent finishes, such as its measurement
    # after coding, so its paid call is kept and never made again on resume.
    monkeypatch.setattr(control_store, "STOP_POLL", 3600.0)
    with ControlStore.open(isolated_host_state) as store:
        launch = ticket(store, scope="pb-worker-test.scope")
    entries: list[dict[str, Any]] = [{"id": "a0", "task_id": "t", "arm": "baseline", "attempt": 1}]
    ledger = Ledger.create(tmp_path / LEDGER_NAME, entries, {"a0": "pending"})
    marker = tmp_path / "ran"
    request = invocation_runtime.OperationRequest(
        operation="echo",
        arguments={"value": 1, "marker": str(marker)},
        timeout=30.0,
        spec=invocation_runtime.InvocationSpec(uuid4().hex, None),
    )
    owner = run_lock.ExecutionOwner(
        "run", str(tmp_path), str(tmp_path), launch_id=launch.launch_id
    )
    try:
        with control_store.execute(owner):
            with ControlStore.open(isolated_host_state) as store:
                store.request_stop(launch.launch_id)
            assert invocation_runtime.run_operation(request) == 1
            with pytest.raises(run_lock.StopRequested):
                ledger.intent(slot=0, stage="coding", attempt_id="a0")
        assert ledger.invocations() == []
    finally:
        ledger.close()
    assert marker.exists()
