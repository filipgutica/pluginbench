from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, wait as wait_for_futures
from threading import Barrier, Event, Lock, get_ident

import pytest

from pluginbench import parallel


@pytest.mark.parametrize("value", [True, False, "2", 1.5, 0, 17, None])
def test_invalid_jobs_never_admits_work(value: object) -> None:
    with pytest.raises(ValueError, match="integer from 1 to 16"):
        parallel.validate_jobs(value)


def test_bounded_jobs_overlap_and_coordinator_updates_execute_once() -> None:
    coordinator = get_ident()
    gate = Barrier(3)
    lock = Lock()
    active = 0
    maximum = 0
    worker_ids: set[int] = set()
    completed: list[int] = []
    updates: list[tuple[int, int]] = []

    def operation(item: int, cancelled: Event, emit: Callable[[Callable[[], None]], None]) -> None:
        nonlocal active, maximum
        with lock:
            worker_ids.add(get_ident())
            active += 1
            maximum = max(maximum, active)
        gate.wait(timeout=5)
        emit(lambda: updates.append((item, get_ident())))
        with lock:
            completed.append(item)
            active -= 1

    parallel.run_jobs(list(range(6)), jobs=3, operation=operation)
    assert maximum == 3 and active == 0
    assert len(worker_ids) == 3 and coordinator not in worker_ids
    assert sorted(completed) == list(range(6))
    assert sorted(item for item, _ in updates) == list(range(6))
    assert {thread for _, thread in updates} == {coordinator}


def test_one_job_preserves_serial_execution_and_callback_order() -> None:
    thread = get_ident()
    events: list[tuple[str, int]] = []
    update_threads: list[int] = []

    def update(item: int) -> None:
        update_threads.append(get_ident())
        events.append(("update", item))

    def operation(item: int, cancelled: Event, emit: Callable[[Callable[[], None]], None]) -> None:
        assert get_ident() != thread and not cancelled.is_set()
        events.append(("start", item))
        emit(lambda: update(item))
        events.append(("finish", item))

    parallel.run_jobs(
        [2, 0, 1],
        jobs=1,
        operation=operation,
        on_submit=lambda item: events.append(("submit", item)),
    )
    assert [event for event in events if event[0] != "update"] == [
        (phase, item) for item in (2, 0, 1) for phase in ("submit", "start", "finish")
    ]
    assert [event for event in events if event[0] in ("submit", "update")] == [
        (phase, item) for item in (2, 0, 1) for phase in ("submit", "update")
    ]
    assert update_threads == [thread] * 3


@pytest.mark.parametrize("failure", [RuntimeError("fatal"), KeyboardInterrupt("interrupt")])
@pytest.mark.parametrize("jobs", [1, 2])
def test_worker_failure_stops_admission_and_drains_active_results(
    failure: BaseException, jobs: int
) -> None:
    gate = Barrier(jobs)
    lock = Lock()
    started: list[int] = []
    saved: list[int] = []
    repairs: list[int] = []

    def operation(item: int, cancelled: Event, emit: Callable[[Callable[[], None]], None]) -> None:
        with lock:
            started.append(item)
        gate.wait(timeout=5)
        if item == 0:
            raise failure
        assert cancelled.wait(timeout=5)
        emit(lambda: saved.append(item))
        if not cancelled.is_set():
            repairs.append(item)

    with pytest.raises(type(failure), match=str(failure)):
        parallel.run_jobs(list(range(10)), jobs=jobs, operation=operation)
    assert sorted(started) == list(range(jobs))
    assert saved == list(range(1, jobs)) and repairs == []


def test_coordinator_interrupt_drains_worker_updates_without_admitting_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = Barrier(3)
    saved: list[int] = []
    submitted: list[int] = []
    interrupted = False

    def wait(*args: object, **kwargs: object) -> object:
        nonlocal interrupted
        if not interrupted:
            gate.wait(timeout=5)
            interrupted = True
            raise KeyboardInterrupt("coordinator interrupted")
        # The real wait only receives the scheduler's known argument shape.
        active = args[0]
        assert isinstance(active, set)
        return wait_for_futures(active, timeout=0.05, return_when=FIRST_COMPLETED)

    def operation(item: int, cancelled: Event, emit: Callable[[Callable[[], None]], None]) -> None:
        gate.wait(timeout=5)
        assert cancelled.wait(timeout=5)
        emit(lambda: saved.append(item))

    monkeypatch.setattr(parallel, "wait", wait)
    with pytest.raises(KeyboardInterrupt, match="coordinator interrupted"):
        parallel.run_jobs(list(range(8)), jobs=2, operation=operation, on_submit=submitted.append)
    assert submitted == [0, 1] and sorted(saved) == [0, 1]


def test_every_interrupt_while_draining_says_what_it_waits_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = Barrier(3)
    release = Event()
    interrupts = 0

    def wait(*args: object, **kwargs: object) -> object:
        nonlocal interrupts
        if interrupts < 2:
            if interrupts == 0:
                gate.wait(timeout=5)
            interrupts += 1
            raise KeyboardInterrupt("stop")
        release.set()
        active = args[0]
        assert isinstance(active, set)
        return wait_for_futures(active, timeout=0.05, return_when=FIRST_COMPLETED)

    def operation(item: int, cancelled: Event, emit: Callable[[Callable[[], None]], None]) -> None:
        gate.wait(timeout=5)
        assert release.wait(timeout=5)

    monkeypatch.setattr(parallel, "wait", wait)
    with pytest.raises(KeyboardInterrupt, match="stop"):
        parallel.run_jobs(list(range(4)), jobs=2, operation=operation)
    notices = capsys.readouterr().err.splitlines()
    assert notices == [
        "Stopping: no new work starts. Waiting for 2 running call(s) to finish, "
        "so the work already paid for is kept."
    ] * 2
