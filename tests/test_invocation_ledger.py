"""Contract C2: every invocation is counted before it starts, and no slot gets a fourth.

Crashes come from a child process that SIGKILLs itself at a named boundary (`resume_support`),
so no except or finally block records anything. Nothing here calls Docker or a model, and no
behaviour depends on time.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import resume_support
from native_support import execution as native_execution
from pluginbench import harbor_backend, processes, provider_stops, source_only, study
from pluginbench.ledger import (
    LEDGER_NAME,
    SCHEMA_VERSION,
    CeilingReached,
    Ledger,
    SlotExhausted,
)
from test_grade import HEALTHY, _coded
from test_resume import _killed_resume, _killed_run


def _invocations(root: Path) -> list[dict[str, Any]]:
    ledger = Ledger.read(root / LEDGER_NAME)
    try:
        return ledger.invocations()
    finally:
        ledger.close()


def _states(root: Path, stage: str = "coding") -> list[str]:
    return [row["state"] for row in _invocations(root) if row["stage"] == stage]


def _one_slot(tmp_path: Path) -> study.ResolvedStudy:
    return study.load_study(
        resume_support.recipe(tmp_path / "config", tasks=1, treatments=0, repeats=1, jobs=1)
    )


# Crash boundaries.


@pytest.mark.parametrize("boundary", ["during-call", "after-identity", "after-execution"])
def test_a_slot_killed_three_times_after_its_intent_never_gets_a_fourth_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    # during-call: spawned, identity not yet recorded; after-identity: running, no output;
    # after-execution: output saved, not yet accepted.
    root = _killed_run(tmp_path, boundary=boundary, at=1, tasks=1, treatments=0, repeats=1)
    _killed_resume(root, boundary)
    _killed_resume(root, boundary)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    with pytest.raises(SlotExhausted, match="slot 0 used its 3 coding invocations"):
        study.resume_study(root)
    assert calls == []
    rows = _invocations(root)
    assert [row["state"] for row in rows] == ["unresolved"] * 3
    assert [row["n"] for row in rows] == [1, 2, 3]
    started = boundary != "during-call"
    assert all((row["identity"] is not None) is started for row in rows)


def test_a_kill_before_acceptance_keeps_the_saved_output_as_the_finished_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record receipt is on disk; the ledger had not accepted it when the run was killed.
    root = _killed_run(tmp_path, boundary="after-record", at=1, tasks=1, treatments=0, repeats=1)
    assert _states(root) == ["started"]
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    study.resume_study(root)
    assert calls == [] and _states(root) == ["finished"]


def test_the_intent_fixes_the_scope_and_container_label_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _killed_run(tmp_path, boundary="during-call", at=1, tasks=1, treatments=0, repeats=1)
    [row] = _invocations(root)
    assert row["state"] == "intent" and row["identity"] is None
    assert row["scope"] == f"pb-inv-{row['id']}.scope"
    assert row["container_label"] == f"pluginbench.invocation={row['id']}"


# Halts and the ceiling.


def test_provider_halts_leave_the_slot_cap_but_count_toward_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    monkeypatch.setattr(provider_stops, "saved_stop", lambda artifacts, harness: "a usage limit")
    with pytest.raises(study.ProviderStopped):
        study.run_study(_one_slot(tmp_path), tmp_path / "run")
    root = tmp_path / "run"
    for _ in range(2):
        with pytest.raises(study.ProviderStopped):
            study.resume_study(root)
    assert _states(root) == ["halted_provider"] * 3
    ledger = Ledger.open(root / LEDGER_NAME)
    try:
        assert ledger.refusal(0, "coding") is None  # A halt gives the slot its call back.
    finally:
        ledger.close()
    # One slot's ceiling is 3 paid invocations, and every halted one counts.
    with pytest.raises(CeilingReached, match="ceiling of 3 paid invocations"):
        study.resume_study(root)
    assert len(calls) == 3 and _states(root) == ["halted_provider"] * 3


# Deterministic short-circuit.


@pytest.mark.parametrize("same", [True, False])
def test_the_same_infrastructure_error_twice_ends_the_slot_without_a_third_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same: bool
) -> None:
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def undelivered(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["entry"]["id"]))
        kwargs["on_spawn"](os.getpid())
        area = kwargs["area"]
        execution = native_execution(area, kwargs["protocol"]["native_task_identity"], reward=1)
        cause = "missing skill" if same else f"missing skill {len(calls)}"
        return {**execution, "treatment_error": cause}

    monkeypatch.setattr(harbor_backend, "invoke_native", undelivered)
    with pytest.raises(study.CodingInfrastructure):
        study.run_study(_one_slot(tmp_path), tmp_path / "run")
    root = tmp_path / "run"
    with pytest.raises(study.CodingInfrastructure):
        study.resume_study(root)
    if same:
        with pytest.raises(SlotExhausted, match="twice with the same error"):
            study.resume_study(root)
        assert len(calls) == 2
        return
    with pytest.raises(study.CodingInfrastructure):
        study.resume_study(root)
    with pytest.raises(SlotExhausted, match="used its 3 coding invocations"):
        study.resume_study(root)
    assert len(calls) == 3 and _states(root) == ["failed_transient"] * 3


# Grading.


def _regrade(
    replays: list[str], *, exception: str | None, reward: int | None = None
) -> Any:
    def regrade(**kwargs: Any) -> dict[str, object]:
        kwargs["on_spawn"](os.getpid())
        replays.append(str(kwargs["directory"]))
        if exception is None:
            rewards = {} if reward is None else {"reward": reward}
            return {"verifier_result": {"rewards": rewards}, "exception_info": None}
        message = exception.format(len(replays))
        return {
            "verifier_result": None,
            "exception_info": {"exception_type": "DockerDaemonError", "exception_message": message},
        }

    return regrade


@pytest.mark.parametrize(("message", "replayed"), [("down", 2), ("down {}", 3)])
def test_grading_spends_at_most_three_replays_per_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, replayed: int
) -> None:
    def unverified(area: Path, execution: dict[str, Any]) -> None:
        execution["native_verifier_result"] = None

    root, _ = _coded(tmp_path, monkeypatch, unverified)
    replays: list[str] = []
    monkeypatch.setattr(harbor_backend, "regrade_native", _regrade(replays, exception=message))
    monkeypatch.setattr(harbor_backend, "_assert_native_image", lambda image, expected: None)
    for _ in range(4):
        index = source_only.record_source_only_grades(root, requalify=HEALTHY)
    # Two attempts, so two slots; the same message twice ends a slot after two.
    assert len(replays) == 2 * replayed
    assert _states(root, "grading") == ["failed_transient"] * (2 * replayed)
    pending = study._bound_document(root, index)["pending"]
    reason = "twice with the same error" if replayed == 2 else "used its 3 grading invocations"
    assert all(reason in stages["handoff"] for stages in pending.values()) and len(pending) == 2


def test_the_one_regrade_of_a_filtered_patch_is_its_second_grading_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unverified(area: Path, execution: dict[str, Any]) -> None:
        execution["native_verifier_result"] = None

    def agent_test(workspace: Path) -> None:
        (workspace / "tests").mkdir(exist_ok=True)
        (workspace / "tests/test_extra.py").write_text("def test_extra():\n    pass\n")

    root, _ = _coded(tmp_path, monkeypatch, unverified, write=agent_test)
    replays: list[str] = []
    monkeypatch.setattr(harbor_backend, "regrade_native", _regrade(replays, exception=None))
    monkeypatch.setattr(harbor_backend, "_assert_native_image", lambda image, expected: None)
    source_only.record_source_only_grades(root, requalify=HEALTHY)
    assert len(replays) == 4 and _states(root, "grading") == ["finished"] * 4
    assert [row["n"] for row in _invocations(root) if row["stage"] == "grading"] == [1, 2] * 2


# The ledger itself.


def test_a_grading_pass_resolves_only_grading_invocations(tmp_path: Path) -> None:
    ledger = Ledger.create(
        tmp_path / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "baseline", "attempt": 1}],
        {"a": "pending"},
    )
    try:
        coding = ledger.intent(slot=0, stage="coding", attempt_id="a")
        ledger.intent(slot=0, stage="grading", attempt_id="a")
        assert ledger.resolve_crashed("grading") == 1
        assert {row["stage"]: row["state"] for row in ledger.invocations()} == {
            "coding": "intent",
            "grading": "unresolved",
        }
        ledger.end(coding, "finished")
    finally:
        ledger.close()


def test_a_version_1_ledger_is_read_as_it_is_and_migrated_by_a_writer(tmp_path: Path) -> None:
    path = tmp_path / LEDGER_NAME
    ledger = Ledger.create(
        path,
        [{"id": f"a{slot}", "task_id": "t", "arm": "baseline", "attempt": slot} for slot in (1, 2)],
        {"a1": "pending", "a2": "pending"},
    )
    ledger.close()
    connection = sqlite3.connect(path)
    connection.executescript("DROP TABLE invocations; DROP TABLE budget; PRAGMA user_version = 1;")
    connection.close()
    observer = Ledger.read(path)
    assert observer.invocations() == [] and len(observer.attempts()) == 2
    observer.close()
    writer = Ledger.open(path)
    try:
        for call in range(6):
            halted = writer.intent(slot=call % 2, stage="coding", attempt_id="a1")
            writer.end(halted, "halted_provider")
        # Both slots' caps: the default ceiling a migrated ledger gets.
        with pytest.raises(CeilingReached, match="ceiling of 6"):
            writer.intent(slot=0, stage="coding", attempt_id="a1")
    finally:
        writer.close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    connection.close()


# The spawn hook on the real process path.


def test_run_command_reports_its_child_and_stops_it_when_the_report_fails(
    tmp_path: Path,
) -> None:
    seen: list[int] = []
    result = processes.run_command(
        ["true"], cwd=tmp_path, env={"PATH": os.environ["PATH"]}, timeout=10, on_spawn=seen.append
    )
    assert result.returncode == 0 and len(seen) == 1

    def refuse(pid: int) -> None:
        seen.append(pid)
        raise RuntimeError("ledger write failed")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="ledger write failed"):
        processes.run_command(
            ["sleep", "30"], cwd=tmp_path, env={"PATH": os.environ["PATH"]}, timeout=60,
            on_spawn=refuse,
        )
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.killpg(seen[-1], 0)
