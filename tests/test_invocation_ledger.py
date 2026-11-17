"""Contract C2: every invocation is counted before it starts, and no slot gets a fourth.

Crashes come from a child process that SIGKILLs itself at a named boundary (`resume_support`),
so no except or finally block records anything. Nothing here calls Docker or a model, and no
behaviour depends on time.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import operation_support
import resume_support
import test_baselines
import test_study_review
from native_support import execution as native_execution, runtime_identity, synthetic_contract
from pluginbench import harbor_backend, processes, provider_stops, source_only, study, study_review
from pluginbench import codex, invocation_runtime, submissions, trial_containers
from pluginbench import ledger as invocation_ledger
from pluginbench.control_store import CleanupBlocked
from pluginbench.ledger import (
    LEDGER_NAME,
    SCHEMA_VERSION,
    CeilingReached,
    Ledger,
    SlotExhausted,
)
from test_grade import HEALTHY, _coded
from test_resume import _PATH, _killed_resume, _killed_run
from test_provider_stops import codex_rollout

# Every resume here recovers what a killed run left, which needs the host execution lock.
pytestmark = pytest.mark.usefixtures("host_execution")


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


def test_a_grading_pass_resolves_only_grading_invocations_whose_cleanup_is_verified(
    tmp_path: Path,
) -> None:
    ledger = Ledger.create(
        tmp_path / LEDGER_NAME,
        [{"id": "a", "task_id": "t", "arm": "baseline", "attempt": 1}],
        {"a": "pending"},
    )
    try:
        coding = ledger.intent(slot=0, stage="coding", attempt_id="a")
        grading = ledger.intent(slot=0, stage="grading", attempt_id="a")
        with pytest.raises(ValueError, match=f"{grading} is not verified"):
            ledger.resolve_crashed("grading", cleaned=set())
        assert _states(tmp_path, "grading") == ["intent"]
        assert ledger.resolve_crashed("grading", cleaned={grading}) == 1
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
    with pytest.raises(ValueError, match="no invocation history"):
        Ledger.open(path)
    writer = Ledger.open(path, history=lambda: [])
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


# Review calls share the ceiling (finding 1).


def test_a_review_that_would_pass_the_ceiling_is_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    ledger = Ledger.open(root / LEDGER_NAME)
    try:
        # Two coding calls finished; four halts use the rest of the two slots' ceiling of 6.
        for call in range(4):
            halted = ledger.intent(slot=call % 2, stage="coding", attempt_id="halted")
            ledger.end(halted, "halted_provider")
    finally:
        ledger.close()
    calls = test_study_review._judge(monkeypatch, root)
    with pytest.raises(CeilingReached, match="2 review calls"):
        study_review.review_study(root)
    assert calls == [] and not (root / "review-revisions/review").exists()


def test_earlier_review_revisions_count_toward_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    ledger = Ledger.open(root / LEDGER_NAME)
    try:
        halted = ledger.intent(slot=0, stage="coding", attempt_id="halted")
        ledger.end(halted, "halted_provider")
    finally:
        ledger.close()
    calls = test_study_review._judge(monkeypatch, root)
    study_review.review_study(root)  # 3 coding rows + 2 reviews of the ceiling of 6.
    with pytest.raises(CeilingReached):
        study_review.review_study(root, revision="second")
    assert len(calls) == 2


# Migration keeps the history a version 1 ledger could not hold (finding 2).


def _as_version_1(root: Path) -> None:
    connection = sqlite3.connect(root / LEDGER_NAME)
    connection.executescript("DROP TABLE invocations; DROP TABLE budget; PRAGMA user_version = 1;")
    connection.close()


def test_a_migrated_ledger_counts_the_calls_its_receipts_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def undelivered(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["entry"]["id"]))
        execution = native_execution(
            kwargs["area"], kwargs["protocol"]["native_task_identity"], reward=1
        )
        return {**execution, "treatment_error": f"missing skill {len(calls)}"}

    monkeypatch.setattr(harbor_backend, "invoke_native", undelivered)
    with pytest.raises(study.CodingInfrastructure):
        study.run_study(_one_slot(tmp_path), tmp_path / "run")
    root = tmp_path / "run"
    for _ in range(2):
        with pytest.raises(study.CodingInfrastructure):
            study.resume_study(root)
    # As a run coded before invocation rows existed left it: three receipts, no rows.
    _as_version_1(root)
    with pytest.raises(SlotExhausted, match="used its 3 coding invocations"):
        study.resume_study(root)
    assert len(calls) == 3 and _states(root) == ["unresolved"] * 3


def test_a_version_1_ledger_whose_receipts_name_no_known_attempt_is_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    with pytest.raises(study.ProviderStopped):
        monkeypatch.setattr(provider_stops, "saved_stop", lambda artifacts, harness: "a limit")
        study.run_study(_one_slot(tmp_path), tmp_path / "run")
    root = tmp_path / "run"
    _as_version_1(root)
    stray = root / "invocations/0123456789abcdef/handoff.json"
    stray.parent.mkdir(parents=True)
    stray.write_text("{}")
    with pytest.raises(ValueError, match="cannot tell which slot"):
        study.resume_study(root)
    assert len(calls) == 1


# Cached vanilla regrades use the grading budget (finding 3).


def test_cached_vanilla_regrades_spend_the_slot_s_grading_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime_identity())
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    monkeypatch.setattr(trial_containers, "running", lambda evidence, **kwargs: [])

    def invoke(**kwargs: Any) -> dict[str, Any]:
        area = kwargs["area"]
        (area / "workspace/answer.py").write_text("answer = 1\n")
        return native_execution(
            area,
            kwargs["protocol"]["native_task_identity"],
            answer_path="answer.py",
            timeout=1800,
            version="0.143.0",
        )

    regrades: list[str] = []

    def regrade(**kwargs: Any) -> dict[str, object]:
        regrades.append(str(kwargs["directory"]))
        raise OSError(f"docker went away {len(regrades)}")

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    one = test_baselines._study(tmp_path / "one")
    study.run_study(
        one, tmp_path / "first", baseline_cache=tmp_path / "cache",
        baseline_identity=test_baselines._coding(one.tasks),
    )
    monkeypatch.setattr(harbor_backend, "regrade_native", regrade)
    two = test_baselines._study(tmp_path / "two", guidance="another")
    root = tmp_path / "second"
    cache: dict[str, Any] = {
        "baseline_cache": tmp_path / "cache",
        "baseline_identity": test_baselines._coding(two.tasks),
    }
    failures: list[BaseException] = []
    for attempt in range(8):
        try:
            if attempt == 0:
                study.run_study(two, root, **cache)
            else:
                study.resume_study(root, **cache)
        except (OSError, ValueError) as error:
            failures.append(error)
    grading = [row for row in _invocations(root) if row["stage"] == "grading"]
    # Two cached vanilla slots, three replays each; every replay was recorded before it ran.
    assert len(regrades) == len(grading) == 6
    assert {row["state"] for row in grading} == {"failed_transient"}
    assert isinstance(failures[-1], study.CodingInfrastructure)
    assert str(failures[-1]).count("used its 3 grading invocations") == 2


# A grade's replay finishes only once its grade is saved (finding 4).


def test_a_replay_whose_grade_is_not_saved_is_not_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unverified(area: Path, execution: dict[str, Any]) -> None:
        execution["native_verifier_result"] = None

    root, _ = _coded(tmp_path, monkeypatch, unverified)
    replays: list[str] = []
    monkeypatch.setattr(
        harbor_backend, "regrade_native", _regrade(replays, exception=None, reward=1)
    )
    monkeypatch.setattr(harbor_backend, "_assert_native_image", lambda image, expected: None)
    keep = source_only._keep
    saves: list[Path] = []

    def full_disk(staging: Path | None, target: Path, record: dict[str, Any]) -> None:
        saves.append(target)
        if len(saves) <= 2:
            raise OSError(f"no space left on device ({len(saves)})")
        keep(staging, target, record)

    monkeypatch.setattr(source_only, "_keep", full_disk)
    for _ in range(2):
        with pytest.raises(OSError, match="no space"):
            source_only.record_source_only_grades(root, requalify=HEALTHY)
    index = source_only.record_source_only_grades(root, requalify=HEALTHY)
    assert study._bound_document(root, index)["pending"] == {}
    by_slot: dict[int, list[str]] = {}
    for row in _invocations(root):
        if row["stage"] == "grading":
            by_slot.setdefault(row["slot"], []).append(row["state"])
    # Each failed save stopped the pass at the first slot, whose replay never finished.
    assert sorted(by_slot.values()) == [
        ["failed_transient", "failed_transient", "finished"],
        ["finished"],
    ]


# Every judge call is a ledger row (the re-review findings).


def test_reviews_of_a_version_1_run_are_migrated_and_capped_per_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    _as_version_1(root)
    calls = test_study_review._judge(monkeypatch, root)
    for revision in ("first", "second", "third"):
        study_review.review_study(root, revision=revision)
    with pytest.raises(SlotExhausted, match="used its 3 review invocations"):
        study_review.review_study(root, revision="fourth")
    assert len(calls) == 6 and not (root / "review-revisions/fourth").exists()
    assert _states(root, "review") == ["finished"] * 6
    connection = sqlite3.connect(root / LEDGER_NAME)
    try:
        # Two slots, each coded and reviewed: the default ceiling of a reviewed run.
        assert connection.execute("SELECT paid_call_ceiling FROM budget").fetchone()[0] == 12
    finally:
        connection.close()


def test_a_run_without_a_ledger_is_not_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    (root / LEDGER_NAME).unlink()
    calls = test_study_review._judge(monkeypatch, root)
    with pytest.raises(ValueError, match="no invocation ledger"):
        study_review.review_study(root)
    assert calls == [] and not (root / "review-revisions/review").exists()


def _review_child() -> None:
    """argv: run. Reviews it with a judge call that SIGKILLs this process as it starts."""
    patch = pytest.MonkeyPatch()
    operation_support.in_place(patch)
    patch.setattr(submissions, "reviewed_contract", synthetic_contract)
    patch.setattr(codex, "invoke", lambda **kwargs: os.kill(os.getpid(), signal.SIGKILL))
    study_review.review_study(Path(sys.argv[1]))
    raise SystemExit("the review finished without reaching its judge call")


def test_a_judge_call_killed_as_it_starts_still_counts_toward_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    ledger = Ledger.open(root / LEDGER_NAME)
    try:
        for slot in (0, 1):  # Two halts beside the two coding calls: 4 of the ceiling of 6.
            ledger.end(ledger.intent(slot=slot, stage="coding", attempt_id="halt"), "halted_provider")
    finally:
        ledger.close()
    child = subprocess.run(
        [sys.executable, "-c", "import test_invocation_ledger as t; t._review_child()", str(root)],
        env={**os.environ, "PYTHONPATH": _PATH},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    calls = test_study_review._judge(monkeypatch, root)
    # The killed call is the fifth paid invocation, so two more reviews would make seven.
    with pytest.raises(CeilingReached):
        study_review.review_study(root, revision="second")
    assert calls == [] and _states(root, "review") == ["unresolved"]


def test_coding_resumed_after_a_review_counts_the_review_s_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def vanilla_fails(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["entry"]["id"]))
        area = kwargs["area"]
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        execution = native_execution(area, kwargs["protocol"]["native_task_identity"], reward=1)
        if kwargs["entry"]["arm"] == "baseline":
            return {**execution, "treatment_error": f"missing skill {len(calls)}"}
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", vanilla_fails)
    recipe = resume_support.recipe(tmp_path / "config", tasks=1, treatments=1, repeats=1, jobs=1)
    root = tmp_path / "run"
    with pytest.raises(study.CodingInfrastructure):
        study.run_study(study.load_study(recipe), root, paid_call_ceiling=3)
    reviews = test_study_review._judge(monkeypatch, root)
    study_review.review_study(root)  # The package attempt's review: the third paid call.
    assert len(reviews) == 1
    with pytest.raises(CeilingReached):
        study.resume_study(root)
    assert len(calls) == 2


def test_a_derived_citation_revision_adds_no_review_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    calls = test_study_review._judge(
        monkeypatch, root, citation_evidence="\tanswer = 1", citation_line=1, severity="P2"
    )
    study_review.review_study(root, revision="fixed")
    study_review.recover_review_citations(root, "fixed")
    assert (root / "review-revisions/fixed-citations").is_dir()
    # 2 coding and 2 review calls of the ceiling of 6; the recovery made none.
    study_review.review_study(root, revision="second")
    # Each review's citation fails validation, an infrastructure error under C1; it still counts.
    assert len(calls) == 4 and _states(root, "review") == ["failed_transient"] * 4


# Review outcomes and inputs (the second re-review findings).


@pytest.mark.parametrize("raises", [False, True])
def test_a_provider_halt_in_review_gives_the_call_back_and_stops_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    calls = test_study_review._judge(monkeypatch, root)
    judged = codex.invoke

    def limited(**kwargs: Any) -> dict[str, object]:
        execution = judged(**kwargs)
        trace = kwargs["area"] / "artifacts/host-trace-0.jsonl"
        trace.write_text(codex_rollout("usage_limit_exceeded"))
        if raises:  # As the launcher can, once the trace is saved.
            raise ValueError("native artifacts failed validation")
        return execution

    monkeypatch.setattr(codex, "invoke", limited)
    with pytest.raises(study.ProviderStopped, match="usage_limit_exceeded"):
        study_review.review_study(root)
    # The halt stops the pass: the other attempt's judge is never called.
    assert len(calls) == 1 and _states(root, "review") == ["halted_provider"]
    [halted] = _invocations(root)[-1:]
    ledger = Ledger.open(root / LEDGER_NAME)
    try:
        assert ledger.refusal(halted["slot"], "review") is None
        assert ledger.paid_room() == 6 - 2 - 1  # Two coding calls and the halt.
    finally:
        ledger.close()


def test_a_slot_that_failed_twice_for_one_reviewer_is_still_reviewed_by_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    connection = sqlite3.connect(root / LEDGER_NAME)
    with connection:  # Room for every call, so only the cap and short-circuit refuse.
        connection.execute("UPDATE budget SET paid_call_ceiling = 100")
    connection.close()
    calls = test_study_review._judge(
        monkeypatch, root, citation_evidence="\tanswer = 1", citation_line=1, severity="P2"
    )
    judged = codex.invoke
    # The fake answers as the frozen reviewer; another model's answer then fails validation.
    monkeypatch.setattr(
        codex,
        "invoke",
        lambda **kwargs: judged(
            **{**kwargs, "protocol": {**kwargs["protocol"], "models": {"codex": "gpt-6-astra"}}}
        ),
    )
    for revision in ("first", "second"):
        study_review.review_study(root, revision=revision)
    # The same error twice ends each slot for this reviewer, not for another.
    with pytest.raises(SlotExhausted, match="twice with the same error"):
        study_review.review_study(root, revision="third")
    study_review.review_study(root, revision="other", model="gpt-6-luna")
    assert len(calls) == 6
    # The three-call cap is shared by every reviewer and revision.
    with pytest.raises(SlotExhausted, match="used its 3 review invocations"):
        study_review.review_study(root, revision="last", model="gpt-6.1-sol")
    assert len(calls) == 6


def test_a_judge_call_whose_observation_is_not_saved_is_not_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    test_study_review._judge(monkeypatch, root)
    write = study._write

    def failing(path: Path, value: object) -> None:
        if path.name == "observation.json":
            raise OSError("disk full")
        write(path, value)

    monkeypatch.setattr(study, "_write", failing)
    with pytest.raises(OSError, match="disk full"):
        study_review.review_study(root)
    assert _states(root, "review") == ["failed_transient"]
    state = json.loads((root / "review-revisions/review/review-state.json").read_text())
    assert "reviewed" not in [row["status"] for row in state["records"]]


def test_a_reviewed_row_without_an_observation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    test_study_review._judge(monkeypatch, root)
    study_review.review_study(root)
    path = root / "review-revisions/review/review-state.json"
    state = json.loads(path.read_text())
    state["records"][0]["observation"] = None
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="reviewed row has no bound observation"):
        study_review.validate_review_revision(root, "review")


def test_an_operation_whose_scope_was_launched_counts_as_spent_though_never_admitted() -> None:
    """Its bootstrap ran, so a scope that failed its startup gate is no refundable abort; a
    call refused before anything spawned still is."""
    launched = invocation_runtime.OperationFailed("the operation's scope did not start")
    assert invocation_ledger.outcome(launched, spawned=False)[0] == "failed_transient"
    assert invocation_ledger.outcome(ValueError("refused"), spawned=False) == (
        "aborted_prespawn", None
    )


# The owner's real `run_operation`, which the autouse fixture replaces with `run_in_place`.
_RUN_OPERATION = invocation_runtime.run_operation


def _through_run_operation(
    monkeypatch: pytest.MonkeyPatch, *, launch: Any, cleanup: Any
) -> None:
    """Run the study's operations through the owner's real `run_operation`: it records each
    operation and verifies its cleanup. `launch` stands in for the scope, `cleanup` for the
    verification."""
    monkeypatch.setattr(invocation_runtime, "run_operation", _RUN_OPERATION)
    monkeypatch.setattr(invocation_runtime, "_launch", launch)
    monkeypatch.setattr(invocation_runtime, "verify_cleanup", cleanup)


def test_an_unverified_cleanup_holds_its_slot_and_starts_no_other_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_execution: Any
) -> None:
    """The call ran, then a container of it survived its removal. A retryable error would free
    its slot for the next task; this stops the run with the call spent, and the host fenced."""
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def launch(lock: Any, request: Any, command: Any, directory: Any, started: Any, progress: Any) -> Any:
        return operation_support.run_in_place(request, started=started, progress=progress)

    def survived(operation: dict[str, Any], *, lock: Any) -> None:
        raise CleanupBlocked(f"a container of invocation {operation['id']} survived its removal")

    _through_run_operation(monkeypatch, launch=launch, cleanup=survived)
    resolved = study.load_study(
        resume_support.recipe(tmp_path / "config", tasks=3, treatments=0, repeats=1, jobs=1)
    )

    with pytest.raises(CleanupBlocked, match="survived its removal"):
        study.run_study(resolved, tmp_path / "run")

    assert len(calls) == 1
    assert _states(tmp_path / "run") == ["unresolved"]  # Spent, never refunded.
    assert host_execution.leftover


def test_a_coding_call_whose_scope_never_admitted_it_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its bootstrap ran, so it is no refundable abort, though coding saves the failure as an
    infrastructure error."""
    resume_support.install(monkeypatch, [])

    def never_admitted(*args: Any) -> Any:
        raise invocation_runtime.OperationFailed("the operation's scope did not start")

    _through_run_operation(monkeypatch, launch=never_admitted, cleanup=lambda operation, *, lock: None)

    with pytest.raises(study.CodingInfrastructure, match="scope did not start"):
        study.run_study(_one_slot(tmp_path), tmp_path / "run")

    assert _states(tmp_path / "run") == ["failed_transient"]


def test_an_unverified_review_cleanup_ends_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review pass records a failed call and goes on; a call whose cleanup was not verified
    stops it, with the call spent."""
    root = test_study_review._existing_study(tmp_path, monkeypatch)
    calls = test_study_review._judge(monkeypatch, root, behavior="clean")

    def launch(lock: Any, request: Any, command: Any, directory: Any, started: Any, progress: Any) -> Any:
        return operation_support.run_in_place(request, started=started, progress=progress)

    def survived(operation: dict[str, Any], *, lock: Any) -> None:
        raise CleanupBlocked(f"a process of invocation {operation['id']} survived its scope")

    _through_run_operation(monkeypatch, launch=launch, cleanup=survived)

    with pytest.raises(CleanupBlocked, match="survived its scope"):
        study_review.review_study(root)

    assert len(calls) == 1
    assert _states(root, "review") == ["unresolved"]
