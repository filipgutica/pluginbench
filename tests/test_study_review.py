from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
import yaml

from pluginbench import codex, study, study_review
from pluginbench.assessment_models import LedgerCompleteness


class NativeInvocationKwargs(TypedDict):
    entry: dict[str, object]
    protocol: dict[str, object]
    prompt: str
    timeout: int
    read_only: bool
    output_schema: Path | None


def _existing_study(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    interrupted: bool = False,
    repeats: int = 1,
    repairs: bool = False,
    prefixed_source: bool = False,
) -> Path:
    suite = tmp_path / "suite"
    (suite / "fixture/src").mkdir(parents=True)
    (suite / "fixture/src/answer.py").write_text("answer = 0\n")
    if prefixed_source:
        (suite / "fixture/after/src").mkdir(parents=True)
        (suite / "fixture/after/src/answer.py").write_text("answer = 2\n")
    (suite / "verify.py").write_text(
        "import json, pathlib, sys\npassed = pathlib.Path('src/answer.py').read_text() == 'answer = 1\\n'\nprint(json.dumps({'pass': passed}))\nsys.exit(0 if passed else 1)\n"
    )
    (suite / "tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "answer",
                        "prompt": "Set the answer to one.",
                        "fixture": "fixture",
                        "verifier": {"command": [sys.executable], "script": "verify.py"},
                    }
                ],
            }
        )
    )
    (tmp_path / "guidance.md").write_text("SECRET TREATMENT GUIDANCE\n")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "Offline review transport",
                "tasks": "suite/tasks.yaml",
                "guidance": {"candidate": ["guidance.md"]},
                "repeats": repeats,
                "max_repairs": 1 if repairs else 0,
            }
        )
    )
    monkeypatch.setattr(codex, "preflight", lambda: {"harness_version": "offline fixture"})

    implementation_calls = 0

    def implement(*, area: Path, **kwargs: object) -> dict[str, object]:
        nonlocal implementation_calls
        implementation_calls += 1
        if interrupted:
            raise RuntimeError("offline interruption")
        (area / "workspace/src/answer.py").write_text(
            "answer = 0\n" if repairs and implementation_calls % 2 else "answer = 1\n"
        )
        return {
            "successful_native_completion": True,
            "elapsed_seconds": 1.0,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
        }

    monkeypatch.setattr(codex, "invoke", implement)
    root = tmp_path / "results"
    if interrupted:
        with pytest.raises(RuntimeError, match="offline interruption"):
            study.run_study(study.load_study(recipe), root)
    else:
        study.run_study(study.load_study(recipe), root)
    return root


def _judge(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    behavior: str = "issues",
    citation_evidence: str | None = None,
    citation_line: int = 1,
    citation_path: str = "src/answer.py",
    cli_version: str | None = None,
    severity: str | None = None,
    additional_issue: bool = False,
) -> list[Path]:
    calls: list[Path] = []

    def invoke(
        *,
        area: Path,
        read_only: bool,
        output_schema: Path,
        protocol: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(area)
        workspace = area / "workspace"
        assert read_only is True
        assert output_schema == workspace / "response-schema.json"
        assert protocol.get("models") == {"codex": "gpt-6-astra"}
        assert protocol.get("reasoning") == "high"
        assert not workspace.is_relative_to(root)
        assert not list(workspace.rglob("mapping.json"))
        text = "\n".join(path.read_text() for path in workspace.rglob("*") if path.is_file())
        assert "SECRET TREATMENT GUIDANCE" not in text
        assert "gpt-6.1-sol" not in text
        assert '"arm"' not in text
        assert not list(workspace.rglob("execution.json"))
        packet_id = json.loads((workspace / "packet.json").read_text())["packet_id"]
        response: dict[str, object] = {
            "packet_id": packet_id,
            "status": "reviewed",
            "issues": [],
            "limitations": ["Synthetic review transport fixture"],
            "outside_packet_access": False,
        }
        if behavior == "issues":
            response["issues"] = [
                {
                    "issue_id": "I1",
                    "category": "maintainability",
                    "description": "Synthetic actionable judgment",
                    "rationale": "Tests ledger transport only",
                    "path": "src/answer.py",
                    "line": 1,
                    "evidence": "answer = 1",
                    "smallest_fix": "Use the task's existing abstraction",
                }
            ]
        if citation_evidence is not None:
            response["issues"] = [
                {
                    "issue_id": "I1",
                    "category": "tests",
                    "description": "Synthetic source-bound finding",
                    "rationale": "The required assertion is missing",
                    "path": citation_path,
                    "line": citation_line,
                    "evidence": citation_evidence,
                    "smallest_fix": "Add the required assertion",
                }
            ]
        if behavior == "arm_leak":
            response["arm"] = "candidate"
        if behavior == "absent_evidence":
            response["issues"] = [
                {
                    "issue_id": "I1",
                    "category": "correctness",
                    "description": "Missing evidence",
                    "rationale": "Cannot be checked",
                    "path": "src/answer.py",
                    "line": 1,
                    "evidence": "invented",
                    "smallest_fix": "Fix it",
                }
            ]
        if behavior == "outside_disclosed":
            response["outside_packet_access"] = True
        if behavior == "unsupported":
            response["status"] = "unsupported"
        if behavior == "packet_mutation":
            (workspace / "after/src/answer.py").write_text("changed by judge\n")
        if behavior == "source_mutation":
            source = next((root / "attempts").glob("*/handoff/snapshot/src/answer.py"))
            source.write_text("changed original source\n")
        artifacts = area / "artifacts"
        artifacts.mkdir()
        if severity is not None:
            issues = response["issues"]
            assert isinstance(issues, list)
            for issue in issues:
                issue["severity"] = severity
        if additional_issue:
            issues = response["issues"]
            assert isinstance(issues, list) and issues
            issues.append(
                {
                    **issues[0],
                    "issue_id": "I2",
                    "description": "Another original claim",
                    "severity": "P1",
                }
            )
        execution: dict[str, object] = {
            "successful_native_completion": True,
            "actual_primary_models": ["gpt-6-astra"],
            "runtime_effort_evidence": ["high"],
            "elapsed_seconds": 2.5,
            "usage": {"input_tokens": 30, "output_tokens": 7},
            "partial_usage": [],
            "native_dollars": None,
            "exit_code": 0,
            "timed_out": False,
            "command_items": [],
            "final": json.dumps(response),
        }
        startup = [{"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "high"}}]
        if cli_version is not None:
            startup.insert(0, {"type": "session_meta", "payload": {"cli_version": cli_version}})
            execution["startup"] = startup
        if behavior == "bad_json":
            execution["final"] = "not JSON"
        if behavior == "runtime_unknown":
            execution["actual_primary_models"] = []
        if behavior == "trace_leak":
            execution["command_items"] = [{"command": f"cat {root}/review/mapping.json"}]
        if behavior in {"aggregate", "aggregate_issues"} or citation_evidence is not None:
            if behavior == "aggregate_issues":
                response["issues"] = [
                    {
                        "issue_id": "I1",
                        "category": "tests",
                        "description": "Missing task coverage",
                        "rationale": "Synthetic recovery fixture",
                        "path": "src/answer.py",
                        "line": 1,
                        "evidence": "answer = 1",
                        "smallest_fix": "Add the relevant task assertion",
                    }
                ]
            final = json.dumps(response)
            execution["final"] = (
                "I will inspect the packet.\n" + final
                if behavior in {"aggregate", "aggregate_issues"}
                else final
            )
            (artifacts / "stdout.jsonl").write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "I will inspect the packet."},
                        },
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": final},
                        },
                        {"type": "turn.completed", "usage": execution["usage"]},
                    )
                )
                + "\n"
            )
            (artifacts / "host-trace-0.jsonl").write_text(
                "\n".join(json.dumps(event) for event in startup) + "\n"
            )
        (artifacts / "execution.json").write_text(json.dumps({"elapsed_seconds": 2.5}))
        (artifacts / "host-evidence.json").write_text(json.dumps(execution))
        if behavior == "raised":
            raise RuntimeError("judge transport failed after usage capture")
        return execution

    monkeypatch.setattr(codex, "invoke", invoke)
    return calls


@pytest.mark.parametrize("behavior", ["issues", "clean"])
def test_review_binds_ledgers_and_preserves_source_run_and_implementation_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    original_run = (root / "run.json").read_bytes()
    freeze = (root / "frozen-inventory.json").read_bytes()
    plan = (root / "plan.json").read_bytes()
    calls = _judge(monkeypatch, root, behavior=behavior)
    state_path = study_review.review_study(root)
    state = json.loads(state_path.read_text())
    assert state["status"] == "complete" and state["assigned"] == len(calls) == 2
    assert state["native_invocations"] == 2
    assert (root / "review/original-run.json").read_bytes() == original_run
    assert (root / "frozen-inventory.json").read_bytes() == freeze
    assert (root / "plan.json").read_bytes() == plan
    run = json.loads((root / "run.json").read_text())
    old = json.loads(original_run)
    assert run["actual_calls"] == old["actual_calls"] == 2
    assert run["status"] == old["status"] and run["attempt_states"] == old["attempt_states"]
    assert run["assessment_input"] != old["assessment_input"]
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 2
    for attempt in report["attempts"]:
        assert attempt["stages"][0]["work"]["tokens"]["input_tokens"] == 10
        assert attempt["correction_burden"]["verified_issue_count"] == (
            1 if behavior == "issues" else 0
        )
        assert attempt["correction_burden"]["external_rounds"] == 1
        assert (
            "AI judge" in str(attempt["correction_burden"]["verified_issues"])
            if behavior == "issues"
            else attempt["stages"][0]["accepted"] is True
        )
    observation = study._bound_document(root, state["records"][0]["observation"])
    assert observation["execution"]["usage"]["input_tokens"] == 30
    assert observation["execution"]["elapsed_seconds"] == 2.5
    assert any("not that all defects" in value for value in state["limitations"])
    assert any("calibration is unverified" in value for value in state["limitations"])
    with pytest.raises(ValueError, match="already|replayed"):
        study_review.review_study(root)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "behavior",
    [
        "bad_json",
        "arm_leak",
        "absent_evidence",
        "outside_disclosed",
        "runtime_unknown",
        "trace_leak",
        "packet_mutation",
        "raised",
        "unsupported",
    ],
)
def test_failed_review_stays_unknown_keeps_usage_and_is_not_paid_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, behavior=behavior)
    state = json.loads(study_review.review_study(root).read_text())
    assert state["status"] == "incomplete" and state["assigned"] == 2
    assert {row["status"] for row in state["records"]} == {
        "unavailable" if behavior == "unsupported" else "unknown"
    }
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 2
    assert all(
        attempt["correction_burden"]["status"] == "unavailable" for attempt in report["attempts"]
    )
    observation = study._bound_document(root, state["records"][0]["observation"])
    assert observation["execution"]["usage"] == {"input_tokens": 30, "output_tokens": 7}
    with pytest.raises(ValueError, match="already|replayed"):
        study_review.review_study(root)
    assert len(calls) == 2


def test_missing_handoffs_keep_all_assignments_without_judge_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch, interrupted=True)
    calls = _judge(monkeypatch, root)
    state = json.loads(study_review.review_study(root).read_text())
    assert state["assigned"] == 2 and state["native_invocations"] == 0 and calls == []
    assert all(row["status"] == "unavailable" for row in state["records"])
    assert len(json.loads((root / "report/assessment-report.json").read_text())["attempts"]) == 2


def test_active_study_is_refused_without_judge_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    run = json.loads((root / "run.json").read_text())
    run["status"] = "running"
    study._write(root / "run.json", run)
    calls = _judge(monkeypatch, root)
    with pytest.raises(ValueError, match="active run"):
        study_review.review_study(root)
    assert calls == [] and not (root / "review").exists()


def test_stale_source_is_refused_before_judge_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    source = next((root / "attempts").glob("*/handoff/snapshot/src/answer.py"))
    source.write_text("stale source\n")
    calls = _judge(monkeypatch, root)
    with pytest.raises(ValueError, match="digest"):
        study_review.review_study(root)
    assert calls == []


def test_source_mutation_aborts_remaining_calls_and_preserves_current_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    original = (root / "run.json").read_bytes()
    calls = _judge(monkeypatch, root, behavior="source_mutation")
    with pytest.raises(ValueError, match="original study changed"):
        study_review.review_study(root)
    assert len(calls) == 1
    assert (root / "run.json").read_bytes() == original
    state = json.loads((root / "review/review-state.json").read_text())
    assert state["status"] == "incomplete" and state["assigned"] == 2


@pytest.mark.parametrize("interrupted_call", [1, 2])
@pytest.mark.parametrize("final_usage", [True, False])
def test_interrupted_judge_binds_reportable_usage_and_pending_assignments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_call: int,
    final_usage: bool,
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    original_run = (root / "run.json").read_bytes()
    freeze = (root / "frozen-inventory.json").read_bytes()
    calls = _judge(monkeypatch, root, behavior="clean")
    invoke = codex.invoke
    usage = {"input_tokens": 30, "output_tokens": 7}

    def interrupt(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        execution = invoke(area=area, **kwargs)
        if len(calls) != interrupted_call:
            return execution
        artifacts = area / "artifacts"
        # Native KeyboardInterrupt archives traces but bypasses host-evidence parsing.
        (artifacts / "host-evidence.json").unlink()
        (artifacts / "stdout.jsonl").write_text(
            json.dumps({"type": "turn.completed", "usage": usage}) + "\n" if final_usage else ""
        )
        (artifacts / "host-trace-0.jsonl").write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-6-astra", "effort": "high"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"total_token_usage": usage},
                        },
                    },
                )
            )
            + "\n"
        )
        raise KeyboardInterrupt("offline judge interruption")

    monkeypatch.setattr(codex, "invoke", interrupt)
    with pytest.raises(KeyboardInterrupt, match="offline judge interruption"):
        study_review.review_study(root)
    run = json.loads((root / "run.json").read_text())
    state = study._bound_document(root, run["review"])
    assert state["status"] == "interrupted"
    assert state["assigned"] == 2 and state["native_invocations"] == interrupted_call
    assert sum(row["status"] == "interrupted" for row in state["records"]) == 1
    assert sum(row["status"] == "pending" for row in state["records"]) == 2 - interrupted_call
    assert sum(row["status"] == "reviewed" for row in state["records"]) == interrupted_call - 1
    row = next(row for row in state["records"] if row["status"] == "interrupted")
    observation = study._bound_document(root, row["observation"])
    captured = observation["execution"]
    assert captured["successful_native_completion"] is False
    assert captured["elapsed_seconds"] == 2.5
    assert captured["actual_primary_models"] == ["gpt-6-astra"]
    assert captured["runtime_effort_evidence"] == ["high"]
    assert captured["usage"] == (usage if final_usage else None)
    assert (
        (captured["partial_usage"] == [])
        if final_usage
        else (captured["partial_usage"][0]["tokens"] == usage)
    )
    assert (root / "review/original-run.json").read_bytes() == original_run
    assert (root / "frozen-inventory.json").read_bytes() == freeze
    assert run["status"] == "complete" and run["actual_calls"] == 2
    study.report_study(root)
    report = json.loads((root / "report/assessment-report.json").read_text())
    review = report["independent_review"]
    assert review["status"] == "interrupted" and review["assigned"] == 2
    assert review["native_invocation_requests"] == interrupted_call
    cost = next(item for item in review["records"] if item["status"] == "interrupted")
    assert cost["work"]["elapsed_seconds"] == 2.5
    assert cost["work"]["tokens"] == (usage if final_usage else None)
    assert cost["execution"]["partial_usage"] == captured["partial_usage"]
    assert all(item["work"] is None for item in review["records"] if item["status"] == "pending")
    assert all(
        attempt["stages"][0]["work"]["tokens"]["input_tokens"] == 10
        for attempt in report["attempts"]
    )
    with pytest.raises(ValueError, match="already|replayed"):
        study_review.review_study(root)
    assert len(calls) == interrupted_call


def test_interruption_before_judge_call_binds_all_pending_review_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root)

    def interrupt(**kwargs: object) -> None:
        raise KeyboardInterrupt("packet preparation interrupted")

    monkeypatch.setattr(study_review, "_packet", interrupt)
    with pytest.raises(KeyboardInterrupt, match="packet preparation interrupted"):
        study_review.review_study(root)
    run = json.loads((root / "run.json").read_text())
    state = study._bound_document(root, run["review"])
    assert state["status"] == "interrupted" and state["native_invocations"] == 0
    assert calls == [] and all(row["status"] == "pending" for row in state["records"])
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["independent_review"]["records"]) == 2


@pytest.mark.parametrize("behavior", ["aggregate", "aggregate_issues"])
def test_offline_reprocess_recovers_final_message_without_calls_or_replacing_original_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, behavior=behavior)
    original_state_path = study_review.review_study(root)
    original_state = json.loads(original_state_path.read_text())
    assert {row["status"] for row in original_state["records"]} == {"unknown"}
    preserved = {
        path: path.read_bytes()
        for path in (
            original_state_path,
            root / "review/original-run.json",
            root / "frozen-inventory.json",
            root / "plan.json",
            *(root / row["observation"]["path"] for row in original_state["records"]),
        )
    }

    def forbidden(**kwargs: object) -> dict[str, object]:
        raise AssertionError("offline recovery must never call the judge")

    monkeypatch.setattr(codex, "invoke", forbidden)
    state_path = study_review.reprocess_review(root)
    state = json.loads(state_path.read_text())
    assert state_path != original_state_path
    assert state["status"] == "complete" and state["native_invocations"] == len(calls) == 2
    recovery = study._bound_document(root, state["recovery"])
    assert recovery["native_invocations"] == 0
    assert study._bound_document(root, recovery["original_state"]) == original_state
    assert len(recovery["native_artifacts"]) == 8
    for reference in recovery["native_artifacts"]:
        assert study._digest(root / reference["path"]) == reference["sha256"]
    for path, expected in preserved.items():
        assert path.read_bytes() == expected
    run = json.loads((root / "run.json").read_text())
    assert study._bound_document(root, run["review"]) == state
    assert run["actual_calls"] == 2 and run["status"] == "complete"
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert report["independent_review"]["status"] == "complete"
    assert report["independent_review"]["native_invocation_requests"] == 2
    for row, old_row in zip(state["records"], original_state["records"], strict=True):
        observation = study._bound_document(root, row["observation"])
        assert observation["recovery"]["original_observation"] == old_row["observation"]
        assert observation["execution"]["usage"] == {"input_tokens": 30, "output_tokens": 7}
        assert observation["execution"]["elapsed_seconds"] == 2.5
        assert "startup" not in observation["execution"]
        assert observation["execution"]["observed_cli_version"] is None
    for attempt in report["attempts"]:
        assert attempt["stages"][0]["work"]["tokens"]["input_tokens"] == 10
        assert attempt["correction_burden"]["verified_issue_count"] == (
            1 if behavior == "aggregate_issues" else 0
        )
        assert attempt["correction_burden"]["external_rounds"] == 1
    bound_run = (root / "run.json").read_bytes()
    assert study_review.reprocess_review(root) == state_path
    assert (root / "run.json").read_bytes() == bound_run
    with pytest.raises(ValueError, match="already|replayed"):
        study_review.review_study(root)


@pytest.mark.parametrize("mutation", ["source", "packet"])
def test_reprocess_refuses_stale_source_or_packet_without_changing_current_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root, behavior="aggregate")
    state = json.loads(study_review.review_study(root).read_text())
    current_run = (root / "run.json").read_bytes()
    if mutation == "source":
        target = next((root / "attempts").glob("*/handoff/snapshot/src/answer.py"))
    else:
        target = root / "review/packets" / state["records"][0]["packet_id"] / "after/src/answer.py"
    target.write_text("changed after original review\n")
    with pytest.raises(ValueError, match="digest|packet"):
        study_review.reprocess_review(root)
    assert (root / "run.json").read_bytes() == current_run
    assert not (root / "review/recovery").exists()


@pytest.mark.parametrize("mutation", ["response", "usage"])
def test_reprocess_cannot_replace_bound_result_or_usage_with_changed_native_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root, behavior="aggregate")
    original_state = json.loads(study_review.review_study(root).read_text())
    row = original_state["records"][0]
    stdout = (root / row["observation"]["path"]).parent / "artifacts/stdout.jsonl"
    events = [json.loads(line) for line in stdout.read_text().splitlines()]
    if mutation == "response":
        response = json.loads(events[1]["item"]["text"])
        response["limitations"] = ["Injected replacement"]
        events[1]["item"]["text"] = json.dumps(response)
    else:
        events[-1]["usage"] = {"input_tokens": 99, "output_tokens": 8}
    stdout.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    state = json.loads(study_review.reprocess_review(root).read_text())
    recovered = next(item for item in state["records"] if item["id"] == row["id"])
    assert recovered["status"] == "unknown"
    assert "original bound native" in recovered["reason"]
    observation = study._bound_document(root, recovered["observation"])
    assert observation["execution"]["usage"] == {"input_tokens": 30, "output_tokens": 7}
    assert observation["response"] is None
    stdout.write_text("changed again after recovery\n")
    with pytest.raises(ValueError, match="artifact changed"):
        study_review.reprocess_review(root)


def test_reprocess_preserves_interruption_pending_rows_and_captured_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch, repeats=2)
    calls = _judge(monkeypatch, root, behavior="aggregate")
    invoke = codex.invoke

    def interrupt(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        execution = invoke(area=area, **kwargs)
        if len(calls) == 2:
            raise KeyboardInterrupt("offline second judge interruption")
        return execution

    monkeypatch.setattr(codex, "invoke", interrupt)
    with pytest.raises(KeyboardInterrupt):
        study_review.review_study(root)

    def forbidden(**kwargs: object) -> dict[str, object]:
        raise AssertionError("reprocessing interrupted reviews must remain offline")

    monkeypatch.setattr(codex, "invoke", forbidden)
    state = json.loads(study_review.reprocess_review(root).read_text())
    assert state["status"] == "interrupted" and state["assigned"] == 4
    assert state["native_invocations"] == len(calls) == 2
    assert sorted(row["status"] for row in state["records"]) == [
        "interrupted",
        "pending",
        "pending",
        "reviewed",
    ]
    report = json.loads((root / "report/assessment-report.json").read_text())
    review = report["independent_review"]
    assert review["assigned"] == 4 and review["native_invocation_requests"] == 2
    assert all(row["work"] is None for row in review["records"] if row["status"] == "pending")
    assert all(
        row["work"]["tokens"]["input_tokens"] == 30
        for row in review["records"]
        if row["status"] in {"interrupted", "reviewed"}
    )


def test_review_packets_use_bound_revised_task_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    run = json.loads((root / "run.json").read_text())
    original = root / "observer-original-run.json"
    study._write(original, run)
    verifier = root / "observer-verifier.py"
    revised_code = "BOUND_REVISED_VERIFIER = True\n" + (root / "frozen/suite/verify.py").read_text()
    verifier.write_text(revised_code)
    revision_path = root / "observer-revision.json"
    study._write(
        revision_path,
        {
            "original_run": study._reference(root, original),
            "original_assessment": run["assessment_input"],
            "task_verifiers": {"answer": study._reference(root, verifier)},
            "observations": [],
        },
    )
    revision = study._reference(root, revision_path)
    run["observer_revision"] = revision
    study._write(root / "run.json", run)
    calls = _judge(monkeypatch, root, behavior="clean")
    invoke = codex.invoke

    def check_packet(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        assert (area / "workspace/verifier.py").read_text() == revised_code
        return invoke(area=area, **kwargs)

    monkeypatch.setattr(codex, "invoke", check_packet)
    state = json.loads(study_review.review_study(root).read_text())
    assert state["status"] == "complete" and len(calls) == 2
    assert json.loads((root / "run.json").read_text())["observer_revision"] == revision
    for row in state["records"]:
        packet = root / "review/packets" / row["packet_id"]
        assert (packet / "verifier.py").read_text() == revised_code


@pytest.mark.parametrize(
    "evidence",
    [
        'Exact line: "answer = 1". The other headings are "Changes" and "Verification".',
        'Exact quote: "answer = 1" An explanation follows the cited source.',
    ],
)
def test_citation_wrapper_recovery_preserves_raw_result_and_separately_binds_literal_quote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, citation_evidence=evidence)
    original_path = study_review.review_study(root)
    original = json.loads(original_path.read_text())
    assert {row["status"] for row in original["records"]} == {"unknown"}
    preserved = {
        root / row["observation"]["path"]: (root / row["observation"]["path"]).read_bytes()
        for row in original["records"]
    }
    preserved[original_path] = original_path.read_bytes()

    def forbidden(**kwargs: object) -> dict[str, object]:
        raise AssertionError("citation recovery cannot invoke a model")

    monkeypatch.setattr(codex, "invoke", forbidden)
    state_path = study_review.reprocess_review(root)
    state = json.loads(state_path.read_text())
    assert state["status"] == "complete" and state["native_invocations"] == len(calls) == 2
    protocol = study._bound_document(root, state["recovery"])
    assert (
        protocol["native_invocations"] == 0
        and protocol["original_protocol"] == original["protocol"]
    )
    assert protocol["counts"] == {
        "original_statuses": {"unknown": 2},
        "revised_statuses": {"reviewed": 2},
        "newly_reviewed_records": 2,
        "recovered_issue_count": 2,
        "recovered_literal_quote_count": 2,
    }
    for row, old_row in zip(state["records"], original["records"], strict=True):
        observation = study._bound_document(root, row["observation"])
        old = study._bound_document(root, old_row["observation"])
        assert observation["execution"]["final"] == old["execution"]["final"]
        assert observation["execution"]["usage"] == old["execution"]["usage"]
        assert observation["raw_response"]["issues"][0]["evidence"] == evidence
        assert observation["response"]["issues"][0]["evidence"] == "answer = 1"
        quote = observation["recovery"]["citation_evidence"][0]
        packet = study._bound_document(root, row["packet"])
        assert quote == {
            "issue_id": "I1",
            "path": "src/answer.py",
            "line": 1,
            "source_digest": packet["submission_digest"],
            "raw_evidence": evidence,
            "extracted_quote": "answer = 1",
            "trailing_context": evidence.split('"', 2)[2],
        }
        assert observation["recovery"]["original_reason"] == old_row["reason"]
    for path, data in preserved.items():
        assert path.read_bytes() == data
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert report["independent_review"]["recovery"]["counts"] == protocol["counts"]
    assert report["independent_review"]["recovery"]["native_invocations"] == 0
    assert all(row["recovered"] is True for row in report["independent_review"]["records"])
    assert all(
        attempt["correction_burden"]["verified_issue_count"] == 1 for attempt in report["attempts"]
    )
    assert all(
        row["work"]["tokens"]["input_tokens"] == 30
        for row in report["independent_review"]["records"]
    )
    assert study_review.reprocess_review(root) == state_path


@pytest.mark.parametrize(
    "evidence,line,path",
    [
        ('Explanation. Exact line: "answer = 1".', 1, "src/answer.py"),
        ('exact line: "answer = 1".', 1, "src/answer.py"),
        ('Exact line: "".', 1, "src/answer.py"),
        ('Exact line: "answer = 1', 1, "src/answer.py"),
        ('Exact line: "answer = 1"garbage', 1, "src/answer.py"),
        ('Exact line: "answer = 1" "another quoted payload"', 1, "src/answer.py"),
        ('Exact line: "answer = " 1". Ambiguous closing delimiter.', 1, "src/answer.py"),
        ('Exact line: "answer = 1\\". Escaped delimiter', 1, "src/answer.py"),
        ('Exact line: " answer = 1". Requires whitespace normalization', 1, "src/answer.py"),
        ('Exact line: "ANSWER = 1". Requires case normalization', 1, "src/answer.py"),
        ('Exact line: "answer = 1".', 2, "src/answer.py"),
        ('Exact line: "answer = 1".', 1, "wrong.py"),
    ],
)
def test_citation_recovery_rejects_unsupported_ambiguous_or_mislocated_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str, line: int, path: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(
        monkeypatch, root, citation_evidence=evidence, citation_line=line, citation_path=path
    )
    original = json.loads(study_review.review_study(root).read_text())
    state = json.loads(study_review.reprocess_review(root).read_text())
    assert state["status"] == "incomplete" and state["native_invocations"] == len(calls) == 2
    assert {row["status"] for row in state["records"]} == {"unknown"}
    recovery = study._bound_document(root, state["recovery"])
    assert recovery["counts"]["newly_reviewed_records"] == 0
    assert (
        recovery["counts"]["original_statuses"]
        == recovery["counts"]["revised_statuses"]
        == {"unknown": 2}
    )
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert all(row["recovered"] is False for row in report["independent_review"]["records"])
    for row, old_row in zip(state["records"], original["records"], strict=True):
        observation = study._bound_document(root, row["observation"])
        old = study._bound_document(root, old_row["observation"])
        assert observation["execution"] == old["execution"]
        assert observation["response"] is None
        assert not observation.get("recovery", {}).get("citation_evidence")


def test_recovery_keeps_historical_schema_exact_when_future_evidence_description_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root, behavior="aggregate")
    schema = study_review.JudgeResponse.model_json_schema()
    del schema["$defs"]["JudgeIssue"]["properties"]["evidence"]["description"]
    with monkeypatch.context() as historical:
        historical.setattr(
            study_review.JudgeResponse, "model_json_schema", classmethod(lambda cls: schema)
        )
        original_path = study_review.review_study(root)
    original = json.loads(original_path.read_text())
    protocol_path = root / original["protocol"]["path"]
    preserved = protocol_path.read_bytes()
    state = json.loads(study_review.reprocess_review(root).read_text())
    assert state["status"] == "complete"
    assert protocol_path.read_bytes() == preserved
    assert study._bound_document(root, original["protocol"])["response_schema"] == schema


def test_citation_wrapper_quote_on_another_valid_line_is_not_relocated(tmp_path: Path) -> None:
    (tmp_path / "after").mkdir()
    (tmp_path / "after/result.py").write_text("header = 0\nanswer = 1\n")
    execution: dict[str, object] = {
        "final": json.dumps(
            {
                "packet_id": "offline",
                "status": "reviewed",
                "outside_packet_access": False,
                "limitations": [],
                "issues": [
                    {
                        "issue_id": "I1",
                        "category": "tests",
                        "description": "Missing assertion",
                        "rationale": "Source grounding fixture",
                        "path": "result.py",
                        "line": 1,
                        "evidence": 'Exact line: "answer = 1". This exists on the next line.',
                        "smallest_fix": "Add the assertion",
                    }
                ],
            }
        )
    }
    with pytest.raises(ValueError, match="does not match its source line"):
        study_review._recover_citation_evidence(
            execution=execution, packet=tmp_path, source_digest="sha256:" + "a" * 64
        )


def test_citation_wrapper_does_not_recover_unsuccessful_original_native_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, citation_evidence='Exact line: "answer = 1". Explanation')
    invoke = codex.invoke

    def unsuccessful(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        execution = invoke(area=area, **kwargs)
        execution["successful_native_completion"] = False
        return execution

    monkeypatch.setattr(codex, "invoke", unsuccessful)
    original = json.loads(study_review.review_study(root).read_text())
    state = json.loads(study_review.reprocess_review(root).read_text())
    assert len(calls) == 2 and state["records"] == original["records"]
    assert state["status"] == "incomplete"
    assert study._bound_document(root, state["recovery"])["counts"]["newly_reviewed_records"] == 0


def test_prior_recovery_remains_reportable_but_new_adapter_reprocess_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root, behavior="aggregate")
    study_review.review_study(root)
    state_path = study_review.reprocess_review(root)
    state = json.loads(state_path.read_text())
    digest = study._digest
    adapter = Path(study_review.__file__)

    def changed_adapter(path: Path) -> str:
        return "sha256:" + "0" * 64 if path == adapter else digest(path)

    monkeypatch.setattr(study, "_digest", changed_adapter)
    study.report_study(root)
    assert (
        json.loads((root / "report/assessment-report.json").read_text())["independent_review"][
            "status"
        ]
        == "complete"
    )
    with pytest.raises(ValueError, match="different parser/adapter"):
        study_review.reprocess_review(root)
    assert json.loads(state_path.read_text()) == state


@pytest.mark.parametrize(
    "selection, expected_answer", [("handoff", "answer = 0"), ("final", "answer = 1")]
)
def test_review_selected_submission_binds_original_before_and_actual_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: study_review.ReviewStage,
    expected_answer: str,
) -> None:
    root = _existing_study(tmp_path, monkeypatch, repairs=True)
    calls = _judge(monkeypatch, root, citation_evidence=expected_answer)
    native = codex.invoke

    def inspect_packet(
        *, area: Path, **kwargs: Unpack[NativeInvocationKwargs]
    ) -> dict[str, object]:
        workspace = area / "workspace"
        assert (workspace / "before/src/answer.py").read_text() == "answer = 0\n"
        assert (workspace / "after/src/answer.py").read_text() == expected_answer + "\n"
        assert json.loads((workspace / "packet.json").read_text())["stage"] == "submission"
        assert json.loads((workspace / "verifier.json").read_text())["checks"]["verifier"] is (
            selection == "final"
        )
        packet_text = "\n".join(p.read_text() for p in workspace.rglob("*") if p.is_file())
        assert "repair-1" not in packet_text and "handoff" not in packet_text
        assert "input_tokens" not in packet_text and "output_tokens" not in packet_text
        return native(area=area, **kwargs)

    monkeypatch.setattr(codex, "invoke", inspect_packet)
    preflight = study_review.review_selection(root, selection)
    assert preflight == {"stage": selection, "assigned": 2, "eligible": 2, "unavailable": 0}
    state = json.loads(study_review.review_study(root, stage=selection).read_text())
    assert state["status"] == "complete" and len(calls) == 2
    protocol = study._bound_document(root, state["protocol"])
    manifest = study._bound_document(
        root, json.loads((root / "run.json").read_text())["assessment_input"]
    )
    for row in state["records"]:
        record = next(record for record in manifest["records"] if record["id"] == row["id"])
        selected = record["stages"][0 if selection == "handoff" else -1]
        assert row["stage_id"] == selected["id"]
        assert row["submission_digest"] == selected["snapshot"]["sha256"]
        assert protocol["selections"][row["id"]]["final_equals_handoff"] is False
        binding = study._bound_document(root, row["packet"])
        assert binding["before_digest"] == record["before"]["sha256"]
        assert (
            binding["stage_id"] == selected["id"]
            and binding["submission_digest"] == row["submission_digest"]
        )
        ledger = study._bound_document(root, record["correction_ledger"])
        assert ledger["review_rounds"][0]["stage"] == selected["id"]
        assert ledger["corrections"][0]["from_stage"] == selected["id"]
        proof = study._bound_document(
            (root / record["correction_ledger"]["path"]).parent, ledger["completeness"]
        )
        assert proof["complete"] is True and proof["reviewed_stages"] == [selected["id"]]
        assert proof["stage_digests"] == {
            stage["id"]: stage["snapshot"]["sha256"] for stage in record["stages"]
        }
    study.report_study(root)
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert all(row["finding_count"] == 1 for row in report["independent_review"]["records"])
    assert all(
        attempt["correction_burden"]["status"] == "incomplete" for attempt in report["attempts"]
    )


def test_legacy_completeness_receipt_without_review_scope_remains_valid() -> None:
    receipt = LedgerCompleteness.model_validate(
        {
            "schema_version": 1,
            "stage_digests": {"handoff": "legacy-bound-digest"},
            "reviewer": "Independent reviewer",
            "independent": True,
            "complete": True,
        }
    )
    assert receipt.reviewed_stages is None


def test_final_review_never_falls_back_to_available_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch, repairs=True)
    run = json.loads((root / "run.json").read_text())
    manifest = study._bound_document(root, run["assessment_input"])
    for record in manifest["records"]:
        record["stages"][-1].update(snapshot=None, correctness=None, evidence=None)
    study._checkpoint(
        root,
        study.load_study(root / "frozen/resolved-recipe.json"),
        manifest,
        run["attempt_states"],
        run["actual_calls"],
        status=run["status"],
    )
    calls = _judge(monkeypatch, root)
    assert study_review.review_selection(root, "final")["eligible"] == 0
    assert study_review.review_selection(root, "handoff")["eligible"] == 2
    state = json.loads(study_review.review_study(root, stage="final").read_text())
    assert state["assigned"] == 2 and state["native_invocations"] == 0 and calls == []
    assert all(
        row["status"] == "unavailable" and row["submission_digest"] is None
        for row in state["records"]
    )


def test_final_review_offline_recovery_keeps_selected_stage_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch, repairs=True)
    _judge(monkeypatch, root, citation_evidence='Exact line: "answer = 1". Explanation')
    original = json.loads(study_review.review_study(root, stage="final").read_text())
    assert all(row["status"] == "unknown" for row in original["records"])

    def forbidden(**kwargs: object) -> dict[str, object]:
        raise AssertionError("offline recovery must not call a model")

    monkeypatch.setattr(codex, "invoke", forbidden)
    state = json.loads(study_review.reprocess_review(root).read_text())
    assert all(row["status"] == "reviewed" for row in state["records"])
    run = json.loads((root / "run.json").read_text())
    manifest = study._bound_document(root, run["assessment_input"])
    for row in state["records"]:
        record = next(record for record in manifest["records"] if record["id"] == row["id"])
        selected = record["stages"][-1]
        assert row["stage_id"] == selected["id"]
        ledger = study._bound_document(root, record["correction_ledger"])
        assert ledger["review_rounds"][0]["stage"] == selected["id"]
        observation = study._bound_document(root, row["observation"])
        assert (
            observation["recovery"]["citation_evidence"][0]["source_digest"]
            == selected["snapshot"]["sha256"]
        )


def test_final_reprocess_rejects_observation_stage_change_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch, repairs=True)
    _judge(monkeypatch, root, citation_evidence='Exact line: "answer = 1". Explanation')
    state_path = study_review.review_study(root, stage="final")
    state = json.loads(state_path.read_text())
    row = state["records"][0]
    observation_path = root / row["observation"]["path"]
    observation = json.loads(observation_path.read_text())
    observation["stage_id"] = "handoff"
    study._write(observation_path, observation)
    row["observation"] = study._reference(root, observation_path)
    study._write(state_path, state)
    run = json.loads((root / "run.json").read_text())
    run["review"] = study._reference(root, state_path)
    study._write(root / "run.json", run)
    saved_run = (root / "run.json").read_bytes()
    with pytest.raises(ValueError, match="selected submission"):
        study_review.reprocess_review(root)
    assert (root / "run.json").read_bytes() == saved_run
    assert not (root / "review/recovery").exists()


@pytest.mark.parametrize("mutate_version", [False, True])
def test_reprocess_preserves_bound_startup_identity_and_cost_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate_version: bool
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, behavior="aggregate", cli_version="bound-original")
    original = json.loads(study_review.review_study(root).read_text())
    row = original["records"][0]
    old = study._bound_document(root, row["observation"])
    assert "observed_cli_version" not in old["execution"]  # Legacy receipt predates this field.
    if mutate_version:
        trace = (root / row["observation"]["path"]).parent / "artifacts/host-trace-0.jsonl"
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        events[0]["payload"]["cli_version"] = "unbound-mutated"
        trace.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    def forbidden(**kwargs: object) -> dict[str, object]:
        raise AssertionError("offline recovery cannot invoke a model")

    monkeypatch.setattr(codex, "invoke", forbidden)
    state = json.loads(study_review.reprocess_review(root).read_text())
    recovered = next(item for item in state["records"] if item["id"] == row["id"])
    assert recovered["status"] == ("unknown" if mutate_version else "reviewed")
    if mutate_version:
        assert "startup differs from the original bound native receipt" in recovered["reason"]
    observation = study._bound_document(root, recovered["observation"])
    assert codex.observed_cli_version(observation["execution"]["startup"]) == "bound-original"
    for field in (
        "startup",
        "usage",
        "partial_usage",
        "actual_primary_models",
        "runtime_effort_evidence",
        "command_items",
        "elapsed_seconds",
        "exit_code",
        "successful_native_completion",
    ):
        assert observation["execution"][field] == old["execution"][field]
    assert state["native_invocations"] == len(calls) == 2


def test_fixed_review_revision_preserves_legacy_and_uses_identical_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root)
    study_review.review_study(root)
    preserved = study._inventory(root)
    guidance = tmp_path / "fixed-criteria.md"
    guidance.write_text("Keep changes scoped. Reuse nearby helpers.\n")
    calls = _judge(monkeypatch, root, severity="P2")
    native = codex.invoke
    prompts: list[str] = []

    def inspect_call(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        prompt = kwargs["prompt"]
        assert isinstance(prompt, str)
        prompts.append(prompt)
        assert guidance.read_text() in prompt
        assert not (area / "workspace/metrics.json").exists()
        assert "severity" in (area / "workspace/response-schema.json").read_text()
        return native(area=area, **kwargs)

    monkeypatch.setattr(codex, "invoke", inspect_call)
    state_path = study_review.review_study(root, revision="fixed-v1", guidance=[guidance])
    assert len(calls) == 2 and len(set(prompts)) == 1
    assert all(study._inventory(root)[key] == value for key, value in preserved.items())
    state = json.loads(state_path.read_text())
    assert state["status"] == "complete"
    protocol = study._bound_document(root, state["protocol"])
    assert protocol["schema_version"] == 2 and len(protocol["criteria_sha256"]) == 64
    assert study_review.validate_review_revision(root, "fixed-v1") == state_path
    assert not list(state_path.parent.rglob("ledger.json"))
    for row in state["records"]:
        observation = study._bound_document(root, row["observation"])
        assert observation["response"]["issues"][0]["severity"] == "P2"
    with pytest.raises(ValueError, match="already exists"):
        study_review.review_study(root, revision="fixed-v1", guidance=[guidance])
    assert len(calls) == 2


@pytest.mark.parametrize(
    "evidence,line,recoverable",
    [
        ("\tanswer = 1", 1, True),
        (" \tanswer = 1", 1, True),
        ("\tanswer = 1 ", 1, False),
        ("\tanswer  = 1", 1, False),
        ('Exact line: "answer = 1".', 1, False),
        ("\tanswer = 1", 2, False),
    ],
)
def test_fixed_citation_recovery_preserves_judgment_and_only_removes_added_indentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
    line: int,
    recoverable: bool,
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, citation_evidence=evidence, citation_line=line, severity="P2")
    original_path = study_review.review_study(root, revision="fixed")
    original = json.loads(original_path.read_text())
    preserved = study._inventory(root)
    assert original["status"] == "incomplete"
    monkeypatch.setattr(codex, "invoke", lambda **kw: pytest.fail("recovery cannot call a model"))
    if evidence == "\tanswer = 1" and recoverable:
        write = study._write

        def interrupted(path: Path, value: object) -> None:
            if path.name == "recovery.json":
                raise KeyboardInterrupt
            write(path, value)

        monkeypatch.setattr(study, "_write", interrupted)
        with pytest.raises(KeyboardInterrupt):
            study_review.recover_review_citations(root, "fixed")
        assert not (root / "review-revisions/fixed-citations").exists()
        assert not list((root / "review-revisions").glob(".citation-recovery-*"))
        monkeypatch.setattr(study, "_write", write)
    revision = study_review.recover_review_citations(root, "fixed")
    assert revision == ("fixed-citations" if recoverable else "fixed")
    assert len(calls) == 2
    assert all(study._inventory(root)[key] == value for key, value in preserved.items())
    state_path = study_review.validate_review_revision(root, revision)
    state = json.loads(state_path.read_text())
    assert state["status"] == ("complete" if recoverable else "incomplete")
    for row, old_row in zip(state["records"], original["records"], strict=True):
        assert row["native_start"] == old_row["native_start"]
        receipt = study._bound_document(root, row["native_start"])
        assert receipt["packet_id"] == row["packet_id"]
        assert "/fixed/observations/" in row["native_start"]["path"]
        observation = study._bound_document(root, row["observation"])
        old = study._bound_document(root, old_row["observation"])
        assert observation["execution"] == old["execution"]
        if recoverable:
            raw = json.loads(old["execution"]["final"])
            expected = {**raw, "issues": [{**raw["issues"][0], "evidence": "answer = 1"}]}
            assert observation["response"] == expected
            assert observation["recovery"]["original_observation"] == old_row["observation"]
    report_path, _ = study.report_study(root, review_revision=revision)
    report = json.loads(report_path.read_text())
    assert report["independent_review"]["status"] == state["status"]
    if recoverable:
        assert report["independent_review"]["recovery"]["native_invocations"] == 0
        assert study_review.recover_review_citations(root, "fixed") == revision
        assert study_review.recover_review_citations(root, revision) == revision


@pytest.mark.parametrize(
    "severity,behavior,expected",
    [
        (None, "issues", "unknown"),
        ("P7", "issues", "unknown"),
        (None, "clean", "reviewed"),
        ("P1", "issues", "reviewed"),
    ],
)
def test_fixed_review_requires_valid_severity_without_imputing_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    severity: str | None,
    behavior: str,
    expected: str,
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root, severity=severity, behavior=behavior)
    path = study_review.review_study(root, revision="rated")
    state = json.loads(path.read_text())
    assert len(calls) == 2
    for row in state["records"]:
        assert row["status"] == expected
        observation = study._bound_document(root, row["observation"])
        assert (observation["response"] is None) == (expected == "unknown")
        if behavior == "clean":
            assert observation["response"]["issues"] == []


@pytest.mark.parametrize("revision", ["", ".", "..", "../escape", "name/child", "/absolute"])
def test_fixed_review_refuses_unsafe_names_before_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root)
    with pytest.raises(ValueError, match="single named directory"):
        study_review.review_study(root, revision=revision)
    assert not calls


def test_fixed_criteria_hash_is_shared_across_independent_studies_and_guidance_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashes = []
    for name in ("first", "second"):
        root = _existing_study(tmp_path / name, monkeypatch)
        guidance = tmp_path / name / f"{name}-standards.md"
        guidance.write_text("Same frozen review standards\n")
        _judge(monkeypatch, root, behavior="clean")
        path = study_review.review_study(root, revision=name, guidance=[guidance])
        state = json.loads(path.read_text())
        hashes.append(study._bound_document(root, state["protocol"])["criteria_sha256"])
    assert hashes[0] == hashes[1]


@pytest.mark.parametrize("target", ["guidance", "criteria", "run", "execution"])
def test_fixed_review_rejects_tampered_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    guidance = tmp_path / "common.md"
    guidance.write_text("Common fixed rules\n")
    _judge(monkeypatch, root, behavior="clean")
    path = study_review.review_study(root, revision="fixed", guidance=[guidance])
    protocol = study._bound_document(root, json.loads(path.read_text())["protocol"])
    if target == "guidance":
        edited = root / protocol["guidance"][0]["path"]
    elif target == "criteria":
        edited = root / protocol["criteria"]["path"]
    elif target == "execution":
        run = json.loads((root / "run.json").read_text())
        stages = next(iter(run["executions"].values()))
        edited = root / next(iter(stages.values()))["path"]
    else:
        edited = root / "run.json"
    edited.write_text(edited.read_text() + "\n")
    with pytest.raises(ValueError, match="changed"):
        study_review.validate_review_revision(root, "fixed")


def test_fixed_review_refuses_symlink_parent_and_unversioned_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root)
    guidance = tmp_path / "criteria.md"
    guidance.write_text("Fixed rules")
    with pytest.raises(ValueError, match="requires a named"):
        study_review.review_study(root, guidance=[guidance])
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "review-revisions").symlink_to(outside)
    with pytest.raises(ValueError, match="real directory"):
        study_review.review_study(root, revision="fixed")
    assert not calls


def test_fixed_review_validator_refuses_active_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    _judge(monkeypatch, root, behavior="clean")
    path = study_review.review_study(root, revision="fixed")
    state = json.loads(path.read_text())
    state["status"] = "running"
    study._write(path, state)
    with pytest.raises(ValueError, match="still active"):
        study_review.validate_review_revision(root, "fixed")
    assert study_review.validate_review_revision(root, "fixed", allow_active=True) == path


def test_fixed_review_aborts_further_calls_when_guidance_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    guidance = tmp_path / "common.md"
    guidance.write_text("Original standards\n")
    calls = _judge(monkeypatch, root, behavior="clean")
    native = codex.invoke

    def tamper(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        result = native(area=area, **kwargs)
        (root / "review-revisions/fixed/guidance/0.md").write_text("Changed rules\n")
        return result

    monkeypatch.setattr(codex, "invoke", tamper)
    with pytest.raises(ValueError, match="changed"):
        study_review.review_study(root, revision="fixed", guidance=[guidance])
    state = json.loads((root / "review-revisions/fixed/review-state.json").read_text())
    assert len(calls) == state["native_invocations"] == 1
    assert state["status"] == "incomplete"
    assert sorted(row["status"] for row in state["records"]) == ["pending", "unknown"]
    row = next(row for row in state["records"] if row["status"] == "unknown")
    observation = study._bound_document(root, row["observation"])
    assert observation["status"] == "unknown" and observation["response"] is None
    assert observation["execution"]["usage"]["output_tokens"] == 7
    assert observation["execution"]["successful_native_completion"] is True


def test_parallel_fixed_reviews_overlap_and_keep_anonymous_packet_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    root = _existing_study(tmp_path, monkeypatch)
    criteria = tmp_path / "criteria.md"
    criteria.write_text("Report actionable correctness and maintainability findings.\n")
    _judge(monkeypatch, root, behavior="none")
    native = codex.invoke
    gate, lock = Barrier(2), Lock()
    release = Event()
    started: list[str] = []
    finished: list[str] = []

    def invoke(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        packet_id = json.loads((area / "workspace/packet.json").read_text())["packet_id"]
        with lock:
            ordinal = len(started)
            started.append(packet_id)
        gate.wait(timeout=5)
        if ordinal == 0:
            assert release.wait(timeout=5)
        result = native(area=area, **kwargs)
        with lock:
            finished.append(packet_id)
        if ordinal == 1:
            release.set()
        return result

    monkeypatch.setattr(codex, "invoke", invoke)
    path = study_review.review_study(
        root,
        revision="parallel",
        guidance=[criteria],
        jobs=2,
    )
    state = json.loads(path.read_text())
    assert state["status"] == "complete" and state["jobs"] == 2
    assert len(started) == state["native_invocations"] == state["assigned"] == 2
    assert finished == list(reversed(started))
    assert len(set(started)) == 2
    original = json.loads((root / "assessment-input.json").read_text())["records"]
    assert [row["id"] for row in state["records"]] == [row["id"] for row in original]
    for row in state["records"]:
        receipt = study._bound_document(root, row["native_start"])
        assert receipt["packet_id"] == row["packet_id"]
        observation = study._bound_document(root, row["observation"])
        assert observation["execution"]["usage"]["output_tokens"] == 7
        assert observation["response"]["packet_id"] == row["packet_id"]
    assert study_review.validate_review_revision(root, "parallel") == path


def test_parallel_judge_interrupt_keeps_active_observations_costs_and_unstarted_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    from pluginbench import parallel

    root = _existing_study(tmp_path, monkeypatch, repeats=2)
    original = (root / "run.json").read_bytes()
    criteria = tmp_path / "criteria.md"
    criteria.write_text("Use the same fixed review criteria for every output.\n")
    _judge(monkeypatch, root, behavior="none")
    native = codex.invoke
    gate, lock = Barrier(2), Lock()
    cancellation = Event()
    monkeypatch.setattr(parallel, "Event", lambda: cancellation)
    started: list[str] = []

    def invoke(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        packet_id = json.loads((area / "workspace/packet.json").read_text())["packet_id"]
        with lock:
            ordinal = len(started)
            started.append(packet_id)
        gate.wait(timeout=5)
        if ordinal == 0:
            (area / "artifacts").mkdir()
            (area / "artifacts/execution.json").write_text(
                json.dumps(
                    {
                        "usage": {"input_tokens": 30, "output_tokens": 7},
                    }
                )
            )
            raise KeyboardInterrupt("offline judge interrupt")
        assert cancellation.wait(timeout=5)
        return native(area=area, **kwargs)

    monkeypatch.setattr(codex, "invoke", invoke)
    with pytest.raises(KeyboardInterrupt, match="offline judge interrupt"):
        study_review.review_study(
            root, revision="interrupted-parallel", guidance=[criteria], jobs=2
        )
    path = root / "review-revisions/interrupted-parallel/review-state.json"
    state = json.loads(path.read_text())
    assert state["status"] == "interrupted" and state["native_invocations"] == len(started) == 2
    assert sum(row["status"] == "pending" for row in state["records"]) == 2
    measured = [row for row in state["records"] if row["packet_id"] in started]
    assert {row["status"] for row in measured} == {"reviewed", "interrupted"}
    assert all(
        study._bound_document(root, row["observation"])["execution"]["usage"]["output_tokens"] == 7
        for row in measured
    )
    assert (root / "run.json").read_bytes() == original
    assert study_review.validate_review_revision(root, "interrupted-parallel") == path


@pytest.mark.parametrize("jobs", [True, "2", 2.5, 0, 17])
def test_review_invalid_jobs_has_no_filesystem_effects(tmp_path: Path, jobs: int) -> None:
    with pytest.raises(ValueError, match="integer from 1 to 16"):
        study_review.review_study(tmp_path / "missing", revision="invalid", jobs=jobs)
    assert list(tmp_path.iterdir()) == []


def test_parallel_fixed_guidance_mutation_invalidates_active_observations_and_stops_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    from pluginbench import parallel

    root = _existing_study(tmp_path, monkeypatch, repeats=2)
    original = (root / "run.json").read_bytes()
    criteria = tmp_path / "criteria.md"
    criteria.write_text("Fixed standards\n")
    _judge(monkeypatch, root, behavior="none")
    native = codex.invoke
    gate, lock = Barrier(2), Lock()
    cancellation = Event()
    monkeypatch.setattr(parallel, "Event", lambda: cancellation)
    started: list[str] = []

    def invoke(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        packet_id = json.loads((area / "workspace/packet.json").read_text())["packet_id"]
        with lock:
            ordinal = len(started)
            started.append(packet_id)
        gate.wait(timeout=5)
        if ordinal == 1:
            assert cancellation.wait(timeout=5)
        result = native(area=area, **kwargs)
        if ordinal == 0:
            (root / "review-revisions/tampered/guidance/0.md").write_text("Changed rules\n")
        return result

    monkeypatch.setattr(codex, "invoke", invoke)
    with pytest.raises(ValueError, match="changed"):
        study_review.review_study(root, revision="tampered", guidance=[criteria], jobs=2)
    state = json.loads((root / "review-revisions/tampered/review-state.json").read_text())
    assert state["status"] == "incomplete" and state["native_invocations"] == len(started) == 2
    assert sorted(row["status"] for row in state["records"]) == [
        "pending",
        "pending",
        "unknown",
        "unknown",
    ]
    for row in state["records"]:
        if row["status"] != "unknown":
            continue
        observation = study._bound_document(root, row["observation"])
        assert observation["status"] == "unknown" and observation["response"] is None
        assert observation["execution"]["usage"]["output_tokens"] == 7
    assert (root / "run.json").read_bytes() == original


@pytest.mark.parametrize(
    "path,evidence,line,prefixed_source,recoverable",
    [
        ("after/src/answer.py", "answer = 1", 1, False, True),
        ("after/src/answer.py", "answer = 2", 1, False, False),
        ("after/src/answer.py", "answer = 1", 2, False, False),
        ("after/src/answer.py", "answer = 1", 1, True, False),
        ("after/after/src/answer.py", "answer = 1", 1, False, False),
        ("after//src/answer.py", "answer = 1", 1, False, False),
        ("after/../src/answer.py", "answer = 1", 1, False, False),
    ],
)
def test_fixed_path_prefix_recovery_requires_absent_original_and_exact_bound_citation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    evidence: str,
    line: int,
    prefixed_source: bool,
    recoverable: bool,
) -> None:
    root = _existing_study(tmp_path, monkeypatch, prefixed_source=prefixed_source)
    calls = _judge(
        monkeypatch,
        root,
        citation_path=path,
        citation_evidence=evidence,
        citation_line=line,
        severity="P2",
        additional_issue=True,
    )
    original_path = study_review.review_study(root, revision="prefix")
    original = json.loads(original_path.read_text())
    assert original["status"] == "incomplete"
    preserved = study._inventory(root)
    monkeypatch.setattr(
        codex, "invoke", lambda **kw: pytest.fail("offline recovery cannot invoke a model")
    )
    revision = study_review.recover_review_citations(root, "prefix")
    assert revision == ("prefix-citations" if recoverable else "prefix")
    state = json.loads(study_review.validate_review_revision(root, revision).read_text())
    assert state["status"] == ("complete" if recoverable else "incomplete")
    assert len(calls) == state["native_invocations"] == 2
    assert all(study._inventory(root)[key] == value for key, value in preserved.items())
    for row, old_row in zip(state["records"], original["records"], strict=True):
        assert row["native_start"] == old_row["native_start"]
        observation = study._bound_document(root, row["observation"])
        old = study._bound_document(root, old_row["observation"])
        assert observation["execution"] == old["execution"]
        if recoverable:
            raw = json.loads(old["execution"]["final"])
            expected = {
                **raw,
                "issues": [{**issue, "path": "src/answer.py"} for issue in raw["issues"]],
            }
            assert observation["response"] == expected
            assert len(expected["issues"]) == len(observation["recovery"]["citation_evidence"]) == 2
            proof = observation["recovery"]["citation_evidence"][0]
            assert proof["raw_path"] == path and proof["path"] == "src/answer.py"
            assert proof["raw_evidence"] == evidence == proof["extracted_quote"]
            assert proof["source_digest"] == row["submission_digest"]
    if recoverable:
        recovery = study._bound_document(root, state["recovery"])
        assert recovery["native_invocations"] == 0
        assert study_review.recover_review_citations(root, "prefix") == revision
        assert study_review.recover_review_citations(root, revision) == revision


@pytest.mark.parametrize("target", ["packet", "observation", "guidance"])
def test_path_prefix_recovery_refuses_changed_bound_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    guidance = tmp_path / "common.md"
    guidance.write_text("Use fixed standards.\n")
    _judge(
        monkeypatch,
        root,
        citation_path="after/src/answer.py",
        citation_evidence="answer = 1",
        severity="P2",
    )
    path = study_review.review_study(root, revision="prefix", guidance=[guidance])
    state = json.loads(path.read_text())
    row = state["records"][0]
    changed = {
        "packet": study._bound_path(root, row["packet"]).with_suffix("") / "after/src/answer.py",
        "observation": root / row["observation"]["path"],
        "guidance": path.parent / "guidance/0.md",
    }[target]
    changed.write_text(changed.read_text() + "\nchanged\n")
    monkeypatch.setattr(
        codex, "invoke", lambda **kw: pytest.fail("offline recovery cannot invoke a model")
    )
    with pytest.raises(ValueError, match="changed"):
        study_review.recover_review_citations(root, "prefix")
    assert not (root / "review-revisions/prefix-citations").exists()
