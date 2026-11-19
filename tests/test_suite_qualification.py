from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from typing import Any

import pytest

from pluginbench import evaluation
from pluginbench.benchmark import ReviewerRule, Suite, SuiteTask
from pluginbench.suite_qualification import GATES, RULES, Policy, Receipt, Verdict, freeze


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _task(name: str, cluster: str) -> SuiteTask:
    return SuiteTask(
        content_sha256=_digest(f"content {name}"),
        repository_cluster=cluster,
        coding_contract_sha256=_digest("coding"),
        observer_profile_sha256=_digest(f"profile {name}"),
        mutation_timeout_seconds=600,
    )


REVIEWER = ReviewerRule(
    coding_harness="claude", reviewer_harness="codex", reviewer_model_id="gpt-6.2-sol",
    reviewer_effort="high", reviewer_cli_version="0.167.0", protocol_sha256=_digest("protocol"),
)
SUITE = Suite(
    tasks=(_task("a", "repo-a"), _task("b", "repo-b"), _task("c", "repo-c")),
    attempts=3,
    seed=7,
    observer_contract_sha256=_digest("observers"),
    reviewers=(REVIEWER,),
)
POLICY = Policy(
    required_measures=(
        "changed_line_coverage", "mutation_catch_rate", "complexity_added",
        "production_lines_added", "test_lines_added", "duplicated_test_lines", "review_findings",
    ),
    diagnostic_measures=("dead_code", "decision_line_coverage", "test_change_kill_share"),
    min_tasks=3,
    min_repositories=3,
    max_tasks_per_repository=2,
)
PASSING = {gate.name: (lambda receipt, task: Verdict(status="pass")) for gate in GATES}


def _receipt(task: SuiteTask, gate: str, **facts: Any) -> Receipt:
    pins = {
        "content_sha256": task.content_sha256,
        "coding_contract_sha256": task.coding_contract_sha256,
        "observer_profile_sha256": task.observer_profile_sha256,
        "observer_contract_sha256": SUITE.observer_contract_sha256,
    }
    binds = next(item.binds for item in GATES if item.name == gate)
    return Receipt(task=task.content_sha256, gate=gate, pins={name: pins[name] for name in binds},
                   producer="test", facts=facts)


def _all(suite: Suite = SUITE) -> list[Receipt]:
    return [_receipt(task, gate.name) for task in suite.tasks for gate in GATES]


def test_a_suite_whose_every_gate_passes_freezes() -> None:
    result = freeze(SUITE, _all(), POLICY, rules=PASSING)

    assert result.suite == SUITE and result.blockers == () and result.diagnostics == ()
    assert (result.tasks, result.repositories) == (3, 3)


@pytest.mark.parametrize("gate", [gate.name for gate in GATES])
def test_one_failed_gate_on_one_task_refuses_freeze_unless_it_is_diagnostic(gate: str) -> None:
    failed = SUITE.tasks[1]
    rules = {**PASSING, gate: lambda receipt, task: Verdict(
        status="pass" if task != failed else "fail", reason="probe contradicted it")}

    result = freeze(SUITE, _all(), POLICY, rules=rules)

    finding = {"task": failed.content_sha256, "gate": gate, "reason": "fail: probe contradicted it"}
    diagnostic = gate.removeprefix("measure/") in POLICY.diagnostic_measures
    # A diagnostic failure is reported and the suite still freezes; nothing else is dropped.
    assert (result.suite is None) is not diagnostic
    found = result.diagnostics if diagnostic else result.blockers
    assert [item.model_dump() for item in found] == [finding]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing", "no receipt"),
        ("duplicate", "2 receipts; expected one"),
        ("stale", "stale: produced against other coding_contract_sha256"),
        ("foreign", "receipt for a task not in the suite"),
    ],
)
def test_a_missing_duplicate_stale_or_foreign_receipt_refuses_freeze(change: str, reason: str) -> None:
    receipts = _all()
    correctness = next(r for r in receipts if r.gate == "correctness")
    if change == "missing":
        receipts.remove(correctness)
    elif change == "duplicate":
        receipts.append(correctness)
    elif change == "stale":
        receipts[receipts.index(correctness)] = correctness.model_copy(
            update={"pins": {**correctness.pins, "coding_contract_sha256": _digest("old")}})
    else:
        receipts.append(_receipt(_task("z", "repo-z"), "correctness"))

    result = freeze(SUITE, receipts, POLICY, rules=PASSING)

    assert result.suite is None and [b.reason for b in result.blockers] == [reason]


def test_a_mutation_timeout_set_from_measured_time_leaves_receipts_current() -> None:
    retimed = SUITE.model_copy(update={"tasks": tuple(
        task.model_copy(update={"mutation_timeout_seconds": 900}) for task in SUITE.tasks)})

    assert freeze(retimed, _all(), POLICY, rules=PASSING).suite == retimed


def test_a_profile_fix_makes_only_measurement_receipts_stale() -> None:
    fixed = SUITE.tasks[0].model_copy(update={"observer_profile_sha256": _digest("fixed")})
    suite = SUITE.model_copy(update={"tasks": (fixed, *SUITE.tasks[1:])})

    stale = {b.gate for b in freeze(suite, _all(), POLICY, rules=PASSING).blockers}

    assert "correctness" not in stale and "provenance_licence" not in stale
    assert {"changed_line_coverage", "mutation", "stability", "resources"} <= stale


def test_the_production_rules_refuse_every_suite_today() -> None:
    result = freeze(SUITE, _all(), POLICY)

    assert result.suite is None
    assert any(b.gate == "mutation" and "no qualification rule yet" in b.reason
               for b in result.blockers)


def test_skipped_measures_need_no_receipt() -> None:
    policy = POLICY.model_copy(update={"diagnostic_measures": ()})
    receipts = [r for r in _all() if r.gate.removeprefix("measure/") not in POLICY.diagnostic_measures]

    assert freeze(SUITE, receipts, policy, rules=PASSING).suite == SUITE


@pytest.mark.parametrize(
    ("clusters", "reason"),
    [
        (["repo-a", "repo-b", "repo-b"], "2 repositories; the policy needs 3"),
        (["repo-a", "repo-a", "repo-a"], "3 tasks from repo-a; the policy allows 2"),
    ],
)
def test_population_floors_and_the_repository_cap_refuse_freeze(
    clusters: list[str], reason: str
) -> None:
    suite = SUITE.model_copy(update={"tasks": tuple(
        task.model_copy(update={"repository_cluster": cluster})
        for task, cluster in zip(SUITE.tasks, clusters, strict=True))})

    result = freeze(suite, _all(suite), POLICY, rules=PASSING)

    assert result.suite is None and reason in [b.reason for b in result.blockers]


def test_fewer_tasks_than_the_floor_refuses_freeze() -> None:
    policy = POLICY.model_copy(update={"min_tasks": 4})

    assert "3 tasks; the policy needs 4" in [
        b.reason for b in freeze(SUITE, _all(), policy, rules=PASSING).blockers
    ]


def _reference(**changes: Any) -> dict[str, Any]:
    """Reference-agent rows as harbor_backend.run_reference_agents records them."""
    row = {"status": "complete", "cleanup": {"status": "complete"}, "exception_info": None}
    rows = {
        "oracle": {**row, "patch_bytes": 120, "verifier_result": {"rewards": {"reward": 1}}},
        "nop": {**row, "patch_bytes": 0, "verifier_result": {"rewards": {"reward": 0}}},
    }
    for path, value in changes.items():
        agent, key = path.split("__")
        rows[agent][key] = value
    return rows


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({}, "pass"),
        ({"oracle__patch_bytes": 0}, "fail"),
        ({"nop__verifier_result": {"rewards": {"reward": 1}}}, "fail"),
        ({"oracle__verifier_result": {"rewards": {"reward": 0}}}, "fail"),
        ({"oracle__verifier_result": {"rewards": {"reward": 0.5}}}, "unavailable"),
        ({"nop__status": "failed"}, "unavailable"),
        ({"nop__cleanup": {"status": "failed"}}, "unavailable"),
        ({"oracle__exception_info": {"type": "Timeout"}}, "unavailable"),
        ({"oracle__status": "not_configured", "oracle__reason": "no solve.sh"}, "unavailable"),
    ],
)
def test_correctness_is_computed_from_reference_agent_facts(
    changes: dict[str, Any], status: str
) -> None:
    task = SUITE.tasks[0]
    rows = _reference(**changes)

    verdict = RULES["correctness"](_receipt(task, "correctness", **rows), task)

    assert verdict.status == status
    # `pluginbench check` judges the same rows the same way.
    checked = evaluation._reference_agent_rows(rows)
    assert (status == "pass") is all(row["state"] == "ready" for row in checked.values())


def test_a_policy_refuses_unknown_or_doubly_listed_measures() -> None:
    with pytest.raises(ValueError, match="unknown measures: tokens"):
        Policy.model_validate({**POLICY.model_dump(), "required_measures": ["tokens"]})
    with pytest.raises(ValueError, match="listed twice"):
        Policy.model_validate({**POLICY.model_dump(), "diagnostic_measures": ["complexity_added"]})


def test_qualification_cannot_reach_any_paid_path() -> None:
    # Structural, not a spy: importing the module loads no launcher, ledger, operation,
    # coordinator or evaluation code, so nothing it does can start paid work.
    code = (
        "import json, sys; import pluginbench.suite_qualification; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('pluginbench'))))"
    )
    loaded = json.loads(subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout)

    assert loaded == ["pluginbench", "pluginbench.benchmark", "pluginbench.suite_qualification"]
