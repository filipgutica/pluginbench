"""Bounded independent jobs with one coordinator and cancellation-aware draining."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Empty, SimpleQueue
from threading import Event


def validate_jobs(jobs: object) -> None:
    if type(jobs) is not int or not 1 <= jobs <= 16:
        raise ValueError("jobs must be an integer from 1 to 16")


def run_jobs[T](
    items: Sequence[T],
    *,
    jobs: int,
    operation: Callable[[T, Event, Callable[[Callable[[], None]], None]], None],
    on_submit: Callable[[T], None] | None = None,
) -> None:
    """Stop admission on failure; drain active calls and coordinator updates before raising."""
    validate_jobs(jobs)
    cancelled = Event()
    if jobs == 1:
        try:
            for item in items:
                if on_submit:
                    on_submit(item)
                operation(item, cancelled, lambda callback: callback())
        except BaseException:
            cancelled.set()
            raise
        return
    updates: SimpleQueue[Callable[[], None]] = SimpleQueue()
    failures: SimpleQueue[BaseException] = SimpleQueue()
    first_failure: BaseException | None = None

    def perform(item: T) -> None:
        if cancelled.is_set():
            return
        try:
            operation(item, cancelled, updates.put)
        except BaseException as exc:
            if not isinstance(exc, CancelledError) or not cancelled.is_set():
                failures.put(exc)
            cancelled.set()

    def drain_updates() -> None:
        nonlocal first_failure
        while True:
            try:
                failure = failures.get_nowait()
            except Empty:
                break
            first_failure = first_failure or failure
        while True:
            try:
                callback = updates.get_nowait()
            except Empty:
                break
            try:
                callback()
            except BaseException as exc:
                first_failure = first_failure or exc
                cancelled.set()

    iterator = iter(items)
    active: set[Future[None]] = set()
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="pluginbench") as pool:

        def replenish() -> None:
            while len(active) < jobs and not cancelled.is_set():
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                if on_submit:
                    on_submit(item)
                active.add(pool.submit(perform, item))

        try:
            replenish()
        except BaseException as exc:
            first_failure = exc
            cancelled.set()
        while active:
            try:
                drain_updates()
                done, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                active.difference_update(done)
                drain_updates()
                replenish()
            except BaseException as exc:
                first_failure = first_failure or exc
                cancelled.set()
                # Current native calls finish under their existing timeout. No new work is admitted.
    drain_updates()
    if first_failure is not None:
        raise first_failure
