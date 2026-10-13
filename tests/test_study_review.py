from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, Unpack

import pytest
import yaml

from pluginbench import codex, harbor_backend, study, study_review
from test_study import _offline_harbor


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
                        "provenance": {"agent_image": "example@sha256:" + "a" * 64},
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
                "harness_version": "1.2.3",
            }
        )
    )
    _offline_harbor(monkeypatch)

    implementation_calls = 0

    def implement(*, area: Path, **kwargs: object) -> dict[str, object]:
        nonlocal implementation_calls
        implementation_calls += 1
        if interrupted:
            raise RuntimeError("offline interruption")
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        return {
            "submission_collected": True,
            "successful_native_completion": True,
            "elapsed_seconds": 1.0,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
        }

    monkeypatch.setattr(harbor_backend, "invoke", implement)
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
    severity: str | None = "P2",
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
                    "rationale": "Tests fixed review transport only",
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


def test_unsupported_review_artifacts_cannot_leak_host_data_or_count_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    secret = tmp_path / "operator-private.txt"
    secret.write_text("private operator content must never enter review evidence")
    calls = _judge(monkeypatch, root)
    native = codex.invoke

    def linked_review(*, area: Path, **kwargs: Unpack[NativeInvocationKwargs]) -> dict[str, object]:
        execution = native(area=area, **kwargs)
        (area / "artifacts/other-log").symlink_to(secret)
        return execution

    monkeypatch.setattr(codex, "invoke", linked_review)
    state = json.loads(study_review.review_study(root).read_text())
    assert len(calls) == 2 and state["status"] == "incomplete"
    for row in state["records"]:
        observation = study._bound_document(root, row["observation"])
        assert row["status"] == "unknown"
        assert observation["response"] is None
        assert observation["execution"]["usage"] is None
    for artifact in (root / "review-revisions/review").rglob("*"):
        if artifact.is_file() and not artifact.is_symlink():
            assert secret.read_bytes() not in artifact.read_bytes()


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
    report = json.loads(study.report_study(root, review_revision="review").read_text())
    assert len(report["attempts"]) == 2
    assert all(
        attempt["independent_review"]["finding_count"] is None for attempt in report["attempts"]
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
    state_path = study_review.review_study(root)
    state = json.loads(state_path.read_text())
    assert state["assigned"] == 2 and state["native_invocations"] == 0 and calls == []
    assert all(row["status"] == "unavailable" for row in state["records"])
    assert len(json.loads((root / "report/assessment-report.json").read_text())["attempts"]) == 2
    assert all(not row.get("packet") for row in state["records"])
    del state["records"][0]["submission_digest"]
    study._write(state_path, state)
    with pytest.raises(ValueError, match="source binding"):
        study_review.validate_review_revision(root, "review")


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
    assert calls == [] and not (root / "review-revisions/review").exists()


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
    with pytest.raises(ValueError, match="snapshot digest mismatch"):
        study_review.review_study(root)
    assert len(calls) == 1
    assert (root / "run.json").read_bytes() == original
    state = json.loads((root / "review-revisions/review/review-state.json").read_text())
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
    state = json.loads((root / "review-revisions/review/review-state.json").read_text())
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
    assert (root / "review-revisions/review/original-run.json").read_bytes() == original_run
    assert (root / "frozen-inventory.json").read_bytes() == freeze
    assert run["status"] == "complete" and run["actual_calls"] == 2
    report = json.loads(study.report_study(root, review_revision="review").read_text())
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
    state = json.loads((root / "review-revisions/review/review-state.json").read_text())
    assert state["status"] == "interrupted" and state["native_invocations"] == 0
    assert calls == [] and all(row["status"] == "pending" for row in state["records"])
    report = json.loads(study.report_study(root, review_revision="review").read_text())
    assert len(report["independent_review"]["records"]) == 2
    assert all(not row.get("packet") for row in state["records"])
    del state["records"][0]["stage_id"]
    study._write(root / "review-revisions/review/review-state.json", state)
    with pytest.raises(ValueError, match="source binding"):
        study_review.validate_review_revision(root, "review")


def test_fixed_review_preserves_source_and_uses_identical_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
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
    report_path = study.report_study(root, review_revision=revision)
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


@pytest.mark.parametrize(
    "target",
    [
        "guidance",
        "criteria",
        "run",
        "execution",
        "selections",
        *(
            f"{layer}-{field}-{fault}"
            for layer in ("row", "packet", "observation")
            for field in ("stage", "digest")
            for fault in ("missing", "wrong")
        ),
    ],
)
def test_fixed_review_rejects_tampered_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    guidance = tmp_path / "common.md"
    guidance.write_text("Common fixed rules\n")
    _judge(monkeypatch, root, behavior="clean")
    path = study_review.review_study(root, revision="fixed", guidance=[guidance])
    state = json.loads(path.read_text())
    protocol = study._bound_document(root, state["protocol"])
    if target == "selections" or target.startswith(("row-", "packet-", "observation-")):
        row = state["records"][0]
        if target == "selections":
            del protocol["selections"]
            protocol_path = root / state["protocol"]["path"]
            study._write(protocol_path, protocol)
            state["protocol"] = study._reference(root, protocol_path)
        else:
            layer, field, fault = target.split("-")
            reference = row.get(layer)
            document = row if layer == "row" else study._bound_document(root, reference)
            key = "stage_id" if field == "stage" else "submission_digest"
            if fault == "missing":
                del document[key]
            else:
                document[key] = "foreign-stage" if field == "stage" else "sha256:" + "f" * 64
            if layer != "row":
                document_path = root / reference["path"]
                study._write(document_path, document)
                row[layer] = study._reference(root, document_path)
                if layer == "packet":
                    observation = study._bound_document(root, row["observation"])
                    observation["packet"] = row["packet"]
                    observation_path = root / row["observation"]["path"]
                    study._write(observation_path, observation)
                    row["observation"] = study._reference(root, observation_path)
        study._write(path, state)
        with pytest.raises(ValueError, match="selection|binding|source"):
            study_review.validate_review_revision(root, "fixed")
        return
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


def test_fixed_review_refuses_symlink_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _existing_study(tmp_path, monkeypatch)
    calls = _judge(monkeypatch, root)
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
