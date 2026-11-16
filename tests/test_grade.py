"""Grade contract 1: one fixture per C1 coding and grade row, and the producer inventory.

Coding rows run a whole study through the fake Harbor backend; grade rows then grade it with
`record_source_only_grades`. Nothing here calls Docker or a model.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import resume_support
from native_support import execution as native_execution
from pluginbench import claude, claude_usage, codex, grade, harbor_backend, source_only, study
from pluginbench import submissions
from test_codex import _OUTAGE, outage_trace, stream
from test_provider_stops import PLAIN, _api_error

Invoke = Callable[..., dict[str, Any]]
HEALTHY: source_only.Requalify = lambda task: None  # noqa: E731


def _study(tmp_path: Path) -> study.ResolvedStudy:
    """One task, vanilla and one package, one repeat: two attempts."""
    return study.load_study(
        resume_support.recipe(
            tmp_path / "config", tasks=1, treatments=1, repeats=1, jobs=1, solution=""
        )
    )


def _coded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Path, dict[str, Any]], None],
    *,
    write: Callable[[Path], None] = lambda workspace: None,
) -> tuple[Path, list[str]]:
    """A finished run whose every attempt writes the answer and `write`s, then `change`s."""
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["entry"]["id"]))
        area = kwargs["area"]
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        write(area / "workspace")
        execution = native_execution(area, kwargs["protocol"]["native_task_identity"], reward=1)
        change(area, execution)
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    root = study.run_study(_study(tmp_path), tmp_path / "run")
    return root, calls


def _graded(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requalify: source_only.Requalify = HEALTHY,
    reward: int | None = 1,
    exception: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[bytes]]:
    """Grade a finished run: each attempt's grade record, each infrastructure error, replays.

    Each replay returns `reward`, or no reward and Harbor's `exception` when one is given.
    """
    replayed: list[bytes] = []

    def regrade(**kwargs: Any) -> dict[str, object]:
        trial = kwargs["artifacts"] / "harbor/trial/artifacts"
        replayed.append((trial / submissions.PATCH_ARTIFACT).read_bytes())
        if exception is not None:
            return {"verifier_result": None, "exception_info": exception}
        rewards = {"reward": reward} if reward is not None else {}
        return {"verifier_result": {"rewards": rewards}, "exception_info": None}

    monkeypatch.setattr(harbor_backend, "regrade_native", regrade)
    monkeypatch.setattr(harbor_backend, "_assert_native_image", lambda image, expected: None)
    index = study._bound_document(
        root, source_only.record_source_only_grades(root, requalify=requalify)
    )
    records = {
        attempt: study._bound_document(root, stages["handoff"])
        for attempt, stages in index["records"].items()
    }
    pending = {attempt: stages["handoff"] for attempt, stages in index["pending"].items()}
    return records, pending, replayed


def _harbor_error(kind: str) -> dict[str, str]:
    return {"exception_type": kind, "exception_message": f"{kind} in the fixture"}


# Coding stage.


def test_a_timeout_after_the_first_turn_is_graded_once_and_never_coded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(area: Path, execution: dict[str, Any]) -> None:
        # Harbor records the agent's timeout and still collects and verifies its patch.
        execution.update(
            timed_out=True,
            successful_native_completion=False,
            error=_harbor_error("AgentTimeoutError"),
        )

    root, calls = _coded(tmp_path, monkeypatch, timed_out)
    run = json.loads((root / "run.json").read_text())
    assert run["status"] == "complete" and run["actual_calls"] == len(calls) == 2
    records, pending, replayed = _graded(root, monkeypatch)
    assert pending == {} and replayed == []
    for record in records.values():
        assert record["passed"] is True and record["fail_reason"] is None
        assert record["attributes"]["timed_out"] is True
        assert record["attributes"]["agent_error"] is False


def test_a_timed_out_attempt_whose_verifier_never_graded_is_regraded_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unverified(area: Path, execution: dict[str, Any]) -> None:
        execution.update(timed_out=True, native_verifier_result=None)

    root, calls = _coded(tmp_path, monkeypatch, unverified)
    records, pending, replayed = _graded(root, monkeypatch, reward=0)
    assert len(calls) == 2 and len(replayed) == 2 and pending == {}
    assert {record["passed"] for record in records.values()} == {False}
    assert {record["fail_reason"] for record in records.values()} == {None}


def test_an_agent_error_after_the_first_turn_is_graded_with_its_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exited(area: Path, execution: dict[str, Any]) -> None:
        execution.update(
            successful_native_completion=False, error=_harbor_error("NonZeroAgentExitCodeError")
        )

    root, calls = _coded(tmp_path, monkeypatch, exited)
    records, pending, _ = _graded(root, monkeypatch)
    assert len(calls) == 2 and pending == {}
    for record in records.values():
        assert record["passed"] is True
        assert record["attributes"] == {
            "native_reward": True,
            "test_overlap": False,
            "empty_patch": False,
            "timed_out": False,
            "agent_error": True,
            "mixed_model": False,
        }


def test_a_descendant_on_another_model_is_graded_with_mixed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mixed(area: Path, execution: dict[str, Any]) -> None:
        execution["actual_descendant_models"] = ["another-model"]

    root, calls = _coded(tmp_path, monkeypatch, mixed)
    records, pending, _ = _graded(root, monkeypatch)
    # Graded, never replaced: the attempt has no single model, and it counts per arm.
    assert len(calls) == 2 and pending == {}
    assert all(record["attributes"]["mixed_model"] for record in records.values())
    assert all(record["passed"] for record in records.values())


def test_a_session_that_failed_before_its_first_turn_is_found_for_either_harness() -> None:
    codex_session = codex.parse_events(stream(*_OUTAGE), [outage_trace("header")], "codex")
    codex_session["timed_out"] = False
    assert grade.failed_before_first_turn(codex_session, "codex") == (
        "workspace routing discovery failed"
    )
    started: dict[str, object] = {"type": "system", "subtype": "init", "model": "m", "plugins": [], "mcp_servers": []}
    failed: dict[str, object] = {"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "API Error: Connection error."}
    claude_session = claude.parse_events(stream(started, failed), [])
    assert grade.failed_before_first_turn(claude_session, "claude") == "API Error: Connection error."
    # Any model turn makes it the agent's attempt, as does a timeout.
    answer: dict[str, object] = {"type": "assistant", "message": {"model": "m", "content": [
        {"type": "text", "text": "Working on it."}
    ]}}
    worked = claude.parse_events(stream(started, answer, failed), [])
    assert grade.failed_before_first_turn(worked, "claude") is None
    assert grade.failed_before_first_turn({**worked, "timed_out": True}, "claude") is None
    # A timeout alone proves no turn: the session may have hung before its first one.
    silent = claude.parse_events(stream(started), [])
    assert grade.failed_before_first_turn({**silent, "timed_out": True}, "claude") == (
        "the session timed out before any model turn"
    )


@pytest.mark.parametrize("after_work", [False, True])
def test_a_claude_api_error_counts_as_a_turn_only_after_real_model_work(after_work: bool) -> None:
    # The CLI writes the error as an assistant message from model `<synthetic>`: no model call.
    init, assistant, error, result = _api_error("invalid_request", 400, "API Error: 400 bad")
    events = [init, assistant, error, result] if after_work else [init, error, result]
    if not after_work:
        result["num_turns"] = 0
    session = claude.parse_events(stream(*events), [], coding=True)
    session["timed_out"] = False
    expected = None if after_work else "API Error: 400 bad"
    assert grade.failed_before_first_turn(session, "claude") == expected


def test_a_claude_timeout_whose_transcript_shows_model_work_is_the_agent_s_attempt(
    tmp_path: Path,
) -> None:
    # The stream was cut after system/init, but the session transcript kept the model's reply,
    # so usage does not validate and the total is None.
    init = PLAIN.read_text().splitlines()[0]
    session = claude.parse_events(init, [], coding=True)
    session.update(
        timed_out=True,
        **claude_usage.coding_usage(
            init, PLAIN.parent / "sessions", runner_model="claude-opus-5-5", cli_version="2.1.322"
        ),
    )
    assert session["usage"] is None
    assert session["usage_accounting"]["per_model"]["claude-opus-5-5"][
        "transcript_output_tokens"
    ] == 4
    assert grade.failed_before_first_turn(session, "claude") is None
    # Without the transcript the same timeout shows no turn.
    session["usage_accounting"] = {"status": "incomplete", "reasons": ["no transcript"]}
    assert grade.failed_before_first_turn(session, "claude") == (
        "the session timed out before any model turn"
    )


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_a_cli_that_exits_before_any_model_turn_failed_before_its_first_turn(
    harness: str,
) -> None:
    # A CLI configuration error: no turn.failed, no result message, only Harbor's exit error.
    if harness == "codex":
        session = codex.parse_events(stream({"type": "thread.started", "thread_id": "t"}), [],
                                     "codex")
    else:
        session = claude.parse_events(stream(), [])
    session.update(timed_out=False, error=_harbor_error("NonZeroAgentExitCodeError"))
    assert grade.failed_before_first_turn(session, harness) == (
        "NonZeroAgentExitCodeError: NonZeroAgentExitCodeError in the fixture"
    )
    assert grade.failed_before_first_turn({**session, "error": None}, harness) == (
        "the session ended before any model turn"
    )


@pytest.mark.parametrize("timed_out", [False, True])
def test_a_session_with_no_model_turn_is_never_graded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timed_out: bool
) -> None:
    def startup_failure(area: Path, execution: dict[str, Any]) -> None:
        # Exited non-zero, or timed out, before any usage, message or command.
        execution.update(
            usage=None,
            observed_token_usage=False,
            agent_messages=[],
            successful_native_completion=False,
            timed_out=timed_out,
            error=_harbor_error("AgentTimeoutError" if timed_out else "NonZeroAgentExitCodeError"),
        )

    with pytest.raises(study.BackendUnavailable, match="failed before its first model turn"):
        _coded(tmp_path, monkeypatch, startup_failure)
    root = tmp_path / "run"
    states = json.loads((root / "run.json").read_text())["attempt_states"]
    assert set(states.values()) <= {"interrupted", "pending"} and "interrupted" in states.values()
    assert not list(root.glob("records/*.json"))  # Nothing complete, so nothing to grade.


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda area, execution: execution.update(treatment_error="discovery mismatch"),
            "the treatment was not delivered: discovery mismatch",
        ),
        (
            lambda area, execution: execution.update(error=_harbor_error("EnvironmentStartTimeoutError")),
            "Harbor trial failed: EnvironmentStartTimeoutError",
        ),
        (
            lambda area, execution: execution.update(
                submission_collected=False, submission_error="the container was lost"
            ),
            "the container was lost",
        ),
        (
            # The export changed after Harbor bound its digest (study.py: export freezing).
            lambda area, execution: (area / "workspace/src/late.py").write_text("x = 1\n"),
            "stopped native export changed",
        ),
        (
            # The graded patch is not the hash-bound one (submissions.reconstruct).
            lambda area, execution: (
                area / "artifacts/harbor/trial/artifacts" / submissions.PATCH_ARTIFACT
            ).write_text("tampered\n"),
            "native graded artifacts changed",
        ),
    ],
    ids=["treatment", "harbor-trial", "no-export", "export-changed", "patch-changed"],
)
def test_coding_infrastructure_is_never_complete_and_a_resume_codes_it_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Path, dict[str, Any]], None],
    reason: str,
) -> None:
    with pytest.raises(study.CodingInfrastructure, match="2 attempts ended in an infrastructure"):
        _coded(tmp_path, monkeypatch, change)
    root = tmp_path / "run"
    run = json.loads((root / "run.json").read_text())
    assert run["status"] == "interrupted"
    assert Counter(run["attempt_states"].values()) == {"interrupted": 2}
    for attempt in run["attempt_states"]:
        receipt = json.loads(
            (root / "attempts" / attempt / "handoff" / grade.INFRASTRUCTURE_ERROR).read_text()
        )
        assert receipt["grade_contract"] == grade.GRADE_CONTRACT and reason in receipt["reason"]
        assert not (root / "records" / f"{attempt}.json").exists()

    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    study.resume_study(root)
    run = json.loads((root / "run.json").read_text())
    assert len(calls) == 2 and not set(calls) & set(run["discarded_attempts"])
    assert Counter(run["attempt_states"].values()) == {"complete": 2}


def test_a_worker_that_raises_is_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lost(area: Path, execution: dict[str, Any]) -> None:
        raise OSError("the Harbor worker was lost")

    with pytest.raises(study.CodingInfrastructure, match="the Harbor worker was lost"):
        _coded(tmp_path, monkeypatch, lost)


def test_three_infrastructure_errors_in_a_row_stop_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def lost(**kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["entry"]["id"]))
        raise OSError("the Harbor worker was lost")

    monkeypatch.setattr(harbor_backend, "invoke_native", lost)
    resolved = study.load_study(
        resume_support.recipe(tmp_path / "config", tasks=2, treatments=1, repeats=2, jobs=1)
    )
    with pytest.raises(ValueError, match="the last 3 attempts to finish each ended in an infra"):
        study.run_study(resolved, tmp_path / "run")
    assert len(calls) == 3


# Grade stage.


def test_a_reward_that_is_not_binary_is_regraded_once_then_fails_on_a_healthy_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crashed(area: Path, execution: dict[str, Any]) -> None:
        execution["native_verifier_result"] = {"rewards": {"reward": -1}}

    root, _ = _coded(tmp_path, monkeypatch, crashed)
    asked: list[str] = []

    def healthy(task: Any) -> None:
        asked.append(task.task_id)

    records, pending, replayed = _graded(root, monkeypatch, requalify=healthy, reward=None)
    # One regrade per attempt, and one requalification for their one task.
    assert len(replayed) == 2 and asked == ["task-0"] and pending == {}
    for record in records.values():
        assert (record["passed"], record["fail_reason"]) == (False, grade.VERIFIER_CRASHED)
        assert grade.outcome(record) == ("fail", grade.VERIFIER_CRASHED)


@pytest.mark.parametrize(
    ("kind", "graded"),
    [
        ("EnvironmentStartTimeoutError", False),  # The replay never reached the verifier.
        ("DockerDaemonError", False),
        ("AgentTimeoutError", False),  # The replay's own agent, not the attempt's.
        ("VerifierTimeoutError", True),  # The verifier ran on the submission and crashed.
        ("RewardFileNotFoundError", True),
    ],
)
def test_a_replay_that_fails_before_its_verifier_is_infrastructure_whatever_the_task_s_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, graded: bool
) -> None:
    def crashed(area: Path, execution: dict[str, Any]) -> None:
        execution["native_verifier_result"] = None

    root, _ = _coded(tmp_path, monkeypatch, crashed)
    asked: list[str] = []

    def healthy(task: Any) -> None:
        asked.append(task.task_id)

    records, pending, replayed = _graded(
        root, monkeypatch, requalify=healthy, exception=_harbor_error(kind)
    )
    if graded:
        assert pending == {} and asked == ["task-0"] and len(replayed) == 2
        assert {record["fail_reason"] for record in records.values()} == {grade.VERIFIER_CRASHED}
        return
    # A host or setup failure says nothing about the submission: never a fail, never asks.
    assert records == {} and asked == [] and len(pending) == 2
    assert all(f"the verifier replay failed: {kind}" in reason for reason in pending.values())


def test_a_crashed_verifier_on_an_unhealthy_task_is_infrastructure_and_graded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crashed(area: Path, execution: dict[str, Any]) -> None:
        execution["native_verifier_result"] = None

    root, _ = _coded(tmp_path, monkeypatch, crashed)
    records, pending, _ = _graded(
        root, monkeypatch, requalify=lambda task: "oracle broken: reward 0", reward=None
    )
    assert records == {} and len(pending) == 2
    assert all("did not requalify: oracle broken" in reason for reason in pending.values())
    assert source_only.needs_grading(root, json.loads(json.dumps(_index(root))))

    records, pending, replayed = _graded(root, monkeypatch, reward=1)
    assert pending == {} and len(replayed) == 2
    assert all(record["passed"] for record in records.values())


def _index(root: Path) -> dict[str, str]:
    index = root / "source-only" / f"filter-{source_only.FILTER_REVISION}" / "index.json"
    return study._reference(root, index)


# Paths Git C-quotes in a patch header: a quote, a space with a tab, and non-ASCII bytes.
QUOTED = ['src/say "hi".py', "src/tab\there.py", "src/naïve café.py"]


def test_quoted_paths_are_graded_by_the_paths_git_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def quoted(workspace: Path) -> None:
        for path in [*QUOTED, 'tests/test_"quoted".py']:
            (workspace / path).parent.mkdir(exist_ok=True)
            (workspace / path).write_text("x = 1\n")

    root, _ = _coded(tmp_path, monkeypatch, lambda area, execution: None, write=quoted)
    records, pending, replayed = _graded(root, monkeypatch)
    assert pending == {} and len(replayed) == 2
    for record in records.values():
        assert record["passed"] is True
        assert record["removed_test_files"] == ['tests/test_"quoted".py']
    for patch in replayed:
        kept = submissions.patch_paths(patch)
        assert kept == {"src/answer.py", *QUOTED}


@pytest.mark.parametrize(
    ("patch", "kind"),
    [
        (
            b"diff --git a/link b/link\nnew file mode 120000\nindex 0000000..1\n"
            b"--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+target\n\\ No newline at end of file\n",
            "symlink",
        ),
        (
            b"diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100700\n",
            "file_mode",
        ),
        (
            b"diff --git a/.git/config b/.git/config\nnew file mode 100644\nindex 0000000..1\n"
            b"--- /dev/null\n+++ b/.git/config\n@@ -0,0 +1 @@\n+x\n",
            "unsafe_path",
        ),
    ],
)
def test_each_policy_violation_names_its_kind(patch: bytes, kind: str) -> None:
    with pytest.raises(submissions.PolicyViolation) as raised:
        submissions.patch_paths(patch)
    assert raised.value.kind == kind


def test_a_task_patch_that_breaks_the_policy_is_no_submission_fail(tmp_path: Path) -> None:
    patch = tmp_path / "test.patch"
    patch.write_bytes(b"diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100700\n")
    with pytest.raises(ValueError) as raised:
        submissions.task_patch_paths(patch)
    assert not isinstance(raised.value, submissions.PolicyViolation)


@pytest.mark.parametrize(("health", "outcome"), [(None, "fail"), ("nop broken", "infra")])
def test_a_symlink_submission_fails_only_on_a_healthy_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, health: str | None, outcome: str
) -> None:
    def linked(area: Path, execution: dict[str, Any]) -> None:
        # Harbor's patch holds the symlink; the stopped export refuses it, so none is collected.
        artifacts = area / "artifacts/harbor/trial/artifacts"
        with (artifacts / submissions.PATCH_ARTIFACT).open("a") as patch:
            patch.write(
                "diff --git a/src/link.py b/src/link.py\nnew file mode 120000\n"
                "index 0000000..1111111\n--- /dev/null\n+++ b/src/link.py\n@@ -0,0 +1 @@\n"
                "+answer.py\n\\ No newline at end of file\n"
            )
        execution.update(
            native_artifact_hashes=harbor_backend._native_artifact_hashes(artifacts),
            submission_collected=False,
            submission_error="snapshot contains a symlink: src/link.py",
        )

    root, _ = _coded(tmp_path, monkeypatch, linked)
    run = json.loads((root / "run.json").read_text())
    # Complete with no snapshot: the evidence is intact, the submission breaks the policy.
    assert Counter(run["attempt_states"].values()) == {"complete": 2}
    records, pending, replayed = _graded(root, monkeypatch, requalify=lambda task: health)
    assert replayed == []
    if outcome == "fail":
        assert pending == {}
        for record in records.values():
            assert grade.outcome(record) == ("fail", "unsupported_submission:symlink")
    else:
        assert records == {}
        assert all("did not requalify: nop broken" in reason for reason in pending.values())


def test_changed_evidence_is_infrastructure_whatever_the_task_s_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _coded(tmp_path, monkeypatch, lambda area, execution: None)
    for patch in root.glob(f"attempts/*/handoff/artifacts/harbor/trial/artifacts/{submissions.PATCH_ARTIFACT}"):
        patch.write_text(patch.read_text() + "\n")
    asked: list[str] = []

    def healthy(task: Any) -> None:
        asked.append(task.task_id)

    records, pending, _ = _graded(root, monkeypatch, requalify=healthy)
    assert records == {} and asked == []
    assert set(pending.values()) == {"the graded model.patch changed after grading"}


def test_a_legacy_run_is_never_graded_under_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _coded(tmp_path, monkeypatch, lambda area, execution: None)
    run = json.loads((root / "run.json").read_text())
    plan = study._bound_path(root, run["plan"])
    saved = json.loads(plan.read_text())
    del saved["grade_contract"]
    plan.write_text(json.dumps(saved))
    run["plan"] = study._reference(root, plan)
    (root / "run.json").write_text(json.dumps(run))
    assert source_only.needs_grading(root, None) is False
    assert source_only.pending_replays(root, None) == 0


def test_an_outcome_reads_only_the_grade_record() -> None:
    contract = {"grade_contract": grade.GRADE_CONTRACT, "status": "complete"}
    assert grade.outcome(None) is None
    assert grade.outcome({**contract, "passed": True}) == ("pass", None)
    assert grade.outcome({**contract, "passed": False, "fail_reason": None}) == ("fail", None)
    assert grade.outcome({**contract, "status": "infra", "reason": "lost"}) == ("infra", "lost")
    assert grade.outcome({"source_only_filter": 2, "reward": True})[0] == "legacy"  # type: ignore[index]


# Every producer of an ungraded state in the coding and grade paths, and the C1 row it maps to.
# Measure and review modules are out of this contract (C1 measure rows, P7): mutation,
# coverage_report, assessment_metrics, remeasure, study_review, iterations, measurement_profiles,
# collectors, language_analysis and judge are not scanned.
TOKENS = {"unavailable", "unsupported", "timed_out", "successful_native_completion"}
SCANNED = ("study", "source_only", "submissions", "harbor_backend", "codex", "codex_events",
           "claude", "grade", "baselines")
PRODUCERS: dict[tuple[str, str, str], tuple[str, ...]] = {
    ("study", "_execute_stage", "successful_native_completion"): ("worker_or_container_loss",),
    ("study", "_refuse_mixed_builtins", "successful_native_completion"): ("builtin_mismatch",),
    # Docker command helpers: a host command that times out is infrastructure.
    ("harbor_backend", "_docker", "timed_out"): ("worker_or_container_loss",),
    ("harbor_backend", "_native_completion", "timed_out"): (
        "timeout_after_first_turn", "worker_or_container_loss",
    ),
    ("harbor_backend", "_native_completion", "successful_native_completion"): (
        "treatment_not_delivered", "agent_error_after_first_turn", "harbor_trial_error",
    ),
    ("harbor_backend", "invoke_native", "successful_native_completion"): (
        "worker_or_container_loss",
    ),
    # Collector commands: measure rows, P7.
    ("harbor_backend", "run_native_command", "timed_out"): (),
    ("codex", "invoke", "timed_out"): ("timeout_after_first_turn",),
    ("codex", "invoke", "successful_native_completion"): ("agent_error_after_first_turn",),
    ("codex_events", "parse_events", "successful_native_completion"): (
        "agent_error_after_first_turn",
    ),
    ("claude", "parse_events", "successful_native_completion"): ("agent_error_after_first_turn",),
    ("grade", "attributes", "timed_out"): ("timeout_after_first_turn",),
}
# Exceptions that stop or skip coding, and the row each one is.
EXCEPTIONS = {
    "BackendUnavailable": ("failed_before_first_turn", "provider_stop"),
    "BuiltinsDiffer": ("builtin_mismatch",),
    "CodingInfrastructure": ("worker_or_container_loss", "treatment_not_delivered"),
}


def _producers(module: str) -> set[tuple[str, str, str]]:
    path = Path(study.__file__).with_name(f"{module}.py")
    found: set[tuple[str, str, str]] = set()

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef) else function
            if isinstance(child, ast.Dict):
                for key, value in zip(child.keys, child.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value in TOKENS:
                        found.add((module, name, str(key.value)))
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "status"
                        and isinstance(value, ast.Constant)
                        and value.value in TOKENS
                    ):
                        found.add((module, name, str(value.value)))
            if isinstance(child, ast.keyword) and child.arg in TOKENS:
                found.add((module, name, child.arg))
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value in TOKENS
                    ):
                        found.add((module, name, str(target.slice.value)))
            visit(child, name)

    visit(ast.parse(path.read_text()), "<module>")
    return found


def test_every_producer_of_an_ungraded_state_maps_to_a_c1_row() -> None:
    rows = set(grade.CODING_ROWS) | set(grade.GRADE_ROWS)
    found = set().union(*(_producers(module) for module in SCANNED))
    assert found == set(PRODUCERS), "map each new producer to its C1 row in PRODUCERS"
    assert {row for mapped in PRODUCERS.values() for row in mapped} <= rows
    defined = {
        node.name
        for node in ast.parse(Path(study.__file__).read_text()).body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "ValueError" for base in node.bases)
    }
    assert defined == set(EXCEPTIONS)
    assert {row for mapped in EXCEPTIONS.values() for row in mapped} <= rows
    assert set(submissions.PolicyViolation.KINDS) == {
        "symlink", "file_mode", "unsafe_path", "test_rename",
    }


def test_the_empty_patch_digest_is_the_empty_attribute() -> None:
    assert source_only._EMPTY_SHA256 == hashlib.sha256(b"").hexdigest()
